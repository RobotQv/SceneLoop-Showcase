"""Tripo v3.1 shape subset. Explicit calls, no SQL, task cache or POST retry."""
from decimal import Decimal
import json
from pathlib import Path
import re
import uuid

from .asset_jobs import MAX_IMAGE_BYTES, controlled_path, file_facts, image_facts
from .asset_provider import (Asset3DProvider, AssetJobError, Capability, INPUT_TOKEN, LocalArtifact,
                             ProviderReply, canonical_json, digest, identifier)
from .tripo_transport import PinnedTransport, TripoError, json_body

BASE = "https://openapi.tripo3d.ai/v3"
BASE_COM = "https://openapi.tripo3d.com/v3"
MODEL = "v3.1-20260211"
VERSION = "tripo-v3-shape-1"
RULES = {"model_seed": {"type": "integer", "min": 0, "max": 2147483647},
         "image_seed": {"type": "integer", "min": 0, "max": 2147483647},
         "face_limit": {"type": "integer", "min": 1, "max": 100000},
         "negative_prompt": {"type": "string", "max_length": 255},
         "texture": {"type": "boolean", "enum": [False]}, "pbr": {"type": "boolean", "enum": [False]},
         "geometry_quality": {"type": "string", "enum": ["standard"]},
         "enable_image_autofix": {"type": "boolean", "enum": [False]}}
STATES = {"queued": "queued", "running": "running", "success": "succeeded", "failed": "failed",
          "cancelled": "cancelled", "banned": "banned", "expired": "expired"}
REJECT_CODES = {1000, 1001, 1004, 2002, 2003, 2004, 2008, 2010, 2015, 2018}


