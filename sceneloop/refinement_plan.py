"""Refinement work records, separate from scene commits and independent verdicts."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from .tool_correction import UnexecutedToolRejected

METHOD_VERSION = "refinement-method-v1"
ITEM_FIELDS = {"title", "desired", "basis", "criteria", "object_ids", "method", "next_step",
               "self_check", "status", "issue_ids", "operation_ids", "evaluation_ids"}
STATUSES = {"planned", "active", "blocked", "parked"}
_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")


class RefinementError(Exception):
    def __init__(self, message, code="refinement_invalid", status=400, *, current_version=None):
        super().__init__(message)
        self.code, self.status = code, status
        self.current_version = current_version


class PlanRejected(RefinementError, UnexecutedToolRejected):
    """Known local input/scope rejection, before entering a Plan write."""
    def __init__(self, message, code="refinement_invalid", status=400, *, fields=()):
        super().__init__(message, code, status)
        self.details = {
            "tool_name": "update_refinement_plan",
            "boundary": "plan_prewrite_validation",
            "execution": "confirmed_not_executed",
            "rejected_fields": list(fields),
            "guidance": ("重新读取当前Plan并使用新update_id提交合法增量。委派中的title/desired/basis/criteria/"
                         "object_ids均冻结，包括空object_ids；不要新增全局项或传order。"
                         "在授权工作项内用method/next_step/self_check组织施工，focus只选授权项；"
                         "证据与执行引用必须来自实际回执。不要重复已执行工具。"),
        }


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def discussion_request(prompt):
    """Conservative recognition of explicit discussion; no extra model call.

    An unclassified edit request keeps the existing API's edit intent. A leading
    question/inspection/explanation or explicit plan-only request preserves the
    goal. Ambiguous geometry changes remain subject to the existing clarify tool.
    """
    text = prompt.strip().casefold()
    return bool(re.match(r"^(why\b|how\b|what\b|which\b|explain\b|discuss\b|inspect\b|"
                         r"describe\b|can you explain\b|为什么|为何|怎么|如何|哪里|是否|"
                         r"请解释|解释|讨论|请分析|分析一下|检查一下|请检查|看看|"
                         r"只(?:讨论|分析|检查|调整计划|重排|更新计划)|仅(?:讨论|分析|更新计划))", text)
                or re.match(r"^(?:please )?(?:reorder|update|change) (?:the )?(?:plan|method|next step)\b", text))


def goal_content(run, project, previous=None):
    old = (previous or {}).get("content") or {}
    legacy = (project.get("workspace") or {}).get("goal") or {}
    discussion = run.get("operation") == "edit" and discussion_request(run["prompt"])
    constraints = copy.deepcopy(old.get("constraints") or legacy.get("constraints") or {})
    explicit = run.get("explicit_constraints")
    if explicit is None:  # pre-REFPLAN in-memory callers retain their supplied values
        explicit = run.get("constraints", {})
    constraints.update(copy.deepcopy(explicit))
    for key, default in (("style", ""), ("preserve", ""), ("key_objects_recognizable", True)):
        constraints.setdefault(key, default)
    design = copy.deepcopy(old.get("design_context") or run.get("design_context") or {
        "prompt": legacy.get("scene_intent") or run["prompt"],
        "constraints": copy.deepcopy(legacy.get("constraints") or constraints)})
    request = ((old.get("request") or legacy.get("current_request") or legacy.get("scene_intent") or "")
               if discussion else run["prompt"])
    # A discussion with no recoverable goal preserves unknown history rather
    # than turning a question into an invented scene requirement.
    evaluation = copy.deepcopy(old.get("evaluation") if discussion and old else run.get("evaluation"))
    reference = (copy.deepcopy(old.get("reference")) if discussion and old else
                 {"sha256": (run.get("reference") or {}).get("sha256"),
                  "basis": (run.get("reference") or {}).get("basis")}
                 if (evaluation or {}).get("mode") == "reference" else None)
    return {"request": request, "design_context": design, "constraints": constraints,
            "evaluation": evaluation or {"mode": "intent"}, "reference": reference}, discussion


def validate_update(body):
    if (not isinstance(body, dict) or set(body) - {"update_id", "expected_version", "changes", "focus_item_id", "order"}
            or not isinstance(body.get("update_id"), str) or not _ID.fullmatch(body["update_id"])
            or type(body.get("expected_version")) is not int or body["expected_version"] < 1):
        raise RefinementError("计划更新需要 update_id、expected_version 及允许的增量字段。")
    changes = body.get("changes", [])
    if not isinstance(changes, list) or len(changes) > 32:
        raise RefinementError("每次最多更新32个工作项。")
    seen = set()
    for row in changes:
        if (not isinstance(row, dict) or set(row) - {"item_id", "set", "observations"}
                or not isinstance(row.get("item_id"), str) or not _ID.fullmatch(row["item_id"])
                or row["item_id"] in seen):
            raise RefinementError("工作项身份无效或重复。")
        seen.add(row["item_id"])
        fields = row.get("set", {})
        if not isinstance(fields, dict) or set(fields) - ITEM_FIELDS:
            raise RefinementError("只可写规划内容，不能写 done、saved、judge_passed 或执行事实。")
        for key, value in fields.items():
            if key in {"criteria", "object_ids", "issue_ids", "operation_ids", "evaluation_ids"}:
                if (not isinstance(value, list) or len(value) > 32 or any(
                        not isinstance(x, str) or not x.strip() or len(x) > 2000 for x in value)
                        or len(set(value)) != len(value)):
                    raise RefinementError("标准与引用必须为不重复的有限字符串列表。")
            elif not isinstance(value, str) or len(value) > 4000:
                raise RefinementError("规划文本需要是不超过4000字的字符串。")
        if "status" in fields and fields["status"] not in STATUSES:
            raise RefinementError("工作项状态仅允许 planned/active/blocked/parked。")
        observations = row.get("observations", [])
        if not isinstance(observations, list) or len(observations) > 16:
            raise RefinementError("每次每项最多追加16条观察。")
        for observation in observations:
            if (not isinstance(observation, dict) or set(observation) != {"id", "text", "evidence"}
                    or not isinstance(observation["id"], str) or not _ID.fullmatch(observation["id"])
                    or not isinstance(observation["text"], str) or not observation["text"].strip()
                    or len(observation["text"]) > 4000 or not isinstance(observation["evidence"], list)
                    or not 1 <= len(observation["evidence"]) <= 16):
                raise RefinementError("观察需要稳定id、说明和1–16条实际证据引用。", "refinement_evidence_invalid")
            for evidence in observation["evidence"]:
                if (not isinstance(evidence, dict) or set(evidence) != {"run_id", "revision_id", "artifact_id"}
                        or not isinstance(evidence["run_id"], str) or not isinstance(evidence["artifact_id"], str)
                        or evidence["revision_id"] is not None and not isinstance(evidence["revision_id"], str)):
                    raise RefinementError("证据必须引用实际run/revision/artifact，不接受文件路径或自造事实。", "refinement_evidence_invalid")
    for field in ("order",):
        if field in body and (not isinstance(body[field], list) or any(not isinstance(x, str) for x in body[field])
                              or len(body[field]) > 128 or len(set(body[field])) != len(body[field])):
            raise RefinementError("顺序必须为工作项ID列表。")
    if "focus_item_id" in body and body["focus_item_id"] is not None and not isinstance(body["focus_item_id"], str):
        raise RefinementError("焦点必须为工作项ID或null。")
    return copy.deepcopy(body)


def apply_update(plan, body, observations):
    result = copy.deepcopy(plan)
    indexed = {item["id"]: item for item in result["items"]}
    for change in body.get("changes", []):
        key = change["item_id"]
        if key not in indexed:
            item = {"id": key, "title": "", "desired": "", "basis": "", "criteria": [],
                    "object_ids": [], "method": "", "next_step": "", "self_check": "",
                    "status": "planned", "issue_ids": [], "operation_ids": [], "evaluation_ids": [],
                    "observations": [], "author": "builder"}
            result["items"].append(item)
            indexed[key] = item
            result["order"].append(key)
        item = indexed[key]
        item.update(copy.deepcopy(change.get("set", {})))
        if not item["desired"].strip() or not item["basis"].strip() or not item["criteria"]:
            raise RefinementError("工作项需要期望效果、来源说明和完成标准。")
        for observation in observations.get(key, []):
            prior = next((o for o in item["observations"] if o["id"] == observation["id"]), None)
            if prior is not None:
                if prior != observation:
                    raise RefinementError("同一观察ID不能改写原证据；请追加新观察。", "refinement_evidence_invalid")
            else:
                item["observations"].append(copy.deepcopy(observation))
    if len(result["items"]) > 128 or any(len(i["observations"]) > 128 for i in result["items"]):
        raise RefinementError("计划达到128项或单项128条观察上限；旧历史不会自动删除。")
    if "order" in body:
        if set(body["order"]) != set(indexed):
            raise RefinementError("重排必须保留全部工作项身份。")
        result["order"] = list(body["order"])
    if "focus_item_id" in body:
        result["focus_item_id"] = body["focus_item_id"]
    if result["focus_item_id"] is not None and result["focus_item_id"] not in indexed:
        raise RefinementError("焦点工作项不存在。")
    result["version"] += 1
    return result


def selection_identity(selection):
    return ({"run_id": selection.get("run_id"), "revision_id": selection.get("revision_id")}
            if selection else None)


def project_view(plan, goal, project, *, item_ids=None):
    if plan is None:
        return None
    result = copy.deepcopy(plan)
    if item_ids is not None:
        if (not isinstance(item_ids, list) or any(not isinstance(x, str) for x in item_ids)
                or set(item_ids) - {i["id"] for i in result["items"]}):
            raise RefinementError("读取范围含未知工作项。")
        result["items"] = [i for i in result["items"] if i["id"] in item_ids]
    current = selection_identity(project.get("current_scene"))
    for item in result["items"]:
        for observation in item["observations"]:
            reasons = []
            if observation["goal_id"] != goal["id"]:
                reasons.append("goal_changed")
            if observation["scene"] != current:
                reasons.append("scene_changed")
            observation.update(review_required=bool(reasons), review_reasons=reasons)
        item["self_check_source"] = "builder_statement_not_independent_acceptance"
        item["facts"] = {"saved": False, "judge_passed": False, "scope": "no_item_acceptance_protocol",
                         "operation_receipts": [], "evaluations": []}
    result.update(goal=copy.deepcopy(goal), current_scene=current,
                  working_version=(project.get("workspace") or {}).get("working_version"))
    return result
