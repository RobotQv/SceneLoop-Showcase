"""Bounded, redacted diagnostics for provider HTTP and JSON parse failures.

Snapshots are built from the text already in memory. Raw provider bodies are not
written to a temporary file, run.json, or the exception message.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path

from .project_runtime import is_link

PARSE_LAYERS = frozenset({"http_response", "tool_arguments", "scene_plan"})
PARSE_REASONS = frozenset({"syntax", "duplicate_key", "non_finite", "encoding", "depth_or_type"})
PARSE_SOURCES = frozenset({"provider_response", "local_history"})
PARSE_RELATIVE = frozenset({"raw_body", "decoded_body", "tool_arguments", "stripped_plan"})
SNAPSHOT_STATUSES = frozenset({"saved", "not_saved", "failed"})
OMISSION_REASONS = frozenset({
    "encoding", "unclosed_sensitive_value", "sensitive_boundary", "size_limit",
    "run_limit", "write_failed", "redaction_failed", "local_history",
    "read_failed", "body_truncated", "malformed_json", "empty_body", "unknown_shape",
})
_INTEGER_LIMIT = 2 ** 31 - 1
FRAGMENT_LIMIT = 4 * 1024
SNAPSHOT_FILE_LIMIT = 128 * 1024
MAX_SNAPSHOTS_PER_RUN = 8
HTTP_CAPTURE_LIMIT = 16 * 1024
HTTP_ID_HEADERS = ("x-request-id", "x-dashscope-request-id", "request-id")
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MEDIA_TYPE = re.compile(r"[a-z0-9!#$&^_.+-]{1,32}/[a-z0-9!#$&^_.+-]{1,32}\Z")
_MODEL = re.compile(r"[A-Za-z0-9._:@+/-]{1,256}\Z")
_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_SENSITIVE_KEYS = frozenset({
    "apikey", "authorization", "secret", "password", "credential", "credentials",
    "accesstoken", "refreshtoken", "reasoningcontent", "reasoning", "thinking", "prompt", "bearer",
    "messages", "cookie", "setcookie", "token",
})
_SENSITIVE_KEY_PATTERN = re.compile(
    r'(?i)"(?:api[_-]?key|authorization|secret|password|credential|access[_-]?token|refresh[_-]?token|'
    r'reasoning[_-]?content|reasoning|thinking|prompt|bearer|messages|cookie|set[_-]?cookie|token)"\s*:'
)
_VALUE_SECRET = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/-]{4,}|data:(?:image|application)/\S+|https?://\S*[?#@]\S*|"
    r"(?:api[_-]?key|authorization|password|secret|credential|reasoning[_-]?content|access[_-]?token|cookie)"
    r"\s*[:=]\s*(?!\"\"|null\b)\S)"
)
_LONG_BASE64 = re.compile(r"[A-Za-z0-9+/]{100,}={0,2}")
_QUOTED_SENSITIVE_VALUE = re.compile(
    r'(?i)"(?:api[_-]?key|authorization|secret|password|credential|access[_-]?token|'
    r'reasoning[_-]?content|reasoning|thinking|prompt|bearer|messages|cookie|set[_-]?cookie|token)"\s*:\s*"(?:\\.|[^"\\])+"'
)
_ESCAPED_SENSITIVE = re.compile(
    r'(?i)\\"(?:api[_-]?key|authorization|secret|password|credential|access[_-]?token|'
    r'reasoning[_-]?content|reasoning|thinking|prompt|bearer|messages|cookie|set[_-]?cookie|token)\\"\s*:'
)


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _pairs(items):
    result = {}
    for key, item in items:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = item
    return result


def _constant(value):
    raise ValueError("non-finite number")


def _number(value):
    result = float(value)
    if not result == result or result in {float("inf"), float("-inf")}:
        raise ValueError("non-finite number")
    return result


def inspect_json(value):
    """Return (parsed, None) or (None, failure). Failure never includes the input text."""
    if isinstance(value, bytes):
        raw = value
        try:
            text = raw.decode("utf-8")
        except UnicodeError as error:
            position = {"unit": "byte", "offset": error.start, "relative_to": "raw_body"}
            return None, {"reason": "encoding", "position_known": True, "position": position,
                          "byte_length": len(raw), "sha256": _sha256(raw)}
    elif isinstance(value, str):
        text = value
        try:
            raw = text.encode("utf-8")
        except UnicodeError:
            # A lone surrogate has no UTF-8 length or offset. Do not invent one.
            return None, {"reason": "encoding", "position_known": False}
    else:
        return None, {"reason": "depth_or_type", "position_known": False, "byte_length": 0,
                      "sha256": _sha256(b"")}
    meta = {"byte_length": len(raw), "sha256": _sha256(raw)}
    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_number), None
    except json.JSONDecodeError as error:
        return None, {**meta, "reason": "syntax", "position_known": True, "position": {
            "unit": "char", "offset": error.pos, "line": error.lineno, "column": error.colno}}
    except RecursionError:
        return None, {**meta, "reason": "depth_or_type", "position_known": False}
    except ValueError as error:
        if str(error) == "duplicate key":
            reason = "duplicate_key"
        elif str(error) == "non-finite number":
            reason = "non_finite"
        else:
            reason = "syntax"
        return None, {**meta, "reason": reason, "position_known": False}
    except UnicodeError:
        return None, {**meta, "reason": "encoding", "position_known": False}
    except TypeError:
        return None, {**meta, "reason": "depth_or_type", "position_known": False}


def _normalize_key(value):
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _is_sensitive_key(value):
    return _normalize_key(value) in _SENSITIVE_KEYS


def _active_secrets(secrets):
    return tuple(secret for secret in secrets or () if isinstance(secret, str) and secret)


def _string_is_sensitive(value, secrets=()):
    if not isinstance(value, str) or not value:
        return False
    if any(secret in value for secret in secrets):
        return True
    lowered = value.lower()
    if lowered.startswith("data:image/") or lowered.startswith("data:application/") or lowered.startswith("bearer "):
        return True
    if "ivborw0kggo" in lowered or _LONG_BASE64.search(value):
        return True
    if re.search(r"(?i)https?://\S*[?#@]", value):
        return True
    if _needs_redaction(value):
        return True
    return False


def _needs_redaction(text):
    return bool(_SENSITIVE_KEY_PATTERN.search(text) or _ESCAPED_SENSITIVE.search(text) or _residual_secret(text))


def _residual_secret(text):
    return bool(_VALUE_SECRET.search(text) or _QUOTED_SENSITIVE_VALUE.search(text) or _ESCAPED_SENSITIVE.search(text)
                or _LONG_BASE64.search(text) or "ivborw0kggo" in text.lower())


def _read_string(text, index):
    if index >= len(text) or text[index] != '"':
        return None
    cursor = index + 1
    raw = []
    while cursor < len(text):
        character = text[cursor]
        if character == "\\":
            if cursor + 1 >= len(text):
                return None
            raw.append(text[cursor:cursor + 2])
            cursor += 2
            continue
        if character == '"':
            return cursor + 1, "".join(raw)
        raw.append(character)
        cursor += 1
    return None


def _decode_string(raw):
    try:
        return json.loads('"' + raw + '"')
    except (ValueError, UnicodeError, RecursionError):
        return None


def _skip_container(text, index):
    opener = text[index]
    closer = "}" if opener == "{" else "]"
    depth = 0
    cursor = index
    while cursor < len(text):
        character = text[cursor]
        if character == '"':
            parsed = _read_string(text, cursor)
            if parsed is None:
                return None
            cursor = parsed[0]
            continue
        if character == opener:
            depth += 1
        elif character == closer:
            depth -= 1
            if depth == 0:
                return cursor + 1
        cursor += 1
    return None


def _replace_value(text, index):
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    if index >= len(text):
        return None
    character = text[index]
    if character == '"':
        parsed = _read_string(text, index)
        if parsed is None:
            return None
        return '""', parsed[0]
    if character in "{[":
        end = _skip_container(text, index)
        if end is None:
            return None
        return ("{}" if character == "{" else "[]"), end
    if character in "-+0123456789tfnIN.":
        cursor = index
        while cursor < len(text) and text[cursor] not in ",}] \t\r\n":
            cursor += 1
        if cursor == index:
            return None
        return "null", cursor
    return None


def _redact_values(text, secrets=(), edits=None):
    output = []
    index = 0
    length = len(text)
    while index < length:
        if text[index] != '"':
            output.append(text[index])
            index += 1
            continue
        parsed = _read_string(text, index)
        if parsed is None:
            if _needs_redaction(text[index:]):
                return None
            output.append(text[index:])
            break
        end, raw = parsed
        decoded = _decode_string(raw)
        cursor = end
        while cursor < length and text[cursor] in " \t\r\n":
            cursor += 1
        if cursor < length and text[cursor] == ":" and isinstance(decoded, str) and _is_sensitive_key(decoded):
            output.append(text[index:cursor + 1])
            replaced = _replace_value(text, cursor + 1)
            if replaced is None:
                return None
            output.append(replaced[0])
            if edits is not None:
                edits.append((cursor + 1, replaced[1], len(replaced[0])))
            index = replaced[1]
            continue
        original = text[index:end]
        if _string_is_sensitive(decoded, secrets) or (decoded is None and (_residual_secret(raw) or any(secret in raw for secret in secrets))):
            output.append('""')
            if edits is not None:
                edits.append((index, end, 2))
        else:
            output.append(original)
        index = end
    return "".join(output)


def _map_position(position, edits):
    """Compose exact unchanged spans; a replaced character has no precise image."""
    if position is None or position.get("status") != "exact":
        return
    offset, delta = position["offset"], 0
    for start, end, width in edits:
        if start <= offset < end:
            position.update(status="redacted", offset=None)
            return
        if end <= offset:
            delta += width - (end - start)
    position["offset"] = offset + delta


def redact_text(text, secrets=(), *, position=None):
    """Return (fragment or None, omission reason or None). Redaction happens before clipping.

    Decoded JSON field names are scanned even when this request has no credential
    and the raw text does not match a sensitive-key regular expression.
    """
    if not isinstance(text, str):
        return None, "sensitive_boundary"
    secrets = _active_secrets(secrets)
    edits = [] if position is not None else None
    redacted = _redact_values(text, secrets, edits)
    if redacted is None:
        return None, "unclosed_sensitive_value" if _read_string_unclosed(text) else "sensitive_boundary"
    _map_position(position, edits)
    for secret in secrets:
        if secret in redacted:
            _map_position(position, [(match.start(), match.end(), 0)
                                     for match in re.finditer(re.escape(secret), redacted)])
            redacted = redacted.replace(secret, "")
    if any(secret in redacted for secret in secrets) or _residual_secret(redacted):
        return None, "sensitive_boundary"
    return redacted, None


def _read_string_unclosed(text):
    index = 0
    while index < len(text):
        if text[index] == '"':
            parsed = _read_string(text, index)
            if parsed is None:
                return True
            index = parsed[0]
            continue
        index += 1
    return False


def _window(text, offset=None):
    """Return a UTF-8 bounded window and its character start in this exact text."""
    try:
        raw = text.encode("utf-8")
    except UnicodeError:
        return "", True, 0
    if len(raw) <= FRAGMENT_LIMIT:
        return text, False, 0
    start = 0
    if type(offset) is int and 0 <= offset <= len(text):
        position_bytes = len(text[:offset].encode("utf-8"))
        start = max(0, min(position_bytes - FRAGMENT_LIMIT // 2, len(raw) - FRAGMENT_LIMIT))
        while raw[start] & 0xC0 == 0x80:
            start += 1
    # The source is valid UTF-8; ignoring here removes only an incomplete final code point.
    fragment = raw[start:start + FRAGMENT_LIMIT].decode("utf-8", errors="ignore")
    return fragment, True, len(raw[:start].decode("utf-8"))


def _clip(text, position):
    """Compatibility wrapper for HTTP projections and legacy snapshots."""
    offset = position.get("offset") if isinstance(position, dict) and position.get("unit") == "char" else None
    fragment, truncated, _ = _window(text, offset)
    return fragment, truncated


def _stable_window(fragment, offset):
    """Trim only cut edges until repeated filtering preserves the exact substring.

    A window may end between a sensitive key and its already empty replacement.
    It may also start inside a long string. Never bypass the second privacy check,
    synthesize quotes, or move past the error character (or the EOF boundary).
    Work is bounded by two passes over at most FRAGMENT_LIMIT candidate edges.
    """
    def safe(text):
        checked, omitted = redact_text(text)
        return not omitted and checked == text and not _residual_secret(text)

    minimum = (offset + 1 if offset < len(fragment) else offset) if offset is not None else 1
    for end in range(len(fragment), minimum - 1, -1):
        if safe(fragment[:end]):
            return fragment[:end], 0
    # If both edges were cut unsafely, keep the already shortened right edge.
    fragment = fragment[:minimum]
    maximum = min(offset, len(fragment) - 1) if offset is not None else len(fragment) - 1
    for start in range(1, maximum + 1):
        if safe(fragment[start:]):
            return fragment[start:], start
    return fragment, 0  # A final safety rejection still drops the fragment and its coordinates.


def build_snapshot(text, failure, *, layer, relative_to, secrets=()):
    snapshot = {
        "layer": layer,
        "reason": failure["reason"],
        "relative_to": relative_to,
        "truncated": False,
    }
    if failure.get("byte_length") is not None:
        snapshot["byte_length"] = failure["byte_length"]
    if failure.get("sha256"):
        snapshot["sha256"] = failure["sha256"]
    original = failure.get("position") or {}
    offset = original.get("offset") if isinstance(original, dict) else None
    known = (failure.get("position_known") is True and isinstance(text, str) and isinstance(original, dict)
             and original.get("unit") == "char" and type(offset) is int and 0 <= offset <= len(text))
    position = {"status": "exact" if known else "unknown", "offset": offset if known else None}
    if known:
        snapshot["original_char_offset"] = offset
    if failure["reason"] == "encoding" or text is None:
        snapshot["position_mapping"] = "unavailable"
        snapshot["content_omitted_reason"] = "encoding" if failure["reason"] == "encoding" else "sensitive_boundary"
        return snapshot
    redacted, omitted = redact_text(text, secrets, position=position)
    if omitted:
        snapshot["position_mapping"] = "unavailable"
        snapshot["content_omitted_reason"] = omitted
        return snapshot
    fragment, truncated, start = _window(redacted, position["offset"])
    if not fragment and truncated:
        snapshot["content_omitted_reason"] = "encoding"
        snapshot["truncated"] = True
        return snapshot
    local = position["offset"] - start if position["status"] == "exact" else None
    stable, moved = _stable_window(fragment, local)
    truncated = truncated or stable != fragment
    fragment, start = stable, start + moved
    snapshot["fragment"] = fragment
    snapshot["truncated"] = truncated
    snapshot.update(position_mapping=position["status"], fragment_start_char=start,
                    redacted_char_length=len(redacted))
    if position["status"] == "exact":
        snapshot["redacted_char_offset"] = position["offset"]
        snapshot["fragment_error_char"] = position["offset"] - start
    return snapshot


def _bounded_int(value, upper):
    if type(value) is int and 0 <= value <= upper:
        return value
    return None


def _choice(value, allowed):
    return value if isinstance(value, str) and value in allowed else None


def _position(value, relative_to):
    if not isinstance(value, dict):
        return None
    unit = value.get("unit")
    offset = value.get("offset")
    if not isinstance(unit, str) or unit not in {"char", "byte"} or type(offset) is not int or offset < 0 or offset > _INTEGER_LIMIT:
        return None
    result = {"unit": unit, "offset": offset, "relative_to": relative_to}
    for key in ("line", "column"):
        item = value.get(key)
        if type(item) is int and 1 <= item <= _INTEGER_LIMIT:
            result[key] = item
    return result


def _media_type(value):
    if not isinstance(value, str):
        return None
    media = value.split(";", 1)[0].strip().lower()
    if _MEDIA_TYPE.fullmatch(media):
        return media
    return None


def sanitize_response_parse(value):
    """Whitelist one parse summary. Reapplying the result is stable. Bad shapes return None."""
    if not isinstance(value, dict):
        return None
    source = _choice(value.get("source"), PARSE_SOURCES)
    reason = _choice(value.get("reason"), PARSE_REASONS)
    layer = _choice(value.get("layer"), PARSE_LAYERS)
    relative = _choice(value.get("input_relative_to"), PARSE_RELATIVE)
    if source is None or reason is None or layer is None or relative is None:
        return None
    known = value.get("position_known")
    if type(known) is not bool:
        return None
    result = {"source": source, "layer": layer, "reason": reason, "input_relative_to": relative,
              "position_known": known}
    byte_length = value.get("byte_length", None)
    if byte_length is not None:
        byte_length = _bounded_int(byte_length, _INTEGER_LIMIT)
        if byte_length is None:
            return None
        result["byte_length"] = byte_length
    digest = value.get("sha256", None)
    if digest is not None:
        if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
            return None
        result["sha256"] = digest
    if known:
        position = _position(value.get("position"), relative)
        if position is None:
            result["position_known"] = False
        else:
            result["position"] = position
    fence = value.get("fence_stripped")
    if type(fence) is bool:
        result["fence_stripped"] = fence
    status = _bounded_int(value.get("http_status"), 599)
    if status is not None and status >= 100:
        result["http_status"] = status
    media = _media_type(value.get("content_type"))
    if media:
        result["content_type"] = media
    duration = _bounded_int(value.get("duration_ms"), 3_600_000)
    if duration is not None:
        result["duration_ms"] = duration
    tool_index = _bounded_int(value.get("tool_index"), 32)
    if tool_index is not None:
        result["tool_index"] = tool_index
    tool_name = value.get("tool_name")
    if isinstance(tool_name, str) and _TOOL_NAME.fullmatch(tool_name):
        result["tool_name"] = tool_name
    run_id = value.get("run_id")
    if isinstance(run_id, str) and _HEX32.fullmatch(run_id):
        result["run_id"] = run_id
    brain_call = _bounded_int(value.get("brain_call"), 100000)
    if brain_call is not None:
        result["brain_call"] = brain_call
    model = value.get("model")
    if isinstance(model, str) and _MODEL.fullmatch(model):
        result["model"] = model
    revision = _bounded_int(value.get("profile_revision"), _INTEGER_LIMIT)
    if revision is not None:
        result["profile_revision"] = revision
    snapshot_status = _choice(value.get("snapshot_status"), SNAPSHOT_STATUSES)
    if snapshot_status:
        result["snapshot_status"] = snapshot_status
    snapshot_id = value.get("snapshot_id")
    if isinstance(snapshot_id, str) and _HEX32.fullmatch(snapshot_id):
        result["snapshot_id"] = snapshot_id
    for key in ("snapshot_reason", "content_omitted_reason"):
        item = _choice(value.get(key), OMISSION_REASONS)
        if item:
            result[key] = item
    return result


def public_summary(failure, *, layer, source, relative_to, extra=None):
    summary = {
        "source": source,
        "layer": layer,
        "reason": failure["reason"],
        "input_relative_to": relative_to,
        "position_known": bool(failure.get("position_known")),
    }
    if failure.get("byte_length") is not None:
        summary["byte_length"] = failure["byte_length"]
    if failure.get("sha256"):
        summary["sha256"] = failure["sha256"]
    if failure.get("position_known"):
        summary["position"] = {**failure["position"], "relative_to": relative_to}
    if extra:
        summary.update(extra)
    return sanitize_response_parse(summary) or {}


def safe_request_id(value, secrets=()):
    """Only a small identifier alphabet; credentials and sensitive strings never qualify."""
    if (isinstance(value, str) and _REQUEST_ID.fullmatch(value)
            and not _string_is_sensitive(value, _active_secrets(secrets))):
        return value
    return None


def sanitize_provider_http(value):
    """Public HTTP metadata only. The error reason text stays in the local snapshot."""
    if not isinstance(value, dict):
        return None
    status = _bounded_int(value.get("http_status"), 599)
    if status is None or status < 300:
        return None
    result = {"http_status": status}
    for key, allowed in (
            ("read_status", {"complete", "truncated", "failed"}),
            ("capture_scope", {"complete", "prefix", "unavailable"}),
            ("body_format", {"json", "text", "empty", "omitted"}),
            ("endpoint_kind", {"chat_completions", "image_generation", "other"}),
            ("role", {"builder", "planning", "image", "judge"}),
            ("snapshot_status", SNAPSHOT_STATUSES),
            ("snapshot_reason", OMISSION_REASONS),
            ("content_omitted_reason", OMISSION_REASONS)):
        item = _choice(value.get(key), allowed)
        if item is not None:
            result[key] = item
    for key, upper in (("captured_byte_length", HTTP_CAPTURE_LIMIT), ("duration_ms", 3_600_000),
                       ("call_index", 100000), ("profile_revision", _INTEGER_LIMIT)):
        item = _bounded_int(value.get(key), upper)
        if item is not None:
            result[key] = item
    digest = value.get("captured_sha256")
    if isinstance(digest, str) and _HEX64.fullmatch(digest):
        result["captured_sha256"] = digest
    for key, pattern in (("run_id", _HEX32), ("snapshot_id", _HEX32),
                         ("input_id", _HEX32), ("model", _MODEL)):
        item = value.get(key)
        if isinstance(item, str) and pattern.fullmatch(item) and not _string_is_sensitive(item):
            result[key] = item
    provider = _choice(value.get("provider"), {"deepseek", "zai", "dashscope", "openai", "openai_compatible"})
    if provider:
        result["provider"] = provider
    host = value.get("endpoint_host")
    if (isinstance(host, str) and len(host) <= 253 and re.fullmatch(r"[a-z0-9.:-]+", host)
            and not _string_is_sensitive(host)):
        result["endpoint_host"] = host
    media = _media_type(value.get("content_type"))
    if media:
        result["content_type"] = media
    ids = value.get("request_ids")
    if isinstance(ids, dict):
        safe = {source: safe_request_id(ids.get(source)) for source in ("header", "body")}
        safe = {source: item for source, item in safe.items() if item}
        if safe:
            result["request_ids"] = safe
            name = _choice(value.get("request_id_header"), HTTP_ID_HEADERS)
            if "header" in safe and name:
                result["request_id_header"] = name
    shape = value.get("request_shape")
    if isinstance(shape, dict):
        counts = {key: shape[key] for key in (
            "message_count", "image_count", "tool_count", "request_bytes", "assistant_count",
            "assistant_tool_call_count", "assistant_reasoning_field_count", "max_tokens")
            if _bounded_int(shape.get(key), _INTEGER_LIMIT) is not None}
        if type(shape.get("has_assistant_reasoning")) is bool:
            counts["has_assistant_reasoning"] = shape["has_assistant_reasoning"]
        if "max_tokens" in shape and shape["max_tokens"] is None:
            counts["max_tokens"] = None
        result["request_shape"] = counts
    return result


def build_http_snapshot(raw, metadata, *, truncated=False, read_failed=False,
                        header_ids=(), secrets=()):
    """HTTP body capture is not a parse failure; only a safe error projection is retained."""
    secrets = _active_secrets(secrets)
    summary = dict(metadata)
    summary.update(read_status="failed" if read_failed else "truncated" if truncated else "complete",
                   capture_scope="unavailable" if read_failed else "prefix" if truncated else "complete",
                   captured_byte_length=len(raw), captured_sha256=_sha256(raw))
    for name, value in header_ids:
        identifier = safe_request_id(value, secrets)
        if identifier:
            summary["request_ids"] = {"header": identifier}
            summary["request_id_header"] = name
            break
    fragment, omitted = None, None
    if read_failed:
        omitted = "read_failed"
    elif truncated:
        # A prefix may end inside a credential or echoed prompt. Never persist it as text.
        omitted = "body_truncated"
    elif not raw:
        summary["body_format"] = "empty"
        omitted = "empty_body"
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            text, omitted = None, "encoding"
        if text is not None:
            redacted, omitted = redact_text(text, secrets)
            if redacted is not None:
                if text.lstrip().startswith(("{", "[")) or summary.get("content_type") == "application/json":
                    parsed, failure = inspect_json(redacted)
                    if failure is not None:
                        omitted = "malformed_json"
                    elif isinstance(parsed, dict):
                        projection = {}
                        error = parsed.get("error")
                        if isinstance(error, dict):
                            safe = {key: error[key] for key in ("code", "type", "message")
                                    if isinstance(error.get(key), str) and not _string_is_sensitive(error[key], secrets)}
                            if safe:
                                projection["error"] = safe
                        projection.update({key: parsed[key] for key in ("code", "message")
                                           if isinstance(parsed.get(key), str) and not _string_is_sensitive(parsed[key], secrets)})
                        identifier = safe_request_id(parsed.get("request_id"), secrets)
                        if identifier:
                            summary.setdefault("request_ids", {})["body"] = identifier
                            projection["request_id"] = identifier
                        if projection:
                            fragment = json.dumps(projection, ensure_ascii=False)
                            summary["body_format"] = "json"
                        else:
                            omitted = "unknown_shape"
                    else:
                        omitted = "unknown_shape"
                else:
                    # Labels in plain text cannot reliably delimit arbitrary echoed request values.
                    if re.search(r"(?i)\b(messages|prompt|reasoning|reasoning_content|thinking|tool_calls|arguments)\s*[:=]", redacted):
                        omitted = "sensitive_boundary"
                    else:
                        fragment = redacted
                        summary["body_format"] = "text"
    if omitted:
        summary["content_omitted_reason"] = omitted
        summary.setdefault("body_format", "omitted")
    bounded = sanitize_provider_http(summary) or {"http_status": metadata["http_status"]}
    snapshot = {"kind": "provider_http", **bounded, "truncated": truncated}
    if fragment is not None:
        snapshot["fragment"], clipped = _clip(fragment, None)
        snapshot["truncated"] = truncated or clipped
    return bounded, snapshot


def _window_metadata(value, fragment):
    """Private scalar coordinates: original input -> redacted text -> saved fragment."""
    status = _choice(value.get("position_mapping"), {"exact", "redacted", "unknown", "unavailable"})
    if status is None:
        return {}  # Legacy snapshots never implied a fragment-relative position.
    result = {"position_mapping": status}
    original = _bounded_int(value.get("original_char_offset"), _INTEGER_LIMIT)
    if original is not None:
        result["original_char_offset"] = original
    if fragment is None or fragment != value.get("fragment"):
        result["position_mapping"] = "unavailable"
        return result
    start = _bounded_int(value.get("fragment_start_char"), _INTEGER_LIMIT)
    length = _bounded_int(value.get("redacted_char_length"), _INTEGER_LIMIT)
    if start is None or length is None or start + len(fragment) > length:
        result["position_mapping"] = "unknown"
        return result
    result.update(fragment_start_char=start, redacted_char_length=length)
    if status == "exact":
        offset = _bounded_int(value.get("redacted_char_offset"), length)
        local = _bounded_int(value.get("fragment_error_char"), len(fragment))
        if original is not None and offset is not None and local is not None and start + local == offset:
            result.update(redacted_char_offset=offset, fragment_error_char=local)
        else:
            result["position_mapping"] = "unknown"
    return result


def bound_snapshot(value):
    """Keep only an already redacted snapshot. Drop anything that still looks sensitive."""
    if not isinstance(value, dict):
        return None
    if value.get("kind") == "provider_http":
        bounded = sanitize_provider_http(value)
        if bounded is None:
            return None
        result = {"kind": "provider_http", **bounded, "truncated": value.get("truncated") is True}
        fragment = value.get("fragment")
        if isinstance(fragment, str):
            checked, omitted = redact_text(fragment)
            if omitted or checked != fragment or _residual_secret(fragment):
                result["content_omitted_reason"] = omitted or "sensitive_boundary"
            else:
                result["fragment"], clipped = _clip(fragment, None)
                result["truncated"] = result["truncated"] or clipped
        elif "content_omitted_reason" not in result:
            result["content_omitted_reason"] = "sensitive_boundary"
        return result
    layer = _choice(value.get("layer"), PARSE_LAYERS)
    reason = _choice(value.get("reason"), PARSE_REASONS)
    relative = _choice(value.get("relative_to"), PARSE_RELATIVE)
    if layer is None or reason is None or relative is None:
        return None
    result = {"layer": layer, "reason": reason, "relative_to": relative, "truncated": value.get("truncated") is True}
    byte_length = value.get("byte_length", None)
    if byte_length is not None:
        byte_length = _bounded_int(byte_length, _INTEGER_LIMIT)
        if byte_length is None:
            return None
        result["byte_length"] = byte_length
    digest = value.get("sha256", None)
    if digest is not None:
        if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
            return None
        result["sha256"] = digest
    omitted = _choice(value.get("content_omitted_reason"), OMISSION_REASONS)
    fragment = value.get("fragment")
    if isinstance(fragment, str):
        try:
            checked, reason_omitted = redact_text(fragment)
        except Exception:
            checked, reason_omitted = None, "redaction_failed"
        if reason_omitted or checked != fragment or _residual_secret(fragment):
            result["content_omitted_reason"] = reason_omitted or "sensitive_boundary"
        else:
            clipped, truncated = _clip(fragment, None)
            if clipped:
                result["fragment"] = clipped
                result["truncated"] = result["truncated"] or truncated
            else:
                result["content_omitted_reason"] = "encoding"
    elif omitted:
        result["content_omitted_reason"] = omitted
    else:
        result["content_omitted_reason"] = "sensitive_boundary"
    if "fragment" not in result and "content_omitted_reason" not in result:
        result["content_omitted_reason"] = "sensitive_boundary"
    result.update(_window_metadata(value, result.get("fragment")))
    return result


def _encoded_snapshot(snapshot_id, snapshot):
    payload = {"schema_version": 1, "snapshot_id": snapshot_id, **snapshot}
    try:
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return None
    if len(raw) <= SNAPSHOT_FILE_LIMIT:
        return raw
    reduced = {key: value for key, value in payload.items() if key != "fragment"}
    reduced["truncated"] = True
    reduced["content_omitted_reason"] = reduced.get("content_omitted_reason") or "size_limit"
    for key in ("fragment_start_char", "redacted_char_length", "redacted_char_offset", "fragment_error_char"):
        reduced.pop(key, None)
    if "position_mapping" in reduced:
        reduced["position_mapping"] = "unavailable"
    try:
        raw = json.dumps(reduced, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return None
    if len(raw) > SNAPSHOT_FILE_LIMIT:
        return None
    return raw


def write_snapshot(run_directory, snapshot):
    """Write one new file under diagnostics/. Never overwrite or follow a link."""
    bounded = bound_snapshot(snapshot)
    if bounded is None:
        return "failed", None, "redaction_failed"
    original_root = Path(run_directory)
    if is_link(original_root) or is_link(original_root / "diagnostics"):
        return "failed", None, "write_failed"
    root = original_root.resolve()
    directory = (root / "diagnostics").resolve()
    if not directory.is_relative_to(root):
        return "failed", None, "write_failed"
    try:
        if directory.exists() and (is_link(directory) or not directory.is_dir()):
            return "failed", None, "write_failed"
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return "failed", None, "write_failed"
    if is_link(directory) or directory.resolve() != (root / "diagnostics").resolve():
        return "failed", None, "write_failed"
    existing = [path for path in directory.glob("response-*.json")
                if re.fullmatch(r"response-[0-9a-f]{32}\.json", path.name) and path.is_file() and not is_link(path)]
    if len(existing) >= MAX_SNAPSHOTS_PER_RUN:
        return "not_saved", None, "run_limit"
    snapshot_id = uuid.uuid4().hex
    if is_link(directory / f"response-{snapshot_id}.json"):
        return "failed", None, "write_failed"
    path = (directory / f"response-{snapshot_id}.json").resolve()
    if path.parent != directory or is_link(path) or not path.is_relative_to(directory):
        return "failed", None, "write_failed"
    raw = _encoded_snapshot(snapshot_id, bounded)
    if raw is None:
        return "failed", None, "size_limit"
    try:
        with path.open("xb") as stream:
            stream.write(raw)
    except OSError:
        return "failed", None, "write_failed"
    return "saved", snapshot_id, None
