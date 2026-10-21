"""One predecessor's bounded public failures; never scene authority or replay instructions."""
import copy
import hashlib
import json
import re

MAX_EXPERIENCES = 8
MAX_BYTES = 16 * 1024
MAX_REASON_BYTES = 1024
TERMINAL = {"completed", "failed", "interrupted", "cancelled"}
FIELDS = {"source", "code", "message", "confirmation", "tool", "call_id", "revision_id"}
SOURCES = {"tool", "builder_self_check", "run_error", "judge"}
CONFIRMATIONS = {"confirmed_rejected", "not_scheduled", "unknown", "feedback_only"}
MEANING = "Historical public feedback only; confirmed commits and unknown attempts are separate. Never replay an unknown operation."
INCOMING_STATES = {"no_previous_run", "previous_handoff_missing", "source_mismatch",
                   "previous_generation_unsealed", "scene_mismatch", "available",
                   "no_own_experience", "previous_handoff_corrupt", "freeze_unavailable"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pack(value):
    text = canonical(value)
    envelope = {"document": text, "digest": _digest(text)}
    if len(canonical(envelope).encode("utf-8")) > MAX_BYTES:
        raise ValueError("handoff exceeds UTF-8 bound")
    return envelope


def _unpack(envelope):
    if not isinstance(envelope, dict) or set(envelope) != {"document", "digest"}:
        raise ValueError("invalid handoff envelope")
    text = envelope["document"]
    if not isinstance(text, str) or len(canonical(envelope).encode("utf-8")) > MAX_BYTES:
        raise ValueError("invalid handoff size")
    if _digest(text) != envelope["digest"]:
        raise ValueError("handoff digest mismatch")
    value = json.loads(text)
    if canonical(value) != text or value.get("schema_version") != 1:
        raise ValueError("invalid handoff serialization")
    return value


def _label(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) else "unknown"


def _reason(value):
    # Match the existing business audit scrub; only a public reason is selected,
    # never raw bodies/diagnostics/native messages, geometry or model reasoning.
    from .builder_context import scrub
    from .response_diagnostics import _VALUE_SECRET, _LONG_BASE64
    if not isinstance(value, str) or not value:
        return "Public reason unavailable."
    if len(value.encode("utf-8")) > MAX_REASON_BYTES:
        return "Public reason omitted: UTF-8 size limit."
    value = scrub(value)
    if _VALUE_SECRET.search(value) or _LONG_BASE64.search(value):
        return "Public reason omitted: sensitive content."
    # A serialized document is evidence, not a public summary. Do not forward it.
    if value.lstrip().startswith(("{", "[")):
        return "Structured details omitted; public reason unavailable."
    return value


def experience(source, code, message, confirmation, **identity):
    row = {"source": source, "code": _label(code), "message": _reason(message),
           "confirmation": confirmation}
    row.update({key: _label(value) for key, value in identity.items() if key in FIELDS and value is not None})
    return row


def record_tool(state, value):
    """Retain only this Run's own failures even after successful reads/decisions."""
    if value.get("receipt_reused"):
        return
    result, tool = value["result"], value["name"]
    row = None
    if result.get("ok") is False:
        error = result.get("error") or {}
        confirmation = ("unknown" if error.get("outcome_unknown") else
                        "not_scheduled" if error.get("code") == "not_executed" else "confirmed_rejected")
        row = experience("tool", error.get("code"), error.get("message"), confirmation,
                         tool=tool, call_id=value.get("id"))
    if row:
        _append(state, row)


def _append(state, row):
    rows = state.setdefault("handoff_experiences", [])
    rows.append(row)
    del rows[:-MAX_EXPERIENCES]
    state["handoff_experience_count"] = state.get("handoff_experience_count", 0) + 1


def record_assessment(state, revision_id, assessment):
    if assessment.get("verdict") != "better" or assessment.get("preservation") != "pass":
        _append(state, experience("builder_self_check", assessment.get("verdict"), assessment.get("reason"),
                                  "unknown" if assessment.get("verdict") == "uncertain" or assessment.get("preservation") == "unknown" else "feedback_only",
                                  revision_id=revision_id))


def record_judge(state, entry):
    judgement = entry.get("judgement") or {}
    if not (entry.get("status") in {"failed", "stale", "unknown"}
            or judgement.get("verdict") in {"continue", "rollback", "uncertain"}):
        return
    token = _digest(canonical({key: entry.get(key) for key in ("input_id", "time", "trigger", "key", "status")}))
    seen = state.setdefault("handoff_judge_seen", [])
    if token in seen:
        return
    seen.append(token)
    del seen[:-MAX_EXPERIENCES]
    # Preserve the public structured reason; model next-focus is labelled feedback.
    message = judgement.get("message") or entry.get("reason")
    if not message and judgement.get("next_focus"):
        message = "; ".join(item for item in judgement["next_focus"] if isinstance(item, str))
    _append(state, experience("judge", entry.get("reason") or judgement.get("verdict") or entry.get("status"),
                              message, "unknown" if entry.get("unknown") or entry.get("status") == "unknown" or judgement.get("verdict") == "uncertain" else "feedback_only"))


def scene_provenance(project):
    current = project.get("current_scene") or {}
    return {"selection": {key: current.get(key) for key in ("run_id", "revision_id", "scene_id")},
            "working_version": project.get("workspace", {}).get("working_version")}


def _valid_experiences(rows):
    if not isinstance(rows, list) or len(rows) > MAX_EXPERIENCES:
        raise ValueError("invalid experiences")
    for row in rows:
        if (not isinstance(row, dict) or set(row) - FIELDS
                or row.get("source") not in SOURCES or row.get("confirmation") not in CONFIRMATIONS
                or any(not isinstance(v, str) for v in row.values())
                or row.get("message") != _reason(row.get("message"))
                or any(_label(v) != v for k, v in row.items() if k not in {"message", "source", "confirmation"})):
            raise ValueError("invalid experience fields")


def _valid_outgoing(doc):
    expected = {"schema_version", "source", "experiences", "omitted_count", "confirmed_commits",
                "meaning", "sealed_at", "generation", "snapshot_digest"}
    if set(doc) != expected or doc["schema_version"] != 1 or doc["meaning"] != MEANING:
        raise ValueError("invalid outgoing fields")
    _valid_experiences(doc["experiences"])
    source = doc["source"]
    if (set(source) != {"project_id", "run_id", "terminal_status", "terminal_updated_at", "scene"}
            or source["terminal_status"] not in TERMINAL
            or any(not isinstance(source[k], str) or not re.fullmatch(r"[0-9a-f]{32}", source[k]) for k in ("project_id", "run_id"))
            or not isinstance(source["terminal_updated_at"], str) or len(source["terminal_updated_at"]) > 64):
        raise ValueError("invalid source")
    scene = source["scene"]
    if set(scene) != {"selection", "working_version"} or set(scene["selection"]) != {"run_id", "revision_id", "scene_id"}:
        raise ValueError("invalid scene provenance")
    selection = scene["selection"]
    for key in ("run_id", "scene_id"):
        if selection[key] is not None and (not isinstance(selection[key], str) or not re.fullmatch(r"[0-9a-f]{32}", selection[key])):
            raise ValueError("invalid scene identifier")
    if selection["revision_id"] is not None and (not isinstance(selection["revision_id"], str) or not re.fullmatch(r"r[0-9]{3,6}", selection["revision_id"])):
        raise ValueError("invalid revision")
    if scene["working_version"] is not None and (type(scene["working_version"]) is not int or scene["working_version"] < 0):
        raise ValueError("invalid working version")
    commits = doc["confirmed_commits"]
    if (set(commits) != {"count", "last_operation_id"} or type(commits["count"]) is not int or commits["count"] < 0
            or (commits["last_operation_id"] is not None and _label(commits["last_operation_id"]) != commits["last_operation_id"])
            or type(doc["omitted_count"]) is not int or doc["omitted_count"] < 0
            or type(doc["generation"]) is not int or doc["generation"] < 1
            or not isinstance(doc["sealed_at"], str) or len(doc["sealed_at"]) > 64):
        raise ValueError("invalid public metadata")
    original = {key: value for key, value in doc.items() if key not in {"sealed_at", "generation", "snapshot_digest"}}
    if _digest(canonical(original)) != doc["snapshot_digest"]:
        raise ValueError("invalid snapshot digest")


def outgoing(run, project, timestamp):
    """Use persisted own public facts, never incoming or the preceding Run's audit."""
    state = run.get("agent") or {}
    rows = copy.deepcopy(state.get("handoff_experiences", []))[-MAX_EXPERIENCES:]
    count = max(len(rows), state.get("handoff_experience_count", len(rows)))
    if not rows:  # Older Run: project only its existing recent public decisions.
        scratch = {}
        for row in state.get("builder_decisions", [])[-MAX_EXPERIENCES:]:
            record_tool(scratch, {"id": row.get("call_id"), "name": row.get("tool", "unknown"),
                                 "result": {"ok": row.get("ok"), "error": row.get("error"),
                                            "result": {"assessment": row.get("builder_assessment")}}})
        rows = scratch.get("handoff_experiences", [])
        count = scratch.get("handoff_experience_count", 0)
    final_state = {"handoff_experiences": rows, "handoff_experience_count": count,
                   "handoff_judge_seen": copy.deepcopy(state.get("handoff_judge_seen", []))}
    # New Runs retain their own observation order. Only append an undelivered final
    # acceptance; never replay older feedback evicted by the bounded collector.
    history = run.get("judge_history", [])
    for entry in (history[-1:] if "handoff_experience_count" in state else history[-MAX_EXPERIENCES:]):
        record_judge(final_state, entry)
    rows = final_state["handoff_experiences"]
    count = final_state["handoff_experience_count"]
    error = run.get("error") or {}
    if error:
        count += 1
        rows.append(experience("run_error", error.get("code"), error.get("message"),
                               "unknown" if error.get("outcome_unknown") else
                               "not_scheduled" if error.get("scheduled") is False else "confirmed_rejected"))
        del rows[:-MAX_EXPERIENCES]
    _valid_experiences(rows)
    receipts = run.get("scene_commit_receipts", [])
    scene = copy.deepcopy((run.get("run_handoff") or {}).get("activation_scene")) or scene_provenance(project)
    if receipts:
        selection = run.get("working_scene") or {}
        scene = {"selection": {"run_id": selection.get("run_id"), "revision_id": selection.get("revision_id"),
                               "scene_id": selection.get("scene_id", run.get("scene_id"))},
                 "working_version": receipts[-1]["working_version"]}
    own = {"schema_version": 1, "source": {"project_id": run["project_id"], "run_id": run["id"],
           "terminal_status": run["status"], "terminal_updated_at": run.get("updated_at"),
           "scene": scene}, "experiences": rows,
           "omitted_count": max(0, count - len(rows)),
           "confirmed_commits": {"count": len(receipts), "last_operation_id": receipts[-1]["operation_id"] if receipts else None},
           "meaning": MEANING}
    # Account for the nested incoming wrapper and JSON string escaping too.
    # Omit whole reasons (keep source/code), rather than truncate structured rows.
    while True:
        trial = dict(own, sealed_at=timestamp, generation=1, snapshot_digest="0" * 64)
        try:
            incoming_size = len(canonical(_pack({"schema_version": 1, "status": "available",
                "activated_at": timestamp, "source_run_id": run["id"], "previous": trial})).encode("utf-8"))
            if incoming_size <= MAX_BYTES - 256:
                break
        except ValueError:
            pass
        longest = max(rows, key=lambda r: len(r["message"].encode("utf-8")), default=None)
        if longest and len(longest["message"].encode("utf-8")) > 100:
            longest["message"] = "Public reason omitted: total UTF-8 size limit."
        elif rows:
            rows.pop(0)
            own["omitted_count"] += 1
        else:
            raise ValueError("invalid provenance size")
    fingerprint = _digest(canonical(own))
    generation = 1
    try:
        prior = _unpack(run.get("run_handoff", {}).get("outgoing"))
        if prior.get("snapshot_digest") == fingerprint:
            return run["run_handoff"]["outgoing"]
        generation = prior["generation"] + 1
    except (ValueError, TypeError, KeyError, AttributeError):
        pass
    own.update(sealed_at=timestamp, generation=generation, snapshot_digest=fingerprint)
    return _pack(own)


def empty_incoming(status, timestamp, source_id=None):
    return _pack({"schema_version": 1, "status": status, "activated_at": timestamp,
                  "source_run_id": source_id, "previous": None})


def predecessor(predecessors):
    def ended_at(run):
        try:
            return _unpack((run.get("run_handoff") or {})["outgoing"])["sealed_at"], run["id"]
        except (ValueError, TypeError, KeyError, AttributeError):
            return run.get("updated_at", run.get("created_at", "")), run["id"]
    return max(predecessors, key=ended_at, default=None)


def freeze(run, project, predecessors, timestamp):
    """Select the nearest ended Run, including missing/empty records; never fall back."""
    previous = predecessor(predecessors)
    if previous is None:
        return empty_incoming("no_previous_run", timestamp)
    source_id = previous["id"]
    holder = previous.get("run_handoff") or {}
    if not isinstance(holder, dict):
        return empty_incoming("previous_handoff_corrupt", timestamp, source_id)
    envelope = holder.get("outgoing")
    if envelope is None:
        return empty_incoming("previous_handoff_missing", timestamp, source_id)
    try:
        doc = _unpack(envelope)
        _valid_outgoing(doc)
        source = doc["source"]
        if source["project_id"] != run["project_id"] or source["run_id"] != source_id or previous.get("project_id") != run["project_id"]:
            return empty_incoming("source_mismatch", timestamp, source_id)
        if (source["terminal_updated_at"] != previous.get("updated_at")
                or source["terminal_status"] != previous["status"]):
            return empty_incoming("previous_generation_unsealed", timestamp, source_id)
        if source["scene"] != scene_provenance(project):
            return empty_incoming("scene_mismatch", timestamp, source_id)
        return _pack({"schema_version": 1, "status": "available" if doc["experiences"] else "no_own_experience",
                      "activated_at": timestamp, "source_run_id": source_id, "previous": doc})
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return empty_incoming("previous_handoff_corrupt", timestamp, source_id)


def incoming_text(run):
    """Always use frozen bytes, including when an old predecessor later resumes."""
    holder = run.get("run_handoff")
    if holder is None:
        return None
    try:
        doc = _unpack(holder["incoming"])
        if (doc.get("status") not in INCOMING_STATES
                or not isinstance(doc.get("activated_at"), str) or len(doc["activated_at"]) > 64
                or set(doc) != {"schema_version", "status", "activated_at", "source_run_id", "previous"}):
            raise ValueError("invalid incoming fields")
        if doc["status"] == "no_previous_run":
            if doc["previous"] is not None or doc["source_run_id"] is not None:
                raise ValueError("invalid empty incoming")
            return None
        if doc["previous"] is not None:
            _valid_outgoing(doc["previous"])
            if doc["previous"]["source"]["project_id"] != run["project_id"] or doc["previous"]["source"]["run_id"] != doc["source_run_id"]:
                raise ValueError("incoming source changed")
            _valid_experiences(doc["previous"]["experiences"])
        text = holder["incoming"]["document"]
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        text = canonical({"schema_version": 1, "status": "frozen_handoff_corrupt", "previous": None})
    return ("Historical experience from one previous Run (not current scene/Plan/issues/Judge facts). "
            "Use only as caution; do not replay unknown operations. The latest dynamic state below is authoritative.\n" + text)
