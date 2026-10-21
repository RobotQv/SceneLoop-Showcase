"""Persistent project selection and repeatable scene tasks, without model calls on open."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import uuid

from .judge_contract import (
    JUDGEMENT_VERDICTS, JUDGEMENT_SCOPE, STRUCTURAL_STAGES,
    JUDGEMENT_TARGET_FIELDS, JUDGEMENT_BATCH_FIELDS, JUDGEMENT_KINDS,
    scope_for_stage, judge_config_identity, judge_outcome, judgement_pair_is_consistent,
)


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def is_link(path):
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _prepare_judge_checkpoint(manager, identifier, state, stop_reason, judge_type):
    run = manager.get(identifier)
    settings = run.get("judge") or {}
    if not settings.get("enabled") or not run.get("project_id"):
        return judge_outcome(stop_reason, "not_requested"), None
    project = manager.get_project(run["project_id"])
    workspace = project["workspace"]
    stage = workspace.get("stage", "structure")
    scope, _ = scope_for_stage(stage)
    evaluation = (run.get("evaluation") or {}).get("mode", "reference")
    operation = run.get("operation")
    key = None
    override = None
    describe = ""
    shouldexec = False
    local_periodic_due = False
    if (operation == "edit" and run.get("iteration", {}).get("mode") == "local_round_v1"
            and stop_reason == "periodic"):
        readiness = manager._projects().state_store(run["project_id"]).readiness(identifier)
        local_periodic_due = (not readiness["needs_reconciliation"]
                              and readiness["rounds_since_judge"] is not None
                              and readiness["rounds_since_judge"] >= settings["trigger_rounds"])
    judge = judge_type(manager, identifier)
    if operation == "edit":
        batch = workspace.get("batch_start") or {}
        selection = project.get("current_scene")
        if selection is None and not batch:
            return judge_outcome(stop_reason, "not_requested", reason="no_scene"), None
        if not batch:
            key = manager._judge_cache_key(run, project, workspace, kind="open", scope=scope,
                                        stage=stage, evaluation=evaluation, selection=selection,
                                        working_version=workspace.get("working_version"),
                                        before=None, batch=None)
            explicit = bool(re.search(r"(?:裁判|验收|judge|检查是否通过)", run.get("prompt", ""), re.I))
            shouldexec = local_periodic_due or explicit or stop_reason in {
                "goal_reached", "round_limit", "brain_limit", "observation_limit", "tool_limit"}
            override = {"kind": "open"}
            describe = "当前工作版（显式只验收，无批次起点）"
        else:
            key = manager._judge_cache_key(run, project, workspace, kind="working", scope=scope,
                                        stage=stage, evaluation=evaluation, selection=selection,
                                        working_version=workspace.get("working_version"),
                                        before=batch.get("selection"), batch=batch)
            shouldexec = local_periodic_due or judge.should_run(stop_reason)
            describe = f"已提交工作版（触发：{stop_reason}）"
    elif operation == "generate":
        revisions = (state or {}).get("revisions", [])
        selected = (state or {}).get("selected_revision")
        if not selected or not revisions:
            return judge_outcome(stop_reason, "not_requested", reason="no_candidate"), None
        accepted = [row for row in (state or {}).get("actions", [])
                    if row.get("status") == "modified_pending_verification"]
        first = revisions[0]["id"]
        candidate = {"run_id": identifier, "revision_id": selected}
        before = {"run_id": identifier, "revision_id": first} if selected != first else None
        key = manager._judge_cache_key(run, project, workspace, kind="candidate", scope=scope,
                                    stage=stage, evaluation=evaluation, selection=candidate,
                                    working_version=None, before=before, batch=None,
                                    accepted_count=len(accepted))
        judged = max([row.get("accepted_count") or 0
                      for row in run.get("judge_history", [])
                      if (row.get("key") or {}).get("kind") == "candidate"
                      and row.get("status") == "completed"] or [0])
        explicit = bool(re.search(r"(?:裁判|验收|judge|检查是否通过)", run.get("prompt", ""), re.I))
        shouldexec = (len(accepted) > judged or explicit
                      or stop_reason in {"goal_reached", "round_limit", "brain_limit",
                                         "observation_limit", "tool_limit"})
        override = {"kind": "candidate", "selection": candidate, "before": before}
        describe = f"本轮候选 {selected}（触发：{stop_reason}）"
    else:
        return judge_outcome(stop_reason, "not_requested", reason="unsupported_operation"), None
    prior = manager._judge_cache_hit(run, run.get("judge_history", []), key)
    if prior is not None:
        return prior, None
    for old in reversed(run.get("judge_history", [])):
        if (old.get("status") == "completed" and not old.get("input_id")
                and manager._acceptance_subject(old.get("key") or {})
                == manager._acceptance_subject(key)):
            return judge_outcome(stop_reason, "failed",
                                 judgement=copy.deepcopy(old.get("judgement")),
                                 unknown=True, reason="legacy_unknown",
                                 key=copy.deepcopy(key), time=manager._timestamp()), None
    concluded = manager._concluded_acceptance(run, key)
    if concluded is not None:
        return judge_outcome(
            concluded.get("trigger", stop_reason), concluded["status"],
            judgement=copy.deepcopy(concluded.get("judgement")),
            input_id=concluded.get("input_id"),
            application_receipt=copy.deepcopy(concluded.get("application_receipt")),
            unknown=bool(concluded.get("unknown")), reason=concluded.get("reason"),
            key=copy.deepcopy(concluded.get("key")), time=concluded.get("time"),
            committed_rounds=concluded.get("committed_rounds"),
            accepted_count=concluded.get("accepted_count"),
            applied_post=copy.deepcopy(concluded.get("applied_post"))), None
    return None, {"run": run, "scope": scope, "judge": judge, "key": key,
                  "override": override, "describe": describe, "shouldexec": shouldexec}


class ProjectRunMixin:
    def open_walkthrough(self, identifier, body):
        from .walkthrough import open_walkthrough
        return open_walkthrough(self, identifier, body)

    def _projects(self):
        from .runtime import AppError
        if self.project_repository is None:
            raise AppError("当前服务未启用项目工作区，请重启新版服务。", "projects_unavailable", 503)
        return self.project_repository

    def list_projects(self):
        return self._projects().list()

    def get_project(self, identifier):
        project = self._projects().get(identifier)
        modes = {}
        for mode in ("headless", "attached"):
            try:
                adapter = self._adapter_for({"project_id": identifier, "execution": {"mode": mode}})
                capability = adapter.capabilities
                if callable(capability):
                    capability = capability()
                if mode == "attached":
                    self._check_live_root(adapter, identifier)
                modes[mode] = {"available": bool(capability.get("available", self.discovery.get("available", False))),
                               "reason": capability.get("reason", capability.get("error", ""))}
            except Exception as error:
                modes[mode] = {"available": False, "reason": str(error) if getattr(error, "code", None) else "项目连接尚不可用。"}
        project["blender"] = {**self.discovery, "modes": modes}
        current = project.get("current_scene")
        project["design_context"] = self._design_context(self.runs[current["run_id"]]) if current and current["run_id"] in self.runs else None
        return project

    def open_project(self, path, name=None):
        with self.lock:
            project = self._projects().open(path, name)
            self._load_root(self._projects().runs_root(project["id"]), project["id"])
            return self.get_project(project["id"])

    def run_directory(self, identifier):
        # Never derive a filesystem root from persisted run JSON or an HTTP path.
        from .runtime import AppError
        if identifier not in self.run_directories:
            raise AppError("任务目录尚未绑定。", "run_directory_missing", 404)
        # Resolving a bound directory consumes only run ownership, not committed
        # fields. Keep SQL receipt projection at get/list/save state boundaries;
        # doing it here repeats it for every artifact and again inside _save.
        if identifier not in self.runs:
            raise AppError("任务不存在。", "not_found", 404)
        run = self.runs[identifier]
        base = self._projects().runs_root(run["project_id"]) if run.get("project_id") else self.root
        expected = base / identifier
        bound = self.run_directories[identifier]
        if (bound != expected or is_link(expected) or is_link(expected / "run.json")
                or expected.resolve().parent != base.resolve()):
            raise AppError("任务目录指向已改变，停止访问外部路径。", "unsafe_run_path", 409)
        return expected.resolve()

    def _adapter_for(self, run):
        arguments = {"mode": run["execution"]["mode"], "blender_path": self.discovery.get("path")}
        if run.get("project_id") and arguments["mode"] == "attached":
            arguments["bridge_dir"] = self._projects().runs_root(run["project_id"]).parent / "bridge"
        return self.blender.create_adapter(**arguments)

    def _check_live_root(self, adapter, project_id):
        from .runtime import AppError
        if not hasattr(adapter, "bridge_dir") or not hasattr(self.blender, "bridge_status"):
            return
        status = self.blender.bridge_status(adapter.bridge_dir)
        if status.get("connected"):
            output = self._projects().runs_root(project_id).resolve()
            allowed = Path(status["output_root"]).resolve()
            if not output.is_relative_to(allowed) or output.is_relative_to(Path(adapter.bridge_dir).resolve()):
                raise AppError("Live 桥的输出根不包含该项目，请设置项目的 SCENELOOP_BRIDGE_DIR / SCENELOOP_OUTPUT_ROOT 后重连。", "bridge_project_mismatch", 409)

    def _project_environment(self, request):
        from .runtime import AppError
        adapter = self._adapter_for(request)
        capabilities = adapter.capabilities
        if callable(capabilities):
            capabilities = capabilities()
        if not capabilities.get("available", self.discovery.get("available", False)):
            raise AppError("项目的 Blender 连接不可用。Live 模式需将桥接目录设为项目的 .sceneloop/bridge，并在该项目输出根启动桥接。", "blender_unavailable", 409)
        if request["execution"]["mode"] == "attached":
            self._check_live_root(adapter, request["project_id"])
        return adapter

    def bind_refinement_goal(self, identifier):
        """Execution boundary after queue/reference resolution, before any Builder call."""
        with self.lock:
            run = self._get(identifier)
            self._assert_asset_write(run.get("project_id"), run_id=identifier)
            if not run.get("project_id"):
                return None
            from .refinement_method import bind_method
            method = bind_method(run)
            store = self._projects().state_store(run["project_id"])
            delegated = self._director_host().binding(identifier,run['project_id']) if run.get('director_delegation') else None
            if run.get('operation')=='conversation' and self._director_host().store(run['project_id']).task().get('goal_ref'):
                binding = store.bind_director_parent(run,director_store=self._director_host().store(run['project_id']))
            else:
                binding = store.bind_director_child(run,delegated) if delegated else store.bind_refinement(run)
            run["refinement_method"] = method
            run["goal_snapshot"] = copy.deepcopy(binding["goal"])
            run["refinement_binding"] = {k: copy.deepcopy(v) for k, v in binding.items() if k != "goal"}
            run["constraints"] = copy.deepcopy(binding["goal"]["content"]["constraints"])
            self._save(run)
            self._freeze_run_handoff(run)
            return copy.deepcopy(binding)

    def common_goal(self, run):
        """C1 legacy records keep their original digest; new runs consume SQL binding."""
        if run.get("goal_snapshot") and run.get("project_id"):
            value = self._projects().state_store(run["project_id"]).refinement_state(run["id"])
            if value["binding"]:
                return copy.deepcopy(value["binding"]["goal"])
            from .refinement_plan import RefinementError
            raise RefinementError("已绑定目标的SQL记录缺失。", "refinement_unbound", 409)
        return {"request": run["prompt"], "design_context": run.get("design_context"),
                "constraints": run.get("constraints", {})}

    def _assert_refinement_goal(self, run):
        if self.runs.get(run['id'],{}).get('director_ledger_unknown'):
            from .refinement_plan import RefinementError
            raise RefinementError('费用attempt持久化待核实，停止后续写入。','director_ledger_unknown',409)
        if run.get("goal_snapshot") and run.get("project_id"):
            value = self._projects().state_store(run["project_id"]).refinement_state(run["id"])
            if (not value["binding"] or not value["goal"]
                    or value["binding"]["goal"]["id"] != value["goal"]["id"]):
                from .refinement_plan import RefinementError
                raise RefinementError("执行目标已变化，保留原候选并停止旧目标写入。", "refinement_goal_changed", 409)
        return None

    def get_refinement_plan(self, project_id, *, item_ids=None):
        from .refinement_plan import project_view
        with self.lock:
            store = self._projects().state_store(project_id)
            state = store.refinement_state()
            project = store.load()[0]
            result = project_view(state["plan"], state["goal"], project, item_ids=item_ids)
            if result is None:
                return None
            evaluation = self._refinement_evaluation_view(project, result)
            for item in result["items"]:
                facts = item["facts"]
                reviews = [r for e in evaluation["evaluations"] for r in e["item_results"]
                           if r["item_id"] == item["id"]]
                current_reviews = [r for r in reviews if r["current"]]
                latest = current_reviews[-1] if current_reviews else None
                facts["item_reviews"] = reviews
                facts["judge_passed"] = bool(latest and latest["verdict"] == "pass")
                if reviews:
                    facts["scope"] = "explicit_item_acceptance"
                for operation_id in item["operation_ids"]:
                    receipt = store.lookup_receipt(operation_id)
                    if receipt["status"] == "committed":
                        record = receipt["receipt"]
                        facts["operation_receipts"].append({"operation_id": operation_id,
                            "selection": record.get("target_selection"), "working_version": record.get("working_version"),
                            "artifact_status": receipt.get("artifact_status"), "meaning": "scene_saved_not_item_accepted"})
                facts["saved"] = bool(facts["operation_receipts"])
                for evaluation_id in item["evaluation_ids"]:
                    receipt = store.lookup_evaluation_receipt(evaluation_id)
                    if receipt:
                        facts["evaluations"].append({"evaluation_id": evaluation_id,
                            "application_status": receipt.get("application_status"),
                            "result": copy.deepcopy(receipt.get("result")),
                            "meaning": "whole_evaluation_not_item_pass"})
                for observation in item["observations"]:
                    for ref in observation["evidence"]:
                        try:
                            run = self.get(ref["run_id"])
                            artifact = next((a for a in run["artifacts"] if a["id"] == ref["artifact_id"]), None)
                            if (run.get("project_id") != project_id or artifact is None
                                    or artifact.get("revision_id") != ref["revision_id"]
                                    or artifact.get("sha256") != ref["sha256"]):
                                raise ValueError("evidence identity changed")
                            self._checked_artifact(run, artifact)
                        except Exception:
                            observation["review_required"] = True
                            if "evidence_unavailable" not in observation["review_reasons"]:
                                observation["review_reasons"].append("evidence_unavailable")
            return result

    def _refinement_standards(self, project_id):
        from .refinement_evaluation import standards
        state = self._projects().state_store(project_id).refinement_state()
        return standards({**state["plan"], "goal": state["goal"]}) if state["plan"] else None

    def _refinement_evaluation_view(self, project, plan):
        from .refinement_evaluation import project_evaluations
        store = self._projects().state_store(project["id"])
        def verify(receipt, target):
            reasons = []
            try:
                run = self.get(receipt["run_id"])
                if self._validate_evaluation_input(project["id"], receipt["input_id"],
                                                  run_id=run["id"], judgement=receipt["result"]):
                    reasons.append("input_unavailable")
                if not self._snapshots_match(target.get("current_files"),
                                             self._snapshot_selection_files(target.get("current"))):
                    reasons.append("files_changed")
                if target.get("evaluation") == "reference" and not self._reference_bytes_match(
                        run, target.get("target_digest")):
                    reasons.append("reference_changed")
            except Exception:
                reasons.append("evidence_unavailable")
            return reasons
        return project_evaluations(project, plan, store.refinement_evaluations(), verify)

    def get_refinement(self, project_id):
        with self.lock, self._projects().lock:
            plan = self.get_refinement_plan(project_id)
            project = self._projects().state_store(project_id).load()[0]
            return {"plan": plan, "evaluation": self._refinement_evaluation_view(project, plan)}

    def update_refinement_plan(self, identifier, body, *, agent_state=None):
        from .refinement_plan import PlanRejected, RefinementError, selection_identity, validate_update
        from .scene_evidence import known_object_ids
        try:
            body = validate_update(body)
        except RefinementError as error:
            # Only this pure local validator establishes a known write-free
            # input rejection. SQL/CAS/evidence failures below stay distinct.
            raise PlanRejected(str(error), error.code, error.status, fields=['input']) from None
        # Preserve the established run -> repository lock order. The repository
        # OS lock owns this project; the final SQL CAS also checks scene identity.
        with self.lock, self._projects().lock:
            run = self.get(identifier)
            delegated = self._director_host().binding(identifier,run.get('project_id')) if run.get('director_delegation') else None
            if delegated:
                brief = delegated['brief']
                from .director_goal_plan import check_builder
                check_builder(brief,body)
                denied=[]
                if 'order' in body: denied.append('order')
                if body.get('focus_item_id') not in {None,*brief['item_ids']}: denied.append('focus_item_id')
                for index,row in enumerate(body.get('changes',[])):
                    if row['item_id'] not in brief['item_ids']: denied.append(f'changes[{index}].item_id')
                    if set(row.get('set',{}).get('object_ids',[]))-set(brief['object_ids']):
                        denied.append(f'changes[{index}].set.object_ids')
                if denied:
                    raise PlanRejected('委派Builder只可更新授权工作项、对象范围及自身focus。','director_scope_denied',409,
                                       fields=denied)
            self._assert_asset_write(run.get("project_id"), run_id=identifier)
            if run.get("cancel_requested"):
                raise RefinementError("已取消，不再更新计划。", "cancelled", 409)
            if not run.get("project_id") or not run.get("goal_snapshot"):
                raise RefinementError("本任务未绑定项目计划。", "refinement_unbound", 409)
            store = self._projects().state_store(run["project_id"])
            prior = store.refinement_update_receipt(identifier, body)
            if prior is not None:
                return prior
            state = store.refinement_state(identifier)
            if state["plan"]["version"] != body["expected_version"]:
                raise RefinementError("计划版本已变化。", "refinement_version_conflict", 409,
                                      current_version=state["plan"]["version"])
            self._assert_refinement_goal(run)
            project = store.load()[0]
            current = selection_identity(project.get("current_scene"))
            agent = agent_state if agent_state is not None else run.get("agent", {})
            selections = [current] if current else []
            selections += [{"run_id": identifier, "revision_id": key} for key in
                           {agent.get("selected_revision"), agent.get("pending_revision")} - {None}]
            allowed = {(r["run_id"], r["revision_id"]) for r in selections}
            versions = {}
            def version_for(run_id, revision_id):
                key = (run_id, revision_id)
                if key not in allowed:
                    raise RefinementError("引用不是当前正式版或本任务实际候选。", "refinement_reference_invalid", 409)
                if key not in versions:
                    candidate = self.get(run_id)
                    if candidate.get("project_id") != run["project_id"]:
                        raise RefinementError("引用跨项目。", "refinement_reference_invalid", 409)
                    versions[key] = self.scene_version(run_id, revision_id, allow_active=run_id == identifier)
                return versions[key]
            observations = {}
            for change in body.get("changes", []):
                fields = change.get("set", {})
                if fields.get("object_ids"):
                    known = set()
                    for selection in selections:
                        version = version_for(**selection)
                        inspection = json.loads(version["files"]["inspection"].read_text(encoding="utf-8"))
                        known.update(known_object_ids(version["plan"], inspection))
                    if set(fields["object_ids"]) - known:
                        raise RefinementError("对象引用不在当前场景/候选内；尚未建立的对象请保留空范围。", "refinement_reference_invalid")
                for issue_id in fields.get("issue_ids", []):
                    if issue_id not in {i.get("id") for i in project["workspace"].get("issues", [])}:
                        raise RefinementError("Issue引用不存在。", "refinement_reference_invalid")
                for operation_id in fields.get("operation_ids", []):
                    found = store.lookup_receipt(operation_id)
                    if found["status"] != "committed" or found["receipt"].get("project_id") != run["project_id"]:
                        raise RefinementError("需要本项目实际提交回执。", "refinement_reference_invalid")
                for evaluation_id in fields.get("evaluation_ids", []):
                    found = store.lookup_evaluation_receipt(evaluation_id)
                    if not found or found.get("project_id") != run["project_id"]:
                        raise RefinementError("需要本项目实际Evaluation回执。", "refinement_reference_invalid")
                rows = []
                for observation in change.get("observations", []):
                    refs = []
                    for ref in observation["evidence"]:
                        version = version_for(ref["run_id"], ref["revision_id"])
                        artifact = next((a for a in version["run"]["artifacts"] if a["id"] == ref["artifact_id"]), None)
                        if (not artifact or artifact.get("revision_id") != ref["revision_id"]
                                or artifact.get("kind") not in {"render", "inspection", "geometry_inspection"}):
                            raise RefinementError("证据不是所引用版本的真实观察/检查产物。", "refinement_evidence_invalid")
                        self._checked_artifact(version["run"], artifact)
                        refs.append({**ref, "sha256": artifact["sha256"], "kind": artifact["kind"]})
                    rows.append({**copy.deepcopy(observation), "evidence": refs,
                                 "goal_id": state["goal"]["id"], "scene": current, "author": "builder"})
                observations[change["item_id"]] = rows
            return store.update_refinement(identifier, body, observations, expected_scene=current)

    def _local_round_state(self, run, *, allow_paused=False):
        from .refinement_round import RoundError, criteria_snapshot, digest, enabled
        if not enabled(run) or not run.get("project_id"):
            raise RoundError("此Run未启用局部草稿轮。", "round_not_resumable", 409)
        store = self._projects().state_store(run["project_id"])
        document = store.local_round(run["id"])
        if document is None or document["status"] not in ({"active", "paused"} if allow_paused else {"active"}):
            raise RoundError("当前没有可写的局部轮。", "round_not_resumable", 409)
        project = self._projects().get(run["project_id"], verify_artifacts=False)
        start = document["start"]
        if (project["revision"] != start["project_revision"]
                or project["workspace"]["working_version"] != start["working_version"]
                or project.get("current_scene") != start["selection"]):
            raise RoundError("Working已变化，旧草稿保留但不能继续写入。", "round_working_changed", 409)
        state = store.refinement_state(run["id"])
        if (not state["goal"] or not state["binding"] or not state["plan"]
                or state["goal"]["id"] != document["goal_id"]
                or state["binding"]["goal"]["id"] != document["goal_id"]):
            raise RoundError("共同目标已变化。", "round_goal_changed", 409)
        if digest(criteria_snapshot(state["plan"], document["item_ids"])) != document["criteria_digest"]:
            raise RoundError("受评标准已变化。", "round_criteria_changed", 409)
        for step in document["steps"]:
            if step["status"] in {"scheduled", "unknown"}:
                raise RoundError("草稿外部写结果尚未确认。", "round_operation_unknown", 409)
        return store, document

    def get_local_round(self, identifier):
        """Read-only projection; artifact checks never promote a draft."""
        from .refinement_round import RoundError, enabled
        with self.lock:
            run = self._get(identifier)
            if not enabled(run):
                return None
            store = self._projects().state_store(run["project_id"])
            document = store.local_round(identifier)
            if document is None:
                return None
            ready, reason = False, None
            if document["status"] == "paused":
                try:
                    self._local_round_state(run, allow_paused=True)
                    if (not document.get("checkpoint") or document["checkpoint"].get("revision_id")
                            != document["selected_revision"] or document["pending_revision"] is not None):
                        raise RoundError("草稿没有当前已确认恢复点。", "round_not_resumable", 409)
                    if store.verify_artifacts(document["start_artifacts"]):
                        raise RoundError("局部轮起点证据已变化。", "round_evidence_invalid", 409)
                    for artifact in document["checkpoint"]["artifacts"]:
                        if store.verify_artifacts([artifact]):
                            raise RoundError("草稿恢复点产物已变化。", "round_evidence_invalid", 409)
                    from .refinement_budget import resolve_snapshot
                    limits = resolve_snapshot(run)
                    used = (document.get("agent_state") or {}).get("used") or {}
                    usage = run.get("usage") or {}
                    if (usage.get("brain_calls", 0) >= limits["max_brain_calls"]
                            or used.get("tool_calls", 0) >= limits["max_tool_calls"]
                            or used.get("observations", 0) >= limits["max_observations"]):
                        raise RoundError("累计资源已用完。", "round_resource_exhausted", 409)
                    ready = True
                except RoundError as error:
                    reason = error.code
            result = copy.deepcopy(document)
            result.pop("agent_state", None)
            worker = self.workers.get(identifier)
            result["end_ready"] = (document["status"] in {"active", "paused"}
                                   and not (worker and worker.is_alive()))
            if not ready and reason is None and document["status"] == "active":
                reason = ("round_operation_unknown" if any(step["status"] in {"scheduled", "unknown"}
                                                            for step in document["steps"])
                          else "round_not_resumable")
            result["resume_ready"], result["resume_block_reason"] = ready, reason
            result["budget_progress"] = copy.deepcopy(run["budget_progress"])
            transform_proofs=[]
            for step in document['steps']:
                if step.get('kind')=='asset_transform' and step['status'] in {'scheduled','unknown'}:
                    transform_proofs.append(self._asset_transform_receipt_view(run,step,store))
            if transform_proofs:result['asset_transform_receipts']=transform_proofs
            return result

    def _asset_transform_receipt_view(self, run, step, store):
        """Only read original proof. Missing native ACK never resumes a writer."""
        from .asset_contract import registration,inspection as inspect_asset
        result={'operation_id':step['operation_id'],'attempt_id':step['attempt_id'],
                'revision_id':step['revision_id'],'status':'unverified','resume_authorized':False}
        try:
            if not re.fullmatch(r'r[0-9]{3,6}',step['revision_id']):raise ValueError
            root=self.run_directory(run['id']);folder=root/'candidates'/step['revision_id']
            path=folder/'asset-transform-receipt.json'
            if (is_link(root/'candidates') or is_link(folder) or is_link(path)
                    or not path.resolve().is_relative_to(root) or not path.is_file() or path.stat().st_size>1024*1024):raise ValueError
            value=json.loads(path.read_text(encoding='utf-8'))
            for key,expected in (('operation_id',step['operation_id']),('attempt_id',step['attempt_id']),
                                 ('run_id',run['id']),('base_revision',step['base_revision']),('revision_id',step['revision_id'])):
                if value.get(key)!=expected:raise ValueError
            expected_request={'reason':step['reason'],'operations':step['operations']}
            if value.get('schema_version')!=1 or value.get('request')!=expected_request:raise ValueError
            operation=store.operation_state(step['operation_id'])
            identity=(operation or {}).get('identity') or {}
            from .refinement_round import digest
            if (identity.get('kind')!='asset_transform' or identity.get('base_revision')!=step['base_revision']
                    or identity.get('operations_digest')!=digest(step['operations'])):raise ValueError
            artifacts=self._fixed_scene_artifacts(run,step['revision_id'])
            if value.get('artifacts')!=artifacts or store.verify_artifacts(artifacts):raise ValueError
            plan=json.loads((folder/'plan.json').read_text(encoding='utf-8'))
            actual=json.loads((folder/'inspection.json').read_text(encoding='utf-8'))
            rows=registration(plan['assets'],[o['id'] for o in plan['objects']])['instances']
            target=step['operations'][0]['instance_id'];registered=next(row for row in rows if row['instance_id']==target)
            measured=next(row for row in actual['assets'] if row['instance_id']==target)
            inspect_asset(measured,registered)
            receipt_asset=value.get('asset') or {}
            if any(receipt_asset.get(k)!=registered[k] for k in ('instance_id','source','transform')):raise ValueError
            result.update(status='verified_receipt_needs_native_review',receipt_sha256=file_sha256(path),
                          meaning='Original candidate/attempt evidence verified; no Working publish, second write or model replay authorized.')
        except Exception:
            result['reason_code']='asset_transform_receipt_unverified'
        return result

    def begin_local_round(self, identifier, body, *, agent_state):
        from .refinement_round import RoundError, criteria_snapshot, digest, enabled, validate_begin
        from .refinement_budget import budget_progress, edit_limit, resolve_snapshot
        value = validate_begin(body)
        with self.lock:
            run = self._get(identifier)
            if run.get('director_delegation'):
                delegated = self._director_host().binding(identifier,run['project_id'])
                self._director_host().child_admission(run)
                if not delegated or set(value['item_ids'])-set(delegated['brief']['item_ids']):
                    raise RoundError('局部轮不能引用委派范围外工作项。','director_scope_denied',409)
            if not enabled(run):
                raise RoundError("此Run未启用局部轮。", "round_not_resumable", 409)
            self._verify_project_baseline(run)
            self._assert_refinement_goal(run)
            store = self._projects().state_store(run["project_id"])
            limits = resolve_snapshot(run)
            task_saves = next((r for r in (run.get('resource_budget') or {}).get('resources', [])
                               if r['resource'] == 'saved_rounds' and r['scope'] == 'task'), None)
            if task_saves and (task_saves['remaining'] is None or task_saves['remaining'] < 1):
                raise RoundError('本task总保存池不足，Run增量不能替代task批准。', 'round_task_save_limit', 409)
            readiness = store.readiness(identifier)
            since = readiness["rounds_since_judge"]
            trigger = run["judge"]["trigger_rounds"]
            if (readiness["needs_reconciliation"] or readiness["saved_edit_rounds"] is None
                    or since is None):
                raise RoundError("正式保存或Judge计数待核对。", "round_judge_required", 409)
            if (readiness["saved_limit_reached"]
                    or readiness["saved_edit_rounds"] >= limits["saved_round_limit"]):
                raise RoundError("本请求的正式保存上限已达到。", "round_resource_exhausted", 409)
            if since >= trigger:
                raise RoundError("先完成当前Working的适用Judge。", "round_judge_required", 409)
            if limits["schema_version"] == 2:
                blocker = edit_limit(budget_progress(run, store, agent_state=agent_state))
                if blocker:
                    raise RoundError(f"新局部轮预算不足：{blocker}。", "round_resource_exhausted", 409)
            state = store.refinement_state(identifier)
            if state["plan"] is None or state["goal"] is None:
                raise RoundError("Plan或目标缺失。", "round_goal_changed", 409)
            criteria = criteria_snapshot(state["plan"], value["item_ids"])
            project = self._projects().get(run["project_id"], verify_artifacts=False)
            start = {"project_revision": project["revision"],
                     "working_version": project["workspace"]["working_version"],
                     "selection": copy.deepcopy(project["current_scene"]),
                     "revision_id": agent_state.get("selected_revision")}
            if start["revision_id"] is None:
                raise RoundError("局部轮开始前需要真实基线观察。")
            artifacts = self._fixed_scene_artifacts(run, start["revision_id"])
            round_id = uuid.uuid4().hex
            prepared = self._scene_operation(identifier, "local-round-" + round_id, "builder")
            document = {"schema_version": 1, "id": round_id, "run_id": identifier,
                        "project_id": run["project_id"], "version": 1, "status": "active",
                        "created_at": self._timestamp(), "goal_id": state["goal"]["id"],
                        "goal_digest": state["goal"]["content_digest"],
                        "plan_id": state["plan"]["id"], "item_ids": value["item_ids"],
                        "criteria_items": criteria, "criteria_digest": digest(criteria),
                        "local_goal": value["goal"], "local_criteria": value["criteria"],
                        "start": start, "start_artifacts": artifacts,
                        "selected_revision": start["revision_id"], "pending_revision": None,
                        "steps": [], "checkpoint": None, "commit_operation_id": prepared["operation_id"],
                        "commit_identity": prepared["identity"], "active_seconds_used": 0,
                        "agent_state": copy.deepcopy(agent_state), "receipt_operation_id": None}
            store.begin_local_round(document)
            return {"round_id": round_id, "version": 1, "start": copy.deepcopy(start),
                    "goal": value["goal"], "criteria": value["criteria"]}

    def start_local_step(self, identifier, revision_id, base_revision, edit_plan, *, kind='draft_edit', candidate_copy=None):
        from .refinement_round import RoundError, digest
        from .refinement_budget import budget_progress, edit_limit, resolve_snapshot
        if kind not in {'draft_edit','asset_transform'}:
            raise RoundError('未知局部草稿操作类型。','round_invalid',400)
        if kind=='asset_transform' and (len(edit_plan.get('operations',[]))!=1 or edit_plan['operations'][0].get('op')!='asset_transform'):
            raise RoundError('资产变换必须保留单次asset_transform身份。','round_invalid',400)
        if kind!='asset_transform' and any(row.get('op')=='asset_transform' for row in edit_plan.get('operations',[])):
            raise RoundError('资产变换不能冒充普通草稿身份。','round_invalid',400)
        with self.lock:
            scoped_run = self._get(identifier)
            if scoped_run.get('director_delegation'):
                delegated = self._director_host().binding(identifier,scoped_run['project_id'])
                self._director_host().child_admission(scoped_run)
                from .refinement_plan import RefinementError
                from .director_creation import check_operations
                if candidate_copy is not None:
                    from .creative_continuation import verify, copy_plan
                    lineage=delegated['brief'].get('creative_continuation')
                    parent=self._get(delegated['parent_id'])
                    if (not lineage or lineage.get('kind')!='pending_edit' or candidate_copy!=lineage['identity']
                            or kind!='draft_edit' or edit_plan!=copy_plan(lineage)):
                        raise RoundError('草稿复制身份无效。','round_invalid',409)
                    verify(self._director_host(),parent,lineage,child_id=identifier)
                    check_operations(self._director_host(),scoped_run,lineage['pending_round']['operations'],base_revision=base_revision)
                else:
                    check_operations(self._director_host(),scoped_run,edit_plan.get('operations',[]),base_revision=base_revision)
            elif candidate_copy is not None:
                raise RoundError('草稿复制缺少可信委派。','round_invalid',409)
            run = self._get(identifier)
            self._assert_asset_write(run.get("project_id"), run_id=identifier)
            store, document = self._local_round_state(run)
            limits = resolve_snapshot(run)
            if limits["schema_version"] == 2:
                blocker = edit_limit(budget_progress(run, store, round_document=document))
                if blocker:
                    raise RoundError(f"草稿编辑预算不足：{blocker}；已确认草稿仍可自检或提交。",
                                     "round_resource_exhausted", 409)
            elif store.local_edit_attempt_count(identifier) >= limits["max_edit_attempts"]:
                raise RoundError("本Run草稿编辑尝试已用完。", "round_resource_exhausted", 409)
            if document["selected_revision"] != base_revision or document["pending_revision"] is not None:
                raise RoundError("编辑基线不是当前已确认草稿。", "round_working_changed", 409)
            operation_id = uuid.uuid5(uuid.UUID(run["project_id"]),
                f"{identifier}:{document['id']}:{'draft' if kind=='draft_edit' else kind}:{base_revision}:{digest(edit_plan['operations'])}").hex
            identity = {"project_id": run["project_id"], "run_id": identifier,
                        "kind": kind, "round_id": document["id"],
                        "base_revision": base_revision, "operations_digest": digest(edit_plan["operations"]),
                        "source_selection": copy.deepcopy(document["start"]["selection"]),
                        "expected_project_revision": document["start"]["project_revision"]}
            if candidate_copy is not None:
                identity.update(execution_kind='candidate_copy',continuation_identity=candidate_copy)
            store.prepare_operation({"operation_id": operation_id, "identity": identity})
            operation = store.operation_state(operation_id)
            attempts = operation["attempts"]
            if attempts:
                last = attempts[-1]
                if last["status"] != "failed" or not last["retryable"]:
                    raise RoundError("同一草稿计划已执行或结果未知，不能换工具ID重放。",
                                     "round_operation_unknown", 409)
                retry = True
            else:
                retry = False
            attempt_id = uuid.uuid5(uuid.UUID(operation_id), f"attempt:{len(attempts)}").hex
            store.begin_attempt(operation_id, attempt_id, retry=retry,
                                local_limits=limits if limits["schema_version"] == 2 else None,
                                round_id=document["id"])
            step = {"id": attempt_id, "operation_id": operation_id, "attempt_id": attempt_id,
                    "kind":kind,
                    "retry_index": len(attempts), "revision_id": revision_id,
                    "base_revision": base_revision, "operations": copy.deepcopy(edit_plan["operations"]),
                    "reason": edit_plan["reason"], "status": "scheduled", "artifacts": [],
                    "assessment": None, "created_at": self._timestamp()}
            if candidate_copy is not None:step['execution_kind']='candidate_copy'
            document["steps"].append(step)
            updated = store.update_local_round(document, document["version"], status="active")
            return {"round_id": updated["id"], "round_version": updated["version"],
                    "operation_id": operation_id, "attempt_id": attempt_id}

    def finish_local_step(self, identifier, step_id, *, status, agent_state=None, detail=None):
        from .refinement_round import RoundError
        with self.lock:
            run = self._get(identifier)
            store = self._projects().state_store(run["project_id"])
            document = store.local_round(identifier)
            if document is None or document["status"] != "active":
                raise RoundError("局部轮已结束。", "round_not_resumable", 409)
            step = next((row for row in document["steps"] if row["id"] == step_id), None)
            if step is None or step["status"] != "scheduled":
                raise RoundError("草稿步骤身份或状态无效。", "round_operation_unknown", 409)
            if status not in {"pending", "failed", "unknown"}:
                raise RoundError("草稿结果无效。")
            step["status"] = status
            if detail:
                step["detail"] = str(detail)[:500]
            if status == "pending":
                step["artifacts"] = self._fixed_scene_artifacts(run, step["revision_id"])
                document["pending_revision"] = step["revision_id"]
            if agent_state is not None:
                document["agent_state"] = copy.deepcopy(agent_state)
            return store.update_local_round(document, document["version"], status="active")

    def review_local_step(self, identifier, step_id, assessment, *, agent_state):
        from .refinement_round import RoundError
        with self.lock:
            run = self._get(identifier)
            store, document = self._local_round_state(run)
            step = next((row for row in document["steps"] if row["id"] == step_id), None)
            if (step is None or step["status"] != "pending"
                    or document["pending_revision"] != step["revision_id"]):
                raise RoundError("没有匹配的待审草稿步骤。", "round_operation_unknown", 409)
            step["assessment"] = copy.deepcopy(assessment)
            if assessment["verdict"] == "better" and assessment["preservation"] == "pass":
                step["status"] = "selected"
                document["selected_revision"] = step["revision_id"]
            elif assessment["verdict"] == "uncertain" or assessment["preservation"] == "unknown":
                step["status"] = "needs_user_review"
            else:
                step["status"] = "rejected"
            document["pending_revision"] = None
            document["agent_state"] = copy.deepcopy(agent_state)
            return store.update_local_round(document, document["version"], status="active")

    def checkpoint_local_round(self, identifier, *, agent_state, active_seconds):
        from .refinement_round import RoundError
        with self.lock:
            run = self._get(identifier)
            store, document = self._local_round_state(run)
            if document["pending_revision"] or document["selected_revision"] == document["start"]["revision_id"]:
                raise RoundError("只有已确认且已修改的草稿可建立恢复点。", "round_not_resumable", 409)
            artifacts = self._fixed_scene_artifacts(run, document["selected_revision"])
            document["checkpoint"] = {"revision_id": document["selected_revision"],
                                      "artifacts": artifacts, "created_at": self._timestamp()}
            document["agent_state"] = copy.deepcopy(agent_state)
            document["active_seconds_used"] = max(0, active_seconds)
            run["local_round_active_seconds"] = max(run.get("local_round_active_seconds", 0),
                                                     document["active_seconds_used"])
            document["status"] = "paused"
            updated = store.update_local_round(document, document["version"], status="active")
            self._update(identifier, "awaiting_resume", "草稿恢复点已固定；等待用户显式续做同一Run。",
                         status="awaiting_resume", agent=copy.deepcopy(agent_state))
            return {"round_id": updated["id"], "version": updated["version"],
                    "revision_id": updated["selected_revision"], "resume_ready": True}

    def suspend_local_round(self, identifier, *, agent_state, active_seconds):
        """Keep a stopped draft and its exact history; only verified checkpoints resume."""
        from .runtime import AppError
        from .project_state_store import StateStoreError
        with self.lock:
            run = self._get(identifier)
            store = self._projects().state_store(run["project_id"])
            document = store.local_round(identifier)
            if document is None or document["status"] != "active":
                return document
            if any(step["status"] in {"scheduled", "unknown"} for step in document["steps"]):
                return document
            if (document["pending_revision"] is None
                    and document["selected_revision"] != document["start"]["revision_id"]
                    and (not document.get("checkpoint") or
                         document["checkpoint"]["revision_id"] != document["selected_revision"])):
                try:
                    artifacts = self._fixed_scene_artifacts(run, document["selected_revision"])
                except (AppError, StateStoreError, OSError, ValueError):
                    # A damaged draft is still endable, but must not claim resume readiness.
                    pass
                else:
                    document["checkpoint"] = {
                        "revision_id": document["selected_revision"],
                        "artifacts": artifacts, "created_at": self._timestamp()}
            document["agent_state"] = copy.deepcopy(agent_state)
            document["active_seconds_used"] = max(0, document.get("active_seconds_used", 0),
                                                  active_seconds)
            run["local_round_active_seconds"] = max(run.get("local_round_active_seconds", 0),
                                                     document["active_seconds_used"])
            document["status"] = "paused"
            updated = store.update_local_round(document, document["version"], status="active")
            self._save(run)
            return updated

    def end_local_round(self, identifier, body, *, from_builder=False):
        from .refinement_round import RoundError, validate_end
        with self.lock:
            run = self._get(identifier)
            store = self._projects().state_store(run["project_id"])
            if from_builder:
                document = store.local_round(identifier)
                if document is None:
                    raise RoundError("局部轮不存在。", "round_not_resumable", 409)
                body = {"end_id": uuid.uuid4().hex, "round_id": document["id"],
                        "expected_round_version": document["version"], "reason": body}
            request = validate_end(body)
            worker = self.workers.get(identifier)
            if not from_builder and worker and worker.is_alive():
                raise RoundError("Run仍在执行，等待停止后结束草稿。", "round_busy", 409)
            result = store.end_local_round(identifier, request)
            if self.asset_host and run.get("asset_request"):
                self.asset_host.close(self, identifier, reason='builder_end' if from_builder else 'user_end')
            if not from_builder and run["status"] not in {"interrupted", "cancelled", "failed"}:
                self._event(run, "interrupted", "未保存的局部草稿已明确结束；正式Working不变。")
                run["status"] = "interrupted"
                run["error"] = {"code": "round_ended", "message": request["reason"],
                                "outcome_unknown": False}
                self._save(run)
            if not from_builder:
                self._seal_run_handoff(identifier)
            return result

    def _project_request(self, request):
        from .runtime import AppError
        if not request.get("project_id"):
            return None, None
        project = self.get_project(request["project_id"])
        expected = request.get("expected_project_revision")
        if expected is not None and project["revision"] != expected:
            raise AppError("项目当前场景已变更，请刷新后再发送要求。", "project_changed", 409)
        self._project_environment(request)
        if request["operation"] != "edit":
            return project, None
        self._verify_project_baseline(request)
        version = self.scene_version(**request["base_scene"])
        if request["evaluation"]["mode"] == "reference" and not (version["run"].get("reference") or version["run"].get("available_reference") or request.get('project_scene_reference')):
            raise AppError("当前场景没有明确采用的参考图，请使用意图与前后效果比较。", "reference_missing", 409)
        return project, version

    def _verify_project_baseline(self, run):
        from .runtime import AppError
        if run.get("operation") != "edit":
            return
        # This guard consumes only SQL revision/head identity. Artifact bytes
        # are checked at scene use/commit; public availability is not used here.
        project = self._projects().get(run["project_id"], verify_artifacts=False)
        current = project.get("current_scene") or {}
        base = run.get("working_scene") or run["base_scene"]
        if (project["revision"] != run["expected_project_revision"]
                or current.get("run_id") != base["run_id"] or current.get("revision_id") != base["revision_id"]):
            raise AppError("项目当前场景已经改变，已停止旧基线上的后续步骤；已有候选保留。", "project_changed", 409)

    @staticmethod
    def _design_context(run):
        return copy.deepcopy(run.get("design_context") or {"prompt": run["prompt"], "constraints": run.get("constraints", {})})

    def _checked_artifact(self, run, artifact, *, directory=None):
        from .runtime import AppError
        directory = self.run_directory(run["id"]) if directory is None else directory
        path = (directory / artifact["path"]).resolve()
        if (not path.is_relative_to(directory) or not path.is_file()
                or file_sha256(path) != artifact["sha256"]):
            raise AppError("所选版本文件已变化或缺失，不能以旧截图继续修改。请重新导入当前文件。", "revision_changed", 409)
        return path

    def scene_version(self, run_id, revision_id=None, *, allow_active=False):
        from .runtime import AppError, TERMINAL
        run = self.get(run_id)
        if run["status"] not in TERMINAL and not allow_active:
            raise AppError("场景仍在执行中，请等待后选择完整版本。", "scene_busy", 409)
        if run.get("agent"):
            revision = next((item for item in run["agent"]["revisions"] if item["id"] == revision_id), None)
            if revision is None:
                raise AppError("请选择一个已完成的场景版本。", "revision_missing", 409)
        elif revision_id is not None:
            raise AppError("该任务没有这个场景版本。", "revision_missing", 409)
        artifacts = [item for item in run["artifacts"] if item.get("revision_id") == revision_id]
        required = {}
        # One version validation shares a freshly verified directory. Recheck
        # it after all reads; do not cache roots or authoritative state across calls.
        directory = self.run_directory(run_id)
        for kind in ("blend", "plan", "inspection"):
            artifact = next((item for item in artifacts if item["kind"] == kind), None)
            if artifact is None:
                raise AppError("所选版本还没有完整的场景、计划和检查产物。", "incomplete_scene", 409)
            required[kind] = self._checked_artifact(run, artifact, directory=directory)
        renders = [item for item in artifacts if item["kind"] == "render" and (item.get("view_id") in ("main", "auxiliary") or item.get("view_id") is None)]
        if len(renders) < 2:
            raise AppError("所选版本尚未完成比较截图。", "incomplete_scene", 409)
        for artifact in renders:
            self._checked_artifact(run, artifact, directory=directory)
        plan = json.loads(required["plan"].read_text(encoding="utf-8"))
        workspace = required["blend"].parent
        workspace_plan = (workspace / "plan.json").resolve()
        if not workspace_plan.is_relative_to(workspace) or not workspace_plan.is_file() or json.loads(workspace_plan.read_text(encoding="utf-8")) != plan:
            raise AppError("场景内部计划与登记计划不一致，请重新导入。", "revision_changed", 409)
        if self.run_directory(run_id) != directory:
            raise AppError("任务目录在版本校验期间发生变化。", "unsafe_run_path", 409)
        return {"run": run, "workspace": workspace, "plan": plan, "files": required,
                "revision_id": revision_id}

    @staticmethod
    def _selection_for(run, revision_id, plan):
        return {"run_id": run["id"], "revision_id": revision_id, "scene_id": run["scene_id"],
                "title": (plan.get("title") or plan.get("scene_name") or run["prompt"])[:500]}

    def _begin_builder_batch(self, run):
        """Entry point for Builder runs; batches open only at accepted commits.

        A new request, pure discussion, read-only check or failed attempt must
        never publish or replace the recovery point. The active batch (if any)
        is kept untouched here; the first successfully accepted modification
        publishes a fresh batch whose Before is the real pre-edit version.
        """
        return self.get_project(run["project_id"])

    @staticmethod
    def _timestamp():
        from .runtime import now
        return now()

    def _scene_operation(self, identifier, revision_id, kind, *, prepare=True):
        """Prepare a stable operation before the external scene write."""
        run = self.get(identifier)
        if not run.get("project_id"):
            return None
        operation_id = uuid.uuid5(uuid.UUID(run["project_id"]),
                                  f"{identifier}:{kind}:{revision_id}").hex
        state = self._projects().state_store(run["project_id"])
        existing = state.operation_state(operation_id)
        if existing is not None:
            return {"operation_id": operation_id, "identity": existing["identity"]}
        if not prepare:
            from .runtime import AppError
            raise AppError("场景操作未在外部写入前准备。", "operation_missing", 409)
        project = self._projects().get(run["project_id"], verify_artifacts=False)
        goal = {"prompt": run.get("prompt"), "constraints": run.get("constraints"),
                "evaluation": run.get("evaluation"), "reference": run.get("reference"),
                "base_scene": run.get("base_scene"), "operation": run.get("operation")}
        native_source = (run.get("import_source") or {}).get("native_candidate")
        if native_source is not None:
            goal["native_candidate"] = copy.deepcopy(native_source)
        goal_digest = hashlib.sha256(json.dumps(
            goal, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        identity = {"project_id": run["project_id"], "run_id": identifier, "kind": kind,
                    "source_selection": copy.deepcopy(project.get("current_scene")),
                    "expected_project_revision": run.get("expected_project_revision"),
                    "expected_working_version": project["workspace"]["working_version"],
                    "goal_identity": {"sha256": goal_digest, "source": "run_request_snapshot"}}
        if native_source is not None:
            identity["source_candidate"] = copy.deepcopy(native_source)
        state.prepare_operation({"operation_id": operation_id, "identity": identity})
        return {"operation_id": operation_id, "identity": identity}

    def _fixed_scene_artifacts(self, run, revision_id):
        """Hash the published primary files before entering the metadata transaction."""
        from .runtime import AppError
        project = self._projects().get(run["project_id"], verify_artifacts=False)
        root = Path(project["path"])
        base = self.run_directory(run["id"])
        selected = []
        for kind in ("blend", "plan", "inspection"):
            candidates = [item for item in run.get("artifacts", [])
                          if item.get("revision_id") == revision_id and item.get("kind") == kind]
            if not candidates:
                raise AppError("提交候选缺少必需产物。", "incomplete_scene", 409)
            selected.append(candidates[-1])
        for view in ("main", "auxiliary"):
            candidates = [item for item in run.get("artifacts", [])
                          if item.get("revision_id") == revision_id and item.get("kind") == "render"
                          and (item.get("view_id") == view or
                               (revision_id is None and Path(item.get("path", "")).name == view + ".png"))]
            if not candidates:
                raise AppError("提交候选缺少主辅截图。", "incomplete_scene", 409)
            selected.append(candidates[-1])
        result = []
        for item in selected:
            source = self._checked_artifact(run, item)
            relative = source.relative_to(root).as_posix()
            result.append({"kind": item["kind"], "view_id": item.get("view_id"),
                           "path": relative, "size": source.stat().st_size,
                           "sha256": file_sha256(source)})
        return result

    def commit_agent_revision(self, identifier, revision_id, edit_reason, operations, *, local_round_id=None):
        """Atomically publish Builder head, batch, issue, counters and receipt."""
        from .runtime import AppError
        run = self.get(identifier)
        self._assert_asset_write(run.get("project_id"), run_id=identifier)
        if run.get("operation") != "edit":
            return None
        project_id = run["project_id"]
        prepared = self._scene_operation(identifier,
            "local-round-" + local_round_id if local_round_id else revision_id,
            "builder", prepare=False)
        state = self._projects().state_store(project_id)
        prior = state.lookup_receipt(prepared["operation_id"])
        if prior["status"] == "committed":
            receipt = prior["receipt"]
            earlier = receipt["run_commit"]
            if (earlier["revision_id"] != revision_id or earlier["reason"] != edit_reason
                    or earlier["operations"] != operations):
                raise AppError("相同操作 ID 不能提交另一份编辑内容。", "operation_conflict", 409)
            return {"workspace": {"working_version": receipt["working_version"]},
                    "revision": receipt["project_revision"]}
        if run.get('director_delegation'):
            self._director_host().child_admission(run)
        local_meta = None
        if local_round_id is not None:
            from .refinement_round import RoundError
            store, document = self._local_round_state(run)
            if (document["id"] != local_round_id or document["selected_revision"] != revision_id
                    or document["pending_revision"] is not None):
                raise RoundError("轮末草稿身份不一致。", "round_working_changed", 409)
            selected_steps = [step for step in document["steps"] if step["status"] == "selected"]
            if not selected_steps:
                raise RoundError("空改不能正式提交。", "round_not_resumable", 409)
            local_meta = {"id": document["id"], "version": document["version"],
                          "goal_id": document["goal_id"],
                          "criteria_digest": document["criteria_digest"],
                          "step_ids": [step["id"] for step in selected_steps],
                          "judge_trigger": run["judge"]["trigger_rounds"]}
        version = self.scene_version(identifier, revision_id, allow_active=True)
        if local_meta is not None:
            from .refinement_round import RoundError, scene_semantics
            initial = self.scene_version(identifier, document["start"]["revision_id"], allow_active=True)
            before_inspection = json.loads(initial["files"]["inspection"].read_text(encoding="utf-8"))
            after_inspection = json.loads(version["files"]["inspection"].read_text(encoding="utf-8"))
            if scene_semantics(initial["plan"], before_inspection) == scene_semantics(
                    version["plan"], after_inspection):
                raise RoundError("整轮最终场景与起点没有可确认的净变化，不能正式保存。",
                                 "round_no_change", 409)
        project = self._projects().get(project_id, verify_artifacts=False)
        identity = prepared["identity"]
        if (project["revision"] != identity["expected_project_revision"]
                or project.get("current_scene") != identity["source_selection"]):
            raise AppError("提交修改时工作版已经改变；候选保留，但不会覆盖较新的版本。", "project_changed", 409)
        selection = self._selection_for(run, revision_id, version["plan"])
        artifacts = self._fixed_scene_artifacts(run, revision_id)
        command = {"operation_id": prepared["operation_id"], "identity": identity,
                   "project_id": project_id, "run_id": identifier, "kind": "builder",
                   "source_selection": identity["source_selection"],
                   "expected_project_revision": identity["expected_project_revision"],
                   "expected_working_version": identity["expected_working_version"],
                   "goal_identity": identity["goal_identity"],
                   "target_selection": selection, "legacy_revision_id": revision_id,
                   "artifacts": artifacts, "reason": edit_reason,
                   "operations": copy.deepcopy(operations)}
        if local_meta is not None:
            command["local_round"] = copy.deepcopy(local_meta)
        receipt = {"operation_id": prepared["operation_id"], "project_id": project_id,
                   "run_id": identifier, "kind": "builder",
                   "global_revision_id": state.global_revision_id(project_id, identifier, revision_id),
                   "legacy_revision_id": revision_id, "artifacts": artifacts,
                   "artifact_digest": hashlib.sha256(json.dumps(
                       artifacts, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                   "run_commit": {"revision_id": revision_id, "reason": edit_reason,
                                  "operations": copy.deepcopy(operations),
                                  "operation_id": prepared["operation_id"]}}
        if local_meta is not None:
            receipt["run_commit"]["local_round_id"] = local_round_id
            receipt["run_commit"]["step_ids"] = list(local_meta["step_ids"])
        previous = copy.deepcopy(project["current_scene"])
        previous_working = project["workspace"]["working_version"]

        def mutate(value):
            batch = value.get("batch_start")
            if batch and batch.get("status") == "active":
                batch["edit_rounds"] = int(batch.get("edit_rounds", 0)) + 1
            else:
                value["batch_start"] = {"id": uuid.uuid4().hex, "selection": previous,
                                        "working_version": previous_working,
                                        "created_at": self._timestamp(),
                                        "status": "active", "edit_rounds": 1, "last_judged_round": 0}
            object_ids = []
            for operation in operations:
                identifier_value = operation.get("id") or operation.get("object", {}).get("id")
                if isinstance(identifier_value, str) and identifier_value not in object_ids:
                    object_ids.append(identifier_value)
            issue_id = "builder-" + identifier[:8] + "-" + revision_id
            value["issues"] = [row for row in value["issues"] if row.get("id") != issue_id]
            value["issues"].append({"id": issue_id, "title": edit_reason, "object_ids": object_ids,
                                    "source": "builder", "priority": "normal",
                                    "status": "modified_pending_verification",
                                    "updated_at": self._timestamp(), "working_version": value["working_version"]})
            return value

        saved, committed = self._projects().commit_builder_scene(
            project_id, command, receipt, mutate)
        if self.asset_host and run.get("asset_request"):
            self.asset_host.close(self, identifier)
        with self.lock:
            stored = self._get(identifier)
            stored["expected_project_revision"] = saved["project_revision"]
            stored["working_scene"] = copy.deepcopy(saved["target_selection"])
            commit = copy.deepcopy(saved["run_commit"])
            commit.update(working_version=saved["working_version"],
                          project_revision=saved["project_revision"],
                          time=saved["committed_at"])
            if not any(row.get("operation_id") == prepared["operation_id"]
                       for row in stored.setdefault("commits", [])):
                stored["commits"].append(commit)
            self._save(stored)
        return committed

    def bootstrap_initial_readiness(self, identifier, revision_id, *, agent_state=None):
        """Read-only preflight; missing self-check is distinct from corrupt facts.

        The publication transaction still rechecks this at its original gate.
        A caller may retain an unqualified candidate, never publish it.
        """
        from .runtime import AppError
        from .providers import ProviderError
        run = self.get(identifier)
        state = agent_state if agent_state is not None else run.get('agent') or {}
        project = self._projects().get(run['project_id'], verify_artifacts=False)
        store = self._projects().state_store(run['project_id'])
        result = {'ready': False, 'candidate_revision': revision_id, 'working_published': False,
                  'required_tool': 'review_initial', 'required_evidence': []}
        if revision_id is not None:
            initial = self._scene_operation(identifier, revision_id, 'initial', prepare=False)
            if store.lookup_receipt(initial['operation_id'])['status'] == 'committed':
                return {**result, 'ready': True, 'working_published': True, 'reason': 'already_published'}
        if project.get('current_scene') is not None:
            raise AppError('初版发布基线已被其它Working占用。', 'bootstrap_initial_conflict', 409)
        self._verify_project_baseline(run)
        if run.get('director_ledger_unknown'):
            raise ProviderError('builder_ledger_unknown', '调用费用账本未核实。', outcome_unknown=True)
        with store._connection() as c:
            unknown = (c.execute("SELECT 1 FROM operations WHERE run_id=? AND state='unknown'", (identifier,)).fetchone()
                or c.execute("SELECT 1 FROM edit_attempts a JOIN operations o ON o.operation_id=a.operation_id WHERE o.run_id=? AND a.status IN ('scheduled','unknown')", (identifier,)).fetchone())
        if unknown or state.get('uncertain_write'):
            raise ProviderError('write_result_unknown', '原候选写入状态未核实，不能继续收尾。', outcome_unknown=True)
        artifacts = None
        for candidate in {revision_id, state.get('pending_revision')} - {None}:
            fixed = self._fixed_scene_artifacts(run, candidate)
            if candidate == revision_id: artifacts = fixed
        if revision_id is None: return {**result, 'reason': 'no_candidate', 'required_tool': 'create_scene'}
        result['required_evidence'] = [o['id'] for o in state.get('observations', [])
            if o['revision_id'] == revision_id and o.get('comparison') and o['view_id'] in {'main','auxiliary'}]
        if state.get('pending_revision'):
            return {**result, 'reason': 'pending_candidate', 'required_tool': 'review_candidate',
                    'pending_revision': state['pending_revision']}
        checked = state.get('initial_self_check') or {}
        if not checked: return {**result, 'reason': 'missing_self_check'}
        if checked.get('revision_id') != revision_id: return {**result, 'reason': 'stale_self_check'}
        if checked.get('artifacts') != artifacts:
            raise AppError('初版自检的产物身份与当前候选不一致。', 'bootstrap_self_check_evidence_changed', 409)
        if checked.get('verdict') != 'pass':
            return {**result, 'reason': 'self_check_not_passed', 'self_check_verdict': checked.get('verdict')}
        return {**result, 'ready': True, 'reason': 'self_check_passed'}

    def commit_initial_working_scene(self, identifier, revision_id):
        """Publish first complete scene through the same receipt transaction."""
        run = self.get(identifier)
        if not run.get("project_id"):
            return None
        project = self._projects().get(run["project_id"], verify_artifacts=False)
        if run.get('bootstrap'):
            from .runtime import AppError
            readiness = self.bootstrap_initial_readiness(identifier, revision_id)
            if readiness['working_published']: return project
            if not readiness['ready']:
                raise AppError('初版需要绑定当前主辅证据的明确通过自检。','bootstrap_self_check_required',409)
        if project.get("current_scene") is not None:
            return project
        state = self._projects().state_store(run["project_id"])
        prepared = self._scene_operation(identifier, revision_id, "initial", prepare=False)
        prior = state.lookup_receipt(prepared["operation_id"])
        if prior["status"] == "committed":
            return self._projects().get(run["project_id"], verify_artifacts=False)
        version = self.scene_version(identifier, revision_id, allow_active=True)
        selection = self._selection_for(run, revision_id, version["plan"])
        expected = prepared["identity"]["expected_project_revision"]
        if expected != project["revision"]:
            return project
        artifacts = self._fixed_scene_artifacts(run, revision_id)
        committed = self._projects().set_current(
            run["project_id"], selection, expected,
            reason=f"initial:{identifier}:{revision_id}", preserve_previous=False,
            operation_id=prepared["operation_id"], identity=prepared["identity"],
            kind="initial", artifacts=artifacts)
        with self.lock:
            stored = self._get(identifier)
            stored["expected_project_revision"] = committed["revision"]
            stored["working_scene"] = copy.deepcopy(selection)
            stored["initial_working_commit"] = {
                "working_version": committed["workspace"]["working_version"],
                "time": self._timestamp(), "operation_id": prepared["operation_id"]}
            self._save(stored)
        return committed

    def record_conversation_turn(self, identifier, *, kind, summary, status, evidence=None):
        run = self.get(identifier)
        if not run.get("project_id"):
            return None
        if run.get('director_delegation') and self._director_host().binding(identifier,run['project_id']):
            return None
        project = self.get_project(run["project_id"])
        binding = self._projects().state_store(run["project_id"]).refinement_state(identifier)["binding"]
        readonly=run.get('operation')=='conversation' and self._director_host().is_readonly(run)
        peer_decision={}
        if readonly:
            store=self._director_host().store(run['project_id']);session=store.session(identifier)
            claim=store.decision_for(session['event_id']) or {}
            peer_decision=session.get('decision') or {'as_of':claim.get('input',{}).get('readonly_snapshot',{}).get('as_of',
                session['run'].get('readonly_accepted'))}
        def mutate(value):
            # Bound execution goals never change when a conversation finishes.
            # The pre-REFPLAN transcript API retains its historical projection;
            # it cannot alter the independent SQL goal or impersonate a binding.
            if binding is None and run.get('operation') != 'conversation':
                goal = value.get("goal") or {"scene_intent": "", "history": []}
                old_focus = goal.get("current_request", "")
                if not goal.get("scene_intent"):
                    goal["scene_intent"] = (run.get("design_context") or {}).get("prompt") or run["prompt"]
                goal.update(current_request=run["prompt"], constraints=copy.deepcopy(run.get("constraints", {})),
                            updated_at=self._timestamp(), authority="legacy_conversation_projection")
                if old_focus and old_focus != run["prompt"]:
                    goal.setdefault("history", []).append({"request": old_focus, "superseded_by": identifier,
                                                            "time": self._timestamp()})
                    goal["history"] = goal["history"][-40:]
                value["goal"] = goal
            turn = {"id": identifier, "sequence": len(value["turns"]) + 1, "created_at": run["created_at"],
                    "user": run["prompt"], "assistant": summary, "kind": kind, "status": status,
                    "working_version": value["working_version"],
                    "object_ids": sorted({item for commit in run.get("commits", [])
                                           for operation in commit.get("operations", [])
                                           for item in [operation.get("id") or operation.get("object", {}).get("id")]
                                           if isinstance(item, str)}),
                    "issue_ids": [row.get("id") for row in value["issues"]
                                  if row.get("working_version") == value["working_version"]],
                    "evidence": copy.deepcopy(evidence or [])}
            if readonly:
                old=next((row for row in value['turns'] if row.get('id')==identifier),None)
                as_of=copy.deepcopy(peer_decision.get('as_of'))
                turn.update(conversation_mode='read_only',as_of=as_of,issue_ids=[],
                    working_version=(as_of or {}).get('working',{}).get('working_version',value['working_version']))
                if old:turn['sequence']=old['sequence']
            value["turns"] = [row for row in value["turns"] if row.get("id") != identifier] + [turn]
            if len(value["turns"]) > 40:
                removed = value["turns"][:-40]
                lines = [value["rolling_summary"].get("text", "")]
                lines += [f"[{row.get('sequence')}] 用户：{row.get('user','')[:300]}；结果：{row.get('assistant','')[:500]}"
                          for row in removed]
                value["rolling_summary"] = {
                    "text": "\n".join(line for line in lines if line)[-8000:],
                    "through_turn": removed[-1].get("sequence", 0),
                    "sources": (value["rolling_summary"].get("sources", []) +
                                [row.get("id") for row in removed])[-120:],
                }
                value["turns"] = value["turns"][-40:]
            return value
        return self._projects().update_workspace(run["project_id"], project["revision"], mutate)

    @staticmethod
    def _digest(value):
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _validate_evaluation_input(self, project_id, input_id, *,
                                   run_id=None, judgement=None):
        """One host validator for registration and cache, including identity."""
        from .evaluation_input import (
            EvaluationInputError, validate_evaluation_inputs, validate_manifest)
        if not isinstance(input_id, str) or not input_id:
            return [{"input_id": input_id, "reason": "input_missing"}]
        state = self._projects().state_store(project_id)
        record = state.lookup_evaluation_input(input_id)
        if record is None:
            return [{"input_id": input_id, "reason": "input_not_registered"}]
        manifest = record["manifest"]
        if record["state"] != "sealed" or record["seal_state"] != "completed":
            return [{"input_id": input_id, "reason": "input_not_sealed"}]
        try:
            validate_manifest(manifest, require_sent=True)
        except EvaluationInputError as error:
            return [{"input_id": input_id, "reason": error.code}]
        if (manifest.get("input_digest") != record["input_digest"]
                or manifest.get("project_id") != project_id
                or run_id is not None and manifest.get("run_id") != run_id):
            return [{"input_id": input_id, "reason": "input_identity_mismatch"}]
        if judgement is not None and (
                judgement.get("evaluation_id") != manifest.get("evaluation_id")
                or judgement.get("input_id") != input_id
                or judgement.get("input_digest") != record["input_digest"]
                or judgement.get("target") != (manifest.get("context") or {}).get("target")):
            return [{"input_id": input_id, "reason": "input_identity_mismatch"}]
        root = Path(self._projects().get(project_id, verify_artifacts=False)["path"])
        if judgement is not None:
            from .refinement_evaluation import validate_reviews
            context = manifest.get("context") or {}
            # Validate again at registration, independently of the Provider parser.
            evidence = set()
            for row in manifest["items"]:
                if row.get("sent_requests") and row.get("evidence_id"):
                    evidence.add(row["evidence_id"])
            try:
                validate_reviews(judgement, evidence, context.get("refinement_standards"),
                                 context.get("pending_issues", []))
            except (ValueError, TypeError, KeyError):
                return [{"input_id": input_id, "reason": "invalid_reviews"}]
        return validate_evaluation_inputs(
            [item for item in manifest["items"] if item.get("sent_requests")],
            project_root=root)

    @staticmethod
    def _selection_identity(selection):
        """Return (run_id, revision_id) or None; revision_id must exist (null is legal)."""
        if not isinstance(selection, dict) or "run_id" not in selection or "revision_id" not in selection:
            return None
        run_id, revision_id = selection.get("run_id"), selection.get("revision_id")
        if not isinstance(run_id, str) or not re.fullmatch(r"[0-9a-f]{32}", run_id):
            return None
        if revision_id is not None and (not isinstance(revision_id, str)
                                        or not re.fullmatch(r"r[0-9]{3,6}", revision_id)):
            return None
        return (run_id, revision_id)

    @staticmethod
    def _digest_valid(value):
        return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))

    def _snapshot_selection_files(self, selection):
        """Capture blend/inspection/render hashes; None means missing or changed."""
        from .runtime import AppError
        if not isinstance(selection, dict):
            return None
        try:
            version = self.scene_version(selection["run_id"], selection.get("revision_id"),
                                         allow_active=True)
        except (AppError, KeyError, TypeError):
            return None
        try:
            directory = self.run_directory(selection["run_id"])
            snapshot = {"blend": file_sha256(version["files"]["blend"]),
                        "inspection": file_sha256(version["files"]["inspection"]),
                        "renders": {}, "views": {}}
            for artifact in version["run"]["artifacts"]:
                if artifact.get("kind") != "render":
                    continue
                if artifact.get("revision_id") != version["revision_id"]:
                    continue
                view = artifact.get("view_id")
                if view not in ("main", "auxiliary") and artifact.get("observation_id") is None:
                    continue
                # Artifact membership already comes from scene_version's Run
                # snapshot. Rehash every render without re-projecting that Run.
                path = (directory / artifact["path"]).resolve()
                if not path.is_relative_to(directory) or not path.is_file():
                    return None
                digest = file_sha256(path)
                snapshot["renders"][artifact["id"]] = digest
                if view in ("main", "auxiliary"):
                    # The required fixed comparison views are part of the frozen
                    # identity: a version without them cannot satisfy acceptance.
                    snapshot["views"][view] = digest
            if self.run_directory(selection["run_id"]) != directory:
                return None
            return snapshot
        except (AppError, OSError, KeyError):
            return None

    @staticmethod
    def _snapshots_match(captured, current):
        if not isinstance(captured, dict) or not isinstance(current, dict):
            return False
        if captured.get("blend") is None or captured.get("inspection") is None:
            return False
        if captured["blend"] != current.get("blend") or captured["inspection"] != current.get("inspection"):
            return False
        if captured.get("views") != current.get("views"):
            return False
        old, new = captured.get("renders") or {}, current.get("renders") or {}
        return all(key in new and new[key] == digest for key, digest in old.items())

    def _judgement_target_incomplete(self, target):
        """Return a reason when the frozen target is not complete and well-typed."""
        if not isinstance(target, dict) or set(target) != JUDGEMENT_TARGET_FIELDS:
            return "target_incomplete"
        if (type(target.get("schema_version")) is not int or target["schema_version"] != 1
                or not isinstance(target.get("project_id"), str)):
            return "target_incomplete"
        if target.get("kind") not in JUDGEMENT_KINDS:
            return "target_incomplete"
        if self._selection_identity(target.get("current")) is None:
            return "target_incomplete"
        if target["kind"] == "working":
            if self._selection_identity(target.get("before")) is None:
                return "target_incomplete"
            if type(target.get("before_working_version")) is not int or target["before_working_version"] < 0:
                return "target_incomplete"
            if type(target.get("working_version")) is not int or target["working_version"] < 0:
                return "target_incomplete"
            batch = target.get("batch")
            if (not isinstance(batch, dict) or set(batch) != JUDGEMENT_BATCH_FIELDS
                    or not isinstance(batch.get("id"), str) or not batch["id"]
                    or not isinstance(batch.get("status"), str) or not batch["status"]
                    or type(batch.get("edit_rounds")) is not int or batch["edit_rounds"] < 0
                    or type(batch.get("last_judged_round")) is not int or batch["last_judged_round"] < 0):
                return "target_incomplete"
        else:
            # Candidate and explicit open acceptances carry an explicit
            # no-batch identity; a candidate may bind a real candidate start as
            # Before, while an open acceptance must have none. Working-version
            # fields are always null for both (never a fabricated one).
            if target.get("batch") is not None:
                return "target_incomplete"
            if target.get("before_working_version") is not None:
                return "target_incomplete"
            before = target.get("before")
            if before is not None and self._selection_identity(before) is None:
                return "target_incomplete"
            if target["kind"] == "candidate":
                if target.get("working_version") is not None:
                    return "target_incomplete"
            else:
                if before is not None:
                    return "target_incomplete"
                if type(target.get("working_version")) is not int or target["working_version"] < 0:
                    return "target_incomplete"
        if not isinstance(target.get("stage"), str) or not target["stage"]:
            return "target_incomplete"
        if not isinstance(target.get("evaluation"), str) or target["evaluation"] not in {"intent", "reference"}:
            return "target_incomplete"
        for field in ("reference_digest", "target_digest"):
            if target.get(field) is not None and not self._digest_valid(target[field]):
                return "target_incomplete"
        if not isinstance(target.get("judge_config"), dict):
            return "target_incomplete"
        if not self._digest_valid(target.get("goal_digest")) or not self._digest_valid(target.get("issues_digest")):
            return "target_incomplete"
        if not isinstance(target.get("scope"), dict):
            return "target_incomplete"
        if not self._file_snapshot_shape(target.get("current_files")):
            return "target_incomplete"
        if target.get("before") is None:
            if target.get("before_files") is not None or target.get("before_working_version") is not None:
                return "target_incomplete"
        elif not self._file_snapshot_shape(target.get("before_files")):
            return "target_incomplete"
        return None

    @staticmethod
    def _file_snapshot_shape(value):
        if not (isinstance(value, dict) and ProjectRunMixin._digest_valid(value.get("blend"))
                and ProjectRunMixin._digest_valid(value.get("inspection"))
                and isinstance(value.get("renders"), dict)
                and len(value["renders"]) >= 2
                and all(isinstance(digest, str) and ProjectRunMixin._digest_valid(digest)
                        for digest in value["renders"].values())):
            return False
        # The fixed main/auxiliary views are required evidence, not optional
        # extras: an empty or wrong-shaped snapshot must never pass registration.
        views = value.get("views")
        return (isinstance(views, dict) and {"main", "auxiliary"}.issubset(views)
                and all(isinstance(digest, str) and ProjectRunMixin._digest_valid(digest)
                        for digest in views.values()))

    def _judgement_applicable(self, project, target, *, expected, run=None):
        """Return None when the frozen judgement target still describes this project.

        A stale reason string otherwise. Metadata-only changes do not bump the
        project revision, so applicability is checked against the complete
        evaluated identity, the Before resolved from the current batch and the
        bound Stage/evaluation/reference/scope/issues/goal values. When the
        adopted reference is part of the evaluation, its real bytes are
        re-verified too: identical metadata never implies identical evidence.
        """
        incomplete = self._judgement_target_incomplete(target)
        if incomplete is not None:
            return incomplete
        if target.get("kind") not in expected.get("kinds", ("working",)):
            return "kind_not_working"
        if target.get("scope") != expected.get("scope"):
            return "scope_changed"
        if target.get("goal_digest") != expected.get("goal_digest"):
            return "goal_changed"
        if run is not None and run.get("goal_snapshot"):
            state = self._projects().state_store(run["project_id"]).refinement_state(run["id"])
            if not state["goal"] or state["goal"]["id"] != run["goal_snapshot"]["id"]:
                return "goal_changed"
        if expected.get("review_result") is not None:
            from .refinement_evaluation import criteria_reason
            result = expected["review_result"]
            reason = criteria_reason(expected.get("refinement_standards"),
                self._refinement_standards(project["id"]),
                item_ids=[r["item_id"] for r in result.get("item_results", [])],
                issue_ids=[r["issue_id"] for r in result.get("issue_reviews", [])])
            if reason:
                return reason
        if target.get("evaluation") != expected.get("evaluation"):
            return "evaluation_changed"
        if target.get("reference_digest") != expected.get("reference_digest"):
            return "reference_changed"
        if target.get("target_digest") != expected.get("target_digest"):
            return "reference_changed"
        if (run is not None and target.get("evaluation") == "reference"
                and self._digest_valid(target.get("target_digest"))
                and not self._reference_bytes_match(run, target["target_digest"])):
            # The reference bytes were mutated or removed while the model was
            # working: the old pass must not be applied to a different target.
            return "reference_changed"
        if target.get("judge_config") != expected.get("judge_config"):
            return "judge_config_changed"
        if target.get("project_id") != project.get("id"):
            return "project_changed"
        workspace = project.get("workspace") or {}
        kind = target.get("kind")
        if kind == "candidate":
            if (workspace.get("stage") or "") != target["stage"]:
                return "stage_changed"
            if not self._snapshots_match(target.get("current_files"),
                                         self._snapshot_selection_files(target.get("current"))):
                return "files_changed"
            if target.get("before") is not None and not self._snapshots_match(
                    target.get("before_files"), self._snapshot_selection_files(target.get("before"))):
                return "files_changed"
            # A candidate consumes the project issues as pending evidence, so
            # the same frozen source must be verified before applying.
            if self._digest(workspace.get("issues", [])) != target["issues_digest"]:
                return "issues_changed"
            return None
        if self._selection_identity(project.get("current_scene")) != self._selection_identity(target["current"]):
            return "current_scene_changed"
        if workspace.get("working_version") != target["working_version"]:
            return "working_version_changed"
        if (workspace.get("stage") or "") != target["stage"]:
            return "stage_changed"
        if not self._snapshots_match(target.get("current_files"),
                                     self._snapshot_selection_files(target.get("current"))):
            return "files_changed"
        batch = workspace.get("batch_start") or {}
        expected_batch = target["batch"]
        if kind == "open" or expected_batch is None:
            if batch:
                return "batch_changed"
        else:
            if batch.get("id") != expected_batch["id"]:
                return "batch_changed"
            if batch.get("status") != expected_batch["status"]:
                return "batch_changed"
            if batch.get("edit_rounds") != expected_batch["edit_rounds"]:
                return "batch_changed"
            if batch.get("last_judged_round") != expected_batch["last_judged_round"]:
                return "batch_changed"
        batch_selection = batch.get("selection")
        if target.get("before") is not None or kind == "working":
            before_now = batch_selection if isinstance(batch_selection, dict) else project.get("current_scene")
            before_working_now = (batch.get("working_version") if isinstance(batch_selection, dict)
                                  else workspace.get("working_version"))
            if self._selection_identity(before_now) != self._selection_identity(target["before"]):
                return "before_changed"
            if before_working_now != target["before_working_version"]:
                return "before_changed"
            if not self._snapshots_match(target.get("before_files"),
                                         self._snapshot_selection_files(target.get("before"))):
                return "files_changed"
        if self._digest(workspace.get("issues", [])) != target["issues_digest"]:
            return "issues_changed"
        return None

    def _expected_goal_digest(self, run):
        return self._digest(self.common_goal(run))

    def _registration_conflict(self, judgement, target):
        """Pure verdict/record identity checks performed before any project write."""
        if not isinstance(judgement, dict) or not judgement_pair_is_consistent(
                judgement.get("verdict"), judgement.get("stage_passed")):
            return "invalid_verdict_pair"
        if not isinstance(target, dict):
            return "target_missing"
        if self._judgement_target_incomplete(target) is not None:
            # Malformed targets are reported by the applicability check, not
            # mistaken for identity conflicts here.
            return None
        target_current = self._selection_identity(target.get("current"))
        if target_current is not None and self._selection_identity(judgement.get("selection")) != target_current:
            return "selection_conflict"
        batch = target.get("batch")
        if isinstance(batch, dict) and isinstance(batch.get("id"), str) and batch["id"]:
            if judgement.get("batch_id") != batch["id"]:
                return "batch_id_conflict"
        elif target.get("kind") == "working":
            return "batch_id_conflict"
        elif judgement.get("batch_id") is not None:
            return "batch_id_conflict"
        judgement_version = judgement.get("working_version")
        if target.get("kind") == "candidate":
            if judgement_version is not None:
                return "working_version_conflict"
        else:
            if type(judgement_version) is not int or judgement_version < 0:
                return "working_version_conflict"
            if type(target.get("working_version")) is int and judgement_version != target["working_version"]:
                return "working_version_conflict"
        return None

    def _judgement_expected(self, run, scope):
        evaluation = (run.get("evaluation") or {}).get("mode", "reference")
        reference = run.get("reference") if evaluation == "reference" else None
        digest = (reference or {}).get("sha256")
        return {"goal_digest": self._expected_goal_digest(run), "scope": scope,
                "evaluation": evaluation, "reference_digest": digest, "target_digest": digest,
                "judge_config": judge_config_identity(run)}

    def _record_judgement(self, identifier, judgement, *, expected_scope=None,
                          evaluation_receipt=None, input_id=None):
        """Conditionally apply a judgement to the exact project state it evaluated.

        Working judgements update batches, water levels and issues; explicit
        open acceptances update the latest record and issues without touching
        batches; candidate judgements are recorded in-run only and never alter
        workspace batches, issues or the current pointer.

        When ``evaluation_receipt`` is provided, the idempotent receipt check
        and the workspace/accepted_judges/receipt write share one SQL
        transaction.  Input integrity is validated before any workspace change.
        """
        from .evaluation_input import result_digest as _result_digest
        run = self.get(identifier)
        target = judgement.get("target") if isinstance(judgement, dict) else None
        record = copy.deepcopy(judgement) if isinstance(judgement, dict) else {}
        kind = target.get("kind", "working") if isinstance(target, dict) else "working"
        state_store = self._projects().state_store(run["project_id"])
        evaluation_id = record.get("evaluation_id")
        input_record = state_store.lookup_evaluation_input(input_id) if input_id else None
        frozen_standards = (((input_record or {}).get("manifest") or {}).get("context") or {}).get("refinement_standards")
        if isinstance(evaluation_id, str) and evaluation_id:
            original = state_store.lookup_evaluation_receipt(evaluation_id)
            if original is not None:
                if (original.get("run_id") != identifier
                        or original.get("project_id") != run["project_id"]
                        or original.get("input_id") != input_id
                        or original.get("input_digest") != record.get("input_digest")
                        or original.get("result_digest") != _result_digest(judgement)
                        or evaluation_receipt is not None and (
                            evaluation_receipt.get("evaluation_id") != evaluation_id
                            or evaluation_receipt.get("input_id") != input_id
                            or evaluation_receipt.get("input_digest") != original.get("input_digest")
                            or evaluation_receipt.get("result_digest") != original.get("result_digest"))):
                    record.update(current=False, status="failed", invalid=True,
                                  invalid_reason="evaluation_conflict")
                    return {"applied": False, "judgement": record,
                            "reason": "evaluation_conflict", "receipt": None}
                original_applied = original.get("application_status") in {
                    "applied", "candidate_registered"}
                applicability = False
                if isinstance(target, dict):
                    with self.lock:
                        with self._projects().lock:
                            project = self._projects().get(run["project_id"],
                                                           verify_artifacts=False)
                            expected_now = self._judgement_expected(
                                run, expected_scope if expected_scope is not None
                                else JUDGEMENT_SCOPE)
                            applicability = self._judgement_applicable(
                                project, target,
                                expected={**expected_now, "review_result": judgement,
                                          "refinement_standards": frozen_standards,
                                          "kinds": ("candidate",) if kind == "candidate"
                                          else ("working", "open")}, run=run) is None
                record["current"] = bool(original_applied and applicability)
                return {"applied": original_applied, "judgement": record,
                        "reason": original.get("application_reason"),
                        "receipt": original, "currently_applicable": applicability}
        failures = self._validate_evaluation_input(
            run["project_id"], input_id, run_id=identifier, judgement=judgement)
        if not isinstance(evaluation_receipt, dict):
            failures.append({"input_id": input_id, "reason": "receipt_missing"})
        elif (evaluation_receipt.get("evaluation_id") != evaluation_id
              or evaluation_receipt.get("input_id") != input_id
              or evaluation_receipt.get("input_digest") != record.get("input_digest")
              or evaluation_receipt.get("result_digest") != _result_digest(judgement)
              or evaluation_receipt.get("run_id") != identifier
              or evaluation_receipt.get("project_id") != run["project_id"]):
            failures.append({"input_id": input_id, "reason": "receipt_identity_mismatch"})
        if failures:
            record["current"] = False
            record["status"] = "failed"
            record["invalid"] = True
            record["invalid_reason"] = "input_integrity_failed"
            record["input_failures"] = failures
            record.setdefault("message", "登记入口拒绝：已发送输入完整性校验失败。")
            return {"applied": False, "judgement": record,
                    "reason": "input_integrity_failed", "receipt": None}
        expected = self._judgement_expected(
            run, expected_scope if expected_scope is not None else JUDGEMENT_SCOPE)
        expected["review_result"] = judgement
        expected["refinement_standards"] = frozen_standards
        conflict = self._registration_conflict(judgement, target)
        if conflict is not None:
            record["current"] = False
            if conflict == "target_missing":
                record["stale"] = True
                record["stale_reason"] = "target_missing"
                return {"applied": False, "judgement": record, "reason": "target_missing", "receipt": None}
            record["status"] = "failed"
            record["invalid"] = True
            record["invalid_reason"] = conflict
            record.setdefault("message", f"登记入口拒绝无效判决：{conflict}。")
            return {"applied": False, "judgement": record, "reason": conflict, "receipt": None}
        if (isinstance(target, dict) and judgement.get("verdict") == "pass"
                and isinstance(target.get("scope"), dict)
                and any(value == "unknown" for value in target["scope"].values())):
            # An uncertain scope can never be registered as passed: structural
            # acceptance must not stand in for unjudged material/lighting goals.
            record["current"] = False
            record["status"] = "failed"
            record["invalid"] = True
            record["invalid_reason"] = "scope_uncertain"
            record.setdefault("message", "登记入口拒绝无效判决：scope_uncertain。")
            return {"applied": False, "judgement": record, "reason": "scope_uncertain", "receipt": None}
        if kind == "candidate":
            incomplete = self._judgement_target_incomplete(target)
            if incomplete is not None:
                record["current"] = False
                record["status"] = "failed"
                record["invalid"] = True
                record["invalid_reason"] = incomplete
                record.setdefault("message", f"登记入口拒绝无效判决：{incomplete}。")
                return {"applied": False, "judgement": record, "reason": incomplete, "receipt": None}
            if target["current"].get("run_id") != identifier:
                record["current"] = False
                record["status"] = "failed"
                record["invalid"] = True
                record["invalid_reason"] = "owner_conflict"
                record.setdefault("message", "登记入口拒绝无效判决：owner_conflict。")
                return {"applied": False, "judgement": record, "reason": "owner_conflict", "receipt": None}
            agent = run.get("agent") or {}
            if target["current"].get("revision_id") != agent.get("selected_revision"):
                record["current"] = False
                record["status"] = "failed"
                record["invalid"] = True
                record["invalid_reason"] = "unselected_revision"
                record.setdefault("message", "登记入口拒绝无效判决：unselected_revision。")
                return {"applied": False, "judgement": record, "reason": "unselected_revision", "receipt": None}
            before = target.get("before")
            if before is not None and before.get("run_id") != identifier:
                record["current"] = False
                record["status"] = "failed"
                record["invalid"] = True
                record["invalid_reason"] = "before_owner_conflict"
                record.setdefault("message", "登记入口拒绝无效判决：before_owner_conflict。")
                return {"applied": False, "judgement": record, "reason": "before_owner_conflict", "receipt": None}
            # Candidate acceptance must read the project state under the same
            # repository lock as any concurrent workspace write, never a
            # lock-free snapshot, and it never mutates the original working
            # workspace. Its judgement stays in-run.
            #
            # Lock order contract: the run lock (A) is always acquired before
            # the project lock (B) on the registration path. The predicate needs
            # the run lock to resolve file snapshots, so taking B first would
            # form the A->B / B->A wait cycle with list/query/save.
            with self.lock:
                with self._projects().lock:
                    project = self._projects().get(run["project_id"], verify_artifacts=False)
                    reason = self._judgement_applicable(
                        project, target, expected={**expected, "kinds": ("candidate",)}, run=run)
            if reason is not None:
                record["current"] = False
                record["stale"] = True
                record["stale_reason"] = reason
                return {"applied": False, "judgement": record, "reason": reason, "receipt": None}
            record["current"] = False
            receipt = None
            if evaluation_receipt is not None:
                receipt = {**copy.deepcopy(evaluation_receipt),
                           "application_status": "candidate_registered",
                           "application_reason": None}
                receipt["projection"]["judgement"]["current"] = False
                receipt["projection"]["application_receipt"] = {
                    key: value for key, value in receipt.items() if key != "projection"}
                state = self._projects().state_store(run["project_id"])
                receipt, _ = state.register_evaluation_receipt(receipt)
            return {"applied": True, "judgement": record, "reason": None, "receipt": receipt}
        expected = {**expected, "kinds": ("working", "open")}
        decision = {"reason": None}

        def check(project):
            decision["reason"] = self._judgement_applicable(project, target, expected=expected, run=run)
            return decision["reason"] is None

        def mutate(value):
            value["latest_judgement"] = {**copy.deepcopy(judgement), "current": True}
            if kind == "working":
                batch = value.get("batch_start")
                if batch and batch.get("id") == judgement.get("batch_id"):
                    batch["last_judged_round"] = int(batch.get("edit_rounds", 0))
                    if judgement["verdict"] == "pass":
                        batch["status"] = "passed"
            from .refinement_evaluation import apply_reviews
            apply_reviews(value, judgement, self._timestamp())
            if evaluation_receipt is not None:
                projection = evaluation_receipt["projection"]
                projection["judgement"]["current"] = True
                projection["applied_post"] = {
                    "issues_digest": self._digest(value["issues"]),
                    "edit_rounds": int((value.get("batch_start") or {}).get("edit_rounds", 0)),
                    "last_judged_round": int((value.get("batch_start") or {}).get("last_judged_round", 0)),
                    "batch_status": (value.get("batch_start") or {}).get("status")}
                projection["application_receipt"] = {
                    key: item for key, item in evaluation_receipt.items() if key != "projection"}
            return value

        # Same lock order contract as the candidate branch: hold the run lock
        # before the project lock so the predicate's run/file reads cannot
        # invert against list/query/save.
        receipt = None
        with self.lock:
            if evaluation_receipt is not None:
                evaluation_receipt = {**copy.deepcopy(evaluation_receipt),
                                      "application_status": "applied",
                                      "application_reason": None}
            updated = self._projects().update_workspace_checked(
                run["project_id"], check, mutate,
                accepted_judge=({"id": judgement["id"], "selection": target["current"],
                                 "working_version": judgement["working_version"]}
                                if isinstance(target, dict) and isinstance(target.get("current"), dict)
                                else None),
                evaluation_receipt=evaluation_receipt)
        applied = updated is not None
        if updated is not None:
            receipt = updated.get("evaluation_receipt")
        record["current"] = applied
        if applied:
            record.pop("stale", None)
            record.pop("stale_reason", None)
        else:
            record["stale"] = True
            record["stale_reason"] = decision["reason"] or "target_missing"
        return {"applied": applied, "judgement": record,
                "reason": record.get("stale_reason"), "receipt": receipt}

    def _try_recover_evaluation_receipt(self, identifier, eval_receipt, run, scope,
                                         stop_reason, key, state):
        """Recover the durable result when SQL committed but the response was lost.

        Returns None when no receipt exists (confirmed uncommitted → caller may
        register a failure).  Returns {"status": "unknown"} when the lookup is
        itself uncertain.  Returns {"status": "recovered", ...} when a matching
        receipt is found so the caller can restore the original public result.
        A receipt with a different identity is a real conflict and is not
        silently recovered.
        """
        if eval_receipt is None:
            return None
        evaluation_id = eval_receipt.get("evaluation_id")
        if not evaluation_id:
            return None
        try:
            state_store = self._projects().state_store(run["project_id"])
            existing = state_store.lookup_evaluation_receipt(evaluation_id)
        except Exception:
            return {"status": "unknown", "reason": "receipt_lookup_failed"}
        if existing is None:
            return None
        if (existing.get("evaluation_id") != evaluation_id
                or existing.get("input_id") != eval_receipt.get("input_id")
                or existing.get("input_digest") != eval_receipt.get("input_digest")
                or existing.get("result_digest") != eval_receipt.get("result_digest")
                or existing.get("run_id") != identifier
                or existing.get("project_id") != run["project_id"]):
            return {"status": "conflict", "reason": "evaluation_conflict"}
        original_result = existing.get("result") or {}
        projection = existing.get("projection") or {}
        judge_status = projection.get("status", "completed")
        return {
            "status": "recovered",
            "judgement": copy.deepcopy(original_result),
            "application_receipt": copy.deepcopy(existing),
            "judge_status": judge_status,
            "input_id": eval_receipt.get("input_id"),
            "reason": existing.get("application_reason"),
            "message": ("评审结果已成功提交；响应丢失后从原回执恢复，"
                        "不重复登记、不重复调用 Provider。"),
        }

    def _repair_judge_history_projection(self, identifier, expected_receipt, run, scope,
                                         stop_reason, key, state):
        """Resolve one failed history write using the exact durable evaluation."""
        from .runtime import AppError
        with self.lock:
            if self._cancelled(identifier):
                return judge_outcome(stop_reason, "cancelled",
                                     input_id=(expected_receipt or {}).get("input_id"))
            recovered = self._try_recover_evaluation_receipt(
                identifier, expected_receipt, run, scope, stop_reason, key, state)
            if recovered is None:
                raise AppError("评审历史写入失败，未找到对应提交回执；保留现场待核对。",
                               "judge_projection_failed", 503)
            if recovered["status"] in {"unknown", "conflict"}:
                return judge_outcome(
                    stop_reason, "unknown" if recovered["status"] == "unknown" else "failed",
                    input_id=(expected_receipt or {}).get("input_id"),
                    unknown=recovered["status"] == "unknown", reason=recovered["reason"],
                    key=copy.deepcopy(key), time=self._timestamp())
            receipt = recovered["application_receipt"]
            result = judge_outcome(**{name: copy.deepcopy(value)
                                     for name, value in receipt["projection"].items()
                                     if name != "schema_version"})
            result["application_receipt"] = copy.deepcopy(receipt)
            try:
                # _get rebuilds this evaluation from SQL, removing any partial
                # JSON append. Retry only its projection, never registration.
                self._save(self._get(identifier))
            except Exception:
                raise AppError("评审结果已提交，但评审历史展示恢复写入受阻；保留原回执，需修复本地存储。",
                               "judge_projection_failed", 503) from None
            return result

    def _record_judge_failure(self, identifier, message, *, format_error=None, target=None,
                              expected_scope=None, receipt_context=None, diagnostics=None):
        """Record a failed judgement against its original target, never a newer one.

        Candidate failures stay in-run only; working/open failures bind to the
        exact workspace state like successful registrations.
        """
        run = self.get(identifier)
        bound = isinstance(target, dict)
        failure = {"id": uuid.uuid4().hex, "status": "failed", "verdict": "uncertain",
                   "message": message, "created_at": self._timestamp(),
                   "target": copy.deepcopy(target) if bound else None,
                   "selection": copy.deepcopy(target.get("current")) if bound else None,
                   "working_version": target.get("working_version") if bound else None}
        if format_error:
            failure["format_error"] = copy.deepcopy(format_error)
        if diagnostics:
            from .providers import sanitize_agent_diagnostics
            safe = sanitize_agent_diagnostics(diagnostics)
            if 'provider_transport' in safe:
                failure['diagnostics'] = {'provider_transport':safe['provider_transport']}
        receipt = None
        if receipt_context:
            from .evaluation_input import result_digest as _result_digest
            manifest = receipt_context["manifest"]
            input_id = manifest["input_id"]
            projection = judge_outcome(
                receipt_context["trigger"], "failed", judgement=copy.deepcopy(failure),
                input_id=input_id, unknown=receipt_context["unknown"],
                reason="judge_error", key=copy.deepcopy(receipt_context["key"]),
                time=receipt_context["time"],
                committed_rounds=receipt_context["committed_rounds"],
                accepted_count=receipt_context["accepted_count"])
            receipt = {
                "evaluation_id": manifest["evaluation_id"], "input_id": input_id,
                "input_digest": manifest["input_digest"],
                "result_digest": _result_digest(failure),
                "result": copy.deepcopy(failure),
                "run_id": identifier, "project_id": run["project_id"],
                "verdict": failure["verdict"], "scope": copy.deepcopy(expected_scope),
                "application_status": "failure_recorded",
                "application_reason": "judge_error", "projection": projection}
        decision = {"reason": "target_unknown" if not bound else None}
        kind = target.get("kind", "working") if bound else "working"
        expected = self._judgement_expected(
            run, expected_scope if expected_scope is not None else JUDGEMENT_SCOPE)
        expected = {**expected, "kinds": ("working", "open")}

        def check(project):
            decision["reason"] = self._judgement_applicable(project, target, expected=expected, run=run)
            return decision["reason"] is None

        def mutate(value):
            # The failure fact and SQL receipt share the project transaction.
            value["latest_judgement"] = {**copy.deepcopy(failure), "current": True}
            if receipt is not None:
                receipt["projection"]["judgement"]["current"] = True
                receipt["result"] = {**copy.deepcopy(failure), "current": True}
                receipt["result_digest"] = _result_digest(receipt["result"])
                receipt["projection"]["applied_post"] = {
                    "issues_digest": self._digest(value["issues"]),
                    "edit_rounds": int((value.get("batch_start") or {}).get("edit_rounds", 0)),
                    "last_judged_round": int((value.get("batch_start") or {}).get("last_judged_round", 0)),
                    "batch_status": (value.get("batch_start") or {}).get("status")}
                receipt["projection"]["application_receipt"] = {
                    key: item for key, item in receipt.items() if key != "projection"}
            return value

        applied = False
        if bound and kind != "candidate":
            with self.lock:
                applied = self._projects().update_workspace_checked(
                    run["project_id"], check, mutate,
                    evaluation_receipt=receipt) is not None
        if applied:
            failure["current"] = True
            failure.pop("stale", None)
            failure.pop("stale_reason", None)
        elif bound and kind == "candidate":
            failure["current"] = False
        else:
            failure["current"] = False
            failure["stale"] = True
            failure["stale_reason"] = decision["reason"] or "target_unknown"
        if receipt is not None and not applied:
            reason = failure.get("stale_reason")
            receipt["application_status"] = "stale" if reason else "failure_recorded"
            receipt["application_reason"] = reason or "judge_error"
            receipt["projection"]["status"] = "stale" if reason else "failed"
            receipt["projection"]["reason"] = reason or "judge_error"
            receipt["projection"]["judgement"] = copy.deepcopy(failure)
            receipt["result"] = copy.deepcopy(failure)
            receipt["result_digest"] = _result_digest(failure)
            receipt["projection"]["application_receipt"] = {
                key: item for key, item in receipt.items() if key != "projection"}
            self._projects().state_store(run["project_id"]).register_evaluation_receipt(receipt)
        return failure

    def save_checkpoint(self, project_id, body=None, *, _run_id=None):
        self._assert_asset_write(project_id, run_id=_run_id)
        from .runtime import AppError, write_json
        body = body or {}
        if not isinstance(body, dict) or set(body) - {"title"}:
            raise AppError("检查点参数无效。")
        title = body.get("title") or "手动检查点"
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise AppError("检查点名称需要是 1–200 字文字。")
        with self.lock:
            project = self.get_project(project_id)
            selection = project.get("current_scene")
            if not selection:
                raise AppError("当前没有 Working Scene 可以保存检查点。", "working_scene_missing", 409)
            version = self.scene_version(selection["run_id"], selection.get("revision_id"))
            identifier = uuid.uuid4().hex
            root = Path(project["path"]) / ".sceneloop" / "checkpoints"
            temporary = root / ("." + identifier + ".tmp")
            final = root / identifier
            temporary.mkdir(parents=True, exist_ok=False)
            try:
                copied = {}
                for name, source in version["files"].items():
                    target = temporary / source.name
                    shutil.copy2(source, target)
                    copied[name] = {"name": target.name, "sha256": file_sha256(target)}
                for view in ("main", "auxiliary"):
                    source = version["workspace"] / (view + ".png")
                    if source.is_file() and not is_link(source):
                        target = temporary / source.name
                        shutil.copy2(source, target)
                        copied[view] = {"name": target.name, "sha256": file_sha256(target)}
                manifest = {"schema_version": 1, "id": identifier, "title": title.strip(),
                            "created_at": self._timestamp(), "selection": copy.deepcopy(selection),
                            "working_version": project["workspace"]["working_version"],
                            "stage": project["workspace"]["stage"], "files": copied,
                            "design_context": self._design_context(version["run"]),
                            "issues": [copy.deepcopy(row) for row in project["workspace"]["issues"]
                                       if row.get("working_version") == project["workspace"]["working_version"]]}
                write_json(temporary / "checkpoint.json", manifest)
                root.mkdir(parents=True, exist_ok=True)
                temporary.replace(final)
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
            def mutate(value):
                value["checkpoints"].append({**manifest, "path": f"checkpoints/{identifier}"})
                value["checkpoints"] = value["checkpoints"][-100:]
                return value
            updated = self._projects().update_workspace(project_id, project["revision"], mutate)
            return {"checkpoint": updated["workspace"]["checkpoints"][-1], "project": updated}

    def _materialize_checkpoint(self, project_id, checkpoint):
        """Create a self-contained, immutable run from a manual checkpoint copy."""
        from .runtime import AppError, now
        project = self.get_project(project_id)
        checkpoint_id = checkpoint.get("id")
        if not isinstance(checkpoint_id, str) or not re.fullmatch(r"[0-9a-f]{32}", checkpoint_id):
            raise AppError("检查点标识无效。", "checkpoint_changed", 409)
        root = (Path(project["path"]) / ".sceneloop" / "checkpoints" / checkpoint_id).resolve()
        managed = (Path(project["path"]) / ".sceneloop" / "checkpoints").resolve()
        if (not root.is_relative_to(managed) or is_link(root)
                or not (root / "checkpoint.json").is_file() or is_link(root / "checkpoint.json")):
            raise AppError("检查点文件缺失或位置已改变。", "checkpoint_changed", 409)
        try:
            manifest = json.loads((root / "checkpoint.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise AppError("检查点清单无法读取。", "checkpoint_changed", 409) from None
        if manifest.get("id") != checkpoint_id or manifest.get("files") != checkpoint.get("files"):
            raise AppError("检查点登记信息与文件清单不一致。", "checkpoint_changed", 409)
        required = {"blend": "scene.blend", "plan": "plan.json", "inspection": "inspection.json",
                    "main": "main.png", "auxiliary": "auxiliary.png"}
        sources = {}
        for key, default_name in required.items():
            row = manifest.get("files", {}).get(key)
            if not isinstance(row, dict):
                if key in {"main", "auxiliary"}:
                    continue
                raise AppError("检查点缺少可恢复的场景文件。", "checkpoint_changed", 409)
            name = row.get("name", default_name)
            source = (root / name).resolve()
            if (not source.is_relative_to(root) or is_link(source) or not source.is_file()
                    or file_sha256(source) != row.get("sha256")):
                raise AppError("检查点文件已变化，停止恢复。", "checkpoint_changed", 409)
            sources[key] = source
        identifier = uuid.uuid4().hex
        output = self._projects().runs_root(project_id) / identifier
        workspace = output / "candidate"
        workspace.mkdir(parents=True, exist_ok=False)
        try:
            for key, source in sources.items():
                shutil.copy2(source, workspace / required[key])
            plan = json.loads((workspace / "plan.json").read_text(encoding="utf-8"))
            # A generated checkpoint copy still carries the original candidate's
            # embedded workspace marker; re-bind it to this new isolated directory
            # (isolation semantics, not the user's live scene) and rebuild the
            # dependent inspection before anything is registered. Imported copies
            # keep their existing restore behavior.
            if plan.get("scene_kind") != "imported":
                adapter = self.blender.create_adapter(mode="headless", blender_path=self.discovery.get("path"))
                adapter.rebind_candidate(workspace)
            original = manifest.get("selection") or checkpoint.get("selection") or {}
            run = {"id": identifier, "project_id": project_id, "operation": "checkpoint_restore",
                   "scene_id": original.get("scene_id") or uuid.uuid4().hex,
                   "prompt": f"恢复检查点：{checkpoint.get('title') or checkpoint_id}",
                   "created_at": now(), "status": "completed", "stage": "completed", "events": [],
                   "artifacts": [], "cancel_requested": False,
                   "usage": {"image_calls": 0, "brain_calls": 0, "judge_calls": 0,
                             "judge_tool_calls": 0, "asset3d_calls": 0, "retries": 0, "cost": None},
                   "quality_status": "needs_user_review", "reference": None, "round": 0,
                   "profiles": {}, "iteration": {"enabled": False, "max_rounds": 1},
                   "constraints": copy.deepcopy((manifest.get("design_context") or {}).get("constraints", {})),
                   "execution": {"mode": "headless"}, "idempotency_key": "checkpoint-" + checkpoint_id,
                   "request_fingerprint": checkpoint_id, "scene_kind": plan.get("scene_kind", "parametric"),
                   "design_context": copy.deepcopy(manifest.get("design_context") or {}),
                   "restored_from_checkpoint": checkpoint_id}
            self.runs[identifier] = run
            self.run_directories[identifier] = output
            self._event(run, "completed", "已从手动检查点的独立副本建立可恢复场景；未调用模型。")
            self._save(run)
            for name, kind in (("plan.json", "plan"), ("scene.blend", "blend"),
                               ("inspection.json", "inspection")):
                self._artifact(identifier, workspace / name, kind)
            for view in ("main", "auxiliary"):
                path = workspace / (view + ".png")
                if path.is_file():
                    self._artifact(identifier, path, "render", view_id=view)
            return self._selection_for(run, None, plan)
        except Exception:
            self.runs.pop(identifier, None)
            self.run_directories.pop(identifier, None)
            shutil.rmtree(output, ignore_errors=True)
            raise

    def _restore_selection(self, project_id, selection, expected_revision, *, reason, preserve_current=False):
        self._assert_asset_write(project_id)
        from .runtime import AppError
        project = self.get_project(project_id)
        if project["revision"] != expected_revision:
            raise AppError("Working Scene 已改变，请刷新后重试恢复。", "project_changed", 409)
        version = self.scene_version(selection["run_id"], selection.get("revision_id"))
        if preserve_current and project.get("current_scene") and project["current_scene"] != selection:
            self.save_checkpoint(project_id, {"title": f"恢复前 Working Version {project['workspace']['working_version']}"})
            project = self.get_project(project_id)
        return self._projects().set_current(
            project_id, selection, project["revision"],
            reason=reason, preserve_previous=True,
            artifacts=self._fixed_scene_artifacts(version["run"], version["revision_id"]))

    def workspace_action(self, project_id, action, body, *, _run_id=None):
        self._assert_asset_write(project_id, run_id=_run_id)
        from .runtime import AppError
        if not isinstance(body, dict):
            raise AppError("工作区操作格式无效。")
        project = self.get_project(project_id)
        expected = body.get("expected_revision")
        if type(expected) is not int:
            raise AppError("工作区操作需要当前项目版本。")
        if project["revision"] != expected:
            raise AppError("Working Scene 已改变，请刷新后重试。", "project_changed", 409)
        if action == "checkpoint":
            return self.save_checkpoint(project_id, {"title": body.get("title") or "手动检查点"}, _run_id=_run_id)
        if action == "undo":
            recoveries = project["workspace"].get("recoveries", [])
            if not recoveries:
                raise AppError("没有可撤回的自动恢复点。", "recovery_missing", 409)
            target = recoveries[-1]
            updated = self._restore_selection(project_id, target["selection"], expected,
                                               reason="undo", preserve_current=bool(body.get("preserve_current")))
            def mutate(value):
                value["recoveries"] = [row for row in value["recoveries"]
                                       if row.get("selection") != target["selection"]]
                # The restored version starts any future construction batch;
                # the pre-restore Before and counters must not survive.
                value["batch_start"] = None
                return value
            updated = self._projects().update_workspace(project_id, updated["revision"], mutate)
            return {"project": updated, "restored": target}
        if action == "restore-batch":
            batch = project["workspace"].get("batch_start")
            if not batch:
                raise AppError("当前没有裁判批次起点。", "recovery_missing", 409)
            updated = self._restore_selection(project_id, batch["selection"], expected,
                                               reason="batch_restore", preserve_current=bool(body.get("preserve_current")))
            def mutate(value):
                value["batch_start"] = None
                return value
            updated = self._projects().update_workspace(project_id, updated["revision"], mutate)
            return {"project": updated, "restored": batch}
        if action == "restore-checkpoint":
            checkpoint_id = body.get("checkpoint_id")
            checkpoint = next((row for row in project["workspace"]["checkpoints"]
                               if row.get("id") == checkpoint_id), None)
            if checkpoint is None:
                raise AppError("检查点不存在。", "checkpoint_missing", 404)
            selection = self._materialize_checkpoint(project_id, checkpoint)
            updated = self._restore_selection(project_id, selection, expected,
                                               reason=f"checkpoint:{checkpoint_id}",
                                               preserve_current=bool(body.get("preserve_current")))
            def mutate(value):
                value["batch_start"] = None
                return value
            updated = self._projects().update_workspace(project_id, updated["revision"], mutate)
            return {"project": updated, "restored": checkpoint}
        if action == "settings":
            limit, stage = body.get("recovery_limit"), body.get("stage")
            if limit not in (1, 2) or not isinstance(stage, str) or not stage.strip() or len(stage) > 80:
                raise AppError("恢复点数量或阶段设置无效。")
            if stage.strip()=='appearance':
                from .director_creation import authority,capability
                host=getattr(self,'_director_runtime',None)
                if not host or not authority(host,{'project_id':project_id}) or not capability(self,project_id)['supported']:
                    raise AppError('外观阶段需要原可信创作授权及受支持原生能力。','creation_stage_unsupported',409)
            def mutate(value):
                value["recovery_limit"] = limit
                value["recoveries"] = value["recoveries"][-limit:]
                value["stage"] = stage.strip()
                return value
            return {"project": self._projects().update_workspace(project_id, expected, mutate)}
        raise AppError("工作区操作不存在。", "not_found", 404)

    def query_project_history(self, project_id, query):
        from .context_builder import query_history
        from .runtime import AppError
        if not isinstance(query, dict) or set(query) - {"object_id", "issue_id", "version", "keyword", "limit"}:
            raise AppError("历史查询参数无效。")
        project = self.get_project(project_id)
        limit = query.get("limit", 20)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AppError("历史查询数量无效。")
        return {"turns": query_history(project["workspace"], object_id=query.get("object_id"),
                                        issue_id=query.get("issue_id"), version=query.get("version"),
                                        keyword=query.get("keyword"), limit=limit)}

    @staticmethod
    def _judge_mutable_fields():
        return ("issues_digest", "edit_rounds", "last_judged_round", "batch_status")

    def _judge_cache_key(self, run, project, workspace, *, kind, scope, stage, evaluation,
                         selection, working_version, before, batch, accepted_count=None):
        issues = workspace.get("issues", [])
        reference = run.get("reference") if evaluation == "reference" else None
        digest = (reference or {}).get("sha256")
        frozen = self._refinement_standards(project["id"]) if run.get("goal_snapshot") else None
        return {"refinement_standards": frozen,
                "kind": kind, "selection": copy.deepcopy(selection), "working_version": working_version,
                "before": copy.deepcopy(before),
                "stage": stage, "scope": copy.deepcopy(scope), "evaluation": evaluation,
                "goal_digest": self._expected_goal_digest(run), "issues_digest": self._digest(issues),
                "reference_digest": digest, "target_digest": digest,
                "judge_config": judge_config_identity(run),
                "batch_id": (batch or {}).get("id"),
                "edit_rounds": int((batch or {}).get("edit_rounds", 0)),
                "last_judged_round": int((batch or {}).get("last_judged_round", 0)),
                "batch_status": (batch or {}).get("status"),
                "accepted_count": accepted_count,
                "current_files": self._snapshot_selection_files(selection),
                "before_files": self._snapshot_selection_files(before) if before is not None else None}

    def _verify_key_files(self, run, key):
        """Re-check the frozen files behind a cache candidate; versions never imply bytes."""
        if not self._snapshots_match(key.get("current_files"),
                                     self._snapshot_selection_files(key.get("selection"))):
            return False
        if key.get("before") is not None and not self._snapshots_match(
                key.get("before_files"), self._snapshot_selection_files(key.get("before"))):
            return False
        if (key.get("evaluation") == "reference" and key.get("target_digest") is not None
                and not self._reference_bytes_match(run, key["target_digest"])):
            return False
        return True

    def _reference_bytes_match(self, run, digest):
        from .runtime import AppError
        descriptor = run.get("reference") or {}
        if descriptor.get("sha256") != digest:
            return False
        artifact = next((item for item in run.get("artifacts", [])
                         if item.get("id") == descriptor.get("artifact_id")), None)
        if artifact is None:
            return False
        try:
            path = self.artifact_path(run["id"], artifact["path"])
        except AppError:
            return False
        try:
            return file_sha256(path) == digest
        except OSError:
            return False

    def _judge_cache_hit(self, run, history, key):
        """Reuse a prior acceptance only when every evaluated input still matches.

        Fields the Judge registration itself updates (issues, water levels,
        batch status) may match either the pre-execution key or the recorded
        post-application snapshot; any external change misses and re-judges.
        A field-level match still re-verifies the actual selection files and
        the reference bytes before reuse: identical metadata never implies
        identical evidence.  Sealed evaluation inputs are re-validated so a
        tampered consumed image or measurement rejects reuse.
        """
        mutable = self._judge_mutable_fields()
        file_fields = ("current_files", "before_files")
        for row in reversed(history or []):
            prior = row.get("key")
            if not isinstance(prior, dict):
                continue
            if prior.get("refinement_standards") != key.get("refinement_standards"):
                continue
            if any(prior.get(field) != key.get(field) for field in prior
                   if field not in mutable and field not in file_fields):
                continue
            if any(key.get(field) != prior.get(field) for field in key
                   if field not in mutable and field not in file_fields and field in prior):
                continue
            if not self._snapshots_match(prior.get("current_files"), key.get("current_files")):
                continue
            if prior.get("before") is not None and not self._snapshots_match(
                    prior.get("before_files"), key.get("before_files")):
                continue
            post = row.get("applied_post") or {}
            current = {field: key.get(field) for field in mutable}
            if current != {field: prior.get(field) for field in mutable}:
                if current != {field: post.get(field) for field in mutable}:
                    continue
            if not self._verify_key_files(run, key):
                continue
            prior_input_id = row.get("input_id")
            failures = self._validate_evaluation_input(
                run["project_id"], prior_input_id,
                run_id=run["id"], judgement=row.get("judgement"))
            if failures:
                continue
            return judge_outcome(
                row.get("trigger"), row.get("status"), judgement=copy.deepcopy(row.get("judgement")),
                input_id=prior_input_id,
                application_receipt=copy.deepcopy(row.get("application_receipt")),
                unknown=bool(row.get("unknown")), reason=row.get("reason"),
                key=copy.deepcopy(row.get("key")), time=row.get("time"),
                committed_rounds=row.get("committed_rounds"),
                accepted_count=row.get("accepted_count"),
                applied_post=copy.deepcopy(row.get("applied_post")))
        return None

    @staticmethod
    def _acceptance_subject(key):
        return (key.get("kind"), copy.deepcopy(key.get("selection")), key.get("working_version"),
                copy.deepcopy(key.get("before")), key.get("batch_id"), key.get("accepted_count"))

    def _concluded_acceptance(self, run, key):
        """Return a prior terminal stale/failed attempt for this exact subject.

        A later Stage/issue/context change must not turn an already concluded
        (stale/failed/unknown) acceptance into an automatic new charge; the
        caller reuses the recorded result instead of replaying the old request.
        """
        subject = self._acceptance_subject(key)
        for row in reversed(run.get("judge_history", [])):
            if row.get("status") not in {"stale", "failed"}:
                continue
            if self._acceptance_subject(row.get("key") or {}) == subject:
                return copy.deepcopy(row)
        return None

    def _judge_applied_post(self, project_id):
        workspace = self.get_project(project_id)["workspace"]
        batch = workspace.get("batch_start") or {}
        return {"issues_digest": self._digest(workspace.get("issues", [])),
                "edit_rounds": int(batch.get("edit_rounds", 0)),
                "last_judged_round": int(batch.get("last_judged_round", 0)),
                "batch_status": batch.get("status")}

    @staticmethod
    def _judge_unknown_cause(error):
        """True when the failure leaves the real outcome unknown (not merely rejected)."""
        if getattr(error, "outcome_unknown", False):
            return True
        if getattr(error, "code", None) in {"request_outcome_unknown", "unknown"}:
            return True
        diagnostics = getattr(error, "diagnostics", None) or {}
        return diagnostics.get("finish_reason") == "unknown"

    def judge_agent_checkpoint(self, identifier, state, stop_reason):
        """Independent, version-bound acceptance, reusable inside the Builder loop.

        Working, explicit open (no batch/Before) and generate-candidate
        identities share one freshness-guarded cache; a failed/unknown attempt
        is retained and is never silently retried for the same state.
        """
        from .evaluation_input import result_digest as _result_digest
        from .judge import JudgeAgent
        early_outcome, context = _prepare_judge_checkpoint(
            self, identifier, state, stop_reason, JudgeAgent)
        if early_outcome is not None:
            return early_outcome
        run = context["run"]
        scope = context["scope"]
        judge = context["judge"]
        judge.resource_continuation = (state.get('_pending_judge') or {}).get('continuation')
        key = context["key"]
        override = context["override"]
        describe = context["describe"]
        shouldexec = context["shouldexec"]
        judgement = None
        judge_status = "not_requested"
        unknown_cause = False
        input_id = None
        application_receipt = None
        reason = None
        eval_receipt = None
        if shouldexec:
            self._update(identifier, "judging", f"独立裁判正在检查{describe}。",
                         agent=copy.deepcopy(state))
            try:
                judgement = judge.execute(override)
                input_id = judgement.get("input_id")
                eval_receipt = None
                if input_id:
                    rd = _result_digest(judgement)
                    eval_receipt = {
                        "evaluation_id": judgement.get("evaluation_id", input_id),
                        "input_id": input_id,
                        "input_digest": judgement.get("input_digest", ""),
                        "result_digest": rd, "result": copy.deepcopy(judgement),
                        "run_id": identifier, "project_id": run["project_id"],
                        "verdict": judgement.get("verdict"),
                        "scope": copy.deepcopy(scope),
                        "projection": judge_outcome(
                            stop_reason, "completed", judgement=copy.deepcopy(judgement),
                            input_id=input_id, key=copy.deepcopy(key),
                            time=self._timestamp(), committed_rounds=len(run.get("commits", [])),
                            accepted_count=key.get("accepted_count") if key else None),
                    }
                application = self._record_judgement(
                    identifier, judgement, expected_scope=scope,
                    evaluation_receipt=eval_receipt, input_id=input_id)
                judgement = application["judgement"]
                application_receipt = application.get("receipt")
                if application["applied"]:
                    judge_status = "completed"
                    bound = (f"工作版本 {judgement['working_version']}" if judgement.get("working_version") is not None
                             else f"候选 {judgement.get('selection', {}).get('revision_id')}")
                    message = f"裁判结论：{judgement['verdict']}；结论绑定{bound}。"
                elif judgement.get("invalid"):
                    judge_status = "failed"
                    reason = application["reason"]
                    message = (f"裁判登记被拒绝（{application['reason']}）；"
                               "未应用到当前验收，已提交版本保持不变。")
                else:
                    judge_status = "stale"
                    reason = application["reason"]
                    message = (f"裁判结论对应的受评版本、Stage 或批次已变化（{application['reason']}）；"
                               "旧结论只作为历史记录保留，未应用到当前验收。")
                self._update(identifier, "judging", message, agent=copy.deepcopy(state),
                             judgement=copy.deepcopy(judgement))
            except Exception as error:
                from .judge_continuation import JudgeResourceWait, JudgeContinuationUnavailable
                if isinstance(error, (JudgeResourceWait, JudgeContinuationUnavailable)):
                    # No new request was sent, and all previous requests have
                    # returned. Neither waiting nor a local protected-save
                    # failure is a verdict or an evaluation quality failure.
                    raise
                seal_state = "cancelled" if self._cancelled(identifier) else "failed"
                if self._cancelled(identifier):
                    return judge_outcome(stop_reason, "cancelled",
                                         input_id=getattr(judge, "_input_manifest", {}) and judge._input_manifest.get("input_id"))
                input_manifest = getattr(judge, "_input_manifest", None)
                input_id = input_manifest.get("input_id") if input_manifest else None
                failure_diagnostics = None
                if isinstance(error, self.providers.ProviderError):
                    failure_diagnostics = self._response_diagnostics(identifier, error, role="judge", input_id=input_id)
                # S1: SQL commit may have succeeded before the response was lost.
                # Check the durable receipt first; only register a failure when
                # we can confirm no commit, or keep unknown when the query is
                # itself uncertain.  Never overwrite a real result with a
                # synthetic failure that trips the idempotent conflict guard.
                recovered = self._try_recover_evaluation_receipt(
                    identifier, eval_receipt, run, scope, stop_reason, key, state)
                if recovered is not None:
                    if recovered.get("status") == "unknown":
                        return judge_outcome(
                            stop_reason, "unknown",
                            input_id=input_id, unknown=True,
                            reason=recovered.get("reason", "receipt_lookup_failed"),
                            key=copy.deepcopy(key), time=self._timestamp(),
                            committed_rounds=len(run.get("commits", [])),
                            accepted_count=key.get("accepted_count") if key else None)
                    if recovered.get("status") == "conflict":
                        judge_status = "failed"
                        reason = "evaluation_conflict"
                        message = ("评审 ID 已被不同内容占用；不重复登记，"
                                   "保留原回执与真实冲突事实。")
                        self._update(identifier, "judging", message, agent=copy.deepcopy(state))
                    else:
                        judgement = recovered["judgement"]
                        judge_status = recovered["judge_status"]
                        application_receipt = recovered["application_receipt"]
                        reason = recovered.get("reason")
                        input_id = recovered.get("input_id", input_id)
                        message = recovered["message"]
                        self._update(identifier, "judging", message, agent=copy.deepcopy(state),
                                     judgement=copy.deepcopy(judgement))
                else:
                    message = str(error) if getattr(error, "code", None) or error.__class__.__name__ == "JudgeError" else "裁判执行失败；已提交版本保持不变。"
                    format_error = (copy.deepcopy(getattr(error, "diagnostics", None))
                                    if error.__class__.__name__ == "JudgeError" else None)
                    unknown_cause = self._judge_unknown_cause(error)
                    receipt_context = ({
                        "manifest": input_manifest, "trigger": stop_reason,
                        "unknown": unknown_cause, "key": key, "time": self._timestamp(),
                        "committed_rounds": len(run.get("commits", [])),
                        "accepted_count": key.get("accepted_count") if key else None,
                    } if input_id else None)
                    judgement = self._record_judge_failure(identifier, message, format_error=format_error,
                                                           target=getattr(judge, "target", None),
                                                           expected_scope=scope,
                                                           receipt_context=receipt_context,
                                                           diagnostics=failure_diagnostics)
                    if input_id:
                        application_receipt = self._projects().state_store(
                            run["project_id"]).lookup_evaluation_receipt(input_id)
                    judge_status = "stale" if judgement.get("stale") else "failed"
                    reason = judgement.get("stale_reason") or "judge_error"
                    self._update(identifier, "judging", message, agent=copy.deepcopy(state),
                                 judgement=copy.deepcopy(judgement))
        applied_post = None
        if judge_status == "completed" and (judgement or {}).get("current"):
            applied_post = self._judge_applied_post(run["project_id"])
        unknown = (unknown_cause if judge_status in {"failed", "stale"} else
                   judge_status == "completed" and isinstance(judgement, dict)
                   and judgement.get("verdict") == "uncertain")
        result = judge_outcome(
            stop_reason, judge_status,
            judgement=copy.deepcopy(judgement), input_id=input_id,
            application_receipt=copy.deepcopy(application_receipt),
            unknown=bool(unknown), reason=reason,
            key=key, time=self._timestamp(),
            committed_rounds=len(run.get("commits", [])),
            accepted_count=key.get("accepted_count") if key else None,
            applied_post=applied_post)
        if judge_status in {"failed", "stale"} and input_id and not (
                getattr(judge, "_input_manifest", None)
                and self._projects().state_store(run["project_id"]).lookup_evaluation_receipt(input_id)):
            # A valid model response can still be refused by freshness or input
            # integrity gates. Preserve that public failure even without a
            # workspace mutation; it never becomes an applied PASS.
            receipt = {
                "evaluation_id": input_id, "input_id": input_id,
                "input_digest": judge._input_manifest["input_digest"],
                "result_digest": _result_digest(judgement),
                "result": copy.deepcopy(judgement),
                "run_id": identifier, "project_id": run["project_id"],
                "verdict": (judgement or {}).get("verdict"), "scope": copy.deepcopy(scope),
                "application_status": judge_status, "application_reason": reason,
                "projection": copy.deepcopy(result)}
            receipt["projection"]["application_receipt"] = {
                key: item for key, item in receipt.items() if key != "projection"}
            self._projects().state_store(run["project_id"]).register_evaluation_receipt(receipt)
        if judge_status != "not_requested":
            try:
                with self.lock:
                    stored = self._get(identifier)
                    stored.setdefault("judge_history", []).append(copy.deepcopy(result))
                    stored["judge_status"] = judge_status
                    self._save(stored)
            except Exception:
                return self._repair_judge_history_projection(
                    identifier, eval_receipt or application_receipt, run, scope,
                    stop_reason, key, state)
        return result

    def finalize_agent_run(self, identifier, state, stop_reason, summary):
        """Run independent acceptance, then persist the terminal conversation turn.

        Cancellation wins over any in-flight acceptance: a cancelled run is
        never rewritten as completed. An unconfirmed goal claim (uncertain,
        rollback, failed, stale or unknown outcome) is qualified explicitly
        and never stored as goal achievement; the Builder's original claim
        stays in the conversation summary as a claim, while the structured
        acceptance carries the fact. Candidate acceptances never borrow the
        Working quality state.
        """
        run = self.get(identifier)
        published = False
        unpublished_bootstrap = False
        if run.get('bootstrap') and state.get('selected_revision'):
            readiness = self.bootstrap_initial_readiness(identifier, state['selected_revision'], agent_state=state)
            unpublished_bootstrap = not readiness['ready']
            if unpublished_bootstrap and stop_reason == 'goal_reached':
                from .runtime import AppError
                raise AppError('初版自检未通过，不能发布完成主张。', 'bootstrap_self_check_required', 409)
        if run.get("operation") == "generate" and state.get("selected_revision") and not unpublished_bootstrap:
            before_current = None
            if run.get("project_id"):
                before_current = (self.get_project(run["project_id"]).get("current_scene") or {}).copy()
            self.commit_initial_working_scene(identifier, state["selected_revision"])
            run = self.get(identifier)
            if before_current is not None:
                after_current = self.get_project(run["project_id"]).get("current_scene") or {}
                published = (after_current != before_current
                             and after_current.get("revision_id") == state.get("selected_revision"))
        acceptance = (judge_outcome(stop_reason, 'not_requested', reason='bootstrap_candidate_unpublished')
                      if unpublished_bootstrap else self.judge_agent_checkpoint(identifier, state, stop_reason))
        if self._cancelled(identifier):
            return
        judgement, judge_status = acceptance["judgement"], acceptance["status"]
        state.pop('_pending_judge', None)
        judgement_kind = judgement.get("kind", "working") if isinstance(judgement, dict) else "working"
        kind = "discussion" if stop_reason in {"discussion", "clarification"} else (
            "edit" if state.get("used", {}).get("edit_rounds", 0) else "inspection")
        builder_claim = summary
        final_message = summary
        if run.get("operation") == "edit" and kind == "edit":
            if (run.get("iteration") or {}).get("mode") == "local_round_v1" and not run.get("commits"):
                final_message += " 草稿历史已保留，Working未因本轮草稿改变。"
            else:
                final_message += " 已提交的修改已成为当前 Working Scene；仍需按裁判或人工结论确认质量。"
        elif run.get("operation") == "generate" and state.get("selected_revision"):
            revision = state.get("selected_revision")
            final_message += (f" 候选 {revision} 已保留但未满足初版发布条件；Working 未发布，未进行独立 Judge。"
                              if unpublished_bootstrap else
                              f" 候选 {revision} 已评审；已成为初始 Working Scene，仍需人工确认质量。"
                              if published else
                              f" 候选 {revision} 已评审，未发布；原 Working Scene 保持不变。")
        confirmed = (judge_status == "completed" and isinstance(judgement, dict)
                     and judgement.get("verdict") == "pass" and judgement.get("stage_passed") is True
                     and judgement_kind in {"working", "open"})
        candidate_confirmed = (judge_status == "completed" and isinstance(judgement, dict)
                               and judgement.get("verdict") == "pass" and judgement.get("stage_passed") is True
                               and judgement_kind == "candidate")
        goal_claimed = stop_reason == "goal_reached"
        unknown = bool(acceptance.get("unknown")) or (
            judge_status == "completed" and isinstance(judgement, dict) and judgement.get("verdict") == "uncertain")
        acceptance_record = {"goal_claimed": goal_claimed,
                             "goal_confirmed": bool((confirmed or candidate_confirmed) and goal_claimed),
                             "verdict": judgement.get("verdict") if isinstance(judgement, dict) else None,
                             "judge_status": judge_status,
                             "trigger": acceptance.get("trigger"),
                             "unknown": unknown,
                             "subject": "candidate" if judgement_kind == "candidate" else "working"}
        if judgement_kind == "open" and judge_status == "completed" and isinstance(judgement, dict) \
                and judgement.get("verdict") == "pass":
            final_message += " 当前工作版通过本 Stage 的独立验收（无施工批次）。"
        # Only an activated independent acceptance can "unconfirm" a completion
        # claim; a run without Judge keeps the Builder's clearly-labelled claim.
        judge_enabled = bool((run.get("judge") or {}).get("enabled"))
        unconfirmed_goal = (goal_claimed and not acceptance_record["goal_confirmed"]
                            and (unknown or judge_enabled))
        if unknown or unconfirmed_goal:
            reason = ("结果未知" if unknown else
                      "独立验收未能确认目标达成" if judge_status in {"completed", "not_requested"}
                      else "独立验收未能形成有效结论")
            detail = ""
            if isinstance(judgement, dict):
                detail = judgement.get("message") or judgement.get("verdict") or ""
            # Do not persist the Builder's unconfirmed completion claim as the
            # terminal fact; keep it only as the separately recorded claim.
            final_message = f"独立验收未确认完成主张（{reason}{('：' + detail) if detail else ''}）；需人工检查。"
            if run.get("operation") == "edit" and kind == "edit":
                final_message += " 已提交的修改已成为当前 Working Scene。"
            elif run.get("operation") == "generate" and state.get("selected_revision"):
                final_message += (f" 候选 {state.get('selected_revision')} 已评审；"
                                  "质量仍需人工确认。")
        if candidate_confirmed:
            quality_status = "candidate_passed"
        elif confirmed:
            quality_status = "judge_passed"
        else:
            quality_status = "needs_user_review"
        # An unconfirmed completion claim must never survive as goal_reached in
        # the terminal facts; the Builder's own claim stays in the conversation
        # summary, while the structured acceptance carries the real outcome.
        final_state = copy.deepcopy(state)
        if (unknown or unconfirmed_goal) and final_state.get("stop_reason") == "goal_reached":
            final_state["stop_reason"] = "needs_user_review"
        self._update(identifier, "completed", final_message, status="completed",
                     quality_status=quality_status,
                     agent=final_state, judge_status=judge_status,
                     judgement=copy.deepcopy(judgement), acceptance=acceptance_record,
                     builder_claim=builder_claim)
        try:
            evidence = state.get("response_evidence") or []
            self.record_conversation_turn(identifier, kind=kind, summary=final_message,
                                          status="completed", evidence=evidence)
        except Exception:
            # The terminal run remains truthful even if secondary context indexing fails.
            self._update(identifier, "completed", "执行结果已保存，但会话索引更新失败；场景工作版不受影响。",
                         context_index_status="failed")

    def select_scene(self, identifier, body):
        self._assert_asset_write(identifier)
        from .runtime import AppError
        if not isinstance(body, dict) or set(body) != {"run_id", "revision_id", "expected_revision"}:
            raise AppError("采用场景需要任务、候选版本和预期项目版本。")
        with self.lock:
            self.get_project(identifier)
            version = self.scene_version(body["run_id"], body["revision_id"])
            run = version["run"]
            if run.get("project_id") != identifier:
                raise AppError("请先把历史场景导入这个项目，再采用其项目内副本。", "scene_project_mismatch", 409)
            selection = {"run_id": run["id"], "revision_id": body["revision_id"], "scene_id": run["scene_id"],
                         "title": (version["plan"].get("title") or version["plan"].get("scene_name") or run["prompt"])[:500]}
            project = self._projects().set_current(
                identifier, selection, body["expected_revision"],
                artifacts=self._fixed_scene_artifacts(run, body["revision_id"]))
            # Explicit adoption changes the working pointer: prior Judge batch
            # and cache state must not survive it.
            def clear_batch(value):
                value["batch_start"] = None
                return value
            project = self._projects().update_workspace(identifier, project["revision"], clear_batch)
            return self.get_project(identifier)

    def _copy_reference(self, source_run, target_id, *, activate=True):
        from .runtime import AppError
        reference = source_run.get("reference") or source_run.get("available_reference")
        if not reference:
            raise AppError("该场景没有已采用参考。", "reference_missing", 409)
        artifact = next((item for item in source_run["artifacts"] if item["id"] == reference["artifact_id"]), None)
        if artifact is None or artifact["sha256"] != reference["sha256"]:
            raise AppError("旧参考的登记信息不一致。", "reference_changed", 409)
        source = self._checked_artifact(source_run, artifact)
        output = self.run_directory(target_id) / "concept" / ("reference" + source.suffix)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, output)
        if file_sha256(output) != reference["sha256"]:
            raise AppError("参考图在复制期间发生变化，已停止沿用。", "reference_changed", 409)
        item = self._artifact(target_id, output, "concept", metadata=artifact.get("source"))
        if item["sha256"] != reference["sha256"]:
            raise AppError("参考图副本发生变化，已停止沿用。", "reference_changed", 409)
        descriptor = {"artifact_id": item["id"], "sha256": reference["sha256"],
                      "basis": "explicit_inherited_reference" if activate else "retained_project_reference"}
        message = ("已按本次明确选择沿用该场景的参考图；不会调用生图服务。" if activate
                   else "项目参考图已保留；本次不将其作为评价图片发送给大脑。")
        self._update(target_id, "reference_inherited", message,
                     reference=descriptor if activate else None, available_reference=descriptor)

    def _edit_scene(self, identifier):
        from .visual_agent import VisualAgent
        try:
            if self._cancelled(identifier):
                return
            run = self.get(identifier)
            self._verify_project_baseline(run)
            version = self.scene_version(**run["base_scene"])
            self._begin_builder_batch(run)
            from .project_references import attach_scene
            if attach_scene(self,identifier):
                pass
            elif run["evaluation"]["mode"] == "reference":
                self._copy_reference(version["run"], identifier)
            else:
                if version["run"].get("reference") or version["run"].get("available_reference"):
                    self._copy_reference(version["run"], identifier, activate=False)
                self._update(identifier, "planning", "续改当前场景：冻结原意图与风格，通过前后截图比较；本次未指定参考图。")
            self.bind_refinement_goal(identifier)
            VisualAgent(self, identifier).execute()
        except Exception as error:
            self._fail(identifier, error)

    def _bootstrap_scene(self, identifier):
        from .visual_agent import VisualAgent
        try:
            run=self.get(identifier)
            if self._cancelled(identifier):return
            record=self._director_host().binding(identifier,run['project_id'])
            from .director_creation import authority
            parent=self.runs[record['parent_id']] if record else None
            if (not record or not record['brief'].get('bootstrap') or record['state']!='started'
                    or record['brief'].get('creation_authorization')!=authority(self._director_host(),parent)
                    or self.get_project(run['project_id']).get('current_scene')):
                from .runtime import AppError
                raise AppError('初建委派或空Working基线失效。','director_stale',409)
            from .project_references import attach_scene
            attach_scene(self,identifier)
            self.bind_refinement_goal(identifier)
            VisualAgent(self,identifier).execute()
        except Exception as error:
            self._fail(identifier,error)

    def import_scene(self, identifier, body):
        self._assert_asset_write(identifier)
        from .runtime import AppError, now
        from .native_candidate import normalize_source, verify_source
        if not isinstance(body, dict):
            raise AppError("导入请求格式无效。")
        key = body.get("idempotency_key")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{12,128}", key):
            raise AppError("导入需要有效的幂等标识。")
        expected = body.get("expected_revision")
        if type(expected) is not int or expected < 1:
            raise AppError("导入需要有效的项目版本。")
        mode = body.get("execution", {}).get("mode", "headless") if isinstance(body.get("execution", {}), dict) else None
        if mode not in ("headless", "attached"):
            raise AppError("Blender 模式无效。")
        scene_name = body.get("scene_name")
        if scene_name is not None and (not isinstance(scene_name, str) or len(scene_name) > 500):
            raise AppError("场景名称无效。")
        selection = None
        path = body.get("path")
        native = None
        if "native_candidate" in body:
            if path is not None or body.get("run_id") is not None or body.get("revision_id") is not None:
                raise AppError("请仅选择一个导入来源。")
            if mode != "headless" or scene_name is not None:
                raise AppError("native候选仅支持Headless原生workspace导入。", "native_candidate_invalid")
            native = normalize_source(body["native_candidate"])
        elif path is not None:
            if body.get("run_id") is not None or not isinstance(path, str) or not path.strip():
                raise AppError("请仅选择一个导入来源。")
            source = Path(path)
            if not source.is_absolute() or source.suffix.lower() != ".blend" or not source.is_file():
                raise AppError("请选择现有 .blend 文件的完整路径。")
            path = str(source.resolve())
        else:
            if not isinstance(body.get("run_id"), str):
                raise AppError("请选择 .blend 文件或已有生成版本。")
            selection = {"run_id": body["run_id"], "revision_id": body.get("revision_id")}
        normalized = {"project_id": identifier, "operation": "import", "path": path,
                      "selection": selection, "scene_name": scene_name, "expected_revision": expected,
                      "execution": {"mode": mode}, "idempotency_key": key}
        if native is not None:
            normalized["native_candidate"] = native
        fingerprint = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
        with self.lock:
            for previous in self.runs.values():
                if previous["idempotency_key"] == key:
                    if previous["request_fingerprint"] != fingerprint:
                        raise AppError("导入标识已用于另一份请求。", "idempotency_conflict", 409)
                    if native is not None:
                        # Query authoritative receipts even if the post-commit Run ACK was lost.
                        operation_id = uuid.uuid5(uuid.UUID(identifier), f"{previous['id']}:initial:None").hex
                        prior = self._projects().state_store(identifier).lookup_receipt(operation_id)
                        if prior["status"] == "committed" and prior.get("artifact_status") == "available":
                            if previous.get("status") != "completed":
                                self._update(previous["id"], "completed", "已按SQL回执恢复native导入；没有重放Blender。",
                                    status="completed", quality_status="needs_user_review", error=None,
                                    native_intake={"state": "completed", "source": copy.deepcopy(native)})
                            return self.get(previous["id"])
                    return copy.deepcopy(previous)
            project = self.get_project(identifier)
            if project["revision"] != expected:
                raise AppError("项目已改变，请刷新再导入。", "project_changed", 409)
            self._assert_idle()
            self._project_environment(normalized)
            if native is not None:
                _, native_plan = verify_source(native)
            version = self.scene_version(**selection) if selection else None
            run_id = uuid.uuid4().hex
            run = {"id": run_id, "project_id": identifier, "operation": "import", "scene_id": uuid.uuid4().hex,
                   "prompt": "导入场景：" + (native_plan.get("title", "native候选") if native is not None else
                       version["plan"].get("title", "历史版本") if version else Path(path).stem),
                   "created_at": now(), "status": "running", "stage": "importing", "events": [], "artifacts": [],
                   "cancel_requested": False,
                   "usage": {"image_calls": 0, "brain_calls": 0, "judge_calls": 0,
                             "judge_tool_calls": 0, "asset3d_calls": 0, "retries": 0, "cost": None},
                   "quality_status": "not_evaluated", "reference": None, "round": 0, "profiles": {},
                   "iteration": {"enabled": False, "max_rounds": 3}, "constraints": {}, "execution": normalized["execution"],
                   "idempotency_key": key, "request_fingerprint": fingerprint, "import_source": normalized,
                   "expected_project_revision": expected}
            if version:
                run["design_context"] = self._design_context(version["run"])
            if native is not None:
                run["native_intake"] = {"state": "admitted", "source": copy.deepcopy(native)}
            self.runs[run_id] = run
            self.run_directories[run_id] = self._projects().runs_root(identifier) / run_id
            self._event(run, "importing", "正在为所选场景建立项目内副本；不调用模型，不覆盖原文件。")
            self._save(run)
            self._start(run, self._import_scene)
            return copy.deepcopy(run)

    def _import_scene(self, identifier):
        from .runtime import AppError
        from .native_candidate import verify_source
        try:
            if self._cancelled(identifier):
                return
            run = self.get(identifier)
            specification = run["import_source"]
            native = specification.get("native_candidate")
            if native is not None:
                # Persist a once-only external-write admission before Blender.
                # SQL prepared alone never proves the external fork did not run.
                with self.lock:
                    stored = self._get(identifier)
                    operation_id = uuid.uuid5(uuid.UUID(stored["project_id"]),
                                              f"{identifier}:initial:None").hex
                    state = self._projects().state_store(stored["project_id"])
                    prior = state.lookup_receipt(operation_id)
                    if prior["status"] == "committed":
                        if prior.get("artifact_status") != "available":
                            raise AppError("已提交native产物不可用，不能重放导入。", "native_intake_unknown", 409)
                        self._update(identifier, "completed", "已按SQL回执恢复native导入；没有重放Blender。",
                                     status="completed", quality_status="needs_user_review", error=None,
                                     native_intake={"state": "completed", "source": copy.deepcopy(native)})
                        return
                    admission = stored.get("native_intake") or {}
                    if admission.get("source") != native:
                        raise AppError("native导入来源身份不一致。", "native_candidate_changed", 409)
                    if admission.get("state") != "admitted":
                        return  # Already writing/failed/interrupted: no second dispatch.
                    if prior["status"] != "missing":
                        error = AppError("native操作已经准备或结果未知，不能重复fork。", "native_intake_unknown", 409)
                        error.outcome_unknown = True
                        raise error
                    verify_source(native)
                    self._scene_operation(identifier, None, "initial")
                    stored["native_intake"]["state"] = "writing"
                    self._save(stored)
            workspace = self.run_directory(identifier) / "candidate"
            adapter = self._adapter_for(run)
            self._scene_operation(identifier, None, "initial")
            if run["execution"]["mode"] == "attached":
                self._update(identifier, "importing", "Live 已连接；普通导入场景以隔离文件后台处理，并在连接窗口展示候选，原打开文件不被替换。")
            if native is not None:
                source, _ = verify_source(native)
                adapter.fork_candidate(source, workspace)
                verify_source(native)
            elif specification["selection"]:
                version = self.scene_version(**specification["selection"])
                adapter.fork_candidate(version["workspace"], workspace)
                self.scene_version(**specification["selection"])
                if version["run"].get("reference") or version["run"].get("available_reference"):
                    self._copy_reference(version["run"], identifier, activate=bool(version["run"].get("reference")))
            else:
                adapter.import_scene(specification["path"], workspace, scene_name=specification["scene_name"])
            if self._cancelled(identifier):
                return
            for filename, kind in (("plan.json", "plan"), ("scene.blend", "blend")):
                self._artifact(identifier, workspace / filename, kind)
            self._update(identifier, "rendering", "已建立保真的场景副本，正在生成当前场景的比较视角。")
            adapter.render(workspace)
            for view in ("main", "auxiliary"):
                self._artifact(identifier, workspace / (view + ".png"), "render", view_id=view)
            self._artifact(identifier, workspace / "inspection.json", "inspection")
            if self._cancelled(identifier):
                return
            plan = json.loads((workspace / "plan.json").read_text(encoding="utf-8"))
            self.commit_initial_working_scene(identifier, None)
            self._update(identifier, "completed", "场景已导入。若项目此前没有场景，此副本已成为 Working Scene；原文件保持不变。",
                         status="completed", quality_status="needs_user_review", scene_kind=plan.get("scene_kind", "parametric"),
                         **({"native_intake": {"state": "completed", "source": copy.deepcopy(native)}} if native is not None else {}))
        except Exception as error:
            self._fail(identifier, error)
