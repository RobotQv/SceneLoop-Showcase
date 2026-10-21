"""Asset generation contracts. No transport, credentials, SQL or implicit work.

These interfaces are internal backend boundaries, not a model/HTTP approval API.
An injected provider and approval/receipt source must be trusted by the host.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from .tool_correction import UnexecutedToolRejected


class AssetJobError(ValueError):
    def __init__(self, code, message="Asset job action refused", *, details=None):
        super().__init__(message)
        self.code = code
        self.details = details


class AssetInputRejected(AssetJobError, UnexecutedToolRejected):
    """Only deterministic request_asset field validation, before any action."""
    def __init__(self, code, fields, guidance):
        super().__init__(code, guidance, details={
            'tool_name':'request_asset', 'boundary':'asset_preinput_validation',
            'execution':'confirmed_not_executed', 'fields':fields, 'guidance':guidance})


def canonical_json(value):
    def check(item):
        if isinstance(item, dict):
            if any(type(key) is not str for key in item):
                raise ValueError
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError
    try:
        check(value)
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise AssetJobError("invalid_asset_json") from None


def digest(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
        raise AssetJobError("invalid_asset_identifier")
    return value


@dataclass(frozen=True)
class Capability:
    provider_id: str
    model: str
    mode: str
    input_modes: tuple[str, ...]
    parameters: dict
    implementation_version: str
    source: str
    checked_at: str
    evidence: str = "unverified"
    cancel_supported: bool = False

    def snapshot(self):
        if (self.mode not in {"cloud_api", "local_http", "local_process"}
                or not self.input_modes or set(self.input_modes) - {"text", "image"}
                or self.evidence not in {"documented", "mock-tested", "live-verified"}
                or not self.source or not self.checked_at or not self.implementation_version
                or type(self.cancel_supported) is not bool):
            raise AssetJobError("unsupported_asset_capability")
        return json.loads(canonical_json({**self.__dict__, "input_modes": list(self.input_modes)}))

    def validate_parameters(self, value):
        reserved = {"approved", "authorization_id", "max_calls", "remaining", "budget_override", "file_token",
                    "api_key", "credential", "protected_key"}
        if not isinstance(value, dict) or set(value) - set(self.parameters) or set(value) & reserved:
            raise AssetJobError("unsupported_asset_parameter")
        canonical_json(value)
        for name, item in value.items():
            rule = self.parameters[name]
            types = {"integer": int, "number": (int, float), "boolean": bool, "string": str,
                     "array": list}
            expected = types.get(rule.get("type"))
            if expected is None or type(item) not in (expected if isinstance(expected, tuple) else (expected,)):
                raise AssetJobError("invalid_asset_parameter")
            if isinstance(item, (float, int)) and not isinstance(item, bool):
                if not math.isfinite(item) or item < rule.get("min", -math.inf) or item > rule.get("max", math.inf):
                    raise AssetJobError("invalid_asset_parameter")
            if "enum" in rule and item not in rule["enum"]:
                raise AssetJobError("invalid_asset_parameter")
            if isinstance(item, (str, list)) and len(item) > rule.get("max_length", 4096):
                raise AssetJobError("invalid_asset_parameter")
            if isinstance(item, list) and any(type(child) is not str or len(child) > 256 for child in item):
                raise AssetJobError("invalid_asset_parameter")


@dataclass(frozen=True)
class CallIdentity:
    project_id: str
    job_id: str
    stage: str
    attempt_id: str
    generation: int
    semantic_digest: str
    wire_digest: str | None = None
    task_id: str | None = None


@dataclass(frozen=True)
class ProviderReply:
    identity: CallIdentity
    disposition: str = "accepted"
    file_token: str | None = field(default=None, repr=False)
    task_id: str | None = None
    remote_status: str | None = None
    progress: float | None = None
    cost: dict | None = None
    output: dict | None = None
    output_availability: str | None = None
    model: str | None = None
    input_kind: str | None = None


@dataclass(frozen=True)
class LocalArtifact:
    identity: CallIdentity
    artifact_id: str
    relative_path: str


@dataclass(frozen=True)
class HumanGrant:
    """Only a trusted ApprovalSource can resolve a reference into this record."""
    id: str
    project_id: str
    semantic_digest: str
    profile_digest: str
    scope: str
    origin: str
    fee_boundary: str
    expires_at: float | None = None
    revoked: bool = False
    max_calls: int = 1

    def snapshot(self):
        identifier(self.id)
        if (self.scope not in {"upload", "generation"} or not self.origin or not self.fee_boundary
                or type(self.max_calls) is not int or self.max_calls != 1
                or type(self.revoked) is not bool or self.revoked
                or (self.expires_at is not None and
                    (type(self.expires_at) not in (int, float) or not math.isfinite(self.expires_at)))):
            raise AssetJobError("asset_approval_invalid")
        record = dict(self.__dict__)
        return {**record, "digest": digest(record)}


class ApprovalSource(ABC):
    @abstractmethod
    def resolve(self, reference: str) -> HumanGrant:
        """Resolve an existing human grant; never mint it from model parameters."""


class ReceiptSource(ABC):
    @abstractmethod
    def resolve(self, reference: str, identity: CallIdentity) -> ProviderReply:
        """Return verified provenance for the ORIGINAL attempt, not a prompt match."""


class Asset3DProvider(ABC):
    @abstractmethod
    def capabilities(self, profile: dict) -> Capability:
        """Local, deterministic registry lookup. No calls or job creation."""

    @abstractmethod
    def validate(self, profile: dict, input_descriptor: dict, parameters: dict) -> dict:
        """Local wire template. Cloud image uses exactly one INPUT_TOKEN marker."""

    @abstractmethod
    def upload_input(self, identity, profile, credential, input_path) -> ProviderReply:
        pass

    @abstractmethod
    def submit(self, identity, profile, credential, wire) -> ProviderReply:
        """One create call only. No upload, polling, download or SQL."""

    @abstractmethod
    def query(self, identity, profile, credential) -> ProviderReply:
        pass

    @abstractmethod
    def cancel(self, identity, profile, credential) -> ProviderReply:
        pass

    @abstractmethod
    def fetch_result(self, identity, profile, credential, output) -> LocalArtifact:
        """B1-A: trusted controlled local file. HTTP/download is a later adapter."""

    def check_artifact(self, raw):
        """Optional provider preparation guard, after raw binding and before ready.

        The default preserves existing local-asset/Provider behavior.
        """


INPUT_TOKEN = {"$asset_input": "file_token"}


def materialize_wire(template, token=None):
    count = 0
    def visit(item):
        nonlocal count
        if item == INPUT_TOKEN:
            count += 1
            return token
        if isinstance(item, dict):
            return {key: visit(value) for key, value in item.items()}
        if isinstance(item, list):
            return [visit(value) for value in item]
        return item
    result = visit(template)
    return result, count


REMOTE = {"unknown", "queued", "running", "succeeded", "failed", "banned", "expired", "cancelled"}
TERMINAL = {"succeeded", "failed", "banned", "expired", "cancelled"}
TASK_COST_ACTIONS = {"create_reply", "reconcile_create", "query_reply", "cancel_reply"}
QUARANTINE_REASONS = {"asset_receipt_identity_conflict", "asset_task_identity_conflict",
                      "asset_query_generation_conflict", "asset_remote_terminal_conflict"}
SAFE_ERRORS = {None, "asset_artifact_integrity_error", "asset_upload_outcome_unknown", "asset_create_outcome_unknown",
               "asset_preparation_interrupted", "asset_token_unusable", "asset_query_uncertain", "asset_query_unrecognized",
               "asset_raw_unbound_or_unavailable", "asset_artifact_contract_or_integrity", "asset_configuration_conflict",
               "asset_credential_unavailable", "asset_credential_binding_conflict", "asset_provider_version_conflict",
               "asset_input_integrity_error", "asset_file_invalid", "asset_path_not_controlled", "asset_receipt_identity_conflict",
               "asset_shape_images_blocked"}
EDGES = {
    "upload": {"not_required": set(), "pending": {"uploading"},
               "uploading": {"accepted", "rejected", "outcome_unknown"},
               "accepted": {"pending"}, "rejected": {"pending"},
               "outcome_unknown": {"accepted", "rejected", "pending"}},
    "dispatch": {"prepared": {"submitting"}, "submitting": {"accepted", "rejected", "outcome_unknown"},
                 "accepted": set(), "rejected": set(), "outcome_unknown": {"accepted", "rejected"}},
    "remote": {"unknown": REMOTE - {"unknown"}, "queued": {"running"} | TERMINAL,
               "running": TERMINAL, **{state: set() for state in TERMINAL}},
    "local": {"active": {"waiting", "stop_requested"}, "waiting": {"active", "stop_requested"},
              "stop_requested": {"stopped"}, "stopped": {"active"}},
    "preparation": {"not_started": {"downloading"}, "downloading": {"raw_available", "failed"},
                    "raw_available": {"ready", "blocked", "failed"},
                    "ready": {"blocked"}, "blocked": {"raw_available"},
                    "failed": {"downloading", "raw_available"}},
    "cost": {"not_dispatched": {"unknown"}, "unknown": {"possible_frozen", "consumed", "release_pending", "no_charge_confirmed"},
             "possible_frozen": {"consumed", "release_pending"}, "consumed": {"release_pending"},
             "release_pending": {"released", "consumed"}, "released": {"consumed"},
             "no_charge_confirmed": {"consumed"}},
}


def _require(condition):
    if not condition:
        raise AssetJobError("asset_job_state_integrity_error")


def validate_job(job, previous=None, *, action="read"):
    """Pure validator used inside each write and on every authoritative read.

    File integrity is checked outside SQL by the coordinator. This validates its
    recorded evidence, immutable identities and cumulative admission history.
    """
    try:
        _validate_job(job, previous, action=action)
    except AssetJobError as error:
        if error.code == "asset_job_state_integrity_error":
            raise
        raise AssetJobError("asset_job_state_integrity_error") from None
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        raise AssetJobError("asset_job_state_integrity_error") from None


def _validate_job(j, old, *, action):
    _require(j["schema_version"] == 1 and type(j["version"]) is int and j["version"] >= 1)
    for key in ("project_id", "job_id", "submission_key"):
        identifier(j[key])
    for dimension, edges in EDGES.items():
        _require(j[dimension] in edges)
        if old and old[dimension] != j[dimension]:
            _require(j[dimension] in edges[old[dimension]])
    frozen = j["semantic"]
    _require(j["safe_error"] in SAFE_ERRORS)
    query = j["last_query"]
    if query is not None:
        _require(set(query) == {"status", "progress", "generation"} and query["status"] in REMOTE
                 and type(query["generation"]) is int and 1 <= query["generation"] <= j["query_generation"])
        _require(query["progress"] is None or (type(query["progress"]) in (int, float) and 0 <= query["progress"] <= 100))
    _require(j["semantic_digest"] == digest(frozen))
    _require(j["profile_digest"] == digest(frozen["profile"]))
    _require(frozen["profile"]["kind"] == "asset3d")
    _require(frozen["input"]["kind"] in frozen["capability"]["input_modes"])
    needs_upload = frozen["input"]["kind"] == "image" and frozen["profile"]["mode"] == "cloud_api"
    _, markers = materialize_wire(frozen["wire_template"])
    _require(markers == (1 if needs_upload else 0))
    _require((j["upload"] != "not_required") == needs_upload)
    for name in ("generation_grant", "upload_grant"):
        grant = j[name]
        if grant is not None:
            _require(grant["digest"] == digest({k: v for k, v in grant.items() if k != "digest"}))
            _require(grant["project_id"] == j["project_id"] and grant["semantic_digest"] == j["semantic_digest"]
                     and grant["profile_digest"] == j["profile_digest"] and grant["max_calls"] == 1
                     and grant["scope"] == ("generation" if name == "generation_grant" else "upload"))
    _require(j["generation_grant"] is not None)
    _require(type(j["creation_attempts"]) is int and j["creation_attempts"] in (0, 1))
    _require((j["dispatch"] == "prepared") == (j["creation_attempts"] == 0))
    _require((j["cost"] == "not_dispatched") == (j["creation_attempts"] == 0))
    _require((j["dispatch"] == "accepted") == bool(j["task_id"]))
    if j["task_id"]:
        identifier(j["task_id"])
    _require(type(j["upload_attempts"]) is int and j["upload_attempts"] >= 0
             and j["upload_attempts"] == len(j["upload_history"]))
    ids = set()
    for index, attempt in enumerate(j["upload_history"], 1):
        _require(attempt["number"] == index and attempt["grant"]["scope"] == "upload"
                 and attempt["grant"]["id"] not in ids)
        ids.add(attempt["grant"]["id"])
        identifier(attempt["attempt_id"])
        _require(attempt["grant"]["digest"] == digest({k: v for k, v in attempt["grant"].items() if k != "digest"}))
        _require(attempt["grant"]["project_id"] == j["project_id"]
                 and attempt["grant"]["semantic_digest"] == j["semantic_digest"]
                 and attempt["grant"]["profile_digest"] == j["profile_digest"])
    attempt_ids = {item["attempt_id"] for item in j["upload_history"]}
    for receipt in j["upload_receipts"]:
        _require(set(receipt) == {"attempt_id", "disposition", "file_token", "provenance"}
                 and receipt["attempt_id"] in attempt_ids and receipt["disposition"] in {"accepted", "rejected"})
        if receipt["disposition"] == "accepted":
            _require(bool(receipt["file_token"]) and receipt["provenance"]["attempt_id"] == receipt["attempt_id"])
        else:
            _require(receipt["file_token"] is None and receipt["provenance"] is None)
    if j["upload"] in {"uploading", "accepted", "rejected", "outcome_unknown"}:
        _require(j["upload_attempts"] > 0 and j["upload_grant"] == j["upload_history"][-1]["grant"])
    if j["upload"] == "accepted":
        _require(isinstance(j["file_token"], str) and 0 < len(j["file_token"]) <= 4096)
        provenance = j["token_provenance"]
        _require(provenance == token_provenance(j))
        _require(any(receipt == {"attempt_id": provenance["attempt_id"], "disposition": "accepted",
                                "file_token": j["file_token"], "provenance": provenance} for receipt in j["upload_receipts"]))
    else:
        _require(j["file_token"] is None and j["token_provenance"] is None)
    _require(type(j["token_invalid"]) is bool)
    if j["creation_attempts"]:
        _require(bool(j["creation_attempt_id"]) and j["final_wire"] is not None)
        _require(j["final_wire_digest"] == digest(j["final_wire"]))
        wire, _ = materialize_wire(frozen["wire_template"], j["file_token"])
        _require(j["final_wire"] == wire)
        if needs_upload:
            _require(j["upload"] == "accepted")
    else:
        _require(j["final_wire"] is None and j["final_wire_digest"] is None
                 and j["creation_attempt_id"] is None)
    if j["dispatch"] != "accepted":
        _require(j["remote"] == "unknown" and j["preparation"] == "not_started" and j["output"] is None)
    if j["preparation"] in {"downloading", "raw_available", "ready", "blocked", "failed"}:
        _require(j["dispatch"] == "accepted" and j["remote"] == "succeeded" and j["output"] is not None)
    if j["output"] is not None:
        _require(j["remote"] == "succeeded")
        identifier(j["output"]["artifact_id"])
        _require(set(j["output"]) == {"artifact_id", "format"} and j["output"]["format"] == "glb")
    _require(j["output_availability"] in {"unknown", "available", "expired"})
    raw = j["raw_artifact"]
    if raw is not None:
        _require(j["dispatch"] == "accepted" and j["remote"] == "succeeded")
        _require(raw["source"] == artifact_source(j, raw["sha256"], raw["byte_size"]))
        _require(raw["path"].startswith(f'.sceneloop/asset-jobs/{j["job_id"]}/raw/'))
        _require(re.fullmatch(r"[0-9a-f]{64}", raw["sha256"]) is not None and raw["byte_size"] > 0)
    if j["preparation"] in {"raw_available", "ready", "blocked"}:
        _require(raw is not None)
    if j["preparation"] == "ready":
        _require(j["checks"] == {"contract": "static_glb_v1", "sha256": raw["sha256"],
                                 "byte_size": raw["byte_size"], "passed": True})
    for count in ("query_generation", "preparation_generation"):
        _require(type(j[count]) is int and j[count] >= 0)
    for fact in j["cost_history"]:
        _require(set(fact) == {"state", "credits", "receipt_id", "kind"})
        _require(fact["state"] in EDGES["cost"] and fact["state"] not in {"unknown", "not_dispatched"})
        identifier(fact["receipt_id"])
        _require(fact["kind"] in {"task", "accounting"})
        value = fact["credits"]
        _require(value is None or (type(value) in (int, float) and math.isfinite(value) and value >= 0))
    if j["cost"] not in {"unknown", "not_dispatched"}:
        _require(bool(j["cost_history"]) and j["cost_history"][-1]["state"] == j["cost"])
    _require(j["credits"] == (j["cost_history"][-1]["credits"] if j["cost_history"] else None))
    _require(len(j["quarantined_receipts"]) <= 128)
    for receipt in j["quarantined_receipts"]:
        # Previously committed three-field records remain readable; new facts
        # add only normalized enum/number values, never a raw response or message.
        fields = {"stage", "task_id", "reason"}
        _require(set(receipt) in (fields, fields | {"remote_status", "request_generation"})
                 and receipt["stage"] in {"upload", "create", "query", "cancel", "preparation", "accounting"}
                 and receipt["reason"] in QUARANTINE_REASONS)
        if receipt["task_id"] is not None:
            identifier(receipt["task_id"])
        if "remote_status" in receipt:
            _require(receipt["remote_status"] is None or receipt["remote_status"] in REMOTE)
            generation = receipt["request_generation"]
            _require(generation is None or (type(generation) is int and 0 <= generation <= 9007199254740991))
    if old:
        for key in ("schema_version", "project_id", "job_id", "submission_key", "semantic", "semantic_digest",
                    "profile_digest", "credential_binding", "fixed_input", "generation_grant"):
            _require(j[key] == old[key])
        _require(j["version"] == old["version"] + 1)
        _require(j["creation_attempts"] >= old["creation_attempts"])
        _require(j["upload_attempts"] >= old["upload_attempts"])
        if j["creation_attempts"] != old["creation_attempts"]:
            _require(action == "create_gate" and old["dispatch"] == "prepared" and j["dispatch"] == "submitting"
                     and old["local"] == "active" and not old["token_invalid"] and j["cost"] == "unknown")
        if j["upload_attempts"] != old["upload_attempts"]:
            _require(action == "upload_gate" and old["upload"] == "pending" and j["upload"] == "uploading"
                     and old["local"] == "active" and j["creation_attempts"] == 0
                     and j["upload_attempts"] == old["upload_attempts"] + 1)
        for count in ("query_generation", "preparation_generation"):
            _require(j[count] >= old[count])
        _require(j["upload_history"][:len(old["upload_history"])] == old["upload_history"])
        _require(j["cost_history"][:len(old["cost_history"])] == old["cost_history"])
        added_cost = j["cost_history"][len(old["cost_history"]):]
        if added_cost:
            _require(action == "accounting" or action in TASK_COST_ACTIONS)
            expected_kind = "accounting" if action == "accounting" else "task"
            _require(all(fact["kind"] == expected_kind for fact in added_cost))
        _require(j["upload_receipts"][:len(old["upload_receipts"])] == old["upload_receipts"])
        _require(j["quarantined_receipts"][:len(old["quarantined_receipts"])] == old["quarantined_receipts"])
        if old["task_id"]:
            _require(j["task_id"] == old["task_id"])
        if old["raw_artifact"] is not None:
            _require(j["raw_artifact"] == old["raw_artifact"])
        if old["output"] is not None:
            _require(j["output"] == old["output"])
        if old["checks"] is not None:
            _require(j["checks"] == old["checks"])
        if old["upload"] == "accepted" and j["upload"] != "pending":
            _require(j["file_token"] == old["file_token"] and j["token_provenance"] == old["token_provenance"])
        if old["creation_attempts"]:
            for key in ("creation_attempt_id", "final_wire", "final_wire_digest", "file_token", "token_provenance", "upload_grant"):
                _require(j[key] == old[key])
        if j["local"] == "active" and old["local"] in {"waiting", "stopped"}:
            _require(action == "resume")
        if j["upload"] == "pending" and old["upload"] in {"accepted", "rejected", "outcome_unknown"}:
            _require(action == "renew_upload" and j["creation_attempts"] == 0
                     and j["upload_grant"]["id"] not in ids
                     and (old["upload"] != "accepted" or old["token_invalid"]))
        if old["upload_grant"] != j["upload_grant"]:
            _require(action == "renew_upload" and j["creation_attempts"] == 0)
        if old["dispatch"] == "outcome_unknown" and j["dispatch"] in {"accepted", "rejected"}:
            _require(action == "reconcile_create")
        if old["upload"] == "outcome_unknown" and j["upload"] in {"accepted", "rejected"}:
            _require(action == "reconcile_upload")
        if old["cost"] in {"consumed", "released", "no_charge_confirmed"} and j["cost"] != old["cost"]:
            _require(action == "accounting" and j["cost_history"][-1]["kind"] == "accounting")
        if old["cost"] in {"consumed", "released", "no_charge_confirmed"} and j["credits"] != old["credits"]:
            _require(action == "accounting" and j["cost_history"][-1]["kind"] == "accounting")


def token_provenance(j):
    profile, image = j["semantic"]["profile"], j["semantic"]["input"]
    return {"input_sha256": image["sha256"], "byte_size": image["byte_size"],
            "attempt_id": j["upload_history"][-1]["attempt_id"], "provider_id": profile["provider_id"],
            "endpoint": profile["base_url"], "profile_id": profile["id"], "revision": profile["revision"],
            "credential_binding": j["credential_binding"]}


def artifact_source(j, sha256, byte_size):
    return {"project_id": j["project_id"], "job_id": j["job_id"], "task_id": j["task_id"],
            "provider_id": j["semantic"]["profile"]["provider_id"], "profile_digest": j["profile_digest"],
            "semantic_digest": j["semantic_digest"], "wire_digest": j["final_wire_digest"],
            "artifact_id": j["output"]["artifact_id"], "sha256": sha256, "byte_size": byte_size}


def safe_projection(j):
    validate_job(j)
    # Explicit allowlist: never expose prompt, params, wire, token, key binding or private grant origin.
    fields = ("project_id", "job_id", "submission_key", "semantic_digest", "final_wire_digest", "version",
              "upload", "dispatch", "remote", "local", "preparation", "cost", "credits",
              "upload_attempts", "creation_attempts", "output_availability", "last_query", "safe_error",
              "task_id", "raw_artifact", "checks")
    return json.loads(canonical_json({key: j[key] for key in fields}))
