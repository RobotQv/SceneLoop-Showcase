"""Bounded shape download and explicit candidate journal; no Working writes."""
from __future__ import annotations
import json
import math
import os
import struct
import uuid

from .asset_contract import MAX_GLB_BYTES, _parse_glb, fixed_glb, instance_id, measured_rotation, transform
from .asset_jobs import controlled_path, file_facts
from .asset_provider import AssetJobError, artifact_source, canonical_json, identifier
from .tripo_transport import TripoError, chunks


def check_shape(raw):
    doc, _ = _parse_glb(raw)
    if doc.get("images"):
        raise AssetJobError("asset_shape_images_blocked")


def write_json_once(path, value):
    encoded = canonical_json(value).encode("utf-8")
    if len(encoded) > 1024 * 1024:
        raise AssetJobError("asset_candidate_record_limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as output:
        output.write(encoded)
        output.flush()
        os.fsync(output.fileno())


def download_glb(transport, url, root, job_id):
    identifier(job_id)
    relative = f".sceneloop/asset-jobs/{job_id}/incoming/{uuid.uuid4().hex}"
    partial = controlled_path(root, relative + ".partial")
    complete = controlled_path(root, relative + ".glb")
    partial.parent.mkdir(parents=True, exist_ok=True)
    # The empty/partial artifact remains on every failed transfer. It cannot
    # satisfy LocalArtifact/raw_available and is never overwritten or adopted.
    with partial.open("xb") as output:
        with transport.open("GET", url, headers={"Accept": "model/gltf-binary"}, timeout=60) as response:
            if response.status != 200:
                raise TripoError("tripo_download_http", status=response.status)
            try:
                for block in chunks(response, MAX_GLB_BYTES):
                    output.write(block)
            finally:
                output.flush()
                os.fsync(output.fileno())
    if partial.stat().st_size == 0:
        raise TripoError("tripo_empty_artifact")
    # Exclusive final copy avoids platform-dependent rename overwrite behavior.
    # The original transfer file is retained for provenance and manual cleanup.
    with partial.open("rb") as source, complete.open("xb") as output:
        while block := source.read(65536):
            output.write(block)
        output.flush()
        os.fsync(output.fileno())
    return relative + ".glb"


def workspace_facts(path):
    return {name: file_facts(controlled_path(path, name), 512 * 1024 * 1024)[1]
            for name in ("scene.blend", "plan.json")}


def materialized_pose(value):
    """Exact existing Blender storage mapping; no tolerance or request change."""
    pose = transform(value)
    def binary32(value): return struct.unpack("<f", struct.pack("<f", value))[0]
    return {"position": [binary32(v) for v in pose["position"]],
            "scale": [binary32(v) for v in pose["scale"]],
            "rotation": measured_rotation([math.degrees(binary32(math.radians(v))) for v in pose["rotation"]])}


def candidate_row(target, asset_id):
    plan = json.loads(file_facts(controlled_path(target, "plan.json"), 2 * 1024 * 1024)[0])
    return next((row for row in plan.get("assets", {}).get("instances", []) if row.get("instance_id") == asset_id), None)


def import_failure_receipt(coordinator, job_id, operation_id):
    """Read a bounded immutable failure and its original request; never import."""
    relative = f'.sceneloop/asset-jobs/{identifier(job_id)}/candidates/{identifier(operation_id)}'
    paths = [controlled_path(coordinator.root,relative+'/'+name) for name in ('pending.json','failure.json')]
    if any(not p.is_file() or p.stat().st_size > 1024*1024 for p in paths):
        return None
    pending, failure = [json.loads(p.read_text(encoding='utf-8')) for p in paths]
    if (pending.get('job_id') != job_id or pending.get('operation_id') != operation_id
            or failure != {'outcome':'rejected','error':'asset_candidate_import_failed'}):
        return None
    return pending


def import_ready_asset(coordinator, job_id, adapter, operation_id, source_relative, target_relative,
                       asset_id, pose=None):
    """One explicitly requested candidate import. Unknown writes never replay."""
    identifier(operation_id)
    view = coordinator.read(job_id)  # Recheck ready bytes with production authority.
    if view["preparation"] != "ready":
        raise AssetJobError("asset_candidate_requires_ready")
    job = coordinator.store.read(job_id)
    raw = job["raw_artifact"]
    path = controlled_path(coordinator.root, raw["path"])
    content, facts = fixed_glb(path)
    check_shape(content)
    if facts != {key: raw[key] for key in ("sha256", "byte_size")} or raw["source"] != artifact_source(job, **facts):
        raise AssetJobError("asset_artifact_origin_conflict")
    source = controlled_path(coordinator.root, source_relative)
    target = controlled_path(coordinator.root, target_relative)
    if source == target or source in target.parents or target in source.parents:
        raise AssetJobError("asset_candidate_not_isolated")
    request = {"schema": 1, "job_id": job_id, "operation_id": operation_id, "source": raw["source"],
               "source_workspace": source_relative, "source_files": workspace_facts(source),
               "target_workspace": target_relative, "instance_id": instance_id(asset_id), "transform": transform(pose)}
    relative = f".sceneloop/asset-jobs/{job_id}/candidates/{operation_id}"
    pending = controlled_path(coordinator.root, relative + "/pending.json")
    receipt = controlled_path(coordinator.root, relative + "/receipt.json")
    if pending.exists():
        try:
            prior = json.loads(file_facts(pending, 1024 * 1024)[0])
            if prior != request:
                raise AssetJobError("asset_candidate_request_conflict")
            if receipt.exists():
                saved = json.loads(file_facts(receipt, 1024 * 1024)[0])
                row = candidate_row(target, request["instance_id"])
                if (saved["request"] != request or saved["candidate_files"] != workspace_facts(target)
                        or saved["result"]["asset"]["source"] != facts or saved["result"]["asset"] != row
                        or row.get("transform") != materialized_pose(request["transform"])):
                    raise AssetJobError("asset_candidate_receipt_integrity")
                return saved["result"]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise AssetJobError("asset_candidate_outcome_unknown") from None
        raise AssetJobError("asset_candidate_outcome_unknown")
    if target.exists() and any(target.iterdir()):
        raise AssetJobError("asset_candidate_target_exists")
    # Admission for a NEW operation. History above remains readable while
    # closed; a stop after this point does not discard an in-flight receipt.
    coordinator._active(coordinator.store.read(job_id))
    try:
        write_json_once(pending, request)
    except FileExistsError:
        raise AssetJobError("asset_candidate_outcome_unknown") from None
    try:
        result = adapter.import_asset(source, target, path, request["instance_id"], request["transform"])
        if (result["asset"]["source"] != facts or result["asset"]["instance_id"] != request["instance_id"]
                or result["asset"] != candidate_row(target, request["instance_id"])
                or result["asset"]["transform"] != materialized_pose(request["transform"])
                or workspace_facts(source) != request["source_files"]):
            raise AssetJobError("asset_candidate_receipt_integrity")
        write_json_once(receipt, {"request": request, "result": result, "candidate_files": workspace_facts(target)})
        return result
    except Exception as error:
        # Pending is immutable even if writing the outcome note fails. Neither
        # an empty directory nor a partially saved scene proves no write.
        try:
            failure = controlled_path(coordinator.root, relative + "/failure.json")
            write_json_once(failure, {"outcome": "rejected" if getattr(error, "outcome", None) == "rejected"
                                                       else "unknown", "error": "asset_candidate_import_failed"})
        except OSError:
            pass
        if isinstance(error, AssetJobError):
            raise
        raise AssetJobError("asset_candidate_outcome_unknown") from None
