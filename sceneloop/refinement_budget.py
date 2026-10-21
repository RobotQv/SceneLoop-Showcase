"""Frozen local-round policy and one read-only view of Run/SQL consumption."""
from __future__ import annotations

import copy

from .refinement_round import LIMITS as LEGACY_LIMITS, RoundError, enabled


SCHEMA_VERSION = 2
POLICY_ID = "local-round-budget-v2"
PER_ROUND_ATTEMPTS = 6
# Time fields remain in frozen v1/v2 snapshots for compatibility only.
RESERVE = {"brain_calls": 2, "tool_calls": 2, "active_seconds": 180,
           "judge_calls": 1}


def _dimensions(saved_round_limit, attempts_per_round, judge_max_calls):
    """Validate v2 ceilings from the stored R and D, never from today's reserve."""
    if type(saved_round_limit) is not int or not 1 <= saved_round_limit <= 12:
        raise ValueError("局部轮正式保存目标需要为1–12。")
    if type(attempts_per_round) is not int or not 1 <= attempts_per_round <= 12:
        raise ValueError("每局部轮草稿尝试上限需要为1–12。")
    if type(judge_max_calls) is not int or not 1 <= judge_max_calls <= 72:
        raise ValueError("局部轮Judge调用上限需要为1–72。")
    return {"schema_version": SCHEMA_VERSION, "policy_id": POLICY_ID,
            "saved_round_limit": saved_round_limit,
            "max_edit_attempts_per_round": attempts_per_round,
            "max_edit_attempts": attempts_per_round * saved_round_limit,
            "max_brain_calls": 3 + saved_round_limit * (3 * attempts_per_round + 4),
            "max_tool_calls": 4 + saved_round_limit * (4 * attempts_per_round + 6),
            "max_observations": 2 + saved_round_limit * (2 * attempts_per_round + 4),
            "max_seconds": 900 * saved_round_limit,
            "max_judge_calls": judge_max_calls}


def _stored_reserve(value, dimensions):
    """Accept a frozen reserve that still fits its own snapshot."""
    ceilings = {"brain_calls": dimensions["max_brain_calls"],
                "tool_calls": dimensions["max_tool_calls"],
                "active_seconds": dimensions["max_seconds"],
                "judge_calls": dimensions["max_judge_calls"]}
    if not isinstance(value, dict) or set(value) != set(ceilings):
        raise ValueError("收尾预留字段无效。")
    result = {}
    for key, ceiling in ceilings.items():
        amount = value[key]
        if type(amount) is not int or not 0 <= amount <= ceiling:
            raise ValueError("收尾预留超出已冻结上限。")
        result[key] = amount
    return result


def _snapshot(saved_round_limit, attempts_per_round, judge_max_calls):
    """Freeze today's creation reserve onto a new Run only."""
    dimensions = _dimensions(saved_round_limit, attempts_per_round, judge_max_calls)
    dimensions["completion_reserve"] = _stored_reserve(RESERVE, dimensions)
    return dimensions


def new_snapshot(saved_round_limit, *, judge_max_calls=None):
    """Freeze the current creation default; reading an old Run never calls it."""
    judge = 6 * saved_round_limit if judge_max_calls is None else judge_max_calls
    return _snapshot(saved_round_limit, PER_ROUND_ATTEMPTS, judge)


def policy_preview():
    return {"schema_version": SCHEMA_VERSION, "policy_id": POLICY_ID,
            "presets": {str(rounds): new_snapshot(rounds) for rounds in range(1, 13)},
            "judge_max_calls_upper": 72}


