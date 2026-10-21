"""Isolated Blender execution and the shared, data-only ScenePlan contract."""
from __future__ import annotations

import copy
from contextlib import contextmanager, ExitStack
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

try:
    from . import surface_contract
    from .geometry_contract import normalize_parts, geometry_cost, MAX_SCENE_VERTICES, MAX_SCENE_FACES
    from .geometry_cut_contract import MAX_CUTS_PER_BATCH, normalize_cut
    from .observation_contract import normalize_view_spec, validate_observation_metadata
    from .asset_contract import AssetError, fixed_glb, instance_id as asset_instance_id, registration as asset_registration, receipt as asset_receipt, inspection as asset_inspection, transform as asset_transform
except ImportError:  # Blender invokes the worker as a standalone script.
    import surface_contract
    from geometry_contract import normalize_parts, geometry_cost, MAX_SCENE_VERTICES, MAX_SCENE_FACES
    from geometry_cut_contract import MAX_CUTS_PER_BATCH, normalize_cut
    from observation_contract import normalize_view_spec, validate_observation_metadata
    from asset_contract import AssetError, fixed_glb, instance_id as asset_instance_id, registration as asset_registration, receipt as asset_receipt, inspection as asset_inspection, transform as asset_transform

SUPPORTED_KINDS = ("ground", "house", "tent", "tree", "rock", "table", "bench", "crate", "barrel", "fence", "campfire", "stairs", "wall", "box", "cylinder", "sphere", "compound")
PRIMITIVES = {"box", "cylinder", "sphere"}
HERE = Path(__file__).resolve().parent
DEFAULT_BRIDGE_DIR = HERE.parent / ".sceneloop" / "bridge"
_DISCOVERY_CACHE = {}


class BlenderError(RuntimeError):
    """Adapter/worker failure with an explicit execution outcome.

    ``outcome="rejected"`` means the host proved nothing was committed (pure
    validation, geometry refusal, guard). ``outcome="unknown"`` means the write
    may have happened (timeout, lost process/bridge, failure inside commit), so
    callers must stop instead of treating it as a correctable parameter error.
    ``outcome="not_scheduled"`` means the request never reached Blender.
    """

    code = "blender_error"
    outcome = "rejected"
    outcome_unknown = False

    def __init__(self, message, *, outcome=None, code=None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if outcome is not None:
            self.outcome = outcome
            self.outcome_unknown = outcome == "unknown"


@contextmanager
def render_execution_slot(engine, device):
    """Only measured Cycles CPU renders bypass the shared local GPU lease.

    Called beside bpy render after opening the actual scene. Saved Plan fields
    cannot assert that an imported scene uses CPU. This imports no model code.
    """
    if engine == "CYCLES" and device == "CPU":
        yield
        return
    with ExitStack() as stack:
        try:
            factory = importlib.import_module("sceneloop.hunyuan_process").gpu_slot
            stack.enter_context(factory())
        except Exception as exc:
            code = getattr(exc, "code", "local_gpu_lock_unavailable")
            if code not in {"local_gpu_busy", "local_gpu_lock_unavailable", "local_gpu_lock_platform_unsupported"}:
                code = "local_gpu_lock_unavailable"
            raise BlenderError("本地 GPU 执行槽暂不可用；本次渲染未启动。",
                               outcome="not_scheduled", code=code) from exc
        yield


_RESULT_REQUIRED = {
    "build": ("scene_path", "workspace", "inspection"),
    "build_render": ("scene_path", "workspace", "inspection"),
    "render": ("scene_path", "workspace", "inspection"),
    "inspect": ("scene_path", "workspace", "inspection"),
    "dependencies": ("source", "dependency_inspection"),
    "edit": ("scene_path", "plan_path", "inspection", "workspace"),
    "fork": ("scene_path", "plan_path", "inspection", "workspace"),
    # Two real rollback paths exist: the generated worker commit returns the full
    # commit result, while the file-based/imported rollback only reports the
    # restored files plus render staleness. Require the common truthful fields.
    "rollback": ("scene_path", "plan_path", "render_stale"),
    "rebind": ("scene_path", "plan_path", "inspection", "workspace"),
    "observe": ("image_path", "path", "metadata_path"),
    "geometry": ("source", "mesh"),
    "snapshot": ("path", "plan_path", "workspace"),
    "import": ("scene_path", "plan_path", "workspace"),
    "asset_import": ("scene_path", "plan_path", "workspace", "inspection", "asset"),
    "asset_transform": ("scene_path", "plan_path", "workspace", "inspection", "asset"),
}
_RESULT_OBJECT_FIELDS = {"inspection": dict, "source": dict, "mesh": dict, "observations": dict, "dependency_inspection": dict}
_RESULT_TEXT_FIELDS = {"scene_path", "plan_path", "inspection_path", "workspace", "path", "image_path",
                       "metadata_path", "main_path", "auxiliary_path"}
_RESULT_BOOL_FIELDS = {"render_stale"}


def _validated_result(action, response):
    """Strictly validate one worker/bridge success envelope; never guess success.

    A malformed success response means the scheduled write result cannot be
    confirmed, so it is classified unknown (stop, keep the pending candidate)
    instead of publishing a possibly non-existent edit.
    """
    if not isinstance(response, dict) or type(response.get("ok")) is not bool:
        raise BlenderError("Blender 响应缺少布尔 ok，无法确认执行结果；请人工核实候选。", outcome="unknown")
    if not response["ok"]:
        outcome = response.get("outcome")
        if outcome not in ("rejected", "unknown", "not_scheduled"):
            outcome = "unknown"
        code = response.get("code")
        if code not in {"local_gpu_busy", "local_gpu_lock_unavailable", "local_gpu_lock_platform_unsupported"}:
            code = None
        raise BlenderError(response.get("error") or "Blender 执行失败", outcome=outcome, code=code)
    result = response.get("result")
    if not isinstance(result, dict):
        raise BlenderError("Blender 成功响应的 result 不是对象，无法确认执行结果；请人工核实候选。", outcome="unknown")
    required = _RESULT_REQUIRED.get(action)
    if required is None:
        return result
    for field in required:
        if field not in result:
            raise BlenderError(f"Blender 成功响应缺少 {field}，不符合 {action} 协议；请人工核实候选。",
                               outcome="unknown")
    # Known fields are type-checked whenever they appear, so the looser rollback
    # contract still rejects wrong types in optional fields.
    for field, value in result.items():
        expected = _RESULT_OBJECT_FIELDS.get(field)
        if expected is not None and not isinstance(value, expected):
            raise BlenderError(f"Blender 成功响应的 {field} 类型无效；请人工核实候选。", outcome="unknown")
        if field in _RESULT_TEXT_FIELDS and (not isinstance(value, str) or not value):
            raise BlenderError(f"Blender 成功响应的 {field} 不是有效文字；请人工核实候选。", outcome="unknown")
        if field in _RESULT_BOOL_FIELDS and type(value) is not bool:
            raise BlenderError(f"Blender 成功响应的 {field} 不是布尔值；请人工核实候选。", outcome="unknown")
    if action in {"asset_import", "asset_transform"}:
        try:
            asset_receipt(result["asset"])
            measured = result["inspection"].get("assets")
            matched = [row for row in measured if isinstance(row, dict) and row.get("instance_id") == result["asset"]["instance_id"]] if isinstance(measured,list) else []
            if len(matched) != 1:
                raise AssetError("Asset receipt has no matching actual inspection")
            asset_inspection(matched[0], result["asset"])
        except (AssetError, KeyError, TypeError, AttributeError) as error:
            raise BlenderError(f"Invalid asset success receipt: {error}", outcome="unknown") from None
    return result


def _file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _vector(value, label, *, positive=False):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise BlenderError(f"{label} 必须包含三个数值")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > 10000 or (positive and v <= 0) for v in value):
        raise BlenderError(f"{label} 包含无效数值")
    return [float(v) for v in value]


