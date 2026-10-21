"""Independent, read-only stage judge for committed working scenes."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import uuid

from .judge_contract import JUDGEMENT_SCOPE, JUDGEMENT_VERDICTS, judgement_pair_is_consistent
from .judge_contract import judge_config_identity, scope_for_stage
from .project_runtime import file_sha256
from .providers import brain_timeout
from .scene_evidence import artifact_refs, build_scene_evidence, source_identity
from .geometry_inspection_contract import compact_json, evidence_id as geometry_evidence_id
from .geometry_inspection_contract import (MAX_GEOMETRY_REQUESTS, finalize_document,
                                           validate_judge_request)
from .observation_contract import validate_observation_metadata, comparison_identity
from .evaluation_input import EvaluationInputBuilder, EvaluationInputError
from .refinement_evaluation import OPTIONAL_FIELDS, standards, validate_reviews, review_schema
from .refinement_round import enabled as local_round_enabled


ISSUE_CATEGORIES = ["geometry", "composition", "missing_objects", "detail", "preservation",
                     "functional_layout", "material", "lighting"]
ISSUE_SEVERITIES = ["low", "medium", "high", "critical"]
ISSUE_PRIORITIES = ["low", "normal", "high"]
JUDGEMENT_FIELDS = {"verdict", "stage_passed", "issues", "next_focus", "unknowns", "confidence"}
ISSUE_FIELDS = {"id", "category", "object_id", "severity", "priority", "description", "evidence"}


JUDGE_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_judgement",
        "description": "Submit the independent stage judgement. This tool never edits or restores a scene.",
        "parameters": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["pass", "continue", "rollback", "uncertain"]},
                "stage_passed": {"type": "boolean",
                                 "description": "Must be true only for verdict=pass and false for continue, rollback or uncertain."},
                "issues": {"type": "array", "maxItems": 64, "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,120}$"},
                        "category": {"type": "string", "enum": ISSUE_CATEGORIES},
                        "object_id": {"type": "string", "maxLength": 200,
                                      "description": "Omit when the object is unknown."},
                        "severity": {"type": "string", "enum": ISSUE_SEVERITIES},
                        "priority": {"type": "string", "enum": ISSUE_PRIORITIES},
                        "description": {"type": "string", "maxLength": 2000},
                        "evidence": {"type": "array", "maxItems": 32,
                                     "items": {"type": "string"}},
                    },
                    "required": ["category", "severity", "priority", "description", "evidence"],
                    "additionalProperties": False,
                }},
                "next_focus": {"type": "array", "maxItems": 32,
                               "items": {"type": "string", "maxLength": 1000}},
                "unknowns": {"type": "array", "maxItems": 32,
                             "items": {"type": "string", "maxLength": 1000}},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "required": ["verdict", "stage_passed", "issues", "next_focus", "unknowns", "confidence"],
            "additionalProperties": False,
        },
    },
}
JUDGE_TOOL["function"]["parameters"]["properties"].update({
    "item_results": review_schema("item_id", ["pass", "continue", "uncertain"]),
    "issue_reviews": review_schema("issue_id", ["resolved", "remaining", "uncertain"]),
})
VIEW_TOOL = {
    "type": "function",
    "function": {
        "name": "request_comparison_view",
        "description": "Capture the same appearance (default) or structure inspection mode/profile for Before and Current. Structure neutralizes material/light appearance and is not evidence of original material/light quality.",
        "parameters": {
            "type": "object",
            "properties": {
                "view": {"type": "object", "properties": {
                    "preset": {"type": "string", "enum": ["main", "auxiliary"]},
                    "mode": {"type": "string", "enum": ["appearance", "structure"]},
                    "position": {"type": "array", "items": {"type": "number"}},
                    "target": {"type": "array", "items": {"type": "number"}},
                    "lens": {"type": "number"},
                    "roll_degrees": {"type": "number", "minimum": -360, "maximum": 360},
                }},
                "reason": {"type": "string"},
            },
            "required": ["view", "reason"],
            "additionalProperties": False,
        },
    },
}
GEOMETRY_TOOL = {
    "type": "function",
    "function": {
        "name": "inspect_geometry",
        "description": "Read the actual frozen mesh of one object/part on the batch Before or Current side: real vertex/edge/polygon counts, optional mesh-local arrays, matrix_world and boundary/wire/non-manifold/component statistics. Read-only; requires part_id for multi-part objects. At most two geometry requests per judgement.",
        "parameters": {
            "type": "object",
            "properties": {
                "side": {"type": "string", "enum": ["before", "current"]},
                "object_id": {"type": "string"},
                "part_id": {"type": "string"},
                "representation": {"type": "string", "enum": ["original", "evaluated"]},
            },
            "required": ["side", "object_id"],
            "additionalProperties": False,
        },
    },
}

SYSTEM = """You are SceneLoop's independent visual Judge. Evaluate only the supplied
goal, constraints, stage scope, committed Before/Current evidence and objective
inspection. You do not receive or rely on the Builder's dialogue, self-assessment,
explanations or private reasoning. Do not edit the scene. Material and lighting are
out of scope unless the supplied stage explicitly includes them.

Each side's scene evidence separates sections: source (run/revision identity;
a legitimately unversioned null differs from unknown), measured (values from that
frozen revision's inspector; method and field_sources state what each value
measures, e.g. original mesh counts versus evaluated bounds), declared (author/model
labels, roles and requested features - never proof), editable_specification
(editing baseline; generated values are nominal, imported values are measured from
the file) and verification (capability boundaries). Only measured values describe
what exists; a declared feature, role, name or planned dimension is not evidence
that an opening, interior, collision or detail was achieved. pending_issues are
runtime ledger records: titles come from author statements and status is workflow
state, not verified fact. Tool success only means an action executed.