def resolve_snapshot(run):
    """Read v2 exactly; never refill an older Run from today's defaults."""
    if not enabled(run):
        return None
    raw = run.get("local_round_limits")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise RoundError("局部轮预算快照无效。", "round_budget_invalid", 409)
    if type(raw.get("schema_version")) is int and raw.get("schema_version") == SCHEMA_VERSION:
        rounds = raw.get("saved_round_limit")
        judge = raw.get("max_judge_calls")
        try:
            dimensions = _dimensions(rounds, raw.get("max_edit_attempts_per_round"), judge)
            reserve = _stored_reserve(raw.get("completion_reserve"), dimensions)
        except ValueError:
            raise RoundError("局部轮v2预算快照损坏。", "round_budget_invalid", 409) from None
        expected = {**dimensions, "completion_reserve": reserve}
        iteration, settings = run.get("iteration"), run.get("judge")
        if (any(raw.get(key) != value for key, value in expected.items())
                or not isinstance(iteration, dict) or not isinstance(settings, dict)
                or type(iteration.get("max_rounds")) is not int
                or iteration["max_rounds"] != rounds
                or type(settings.get("max_calls")) is not int
                or settings["max_calls"] != judge):
            raise RoundError("局部轮v2预算快照与Run身份不一致。", "round_budget_invalid", 409)
        from .agent_resources import overlay
        result = overlay(_effective_limits(run, raw), run.get('resource_budget') if run.get('agent_resource_base') else None)
        # Each run-scope draft grant is bound to the active round at request
        # time. Future rounds do not each receive that increment again.
        if run.get('agent_resource_base'):
            result['max_edit_attempts_per_round'] += (run.get('resource_round_allowance') or {}).get('extra_attempts', 0)
        return result
    if raw.get("schema_version") is not None and (type(raw.get("schema_version")) is not int or raw.get("schema_version") != 1):
        raise RoundError("未知的局部轮预算快照版本。", "round_budget_invalid", 409)
    # v1 had no policy ID or per-round cap. Preserve every stored ceiling;
    # fill only fields genuinely absent from historical snapshots.
    legacy = {}
    for field in ("max_edit_attempts", "max_brain_calls", "max_tool_calls",
                  "max_observations", "max_seconds"):
        value = raw.get(field, LEGACY_LIMITS[field])
        if type(value) is not int or value < 1:
            raise RoundError("旧局部轮预算快照损坏。", "round_budget_invalid", 409)
        legacy[field] = value
    rounds = run.get("iteration", {}).get("max_rounds")
    judge = run.get("judge", {}).get("max_calls")
    if type(rounds) is not int or type(judge) is not int or rounds < 1 or judge < 1:
        raise RoundError("旧局部轮预算身份损坏。", "round_budget_invalid", 409)
    result = {"schema_version": 1, "policy_id": "local-round-budget-v1-compat",
            "saved_round_limit": rounds,
            "max_edit_attempts_per_round": legacy["max_edit_attempts"],
            **legacy, "max_judge_calls": judge,
            "completion_reserve": {key: 0 for key in RESERVE}}
    if "local_round_overrides" in run or "max_brain_calls" in (run.get("budget") or {}):
        raise RoundError("旧预算不能追加收紧配置。", "round_budget_invalid", 409)
    return result


def _effective_limits(run, raw):
    """Validate the frozen request binding before overlaying a lower hard cap."""
    budget = run.get("budget") or {}
    if not isinstance(budget, dict):
        raise RoundError("Run预算请求身份无效。", "round_budget_invalid", 409)
    override = run.get("local_round_overrides")
    if "max_brain_calls" not in budget and "local_round_overrides" not in run:
        return copy.deepcopy(raw)
    cap = budget.get("max_brain_calls")
    if (type(cap) is not int or not 1 <= cap <= raw["max_brain_calls"]
            or not isinstance(override, dict) or set(override) != {"max_brain_calls"}
            or type(override["max_brain_calls"]) is not int or override["max_brain_calls"] != cap):
        raise RoundError("Builder收紧快照与Run请求身份不一致。", "round_budget_invalid", 409)
    result = copy.deepcopy(raw)
    result["max_brain_calls"] = cap
    return result


def _edit_blocker(remaining, reserve, *, observations=2):
    for resource in ("saved_edit_rounds", "round_edit_attempts", "run_edit_attempts"):
        if remaining[resource] is not None and remaining[resource] <= 0:
            return resource
    if remaining["judge_calls"] < reserve["judge_calls"]:
        return "judge_calls"
    for resource in ("brain_calls", "tool_calls"):
        if remaining[resource] <= reserve[resource]:
            return resource
    if remaining["observations"] < observations:
        return "observations"
    return None