def validate_plan(plan):
    """Return a validated copy; never silently substitute unsupported key objects."""
    if not isinstance(plan, dict) or plan.get("schema_version") != 1:
        raise BlenderError("ScenePlan.schema_version 必须为 1")
    if "scene_kind" in plan:
        raise BlenderError("ScenePlan 不能声明 scene_kind；现有文件必须通过导入接口产生清单")
    result = copy.deepcopy(plan)
    objects = result.get("objects")
    if not isinstance(objects, list) or not 1 <= len(objects) <= 32:
        raise BlenderError("ScenePlan 需要 1–32 个语义物体")
    for key in ("title", "description"):
        if not isinstance(result.get(key, ""), str):
            raise BlenderError(f"{key} 必须为文字")
    assumptions = result.setdefault("assumptions", [])
    if not isinstance(assumptions, list) or any(not isinstance(x, str) for x in assumptions):
        raise BlenderError("assumptions 必须为文字列表")
    ids = set()
    for item in objects:
        if not isinstance(item, dict):
            raise BlenderError("objects 必须为对象列表")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", item_id) or item_id in ids:
            raise BlenderError("每个物体需要唯一的英文字母、数字、下划线或短横线 id")
        ids.add(item_id)
        kind = item.get("kind")
        if kind not in SUPPORTED_KINDS:
            raise BlenderError(f"尚未实现物体类型：{kind}；请重新规划或使用 compound")
        if not isinstance(item.get("key_object", False), bool):
            raise BlenderError(f"{item_id}.key_object 必须为布尔值")
        if item.get("key_object") and kind in PRIMITIVES:
            raise BlenderError(f"关键物体 {item_id} 不能仅使用基础几何体")
        item.setdefault("name", item_id)
        if not isinstance(item["name"], str):
            raise BlenderError("物体 name 必须为文字")
        item["position"] = _vector(item.get("position"), f"{item_id}.position")
        item["dimensions"] = _vector(item.get("dimensions"), f"{item_id}.dimensions", positive=True)
        item["rotation"] = _vector(item.get("rotation", [0, 0, 0]), f"{item_id}.rotation")
        item["scale"] = _vector(item.get("scale", [1, 1, 1]), f"{item_id}.scale", positive=True)
        if any(item["dimensions"][i] * item["scale"][i] > 100 for i in range(3)):
            raise BlenderError(f"{item_id} 缩放后的尺寸不能超过 100 米")
        features = item.setdefault("features", [])
        if not isinstance(features, list) or any(not isinstance(x, str) for x in features):
            raise BlenderError(f"{item_id}.features 必须为文字列表")
        if kind == "compound":
            try:
                item["parts"] = normalize_parts(item.get("parts"))
            except ValueError as error:
                raise BlenderError(f"object_id={item_id}: {error}") from None
    costs = [geometry_cost(part) for item in objects if item["kind"] == "compound" for part in item["parts"]]
    if sum(cost[0] for cost in costs) > MAX_SCENE_VERTICES or sum(cost[1] for cost in costs) > MAX_SCENE_FACES:
        raise BlenderError("ScenePlan 部件总量超过 50000 顶点 / 100000 面的生成上限")
    camera = result.get("camera")
    if not isinstance(camera, dict):
        raise BlenderError("需要 camera.position 与 camera.target")
    camera["position"] = _vector(camera.get("position"), "camera.position")
    camera["target"] = _vector(camera.get("target"), "camera.target")
    if sum((a - b) ** 2 for a, b in zip(camera["position"], camera["target"])) < .01:
        raise BlenderError("相机位置不能与观察点重合")
    lens = camera.setdefault("lens", 50)
    if isinstance(lens, bool) or not isinstance(lens, (int, float)) or not math.isfinite(lens) or not 10 <= lens <= 200:
        raise BlenderError("camera.lens 必须在 10–200mm 范围内")
    if "assets" in result:
        try:
            result["assets"] = asset_registration(result["assets"], ids)
        except AssetError as error:
            raise BlenderError(str(error)) from None
    if "render_profile" in result and result["render_profile"] != "showcase_v1":
        raise BlenderError("Only the frozen showcase_v1 initial render profile is supported")
    if "surface" in result:
        try:
            result["surface"] = surface_contract.normalize(result["surface"], result)
        except surface_contract.SurfaceError as error:
            raise BlenderError(str(error)) from None
    return result


