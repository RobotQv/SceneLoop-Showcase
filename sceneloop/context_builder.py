"""Bounded cross-run context for a project conversation.

Committed Blender files remain the scene authority. This module only assembles
semantic indexes, recent conversation, issues and summaries for model requests.
"""
from __future__ import annotations

import copy
import json

from .scene_evidence import artifact_refs, build_scene_evidence


def estimate_tokens(value):
    """A conservative dependency-free estimate used only for context budgeting."""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return max(1, (len(value) + 2) // 3)


def compact_scene(plan, inspection, source=None):
    """Cross-run summary using the shared measured/declared/specification boundary."""
    return build_scene_evidence(plan, inspection, source=source)


def build_context(project, run, version, *, token_budget=7000):
    """Build context newest-first without splitting a recorded turn/tool group."""
    workspace = copy.deepcopy(project.get("workspace") or {})
    inspection = json.loads(version["files"]["inspection"].read_text(encoding="utf-8"))
    run_id = version["run"]["id"]
    selection = project.get("current_scene") or {}
    known_revision = "revision_id" in version
    revision_id = version.get("revision_id")
    committed = (known_revision and selection.get("run_id") == run_id
                 and selection.get("revision_id") == revision_id)
    source = {"run_id": run_id, "artifacts": artifact_refs(version["run"], revision_id)}
    if known_revision:
        source["revision_id"] = revision_id
    source["working_version"] = workspace.get("working_version") if committed else None
    source["working_scene"] = True if committed else False
    if committed:
        source["note"] = "当前已登记的 Working Scene 摘要；按需 inspect_scene 获取完整测量与编辑规格。"
    scene = compact_scene(version["plan"], inspection, source)
    context = {
        "workspace": {
            "project_id": project["id"],
            "working_version": workspace.get("working_version", 0),
            "working_scene": copy.deepcopy(project.get("current_scene")),
            "stage": workspace.get("stage", "structure"),
            "authority": {
                "scene": "the committed scene.blend for working_scene",
                "semantic_index": "the paired plan.json; re-inspect when it conflicts with Blender",
            },
            "mesh_access": "Actual mesh vertices/edges/faces and topology are read on demand with "
                           "inspect_geometry(object_id, part_id, representation); this summary never embeds full mesh arrays.",
        },
        "project_goal": {
            "scene_intent": (((run.get("goal_snapshot") or {}).get("content") or {}).get("design_context") or {}).get("prompt")
                            or (workspace.get("goal") or {}).get("scene_intent")
                            or (run.get("design_context") or {}).get("prompt"),
            "latest_constraints": copy.deepcopy(run.get("constraints", {})),
            "goal_snapshot": copy.deepcopy(run.get("goal_snapshot")),
            "current_request": run["prompt"],
            "evaluation": copy.deepcopy(run.get("evaluation", {"mode": "intent"})),
            "recent_changes": copy.deepcopy((workspace.get("goal") or {}).get("history", [])[-8:]),
        },
        "current_scene": scene,
        "issues": copy.deepcopy(workspace.get("issues", [])),
        "rolling_summary": copy.deepcopy(workspace.get("rolling_summary", {})),
        "recent_turns": [],
    }
    used = estimate_tokens({key: value for key, value in context.items() if key != "recent_turns"})
    for turn in reversed(workspace.get("turns", [])):
        cost = estimate_tokens(turn)
        if used + cost > token_budget:
            break
        context["recent_turns"].append(copy.deepcopy(turn))
        used += cost
    context["recent_turns"].reverse()
    context["context_budget"] = {"estimated_tokens": used, "limit": token_budget,
                                 "turns_included": len(context["recent_turns"])}
    return context


def query_history(workspace, *, object_id=None, issue_id=None, version=None, keyword=None, limit=20):
    """First-version structured history lookup; no vector database is required."""
    matches = []
    needle = keyword.casefold() if isinstance(keyword, str) and keyword else None
    for turn in reversed(workspace.get("turns", [])):
        encoded = json.dumps(turn, ensure_ascii=False)
        if object_id and object_id not in turn.get("object_ids", []):
            continue
        if issue_id and issue_id not in turn.get("issue_ids", []):
            continue
        if version is not None and turn.get("working_version") != version:
            continue
        if needle and needle not in encoded.casefold():
            continue
        matches.append(copy.deepcopy(turn))
        if len(matches) >= limit:
            break
    return matches


def refinement_context(plan, run, state, *, budget=None):
    """Do not trim focus, criteria or constraints. Other items have scoped tools."""
    from .refinement_plan import METHOD_VERSION
    method = copy.deepcopy(run.get("refinement_method") or {"status": "legacy_unbound"})
    if plan is None:
        return None
    items = plan["items"]
    focus = next((item for item in items if item["id"] == plan["focus_item_id"]), None)
    # Focus/standards are never truncated. Older observations and other items
    # remain available through scoped reads, with explicit omission counts.
    selected = [copy.deepcopy(focus)] if focus else []
    for item in selected:
        reviews = item["facts"].get("item_reviews", [])
        item["facts"]["older_item_reviews_available"] = len(reviews)
        item["facts"].pop("item_reviews", None)
        item["facts"].pop("evaluations", None)
        rows = item["observations"]
        item["older_observations_available"] = max(0, len(rows) - 8)
        item["observations"] = rows[-8:]
    limits, used = state["limits"], state["used"]
    usage = run["usage"]
    remaining = {"brain_calls": max(0, limits["max_brain_calls"] - usage["brain_calls"]),
                 "tool_calls": max(0, limits["max_tool_calls"] - used["tool_calls"]),
                 "observations": max(0, limits["max_observations"] - used["observations"]),
                 "edit_rounds": max(0, limits["max_rounds"] - used["edit_rounds"]),
                 "seconds": None, "time_limit_enforced": False,
                 "judge_calls": (max(0, (run.get("judge") or {}).get("max_calls", 0) - usage.get("judge_calls", 0))
                                 if (run.get("judge") or {}).get("enabled") else 0),
                 "image_calls": max(0, run["budget"]["max_image_generations"] - usage.get("image_calls", 0)),
                 "asset3d_calls": max(0, run["budget"]["max_asset3d_generations"] - usage.get("asset3d_calls", 0)),
                 "retries": max(0, run["budget"]["max_retries"] - usage.get("retries", 0)),
                 "phase": "before_reserving_this_builder_call"}
    if budget is not None:
        available = budget["remaining"]
        remaining.update(brain_calls=available["brain_calls"],
                         tool_calls=available["tool_calls"],
                         observations=available["observations"],
                         edit_rounds=available["saved_edit_rounds"],
                         edit_attempts=available["run_edit_attempts"],
                         edit_attempts_scope="run",
                         seconds=None,
                         judge_calls=available["judge_calls"],
                         elapsed_active_seconds=budget["used"]["active_seconds"])
    revisions = {state.get("selected_revision"), state.get("pending_revision")} - {None}
    return {"schema_version": 1, "goal": copy.deepcopy(plan["goal"]),
            "plan_id": plan["id"], "plan_version": plan["version"], "method_version": METHOD_VERSION,
            "method_binding": {k: v for k, v in method.items() if k != "content"},
            "focus_item_id": plan["focus_item_id"], "items": copy.deepcopy(selected),
            "item_index": [{"id": i["id"], "title": i["title"][:200], "status": i["status"]} for i in items],
            "order": list(plan["order"]), "current_scene": plan["current_scene"],
            "working_version": plan["working_version"],
            "candidate": {"run_id": run["id"], "selected_revision": state.get("selected_revision"),
                          "pending_revision": state.get("pending_revision"), "kind": "candidate_not_draft_round"},
            "evidence_refs": [{"run_id": run["id"], "revision_id": a.get("revision_id"),
                               "artifact_id": a["id"], "kind": a["kind"]} for a in run["artifacts"]
                              if a.get("revision_id") in revisions and a["kind"] in {"render", "inspection", "geometry_inspection"}],
            "remaining": remaining,
            "next_step": focus.get("next_step") if focus else None,
            "meaning": "Builder planning/self-check; scene receipts and independent evaluations are separate facts"}