You may call inspect_geometry at most twice per judgement, separately from
request_comparison_view, to read the actual frozen mesh of one object/part on the
Before or Current side the scheduler binds. Several read-only calls may share one
response and run in the order returned. submit_judgement must be the only call in
its response. Results carry a geometry evidence ID
that may be cited in issues. Topology facts (boundary/wire/non-manifold edges,
closed surfaces) are not proof of openings, interiors or collisions; original and
evaluated meshes are different sources and their indexes are not interchangeable.

Return submit_judgement with verdict pass, continue, rollback or uncertain. stage_passed
must be true only for pass; continue, rollback and uncertain require false. A visual
rollback verdict is a recommendation only. Cite supplied evidence IDs inside each
issue. Findings must use real object IDs when known and classify relevant geometry,
composition, missing objects, detail, preservation or functional-layout concerns.
Each issue may contain only id, category, object_id, severity, priority, description
and evidence. Omit object_id when unknown; never add explanation or recommendation
fields inside an issue.
Confidence is display evidence only and never overrides the verdict. If evidence is
insufficient, request one paired comparison view if budget allows; otherwise choose
uncertain and state what is missing.
For refinement_standards, return item_results for the item IDs actually evaluated;
omitted items are not passed. Use issue_reviews with the exact existing issue_id to
recheck an old Issue, and issues only for new discoveries. Each explicit review
reports improved, remaining and expected_effect in outcome language, not Blender
commands, plus supplied evidence IDs. Aggregate pass never resolves omitted Issues.
Only explicitly evidenced pass/resolved can establish an item/Issue conclusion."""


class JudgeError(Exception):
    code = "judge_failed"

    def __init__(self, message, *, diagnostics=None):
        super().__init__(message)
        self.diagnostics = copy.deepcopy(diagnostics or {})
        self.recoverable_format = self.diagnostics.get("kind") == "judgement_format"


class JudgeStop(JudgeError):
    """The run was cancelled or is already terminal. This is not an integrity failure."""


class JudgeIntegrity(JudgeError):
    """The frozen source changed around a read. Stop, but do not report a user cancel."""


_FORMAT_SUMMARIES = {
    "missing_judgement_tool": "裁判未提交结构化结论；普通文字不能作为验收判定。",
    "root_not_object": "裁判结论不是对象。",
    "root_fields": "裁判结论字段不完整。",
    "invalid_verdict": "裁判结论或阶段状态无效。",
    "inconsistent_verdict": "裁判结论与阶段状态不一致：pass 必须 stage_passed=true，continue/rollback/uncertain 必须 false。",
    "invalid_confidence": "裁判置信度无效。",
    "invalid_text_list": "裁判建议列表无效。",
    "invalid_issues": "裁判问题列表无效。",
    "issue_not_object": "裁判问题条目不是对象。",
    "ambiguous_object_ids": "裁判问题同时关联多个对象，无法无歧义转换。",
    "invalid_category": "裁判问题分类无效。",
    "invalid_level": "裁判问题等级无效。",
    "invalid_evidence": "裁判问题引用了无效证据。",
    "invalid_object_id": "裁判对象 ID 无效。",
    "invalid_issue_id": "裁判问题 ID 无效。",
    "invalid_description": "裁判问题描述无效。",
    "invalid_reviews": "逐项评审身份、说明或证据无效。",
}


def _format_error(code, *, location="judgement", issue_index=None, count=None):
    diagnostics = {"kind": "judgement_format", "code": code, "location": location,
                   "summary": _FORMAT_SUMMARIES[code]}
    if issue_index is not None:
        diagnostics["issue_index"] = issue_index
    if count is not None:
        diagnostics["count"] = count
    raise JudgeError(diagnostics["summary"], diagnostics=diagnostics)


def _text(value, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise JudgeError("裁判返回了无效文字字段。")
    return value.strip()


def normalize_judgement(value):
    """Normalize bounded, unambiguous schema drift without trusting extra prose."""
    result = copy.deepcopy(value)
    changes, total = [], 0

    def changed(code, issue_index=None, count=None):
        nonlocal total
        total += 1
        if len(changes) < 16:
            row = {"code": code}
            if issue_index is not None:
                row["issue_index"] = issue_index
            if count is not None:
                row["count"] = count
            changes.append(row)

    if not isinstance(result, dict):
        return result, None
    extra = set(result) - JUDGEMENT_FIELDS - OPTIONAL_FIELDS
    if extra:
        for key in extra:
            result.pop(key, None)
        changed("dropped_root_fields", count=len(extra))
    for field in ("next_focus", "unknowns"):
        if isinstance(result.get(field), str):
            result[field] = [result[field]]
            changed("wrapped_text_list")
    if isinstance(result.get("issues"), dict):
        result["issues"] = [result["issues"]]
        changed("wrapped_issue_list")
    if isinstance(result.get("issues"), list):
        for index, row in enumerate(result["issues"]):
            if not isinstance(row, dict):
                continue
            aliases = (("issue_id", "id"), ("evidence_ids", "evidence"),
                       ("dimension", "category"), ("title", "description"))
            for source, target in aliases:
                if target not in row and source in row:
                    row[target] = row[source]
                    changed(f"alias_{source}", index)
                row.pop(source, None)
            if "object_id" not in row and "object_ids" in row:
                object_ids = row.get("object_ids")
                if isinstance(object_ids, list) and len(object_ids) == 1:
                    row["object_id"] = object_ids[0]
                    changed("single_object_id", index)
                elif object_ids not in (None, []):
                    _format_error("ambiguous_object_ids", location="issues", issue_index=index)
                row.pop("object_ids", None)
            if isinstance(row.get("evidence"), str):
                row["evidence"] = [row["evidence"]]
                changed("wrapped_evidence", index)
            category = row.get("category")
            if isinstance(category, str):
                normalized = category.strip().lower().replace("-", "_").replace(" ", "_")
                normalized = {"missing_object": "missing_objects", "functional": "functional_layout"}.get(normalized, normalized)
                if normalized != category:
                    row["category"] = normalized
                    changed("normalized_category", index)
            if row.get("severity") == "moderate":
                row["severity"] = "medium"
                changed("normalized_severity", index)
            if row.get("priority") == "medium":
                row["priority"] = "normal"
                changed("normalized_priority", index)
            if row.get("object_id") == "":
                row["object_id"] = None
                changed("empty_object_id", index)
            extra = set(row) - ISSUE_FIELDS
            if extra:
                for key in extra:
                    row.pop(key, None)
                changed("dropped_issue_fields", index, len(extra))
    metadata = None if not total else {"count": total, "changes": changes, "truncated": total > len(changes)}
    return result, metadata


def validate_judgement(value, evidence_ids, *, return_metadata=False, attempt=None):
    value, normalization = normalize_judgement(value)
    if not isinstance(value, dict):
        _format_error("root_not_object")
    if set(value) - OPTIONAL_FIELDS != JUDGEMENT_FIELDS:
        _format_error("root_fields", count=len(JUDGEMENT_FIELDS - set(value)))
    if value["verdict"] not in JUDGEMENT_VERDICTS or type(value["stage_passed"]) is not bool:
        _format_error("invalid_verdict")
    if not judgement_pair_is_consistent(value["verdict"], value["stage_passed"]):
        _format_error("inconsistent_verdict")
    if type(value["confidence"]) not in (int, float) or not 0 <= value["confidence"] <= 1:
        _format_error("invalid_confidence")
    for field in ("next_focus", "unknowns"):
        if not isinstance(value[field], list) or len(value[field]) > 32:
            _format_error("invalid_text_list", location=field)
        try:
            value[field] = [_text(item, 1000) for item in value[field]]
        except JudgeError:
            _format_error("invalid_text_list", location=field)
    if not isinstance(value["issues"], list) or len(value["issues"]) > 64:
        _format_error("invalid_issues", location="issues")
    issues = []
    for index, row in enumerate(value["issues"]):
        if not isinstance(row, dict):
            _format_error("issue_not_object", location="issues", issue_index=index)
        category = row.get("category")
        if category not in ISSUE_CATEGORIES:
            _format_error("invalid_category", location="issues", issue_index=index)
        severity = row.get("severity")
        priority = row.get("priority")
        if severity not in ISSUE_SEVERITIES or priority not in ISSUE_PRIORITIES:
            _format_error("invalid_level", location="issues", issue_index=index)
        evidence = row.get("evidence")
        if (not isinstance(evidence, list) or len(evidence) > 32
                or any(not isinstance(item, str) or item not in evidence_ids for item in evidence)):
            _format_error("invalid_evidence", location="issues", issue_index=index)
        object_id = row.get("object_id")
        if object_id is not None and (not isinstance(object_id, str) or len(object_id) > 200):
            _format_error("invalid_object_id", location="issues", issue_index=index)
        # Auto IDs are bound to this acceptance attempt so repeated
        # judgements never overwrite unrelated issues sharing a bare counter.
        issue_id = row.get("id") or (f"judge-{attempt[:8]}-{index + 1}" if attempt else f"judge-{index + 1}")
        if not isinstance(issue_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", issue_id):
            _format_error("invalid_issue_id", location="issues", issue_index=index)
        try:
            description = _text(row.get("description"), 2000)
        except JudgeError:
            _format_error("invalid_description", location="issues", issue_index=index)
        issues.append({"id": issue_id, "category": category, "object_id": object_id,
                       "severity": severity, "priority": priority,
                       "description": description,
                       "evidence": list(evidence)})
    result = copy.deepcopy(value)
    result["issues"] = issues
    return (result, normalization) if return_metadata else result


class JudgeAgent:
    def __init__(self, manager, identifier):
        self.manager = manager
        self.identifier = identifier
        self.run = manager.get(identifier)
        self.providers = manager.providers
        self.target = None
        self._input_manifest = None
        self._input_digest = None
        self._geometry_source_changed = False

    def _trace(self, kind, value):
        # Only validated, public fields ever reach the trace: evidence the host
        # assembled, tool names with argument sizes/digests, and sanitized
        # diagnostics. Raw assistant content, unverified arguments, reasoning
        # and image bytes must never be persisted here.
        path = self.manager.run_directory(self.identifier) / "judge-trace.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"kind": kind, "value": value}, ensure_ascii=False) + "\n")

    @staticmethod
    def _response_summary(assistant):
        calls = []
        for call in assistant.get("tool_calls") or []:
            function = call.get("function") or {}
            arguments = function.get("arguments")
            entry = {"tool": function.get("name")}
            if isinstance(arguments, str):
                entry["arguments_bytes"] = len(arguments.encode("utf-8"))
                entry["arguments_digest"] = hashlib.sha256(arguments.encode("utf-8")).hexdigest()
            calls.append(entry)
        return {"tool_calls": calls, "has_content": assistant.get("content") not in (None, "")}

    def should_run(self, stop_reason):
        settings = self.run.get("judge") or {}
        if not settings.get("enabled"):
            return False
        project = self.manager.get_project(self.run["project_id"])
        batch = project["workspace"].get("batch_start") or {}
        edits = int(batch.get("edit_rounds", 0))
        judged = int(batch.get("last_judged_round", 0))
        # The batch spans runs: accepted edits minus the last valid acceptance
        # level decide periodic acceptance, never per-run counters. Pure
        # discussion never repays an old batch debt with a new charge.
        explicit = bool(re.search(r"(?:裁判|验收|judge|检查是否通过)", self.run.get("prompt", ""), re.I))
        if stop_reason in {"discussion", "clarification"}:
            return explicit
        return (explicit or stop_reason in {"goal_reached", "round_limit", "brain_limit", "observation_limit", "tool_limit"}
                or edits - judged >= settings.get("trigger_rounds", 3))

    def _input_image(self, builder, role, path, **fields):
        content = self.providers.image_content(path)
        url = (content.get("image_url") or {}).get("url")
        if not isinstance(url, str) or "," not in url:
            raise JudgeError("裁判图片载荷无效。")
        raw = base64.b64decode(url.split(",", 1)[1], validate=True)
        item_id = builder.add_image(role, path, sent_content=raw, **fields)
        builder.bind_image_payload(content, item_id)
        return content

    def _version_evidence(self, selection, prefix, *, allow_active=False, working_version=None,
                          input_builder=None):
        version = self.manager.scene_version(selection["run_id"], selection.get("revision_id"), allow_active=allow_active)
        revision_id = version["revision_id"]
        artifacts = version["run"]["artifacts"]
        content, ids = [], set()
        scene_identity = {"run_id": version["run"]["id"], "revision_id": revision_id,
                          "working_version": working_version}
        for view in ("main", "auxiliary"):
            artifact = next((item for item in artifacts if item.get("revision_id") == revision_id
                             and item.get("kind") == "render" and item.get("view_id") == view), None)
            if artifact is None:
                continue
            try:
                actual = validate_observation_metadata(artifact.get("observation", {}), {"preset": view})
            except ValueError as error:
                raise JudgeError(str(error)) from None
            path = self.manager.artifact_path(version["run"]["id"], artifact["path"])
            if file_sha256(path) != artifact["sha256"]:
                raise JudgeError("裁判证据图片已变化。")
            evidence_id = f"{prefix}-{view}"
            ids.add(evidence_id)
            image = (self._input_image(input_builder, prefix, path,
                        evidence_id=evidence_id, scene_identity=scene_identity,
                        artifact_id=artifact["id"], view=view, observation=actual)
                     if input_builder is not None else self.providers.image_content(path))
            content += [{"type": "text", "text": f"{evidence_id} version={revision_id} mode=appearance"}, image]
        inspection = json.loads(version["files"]["inspection"].read_text(encoding="utf-8"))
        evidence = build_scene_evidence(version["plan"], inspection, source={
            "run_id": version["run"]["id"], "revision_id": revision_id,
            "working_version": working_version, "working_scene": working_version is not None,
            "artifacts": artifact_refs(version["run"], revision_id)}, measured_parts=True)
        return version, content, ids, evidence

    @staticmethod
    def _digest(value):
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _target(self, project, batch, current, before, current_working, before_working, goal, pending_issues, scope,
                *, kind="working", evaluation="intent", reference_digest=None, target_digest=None):
        """Freeze the evaluated identity and mutable acceptance inputs host-side."""
        return {
            "schema_version": 1,
            "kind": kind,
            "project_id": project["id"],
            "current": copy.deepcopy(current),
            "working_version": current_working,
            "before": copy.deepcopy(before),
            "before_working_version": before_working,
            "stage": (project.get("workspace") or {}).get("stage", "structure"),
            "batch": {"id": batch.get("id"), "status": batch.get("status"),
                      "edit_rounds": int(batch.get("edit_rounds", 0)),
                      "last_judged_round": int(batch.get("last_judged_round", 0))} if batch is not None else None,
            "goal_digest": self._digest(goal),
            "issues_digest": self._digest(pending_issues),
            "scope": copy.deepcopy(scope),
            "evaluation": evaluation,
            "reference_digest": reference_digest,
            "target_digest": target_digest,
            "judge_config": self._judge_config_identity(),
            "current_files": self.manager._snapshot_selection_files(current),
            "before_files": self.manager._snapshot_selection_files(before) if before is not None else None,
        }

    def _judge_config_identity(self):
        """Identify the effective Judge configuration without secrets."""
        return judge_config_identity(self.run)

    def _adopted_target_image(self, evaluation, *, input_builder=None):
        """Attach the adopted reference bytes only in reference mode.

        Intent mode never carries an implicit reference. A missing or changed
        reference file fails the acceptance instead of judging blind.
        """
        if evaluation != "reference":
            if self.run.get("available_reference") is not None:
                self._trace("reference", {"mode": "intent", "available_reference": "withheld"})
            return [], None
        descriptor = self.run.get("reference")
        if not isinstance(descriptor, dict):
            raise JudgeError("参考模式缺少已采用参考，无法验收。")
        artifact = next((item for item in self.run.get("artifacts", [])
                         if item.get("id") == descriptor.get("artifact_id")), None)
        if artifact is None:
            raise JudgeError("已采用参考的登记信息不一致，无法验收。")
        path = self.manager.artifact_path(self.identifier, artifact["path"])
        if file_sha256(path) != descriptor.get("sha256") or file_sha256(path) != artifact.get("sha256"):
            raise JudgeError("采用的参考图丢失或已变化，无法验收。")
        digest = descriptor["sha256"]
        image = (self._input_image(input_builder, "reference", path,
                         evidence_id="adopted-target", artifact_id=artifact["id"])
                 if input_builder is not None else self.providers.image_content(path))
        return ([{"type": "text", "text": f"ADOPTED TARGET sha256={digest}"},
                 image], digest)

    def _pre_step_guard(self):
        """Stop before spending anything once the run is cancelled or terminal."""
        run = self.manager.get(self.identifier)
        if run.get("cancel_requested") or run.get("status") in {"cancelled", "interrupted", "failed"}:
            raise JudgeStop("任务已停止；裁判不再发起新的补拍或模型调用，在途结果保留。")

    def _comparison_view(self, before, current, view, index, *, input_builder=None):
        try:
            view = self.manager.blender.validate_view_spec(view)
        except Exception as error:
            raise JudgeError(str(error)) from None
        output_tag = f"{self.identifier[:8]}-{self.attempt_id[:8]}-{index}"
        if output_tag in self.issued_views:
            raise JudgeError("同一验收内重复补拍同一视角序号；拒绝覆盖旧证据。")
        self.issued_views.add(output_tag)
        self._count_judge_tool()
        content, ids, rows = [], set(), []
        for prefix, selection in (("before", before), ("current", current)):
            self._pre_step_guard()
            version = self.manager.scene_version(selection["run_id"], selection.get("revision_id"),
                                                 allow_active=selection["run_id"] == self.identifier)
            adapter = self.manager._adapter_for(version["run"])
            output_id = f"judge-{output_tag}-{prefix}"
            try:
                result = adapter.observe(version["workspace"], view, output_id)
            except self.manager.blender.BlenderError as error:
                raise JudgeError(f"补拍视角失败：{error}") from None
            try:
                actual = validate_observation_metadata(result, view)
            except ValueError as error:
                raise JudgeError(str(error)) from None
            image_path = result.get("image_path", result.get("path"))
            artifact = self.manager._artifact(version["run"]["id"], image_path, "render",
                                              revision_id=selection.get("revision_id"),
                                              view_id=f"judge-{self.attempt_id[:8]}-{index}", observation_id=output_id,
                                              observation=actual)
            path = self.manager.artifact_path(version["run"]["id"], artifact["path"])
            evidence_id = f"judge-{self.attempt_id[:8]}-{index}-{prefix}"
            ids.add(evidence_id)
            rows.append({"evidence_id": evidence_id, "selection": selection,
                         "artifact_id": artifact["id"], "view": view,
                         **copy.deepcopy(actual)})
            image = (self._input_image(input_builder, "diagnostic", path,
                        evidence_id=evidence_id,
                        scene_identity={"run_id": version["run"]["id"],
                                        "revision_id": selection.get("revision_id")},
                        artifact_id=artifact["id"], view=view, camera=result.get("camera"),
                        observation={**copy.deepcopy(actual), "comparison_group": output_tag, "comparison_role": prefix})
                     if input_builder is not None else self.providers.image_content(path))
            content += [{"type": "text", "text": f"{evidence_id} paired diagnostic view mode={actual['observation_mode']} profile={actual['observation_profile_id']} (structure: neutral inspection, not original material/light quality)"}, image]
        try:
            if comparison_identity(rows[0]) != comparison_identity(rows[1]):
                raise ValueError("Before/Current diagnostic observation conditions differ")
        except ValueError as error:
            raise JudgeError(str(error)) from None
        return content, ids, rows

    def _read_budget_remaining(self):
        """Judge calls left after the current one: read-only tools need room to submit."""
        run = self.manager.get(self.identifier)
        used = run["usage"].get("judge_calls", 0)
        settings = self.run.get("judge") or {}
        if run.get('agent_resource_base'):
            cap = next(row['effective_limit'] for row in run['resource_budget']['resources']
                       if row['resource'] == 'judge_calls' and row['scope'] == 'run')
        elif local_round_enabled(self.run):
            from .refinement_budget import resolve_snapshot
            cap = resolve_snapshot(self.run)["max_judge_calls"]
        else:
            cap = settings.get("max_calls", self.run.get("iteration", {}).get("max_rounds", 1))
        return cap - used

    def _prepare_format_correction(self, diagnostics, correction_count):
        """One correction per evaluation, shared by absent and invalid submissions."""
        self._pre_step_guard()
        diagnostics = copy.deepcopy(diagnostics)
        if correction_count >= 1:
            diagnostics["correction"] = "failed_after_one_attempt"
            self._trace("format_correction", {**diagnostics, "correction_count": correction_count})
            raise JudgeError("裁判格式纠正后仍然无效。", diagnostics=diagnostics)
        if self._read_budget_remaining() < 1 and not self.run.get('agent_resource_base'):
            diagnostics["correction"] = "not_attempted_budget_exhausted"
            self._trace("format_correction", {**diagnostics, "correction_count": correction_count})
            raise JudgeError("裁判返回格式无效，且本轮裁判额度不足以纠正。", diagnostics=diagnostics)
        # Resource-enabled runs keep this count and the exact returned exchange
        # when the next loop boundary captures JudgeResourceWait. No request has
        # been retried or reserved here.
        self._trace("format_correction", {**diagnostics, "correction": "requested", "correction_count": 1})
        return diagnostics

    def _count_judge_tool(self):
        with self.manager.lock:
            run = self.manager._get(self.identifier)
            run["usage"]["judge_tool_calls"] = run["usage"].get("judge_tool_calls", 0) + 1
            self.manager._save(run)

    def _geometry_attempt(self, before, current, request, index, working_versions, *, input_builder=None):
        self._pre_step_guard()
        side = request["side"]
        selection = before if side == "before" else current
        if selection is None:
            raise JudgeError("本次验收没有 Before，before 侧几何检查不可用；请直接提交结论。")
        version = self.manager.scene_version(selection["run_id"], selection.get("revision_id"),
                                             allow_active=selection["run_id"] == self.identifier)
        revision_id = version["revision_id"]
        adapter = self.manager._adapter_for(version["run"])
        blend_path = version["files"]["blend"]
        blend_before = file_sha256(blend_path)
        self._geometry_source_changed = False
        try:
            measurement = adapter.inspect_geometry(version["workspace"], request["object_id"],
                                                   request.get("part_id"), request["representation"])
        except self.manager.blender.BlenderError:
            # A changed file is fatal. An unchanged file stays recoverable unless the
            # adapter already marked this refusal as a source-identity failure.
            self._geometry_source_changed = file_sha256(blend_path) != blend_before
            raise
        measured_hash = (measurement.get("source") or {}).get("blend_sha256")
        if file_sha256(blend_path) != blend_before or measured_hash != blend_before:
            raise JudgeIntegrity("几何读取期间源版本发生变化，未登记该证据。")
        working_version = working_versions.get(side)
        identity = source_identity({
            "run_id": version["run"]["id"], "revision_id": revision_id,
            "working_version": working_version, "working_scene": working_version is not None,
            "artifacts": artifact_refs(version["run"], revision_id),
        }, kind=(measurement.get("source") or {}).get("scene_kind", "unknown"))
        evidence = geometry_evidence_id(revision_id=revision_id, object_id=request["object_id"],
                                        part_id=request.get("part_id"), representation=request["representation"],
                                        session=f"{self.identifier[:8]}-{self.attempt_id[:8]}",
                                        index=index, side=side)
        record = {"side": side, "selection": copy.deepcopy(selection), "revision_id": revision_id,
                  "object_id": request["object_id"], "part_id": request.get("part_id"),
                  "representation": request["representation"], "blend_sha256": blend_before}
        self.geometry_evidence[evidence] = record
        document = finalize_document(measurement, identity=identity, evidence_id_value=evidence)
        if input_builder is not None:
            input_builder.add_measurement(
                side, document, evidence_id=evidence,
                scene_identity={"run_id": version["run"]["id"], "revision_id": revision_id},
                source_path=version["files"]["blend"], source_hash=blend_before)
        return evidence, document, record

    def _revalidate_geometry_evidence(self, used):
        """Accept a geometry ID only while its exact frozen source is still verifiable."""
        from .runtime import AppError
        for evidence in sorted(used):
            record = self.geometry_evidence.get(evidence)
            if record is None:
                continue
            selection = record["selection"]
            try:
                version = self.manager.scene_version(selection["run_id"], selection.get("revision_id"),
                                                     allow_active=selection["run_id"] == self.identifier)
            except AppError as error:
                raise JudgeError(f"几何证据 {evidence} 对应版本已不可核验：{error}") from None
            if file_sha256(version["files"]["blend"]) != record["blend_sha256"]:
                raise JudgeError(f"几何证据 {evidence} 读取后源版本已变化；拒绝把该 ID 作为有效证据。")

    def _append_tool(self, messages, call, payload):
        if not isinstance(payload, str):
            payload = json.dumps(payload, ensure_ascii=False)
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": payload})

    def _append_unexecuted(self, messages, calls, error):
        for call in calls:
            self._append_tool(messages, call, {"ok": False, "error": error})

    def _stop_batch(self, messages, calls, index, current_error, rest_error):
        self._append_tool(messages, calls[index], {"ok": False, "error": current_error})
        self._append_unexecuted(messages, calls[index + 1:], rest_error)

    def _perform_view(self, messages, call, arguments, before_selection, selection, input_builder):
        if not isinstance(arguments, dict) or set(arguments) != {"view", "reason"}:
            self._append_tool(messages, call, {"ok": False, "error": "裁判请求了未授权工具。"})
            return None
        try:
            _text(arguments["reason"], 1000)
        except JudgeError as error:
            self._append_tool(messages, call, {"ok": False, "error": str(error)})
            return None
        if self._view_count >= 2:
            self._append_tool(messages, call, {"ok": False, "error": "裁判请求的补充视角超过本轮上限。"})
            return None
        if before_selection is None:
            self._append_tool(messages, call, {
                "ok": False, "error": "本次验收没有 Before，成对补拍不可用；请直接提交结论。"})
            return None
        self._view_count += 1
        try:
            extra, extra_ids, rows = self._comparison_view(
                before_selection, selection, arguments["view"], self._view_count,
                input_builder=input_builder)
        except JudgeStop:
            raise
        except JudgeError as error:
            self._append_tool(messages, call, {"ok": False, "error": str(error)})
            return None
        self._append_tool(messages, call, {"ok": True, "observations": rows})
        return extra, extra_ids

    def _perform_geometry(self, messages, call, arguments, before_selection, selection,
                          working_versions, input_builder, evidence_ids):
        try:
            request = validate_judge_request(arguments)
        except ValueError as error:
            self._append_tool(messages, call, {"ok": False, "error": str(error)})
            return
        self._count_judge_tool()
        self._geometry_count += 1
        if self._geometry_count > MAX_GEOMETRY_REQUESTS:
            self._append_tool(messages, call, {
                "ok": False,
                "error": f"几何请求超过每次验收上限 {MAX_GEOMETRY_REQUESTS} 次；请提交结论。"})
            return
        try:
            evidence_id, document, _record = self._geometry_attempt(
                before_selection, selection, request, self._geometry_count, working_versions,
                input_builder=input_builder)
        except (JudgeStop, JudgeIntegrity):
            raise
        except (JudgeError, ValueError, self.manager.blender.BlenderError) as error:
            source_identity_rejected = getattr(error, "code", None) == "geometry_source_integrity"
            if isinstance(error, self.manager.blender.BlenderError) and (
                    self._geometry_source_changed or source_identity_rejected):
                raise
            self._append_tool(messages, call, {"ok": False, "error": str(error) or "几何检查失败。"})
            return
        evidence_ids.add(evidence_id)
        self._append_tool(messages, call, compact_json({
            "ok": True, "evidence_id": evidence_id, "geometry": document}))

    def execute(self, target_override=None):
        """Run one independent acceptance attempt.

        target_override is None for the normal working acceptance (project
        current scene plus batch Before). {"kind": "candidate", ...} evaluates
        a generate-run candidate without touching workspace batches/issues;
        {"kind": "open"} is an explicit working acceptance with an explicit
        no-Before identity (never a fabricated recovery batch).
        """
        self.attempt_id = uuid.uuid4().hex
        self.issued_views = set()
        self.geometry_evidence = {}
        evaluation_id = uuid.uuid4().hex
        project = self.manager.get_project(self.run["project_id"])
        workspace = project["workspace"]
        evaluation = (self.run.get("evaluation") or {}).get("mode", "reference")
        stage = workspace.get("stage", "structure")
        scope, scope_note = scope_for_stage(stage)
        goal = self.manager.common_goal(self.run)
        plan = (self.manager.get_refinement_plan(self.run["project_id"])
                if self.run.get("goal_snapshot") else None)
        expectations = ({"plan_id": plan["id"], "plan_version": plan["version"],
                         "goal_id": plan["goal"]["id"], "focus_item_id": plan["focus_item_id"],
                         "items": [{k: copy.deepcopy(item[k]) for k in
                                    ("id", "desired", "basis", "criteria", "object_ids")} for item in plan["items"]],
                         "meaning": "Builder-authored expectations, not measured facts or item acceptance"}
                        if plan else None)
        frozen_standards = standards(plan)
        pending_issues = workspace.get("issues", [])
        kind = (target_override or {}).get("kind", "working")
        if kind == "candidate":
            selection = target_override["selection"]
            before_selection = target_override.get("before")
            current_working = None
            before_working = None
            batch = None
        elif kind == "open":
            selection = project.get("current_scene")
            before_selection = None
            current_working = workspace["working_version"]
            before_working = None
            batch = None
            if not selection:
                raise JudgeError("当前没有可验收的工作版。")
        else:
            batch = project["workspace"].get("batch_start") or {}
            selection = project.get("current_scene")
            before_selection = batch.get("selection") or selection
            current_working = workspace["working_version"]
            before_working = batch.get("working_version") if batch.get("selection") else current_working
            if not selection or not before_selection:
                raise JudgeError("当前没有可验收的工作版或批次起点。")
        input_builder = EvaluationInputBuilder(
            evaluation_id=evaluation_id, run_id=self.identifier, attempt_id=self.attempt_id,
            kind=kind, project_id=self.run["project_id"])
        input_state = self.manager._projects().state_store(self.run["project_id"])
        seal_state = "failed"
        try:
            target_content, target_digest = self._adopted_target_image(
                evaluation, input_builder=input_builder)
            reference = self.run.get("reference") if evaluation == "reference" else None
            self.target = self._target(project, batch, selection, before_selection, current_working,
                                       before_working, goal, pending_issues, scope, kind=kind,
                                       evaluation=evaluation,
                                       reference_digest=(reference or {}).get("sha256"),
                                       target_digest=target_digest)
            input_builder.bind_context({
                "target": copy.deepcopy(self.target), "goal": copy.deepcopy(goal),
                "refinement_expectations": copy.deepcopy(expectations),
                "refinement_standards": copy.deepcopy(frozen_standards),
                "pending_issues": copy.deepcopy(pending_issues),
                "scope": copy.deepcopy(scope), "stage": stage,
                "evaluation": evaluation, "judge_config": self._judge_config_identity()})
            if before_selection is not None:
                before_version, before_images, before_ids, before_evidence = self._version_evidence(
                    before_selection, "before", working_version=before_working,
                    allow_active=before_selection["run_id"] == self.identifier,
                    input_builder=input_builder)
            else:
                before_version, before_images, before_ids, before_evidence = None, [], set(), None
            current_version, current_images, current_ids, current_evidence = self._version_evidence(
                selection, "current", allow_active=selection["run_id"] == self.identifier,
                working_version=current_working, input_builder=input_builder)
            evidence_ids = before_ids | current_ids
            payload = {
                "goal": goal,
                "refinement_expectations": expectations,
                "refinement_standards": frozen_standards,
                "evaluation": evaluation,
                "stage": stage,
                "scope": scope,
                "scope_note": scope_note,
                "before": ({"selection": before_selection, "working_version": before_working,
                            "evidence": before_evidence} if before_selection is not None else
                           {"selection": None, "note": "本次验收无 Before：仅按 Target/Stage 验收 Current，不复制 Current 充当 Before。"}),
                "current": {"selection": selection, "working_version": current_working, "evidence": current_evidence},
                "pending_issues": {
                    "meaning": "Runtime ledger records. Titles/reasons are author statements and remain unverified; "
                               "status is workflow state, not a quality conclusion.",
                    "issues": pending_issues,
                },
                "evidence_ids": sorted(evidence_ids),
            }
            self._trace("evidence", payload)
            messages = [{"role": "system", "content": SYSTEM},
                        {"role": "user", "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]
                                                        + before_images + current_images + target_content}]
            profile = self.manager._judge_profile(self.run)
            self._view_count = 0
            self._geometry_count = 0
            correction_count = 0
            corrected_error = None
            normalization = None
            working_versions = {"before": before_working, "current": current_working}
            self.geometry_evidence = {}
            from .judge_continuation import JudgeResourceWait, capture, restore
            if getattr(self, 'resource_continuation', None):
                input_builder, resumed = restore(self, self.resource_continuation, input_builder.context)
                evaluation_id = input_builder.evaluation_id
                messages, evidence_ids = resumed['messages'], set(resumed['evidence_ids'])
                correction_count, corrected_error = resumed['correction_count'], resumed['corrected_error']
            while True:
                self._pre_step_guard()
                if self.run.get('agent_resource_base') and self._read_budget_remaining() < 1:
                    continuation = capture(self, input_builder, messages,
                        evidence_ids=evidence_ids, correction_count=correction_count, corrected_error=corrected_error)
                    seal_state = 'resource_wait'
                    raise JudgeResourceWait(continuation)
                if not self.manager._reserve_call(self.identifier, "judge"):
                    raise JudgeError("裁判调用已停止。")
                if correction_count or self._read_budget_remaining() < 1:
                    tools = [JUDGE_TOOL]
                else:
                    tools = [JUDGE_TOOL, VIEW_TOOL, GEOMETRY_TOOL]
                sequence = input_builder.prepare_request(
                    messages, tools, config_identity=self._judge_config_identity())
                input_builder.persist(input_state)
                assistant = self.manager._agent_step(self.identifier, profile, messages, tools,
                                                     role="judge", timeout=brain_timeout(profile))
                input_builder.returned(sequence)
                input_builder.persist(input_state)
                self._trace("response", self._response_summary(assistant))
                calls = assistant.get("tool_calls") or []
                if not calls:
                    diagnostics = self._prepare_format_correction({
                        "kind": "judgement_format", "code": "missing_judgement_tool",
                        "location": "response.tool_calls",
                        "summary": _FORMAT_SUMMARIES["missing_judgement_tool"],
                        "response_kind": "text_only" if assistant.get("content") else "empty",
                    }, correction_count)
                    correction_count = 1
                    corrected_error = diagnostics
                    messages.append(copy.deepcopy(assistant))
                    # There is no native tool_call_id to answer. A user-level
                    # protocol reminder follows the actual completed assistant.
                    messages.append({"role": "user", "content": json.dumps({
                        "ok": False, "error": "missing_judgement_tool", "reason": diagnostics,
                        "required_action": "Call submit_judgement exactly once using the frozen evidence. "
                                           "Your previous response executed no tool and did not submit a verdict. "
                                           "Return uncertain if the evidence cannot support a conclusion.",
                        "allowed_evidence_ids": sorted(evidence_ids),
                    }, ensure_ascii=False)})
                    continue
                names = [((call.get("function") or {}).get("name")) for call in calls]
                if "submit_judgement" in names and len(calls) != 1:
                    messages.append(copy.deepcopy(assistant))
                    self._append_unexecuted(messages, calls, "submit_judgement_must_be_exclusive")
                    raise JudgeError("submit_judgement 必须独占一次响应。")
                if names == ["submit_judgement"]:
                    call = calls[0]
                    try:
                        self._pre_step_guard()
                    except JudgeStop as stop:
                        messages.append(copy.deepcopy(assistant))
                        self._append_unexecuted(messages, calls, "cancelled_before_execution")
                        raise stop
                    try:
                        arguments = json.loads(call["function"]["arguments"])
                    except (KeyError, TypeError, ValueError):
                        raise JudgeError("裁判工具参数 JSON 无效。") from None
                    try:
                        result, normalization = validate_judgement(
                            arguments, evidence_ids, return_metadata=True, attempt=self.attempt_id)
                        try:
                            validate_reviews(result, evidence_ids, frozen_standards, pending_issues)
                        except (ValueError, TypeError, KeyError):
                            _format_error("invalid_reviews")
                        self._revalidate_geometry_evidence(
                            {item for field in ("issues", "item_results", "issue_reviews")
                             for row in result.get(field, []) for item in row["evidence"]})
                        break
                    except JudgeStop:
                        raise
                    except JudgeError as error:
                        if not error.recoverable_format:
                            raise
                        diagnostics = self._prepare_format_correction(error.diagnostics, correction_count)
                        correction_count = 1
                        corrected_error = diagnostics
                        messages.append(copy.deepcopy(assistant))
                        messages.append({
                            "role": "tool", "tool_call_id": call["id"],
                            "content": json.dumps({
                                "ok": False,
                                "error": "invalid_judgement_format",
                                "reason": diagnostics,
                                "required_action": "Call submit_judgement once with corrected arguments only.",
                                "allowed_evidence_ids": sorted(evidence_ids),
                                "evidence_rule": "Each evidence item must be one exact registered ID; do not invent IDs or append prose.",
                                "allowed_issue_fields": sorted(ISSUE_FIELDS),
                                "required_issue_fields": ["category", "severity", "priority", "description", "evidence"],
                            }, ensure_ascii=False),
                        })
                        continue
                parsed = []
                for call in calls:
                    try:
                        parsed.append(json.loads(call["function"]["arguments"]))
                    except (KeyError, TypeError, ValueError):
                        raise JudgeError("裁判工具参数 JSON 无效。") from None
                messages.append(copy.deepcopy(assistant))
                deferred_images = []
                from .runtime import AppError
                fatal_errors = (JudgeIntegrity, AppError, EvaluationInputError, OSError,
                                self.manager.blender.BlenderError)
                for index, (call, arguments) in enumerate(zip(calls, parsed)):
                    try:
                        self._pre_step_guard()
                    except JudgeStop:
                        self._stop_batch(messages, calls, index,
                                         "cancelled_before_execution", "cancelled_before_execution")
                        raise
                    name = names[index]
                    try:
                        if name == "request_comparison_view":
                            images = self._perform_view(
                                messages, call, arguments, before_selection, selection, input_builder)
                            if images is not None:
                                content, extra_ids = images
                                evidence_ids.update(extra_ids)
                                deferred_images.append(content)
                            continue
                        if name == "inspect_geometry":
                            self._perform_geometry(
                                messages, call, arguments, before_selection, selection,
                                working_versions, input_builder, evidence_ids)
                            continue
                        self._append_tool(messages, call, {"ok": False, "error": "裁判请求了未授权工具。"})
                    except JudgeStop as stop:
                        self._stop_batch(messages, calls, index, str(stop), "cancelled_before_execution")
                        raise
                    except fatal_errors as error:
                        self._stop_batch(messages, calls, index, str(error) or type(error).__name__,
                                         "stopped_before_execution")
                        raise
                for content in deferred_images:
                    messages.append({"role": "user", "content": content})
                continue
            seal_state = "completed"
            result.update({"id": uuid.uuid4().hex, "attempt_id": self.attempt_id, "kind": kind,
                           "evaluation_id": evaluation_id,
                           "input_id": evaluation_id,
                           "input_digest": input_builder.input_digest,
                           "working_version": current_working,
                           "selection": copy.deepcopy(selection),
                           "batch_id": batch.get("id") if batch is not None else None,
                           "reviewer": "independent_model", "profile_mode": self.run["judge"]["mode"],
                           "format_corrections": correction_count, "target": copy.deepcopy(self.target)})
            if normalization:
                result["normalization"] = normalization
            if corrected_error:
                result["corrected_format_error"] = corrected_error
            return result
        finally:
            if any(row["state"] == "scheduled" for row in input_builder.requests):
                seal_state = "unknown"
            if self.manager.get(self.identifier).get("cancel_requested"):
                seal_state = "cancelled"
            input_builder.persist(input_state)
            if seal_state == 'resource_wait':
                self._input_manifest = input_builder.manifest()
                self._input_digest = input_builder.input_digest
            else:
                input_builder.seal(final_state=seal_state)
                self._input_manifest = input_builder.manifest()
                self._input_digest = input_builder.input_digest
                input_state.seal_evaluation_input(
                    input_builder.evaluation_id, final_state=seal_state,
                    manifest=self._input_manifest)