EDITABLE_FIELDS = {"position", "rotation", "scale", "dimensions", "kind", "parts", "name", "features", "key_object"}


def apply_edit_plan(plan, edit_plan):
    """Validate bounded semantic edits and return a new normalized ScenePlan."""
    result = validate_plan(plan)
    if not isinstance(edit_plan, dict) or set(edit_plan) - {"operations", "reason"}:
        raise BlenderError("EditPlan 只支持 operations 和 reason")
    if "reason" in edit_plan and (not isinstance(edit_plan["reason"], str) or len(edit_plan["reason"]) > 4000):
        raise BlenderError("EditPlan.reason 必须为不超过 4000 字符的文字")
    operations = edit_plan.get("operations")
    if not isinstance(operations, list) or not 1 <= len(operations) <= 16:
        raise BlenderError("每次编辑需要 1–16 个操作")
    cut_count = sum(1 for item in operations if isinstance(item, dict) and item.get("op") == "cut_box")
    if cut_count > MAX_CUTS_PER_BATCH:
        raise BlenderError(f"每批 cut_box 最多 {MAX_CUTS_PER_BATCH} 个")
    for operation in operations:
        if not isinstance(operation, dict):
            raise BlenderError("编辑操作必须为对象")
        op = operation.get("op")
        if not isinstance(op, str):
            raise BlenderError("编辑 op 必须为文字")
        if op in surface_contract.OPS:
            try:
                result = surface_contract.apply(result, operation)
            except surface_contract.SurfaceError as error:
                raise BlenderError(str(error)) from None
            continue
        required = {"add": {"op", "object"}, "update": {"op", "id", "changes"}, "remove": {"op", "id"},
                    "add_part": {"op", "id", "part"}, "update_part": {"op", "id", "part_id", "changes"},
                    "remove_part": {"op", "id", "part_id"}}.get(op)
        if op == "cut_box":
            # Pure request validation only: nominal plan geometry is unchanged and
            # whether the actual mesh can be cut is decided by the Blender runtime.
            try:
                request = normalize_cut(operation)
            except ValueError as error:
                raise BlenderError(str(error)) from None
            result = validate_plan(result)
            by_id = {item["id"]: item for item in result["objects"]}
            item = by_id.get(request["id"])
            if item is None:
                raise BlenderError("cut 目标 id 不存在")
            if item["kind"] != "compound":
                raise BlenderError("cut_box 只支持 compound 物体中的已登记部件")
            if not any(part["id"] == request["part_id"] for part in item["parts"]):
                raise BlenderError("cut 部件 part_id 不存在")
            continue
        if required is None or set(operation) != required:
            raise BlenderError("仅支持 add/update/remove、部件编辑、cut_box、assign_surface 和 configure_lighting")
        # Each operation addresses the canonical state produced by its predecessor.
        # This also matches the worker: transient invalid object/part counts are rejected.
        result = validate_plan(result)
        by_id = {item["id"]: item for item in result["objects"]}
        if op == "add":
            item = operation["object"]
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] in by_id:
                raise BlenderError("新增物体必须具有未占用的稳定 id")
            result["objects"].append(copy.deepcopy(item))
            continue
        identifier = operation["id"]
        if not isinstance(identifier, str) or identifier not in by_id:
            raise BlenderError("编辑目标 id 不存在")
        if op in {"add_part", "update_part", "remove_part"}:
            item = by_id[identifier]
            if item["kind"] != "compound":
                raise BlenderError("部件编辑要求 compound 物体；其他类型需显式转换")
            parts = item["parts"]
            if op == "add_part":
                part = operation["part"]
                if not isinstance(part, dict) or not isinstance(part.get("id"), str) or any(p["id"] == part["id"] for p in parts):
                    raise BlenderError("新增部件需要未占用的稳定 id")
                parts.append(copy.deepcopy(part))
                continue
            part_id = operation["part_id"]
            part = next((p for p in parts if p["id"] == part_id), None) if isinstance(part_id, str) else None
            if part is None:
                raise BlenderError("部件 part_id 不存在")
            if op == "remove_part":
                item["parts"] = [p for p in parts if p["id"] != part_id]
                surface_contract.prune_removed(result, operation)
            else:
                changes = operation["changes"]
                if not isinstance(changes, dict) or not changes or "id" in changes:
                    raise BlenderError("部件 changes 不能为空或修改 id")
                # A shape change is explicit geometry replacement; discard only the former shape's data.
                if "shape" in changes and changes["shape"] != part["shape"]:
                    for field in ("dimensions", "vertices", "faces", "profile", "depth", "points", "radius", "sides"):
                        part.pop(field, None)
                part.update(copy.deepcopy(changes))
            continue
        if op == "remove":
            result["objects"] = [item for item in result["objects"] if item["id"] != identifier]
            surface_contract.prune_removed(result, operation)
        else:
            changes = operation["changes"]
            if not isinstance(changes, dict) or not changes or set(changes) - EDITABLE_FIELDS:
                raise BlenderError("changes 仅支持物体的位置、旋转、缩放、尺寸、形状、部件、名称和特征；不能修改 id 或相机")
            by_id[identifier].update(copy.deepcopy(changes))
    return validate_plan(result)


