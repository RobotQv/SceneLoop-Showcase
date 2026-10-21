"""Pure Judge scope, configuration identity, outcome and consistency contracts."""
from __future__ import annotations

import copy

JUDGEMENT_VERDICTS = {"pass": True, "continue": False, "rollback": False, "uncertain": False}
JUDGEMENT_SCOPE = {"material": "out_of_scope", "lighting": "out_of_scope",
                   "dimensions": ["geometry", "composition", "missing_objects", "detail",
                                  "preservation", "functional_layout"]}
STRUCTURAL_STAGES = {"structure", "object_detail", "environment_detail"}


def scope_for_stage(stage):
    """Derive the acceptance scope from the workspace Stage.

    Structural stages keep material/lighting out of scope; material/lighting
    stages include their own dimension. Unknown stages keep their text and use
    the conservative structural scope with an explicit note.
    """
    scope = copy.deepcopy(JUDGEMENT_SCOPE)
    if not isinstance(stage, str) or not stage.strip():
        return scope, "workspace Stage 为空，按结构范围验收。"
    normalized = stage.strip()
    if normalized == "appearance":
        scope['material']='in_scope'
        scope['lighting']='in_scope'
    elif normalized == "material":
        scope["material"] = "in_scope"
    elif normalized == "lighting":
        scope["lighting"] = "in_scope"
    elif normalized not in STRUCTURAL_STAGES:
        # Unknown free-text stages keep their text but cannot silently pass
        # material/lighting goals under a structural scope: mark both unknown
        # so a pass verdict cannot be registered for them.
        scope["material"] = "unknown"
        scope["lighting"] = "unknown"
        return scope, f"未知 Stage“{normalized}”：保持原文，材质/灯光范围不确定；结构通过不代表材质灯光目标通过，缺证据时记 unknown。"
    return scope, None


def judge_config_identity(run):
    """Identify the effective Judge configuration without secrets."""
    settings = run.get("judge") or {}
    stored = settings.get("profile") if settings.get("mode") == "profile" else (run.get("profiles") or {}).get("brain") or {}
    return {"mode": settings.get("mode"), "profile_id": stored.get("id"),
            "revision": stored.get("revision"), "reasoning_effort": settings.get("reasoning_effort")}


def judge_outcome(trigger, status, *, judgement=None, input_id=None,
                  application_receipt=None, unknown=False, reason=None,
                  key=None, time=None, committed_rounds=None,
                  accepted_count=None, applied_post=None):
    """Uniform checkpoint outcome shape for every Judge exit path."""
    return {
        "schema_version": 1, "trigger": trigger, "status": status,
        "judgement": judgement, "input_id": input_id,
        "application_receipt": application_receipt,
        "unknown": bool(unknown), "reason": reason,
        "key": key, "time": time,
        "committed_rounds": committed_rounds,
        "accepted_count": accepted_count, "applied_post": applied_post,
    }
JUDGEMENT_TARGET_FIELDS = {"schema_version", "kind", "project_id", "current", "working_version", "before",
                           "before_working_version", "stage", "batch", "goal_digest", "issues_digest",
                           "scope", "evaluation", "reference_digest", "target_digest", "judge_config",
                           "current_files", "before_files"}
JUDGEMENT_BATCH_FIELDS = {"id", "status", "edit_rounds", "last_judged_round"}
JUDGEMENT_KINDS = {"working", "candidate", "open"}


def judgement_pair_is_consistent(verdict, stage_passed):
    """Single verdict/stage_passed contract shared by the model entry and registration."""
    return (verdict in JUDGEMENT_VERDICTS and type(stage_passed) is bool
            and stage_passed is JUDGEMENT_VERDICTS[verdict])
