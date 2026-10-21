"""Persistent offline asset coordinator, deliberately separate from Run/Builder.

Each method performs one explicit stage. No background scheduler or POST retry.
Committed admission with an uncertain ACK sacrifices an attempt rather than
guessing whether a remote write happened. Only trusted receipt sources reconcile.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import time
import uuid

from .asset_contract import AssetError, MAX_GLB_BYTES, fixed_glb
from .asset_job_store import AssetJobStore
from .asset_provider import (AssetJobError, CallIdentity, HumanGrant, LocalArtifact, ProviderReply,
                             REMOTE, SAFE_ERRORS, TASK_COST_ACTIONS, TERMINAL,
                             artifact_source, canonical_json, digest, identifier,
                             materialize_wire, safe_projection, token_provenance)
from .config_store import BINDING_FIELDS, ProfileError, ProfileRepository, binding_fields
from .project_state_store import StateStoreError


MAX_IMAGE_BYTES = 8 * 1024 * 1024
PROFILE_FIELDS = ("id", "revision", *BINDING_FIELDS)
PUBLIC_PROFILE_EXTRAS = {"name", "preset", "is_draft", "verification", "verified_at", "has_secret", "connection_check"}


def controlled_path(root, relative):
    """Reject traversal/network and links/reparse points before resolving anything."""
    if (not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative
            or relative.startswith("/") or any(part in {"", ".", ".."} for part in relative.split("/"))):
        raise AssetJobError("asset_path_not_controlled")
    root = Path(root)
    candidate = root / relative
    for path in [root, *root.parents, *[root.joinpath(*relative.split("/")[:i])
                                    for i in range(1, len(relative.split("/")) + 1)]]:
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise AssetJobError("asset_path_not_controlled")
    return candidate


def file_facts(path, limit):
    if not path.is_file() or path.stat().st_size > limit:
        raise AssetJobError("asset_file_invalid")
    raw = path.read_bytes()
    if not raw or len(raw) > limit:
        raise AssetJobError("asset_file_invalid")
    return raw, {"sha256": hashlib.sha256(raw).hexdigest(), "byte_size": len(raw)}


def image_facts(raw):
    mime = None
    try:
        if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 33 and raw[8:16] == b"\x00\x00\x00\x0dIHDR":
            width, height = struct.unpack(">II", raw[16:24])
            mime = "image/png"
        elif raw[:2] == b"\xff\xd8":
            index = 2
            while index + 4 <= len(raw):
                if raw[index] != 255:
                    break
                marker = raw[index + 1]
                length = int.from_bytes(raw[index + 2:index + 4], "big")
                if length < 2 or index + 2 + length > len(raw):
                    break
                if marker in {0xC0, 0xC1, 0xC2} and length >= 8:
                    height, width = struct.unpack(">HH", raw[index + 5:index + 9])
                    mime = "image/jpeg"
                    break
                index += 2 + length
        if mime is None or not (1 <= width <= 8192 and 1 <= height <= 8192 and width * height <= 16_777_216):
            raise ValueError
    except (ValueError, struct.error):
        raise AssetJobError("invalid_asset_image") from None
    return {"mime": mime, "width": width, "height": height}


class AssetJobCoordinator:
    def __init__(self, project_store, profiles, provider, approvals, *, receipts=None, clock=time.time,
                 before_dispatch=None):
        self.store = AssetJobStore(project_store)
        self.root = project_store.project_root
        self.project_id = project_store.project_id
        self.profiles, self.provider, self.approvals = profiles, provider, approvals
        self.receipts, self.clock = receipts, clock
        self.before_dispatch = before_dispatch

    def _profile(self, snapshot):
        fields = ("id", "revision", *binding_fields(snapshot)) if isinstance(snapshot, dict) else PROFILE_FIELDS
        if (not isinstance(snapshot, dict) or set(snapshot) - set(fields) - PUBLIC_PROFILE_EXTRAS
                or any(key not in snapshot for key in fields)):
            raise AssetJobError("asset_profile_snapshot_invalid")
        try:
            # Read the saved authority again: another process may have edited/deleted it.
            repository = ProfileRepository(self.profiles.root, self.profiles.protector)
            saved = next((p for p in repository.list() if p["id"] == snapshot["id"]), None)
            if saved is None or any(type(snapshot[k]) is not type(saved[k]) or snapshot[k] != saved[k]
                                    for k in fields) or saved["kind"] != "asset3d":
                raise AssetJobError("asset_configuration_conflict")
            profile = {key: saved[key] for key in fields}
            if profile["credential_mode"] == "stored":
                key = repository.resolve_key(profile)
            elif profile["credential_mode"] == "env":
                key = os.environ.get(profile["credential_env"])
                if not isinstance(key, str) or not key:
                    raise AssetJobError("asset_credential_unavailable")
            else:
                key = None
            return profile, key, digest({"profile": profile, "credential": key})
        except ProfileError:
            raise AssetJobError("asset_configuration_conflict") from None

    def inspect_request(self, request):
        """Freezeable descriptor. Pure local preflight; no job/approval/provider writes."""
        if not isinstance(request, dict) or set(request) != {"profile", "input", "parameters"}:
            raise AssetJobError("invalid_asset_request")
        profile, _, binding = self._profile(request["profile"])
        capability = self.provider.capabilities(profile)
        cap = capability.snapshot()
        if any(cap[key] != profile[key] for key in ("provider_id", "model", "mode")):
            raise AssetJobError("unsupported_asset_capability")
        source = request["input"]
        if not isinstance(source, dict) or source.get("kind") not in cap["input_modes"]:
            raise AssetJobError("unsupported_asset_input")
        raw = None
        if source["kind"] == "text":
            if (set(source) != {"kind", "prompt"} or not isinstance(source["prompt"], str)
                    or not source["prompt"].strip() or len(source["prompt"]) > 4000
                    or any(ord(c) < 32 and c not in "\n\t" for c in source["prompt"])):
                raise AssetJobError("invalid_asset_prompt")
            descriptor = dict(source)
        else:
            if set(source) != {"kind", "path"}:
                raise AssetJobError("invalid_asset_input")
            path = controlled_path(self.root, source["path"])
            raw, facts = file_facts(path, MAX_IMAGE_BYTES)
            descriptor = {"kind": "image", **facts, **image_facts(raw)}
        capability.validate_parameters(request["parameters"])
        parameters = json.loads(canonical_json(request["parameters"]))
        template = self.provider.validate(profile, descriptor, parameters)
        canonical_json(template)
        _, markers = materialize_wire(template)
        cloud_image = descriptor["kind"] == "image" and profile["mode"] == "cloud_api"
        if not isinstance(template, dict) or markers != (1 if cloud_image else 0):
            raise AssetJobError("invalid_asset_wire_template")
        semantic = {"profile": profile, "input": descriptor, "parameters": parameters,
                    "capability": cap, "wire_template": template}
        return {"semantic": semantic, "semantic_digest": digest(semantic), "profile_digest": digest(profile),
                "credential_binding": binding}, raw

    def _grant(self, reference, snapshot, scope, *, frozen=None):
        try:
            grant = self.approvals.resolve(reference)
        except Exception:
            raise AssetJobError("asset_approval_unavailable") from None
        if not isinstance(grant, HumanGrant):
            raise AssetJobError("asset_approval_invalid")
        record = grant.snapshot()
        if (record["project_id"] != self.project_id or record["scope"] != scope
                or record["semantic_digest"] != snapshot["semantic_digest"]
                or record["profile_digest"] != snapshot["profile_digest"]
                or (record["expires_at"] is not None and self.clock() >= record["expires_at"])
                or (frozen is not None and record != frozen)):
            raise AssetJobError("asset_approval_conflict")
        return record

    def prepare(self, submission_key, request, *, generation_approval, upload_approval=None):
        identifier(submission_key)
        inspected, raw = self.inspect_request(request)
        generation = self._grant(generation_approval, inspected, "generation")
        upload = self._grant(upload_approval, inspected, "upload") if upload_approval is not None else None
        existing = self.store.by_key(submission_key)
        if existing:
            if existing["semantic_digest"] != inspected["semantic_digest"] or existing["generation_grant"] != generation:
                raise AssetJobError("asset_submission_conflict")
            return safe_projection(existing)
        job_id = uuid.uuid4().hex
        fixed_input = None
        if raw is not None:
            relative = f".sceneloop/asset-jobs/{job_id}/input/source"
            self._fixed_file(relative, raw)
            fixed_input = {"path": relative, **{k: inspected["semantic"]["input"][k] for k in ("sha256", "byte_size")}}
        job = {"schema_version": 1, "project_id": self.project_id, "job_id": job_id,
               "submission_key": submission_key, "version": 1, **inspected,
               "fixed_input": fixed_input, "generation_grant": generation, "upload_grant": upload,
               "upload": "pending" if raw is not None and inspected["semantic"]["profile"]["mode"] == "cloud_api" else "not_required",
               "dispatch": "prepared", "remote": "unknown", "local": "waiting", "preparation": "not_started",
               "cost": "not_dispatched", "credits": None, "cost_history": [], "file_token": None,
               "token_provenance": None, "token_invalid": False, "task_id": None,
               "upload_attempts": 0, "upload_history": [], "upload_receipts": [], "quarantined_receipts": [], "creation_attempts": 0,
               "creation_attempt_id": None, "final_wire": None, "final_wire_digest": None,
               "query_generation": 0, "preparation_generation": 0, "output": None,
               "output_availability": "unknown", "raw_artifact": None, "checks": None,
               "last_query": None, "safe_error": None}
        try:
            actual, _ = self.store.create(job)
        except (StateStoreError, OSError, RuntimeError):
            # SQL may have committed. Only a matching authority row can be reused.
            actual = self.store.by_key(submission_key)
            if actual is None or actual["semantic_digest"] != job["semantic_digest"] or actual["generation_grant"] != generation:
                raise AssetJobError("asset_sql_outcome_unknown") from None
        self._after_step("input_fixed", actual)
        return safe_projection(actual)

    def _fixed_file(self, relative, raw):
        path = controlled_path(self.root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        controlled_path(self.root, relative)
        # No replacement of raw/unknown files. Preserve crash leftovers.
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        return path

    def _mutate(self, job, patch, action, *, grant=None, gate=False, bridge_gate=None):
        try:
            return self.store.update(job["job_id"], job["version"], patch, action=action, consume_grant=grant,
                                     **({'bridge_gate':bridge_gate} if bridge_gate is not None else {}))
        except (StateStoreError, OSError, RuntimeError):
            actual = self.store.read(job["job_id"])
            if actual["version"] == job["version"] + 1 and all(actual.get(k) == v for k, v in patch.items()):
                if not gate:
                    return actual
                # Even a committed gate cannot authorize a POST after uncertain ACK.
                self.recover(job["job_id"])
            raise AssetJobError("asset_sql_outcome_unknown") from None

    def _facts(self, job):
        profile, key, binding = self._profile(job["semantic"]["profile"])
        if binding != job["credential_binding"]:
            raise AssetJobError("asset_credential_binding_conflict")
        current = self.provider.capabilities(profile).snapshot()
        if current != job["semantic"]["capability"]:
            raise AssetJobError("asset_provider_version_conflict")
        if job["fixed_input"]:
            path = controlled_path(self.root, job["fixed_input"]["path"])
            _, facts = file_facts(path, MAX_IMAGE_BYTES)
            if any(facts[k] != job["fixed_input"][k] for k in facts):
                raise AssetJobError("asset_input_integrity_error")
        return profile, key

    def _action_facts(self, job):
        try:
            return self._facts(job)
        except AssetJobError as error:
            code = error.code if error.code in SAFE_ERRORS else "asset_configuration_conflict"
            self._wait(job["job_id"], code)
            raise AssetJobError(code) from None

    def read(self, job_id):
        job = self.store.read(job_id)
        if job["preparation"] == "ready":
            try:
                path = controlled_path(self.root, job["raw_artifact"]["path"])
                _, facts = file_facts(path, MAX_GLB_BYTES)
                if any(facts[k] != job["raw_artifact"][k] for k in facts):
                    raise AssetJobError("asset_artifact_integrity_error")
            except (AssetJobError, OSError):
                job = self._mutate(job, {"preparation": "blocked", "safe_error": "asset_artifact_integrity_error"},
                                   "preparation_blocked")
        return safe_projection(job)

    def recover(self, job_id):
        job = self.store.read(job_id)
        patch = {}
        if job["upload"] == "uploading":
            patch.update(upload="outcome_unknown", safe_error="asset_upload_outcome_unknown")
        if job["dispatch"] == "submitting":
            patch.update(dispatch="outcome_unknown", safe_error="asset_create_outcome_unknown")
        if job["preparation"] == "downloading":
            patch.update(preparation="failed", safe_error="asset_preparation_interrupted")
        if job["local"] == "active":
            patch["local"] = "waiting"
        elif job["local"] == "stop_requested":
            patch["local"] = "stopped"
        if patch:
            self._mutate(job, patch, "recover")
        return self.read(job_id)

    def resume(self, job_id):
        self.recover(job_id)
        self.read(job_id)  # Block altered ready files, never silently repair them.
        job = self.store.read(job_id)
        self._action_facts(job)
        if job["upload"] == "outcome_unknown" or job["dispatch"] == "outcome_unknown":
            return safe_projection(job)
        if job["local"] != "active":
            job = self._mutate(job, {"local": "active", "safe_error": None}, "resume")
        return safe_projection(job)

    def stop(self, job_id):
        for _ in range(8):
            job = self.store.read(job_id)
            if job["local"] == "stopped":
                return safe_projection(job)
            patch = {"local": "stopped" if job["local"] == "stop_requested" else "stop_requested"}
            try:
                self._mutate(job, patch, "stopped" if patch["local"] == "stopped" else "stop")
            except AssetJobError as error:
                if error.code != "asset_job_version_conflict":
                    raise
        raise AssetJobError("asset_job_version_conflict")

    def _active(self, job):
        if job["local"] != "active":
            raise AssetJobError("asset_explicit_resume_required")

    def _wait(self, job_id, code, **patch):
        for _ in range(8):
            job = self.store.read(job_id)
            fields = {"safe_error": code, **patch}
            if job["local"] == "active":
                fields["local"] = "waiting"
            try:
                return self._mutate(job, fields, "receipt_conflict")
            except AssetJobError as error:
                if error.code != "asset_job_version_conflict":
                    raise
        raise AssetJobError("asset_job_version_conflict")

    @staticmethod
    def _identity(job, stage):
        if stage == "upload":
            attempt, generation = job["upload_history"][-1]["attempt_id"], job["upload_attempts"]
        elif stage == "create":
            attempt, generation = job["creation_attempt_id"], 1
        elif stage == "preparation":
            attempt, generation = job["creation_attempt_id"], job["preparation_generation"]
        else:
            attempt, generation = job["creation_attempt_id"], job["query_generation"]
        return CallIdentity(job["project_id"], job["job_id"], stage, attempt, generation,
                            job["semantic_digest"], None if stage == "upload" else job["final_wire_digest"],
                            job["task_id"] if stage not in {"upload", "create"} else None)

    def _reply(self, job, reply, identity):
        if (not isinstance(reply, ProviderReply) or reply.identity != identity
                or reply.disposition not in {"accepted", "rejected"}
                or (reply.model is not None and reply.model != job["semantic"]["profile"]["model"])
                or (reply.input_kind is not None and reply.input_kind != job["semantic"]["input"]["kind"])):
            self._quarantine(job["job_id"], reply, identity.stage)
            raise AssetJobError("asset_receipt_identity_conflict")

    def _quarantine(self, job_id, reply, stage, *, reason="asset_receipt_identity_conflict"):
        task = reply.task_id if isinstance(reply, ProviderReply) else None
        try:
            identifier(task)
        except AssetJobError:
            task = None
        for _ in range(8):
            job = self.store.read(job_id)
            safe_task = task
            # Recognize the frozen account key without resolving/decrypting it;
            # token strings from any original upload are also private, not IDs.
            if task is not None and (digest({"profile": job["semantic"]["profile"], "credential": task}) == job["credential_binding"]
                                     or any(task == item["file_token"] for item in job["upload_receipts"])):
                safe_task = None
            remote = reply.remote_status if isinstance(reply, ProviderReply) else None
            if not isinstance(remote, str) or remote not in REMOTE:
                remote = None
            generation = (reply.identity.generation if isinstance(reply, ProviderReply)
                          and isinstance(reply.identity, CallIdentity) else None)
            if type(generation) is not int or not 0 <= generation <= 9007199254740991:
                generation = None
            record = {"stage": stage, "task_id": safe_task, "reason": reason,
                      "remote_status": remote, "request_generation": generation}
            patch = {"safe_error": "asset_receipt_identity_conflict"}
            if len(job["quarantined_receipts"]) < 128:
                patch["quarantined_receipts"] = [*job["quarantined_receipts"], record]
            if job["local"] == "active":
                patch["local"] = "waiting"
            try:
                self._mutate(job, patch, "receipt_conflict")
                return
            except AssetJobError as error:
                if error.code != "asset_job_version_conflict":
                    raise
        raise AssetJobError("asset_job_version_conflict")

    def upload(self, job_id):
        job = self.store.read(job_id)
        self._active(job)
        if job["upload"] != "pending" or job["creation_attempts"]:
            raise AssetJobError("asset_upload_not_admissible")
        profile, key = self._action_facts(job)
        grant = self._grant(job["upload_grant"]["id"] if job["upload_grant"] else None, job, "upload", frozen=job["upload_grant"])
        attempt = {"number": job["upload_attempts"] + 1, "attempt_id": uuid.uuid4().hex, "grant": grant}
        job = self._mutate(job, {"upload": "uploading", "upload_attempts": attempt["number"],
                                 "upload_history": [*job["upload_history"], attempt]},
                           "upload_gate", grant=grant, gate=True)
        self._after_step("upload_gate", job)
        if self.store.read(job_id)["local"] != "active":
            return safe_projection(self._wait(job_id, "asset_upload_outcome_unknown", upload="outcome_unknown"))
        identity = self._identity(job, "upload")
        try:
            reply = self.provider.upload_input(identity, profile, key, controlled_path(self.root, job["fixed_input"]["path"]))
            self._after_step("upload_response", job)
            return self._consume_upload(job_id, reply, identity, "upload_reply")
        except Exception as error:
            # A crash (os._exit) is recovered by a NEW process; exceptions never retry.
            current = self.store.read(job_id)
            if current["upload"] == "uploading":
                self._wait(job_id, "asset_upload_outcome_unknown", upload="outcome_unknown")
            if isinstance(error, AssetJobError):
                raise
            raise AssetJobError("asset_upload_outcome_unknown") from None

    def _consume_upload(self, job_id, reply, identity, action):
        for _ in range(8):
            job = self.store.read(job_id)
            self._reply(job, reply, identity)
            if self._identity(job, "upload") != identity:
                raise AssetJobError("asset_receipt_identity_conflict")
            patch = {"upload": reply.disposition, "safe_error": None}
            if job["upload"] == "outcome_unknown" and action == "upload_reply":
                action = "reconcile_upload"  # A verified in-flight response can arrive after recovery.
            if reply.disposition == "accepted":
                if not isinstance(reply.file_token, str) or not 1 <= len(reply.file_token) <= 4096:
                    raise AssetJobError("asset_upload_outcome_unknown")
                patch.update(file_token=reply.file_token, token_provenance=token_provenance(job))
            receipt = {"attempt_id": identity.attempt_id, "disposition": reply.disposition,
                       "file_token": patch.get("file_token"), "provenance": patch.get("token_provenance")}
            if receipt not in job["upload_receipts"]:
                patch["upload_receipts"] = [*job["upload_receipts"], receipt]
            try:
                result = self._mutate(job, patch, action)
                self._after_step("token_committed", result)
                return safe_projection(result)
            except AssetJobError as error:
                if error.code != "asset_job_version_conflict":
                    raise
        raise AssetJobError("asset_job_version_conflict")

    def renew_upload(self, job_id, approval_reference):
        job = self.store.read(job_id)
        if (job["creation_attempts"] or job["upload"] not in {"rejected", "outcome_unknown", "accepted"}
                or (job["upload"] == "accepted" and not job["token_invalid"])):
            raise AssetJobError("asset_upload_not_admissible")
        grant = self._grant(approval_reference, job, "upload")
        result = self._mutate(job, {"upload": "pending", "upload_grant": grant, "file_token": None,
                                    "token_provenance": None, "token_invalid": False,
                                    "safe_error": None}, "renew_upload")
        return safe_projection(result)

    def invalidate_token(self, job_id, receipt_reference):
        job = self.store.read(job_id)
        reply = self._trusted_receipt(job, receipt_reference, "upload")
        if job["upload"] != "accepted" or reply.disposition != "rejected":
            raise AssetJobError("asset_receipt_identity_conflict")
        return safe_projection(self._mutate(job, {"token_invalid": True, "safe_error": "asset_token_unusable"}, "token_invalid"))

    def submit(self, job_id):
        job = self.store.read(job_id)
        self._active(job)
        if job["dispatch"] != "prepared" or job["creation_attempts"] or job["token_invalid"]:
            raise AssetJobError("asset_create_not_admissible")
        if job["upload"] not in {"not_required", "accepted"}:
            raise AssetJobError("asset_persisted_token_required")
        profile, key = self._action_facts(job)
        grant = self._grant(job["generation_grant"]["id"], job, "generation", frozen=job["generation_grant"])
        if profile['provider_id']=='hunyuan':
            authorization=self.store.check_local_admission(job)
            getattr(self.provider,'local',self.provider).timeout=authorization['job_timeout_seconds']
        wire, _ = materialize_wire(job["semantic"]["wire_template"], job["file_token"])
        admission = None
        if profile['provider_id'] == 'hunyuan':
            from .local_resource_warning import for_job, normal
            admission = for_job(self, job)
            if admission is None:
                admission = normal(getattr(self.provider, 'local', self.provider),
                                   self._identity(job, 'create'), profile, wire)
        bridge_gate = self.before_dispatch(job,profile,key,wire) if self.before_dispatch is not None else None
        job = self._mutate(job, {"dispatch": "submitting", "creation_attempts": 1,
                                 "creation_attempt_id": uuid.uuid4().hex, "cost": "unknown",
                                 "final_wire": wire, "final_wire_digest": digest(wire)},
                           "create_gate", grant=grant, gate=True, bridge_gate=bridge_gate)
        self._after_step("create_gate", job)
        if self.store.read(job_id)["local"] != "active":
            return safe_projection(self._wait(job_id, "asset_create_outcome_unknown", dispatch="outcome_unknown"))
        identity = self._identity(job, "create")
        try:
            if profile['provider_id'] == 'hunyuan':
                self.store.check_local_admission(job)
                current = for_job(self, job)
                if current is not None and current != admission:
                    raise AssetJobError('asset_resource_admission_conflict')
                reply = getattr(self.provider, 'local', self.provider).submit(
                    identity, profile, key, wire, admission=admission)
            else:
                reply = self.provider.submit(identity, profile, key, wire)
            self._after_step("create_response", job)
            return self._consume_create(job_id, reply, identity, "create_reply")
        except Exception as error:
            current = self.store.read(job_id)
            if current["dispatch"] == "submitting":
                self._wait(job_id, "asset_create_outcome_unknown", dispatch="outcome_unknown")
            if isinstance(error, AssetJobError):
                raise
            raise AssetJobError("asset_create_outcome_unknown") from None

    def _consume_create(self, job_id, reply, identity, action):
        for _ in range(8):
            job = self.store.read(job_id)
            self._reply(job, reply, identity)
            if self._identity(job, "create") != identity:
                raise AssetJobError("asset_receipt_identity_conflict")
            patch = {"dispatch": reply.disposition, "safe_error": None}
            if job["dispatch"] == "outcome_unknown" and action == "create_reply":
                action = "reconcile_create"
            if reply.disposition == "accepted":
                identifier(reply.task_id)
                patch["task_id"] = reply.task_id
            patch.update(self._cost_patch(job, reply.cost, action=action))
            try:
                result = self._mutate(job, patch, action)
                self._after_step("task_committed", result)
                return safe_projection(result)
            except AssetJobError as error:
                if error.code != "asset_job_version_conflict":
                    raise
        raise AssetJobError("asset_job_version_conflict")

    def _trusted_receipt(self, job, reference, stage):
        self._action_facts(job)
        if self.receipts is None:
            raise AssetJobError("asset_receipt_source_required")
        identity = self._identity(job, stage)
        try:
            reply = self.receipts.resolve(reference, identity)
        except Exception:
            raise AssetJobError("asset_receipt_unavailable") from None
        self._reply(job, reply, identity)
        return reply

    def reconcile_upload(self, job_id, receipt_reference):
        job = self.store.read(job_id)
        if job["upload"] != "outcome_unknown":
            raise AssetJobError("asset_reconciliation_not_admissible")
        return self._consume_upload(job_id, self._trusted_receipt(job, receipt_reference, "upload"),
                                    self._identity(job, "upload"), "reconcile_upload")

    def reconcile_create(self, job_id, receipt_reference):
        job = self.store.read(job_id)
        if job["dispatch"] != "outcome_unknown":
            raise AssetJobError("asset_reconciliation_not_admissible")
        return self._consume_create(job_id, self._trusted_receipt(job, receipt_reference, "create"),
                                    self._identity(job, "create"), "reconcile_create")

    @staticmethod
    def _cost_patch(job, evidence, *, action):
        if evidence is None:
            return {}
        if (not isinstance(evidence, dict) or set(evidence) != {"state", "credits", "receipt_id", "kind"}
                or (action != "accounting" and action not in TASK_COST_ACTIONS)
                or evidence["kind"] != ("accounting" if action == "accounting" else "task")
                or evidence["state"] in {"unknown", "not_dispatched"}):
            raise AssetJobError("asset_cost_evidence_invalid")
        # Full numerical/transition validation runs in the unified SQL validator.
        if job["cost_history"] and evidence == job["cost_history"][-1]:
            return {}
        return {"cost": evidence["state"], "credits": evidence["credits"],
                "cost_history": [*job["cost_history"], json.loads(canonical_json(evidence))]}

    def account(self, job_id, receipt_reference):
        job = self.store.read(job_id)
        reply = self._trusted_receipt(job, receipt_reference, "accounting")
        if not reply.cost or reply.cost["kind"] != "accounting":
            raise AssetJobError("asset_cost_evidence_invalid")
        return safe_projection(self._mutate(job, self._cost_patch(job, reply.cost, action="accounting"), "accounting"))

    def query(self, job_id):
        return self._query_action(job_id, cancel=False)

    def query_recorded(self, job_id):
        """Explicit read-only collection after a host wait ends; never resume/create."""
        return self._query_action(job_id, cancel=False, recorded=True)

    def cancel(self, job_id):
        return self._query_action(job_id, cancel=True)

    def cancel_recorded(self, job_id):
        """Trusted explicit cancellation of the recorded task after local stop."""
        return self._query_action(job_id, cancel=True, recorded=True)

    def _query_action(self, job_id, *, cancel, recorded=False):
        job = self.store.read(job_id)
        if not recorded:
            self._active(job)
        if job["dispatch"] != "accepted":
            raise AssetJobError("asset_bound_task_required")
        if cancel and not job["semantic"]["capability"]["cancel_supported"]:
            return {"supported": False, "job": safe_projection(job)}
        profile, key = self._action_facts(job)
        job = self._mutate(job, {"query_generation": job["query_generation"] + 1}, "query_gate")
        if not recorded and self.store.read(job_id)["local"] != "active":
            return safe_projection(self.store.read(job_id))
        identity = self._identity(job, "cancel" if cancel else "query")
        try:
            reply = (self.provider.cancel if cancel else self.provider.query)(identity, profile, key)
            return self._consume_query(job_id, reply, identity, "cancel_reply" if cancel else "query_reply")
        except Exception as error:
            self._wait(job_id, "asset_query_uncertain")
            if isinstance(error, AssetJobError):
                raise
            raise AssetJobError("asset_query_uncertain") from None

    def _consume_query(self, job_id, reply, identity, action):
        for _ in range(8):
            job = self.store.read(job_id)
            self._reply(job, reply, identity)
            if self._identity(job, identity.stage) != identity:
                self._quarantine(job_id, reply, identity.stage, reason="asset_query_generation_conflict")
                raise AssetJobError("asset_stale_or_foreign_query")
            if reply.task_id != job["task_id"]:
                self._quarantine(job_id, reply, identity.stage, reason="asset_task_identity_conflict")
                raise AssetJobError("asset_stale_or_foreign_query")
            if reply.remote_status not in REMOTE - {"unknown"}:
                return safe_projection(self._wait(job_id, "asset_query_unrecognized"))
            if job["remote"] in TERMINAL and reply.remote_status != job["remote"]:
                self._quarantine(job_id, reply, identity.stage, reason="asset_remote_terminal_conflict")
                raise AssetJobError("asset_job_state_integrity_error")
            progress = reply.progress
            if progress is not None and (type(progress) not in (int, float) or not 0 <= progress <= 100):
                raise AssetJobError("asset_query_invalid_progress")
            patch = {"remote": reply.remote_status, "last_query": {"status": reply.remote_status, "progress": progress,
                      "generation": identity.generation}, "safe_error": None}
            patch.update(self._cost_patch(job, reply.cost, action=action))
            if reply.output is not None:
                patch["output"] = reply.output
            if reply.output_availability is not None:
                patch["output_availability"] = reply.output_availability
            try:
                return safe_projection(self._mutate(job, patch, action))
            except AssetJobError as error:
                if error.code != "asset_job_version_conflict":
                    raise
        raise AssetJobError("asset_job_version_conflict")

    def prepare_artifact(self, job_id):
        return self._prepare_artifact(job_id,recorded=False)

    def prepare_recorded_artifact(self, job_id):
        """Collect the original accepted local result without resuming execution."""
        return self._prepare_artifact(job_id,recorded=True)

    def _prepare_artifact(self, job_id, *, recorded):
        job = self.store.read(job_id)
        if not recorded:
            self._active(job)
        elif job['semantic']['profile']['provider_id']!='hunyuan':
            raise AssetJobError('asset_local_identity_conflict')
        if job["dispatch"] != "accepted" or job["remote"] != "succeeded" or not job["output"]:
            raise AssetJobError("asset_success_output_required")
        profile, key = self._action_facts(job)
        if job["preparation"] == "ready":
            return self.read(job_id)
        if job["raw_artifact"] is not None:
            if job["preparation"] in {"failed", "blocked"}:
                job = self._mutate(job, {"preparation": "raw_available"}, "recheck")
            return self._inspect_raw(job)
        if job["preparation"] not in {"not_started", "failed"}:
            raise AssetJobError("asset_preparation_not_admissible")
        job = self._mutate(job, {"preparation": "downloading", "preparation_generation": job["preparation_generation"] + 1},
                           "preparation_gate")
        self._after_step("preparation_gate", job)
        current = self.store.read(job_id)
        if current["local"] != "active" and not recorded:
            return safe_projection(self._mutate(current, {"preparation": "failed", "safe_error": "asset_preparation_interrupted"},
                                                "preparation_failed"))
        identity = self._identity(job, "preparation")
        try:
            artifact = self.provider.fetch_result(identity, profile, key, job["output"])
            if (not isinstance(artifact, LocalArtifact) or artifact.identity != identity
                    or artifact.artifact_id != job["output"]["artifact_id"]
                    or not artifact.relative_path.startswith(f".sceneloop/asset-jobs/{job_id}/incoming/")):
                raise AssetJobError("asset_artifact_origin_conflict")
            raw, facts = file_facts(controlled_path(self.root, artifact.relative_path), MAX_GLB_BYTES)
            relative = f'.sceneloop/asset-jobs/{job_id}/raw/{identifier(artifact.artifact_id)}.glb'
            self._fixed_file(relative, raw)
            self._after_step("raw_fixed", job)
            descriptor = {"path": relative, **facts, "source": artifact_source(job, **facts)}
            # A stop racing an in-flight fetch still records its fixed source facts.
            for _ in range(8):
                current = self.store.read(job_id)
                if self._identity(current, "preparation") != identity:
                    raise AssetJobError("asset_artifact_origin_conflict")
                try:
                    bound = self._mutate(current, {"raw_artifact": descriptor, "preparation": "raw_available"}, "raw_bound")
                    break
                except AssetJobError as error:
                    if error.code != "asset_job_version_conflict":
                        raise
            else:
                raise AssetJobError("asset_job_version_conflict")
            self._after_step("raw_bound", bound)
            if bound["local"] != "active" and not recorded:
                return safe_projection(bound)
            return self._inspect_raw(bound)
        except Exception as error:
            current = self.store.read(job_id)
            if current["preparation"] == "downloading":
                self._mutate(current, {"preparation": "failed", "safe_error": "asset_raw_unbound_or_unavailable",
                                       **({"local": "waiting"} if current["local"] == "active" else {})}, "preparation_failed")
            if isinstance(error, AssetJobError):
                raise
            raise AssetJobError("asset_raw_unbound_or_unavailable") from None

    def _inspect_raw(self, job):
        try:
            raw = job["raw_artifact"]
            path = controlled_path(self.root, raw["path"])
            _, facts = file_facts(path, MAX_GLB_BYTES)
            if any(facts[k] != raw[k] for k in facts):
                raise AssetJobError("asset_artifact_integrity_error")
            content, inspected = fixed_glb(path)
            if inspected != facts:
                raise AssetJobError("asset_artifact_integrity_error")
            self.provider.check_artifact(content)
            checks = {"contract": "static_glb_v1", **facts, "passed": True}
            self._after_step("artifact_checked", job)
            return safe_projection(self._mutate(job, {"preparation": "ready", "checks": checks, "safe_error": None}, "ready"))
        except (AssetError, AssetJobError, OSError) as error:
            if isinstance(error, AssetJobError) and error.code in {"asset_job_version_conflict", "asset_sql_outcome_unknown"}:
                raise
            code = "asset_shape_images_blocked" if getattr(error, "code", None) == "asset_shape_images_blocked" else "asset_artifact_contract_or_integrity"
            return safe_projection(self._mutate(job, {"preparation": "blocked", "safe_error": code},
                                                "preparation_blocked"))

    def _after_step(self, step, job):
        """Observation boundary; never changes authorization or performs work."""