def is_imported_manifest(value):
    return isinstance(value, dict) and value.get("scene_kind") == "imported"


def validate_imported_manifest(value):
    if not is_imported_manifest(value) or value.get("schema_version") != 1:
        raise BlenderError("需要 imported 场景清单 schema_version=1")
    if not isinstance(value.get("scene_name"), str) or not value["scene_name"]:
        raise BlenderError("导入清单缺少 scene_name")
    objects = value.get("objects")
    if not isinstance(objects, list):
        raise BlenderError("导入清单 objects 必须为列表")
    identifiers = set()
    for item in objects:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", item["id"]) or item["id"] in identifiers:
            raise BlenderError("导入物体需要唯一稳定 id")
        identifiers.add(item["id"])
        if not isinstance(item.get("editable_operations"), list) or any(not isinstance(op, str) or op not in {"update", "remove"} for op in item["editable_operations"]):
            raise BlenderError("导入物体编辑能力无效")
    validate_view_spec(value.get("camera"))
    return copy.deepcopy(value)


def validate_imported_edit_plan(manifest, edit_plan):
    """Return validated operations; imported geometry is never reconstructed."""
    current = validate_imported_manifest(manifest)
    if not isinstance(edit_plan, dict) or set(edit_plan) - {"operations", "reason"}:
        raise BlenderError("EditPlan 只支持 operations 和 reason")
    if "reason" in edit_plan and (not isinstance(edit_plan["reason"], str) or len(edit_plan["reason"]) > 4000):
        raise BlenderError("EditPlan.reason 必须为不超过4000字符的文字")
    operations = edit_plan.get("operations")
    if not isinstance(operations, list) or not 1 <= len(operations) <= 16:
        raise BlenderError("每次编辑需要1–16个操作")
    by_id = {item["id"]: item for item in current["objects"]}
    normalized = []
    generated = sum(bool(item.get("managed_template")) for item in by_id.values())
    for operation in operations:
        if not isinstance(operation, dict) or not isinstance(operation.get("op"), str):
            raise BlenderError("编辑操作必须包含文字 op")
        op = operation["op"]
        keys = {"add": {"op", "object"}, "update": {"op", "id", "changes"}, "remove": {"op", "id"}}.get(op)
        if keys is None or set(operation) != keys:
            raise BlenderError("导入场景仅支持 add/object、update/id/changes、remove/id")
        if op == "add":
            checked = validate_plan({"schema_version": 1, "objects": [operation["object"]], "camera": {"position": [10, -10, 8], "target": [0, 0, 0]}})["objects"][0]
            if checked["id"] in by_id:
                raise BlenderError("新增物体 id 已存在")
            generated += 1
            if generated > 32:
                raise BlenderError("导入场景最多新增32个参数化物体；原场景物体保留")
            by_id[checked["id"]] = {"id": checked["id"], "managed_template": True, "editable_operations": ["update", "remove"]}
            normalized.append({"op": "add", "object": checked})
            continue
        identifier = operation["id"]
        if not isinstance(identifier, str) or identifier not in by_id:
            raise BlenderError("编辑目标 id 不存在")
        item = by_id[identifier]
        if op not in item["editable_operations"]:
            reasons = "; ".join(item.get("readonly_reasons", []))
            raise BlenderError(f"物体 {identifier} 不允许 {op}：{reasons or '只读对象'}")
        if op == "remove":
            generated -= int(bool(item.get("managed_template")))
            del by_id[identifier]
            normalized.append({"op": op, "id": identifier})
            continue
        changes = operation["changes"]
        if not isinstance(changes, dict) or not changes or set(changes) - {"position", "rotation", "scale"}:
            raise BlenderError("导入物体仅可更新 position/rotation/scale；不能重建网格或修改形状")
        normalized.append({"op": op, "id": identifier, "changes": {field: _vector(value, f"{identifier}.{field}", positive=field == "scale") for field, value in changes.items()}})
    return {"operations": normalized, **({"reason": edit_plan["reason"]} if "reason" in edit_plan else {})}


def validate_scene_document(value):
    return validate_imported_manifest(value) if is_imported_manifest(value) else validate_plan(value)


def validate_view_spec(view_spec):
    try:
        return normalize_view_spec(view_spec)
    except ValueError as error:
        raise BlenderError(str(error)) from None