class TripoV3Provider(Asset3DProvider):
    def __init__(self, project_root, *, transport=None):
        self.root = Path(project_root)
        self.transport = transport if transport is not None else PinnedTransport()

    def capabilities(self, profile):
        base = profile.get("base_url")
        if (profile.get("provider_id") != "tripo" or profile.get("model") != MODEL
                or profile.get("mode") != "cloud_api" or base not in (BASE, BASE_COM)):
            raise AssetJobError("unsupported_asset_capability")
        return Capability("tripo", MODEL, "cloud_api", ("text", "image"), RULES, VERSION,
                          ("https://developers.tripo3d.ai/en/docs/generation-text-to-model/standard" if base == BASE
                           else "https://developers.tripo3d.com/en/docs/generation-text-to-model/standard"),
                          "2026-10-09" if base == BASE else "2026-10-10", "documented", False)

    def validate(self, profile, input_descriptor, parameters):
        self.capabilities(profile).validate_parameters(parameters)
        kind = input_descriptor.get("kind")
        if kind not in {"text", "image"}:
            raise AssetJobError("unsupported_asset_input")
        if ((kind == "image" and set(parameters) & {"negative_prompt", "image_seed"})
                or (kind == "text" and "enable_image_autofix" in parameters)):
            raise AssetJobError("unsupported_asset_parameter")
        body = {"model": MODEL, "texture": False, "pbr": False, "geometry_quality": "standard", "face_limit": 20000}
        body.update(parameters)
        if kind == "text":
            prompt = input_descriptor.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 1024:
                raise AssetJobError("invalid_asset_prompt")
            body["prompt"] = prompt
        else:
            body.update(input=INPUT_TOKEN, enable_image_autofix=False)
        return body

    def _diagnose(self, identity, *, status=None, code=None, request_id=None, error=None, credential=None):
        # Fixed slots bound both records and filenames. No raw body/URL/error
        # text, token, prompt or Key is serialized, even on protocol failure.
        safe_id = request_id if isinstance(request_id, str) and re.fullmatch(r"req_[A-Za-z0-9]{1,64}|[0-9a-f-]{32,36}", request_id) else None
        if safe_id and credential and credential in safe_id:
            safe_id = None
        entry = {"stage": identity.stage, "http_status": status if type(status) is int and 100 <= status <= 599 else None,
                 "code": code if type(code) is int and 0 <= code <= 2147483647 else None,
                 "request_id": safe_id, "error": error}
        try:
            from .asset_artifacts import write_json_once
            for index in range(128):
                path = controlled_path(self.root, f".sceneloop/asset-jobs/{identifier(identity.job_id)}/diagnostics/{index:03}.json")
                try:
                    write_json_once(path, entry)
                    return
                except FileExistsError:
                    continue
        except OSError:
            pass

    def _api(self, identity, profile, credential, method, path, *, body=None, content_type=None):
        self.capabilities(profile)
        if not isinstance(credential, str) or not 1 <= len(credential) <= 4096 or any(not 33 <= ord(c) <= 126 for c in credential):
            raise AssetJobError("asset_credential_unavailable")
        headers = {"Authorization": "Bearer " + credential, "Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        status, code, request_id = None, None, None
        try:
            with self.transport.open(method, profile["base_url"] + path, headers=headers, body=body,
                                     timeout=180 if method == "POST" else 60) as response:
                status = response.status
                document = json_body(response)
            code, request_id = document.get("code"), document.get("request_id")
            if type(code) is not int:
                raise TripoError("tripo_protocol")
            if status != 200 or code != 0:
                if method == "POST" and status in {200, 400, 401, 403} and code in REJECT_CODES:
                    self._diagnose(identity, status=status, code=code, request_id=request_id, error="tripo_http", credential=credential)
                    return None
                raise TripoError("tripo_http", status=status)
            if not isinstance(document.get("data"), dict):
                raise TripoError("tripo_protocol")
            self._diagnose(identity, status=status, code=code, request_id=request_id, credential=credential)
            return document["data"]
        except TripoError as error:
            self._diagnose(identity, status=status, code=code, request_id=request_id, error=error.code, credential=credential)
            raise

    def _protocol(self, identity, credential):
        self._diagnose(identity, error="tripo_protocol", credential=credential)
        raise TripoError("tripo_protocol")

    def upload_input(self, identity, profile, credential, input_path):
        raw, _ = file_facts(Path(input_path), MAX_IMAGE_BYTES)
        mime = image_facts(raw)["mime"]
        boundary = "sceneloop" + uuid.uuid4().hex
        extension = "png" if mime == "image/png" else "jpg"
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="input.{extension}"\r\n'
                f'Content-Type: {mime}\r\n\r\n').encode() + raw + f"\r\n--{boundary}--\r\n".encode()
        data = self._api(identity, profile, credential, "POST", "/files", body=body,
                         content_type="multipart/form-data; boundary=" + boundary)
        if data is None:
            return ProviderReply(identity, disposition="rejected")
        token = data.get("file_token")
        if not isinstance(token, str) or not re.fullmatch(r"file_[A-Za-z0-9_.-]{1,4000}", token):
            self._protocol(identity, credential)
        return ProviderReply(identity, file_token=token)

    def submit(self, identity, profile, credential, wire):
        if not isinstance(wire, dict) or ("prompt" in wire) == ("input" in wire):
            raise AssetJobError("unsupported_asset_parameter")
        kind = "text" if "prompt" in wire else "image"
        special = {"model", "prompt", "input"}
        expected = self.validate(profile, {"kind": kind, **({"prompt": wire["prompt"]} if kind == "text" else {})},
                                 {key: value for key, value in wire.items() if key not in special})
        if kind == "image":
            token = wire.get("input")
            if not isinstance(token, str) or not re.fullmatch(r"file_[A-Za-z0-9_.-]{1,4000}", token):
                raise AssetJobError("unsupported_asset_parameter")
            expected["input"] = token
        if wire != expected:
            raise AssetJobError("unsupported_asset_parameter")
        data = self._api(identity, profile, credential, "POST", f"/generation/{kind}-to-model",
                         body=canonical_json(wire).encode("utf-8"), content_type="application/json")
        if data is None:
            return ProviderReply(identity, disposition="rejected")
        task = data.get("task_id")
        try:
            identifier(task)
        except AssetJobError:
            self._protocol(identity, credential)
        # Optional echoes are evidence, not defaults. A present malformed echo
        # becomes a fixed invalid identity so the existing coordinator can
        # quarantine the safe original task/attempt without serializing body.
        model, input_kind = None, None
        if "model" in data:
            value = data["model"]
            model = value if type(value) is str and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value) else "invalid"
        if "type" in data:
            value = data["type"]
            input_kind = {"text_to_model": "text", "image_to_model": "image"}.get(value, "invalid") if type(value) is str else "invalid"
        return ProviderReply(identity, task_id=task, model=model, input_kind=input_kind)

    @staticmethod
    def _artifact(task, kind):
        return "shape-" + kind + "-" + digest({"task_id": task})

    def _task(self, identity, profile, credential):
        task = identifier(identity.task_id)
        data = self._api(identity, profile, credential, "GET", "/tasks/" + task)
        try:
            identifier(data.get("task_id"))
        except AssetJobError:
            self._protocol(identity, credential)
        if data.get("type") not in {"text_to_model", "image_to_model"}:
            self._protocol(identity, credential)
        return data

    def query(self, identity, profile, credential):
        data = self._task(identity, profile, credential)
        kind = "text" if data["type"] == "text_to_model" else "image"
        status = STATES.get(data.get("status"), "unknown")
        cost = None
        if "credits_consumed" in data:
            value = data["credits_consumed"]
            if type(value) not in (int, Decimal) or not Decimal(value).is_finite() or not 0 <= value <= 1000000000 or Decimal(value).as_tuple().exponent < -2:
                self._protocol(identity, credential)
            # This field is a consumed fact, never a refund/accounting receipt.
            # Preserve the existing numeric SQL schema, at most two decimal places.
            if status == "succeeded" or value > 0:
                cost = {"state": "consumed", "credits": float(value), "kind": "task",
                        "receipt_id": "tripo-" + digest({"task": data["task_id"], "credits": str(Decimal(value).quantize(Decimal("0.01")))})}
        output = {"artifact_id": self._artifact(data["task_id"], kind), "format": "glb"} if status == "succeeded" else None
        progress = data.get("progress")
        if isinstance(progress, Decimal):
            if not progress.is_finite() or not 0 <= progress <= 100:
                self._protocol(identity, credential)
            progress = float(progress)
        return ProviderReply(identity, task_id=data["task_id"], remote_status=status, progress=progress,
                             cost=cost, output=output, input_kind=kind, model=data.get("model"),
                             output_availability="expired" if status == "expired" else "available" if output else None)

    def cancel(self, identity, profile, credential):
        raise AssetJobError("asset_cancel_unsupported")

    def check_artifact(self, raw):
        from .asset_artifacts import check_shape
        check_shape(raw)

    def fetch_result(self, identity, profile, credential, output):
        from .asset_artifacts import download_glb
        # Never reuse a signed URL or an in-memory task/type cache after restart.
        for attempt in range(2):
            data = self._task(identity, profile, credential)
            kind = "text" if data["type"] == "text_to_model" else "image"
            if (data["task_id"] != identity.task_id or data.get("model", MODEL) != MODEL
                    or data.get("status") != "success" or output != {"artifact_id": self._artifact(identity.task_id, kind), "format": "glb"}):
                self._protocol(identity, credential)
            detail = data.get("output")
            if not isinstance(detail, dict):
                self._protocol(identity, credential)
            url = detail.get("model_url")
            try:
                relative = download_glb(self.transport, url, self.root, identity.job_id)
                self._diagnose(identity, status=200, credential=credential)
                return LocalArtifact(identity, output["artifact_id"], relative)
            except TripoError as error:
                self._diagnose(identity, status=error.status, error=error.code, credential=credential)
                if error.status != 403 or attempt:
                    raise
        raise TripoError("tripo_download_http")
