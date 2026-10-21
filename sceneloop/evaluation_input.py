"""Sealed evaluation input manifests and shared integrity validation.

Every byte or measurement actually sent to one Judge attempt is registered
here before the provider call.  After a terminal state the manifest is sealed
and its digest is bound to the judgement.  Registration and cache reuse call
the same ``validate_evaluation_inputs`` so a tampered consumed file can never
silently pass either path.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import uuid
from pathlib import Path


class EvaluationInputError(Exception):
    def __init__(self, message, code="evaluation_input_error"):
        super().__init__(message)
        self.code = code


def _digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class EvaluationInputBuilder:
    """Accumulate the exact inputs sent to one Judge attempt, then seal.

    Items are added at the send boundary so the recorded hash always matches
    the bytes handed to the provider.  ``seal`` freezes the manifest; further
    mutation raises.  Files produced but never sent are never registered.
    """

    def __init__(self, *, evaluation_id, run_id, attempt_id, kind, project_id):
        if not all(isinstance(v, str) and v for v in (evaluation_id, run_id, attempt_id, kind, project_id)):
            raise EvaluationInputError("evaluation identity fields must be non-empty strings")
        self.evaluation_id = evaluation_id
        self.run_id = run_id
        self.attempt_id = attempt_id
        self.kind = kind
        self.project_id = project_id
        self.items: list[dict] = []
        self._sealed = False
        self._seal_state: str | None = None
        self._sequence = 0
        self.requests = []
        self.context = None
        self._image_objects = {}
        self._persisted_digest = None

    # -- registration -------------------------------------------------

    def _next_sequence(self):
        self._sequence += 1
        return self._sequence

    def _append(self, item):
        if self._sealed:
            raise EvaluationInputError("evaluation input already sealed", "evaluation_input_sealed")
        item["sequence"] = self._next_sequence()
        item["input_id"] = uuid.uuid4().hex
        item["sent_requests"] = []
        self.items.append(item)
        return item["input_id"]

    def add_image(self, role, path, *, evidence_id=None, scene_identity=None,
                  view=None, camera=None, artifact_id=None, sent_content=None, observation=None):
        """Stage one image with the exact bytes prepared for Provider delivery."""
        if self._sealed:
            raise EvaluationInputError("evaluation input already sealed", "evaluation_input_sealed")
        path = Path(path)
        if not path.is_file():
            raise EvaluationInputError(f"input image missing: {path}", "evaluation_input_file_missing")
        content_hash = (hashlib.sha256(sent_content).hexdigest()
                        if isinstance(sent_content, bytes) else _file_hash(path))
        return self._append({
            **({"observation": copy.deepcopy(observation)} if observation is not None else {}),
            "kind": "image", "role": role, "evidence_id": evidence_id,
            "scene_identity": copy.deepcopy(scene_identity),
            "path": str(path), "artifact_id": artifact_id,
            "content_hash": content_hash,
            "view": copy.deepcopy(view), "camera": copy.deepcopy(camera),
        })

    def add_text(self, role, text, *, evidence_id=None, scene_identity=None):
        """Register a text block (goal, issues, scene summary) actually sent."""
        if self._sealed:
            raise EvaluationInputError("evaluation input already sealed", "evaluation_input_sealed")
        if not isinstance(text, str):
            raise EvaluationInputError("text input must be a string")
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return self._append({
            "kind": "text", "role": role, "evidence_id": evidence_id,
            "scene_identity": copy.deepcopy(scene_identity),
            "text": text, "content_hash": content_hash,
        })

    def add_measurement(self, role, document, *, evidence_id=None, scene_identity=None,
                        source_path=None, source_hash=None):
        """Stage a geometry measurement for a later Provider request."""
        if self._sealed:
            raise EvaluationInputError("evaluation input already sealed", "evaluation_input_sealed")
        if not isinstance(document, dict):
            raise EvaluationInputError("measurement must be a dict")
        content_hash = _digest(document)
        return self._append({
            "kind": "measurement", "role": role, "evidence_id": evidence_id,
            "scene_identity": copy.deepcopy(scene_identity),
            "document": copy.deepcopy(document), "content_hash": content_hash,
            "source_path": str(source_path) if source_path is not None else None,
            "source_hash": source_hash,
        })

    def bind_context(self, context):
        if self._sealed or self.context is not None:
            raise EvaluationInputError("evaluation context already fixed")
        self.context = copy.deepcopy(context)

    def bind_image_payload(self, content, item_id):
        """Associate the exact transient image object with its manifest row."""
        self._image_objects[id(content)] = item_id

    def _request_document(self, messages):
        """Strip image base64 and hidden reasoning, retain exact public text."""
        image_ids = set()
        def clean(value):
            if isinstance(value, dict):
                if value.get("type") == "image_url":
                    url = (value.get("image_url") or {}).get("url")
                    if not isinstance(url, str) or "," not in url:
                        raise EvaluationInputError("invalid image payload")
                    raw = base64.b64decode(url.split(",", 1)[1], validate=True)
                    item_id = self._image_objects.get(id(value))
                    item = next((row for row in self.items if row["input_id"] == item_id), None)
                    if item is None or item["content_hash"] != hashlib.sha256(raw).hexdigest():
                        raise EvaluationInputError("unregistered image payload")
                    image_ids.add(item_id)
                    return {"type": "image_url", "input_id": item_id,
                            "content_hash": item["content_hash"]}
                return {key: clean(item) for key, item in value.items()
                        if key not in {"reasoning_content", "refusal"}}
            if isinstance(value, list):
                return [clean(item) for item in value]
            return value
        return clean(messages), image_ids

    def prepare_request(self, messages, tools, *, config_identity=None):
        if self._sealed:
            raise EvaluationInputError("evaluation input already sealed")
        number = len(self.requests) + 1
        document, image_ids = self._request_document(messages)
        for item in self.items:
            if item["input_id"] in image_ids or (
                    item["kind"] == "measurement" and item.get("evidence_id")
                    and any(item["evidence_id"] in str(message.get("content", ""))
                            for message in messages if isinstance(message, dict))):
                item["sent_requests"].append(number)
        payload = {"messages": document, "tools": copy.deepcopy(tools),
                   "config_identity": copy.deepcopy(config_identity)}
        self.requests.append({"sequence": number, "state": "scheduled",
                              "payload_digest": _digest(payload), "payload": payload})
        failures = validate_evaluation_inputs(
            [item for item in self.items if number in item["sent_requests"]])
        if failures:
            raise EvaluationInputError("input changed before send", "evaluation_input_integrity")
        return number

    def returned(self, number):
        if self._sealed or number != len(self.requests):
            raise EvaluationInputError("invalid request sequence")
        self.requests[-1]["state"] = "returned"

    def persist(self, state):
        manifest = self.manifest()
        if self._persisted_digest is None:
            state.register_evaluation_input(manifest)
        else:
            state.update_evaluation_input(manifest, expected_digest=self._persisted_digest)
        self._persisted_digest = manifest["input_digest"]

    # -- sealing --------------------------------------------------------

    def seal(self, *, final_state):
        """Freeze the manifest with its terminal state; returns input_digest."""
        if self._sealed:
            return self.input_digest
        if final_state not in {"completed", "failed", "cancelled", "unknown"}:
            raise EvaluationInputError(f"invalid seal state: {final_state}")
        self._sealed = True
        self._seal_state = final_state
        return self.input_digest

    @property
    def sealed(self):
        return self._sealed

    @property
    def seal_state(self):
        return self._seal_state

    @property
    def input_digest(self):
        return _digest(self.manifest_body())

    def manifest_body(self):
        return {"schema_version": 1, "evaluation_id": self.evaluation_id,
                "input_id": self.evaluation_id, "run_id": self.run_id,
                "attempt_id": self.attempt_id, "kind": self.kind,
                "project_id": self.project_id, "context": copy.deepcopy(self.context),
                "items": copy.deepcopy(self.items), "requests": copy.deepcopy(self.requests)}

    def manifest(self):
        return {**self.manifest_body(), "input_digest": self.input_digest,
                "sealed": self._sealed, "seal_state": self._seal_state}


def validate_evaluation_inputs(items, *, project_root=None):
    """Re-verify every consumed input item against its recorded hash.

    Returns a list of ``{"input_id", "reason"}`` failures.  An empty list
    means every consumed image, measurement and text block still matches.
    Unrelated diagnostic files that were never registered are ignored.
    """
    from .observation_contract import validate_observation_metadata, comparison_identity
    failures = []
    pairs = {}
    for item in items:
        input_id = item.get("input_id", "?")
        kind = item.get("kind")
        if kind == "image":
            try:
                observation = item.get("observation")
                view = item.get("view") if isinstance(item.get("view"), dict) else None
                if item.get("view") in ("main", "auxiliary"):
                    view = {"preset": item["view"]}
                actual = validate_observation_metadata(observation or {}, view)
                if observation and observation.get("comparison_group") and item.get("sent_requests"):
                    group = pairs.setdefault(observation["comparison_group"], [])
                    group.append((item, comparison_identity(actual), observation.get("comparison_role")))
            except ValueError:
                failures.append({"input_id": input_id, "reason": "observation_identity_mismatch"})
            raw = item.get("path")
            if not isinstance(raw, str) or not raw:
                failures.append({"input_id": input_id, "reason": "invalid_path"})
                continue
            path = Path(raw)
            if project_root is not None:
                root = Path(project_root)
                if not path.is_absolute():
                    path = root / path
                try:
                    if not path.resolve().is_relative_to(root.resolve()):
                        failures.append({"input_id": input_id, "reason": "path_escape"})
                        continue
                except (OSError, ValueError):
                    failures.append({"input_id": input_id, "reason": "path_escape"})
                    continue
            if not path.is_file():
                failures.append({"input_id": input_id, "reason": "file_missing"})
                continue
            try:
                actual = _file_hash(path)
            except OSError:
                failures.append({"input_id": input_id, "reason": "file_unreadable"})
                continue
            if actual != item.get("content_hash"):
                failures.append({"input_id": input_id, "reason": "content_changed"})
        elif kind == "measurement":
            source_path = item.get("source_path")
            source_hash = item.get("source_hash")
            if source_path and source_hash:
                source = Path(source_path)
                if project_root is not None:
                    root = Path(project_root).resolve()
                    if not source.is_absolute():
                        source = root / source
                    if not source.resolve().is_relative_to(root):
                        failures.append({"input_id": input_id, "reason": "path_escape"})
                        continue
                if not source.is_file():
                    failures.append({"input_id": input_id, "reason": "source_missing"})
                    continue
                try:
                    actual_source_hash = _file_hash(source)
                except OSError:
                    failures.append({"input_id": input_id, "reason": "source_unreadable"})
                    continue
                if actual_source_hash != source_hash:
                    failures.append({"input_id": input_id, "reason": "source_changed"})
                    continue
            document = item.get("document")
            if not isinstance(document, dict):
                failures.append({"input_id": input_id, "reason": "invalid_measurement"})
                continue
            if _digest(document) != item.get("content_hash"):
                failures.append({"input_id": input_id, "reason": "content_changed"})
        elif kind == "text":
            text = item.get("text")
            if not isinstance(text, str):
                failures.append({"input_id": input_id, "reason": "invalid_text"})
                continue
            if hashlib.sha256(text.encode("utf-8")).hexdigest() != item.get("content_hash"):
                failures.append({"input_id": input_id, "reason": "content_changed"})
        else:
            failures.append({"input_id": input_id, "reason": "unknown_kind"})
    for group in pairs.values():
        if (len(group) != 2 or {row[2] for row in group} != {"before", "current"}
                or group[0][1] != group[1][1]):
            failures.extend({"input_id": row[0].get("input_id", "?"), "reason": "observation_pair_mismatch"} for row in group)
    return failures


def result_digest(result):
    """Normalized digest of a judgement result for idempotent receipt binding."""
    if isinstance(result, dict):
        normalized = {k: result[k] for k in sorted(result) if k not in {"time"}}
    else:
        normalized = result
    return _digest(normalized)


def validate_manifest(manifest, *, require_sent=False):
    """Verify host identity, ordered rows, content and computed digest."""
    if not isinstance(manifest, dict):
        raise EvaluationInputError("invalid manifest", "evaluation_input_invalid")
    if manifest.get("schema_version") != 1:
        raise EvaluationInputError("unsupported manifest schema", "evaluation_input_invalid")
    fields = ("evaluation_id", "input_id", "run_id", "attempt_id", "kind", "project_id")
    if any(not isinstance(manifest.get(key), str) or not manifest[key] for key in fields):
        raise EvaluationInputError("missing manifest identity", "evaluation_input_invalid")
    if manifest["evaluation_id"] != manifest["input_id"]:
        raise EvaluationInputError("evaluation/input identity mismatch", "evaluation_input_invalid")
    items = manifest.get("items")
    requests = manifest.get("requests")
    if not isinstance(items, list) or not isinstance(requests, list):
        raise EvaluationInputError("invalid manifest rows", "evaluation_input_invalid")
    if [item.get("sequence") for item in items] != list(range(1, len(items) + 1)):
        raise EvaluationInputError("conflicting input sequence", "evaluation_input_invalid")
    if len({item.get("input_id") for item in items}) != len(items):
        raise EvaluationInputError("duplicate input id", "evaluation_input_invalid")
    if [row.get("sequence") for row in requests] != list(range(1, len(requests) + 1)):
        raise EvaluationInputError("conflicting request sequence", "evaluation_input_invalid")
    image_rows = {item.get("input_id"): item for item in items if item.get("kind") == "image"}
    actual_image_requests = {item_id: [] for item_id in image_rows}
    for row in requests:
        if row.get("state") not in {"scheduled", "returned"}:
            raise EvaluationInputError("invalid request state", "evaluation_input_invalid")
        payload = row.get("payload")
        if (not isinstance(payload, dict) or not isinstance(payload.get("messages"), list)
                or not isinstance(payload.get("tools"), list)
                or _digest(payload) != row.get("payload_digest")):
            raise EvaluationInputError("request content mismatch", "evaluation_input_invalid")
        def inspect(value):
            if isinstance(value, dict):
                if value.get("type") == "image_url":
                    item_id = value.get("input_id")
                    item = image_rows.get(item_id)
                    if (item is None or value.get("content_hash") != item.get("content_hash")
                            or set(value) != {"type", "input_id", "content_hash"}):
                        raise EvaluationInputError("image request mismatch", "evaluation_input_invalid")
                    if row["sequence"] not in actual_image_requests[item_id]:
                        actual_image_requests[item_id].append(row["sequence"])
                for child in value.values():
                    inspect(child)
            elif isinstance(value, list):
                for child in value:
                    inspect(child)
        inspect(payload["messages"])
    for item in items:
        sent = item.get("sent_requests", [])
        if not isinstance(sent, list) or any(type(seq) is not int or seq < 1 or seq > len(requests)
                                              for seq in sent):
            raise EvaluationInputError("invalid sent request", "evaluation_input_invalid")
        if len(sent) != len(set(sent)):
            raise EvaluationInputError("duplicate sent request", "evaluation_input_invalid")
        if item.get("kind") == "image" and sent != actual_image_requests[item.get("input_id")]:
            raise EvaluationInputError("image send sequence mismatch", "evaluation_input_invalid")
        if item.get("kind") == "text" and isinstance(item.get("text"), str):
            if hashlib.sha256(item["text"].encode("utf-8")).hexdigest() != item.get("content_hash"):
                raise EvaluationInputError("text content mismatch", "evaluation_input_invalid")
        if item.get("kind") == "measurement" and isinstance(item.get("document"), dict):
            if _digest(item["document"]) != item.get("content_hash"):
                raise EvaluationInputError("measurement content mismatch", "evaluation_input_invalid")
    body = {key: manifest.get(key) for key in (
        "schema_version", "evaluation_id", "input_id", "run_id", "attempt_id",
        "kind", "project_id", "context", "items", "requests")}
    if _digest(body) != manifest.get("input_digest"):
        raise EvaluationInputError("input digest mismatch", "evaluation_input_mismatch")
    if require_sent and (not requests or any(row["state"] != "returned" for row in requests)
                         or not manifest.get("sealed")
                         or manifest.get("seal_state") != "completed"):
        raise EvaluationInputError("input not complete", "evaluation_input_not_sealed")
    return True