def budget_progress(run, store, *, round_document=None, agent_state=None,
                    active_seconds=None):
    """SQL owns attempt/save counts; persisted Run/agent usage owns other units."""
    limits = resolve_snapshot(run)
    if limits is None:
        return None
    if round_document is None:
        round_document = store.local_round(run["id"])
    current = (round_document if round_document and round_document.get("status") in
               {"active", "paused"} else None)
    round_id = current["id"] if current else None
    run_attempts = store.local_edit_attempt_count(run["id"])
    round_attempts = (store.local_edit_attempt_count(run["id"], round_id=round_id)
                      if round_id else None)
    readiness = store.readiness(run["id"])
    saved = readiness["saved_edit_rounds"]
    since = readiness["rounds_since_judge"]
    state = agent_state or run.get("agent") or (current or {}).get("agent_state") or {}
    agent_used = state.get("used") or {}
    usage = run.get("usage") or {}
    active = max(float(run.get("local_round_active_seconds", 0) or 0),
                 float((current or {}).get("active_seconds_used", 0) or 0))
    if active_seconds is not None:
        active = max(active, float(active_seconds))
    used = {"brain_calls": usage.get("brain_calls", 0),
            "tool_calls": agent_used.get("tool_calls", 0),
            "observations": agent_used.get("observations", 0),
            "judge_calls": usage.get("judge_calls", 0),
            "active_seconds": round(active, 3)}
    remaining = {
        "saved_edit_rounds": (max(0, min(12, limits["saved_round_limit"]) - saved)
                              if saved is not None else 0),
        "round_edit_attempts": (max(0, limits["max_edit_attempts_per_round"] - round_attempts)
                                if round_attempts is not None else None),
        "run_edit_attempts": max(0, limits["max_edit_attempts"] - run_attempts),
        "brain_calls": max(0, limits["max_brain_calls"] - used["brain_calls"]),
        "tool_calls": max(0, limits["max_tool_calls"] - used["tool_calls"]),
        "observations": max(0, limits["max_observations"] - used["observations"]),
        "judge_calls": max(0, limits["max_judge_calls"] - used["judge_calls"]),
        "active_seconds": None}
    retry_count = None
    if current:
        retry_count = 0
        if current.get("steps"):
            operation = store.operation_state(current["steps"][-1]["operation_id"])
            retry_count = operation["edit_retry_count"] if operation else 0
    terminal = (run.get("status") in {"completed", "failed", "cancelled"}
                or run.get("cancel_requested") is True)
    if limits["schema_version"] == SCHEMA_VERSION:
        limiting = ("run_status" if terminal else
                    _edit_blocker(remaining, limits["completion_reserve"]))
    else:
        limiting = next((resource for resource in
                         ("saved_edit_rounds", "round_edit_attempts", "run_edit_attempts",
                         "judge_calls", "brain_calls", "tool_calls", "observations")
                         if remaining[resource] is not None and
                         remaining[resource] <= limits["completion_reserve"].get(resource, 0)),
                        None)
    phase = ("stopped" if terminal or (limiting and current is None) else
             "closing" if limiting else "editing")
    return {"limits": limits, "saved_edit_rounds": saved,
            "rounds_since_judge": since, "round_id": round_id,
            "round_edit_attempts": round_attempts, "run_edit_attempts": run_attempts,
            "edit_retry_count": retry_count, "used": used, "remaining": remaining,
            "phase": phase, "limiting_resource": limiting,
            "time_limit_enforced": False}


def edit_limit(progress, *, observations=2):
    """Return the first v2 admission blocker; closing operations remain legal."""
    if progress["limits"]["schema_version"] != SCHEMA_VERSION:
        return None
    if progress["limiting_resource"] == "run_status":
        return "run_status"
    return _edit_blocker(progress["remaining"],
                         progress["limits"]["completion_reserve"],
                         observations=observations)