def observation_directory(workspace, output_id):
    if not isinstance(output_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", output_id):
        raise BlenderError("观察 output_id 仅允许字母、数字、下划线与短横线")
    workspace = Path(workspace).resolve()
    observations = workspace / "observations"
    # Never follow an observations junction/symlink outside the candidate.
    if observations.resolve().parent != workspace:
        raise BlenderError("观察目录不能位于候选目录之外")
    path = observations / output_id
    if path.exists() or path.is_symlink():
        raise BlenderError("观察 output_id 已使用，不能覆盖旧截图")
    return path


def replace_candidate_files(workspace, staged, names=None):
    """Commit the scene/plan together; restore both on ordinary I/O failure.

    This is an exception-safe multi-file transaction, not a cross-file atomic
    filesystem primitive. Immutable revisions retain the previous candidate if
    a process or machine dies during commit. ``names`` limits the replacement
    to the listed products (identity rebinding must not delete live previews).
    """
    workspace, staged = Path(workspace).resolve(), Path(staged).resolve()
    names = tuple(names) if names is not None else ("scene.blend", "plan.json", "inspection.json", "main.png", "auxiliary.png")
    if not all((staged / name).is_file() for name in ("scene.blend", "plan.json")):
        raise BlenderError("候选事务缺少场景或计划")
    backup = workspace / f".transaction-{uuid.uuid4().hex}"
    backup.mkdir()
    changed = []
    try:
        for name in names:
            if (workspace / name).exists():
                shutil.copy2(workspace / name, backup / name)
        for name in names:
            target = workspace / name
            if (staged / name).is_file():
                os.replace(staged / name, target)
            else:
                target.unlink(missing_ok=True)
            changed.append(name)
    except Exception:
        for name in reversed(changed):
            if (backup / name).exists():
                os.replace(backup / name, workspace / name)
            else:
                (workspace / name).unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(backup, ignore_errors=True)


def ground_warnings(rows, plan):
    """Conservative AABB warnings, only for unrotated flat ground objects."""
    ground_ids = {item["id"] for item in plan["objects"] if item["kind"] == "ground" and all(abs(float(angle) % 360) < .0001 for angle in item.get("rotation", (0, 0, 0)))}
    grounds = [row for row in rows if row["id"] in ground_ids]
    warnings = []
    for row in rows:
        if row["kind"] == "ground":
            continue
        lo, hi = row["bounds"]
        for ground in grounds:
            floor_lo, floor_hi = ground["bounds"]
            overlaps = all(lo[axis] < floor_hi[axis] - .001 and hi[axis] > floor_lo[axis] + .001 for axis in (0, 1))
            depth = floor_hi[2] - lo[2]
            if overlaps and depth > .005 and hi[2] > floor_lo[2]:
                warnings.append({"code": "ground_sink", "object_id": row["id"], "ground_id": ground["id"], "depth": round(depth, 5), "ground_top": floor_hi[2], "message": "物体包围盒底部低于重叠地台顶面；可能被地台遮挡，请结合截图判断", "method": "axis_aligned_bounds"})
    return warnings


def discover_blender(explicit_path=None):
    cache_key = (str(explicit_path or ""), os.environ.get("BLENDER_PATH", ""), os.environ.get("PATH", ""))
    cached = _DISCOVERY_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] < 60:
        return copy.deepcopy(cached[1])
    candidates = []
    if explicit_path:
        candidates.append(Path(explicit_path).expanduser())
    elif os.environ.get("BLENDER_PATH"):
        candidates.append(Path(os.environ["BLENDER_PATH"]).expanduser())
    else:
        found = shutil.which("blender")
        if found:
            candidates.append(Path(found))
        if os.name == "nt":
            roots = [Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Blender Foundation"]
            roots += [Path(f"{d}:/Blender Foundation") for d in "CDEFG"]
            for root in roots:
                if root.is_dir():
                    candidates.extend(sorted(root.glob("Blender*/blender.exe"), reverse=True))
        else:
            candidates += [Path("/Applications/Blender.app/Contents/MacOS/Blender"), Path("/usr/bin/blender"), Path("/snap/bin/blender")]
    for candidate in candidates:
        if candidate.is_dir():
            candidate /= "blender.exe" if os.name == "nt" else "blender"
        if not candidate.is_file():
            continue
        try:
            process = subprocess.run([str(candidate), "--version"], capture_output=True, text=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if process.returncode == 0:
                result = {"available": True, "path": str(candidate.resolve()), "version": process.stdout.splitlines()[0], "error": None}
                _DISCOVERY_CACHE[cache_key] = (time.monotonic(), result)
                return copy.deepcopy(result)
        except (OSError, subprocess.TimeoutExpired):
            continue
    result = {"available": False, "path": None, "version": None, "error": "未发现可运行的 Blender；设置 BLENDER_PATH 或提供可执行文件路径"}
    _DISCOVERY_CACHE[cache_key] = (time.monotonic(), result)
    return copy.deepcopy(result)


def bridge_status(bridge_dir=None):
    path = Path(bridge_dir or os.environ.get("SCENELOOP_BRIDGE_DIR", DEFAULT_BRIDGE_DIR)).resolve()
    try:
        data = json.loads((path / "status.json").read_text(encoding="utf-8"))
        data["connected"] = time.time() - float(data["heartbeat"]) < (360 if data.get("busy") else 8)
        data["bridge_dir"] = str(path)
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return {"connected": False, "bridge_dir": str(path), "message": "请先在 Blender 中运行 blender_bridge.py"}


class BlenderAdapter:
    mode = "headless"

    def __init__(self, blender_path=None, bridge_dir=None, timeout=300):
        self.discovery = discover_blender(blender_path)
        self.timeout = timeout

    @property
    def capabilities(self):
        return {"mode": self.mode, "available": self.discovery["available"], "reason": self.discovery.get("error"), "supported_kinds": list(SUPPORTED_KINDS), "new_scene": True, "render": True, "inspect": True, "snapshot": True, "rollback": True, "apply_to_source": False, "arbitrary_python": False, "candidate_edit": True, "import_scene": True, "imported_scene_edit": True, "observe": True, "max_objects": 32, "max_edit_operations": 16, "incremental_edit": True, "part_edit": True, "cut_box": True, "max_cut_operations": MAX_CUTS_PER_BATCH, "part_shapes": ["box", "cylinder", "sphere", "cone", "mesh", "extrusion", "tube"], "custom_camera_roll": True,
                "asset_import": self.mode == "headless", "asset_transform": self.mode == "headless",
                "surface_edit": True, "surface_recipes": list(surface_contract.RECIPES), "lighting_edit": True}

    def create_workspace(self, run_dir):
        path = Path(run_dir).resolve()
        path.mkdir(parents=True, exist_ok=True)
        if (path / "scene.blend").exists() or (path / "plan.json").exists():
            raise BlenderError("候选目录已有结果，请创建新的运行目录")
        return path

    def _asset_target(self, source_workspace, target_workspace):
        if self.mode != "headless":
            raise BlenderError("Asset candidate operations support Headless only; Live has not been verified")
        source, target = Path(source_workspace).resolve(), Path(target_workspace).resolve()
        if source == target or source.is_relative_to(target) or target.is_relative_to(source):
            raise BlenderError("Asset candidate must be isolated from its source")
        if not (source / "scene.blend").is_file() or not (source / "plan.json").is_file():
            raise BlenderError("Asset source requires a frozen parametric .blend and Plan")
        try:
            validate_plan(json.loads((source / "plan.json").read_text(encoding="utf-8")))
        except (OSError, ValueError) as error:
            raise BlenderError(f"Invalid asset source Plan: {error}") from None
        if target.exists() and any(target.iterdir()):
            raise BlenderError("Asset target must be a new empty directory")
        return source, target

    def import_asset(self, source_workspace, target_workspace, glb_path, instance_id, transform=None):
        source, target = self._asset_target(source_workspace, target_workspace)
        try:
            identifier, pose = asset_instance_id(instance_id), asset_transform(transform)
            raw, identity = fixed_glb(glb_path)
        except (AssetError, OSError) as error:
            raise BlenderError(str(error)) from None
        target.mkdir(parents=True, exist_ok=True)
        fixed = target / (".asset-input-" + uuid.uuid4().hex + ".glb")
        try:
            fixed.write_bytes(raw)
            result = self._run("asset_import", target, source_workspace=str(source), asset_path=str(fixed),
                               asset_source=identity, instance_id=identifier, transform=pose, frozen=True)
            return self._asset_result(result, target, identifier, identity)
        finally:
            try:
                fixed.unlink(missing_ok=True)
            except OSError:
                # Ancillary cleanup cannot replace a proven write outcome.
                # Retain only this candidate's fixed input for manual inspection.
                pass

    def transform_asset(self, source_workspace, target_workspace, instance_id, changes):
        source, target = self._asset_target(source_workspace, target_workspace)
        try:
            identifier, value = asset_instance_id(instance_id), asset_transform(changes, partial=True)
        except AssetError as error:
            raise BlenderError(str(error)) from None
        target.mkdir(parents=True, exist_ok=True)
        rows = json.loads((source / "plan.json").read_text(encoding="utf-8")).get("assets", {}).get("instances", [])
        prior = next((row for row in rows if row["instance_id"] == identifier), None)
        if prior is None:
            raise BlenderError("Asset instance not registered in source candidate")
        result = self._run("asset_transform", target, source_workspace=str(source), instance_id=identifier, changes=value, frozen=True)
        return self._asset_result(result, target, identifier, prior["source"], prior["root_id"])

    @staticmethod
    def _asset_result(result, target, identifier, source, root_id=None):
        row = result.get("asset", {})
        if (row.get("instance_id") != identifier or row.get("source") != source or
                (root_id is not None and row.get("root_id") != root_id) or
                any(Path(result.get(k, "")).resolve() != target / name for k,name in
                    (("scene_path","scene.blend"),("plan_path","plan.json"))) or
                Path(result.get("workspace", "")).resolve() != target):
            raise BlenderError("Asset success identity does not match this request", outcome="unknown")
        return result

    def _run(self, action, workspace, **extra):
        if not self.discovery["available"]:
            raise BlenderError(self.discovery["error"], outcome="not_scheduled", code="blender_unavailable")
        workspace = Path(workspace).resolve()
        request = {"action": action, "workspace": str(workspace), **extra}
        request_path = workspace / f"request-{uuid.uuid4().hex}.json"
        response_path = workspace / f"response-{uuid.uuid4().hex}.json"
        request["response_path"] = str(response_path)
        try:
            request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            raise BlenderError("无法写入 Blender 请求；请求未发出，未执行任何写操作。",
                               outcome="not_scheduled", code="host_io_error") from exc
        command = [self.discovery["path"], "--background", "--factory-startup", "--disable-autoexec", "--python-exit-code", "1", "--python", str(HERE / "blender_worker.py"), "--", str(request_path)]
        try:
            process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=self.timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise BlenderError(f"Blender 运行超时（{self.timeout}秒），候选保留于 {workspace}；写结果未知，请人工核实候选。",
                               outcome="unknown", code="write_timeout") from exc
        except OSError as exc:
            raise BlenderError("无法启动 Blender 进程；请求未执行。",
                               outcome="not_scheduled", code="blender_launch_failed") from exc
        if not response_path.is_file():
            raise BlenderError(f"Blender 未返回结果，退出码 {process.returncode}；写结果未知，请人工核实候选与 blender.log。",
                               outcome="unknown")
        try:
            response = json.loads(response_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BlenderError("Blender 返回结果无法读取或解析；写结果未知，请人工核实候选与 blender.log。",
                               outcome="unknown") from exc
        log_error = None
        try:
            (workspace / "blender.log").write_text(process.stdout + "\n" + process.stderr, encoding="utf-8")
        except OSError as exc:
            # Diagnostics are ancillary: a successfully read worker response stays valid.
            log_error = str(exc)
        if process.returncode:
            # A worker may exit non-zero after writing its structured response; the
            # response itself decides rejection/unknown. Only an ok=true response
            # with a failed exit cannot be confirmed.
            result = _validated_result(action, response)
            raise BlenderError(f"Blender 退出码 {process.returncode}；写结果未知，请人工核实候选。", outcome="unknown")
        result = _validated_result(action, response)
        if log_error:
            result = {**result, "host_log_error": log_error}
        return result

    def execute_plan(self, plan, workspace):
        workspace = Path(workspace).resolve()
        normalized = validate_plan(plan)
        if (workspace / "scene.blend").exists():
            raise BlenderError("执行计划需要新的候选目录")
        (workspace / "plan.json").write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
        return self._run("build", workspace)

    def render(self, workspace):
        return self._run("render", workspace, frozen=True)

    def inspect(self, workspace=None):
        if workspace is None:
            return {"discovery": self.discovery, "capabilities": self.capabilities}
        return self._run("inspect", workspace, frozen=True)

    def inspect_dependencies(self, workspace):
        return self._run('dependencies',workspace,frozen=True)

    def import_scene(self, source_path, workspace, scene_name=None):
        source, workspace = Path(source_path).resolve(), Path(workspace).resolve()
        if not source.is_file() or source.suffix.lower() != ".blend":
            raise BlenderError("请选择现有 .blend 文件")
        if source.is_relative_to(workspace):
            raise BlenderError("导入来源不能位于新候选输出目录内")
        if scene_name is not None and (not isinstance(scene_name, str) or not scene_name or len(scene_name) > 256):
            raise BlenderError("scene_name 必须是文件内现有场景名")
        if workspace.exists() and any(workspace.iterdir()):
            raise BlenderError("导入需要新的空候选目录")
        workspace.mkdir(parents=True, exist_ok=True)
        return self._run("import", workspace, source_path=str(source), scene_name=scene_name, frozen=True)

    def fork_candidate(self, source_workspace, target_workspace):
        source, target = Path(source_workspace).resolve(), Path(target_workspace).resolve()
        if source == target or source.is_relative_to(target) or target.is_relative_to(source):
            raise BlenderError("新候选必须与源候选使用互不包含的独立目录")
        if not (source / "scene.blend").is_file() or not (source / "plan.json").is_file():
            raise BlenderError("源目录缺少 SceneLoop 场景或计划")
        validate_scene_document(json.loads((source / "plan.json").read_text(encoding="utf-8")))
        if target.exists() and any(target.iterdir()):
            raise BlenderError("新候选目录必须为空，拒绝覆盖")
        target.mkdir(parents=True, exist_ok=True)
        return self._run("fork", target, source_workspace=str(source), frozen=True)

    def apply_edit(self, edit_plan, workspace):
        workspace = Path(workspace).resolve()
        if not (workspace / "scene.blend").is_file():
            raise BlenderError("尚无候选场景可编辑")
        document = json.loads((workspace / "plan.json").read_text(encoding="utf-8"))
        (validate_imported_edit_plan if is_imported_manifest(document) else apply_edit_plan)(document, edit_plan)
        return self._run("edit", workspace, edit_plan=edit_plan, frozen=True)

    def rebind_candidate(self, workspace):
        """Re-bind a materialized checkpoint copy to its own workspace identity.

        Only the embedded workspace marker and the dependent inspection are
        rewritten; the plan, mesh, materials, modifiers, cameras and every
        artifact hash check stay in force.
        """
        workspace = Path(workspace).resolve()
        scene_file = workspace / "scene.blend"
        plan_file = workspace / "plan.json"
        if not scene_file.is_file() or not plan_file.is_file():
            raise BlenderError("候选缺少场景或计划，无法重绑身份")
        if is_imported_manifest(json.loads(plan_file.read_text(encoding="utf-8"))):
            raise BlenderError("导入场景副本不通过此入口重绑身份，保持既有恢复行为")
        return self._run("rebind", workspace, frozen=False)

    def observe(self, workspace, view_spec, output_id):
        workspace = Path(workspace).resolve()
        view_spec = validate_view_spec(view_spec)
        observation_directory(workspace, output_id)
        try:
            result = self._run("observe", workspace, view_spec=view_spec, output_id=output_id, frozen=True)
        except BlenderError as error:
            if error.outcome == "rejected" and str(error).startswith("structure_observation_unsupported:"):
                error.code = "structure_observation_unsupported"
            raise
        try:
            validate_observation_metadata(result, view_spec)
        except ValueError as error:
            raise BlenderError(str(error)) from None
        return result

    def inspect_geometry(self, workspace, object_id, part_id=None, representation="evaluated"):
        workspace = Path(workspace).resolve()
        scene_file = workspace / "scene.blend"
        if not scene_file.is_file():
            raise BlenderError("候选缺少 scene.blend，无法读取网格")
        if not isinstance(object_id, str) or not object_id:
            raise BlenderError("几何检查需要稳定的 object_id")
        if part_id is not None and (not isinstance(part_id, str) or not part_id):
            raise BlenderError("part_id 需要非空文字")
        if representation not in ("original", "evaluated"):
            raise BlenderError("representation 只支持 original 或 evaluated")
        before = _file_hash(scene_file)
        result = self._run("geometry", workspace, object_id=object_id, part_id=part_id,
                           representation=representation, frozen=True)
        after = _file_hash(scene_file)
        measured_hash = (result.get("source") or {}).get("blend_sha256")
        if before != after or measured_hash != after:
            raise BlenderError("冻结候选在几何读取期间发生变化，结果不可信",
                               code="geometry_source_integrity")
        return result

    def create_snapshot(self, workspace):
        path = Path(workspace).resolve()
        source = path / "scene.blend"
        plan_source = path / "plan.json"
        if not source.is_file() or not plan_source.is_file():
            raise BlenderError("尚无完整候选场景与计划可快照")
        validate_scene_document(json.loads(plan_source.read_text(encoding="utf-8")))
        target = path / "snapshots" / f"{uuid.uuid4().hex}.blend"
        target.parent.mkdir(exist_ok=True)
        if target.parent.resolve().parent != path:
            raise BlenderError("快照目录不能位于候选目录之外")
        try:
            shutil.copy2(source, target)
            shutil.copy2(plan_source, target.with_suffix(".json"))
        except Exception:
            target.unlink(missing_ok=True)
            target.with_suffix(".json").unlink(missing_ok=True)
            raise
        return {"path": str(target), "plan_path": str(target.with_suffix(".json")), "workspace": str(path)}

    def rollback(self, workspace, snapshot):
        path = Path(workspace).resolve()
        source = Path(snapshot["path"]).resolve()
        if source.parent != path / "snapshots" or source.suffix != ".blend" or not source.is_file() or not source.with_suffix(".json").is_file():
            raise BlenderError("快照不属于此候选目录或缺少对应计划")
        validate_scene_document(json.loads(source.with_suffix(".json").read_text(encoding="utf-8")))
        staged = path / f".rollback-{uuid.uuid4().hex}"
        staged.mkdir()
        try:
            shutil.copy2(source, staged / "scene.blend")
            shutil.copy2(source.with_suffix(".json"), staged / "plan.json")
            replace_candidate_files(path, staged)
        finally:
            shutil.rmtree(staged, ignore_errors=True)
        return {"scene_path": str(path / "scene.blend"), "plan_path": str(path / "plan.json"), "render_stale": True}

    def build(self, plan, run_dir):
        workspace = self.create_workspace(run_dir)
        normalized = validate_plan(plan)
        (workspace / "plan.json").write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
        return self._run("build_render", workspace)


class LiveBlenderAdapter(BlenderAdapter):
    mode = "live"

    def __init__(self, blender_path=None, bridge_dir=None, timeout=300):
        self.bridge_dir = Path(bridge_dir or os.environ.get("SCENELOOP_BRIDGE_DIR", DEFAULT_BRIDGE_DIR)).resolve()
        self.timeout = timeout
        status = bridge_status(self.bridge_dir)
        self.discovery = {"available": status["connected"], "path": status.get("binary_path"), "version": status.get("version"), "error": None if status["connected"] else "Blender Live 桥尚未连接"}

    def create_snapshot(self, workspace):
        return self._run("snapshot", workspace)

    def rollback(self, workspace, snapshot):
        path = Path(workspace).resolve()
        source = Path(snapshot["path"]).resolve()
        if source.parent != path / "snapshots" or source.suffix != ".blend" or not source.is_file():
            raise BlenderError("快照不属于此候选目录")
        return self._run("rollback", workspace, snapshot=snapshot)

    def _run(self, action, workspace, **extra):
        status = bridge_status(self.bridge_dir)
        if not status["connected"]:
            raise BlenderError("Blender Live 桥尚未连接，请先在已打开的 Blender 中运行 blender_bridge.py；请求未发出，未执行任何写操作。",
                               outcome="not_scheduled", code="blender_unavailable")
        workspace = Path(workspace).resolve()
        allowed = Path(status["output_root"]).resolve()
        if not workspace.is_relative_to(allowed):
            raise BlenderError(f"Live 候选目录必须位于 {allowed}；请求未发出，未执行任何写操作。",
                               outcome="not_scheduled", code="candidate_outside_output_root")
        identifier = uuid.uuid4().hex
        request = {"action": action, "workspace": str(workspace), "request_id": identifier, **extra}
        temporary = self.bridge_dir / f"{identifier}.tmp"
        try:
            temporary.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
            temporary.rename(self.bridge_dir / f"{identifier}.request.json")
        except OSError as exc:
            raise BlenderError("无法向 Live 桥写入请求；请求未发出，未执行任何写操作。",
                               outcome="not_scheduled", code="host_io_error") from exc
        response_path = self.bridge_dir / f"{identifier}.response.json"
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if response_path.is_file():
                try:
                    response = json.loads(response_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise BlenderError("Blender Live 返回结果无法读取或解析；写结果未知，请人工核实候选。",
                                       outcome="unknown") from exc
                cleanup_error = None
                try:
                    response_path.unlink()
                except OSError as exc:
                    cleanup_error = str(exc)
                result = _validated_result(action, response)
                if cleanup_error:
                    result = {**result, "host_cleanup_error": cleanup_error}
                return result
            time.sleep(.2)
        raise BlenderError("Blender Live 执行超时；操作可能仍在 Blender 中完成，结果未知，请勿重复提交。",
                           outcome="unknown", code="write_timeout")


def create_adapter(mode="headless", blender_path=None, bridge_dir=None, timeout=300):
    if mode in ("headless", "file"):
        return BlenderAdapter(blender_path, bridge_dir, timeout)
    if mode in ("live", "attached"):
        return LiveBlenderAdapter(blender_path, bridge_dir, timeout)
    raise BlenderError(f"不支持的 Blender 模式：{mode}")
