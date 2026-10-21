"""Best-effort local token counts, kept entirely outside assistant messages.

Only the active agent_step observes _post. ContextVars isolate concurrent Runs;
no observer means ordinary provider calls retain their existing behavior.
"""
from contextvars import ContextVar
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import uuid

from . import transport_diagnostics

_ACTIVE = ContextVar("sceneloop_context_usage", default=None)
_INTEGER_LIMIT = 2 ** 31 - 1
_FILE_LIMIT = 4096
_CALL_LIMIT = 10000
_CONTEXT_ERROR = re.compile(
    r"This model's maximum context length is (\d{1,10}) tokens\. "
    r"However, you requested (\d{1,10}) tokens "
    r"\((\d{1,10}) in the messages, (\d{1,10}) in the completion\)\. "
    r"Please reduce the length of the messages or completion\."
    r"(?: \(request_id: [A-Za-z0-9_-]{1,128}\))?")


def _count(value):
    return value if type(value) is int and 0 <= value <= _INTEGER_LIMIT else None


def _notify(method, *args):
    # Observability must never replace a provider success or its original error.
    try:
        observer = _ACTIVE.get()
        if observer is not None:
            getattr(observer, method)(*args)
    except Exception:
        pass


def request_started(profile, endpoint, shape):
    _notify("start", profile, endpoint, shape)


def response_received(response):
    _notify("response", response)


def http_error(snapshot):
    _notify("http_error", snapshot)


def confirmed_context_limit():
    """Only the existing strict arithmetic/provider-error proof may classify it."""
    observer = _ACTIVE.get()
    record = getattr(observer, 'record', None)
    return bool(isinstance(record, dict) and record.get('input_source') == 'provider_error'
                and record.get('context_limit_source') == 'provider_error')


def _plain(path):
    return not (path.is_symlink() or getattr(path, "is_junction", lambda: False)())


