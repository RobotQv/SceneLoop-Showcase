"""Small, synchronous provider adapters. No retries and no provider code execution.

Credentials are resolved only on the server, from the encrypted store or environment.
Provider responses are untrusted data; errors intentionally exclude response bodies, URLs and headers.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from .config_store import ProfileError, normalize_brain_settings, reasoning_capability
from .prompts import REFERENCE_SCOPE_POLICY, SCENE_GEOMETRY_CONTRACT, VISUAL_REVIEW_POLICY
from . import context_usage, response_diagnostics, transport_diagnostics
from .tool_correction import UnexecutedToolRejected

IMAGE_LIMIT = 24 * 1024 * 1024
JSON_LIMIT = 34 * 1024 * 1024
PLAN_LIMIT = 16 * 1024 * 1024
DEFAULT_TIMEOUT = 180
BRAIN_DEFAULT_TIMEOUT = 600
AGENT_REQUEST_LIMIT = 48 * 1024 * 1024
AGENT_TOOL_LIMIT = 8
AGENT_ARGUMENT_LIMIT = 256 * 1024
CHAT_PROVIDERS = frozenset({"deepseek", "zai", "dashscope", "openai", "openai_compatible"})
IMAGE_PROVIDERS = frozenset({"openai", "openai_compatible", "dashscope", "zai"})


AGENT_FINISH_LABELS = frozenset({"stop", "tool_calls", "length", "content_filter",
                                "insufficient_system_resource", "aborted", "missing", "unknown"})
_DIAGNOSTIC_INTEGER_LIMIT = 2 ** 31 - 1
_HOSTNAME_LIMIT = 253
_IMAGE_DOWNLOAD_STAGES = frozenset({"url", "address", "resolve", "transfer", "content"})
_IMAGE_DNS_CATEGORIES = frozenset({"unqueried", "empty", "public", "synthetic", "non_public",
                                   "mixed", "literal", "unavailable"})


def _normalized_hostname(value):
    """Return a canonical host or None. A raw URL, path, or userinfo never qualifies."""
    if not isinstance(value, str) or not value or len(value) > _HOSTNAME_LIMIT:
        return None
    if any(ord(character) < 33 or ord(character) > 126 for character in value):
        return None
    try:
        literal = ipaddress.ip_address(value)
    except ValueError:
        literal = None
    if literal is not None:
        text = str(literal)
        return text if len(text) <= _HOSTNAME_LIMIT else None
    try:
        name = value.encode("idna").decode("ascii").rstrip(".").lower()
    except UnicodeError:
        return None
    if not name or len(name) > _HOSTNAME_LIMIT or ".." in name:
        return None
    for label in name.split("."):
        if (not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                or not all(character.isalnum() or character == "-" for character in label)):
            return None
    return name


def _sanitize_image_download(value):
    """Whitelist one download-failure record. Reapplying the result is a no-op."""
    if not isinstance(value, dict):
        return None
    stage = value.get("stage")
    category = value.get("dns_category")
    attempted = value.get("public_resolve_attempted")
    if (stage not in _IMAGE_DOWNLOAD_STAGES or category not in _IMAGE_DNS_CATEGORIES
            or type(attempted) is not bool):
        return None
    result = {"stage": stage, "dns_category": category, "public_resolve_attempted": attempted}
    hostname = _normalized_hostname(value.get("hostname"))
    if hostname:
        result["hostname"] = hostname
    return result


def sanitize_agent_diagnostics(value):
    """Keep only bounded counts, known labels, and safe image-download fields.

    ProviderError construction and RunManager._fail both apply this function.
    Safe to reapply before persistence. Never accept response bodies, messages,
    reasoning, tool arguments, arbitrary provider strings, identifiers, URLs,
    paths, queries, headers, or extra nested fields.
    """
    if not isinstance(value, dict):
        return {}
    result = {}
    if value.get('response_state') in {'normal', 'length_zero_tools', 'length_tool_calls', 'filtered', 'other_incomplete', 'context_limit'}:
        result['response_state'] = value['response_state']
    if type(value.get('reasoning_field_available')) is bool:
        result['reasoning_field_available'] = value['reasoning_field_available']
    finish = value.get("finish_reason")
    if isinstance(finish, str) and finish in AGENT_FINISH_LABELS:
        result["finish_reason"] = finish
    for key in ("request_max_tokens", "returned_tool_calls"):
        item = value.get(key)
        if type(item) is int and 0 <= item <= _DIAGNOSTIC_INTEGER_LIMIT:
            result[key] = item
    if "request_max_tokens" in value and value["request_max_tokens"] is None:
        result["request_max_tokens"] = None
    usage = value.get("usage")
    if isinstance(usage, dict):
        counts = {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens")
                  if type(usage.get(key)) is int and 0 <= usage[key] <= _DIAGNOSTIC_INTEGER_LIMIT}
        if counts:
            result["usage"] = counts
    image_download = _sanitize_image_download(value.get("image_download"))
    if image_download:
        result["image_download"] = image_download
    response_parse = response_diagnostics.sanitize_response_parse(value.get("response_parse"))
    if response_parse:
        result["response_parse"] = response_parse
    provider_http = response_diagnostics.sanitize_provider_http(value.get("provider_http"))
    if provider_http:
        result["provider_http"] = provider_http
    provider_transport = transport_diagnostics.sanitize(value.get("provider_transport"))
    if provider_transport:
        result["provider_transport"] = provider_transport
    return result


class ProviderError(Exception):
    def __init__(self, code, message, *, outcome_unknown=False, http_status=None, diagnostics=None,
                 response_snapshot=None):
        super().__init__(message)
        self.code = code
        self.outcome_unknown = outcome_unknown
        self.http_status = http_status
        self.diagnostics = {}
        self.response_snapshot = None
        try:
            self.diagnostics = sanitize_agent_diagnostics(diagnostics)
        except Exception:
            self.diagnostics = {}
        try:
            self.response_snapshot = response_diagnostics.bound_snapshot(response_snapshot)
        except Exception:
            self.response_snapshot = None

    def __repr__(self):
        return f"ProviderError({self.code!r})"


class ToolArgumentsRejected(ProviderError, UnexecutedToolRejected):
    """A complete native call with rejected arguments; no tool has run."""


class NativeLengthAdapter:
    """Trusted implementation boundary for an actual opaque wire continuation.

    Never selected by model/profile fields. Generic chat/completions has no
    implementation. An adapter must capture a documented opaque state and add
    its native wire extension without forging assistant or reasoning messages.
    """
    def capture(self, response):
        raise NotImplementedError

    def prepare(self, payload, state):
        raise NotImplementedError


class OutputLengthExhausted(ProviderError):
    """Fully received, validated outer message; exactly zero returned tools."""
    def __init__(self, response, diagnostics, native=None):
        super().__init__('incomplete_agent_step', '本次输出额度耗尽，未形成工具调用；没有执行工具。', diagnostics=diagnostics)
        self.response_sha256 = hashlib.sha256(json.dumps(response, ensure_ascii=False, sort_keys=True,
                                                        separators=(',', ':')).encode()).hexdigest()
        self.native = native  # process-private opaque state; never diagnostics


def _fail(code, message, *, diagnostics=None):
    raise ProviderError(code, message, diagnostics=diagnostics)


def _loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _base(profile):
    value = profile.get("base_url", "")
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 for c in value):
        _fail("invalid_base_url", "API 地址无效。")
    try:
        url = urllib.parse.urlsplit(value)
        port = url.port
    except ValueError:
        _fail("invalid_base_url", "API 地址无效。")
    if (url.scheme not in {"https", "http"} or not url.hostname or url.username
            or url.password or url.query or url.fragment or (port is not None and port < 1)):
        _fail("invalid_base_url", "API 地址应为不含凭据、查询参数的 HTTP(S) 地址。")
    if url.scheme != "https" and not _loopback(url.hostname):
        _fail("insecure_endpoint", "远程模型服务请使用 HTTPS；HTTP 仅支持本机回环地址。")
    return value.rstrip("/")


def _credential(profile):
    name = profile.get("credential_env", "")
    mode = profile.get("credential_mode") or ("env" if name else "none")
    if mode not in {"stored", "env", "none"}:
        _fail("invalid_credential_mode", "请选择有效的密钥保存方式。")
    if mode == "stored":
        # This private field is injected by RunManager after binding the saved
        # profile ID, revision and endpoint. It is never accepted from requests.
        value = profile.get("_resolved_credential")
        if not value:
            _fail("missing_credential", "已保存的密钥尚未解析，请重新选择有效配置。")
    else:
        if not isinstance(name, str) or (name and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name)):
            _fail("invalid_credential_env", "凭据字段必须是环境变量名。")
        if mode == "none" or not name:
            if not _loopback(urllib.parse.urlsplit(_base(profile)).hostname):
                _fail("missing_credential", "远程服务需要保存 API Key 或配置密钥环境变量。")
            return None
        value = os.environ.get(name)
        if not value:
            _fail("missing_credential", "配置引用的密钥环境变量未设置；请设置后重新启动后端，或在配置中填写 API Key。")
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        _fail("invalid_credential", "密钥格式无效，请在配置管理中重新填写。")
    return value


def _image_endpoint(profile):
    base = _base(profile)
    if profile["provider_id"] != "dashscope":
        return base + "/images/generations"
    if "/compatible-mode" in urllib.parse.urlsplit(base).path:
        _fail("unsupported_endpoint", "百炼生图需使用原生 /api/v1 地址，不能使用对话兼容地址。")
    suffix = "/services/aigc/multimodal-generation/generation"
    if base.endswith(suffix):
        return base
    if not base.endswith("/api/v1"):
        base += "/api/v1"
    return base + suffix


def _brain_settings(profile):
    try:
        return normalize_brain_settings(profile, "brain")
    except ProfileError as error:
        _fail("invalid_brain_settings", str(error))


def brain_timeout(profile):
    return _brain_settings(profile)["request_timeout_seconds"] or BRAIN_DEFAULT_TIMEOUT


def _apply_brain_settings(payload, profile):
    settings = _brain_settings(profile)
    maximum = settings["max_output_tokens"]
    if maximum is not None:
        payload["max_tokens"] = maximum
    effort, budget = settings["reasoning_effort"], settings["thinking_budget"]
    if effort is not None:
        payload["reasoning_effort"] = effort
    if budget is not None:
        payload["thinking_budget"] = budget
    if profile["provider_id"] == "dashscope" and (effort is not None or budget is not None):
        payload["enable_thinking"] = effort != "none"


def _length_message(requested_tokens):
    limit = (f"本次配置的输出上限为 {requested_tokens} tokens"
             if requested_tokens is not None else "本次使用供应商默认输出额度")
    return (f"模型响应因长度限制结束（{limit}，同时受模型上下文窗口限制；思考可能计入输出）；"
            "可在配置管理中调整大脑的单次输出上限。未执行本次响应中的工具，也不会自动重试。")


def validate_profile(profile, kind):
    """Offline preflight: structure, adapter support, URL and credential presence.

    Does not claim the remote model/account/key is valid. No HTTP request is sent.
    """
    if kind not in {"brain", "image"}:
        _fail("unsupported_kind", "本轮支持对话与生图服务；3D 服务尚未加入执行链。")
    if not isinstance(profile, dict) or profile.get("kind") != kind:
        _fail("invalid_profile", "请选择与用途一致的模型配置。")
    if profile.get("is_draft"):
        _fail("draft_profile", "请先完善并保存模型配置。")
    allowed = CHAT_PROVIDERS if kind == "brain" else IMAGE_PROVIDERS
    if profile.get("provider_id") not in allowed:
        _fail("unsupported_provider", "当前执行器尚未接入这个服务商；不会猜测其接口。")
    model = profile.get("model")
    if not isinstance(model, str) or not model.strip() or len(model) > 256 or any(ord(c) < 32 for c in model):
        _fail("invalid_model", "模型 ID 无效。")
    _base(profile)
    if kind == "brain":
        _brain_settings(profile)
    if kind == "image":
        if profile["provider_id"] == "dashscope" and not model.startswith("qwen-image"):
            _fail("unsupported_model", "百炼生图适配器仅支持 Qwen-Image 同步生图协议。")
        if profile["provider_id"] == "zai" and model != "glm-image":
            _fail("unsupported_model", "智谱生图适配器目前仅支持 glm-image。")
        _image_endpoint(profile)
    _credential(profile)
    return {"provider_id": profile["provider_id"], "model": model, "kind": kind}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _read_limited(response, limit):
    length = response.headers.get("Content-Length")
    if length:
        try:
            if int(length) > limit:
                _fail("response_too_large", "供应商响应超过本轮允许的大小。")
        except ValueError:
            _fail("invalid_response", "供应商响应长度无效。")
    content = response.read(limit + 1)
    if len(content) > limit:
        _fail("response_too_large", "供应商响应超过本轮允许的大小。")
    return content


def _strict_json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = item
        return result

    def constant(value):
        raise ValueError("non-finite number")

    def number(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("non-finite number")
        return result

    try:
        return json.loads(value, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail("invalid_json", "模型返回了无效 JSON；未执行场景修改。")


_JSON_FAILURE_MESSAGES = {
    "http_response": "模型返回了无效 JSON（HTTP 响应）；未执行场景修改。",
    "tool_arguments": "模型返回了无效 JSON（工具参数）；未执行工具。",
    "scene_plan": "模型返回了无效 JSON（场景计划文本）；未执行场景修改。",
}


def _parse_failure(value, *, layer, relative_to, source="provider_response", extra=None, diagnostics=None,
                   snapshot_text=None, secrets=(), require_object=False):
    message = _JSON_FAILURE_MESSAGES[layer]
    try:
        parsed, failure = response_diagnostics.inspect_json(value)
    except Exception:
        raise ProviderError("invalid_json", message) from None
    if failure is None and require_object and not isinstance(parsed, dict):
        failure = {'reason':'depth_or_type','position_known':False,
                   'byte_length':len(value.encode()),'sha256':hashlib.sha256(value.encode()).hexdigest()}
    if failure is None:
        return parsed
    if isinstance(value, bytes):
        relative_to = "raw_body" if failure.get("reason") == "encoding" else "decoded_body"
    try:
        summary = response_diagnostics.public_summary(
            failure, layer=layer, source=source, relative_to=relative_to, extra=extra)
    except Exception:
        summary = {}
    merged = dict(diagnostics or {})
    if summary:
        merged["response_parse"] = summary
    if source == "local_history":
        error_type = ToolArgumentsRejected if layer == 'tool_arguments' else ProviderError
        raise error_type("invalid_messages", "本地工具参数不是有效 JSON；尚未发送请求。",
                            diagnostics=merged) from None
    text = snapshot_text
    if text is None and failure.get("reason") != "encoding":
        if isinstance(value, str):
            text = value
        elif isinstance(value, bytes):
            try:
                text = value.decode("utf-8")
            except UnicodeError:
                text = None
    try:
        snapshot = response_diagnostics.build_snapshot(
            None if failure.get("reason") == "encoding" else text, failure, layer=layer,
            relative_to=relative_to, secrets=secrets)
    except Exception:
        snapshot = None
    try:
        error_type = ToolArgumentsRejected if layer == 'tool_arguments' else ProviderError
        raise error_type("invalid_json", message, diagnostics=merged, response_snapshot=snapshot) from None
    except ProviderError:
        raise
    except Exception:
        raise ProviderError("invalid_json", message) from None


def _http_request_shape(payload, byte_length):
    """Count the actual outbound structure without retaining any request content."""
    source = payload.get("input") if isinstance(payload.get("input"), dict) else {}
    messages = payload.get("messages", source.get("messages", []))
    if not isinstance(messages, list):
        messages = []
    assistants = [item for item in messages if isinstance(item, dict) and item.get("role") == "assistant"]
    reasoning = sum(any(key in item for key in ("reasoning_content", "reasoning", "thinking"))
                    for item in assistants)
    images = sum(isinstance(part, dict) and (part.get("type") in {"image", "image_url"} or "image" in part)
                 for item in messages if isinstance(item, dict) and isinstance(item.get("content"), list)
                 for part in item["content"])
    return {"message_count": len(messages), "image_count": images,
            "tool_count": len(payload["tools"]) if isinstance(payload.get("tools"), list) else 0,
            "assistant_count": len(assistants), "assistant_reasoning_field_count": reasoning,
            "has_assistant_reasoning": reasoning > 0,
            "assistant_tool_call_count": sum(len(item["tool_calls"]) for item in assistants
                                             if isinstance(item.get("tool_calls"), list)),
            "request_bytes": byte_length, "max_tokens": payload.get("max_tokens")}


def _http_private_values(payload, credential):
    """Transient values from this request only, so a provider echo is also redacted."""
    values = [credential] if credential else []
    if isinstance(payload.get("prompt"), str):
        values.append(payload["prompt"])
    source = payload.get("input") if isinstance(payload.get("input"), dict) else {}
    messages = payload.get("messages", source.get("messages", []))
    if isinstance(messages, list):
        for item in messages:
            if not isinstance(item, dict):
                continue
            for key in ("content", "reasoning_content", "reasoning", "thinking"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    values.append(value)
                elif key == "content" and isinstance(value, list):
                    values.extend(part["text"] for part in value if isinstance(part, dict)
                                  and isinstance(part.get("text"), str) and part["text"])
            for call in item.get("tool_calls", []) if isinstance(item.get("tool_calls"), list) else []:
                function = call.get("function") if isinstance(call, dict) else None
                if isinstance(function, dict) and isinstance(function.get("arguments"), str):
                    values.append(function["arguments"])
    return values


def _capture_http_error(error, profile, endpoint, payload, body, credential, started):
    """Best effort only. _post owns closing the response and preserving the primary error."""
    status = error.code
    metadata = {"http_status": status, "provider": profile.get("provider_id"),
                "model": profile.get("model"), "profile_revision": profile.get("revision"),
                "request_shape": _http_request_shape(payload, len(body))}
    url = urllib.parse.urlsplit(endpoint)
    metadata["endpoint_host"] = _normalized_hostname(url.hostname)
    metadata["endpoint_kind"] = ("chat_completions" if url.path.endswith("/chat/completions") else
                                 "image_generation" if url.path.endswith(("/images/generations", "/image-synthesis")) else "other")
    headers = getattr(error, "headers", None)
    header_ids = []
    if headers:
        try:
            metadata["content_type"] = response_diagnostics._media_type(headers.get("Content-Type"))
            header_ids = [(name, headers.get(name)) for name in response_diagnostics.HTTP_ID_HEADERS]
        except Exception:
            pass
    raw, read_failed = b"", False
    try:
        raw = error.read(response_diagnostics.HTTP_CAPTURE_LIMIT + 1)
        if not isinstance(raw, bytes):
            raw, read_failed = b"", True
    except Exception:
        read_failed = True
    metadata["duration_ms"] = max(0, int((time.perf_counter() - started) * 1000))
    # Config strings and header identifiers receive the same credential screening as the body.
    for key in ("model", "endpoint_host", "content_type"):
        if response_diagnostics._string_is_sensitive(metadata.get(key), (credential,) if credential else ()):
            metadata.pop(key, None)
    return response_diagnostics.build_http_snapshot(
        raw[:response_diagnostics.HTTP_CAPTURE_LIMIT], metadata,
        truncated=len(raw) > response_diagnostics.HTTP_CAPTURE_LIMIT, read_failed=read_failed,
        header_ids=header_ids, secrets=_http_private_values(payload, credential))


def _post(profile, endpoint, payload, *, limit=JSON_LIMIT, timeout=DEFAULT_TIMEOUT, request_limit=None,
          response_capture=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    credential = _credential(profile)
    if credential:
        headers["Authorization"] = "Bearer " + credential
    try:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail("invalid_request", "模型请求不是有效 JSON；尚未发送。")
    if request_limit is not None and len(body) > request_limit:
        _fail("request_too_large", "视觉会话超过 48 MiB；请减少历史图片后继续，尚未发送请求。")
    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    # Explicitly disable redirects so credentials can never follow a new host.
    opener = urllib.request.build_opener(_NoRedirect())
    started = time.perf_counter()
    try:
        context_usage.request_started(profile, endpoint, _http_request_shape(payload, len(body)))
    except Exception:
        pass
    phase = 'request_open'
    try:
        with opener.open(request, timeout=timeout) as response:
            phase = 'response_read'
            raw = _read_limited(response, limit)
            status = getattr(response, "status", None)
            content_type = response.headers.get("Content-Type") if getattr(response, "headers", None) else None
            phase = 'response_close'
    except urllib.error.HTTPError as error:
        status = error.code
        diagnostics, snapshot = {"http_status": status, "content_omitted_reason": "redaction_failed"}, None
        try:
            diagnostics, snapshot = _capture_http_error(error, profile, endpoint, payload, body, credential, started)
            context_usage.http_error(snapshot)
        except Exception:
            pass
        finally:
            try:
                error.close()
            except Exception:
                pass
        detail = {401: "身份验证失败，请检查配置的密钥。", 403: "供应商拒绝访问，请检查模型权限。",
                  404: "接口或模型不存在，请检查 API 地址和模型 ID。", 429: "供应商限流或余额不足，请检查账户。"}.get(status, "模型服务请求失败，请检查供应商控制台。")
        safe_diagnostics = {"provider_http": diagnostics}
        if context_usage.confirmed_context_limit():
            safe_diagnostics['response_state'] = 'context_limit'
        raise ProviderError("provider_http_error", f"{detail}（HTTP {status}）", http_status=status,
                            outcome_unknown=status >= 500, diagnostics=safe_diagnostics,
                            response_snapshot=snapshot) from None
    except (TimeoutError, socket.timeout, urllib.error.URLError, OSError, http.client.HTTPException) as error:
        diagnostics, detail = {}, ''
        try:
            transport = transport_diagnostics.failure(error,phase,time.perf_counter()-started,timeout)
            diagnostics = {'provider_transport':transport}
            detail = transport_diagnostics.summary(transport)
        except Exception:
            pass
        raise ProviderError("request_outcome_unknown", "模型请求未收到完整结果；供应商可能已执行并计费，请检查控制台后决定是否重试。" + detail,
                            outcome_unknown=True,diagnostics=diagnostics) from None
    if response_capture == "http_response":
        duration_ms = max(0, int((time.perf_counter() - started) * 1000))
        extra = {}
        if type(status) is int:
            extra["http_status"] = status
        if isinstance(content_type, str):
            extra["content_type"] = content_type
        extra["duration_ms"] = duration_ms
        result = _parse_failure(raw, layer="http_response", relative_to="decoded_body", extra=extra,
                                secrets=(credential,) if credential else ())
    else:
        result = _strict_json(raw)
    context_usage.response_received(result)
    return result


def _image_type(content):
    if content.startswith(b"\x89PNG\r\n\x1a\n") and content[12:16] == b"IHDR":
        return "image/png", ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp", ".webp"
    _fail("invalid_image", "供应商没有返回支持的 PNG、JPEG 或 WebP 图片。")


def _image_data(path):
    try:
        with Path(path).open("rb") as stream:
            content = stream.read(IMAGE_LIMIT + 1)
    except OSError:
        _fail("image_unavailable", "草图文件无法读取。")
    if len(content) > IMAGE_LIMIT:
        _fail("image_too_large", "草图超过 24 MiB，无法发送给视觉模型。")
    mime, _ = _image_type(content)
    return f"data:{mime};base64," + base64.b64encode(content).decode("ascii")


def image_content(path):
    """Inline actual image bytes. The caller must authorize the artifact path.

    This transient content belongs only in model requests, never persisted logs.
    """
    return {"type": "image_url", "image_url": {"url": _image_data(path)}}


_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
_FAKE_IP6_NETWORK = ipaddress.ip_network("2001:2::/48")
_DNS_RESPONSE_LIMIT = 64 * 1024


def _synthetic_download_address(address):
    """True only for the synthetic ranges that may trigger a separate public lookup."""
    return address in _FAKE_IP_NETWORK or address in _FAKE_IP6_NETWORK


def _system_dns_category(addresses):
    if not addresses:
        return "empty"
    synthetic = [_synthetic_download_address(address) for address in addresses]
    public = [address.is_global for address in addresses]
    if all(public):
        return "public"
    if all(synthetic):
        return "synthetic"
    if any(public) or any(synthetic):
        return "mixed"
    return "non_public"


def _fail_download(code, message, *, hostname, stage, dns_category, attempted):
    image_download = {"stage": stage, "dns_category": dns_category, "public_resolve_attempted": attempted}
    if hostname:
        image_download["hostname"] = hostname
    raise ProviderError(code, message, diagnostics={"image_download": image_download}) from None


def _pinned_https_get(hostname, address, path, *, accept, limit, timeout):
    """No proxy, redirect or credentials; TLS verifies hostname at a checked IP."""
    connection = http.client.HTTPSConnection(hostname, 443, timeout=timeout)
    raw_socket = socket.create_connection((address, 443), timeout=timeout)
    try:
        connection.sock = ssl.create_default_context().wrap_socket(raw_socket, server_hostname=hostname)
    except Exception:
        raw_socket.close()
        raise
    try:
        connection.request("GET", path, headers={"Accept": accept})
        response = connection.getresponse()
        try:
            if response.status != 200:
                _fail("download_failed", "图片下载失败或发生重定向；不会重新执行生图。")
            return _read_limited(response, limit)
        finally:
            response.close()
    finally:
        connection.close()


def _resolve_fake_image_host(hostname, *, timeout):
    """Obtain real IPv4 answers after the caller proved every system answer is synthetic.

    The resolver's IP is fixed so its own DNS cannot be redirected through the
    same mechanism. TLS authenticates Cloudflare, not the numeric IP.
    Only a hostname is disclosed; image paths, signed queries and keys stay local.
    """
    try:
        name = hostname.encode("idna").decode("ascii")
        query = urllib.parse.urlencode({"name": name, "type": "A"})
        raw = _pinned_https_get("cloudflare-dns.com", "1.1.1.1", "/dns-query?" + query,
                                accept="application/dns-json", limit=_DNS_RESPONSE_LIMIT,
                                timeout=min(timeout, 10))
        result = _strict_json(raw)
        if (not isinstance(result, dict) or type(result.get("Status")) is not int
                or result["Status"] != 0 or result.get("TC", False) is not False):
            raise ValueError("DNS status")
        questions = result.get("Question")
        if (not isinstance(questions, list) or len(questions) != 1
                or not isinstance(questions[0], dict) or type(questions[0].get("type")) is not int
                or questions[0]["type"] != 1 or not isinstance(questions[0].get("name"), str)
                or questions[0]["name"].rstrip(".").lower() != name.rstrip(".").lower()):
            raise ValueError("DNS question")
        answers = result.get("Answer")
        if not isinstance(answers, list) or not 1 <= len(answers) <= 128:
            raise ValueError("DNS answers")
        addresses = []
        for answer in answers:
            if (not isinstance(answer, dict) or type(answer.get("type")) is not int
                    or not isinstance(answer.get("data"), str)):
                raise ValueError("DNS record")
            if answer["type"] == 5:  # A normal CNAME chain may precede A records.
                if not 1 <= len(answer["data"]) <= 253:
                    raise ValueError("DNS alias")
                continue
            if answer["type"] != 1:
                raise ValueError("DNS record type")
            address = ipaddress.ip_address(answer["data"])
            if address.version != 4 or not address.is_global:
                raise ValueError("Non-public DNS address")
            addresses.append(str(address))
        if not addresses:
            raise ValueError("No IPv4 answer")
        return addresses
    except (ProviderError, ValueError, OSError, http.client.HTTPException):
        raise ProviderError("download_dns_failed",
                            "图片已生成，但无法安全解析下载地址的真实公网 IP；不会重新执行生图。") from None


def _download_image(url, *, timeout=60):
    """Download an untrusted generated URL without credentials, redirects, or proxies.

    Pin a checked public address and verify the original TLS hostname. A domain
    whose every system answer is inside 198.18.0.0/15 or 2001:2::/48 may use the
    fixed DoH resolver. Mixed, empty, ordinary non-public, and literal synthetic
    IP URLs stay blocked; unwanted answers are never filtered out and contacted.
    """
    hostname = None
    stage = "url"
    dns_category = "unqueried"
    attempted = False
    try:
        if not isinstance(url, str) or len(url) > 16384 or any(ord(character) < 33 for character in url):
            _fail("unsafe_download_url", "生图结果下载地址无效。")
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError:
            _fail("unsafe_download_url", "生图结果下载地址无效。")
        hostname = _normalized_hostname(parsed.hostname)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or parsed.port not in {None, 443}):
            _fail("unsafe_download_url", "生图结果必须使用公开 HTTPS 下载地址。")
        try:
            literal = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            literal = None
        stage = "address"
        if literal is not None and not literal.is_global:
            dns_category = "literal"
            _fail("unsafe_download_url", "生图结果下载地址不能指向本机或内网。")
        try:
            records = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        except OSError:
            dns_category = "unavailable"
            _fail("download_failed", "图片已生成但下载失败；不会自动重新执行生图。")
        addresses = [ipaddress.ip_address(item[4][0]) for item in records]
        dns_category = _system_dns_category(addresses)
        if not addresses or not all(address.is_global for address in addresses):
            if literal is None and addresses and all(_synthetic_download_address(address) for address in addresses):
                attempted = True
                stage = "resolve"
                addresses = _resolve_fake_image_host(parsed.hostname, timeout=timeout)
            else:
                _fail("unsafe_download_url", "生图结果下载地址不能指向本机或内网。")
        stage = "transfer"
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        content = _pinned_https_get(parsed.hostname, str(addresses[0]), path,
                                    accept="image/png,image/jpeg,image/webp", limit=IMAGE_LIMIT, timeout=timeout)
        stage = "content"
        _image_type(content)
        return content
    except ProviderError as error:
        if error.diagnostics.get("image_download"):
            raise
        _fail_download(error.code, str(error), hostname=hostname, stage=stage,
                       dns_category=dns_category, attempted=attempted)
    except (ValueError, OSError, http.client.HTTPException):
        _fail_download("download_failed", "图片已生成但下载失败；不会自动重新执行生图。",
                       hostname=hostname, stage=stage, dns_category=dns_category, attempted=attempted)


def generate_concept(profile, prompt, output_dir):
    validate_profile(profile, "image")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 24000:
        _fail("invalid_prompt", "生图描述不能为空且不超过 24000 字符。")
    if profile["provider_id"] == "dashscope":
        payload = {"model": profile["model"], "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
                   "parameters": {"n": 1, "prompt_extend": False}}
        if profile["model"] in {"qwen-image-3.0", "qwen-image-3.0-pro"}:
            # Their default thinking mode requires prompt extension, which this
            # single-call concept pipeline deliberately disables.
            payload["parameters"]["enable_thinking"] = False
    elif profile["provider_id"] == "zai":
        if len(prompt) > 1000:
            _fail("invalid_prompt", "GLM-Image 生图描述（含项目约束）不能超过 1000 字符；请精简后重试。")
        # GLM-Image returns one URL; n is not part of its documented schema.
        payload = {"model": profile["model"], "prompt": prompt}
    else:
        # Minimal documented payload preserves all model-specific defaults.
        payload = {"model": profile["model"], "prompt": prompt, "n": 1}
    response = _post(profile, _image_endpoint(profile), payload)
    try:
        if profile["provider_id"] == "dashscope":
            parts = response["output"]["choices"][0]["message"]["content"]
            url = next(part["image"] for part in parts if isinstance(part, dict) and "image" in part)
            content = _download_image(url)
        else:
            item = response["data"][0]
            if "b64_json" in item:
                encoded = item["b64_json"]
                if not isinstance(encoded, str) or len(encoded) > (IMAGE_LIMIT + 2) // 3 * 4:
                    _fail("invalid_image", "返回图片为空或超过 24 MiB。")
                content = base64.b64decode(encoded, validate=True)
            else:
                content = _download_image(item["url"])
    except (KeyError, IndexError, TypeError, StopIteration, binascii.Error, ValueError):
        raise ProviderError("invalid_image_response", "生图服务返回格式不符合所选协议；不会自动重试。") from None
    if len(content) > IMAGE_LIMIT:
        _fail("image_too_large", "返回图片超过 24 MiB。")
    mime, extension = _image_type(content)
    digest = hashlib.sha256(content).hexdigest()
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    path = output / ("concept-" + digest[:16] + extension)
    path.write_bytes(content)
    return {"path": str(path.resolve()), "mime_type": mime, "sha256": digest, "size_bytes": len(content),
            "provider_id": profile["provider_id"], "model": profile["model"], "kind": "concept_image"}


SCENE_SYSTEM = ("""You create a constrained Blender ScenePlan JSON object, never code.
Return exactly one JSON object using the following shared contract. No tools or visual
feedback loop are available in this single planning call; do not pretend to have
inspected the resulting Blender scene. Plan recognizable key shapes and clear layout.
""" + REFERENCE_SCOPE_POLICY + "\n" + SCENE_GEOMETRY_CONTRACT + "\n" + VISUAL_REVIEW_POLICY + """
When a requested key object cannot be expressed by fixed templates or a clear simple
assembly, return {"schema_version":1,"blocked_reason":"Describe missing key asset here",
"required_assets":["asset name"]} instead of a misleading plan. Do not silently drop
explicitly requested key objects. State simplifications and uncertainty in assumptions.
""")


def plan_scene(profile, prompt, image_path, constraints):
    validate_profile(profile, "brain")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 16000:
        _fail("invalid_prompt", "场景描述不能为空且不超过 16000 字符。")
    try:
        constraint_text = json.dumps(constraints, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError):
        _fail("invalid_constraints", "项目约束不是有效 JSON。")
    if len(constraint_text) > 16000:
        _fail("invalid_constraints", "项目约束过长。")
    content = [{"type": "text", "text": "User request:\n" + prompt + "\nProject constraints:\n" + constraint_text},
               {"type": "image_url", "image_url": {"url": _image_data(image_path)}}]
    payload = {"model": profile["model"], "messages": [{"role": "system", "content": SCENE_SYSTEM},
               {"role": "user", "content": content}], "stream": False}
    _apply_brain_settings(payload, profile)
    credential = _credential(profile)
    response = _post(profile, _base(profile) + "/chat/completions", payload,
                     limit=PLAN_LIMIT, timeout=brain_timeout(profile), response_capture="http_response")
    try:
        choice = response["choices"][0]
        if choice.get("finish_reason") not in {None, "stop"}:
            diagnostics = _agent_response_diagnostics(response, choice, payload.get("max_tokens"))
            message = (_length_message(payload.get("max_tokens")) if choice.get("finish_reason") == "length"
                       else "模型未完整完成场景计划；未执行场景修改。")
            _fail("incomplete_plan", message, diagnostics=diagnostics)
        result = choice["message"]["content"]
        if not isinstance(result, str):
            _fail("invalid_plan", "模型没有返回文本场景计划。")
    except (KeyError, IndexError, TypeError):
        raise ProviderError("invalid_plan", "对话服务返回格式不符合所选协议。") from None
    result = result.strip()
    fence_stripped = False
    if result.startswith("```json\n") and result.endswith("```"):
        result = result[8:-3].strip()
        fence_stripped = True
    elif result.startswith("```\n") and result.endswith("```"):
        result = result[4:-3].strip()
        fence_stripped = True
    plan = _parse_failure(result, layer="scene_plan", relative_to="stripped_plan",
                          extra={"fence_stripped": fence_stripped},
                          secrets=(credential,) if credential else ())
    if not isinstance(plan, dict):
        _fail("invalid_plan", "场景计划必须是 JSON 对象。")
    if plan.get("blocked_reason"):
        _fail("unsupported_key_asset", "模型判断关键物体超出本轮参数化白模能力，需要接入对应 3D 资产后继续。")
    # The BlenderAdapter applies the authoritative geometric/schema validation.
    return plan


def _agent_tools(tools):
    if not isinstance(tools, list) or not 1 <= len(tools) <= 32:
        _fail("invalid_tools", "工具声明须为 1 至 32 个函数。")
    names = set()
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        parameters = function.get("parameters") if isinstance(function, dict) else None
        if (not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(name, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or name in names
                or not isinstance(parameters, dict) or parameters.get("type") != "object"):
            _fail("invalid_tools", "工具声明无效；尚未发送请求。")
        names.add(name)
    return names


DIRECTOR_ARGUMENT_FEEDBACK = True
DIRECTOR_REPORT_FEEDBACK = True
BUILDER_ARGUMENT_CORRECTION = True
BUILDER_LENGTH_RECOVERY = True

def _agent_calls(calls, *, allowed_names=None, used_ids=None, origin="response", response_context=None, secrets=(), director_arguments=False, syntax_errors=None):
    if calls is None:
        calls = []
    if not isinstance(calls, list) or len(calls) > AGENT_TOOL_LIMIT:
        _fail("invalid_tool_call", "模型单次响应的工具调用数量无效；未执行工具。")
    seen = set(used_ids or ())
    result = []
    for item in calls:
        if not isinstance(item, dict):
            _fail("invalid_tool_call", "模型返回了无效工具调用；未执行工具。")
        identifier = item.get("id")
        function = item.get("function")
        if (item.get("type") != "function" or not isinstance(identifier, str)
                or not 1 <= len(identifier) <= 256 or any(ord(c) < 33 or ord(c) > 126 for c in identifier)
                or identifier in seen or not isinstance(function, dict)):
            _fail("invalid_tool_call", "模型工具调用标识无效或重复；未执行工具。")
        name, arguments = function.get("name"), function.get("arguments")
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
                or (allowed_names is not None and name not in allowed_names)
                or not isinstance(arguments, str) or len(arguments) > AGENT_ARGUMENT_LIMIT):
            _fail("invalid_tool_call", "模型工具名称或参数无效；未执行工具。")
        extra = {"tool_index": len(result)}
        if allowed_names is None or name in allowed_names:
            extra["tool_name"] = name
        if not director_arguments:
            try:
                parsed = _parse_failure(arguments, layer="tool_arguments", relative_to="tool_arguments",
                                        source="local_history" if origin == "history" else "provider_response",
                                        extra=extra, diagnostics=None if origin == "history" else response_context,
                                        secrets=() if origin == "history" else secrets, require_object=True)
            except ToolArgumentsRejected as error:
                if syntax_errors is None:raise
                syntax_errors.append({'id':identifier,'error':error})
                parsed={}
            if not isinstance(parsed, dict):
                _fail("invalid_tool_call", "模型工具参数必须为 JSON 对象；未执行工具。")
        seen.add(identifier)
        result.append({"id": identifier, "type": "function",
                       "function": {"name": name, "arguments": arguments}})
    return result


def _agent_history(messages, *, director_arguments=False, builder_correction=None):
    """Reject orphaned or missing tool results before making a paid request."""
    if not isinstance(messages, list) or not 1 <= len(messages) <= 256:
        _fail("invalid_messages", "视觉会话消息数量无效；尚未发送请求。")
    pending, seen = set(), set()
    for index,message in enumerate(messages):
        if not isinstance(message, dict):
            _fail("invalid_messages", "视觉会话消息无效；尚未发送请求。")
        role, content = message.get("role"), message.get("content")
        if role == "tool":
            identifier = message.get("tool_call_id")
            if not isinstance(identifier, str) or identifier not in pending or not isinstance(content, str):
                _fail("invalid_messages", "工具结果缺少匹配的模型调用；尚未发送请求。")
            pending.remove(identifier)
            continue
        if pending:
            _fail("invalid_messages", "工具结果尚未完整返回；尚未发送请求。")
        if role == "assistant":
            if content is not None and not isinstance(content, str):
                _fail("invalid_messages", "助手消息内容无效；尚未发送请求。")
            reasoning = message.get("reasoning_content")
            if reasoning is not None and not isinstance(reasoning, str):
                _fail("invalid_messages", "助手会话状态无效；尚未发送请求。")
            replies=[]
            for row in messages[index+1:]:
                if not isinstance(row,dict) or row.get('role')!='tool':break
                replies.append(row)
            from .tool_correction import BuilderCorrection
            trusted=(isinstance(builder_correction,BuilderCorrection) and builder_correction.permits_history(message,replies))
            calls = _agent_calls(message.get("tool_calls"), used_ids=seen, origin="history",director_arguments=director_arguments,
                                 syntax_errors=[] if trusted else None)
            pending = {item["id"] for item in calls}
            seen.update(pending)
        elif role == "system":
            if not isinstance(content, str):
                _fail("invalid_messages", "系统消息内容无效；尚未发送请求。")
        elif role == "user":
            if not isinstance(content, (str, list)):
                _fail("invalid_messages", "用户消息内容无效；尚未发送请求。")
        else:
            _fail("invalid_messages", "视觉会话包含无效消息角色；尚未发送请求。")
    if pending:
        _fail("invalid_messages", "工具结果尚未完整返回；尚未发送请求。")
    return seen


def _agent_response_diagnostics(response, choice, requested_tokens):
    raw_finish = choice.get("finish_reason")
    if raw_finish is None:
        finish = "missing"
    elif isinstance(raw_finish, str) and raw_finish in AGENT_FINISH_LABELS:
        finish = raw_finish
    else:
        finish = "unknown"
    result = {"finish_reason": finish, "request_max_tokens": requested_tokens}
    message = choice.get("message")
    if isinstance(message, dict):
        calls = message.get("tool_calls")
        if calls is None:
            result["returned_tool_calls"] = 0
        elif isinstance(calls, list):
            result["returned_tool_calls"] = len(calls)
    usage = response.get("usage")
    if isinstance(usage, dict):
        result["usage"] = {key: usage.get(key) for key in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens")}
        details = usage.get("completion_tokens_details")
        if isinstance(details, dict) and "reasoning_tokens" in details:
            result["usage"]["reasoning_tokens"] = details["reasoning_tokens"]
    return sanitize_agent_diagnostics(result)


_INCOMPLETE_AGENT_MESSAGES = {
    "content_filter": "供应商报告内容过滤，未返回完整的可执行响应；未执行工具，也不会自动重试。",
    "insufficient_system_resource": "供应商报告本次生成资源不足；未执行工具，也不会自动重试。",
    "aborted": "供应商报告本次生成已中止；未执行工具，也不会自动重试。",
    "missing": "供应商未返回结束状态，无法确认本次响应完整；未执行工具，也不会自动重试。",
    "unknown": "供应商返回了未支持的结束状态，无法确认本次响应完整；未执行工具，也不会自动重试。",
}


def agent_step(profile, messages, tools, *, timeout=None, director_arguments=False, builder_correction=None,
               builder_recovery=None, continuation_adapter=None, director_report_only=False):
    """Make exactly one native multimodal tool-call turn; never execute or retry.

    Keep the returned assistant message intact in the transient history, append
    one role:tool message for each tool_call_id, then add user image messages.
    In particular, DeepSeek needs reasoning_content from ALL preceding assistant
    turns whenever tools are present, including turns with no tool calls.
    The caller validates business arguments and owns limits, image provenance,
    execution and persistence; this adapter validates only the wire contract.
    """
    validate_profile(profile, "brain")
    credential = _credential(profile)
    names = _agent_tools(tools)
    if director_report_only:
        if (director_report_only is not True or not director_arguments or builder_correction is not None
                or not names <= {'director_reply', 'director_clarify'}):
            _fail('invalid_messages','只读收尾反馈只能由可信Director调用链开启。')
        # These names are parseable solely so the caller can pair explicit
        # denials. Their schemas remain absent and they are never executable.
        from .director_reply_correction import DENIED_TOOLS
        names = names | DENIED_TOOLS
    if builder_correction is not None:
        from .tool_correction import BuilderCorrection
        if not isinstance(builder_correction,BuilderCorrection) or director_arguments:
            _fail('invalid_messages','Builder纠错能力不属于当前受信执行器。')
    seen = _agent_history(messages,director_arguments=director_arguments,builder_correction=builder_correction)
    if builder_correction is not None:seen.update(builder_correction.seen_ids)
    payload = {"model": profile["model"], "messages": messages, "tools": tools,
               "stream": False}
    _apply_brain_settings(payload, profile)
    if builder_recovery is not None:
        from .builder_recovery import BuilderRecovery
        if not isinstance(builder_recovery, BuilderRecovery) or director_arguments:
            _fail('invalid_messages', 'Builder截断恢复未绑定受信执行器。')
    if continuation_adapter is not None and not isinstance(continuation_adapter, NativeLengthAdapter):
        _fail('invalid_messages', '原生续接必须由可信Provider代码实现，不能由配置声明。')
    if builder_recovery is not None:
        payload = builder_recovery.prepare(payload, continuation_adapter)
    if timeout is None:
        timeout = brain_timeout(profile)
    response = _post(profile, _base(profile) + "/chat/completions", payload,
                     limit=PLAN_LIMIT, timeout=timeout, request_limit=AGENT_REQUEST_LIMIT,
                     response_capture="http_response")
    try:
        choices = response["choices"]
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            _fail("invalid_agent_response", "模型工具会话返回格式无效；未执行工具。")
        choice = choices[0]
        diagnostics = _agent_response_diagnostics(response, choice, payload.get("max_tokens"))
        finish = diagnostics["finish_reason"]
        message = choice["message"]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            _fail("invalid_agent_response", "模型未返回有效助手消息；未执行工具。")
        content, reasoning = message.get("content"), message.get("reasoning_content")
        if (content is not None and not isinstance(content, str)) or (reasoning is not None and not isinstance(reasoning, str)):
            _fail("invalid_agent_response", "模型助手消息内容无效；未执行工具。")
        if finish not in {"stop", "tool_calls"}:
            diagnostics['reasoning_field_available'] = bool(reasoning)
            if finish == 'length':
                raw_calls = message.get('tool_calls')
                if raw_calls is None or raw_calls == []:
                    diagnostics['response_state'] = 'length_zero_tools'
                    native = None
                    if builder_recovery is not None and continuation_adapter is not None:
                        try:
                            state = continuation_adapter.capture(response)
                        except Exception:
                            _fail('builder_native_continuation_unavailable', 'Provider原生截断状态无法核实；未开始续接。', diagnostics=diagnostics)
                        if state is not None:
                            native = (continuation_adapter, state)
                    if builder_recovery is not None:
                        raise OutputLengthExhausted(response, diagnostics, native)
                    _fail('incomplete_agent_step', _length_message(payload.get('max_tokens')), diagnostics=diagnostics)
                # Truncated batches may contain partial JSON/IDs. None may run
                # or be appended as a valid assistant exchange.
                diagnostics['response_state'] = 'length_tool_calls'
            else:
                diagnostics['response_state'] = 'filtered' if finish == 'content_filter' else 'other_incomplete'
            text = (_length_message(payload.get('max_tokens')) if finish == 'length' else _INCOMPLETE_AGENT_MESSAGES[finish])
            _fail('incomplete_agent_step', text, diagnostics=diagnostics)
        failures=[] if builder_correction is not None else None
        calls = _agent_calls(message.get("tool_calls"), allowed_names=names, used_ids=seen,
                             origin="response", response_context=diagnostics,
                             secrets=(credential,) if credential else (),director_arguments=director_arguments,syntax_errors=failures)
        if bool(calls) != (finish == "tool_calls"):
            message = ("供应商结束状态为 stop，但同时返回了工具调用；未执行工具，也不会自动重试。"
                       if finish == "stop" else
                       "供应商结束状态为 tool_calls，但没有返回工具调用；未执行工具，也不会自动重试。")
            _fail("incomplete_agent_step", message, diagnostics=diagnostics)
        result = {"role": "assistant", "content": content, "tool_calls": calls}
        if "reasoning_content" in message:
            result["reasoning_content"] = reasoning
        if builder_correction is not None:builder_correction.capture(result,failures)
        return result
    except (KeyError, IndexError, TypeError):
        raise ProviderError("invalid_agent_response", "对话服务返回格式不符合工具会话协议；未执行工具。") from None


def verify_connection(profile):
    """One bounded read-only check; does not test generation or model quality."""
    from .connection_verify import verify_connection as readonly_check
    return readonly_check(profile)
