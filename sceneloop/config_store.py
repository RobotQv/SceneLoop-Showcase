"""Named model profiles with encrypted credentials and optimistic revision checks.

A server must hold its process data-directory lock while using this repository.
Only explicitly revealed credentials or the checked runtime resolver return keys.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import urlsplit
import uuid

from .connection_verify import safe_conclusion

TEXT_FIELDS = ("name", "kind", "preset", "provider_id", "model", "base_url", "credential_env", "mode", "python_path", "model_dir")
BRAIN_SETTING_FIELDS = ("max_output_tokens", "request_timeout_seconds", "reasoning_effort", "thinking_budget")
# Provider-specific Chat Completions capabilities, verified 2026-09-18.
# Keep in sync with ui/profile-store.js; parity is checked offline.
REASONING_CAPABILITIES = {
    "deepseek::deepseek-flash": {"efforts": ["none", "low", "high", "max"], "budget_max": None},
    "zai::glm-5.3-flash": {"efforts": ["low", "high", "max"], "budget_max": None},
    "dashscope::qwen3.8-flash": {"efforts": ["none", "low", "medium", "xhigh"], "budget_max": 262144},
}


def reasoning_capability(profile):
    provider, model = profile.get("provider_id"), profile.get("model")
    if not isinstance(provider, str) or not isinstance(model, str):
        return {"efforts": [], "budget_max": None}
    return REASONING_CAPABILITIES.get(f"{provider.strip().lower()}::{model.strip().lower()}",
                                      {"efforts": [], "budget_max": None})

BINDING_FIELDS = ("kind", "provider_id", "model", "base_url", "credential_mode", "credential_env", "mode", "python_path", "model_dir") + BRAIN_SETTING_FIELDS


def binding_fields(profile):
    # Old profiles/fingerprints keep their original shape. Only the new local
    # Hunyuan profile binds the separately configured trusted code repository.
    return BINDING_FIELDS + (("repo_dir",) if profile.get("provider_id") == "hunyuan"
                             and profile.get("mode") == "local_process" and "repo_dir" in profile else ())
KINDS = ("brain", "image", "asset3d")
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
ENVIRONMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_BYTES = 5 * 1024 * 1024


class ProfileError(Exception):
    def __init__(self, message, code="invalid_profile", status=400):
        super().__init__(message)
        self.code, self.status = code, status


def _id(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ProfileError("配置 ID 无效。")
    return value


def _url(value):
    try:
        if CONTROL.search(value) or "\\" in value or any(char.isspace() for char in value) or "?" in value or "#" in value:
            raise ValueError
        parsed = urlsplit(value)
        if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
        host = parsed.hostname
        if "%" in host:
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            ascii_host = host.rstrip(".").encode("idna").decode("ascii")
            labels = ascii_host.split(".")
            if len(ascii_host) > 253 or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels):
                raise ValueError
        if parsed.scheme == "http":
            host = parsed.hostname.lower()
            if host != "localhost":
                try:
                    local = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    local = False
                if not local:
                    raise ValueError
    except ValueError:
        raise ProfileError("接口地址必须使用 HTTPS；仅本机回环地址允许 HTTP，地址不能包含账号、密码、查询参数或片段。") from None


def normalize_brain_settings(value, kind):
    """Return validated non-secret request settings; omitted values use defaults."""
    if not isinstance(value, dict):
        raise ProfileError("大脑请求设置必须是 JSON 对象。")
    result = {}
    for field, lower, upper, label in (("max_output_tokens", 1, 1048576, "输出 token 上限"),
                                      ("request_timeout_seconds", 30, 900, "请求等待秒数")):
        setting = value.get(field)
        if setting is not None:
            if kind != "brain":
                raise ProfileError("输出上限和请求等待时间仅适用于大脑配置。")
            if type(setting) is not int or not lower <= setting <= upper:
                raise ProfileError(f"{label}需要为 {lower}–{upper} 的整数，或留空使用默认值。")
        result[field] = setting
    effort, budget = value.get("reasoning_effort"), value.get("thinking_budget")
    capability = reasoning_capability(value)
    if effort is not None or budget is not None:
        if kind != "brain":
            raise ProfileError("思考参数仅适用于大脑配置。")
        if effort is not None and (not isinstance(effort, str) or effort not in capability["efforts"]):
            raise ProfileError("此服务商/型号不支持所选思考强度，请选择有效档位或跟随供应商。")
        if budget is not None:
            upper = capability["budget_max"]
            if upper is None or type(budget) is not int or not 1 <= budget <= upper:
                raise ProfileError("此型号不支持所填思考预算；Qwen3.8-Flash 需填 1–262144 的整数。")
        if effort is not None and budget is not None:
            raise ProfileError("思考强度与思考 token 预算不能同时设置。")
    result.update(reasoning_effort=effort, thinking_budget=budget)
    return result


def _normalize(value, *, legacy_read=False):
    if not isinstance(value, dict):
        raise ProfileError("配置必须是 JSON 对象。")
    result = {}
    for field in TEXT_FIELDS:
        content = value.get(field, "")
        if not isinstance(content, str) or len(content) > 2048 or CONTROL.search(content):
            raise ProfileError("配置文本字段的类型、长度或字符无效。")
        result[field] = content.strip()
    if not result["name"] or len(result["name"]) > 200:
        raise ProfileError("请填写 1–200 字的配置名称。")
    if result["kind"] not in KINDS:
        raise ProfileError("请选择大脑、图像生成或 3D 生成。")
    result.update(normalize_brain_settings(value, result["kind"]))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", result["provider_id"]):
        raise ProfileError("请填写有效的服务商标识。")
    if not result["model"] or len(result["model"]) > 256:
        raise ProfileError("请填写 1–256 字的模型名称。")
    env = result["credential_env"]
    if env and (not ENVIRONMENT.fullmatch(env) or re.match(r"(?:sk[-_]|AIza|eyJ|gh[pousr]_|xox[baprs]-)", env)):
        raise ProfileError("环境变量字段只填写变量名，不要填写密钥。")
    credential_mode = value.get("credential_mode", "env" if env else "none")
    if credential_mode not in ("stored", "env", "none"):
        raise ProfileError("请选择有效的密钥保存方式。")
    result["credential_mode"] = credential_mode
    if credential_mode == "env" and not env:
        raise ProfileError("请填写密钥环境变量名。")
    if credential_mode != "env":
        result["credential_env"] = ""
    mode = result["mode"]
    if result["kind"] == "asset3d":
        if mode not in ("cloud_api", "local_http", "local_process"):
            raise ProfileError("请选择云端 API、本地服务或本地进程。")
        if mode == "cloud_api" and credential_mode == "none":
            raise ProfileError("云端 3D 服务需要配置密钥或环境变量。")
        if mode == "local_process":
            if not result["python_path"] or not result["model_dir"]:
                raise ProfileError("本地进程需要 Python 可执行文件路径和模型目录。")
            if credential_mode != "none":
                raise ProfileError("本地进程请使用无需密钥模式。")
            if result["provider_id"] == "hunyuan" and not (legacy_read and "repo_dir" not in value):
                repo = value.get("repo_dir")
                if (not isinstance(repo, str) or not repo or len(repo) > 2048 or CONTROL.search(repo)
                        or not Path(repo).is_absolute() or repo.startswith(("\\\\", "//"))
                        or ".." in Path(repo).parts):
                    raise ProfileError("Hunyuan需要明确的本地代码repo_dir；model_dir单独指向权重目录。")
                result["repo_dir"] = repo
    elif mode not in ("", "cloud_api", "local_http"):
        raise ProfileError("此模型类型不支持本地进程模式。")
    needs_url = result["kind"] != "asset3d" or mode != "local_process"
    if needs_url and not result["base_url"]:
        raise ProfileError("请填写接口地址。")
    if result["base_url"]:
        _url(result["base_url"])
    return result


def _provided_key(value):
    # None and an empty editor field retain the saved key during update.
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 4096 or any(not 33 <= ord(char) <= 126 for char in value):
        raise ProfileError("API 密钥需为 1–4096 个可见 ASCII 字符，不能包含空格或换行。", "invalid_api_key")
    return value


class ProfileRepository:
    def __init__(self, root, protector=None):
        if protector is None:
            from .credentials import SecretProtector
            protector = SecretProtector()
        self.protector = protector
        self.root = Path(root)
        self.path = self.root / "profiles.json"
        self.lock = threading.RLock()
        self.verification_lock = threading.Lock()
        self.verification_receipts = {}
        try:
            if self.root.is_symlink() or self.path.is_symlink():
                raise ProfileError("配置存储路径不能是符号链接。", "configuration_storage_error", 500)
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._profiles = self._read() if self.path.exists() else []
            if not self.path.exists():
                self._write(self._profiles)
        except OSError:
            raise ProfileError("无法打开配置存储目录。", "configuration_storage_error", 500) from None

    def storage_status(self):
        # Explicit whitelist: a backend may add private diagnostic fields later.
        status = self.protector.status()
        return {"available": bool(status.get("available")), "backend": str(status.get("backend", "unavailable")),
                "label": str(status.get("label", "")), "reason": str(status.get("reason", ""))}

    def _read(self):
        try:
            if self.path.stat().st_size > MAX_BYTES:
                raise ValueError
            document = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(document, dict) or set(document) != {"schema_version", "profiles"} or type(document["schema_version"]) is not int or document["schema_version"] != 1 or not isinstance(document["profiles"], list):
                raise ValueError
            result, ids, names = [], set(), set()
            for item in document["profiles"]:
                if not isinstance(item, dict):
                    raise ValueError
                expected = set(TEXT_FIELDS) | {"credential_mode", "id", "revision", "protected_key"}
                # Earlier stores lack these optional non-secret settings. Normalize
                # only in memory; a read never rewrites the credential store.
                if not expected <= set(item) <= expected | set(BRAIN_SETTING_FIELDS) | {"repo_dir", "verified_at", "connection_check", "connection_binding"}:
                    raise ValueError
                normalized = _normalize(item,legacy_read=True)
                identifier = _id(item["id"])
                revision = item["revision"]
                if type(revision) is not int or not 1 <= revision <= 9007199254740991:
                    raise ValueError
                if any(normalized[field] != item.get(field) for field in normalized):
                    raise ValueError
                name = (item["kind"], item["name"].casefold())
                ciphertext = item["protected_key"]
                if not isinstance(ciphertext, str) or len(ciphertext) > 32768 or bool(ciphertext) != (item["credential_mode"] == "stored"):
                    raise ValueError
                if identifier in ids or name in names:
                    raise ValueError
                ids.add(identifier)
                names.add(name)
                verified_at = item.get("verified_at")
                if verified_at is not None:
                    if not isinstance(verified_at, str) or len(verified_at) > 40:
                        raise ValueError
                    datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
                restored = {**normalized, "id": identifier, "revision": revision, "protected_key": ciphertext,
                            "verified_at": verified_at}
                check = item.get("connection_check")
                if check is not None:
                    if (type(check) is not dict or set(check) != {"status", "code", "http_status", "checked_at"}
                            or safe_conclusion({k: v for k, v in check.items() if k != "checked_at"}) !=
                               {k: v for k, v in check.items() if k != "checked_at"}):
                        raise ValueError
                    checked_at = check["checked_at"]
                    if (not isinstance(checked_at, str) or len(checked_at) > 40 or not checked_at.endswith("Z")
                            or datetime.fromisoformat(checked_at.replace("Z", "+00:00")).utcoffset() != timezone.utc.utcoffset(None)
                            or verified_at != (checked_at if check["status"] == "authenticated" else None)):
                        raise ValueError
                    binding = item.get("connection_binding")
                    if not isinstance(binding, str) or not re.fullmatch(r"[0-9a-f]{64}", binding):
                        raise ValueError
                    restored.update(connection_check=check, connection_binding=binding)
                elif item.get("connection_binding") is not None:
                    raise ValueError
                result.append(restored)
            return result
        except (OSError, ValueError, TypeError, KeyError, ProfileError):
            raise ProfileError("配置存储文件损坏或版本不受支持；原文件已保留，请恢复备份后重启。", "configuration_corrupt", 500) from None

    def _write(self, profiles):
        temporary = None
        try:
            serialized = json.dumps({"schema_version": 1, "profiles": profiles}, ensure_ascii=False, indent=2).encode("utf-8")
            if len(serialized) > MAX_BYTES:
                raise ProfileError("配置库已超过本地存储大小限制。", "configuration_limit", 400)
            descriptor, name = tempfile.mkstemp(prefix="profiles-", suffix=".tmp", dir=self.root)
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(serialized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise ProfileError("保存配置失败；此前保存的配置未改动。", "configuration_storage_error", 500) from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _public(profile):
        check = profile.get("connection_check")
        verified_at = profile.get("verified_at")
        if check is not None and profile.get("connection_binding") != ProfileRepository._stored_binding(profile):
            check, verified_at = None, None
        result = {**{field: profile[field] for field in (*TEXT_FIELDS, *BRAIN_SETTING_FIELDS)}, "id": profile["id"],
                "revision": profile["revision"], "credential_mode": profile["credential_mode"],
                "is_draft": False, "verification": "verified" if verified_at else "unverified",
                "verified_at": verified_at, "has_secret": bool(profile["protected_key"])}
        if check is not None:
            result["connection_check"] = dict(check)
        if "repo_dir" in binding_fields(profile):
            result["repo_dir"] = profile["repo_dir"]
        return result

    @staticmethod
    def _stored_binding(profile):
        # Stored credentials bind ciphertext without decrypting on list/read.
        # Environment credentials bind the value, so rotation invalidates results.
        credential = (os.environ.get(profile["credential_env"], "") if profile["credential_mode"] == "env"
                      else profile["protected_key"] if profile["credential_mode"] == "stored" else "")
        value = {"config": {field: profile[field] for field in (*TEXT_FIELDS, *BRAIN_SETTING_FIELDS, "credential_mode")},
                 "id": profile["id"], "revision": profile["revision"],
                 "credential": hashlib.sha256(credential.encode()).hexdigest()}
        if "repo_dir" in binding_fields(profile):
            value['config']['repo_dir'] = profile['repo_dir']
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _attach_check(profile, check):
        profile["verified_at"] = check["checked_at"] if check and check["status"] == "authenticated" else None
        if check is not None:
            profile["connection_check"] = dict(check)
            profile["connection_binding"] = ProfileRepository._stored_binding(profile)

    def _drop_receipts(self, identifier, fingerprint=None):
        self.verification_receipts = {k: v for k, v in self.verification_receipts.items()
                                      if v["id"] != identifier or (identifier is None and v["fingerprint"] != fingerprint)}

    def list(self):
        with self.lock:
            return [self._public(item) for item in self._profiles]

    def _get(self, identifier, expected_revision=None):
        _id(identifier)
        profile = next((item for item in self._profiles if item["id"] == identifier), None)
        if profile is None:
            raise ProfileError("配置不存在，可能已被删除。", "profile_not_found", 404)
        if type(expected_revision) is not int or expected_revision != profile["revision"]:
            raise ProfileError("配置已变更，请刷新后重新选择或编辑。", "profile_revision_conflict", 409)
        return profile

    def _unique_name(self, profile, excluded_id=None):
        if any(item["id"] != excluded_id and item["kind"] == profile["kind"] and item["name"].casefold() == profile["name"].casefold() for item in self._profiles):
            raise ProfileError("同一类型中已存在这个配置名称，请使用其他名称。", "profile_name_conflict", 409)

    def create(self, profile, api_key=None, verification_token=None):
        normalized = _normalize(profile)
        key = _provided_key(api_key)
        identifier = _id(profile["id"]) if "id" in profile else uuid.uuid4().hex
        with self.lock:
            if verification_token is not None:
                self._profiles = self._read()
            if any(item["id"] == identifier for item in self._profiles):
                raise ProfileError("配置 ID 已存在，请刷新配置库。", "profile_id_conflict", 409)
            self._unique_name(normalized)
            if normalized["credential_mode"] == "stored":
                if key is None:
                    raise ProfileError("请填写要加密保存的 API 密钥。", "api_key_required")
                ciphertext = self.protector.protect(key)
            else:
                if key is not None:
                    raise ProfileError("填写 API 密钥时请选择加密保存模式。", "credential_mode_conflict")
                ciphertext = ""
            result = {**normalized, "id": identifier, "revision": 1, "protected_key": ciphertext}
            self._attach_check(result, self._receipt_check(verification_token, normalized, key, profile.get("id"), None))
            updated = [*self._profiles, result]
            self._write(updated)
            self._profiles = updated
            self.verification_receipts.pop(verification_token, None)
            return self._public(result)

    def update(self, identifier, profile, expected_revision, api_key=None, verification_token=None):
        normalized = _normalize(profile)
        key = _provided_key(api_key)
        if "id" in profile and profile["id"] != identifier:
            raise ProfileError("不能修改配置 ID。")
        with self.lock:
            if verification_token is not None:
                self._profiles = self._read()
            previous = self._get(identifier, expected_revision)
            self._unique_name(normalized, identifier)
            if normalized["credential_mode"] == "stored":
                if key is not None:
                    ciphertext = self.protector.protect(key)
                elif previous["credential_mode"] == "stored":
                    if any(normalized[field] != previous[field] for field in ("base_url", "provider_id")):
                        raise ProfileError("接口地址或服务商已变更，请重新输入密钥以确认用于新接口。", "endpoint_key_confirmation_required")
                    ciphertext = previous["protected_key"]
                else:
                    raise ProfileError("请填写要加密保存的 API 密钥。", "api_key_required")
            else:
                if key is not None:
                    raise ProfileError("填写 API 密钥时请选择加密保存模式。", "credential_mode_conflict")
                ciphertext = ""
            if previous["revision"] >= 9007199254740991:
                raise ProfileError("配置修订次数超过支持范围，请创建新配置。")
            result = {**normalized, "id": identifier, "revision": previous["revision"] + 1, "protected_key": ciphertext}
            unchanged = all(normalized[field] == previous[field] for field in normalized) and key is None
            resolved_key = (key or self.protector.unprotect(ciphertext)) if verification_token and ciphertext else key
            previous_public = self._public(previous)
            check = (self._receipt_check(verification_token, normalized, resolved_key, identifier, expected_revision)
                     if verification_token else previous_public.get("connection_check") if unchanged else None)
            self._attach_check(result, check)
            if unchanged and check is None and verification_token is None:
                # Retain readable legacy timestamps; never promote them to a new check.
                result["verified_at"] = previous_public["verified_at"]
            updated = [result if item["id"] == identifier else item for item in self._profiles]
            self._write(updated)
            self._profiles = updated
            self._drop_receipts(identifier)
            return self._public(result)

    @staticmethod
    def _verification_fingerprint(normalized, key, *, resolve_env=True):
        if resolve_env and normalized["credential_mode"] == "env":
            key = os.environ.get(normalized["credential_env"], "")
        return hashlib.sha256((json.dumps(normalized, sort_keys=True) + "\0" + (key or "")).encode()).hexdigest()

    def _receipt_check(self, token, normalized, key, identifier, revision):
        if token is None:
            return None
        receipt = self.verification_receipts.get(token) if isinstance(token, str) else None
        if (not receipt or receipt["expires"] < time.monotonic() or receipt["id"] != identifier
                or receipt["revision"] != revision
                or receipt["fingerprint"] != self._verification_fingerprint(normalized, key)):
            raise ProfileError("验证后配置已改变或验证回执已过期，请重新验证。", "verification_stale", 409)
        return dict(receipt["connection_check"])

    def verify(self, profile, api_key=None, expected_revision=None, probe=None):
        """Verify a draft once, retaining no plaintext key or provider response."""
        if probe is None:
            from .providers import verify_connection
            probe = verify_connection
        normalized, key = _normalize(profile), _provided_key(api_key)
        identifier = profile.get("id")
        if not self.verification_lock.acquire(blocking=False):
            raise ProfileError("已有连接验证正在进行，请等待结果。", "verification_busy", 409)
        try:
            with self.lock:
                self._profiles = self._read()
                previous = self._get(identifier, expected_revision) if expected_revision is not None else None
                if normalized["credential_mode"] == "stored":
                    if key is None:
                        if previous is None or previous["credential_mode"] != "stored":
                            raise ProfileError("请填写 API Key。", "api_key_required")
                        if any(normalized[f] != previous[f] for f in ("provider_id", "base_url")):
                            raise ProfileError("接口改变后需重新输入密钥。", "endpoint_key_confirmation_required")
                        key = self.protector.unprotect(previous["protected_key"])
                elif key is not None:
                    raise ProfileError("密钥方式与填写内容不一致。", "credential_mode_conflict")
                unchanged = previous is not None and api_key in (None, "") and all(normalized[f] == previous[f] for f in normalized)
                if normalized["credential_mode"] == "env":
                    key = os.environ.get(normalized["credential_env"], "")
                fingerprint = self._verification_fingerprint(normalized, key, resolve_env=False)
            try:
                returned = probe({**normalized, **({"_resolved_credential": key}
                                                  if normalized["credential_mode"] in {"stored", "env"} else {})})
            except Exception:
                # An arbitrary probe exception/body is never sent to the client.
                returned = None
            checked_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            check = {**safe_conclusion(returned), "checked_at": checked_at}
            verified_at = checked_at if check["status"] == "authenticated" else None
            with self.lock:
                if previous is not None:
                    self._profiles = self._read()
                    current = self._get(identifier, expected_revision)
                    if (any(current[f] != previous[f] for f in normalized)
                            or current["protected_key"] != previous["protected_key"]):
                        raise ProfileError("验证时配置或凭据已改变，请重新验证。", "verification_stale", 409)
                if fingerprint != self._verification_fingerprint(normalized, key):
                    raise ProfileError("验证时凭据已改变，请重新验证。", "verification_stale", 409)
                token = secrets.token_urlsafe(32)
                self._drop_receipts(identifier, fingerprint)
                self.verification_receipts = {k: v for k, v in self.verification_receipts.items() if v["expires"] > time.monotonic()}
                if len(self.verification_receipts) >= 100:
                    self.verification_receipts.pop(next(iter(self.verification_receipts)))
                self.verification_receipts[token] = {"fingerprint": fingerprint, "id": identifier, "revision": expected_revision,
                                                     "connection_check": check, "expires": time.monotonic() + 1800}
                if unchanged:
                    self._record_verification(identifier, expected_revision, check)
                return {"verification": "verified" if verified_at else "unverified", "verified_at": verified_at,
                        "connection_check": dict(check), "verification_token": token}
        finally:
            self.verification_lock.release()

    def _record_verification(self, identifier, revision, check):
        current = self._get(identifier, revision)
        changed = {k: v for k, v in current.items() if k not in {"verified_at", "connection_check", "connection_binding"}}
        self._attach_check(changed, check)
        updated = [changed if item is current else item for item in self._profiles]
        self._write(updated)
        self._profiles = updated

    def delete(self, identifier, expected_revision):
        with self.lock:
            self._get(identifier, expected_revision)
            updated = [item for item in self._profiles if item["id"] != identifier]
            self._write(updated)
            self._profiles = updated
            self._drop_receipts(identifier)

    def reveal(self, identifier, expected_revision):
        with self.lock:
            profile = self._get(identifier, expected_revision)
            if profile["credential_mode"] != "stored":
                raise ProfileError("此配置没有加密保存的密钥。", "stored_key_required")
            return self.protector.unprotect(profile["protected_key"])

    def resolve_key(self, profile_snapshot):
        if not isinstance(profile_snapshot, dict):
            raise ProfileError("运行配置快照无效。")
        with self.lock:
            profile = self._get(profile_snapshot.get("id"), profile_snapshot.get("revision"))
            if profile["credential_mode"] != "stored":
                raise ProfileError("此配置没有加密保存的密钥。", "stored_key_required")
            def matches(field):
                default = None if field in BRAIN_SETTING_FIELDS else ""
                supplied = profile_snapshot.get(field, default)
                return type(supplied) is type(profile[field]) and supplied == profile[field]
            if not all(matches(field) for field in binding_fields(profile)):
                raise ProfileError("运行配置与保存记录不一致，请重新选择配置。", "profile_snapshot_mismatch", 409)
            return self.protector.unprotect(profile["protected_key"])