class Capture:
    """One actual HTTP attempt, with an unknown pre-request record updated in place."""

    def __init__(self, run_directory, run_id, role, call_index):
        self.directory = Path(run_directory) / "context-usage"
        self.run_id, self.role, self.call_index = run_id, role, call_index
        self.record = None
        self.path = None
        self.previous = None

    def __enter__(self):
        self.token = _ACTIVE.set(self)
        return self

    def __exit__(self, kind, error, traceback):
        try:
            if self.record is not None:
                self.record["state"] = ("outcome_unknown" if getattr(error, "outcome_unknown", False)
                                        else "error" if error is not None else "success")
                self.record["http_status"] = _count(getattr(error, "http_status", None))
                diagnostics = getattr(error,'diagnostics',None)
                if isinstance(diagnostics, dict) and diagnostics.get('response_state') in {
                        'length_zero_tools', 'length_tool_calls', 'filtered', 'other_incomplete', 'context_limit'}:
                    self.record['response_state'] = diagnostics['response_state']
                transport = transport_diagnostics.bind(
                    diagnostics.get('provider_transport') if isinstance(diagnostics,dict) else None,
                    self.run_id,self.role,self.call_index)
                if transport:
                    self.record['provider_transport'] = transport
                self.record["finished_at_ms"] = int(time.time() * 1000)
                self._save()
        except Exception:
            pass
        finally:
            _ACTIVE.reset(self.token)

    def start(self, profile, endpoint, shape):
        if (self.record is not None or self.role not in {"builder", "judge", "director"}
                or not isinstance(self.run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", self.run_id)
                or type(self.call_index) is not int or not 1 <= self.call_index <= _CALL_LIMIT):
            return
        identity = json.dumps([profile.get("provider_id"), profile.get("model"), endpoint],
                              ensure_ascii=True).encode("utf-8")
        url = urlsplit(endpoint)
        known_limit = (url.scheme == "https" and url.hostname == "api.deepseek.com"
                       and url.port in {None, 443} and not url.username and not url.password
                       and not url.query and not url.fragment
                       and url.path in {"/chat/completions", "/v1/chat/completions"}
                       and profile.get("model") == "deepseek-flash")
        self.record = {
            "schema_version": 1, "run_id": self.run_id, "role": self.role,
            "call_index": self.call_index,
            "model_endpoint_id": hashlib.sha256(identity).hexdigest(),
            "started_at_ms": int(time.time() * 1000), "finished_at_ms": None,
            "state": "pending", "http_status": None,
            "input_tokens": None, "input_source": "unknown",
            "completion_tokens": None, "cache_hit_tokens": None, "cache_miss_tokens": None,
            "max_output_tokens": _count(shape.get("max_tokens")),
            "max_output_source": "request.max_tokens" if shape.get("max_tokens") is not None else "provider_default_unknown",
            "input_plus_max_output": None,
            "context_limit": 1048576 if known_limit else None,
            "context_limit_source": "observed_provider_limit:api.deepseek.com/deepseek-flash" if known_limit else "unknown",
            "message_count": _count(shape.get("message_count")),
            "image_count": _count(shape.get("image_count")),
            "input_delta": None, "input_delta_reason": "no_previous_call",
            "previous_call_index": None, "previous_input_source": None,
        }
        self._prepare()
        self._save()

    def _prepare(self):
        if not _plain(self.directory.parent) or not _plain(self.directory):
            return
        self.directory.mkdir(exist_ok=True)
        if self.directory.resolve().parent != self.directory.parent.resolve():
            return
        candidate = self.directory / f"{self.role}-{self.call_index:05d}.json"
        # Never overwrite an existing attempt, including one left pending by a crash.
        if candidate.exists() or not _plain(candidate):
            return
        for index in range(self.call_index - 1, 0, -1):
            path = self.directory / f"{self.role}-{index:05d}.json"
            if not path.exists() or not _plain(path) or path.stat().st_size > _FILE_LIMIT:
                self.record["input_delta_reason"] = "previous_record_unavailable"
                break
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                self.record["input_delta_reason"] = "previous_record_unavailable"
                break
            if (not isinstance(previous, dict) or previous.get("schema_version") != 1
                    or previous.get("run_id") != self.run_id or previous.get("role") != self.role
                    or previous.get("call_index") != index
                    or not isinstance(previous.get("model_endpoint_id"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", previous["model_endpoint_id"])):
                self.record["input_delta_reason"] = "previous_record_unavailable"
                break
            if previous["model_endpoint_id"] == self.record["model_endpoint_id"]:
                self.previous = previous
                break
        # Exclusive creation also prevents duplicate logging of an attempt.
        with candidate.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(self.record, ensure_ascii=True))
        self.path = candidate

    def _save(self):
        if self.path is None:
            return
        previous = self.previous
        current = self.record["input_tokens"]
        if previous is not None:
            previous_input = _count(previous.get("input_tokens"))
            source = previous.get("input_source")
            if source not in {"response.usage.prompt_tokens", "provider_error"}:
                source, previous_input = "unknown", None
            self.record.update(previous_call_index=previous["call_index"], previous_input_source=source)
            self.record["input_delta_reason"] = ("current_input_unknown" if current is None else
                                                  "previous_input_unknown" if previous_input is None else "same_model_endpoint")
            self.record["input_delta"] = current - previous_input if current is not None and previous_input is not None else None
        maximum = self.record["max_output_tokens"]
        self.record["input_plus_max_output"] = current + maximum if current is not None and maximum is not None else None
        raw = json.dumps(self.record, ensure_ascii=True)
        if len(raw.encode("utf-8")) > _FILE_LIMIT or not _plain(self.directory) or not _plain(self.path):
            return
        temporary = self.directory / (uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                stream.write(raw)
            temporary.replace(self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def response(self, response):
        if self.record is None or not isinstance(response, dict):
            return
        usage = response.get("usage")
        value = _count(usage.get("prompt_tokens")) if isinstance(usage, dict) else None
        if value is not None:
            # prompt_tokens already includes cached input; do not add cache counters.
            self.record.update(input_tokens=value, input_source="response.usage.prompt_tokens")
        if isinstance(usage,dict):
            self.record['completion_tokens'] = _count(usage.get('completion_tokens'))
            self.record['cache_hit_tokens'] = _count(usage.get('prompt_cache_hit_tokens'))
            self.record['cache_miss_tokens'] = _count(usage.get('prompt_cache_miss_tokens'))

    def http_error(self, snapshot):
        if (self.record is None or self.record["context_limit"] is None
                or not isinstance(snapshot, dict) or snapshot.get("http_status") != 400
                or snapshot.get("truncated") or not isinstance(snapshot.get("fragment"), str)):
            return
        if len(snapshot["fragment"].encode("utf-8")) > _FILE_LIMIT:
            return
        value = json.loads(snapshot["fragment"])
        error = value.get("error") if isinstance(value, dict) else None
        if (not isinstance(error, dict) or error.get("code") != "invalid_request_error"
                or error.get("type") != "invalid_request_error" or not isinstance(error.get("message"), str)):
            return
        match = _CONTEXT_ERROR.fullmatch(error["message"])
        if match is None:
            return
        limit, total, input_tokens, maximum = map(int, match.groups())
        if (all(_count(number) is not None for number in (limit, total, input_tokens, maximum))
                and limit == self.record["context_limit"] and total > limit
                and input_tokens + maximum == total and maximum == self.record["max_output_tokens"]):
            self.record.update(input_tokens=input_tokens, input_source="provider_error",
                               context_limit_source="provider_error")
