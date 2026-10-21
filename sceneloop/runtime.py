"""Persistent, explicitly adopted concept-to-scene jobs. No automatic paid retries."""
from __future__ import annotations

import base64
import copy
from contextlib import nullcontext
import hashlib
import json
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import blender_adapter, context_usage, providers, response_diagnostics
from .config_store import ProfileError, normalize_brain_settings
from .credentials import CredentialError
from .visual_agent import VisualAgent, limits_for
from .project_runtime import ProjectRunMixin, file_sha256, is_link
from .project_store import ProjectError
from .project_state_store import StateStoreError
from .refinement_plan import RefinementError
from .refinement_round import MODE as LOCAL_ROUND_MODE, RoundError, enabled as local_round_enabled, validate_resume
from .refinement_budget import budget_progress, new_snapshot, policy_preview, resolve_snapshot
from .prompts import build_concept_prompt

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
PROFILE_FIELDS = ("id", "name", "kind", "provider_id", "model", "base_url", "credential_env", "credential_mode", "mode", "python_path", "model_dir")


def now():
    return datetime.now(timezone.utc).isoformat()


class AppError(Exception):
    def __init__(self, message, code="invalid_request", status=400):
        super().__init__(message)
        self.code, self.status = code, status


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    phase, attempts = 'create_directory', 0
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        phase = 'serialize'
        serialized = json.dumps(value, ensure_ascii=False, indent=2)
        phase = 'write_temporary'
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(serialized)
        phase = 'replace'
        # Retry this local, already serialized replacement only. Windows error 5
        # can also mean persistent ACL denial, so it remains ambiguous on failure.
        for delay in (0, .04, .08, .16):
            if delay: time.sleep(delay)
            attempts += 1
            try:
                temporary.replace(path)
                break
            except OSError as error:
                if getattr(error, 'winerror', None) not in {5, 32, 33} or attempts == 4:
                    raise
    except Exception as error:
        error.json_persistence = {'phase': phase, 'replace_attempts': attempts,
                                  'target': 'run_json' if path.name == 'run.json' else 'json_artifact'}
        raise
    finally:
        try: temporary.unlink(missing_ok=True)
        except OSError: pass  # Preserve the original failure, never mask it with cleanup.


def _normalize_request_profiles(value, operation):
    original = value.get("profiles")
    if not isinstance(original, dict):
        raise AppError("请选择已保存的大脑和生图配置。")
    profiles = {}
    for kind in ("brain", "image", "asset3d"):
        item = original.get(kind)
        if operation == "edit" and kind in {"image", "asset3d"}:
            if isinstance(item, dict) and any(field in item for field in ("api_key", "secret", "_resolved_credential")):
                raise AppError("任务只接受已保存配置引用，不能包含密钥。", "credential_in_task")
            profiles[kind] = None
            continue
        if kind == "asset3d" and item is None:
            profiles[kind] = None
            continue
        if not isinstance(item, dict) or item.get("kind") != kind or item.get("is_draft"):
            raise AppError(f"{kind} 配置未选择或尚未补全。", "configuration_required")
        if any(field in item for field in ("api_key", "secret", "_resolved_credential")):
            raise AppError("任务只接受已保存配置引用，不能包含密钥。", "credential_in_task")
        if any(not isinstance(item.get(field, ""), str) or len(item.get(field, "")) > 2048 for field in PROFILE_FIELDS):
            raise AppError("配置字段类型或长度无效。")
        profiles[kind] = {field: item.get(field, "").strip() for field in PROFILE_FIELDS}
        try:
            profiles[kind].update(normalize_brain_settings(item, kind))
        except ProfileError as error:
            raise AppError(str(error), "invalid_brain_settings") from None
        profiles[kind]["revision"] = item.get("revision", 1) if type(item.get("revision", 1)) is int else 1
        credential_mode = profiles[kind]["credential_mode"] or ("env" if profiles[kind]["credential_env"] else "none")
        if credential_mode not in {"stored", "env", "none"}:
            raise AppError("密钥保存方式无效。", "invalid_credential_mode")
        if credential_mode == "stored" and (type(item.get("revision")) is not int or item["revision"] < 1):
            raise AppError("已保存配置缺少有效修订号，请重新选择。", "invalid_revision")
        profiles[kind]["credential_mode"] = credential_mode
    return profiles


def _normalize_request_budget(value, operation):
    budget = value.get("budget", {})
    if not isinstance(budget, dict):
        raise AppError("预算格式无效。")
    limits = {}
    for field, default, lower, upper in (("max_image_generations", 1, 1, 50), ("max_asset3d_generations", 0, 0, 50), ("max_retries", 0, 0, 10)):
        if operation == "edit":
            limits[field] = 0
            continue
        amount = budget.get(field, default)
        if type(amount) is not int or not lower <= amount <= upper:
            raise AppError(f"{field} 需要是 {lower}–{upper} 的整数。")
        limits[field] = amount
    amount = budget.get("limit")
    if amount is not None and (type(amount) not in (float, int) or not 0 < amount <= 100000):
        raise AppError("预算金额需要大于 0，且不超过 100,000。")
    if budget.get("currency", "CNY") not in ("CNY", "USD"):
        raise AppError("暂仅支持 CNY 或 USD 预算记录。")
    return budget, limits, amount


def _normalize_request_iteration(value, operation, project_id):
    iteration = value.get("iteration", {"enabled": False, "max_rounds": 3})
    if not isinstance(iteration, dict) or set(iteration) - {"enabled", "max_rounds", "mode"} or type(iteration.get("enabled", False)) is not bool:
        raise AppError("自动迭代设置格式无效。")
    round_mode = iteration.get("mode")
    if round_mode != LOCAL_ROUND_MODE:
        try:
            limits_for(iteration.get("max_rounds", 3))
        except ValueError as error:
            raise AppError(str(error)) from None
    if round_mode is not None and round_mode != LOCAL_ROUND_MODE:
        raise AppError("未知的局部轮模式。", "invalid_round_mode")
    if round_mode == LOCAL_ROUND_MODE:
        if (operation != "edit" or project_id is None or iteration.get("enabled") is not True
                or type(iteration.get("max_rounds", 3)) is not int
                or not 1 <= iteration.get("max_rounds", 3) <= 12):
            raise AppError("局部轮仅支持已有Working的项目修改；保存目标需为1–12。", "invalid_round_mode")
        iteration = {"mode": LOCAL_ROUND_MODE, "enabled": True,
                     "max_rounds": iteration.get("max_rounds", 3)}
    else:
        iteration = {"enabled": iteration.get("enabled", False), "max_rounds": iteration.get("max_rounds", 3)}
        if operation == "edit":
            iteration = {"enabled": True, "max_rounds": iteration["max_rounds"] if iteration["enabled"] else 1}
    return iteration, round_mode


def _normalize_request_judge(value, operation, project_id, iteration, round_mode):
    judge_value = value.get("judge")
    default_judge_calls = (6 * iteration["max_rounds"] if round_mode == LOCAL_ROUND_MODE
                           else iteration["max_rounds"])
    judge = {"enabled": False, "mode": "follow_brain", "profile": None,
             "reasoning_effort": None, "trigger_rounds": 3,
             "max_calls": default_judge_calls}
    if judge_value is not None:
        if (not isinstance(judge_value, dict)
                or set(judge_value) - {"enabled", "mode", "profile", "reasoning_effort", "trigger_rounds", "max_calls"}):
            raise AppError("裁判设置格式无效。", "invalid_judge_settings")
        judge["enabled"] = judge_value.get("enabled", True)
        judge["mode"] = judge_value.get("mode", "follow_brain")
        judge["reasoning_effort"] = judge_value.get("reasoning_effort")
        judge["trigger_rounds"] = judge_value.get("trigger_rounds", 3)
        judge["max_calls"] = judge_value.get("max_calls", default_judge_calls)
        if (type(judge["enabled"]) is not bool or judge["mode"] not in {"follow_brain", "profile"}
                or judge["reasoning_effort"] not in {None, "none", "low", "medium", "high", "xhigh", "max"}
                or type(judge["trigger_rounds"]) is not int or not 1 <= judge["trigger_rounds"] <= 12
                or type(judge["max_calls"]) is not int or not 1 <= judge["max_calls"] <=
                (72 if round_mode == LOCAL_ROUND_MODE else 12)):
            raise AppError("裁判次数、触发轮数或思考强度无效。", "invalid_judge_settings")
        if judge["mode"] == "profile":
            item = judge_value.get("profile")
            if not isinstance(item, dict) or item.get("kind") != "brain" or item.get("is_draft"):
                raise AppError("请选择已保存的多模态大脑配置作为裁判。", "configuration_required")
            if any(field in item for field in ("api_key", "secret", "_resolved_credential")):
                raise AppError("裁判任务只接受已保存配置引用，不能包含密钥。", "credential_in_task")
            if any(not isinstance(item.get(field, ""), str) or len(item.get(field, "")) > 2048 for field in PROFILE_FIELDS):
                raise AppError("裁判配置字段类型或长度无效。")
            profile = {field: item.get(field, "").strip() for field in PROFILE_FIELDS}
            try:
                profile.update(normalize_brain_settings(item, "brain"))
            except ProfileError as error:
                raise AppError(str(error), "invalid_brain_settings") from None
            profile["revision"] = item.get("revision", 1) if type(item.get("revision", 1)) is int else 1
            profile["credential_mode"] = profile["credential_mode"] or ("env" if profile["credential_env"] else "none")
            if profile["credential_mode"] not in {"stored", "env", "none"}:
                raise AppError("裁判密钥保存方式无效。", "invalid_credential_mode")
            if profile["credential_mode"] == "stored" and (type(item.get("revision")) is not int or item["revision"] < 1):
                raise AppError("裁判配置缺少有效修订号，请重新选择。", "invalid_revision")
            judge["profile"] = profile
    if round_mode == LOCAL_ROUND_MODE and (not judge["enabled"] or not 1 <= judge["trigger_rounds"] <= 3):
        raise AppError("局部轮要求启用独立Judge，周期阈值为1–3。", "invalid_judge_settings")
    if operation == "generate" and project_id is None and judge["enabled"]:
        raise AppError("无项目的兼容接口暂不支持独立 Judge 验收；请关闭裁判配置或在项目中使用。", "judge_unsupported")
    return judge


def normalize_request(value, *, _bootstrap=False):
    if not isinstance(value, dict):
        raise AppError("任务必须是 JSON 对象。")
    prompt = value.get("prompt", "")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 16000:
        raise AppError("生成任务需要 1–16,000 字的场景需求；较长内容仍可作为本地记录保存。")
    key = value.get("idempotency_key", "")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{12,128}", key):
        raise AppError("缺少有效的任务提交标识，请刷新后重试。")
    if value.get("cost_policy") != "call_limits_only":
        raise AppError("当前无法核算货币上限。请明确选择按调用次数限制执行后启动。", "cost_policy_required")
    operation = value.get("operation", "generate")
    if operation not in ("generate", "edit"):
        raise AppError("请选择新建或修改当前场景。")
    project_id = value.get("project_id")
    if project_id is not None and (not isinstance(project_id, str) or not re.fullmatch(r"[0-9a-f]{32}", project_id)):
        raise AppError("项目标识无效。")
    expected = value.get("expected_project_revision")
    if expected is not None and (type(expected) is not int or expected < 1):
        raise AppError("项目版本无效。")
    base = value.get("base_scene")
    if operation == "edit":
        if project_id is None or expected is None or not isinstance(base, dict) or set(base) != {"run_id", "revision_id"}:
            raise AppError("修改需要项目、当前场景和预期项目版本。")
        if not isinstance(base["run_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", base["run_id"]):
            raise AppError("场景任务标识无效。")
        if base["revision_id"] is not None and (not isinstance(base["revision_id"], str) or not re.fullmatch(r"r[0-9]{3,6}", base["revision_id"])):
            raise AppError("场景版本标识无效。")
    evaluation = value.get("evaluation", {"mode": "intent" if operation == "edit" else "reference"})
    if not isinstance(evaluation, dict) or set(evaluation) != {"mode"} or evaluation["mode"] not in ("intent", "reference"):
        raise AppError("评价模式需要为意图比较或明确采用参考。")
    profiles = _normalize_request_profiles(value, 'edit' if _bootstrap else operation)
    constraints = value.get("constraints", {})
    if not isinstance(constraints, dict):
        raise AppError("项目约束格式无效。")
    constraints = {key: constraints.get(key, default) for key, default in (("style", ""), ("preserve", ""), ("key_objects_recognizable", True))}
    if any(not isinstance(constraints[key], str) or len(constraints[key]) > 4000 for key in ("style", "preserve")):
        raise AppError("设计风格或保留要求过长。")
    if constraints["key_objects_recognizable"] is not True:
        raise AppError("首轮需要启用“关键物体具有可辨识形状”。")
    budget, limits, amount = _normalize_request_budget(value, operation)
    mode = value.get("execution", {})
    if not isinstance(mode, dict) or mode.get("mode", "headless") not in ("headless", "attached"):
        raise AppError("请选择有效的 Blender 连接模式。")
    iteration, round_mode = _normalize_request_iteration(value, operation, project_id)
    judge = _normalize_request_judge(value, operation, project_id, iteration, round_mode)
    request = {"operation": operation, "project_id": project_id, "expected_project_revision": expected,
               "base_scene": copy.deepcopy(base) if operation == "edit" else None, "evaluation": evaluation, "iteration": iteration, "prompt": prompt.strip(), "profiles": profiles, "constraints": constraints,
               "judge": judge,
               "explicit_constraints": {k: copy.deepcopy(constraints[k]) for k in
                                        ("style", "preserve", "key_objects_recognizable")
                                        if k in value.get("constraints", {})},
            "budget": {**limits, "currency": budget.get("currency", "CNY"), "limit": amount},
            "execution": {"mode": mode.get("mode", "headless")}, "cost_policy": "call_limits_only", "idempotency_key": key}
    if _bootstrap:
        request['bootstrap']=True
        request['budget']['max_image_generations']=0
    if round_mode == LOCAL_ROUND_MODE:
        request["local_round_limits"] = new_snapshot(iteration["max_rounds"],
                                                      judge_max_calls=judge["max_calls"])
    if 'asset_generation' in value:
        setting = value['asset_generation']
        if (not isinstance(setting,dict) or set(setting) != {'enabled','profile_id'}
                or setting['enabled'] is not True or not isinstance(setting['profile_id'],str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',setting['profile_id'])
                or not (_bootstrap or round_mode == LOCAL_ROUND_MODE) or mode.get('mode','headless') != 'headless'):
            raise AppError('资产槽需要有效新局部轮和已保存配置。','invalid_asset_generation')
        request['asset_generation'] = copy.deepcopy(setting)
    if "max_brain_calls" in budget:
        cap = budget["max_brain_calls"]
        if (round_mode != LOCAL_ROUND_MODE or type(cap) is not int
                or not 1 <= cap <= request["local_round_limits"]["max_brain_calls"]):
            raise AppError("max_brain_calls仅允许收紧局部轮Builder额度。", "invalid_round_budget")
        request["budget"]["max_brain_calls"] = cap
        request["local_round_overrides"] = {"max_brain_calls": cap}
    if len(concept_prompt(request)) > 24000:
        raise AppError("需求与项目约束合计过长，请缩短后再生成。")
    return request


def concept_prompt(run):
    return build_concept_prompt(run)


class RunManager(ProjectRunMixin):
    def __init__(self, root, *, provider_module=None, adapter_module=None, profile_repository=None,
                 project_repository=None, asset_host=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.providers = provider_module or providers
        self.profile_repository = profile_repository
        self.project_repository = project_repository
        self.asset_host = asset_host
        self.run_directories = {}
        self.blender = adapter_module or blender_adapter
        self.lock = threading.RLock()
        self.runs = {}
        self.workers = {}
        self._handoff_finished = set()
        self.discovery = self.blender.discover_blender()
        self._load()
        self._director_host().hydrate()

    def _load(self):
        self._load_root(self.root)
        if self.project_repository is not None:
            for project in self.project_repository.list():
                if project.get("available", True):
                    try:
                        self._load_root(self.project_repository.runs_root(project["id"]), project["id"])
                    except (OSError, ValueError, ProjectError):
                        continue

    def _load_root(self, directory, project_id=None):
        waiting_parents = []
        for path in Path(directory).glob("*/run.json"):
            if (is_link(path.parent) or is_link(path) or path.parent.resolve().parent != Path(directory).resolve()
                    or path.resolve().parent != path.parent.resolve()):
                continue
            if path.parent.name in self.runs:
                continue
            try:
                run = json.loads(path.read_text(encoding="utf-8"))
                if run.get("id") != path.parent.name or not re.fullmatch(r"[0-9a-f]{32}", run["id"]):
                    continue
                if run.get("project_id") != project_id:
                    continue
                self._project_asset_run(run)
                defer_wait = (run.get('operation') == 'conversation' and run.get('agent_resource_base')
                              and run.get('status') == 'waiting_child' and not run.get('error')
                              and not run.get('cancel_requested'))
                if defer_wait:
                    waiting_parents.append((run, path))
                elif run["status"] not in TERMINAL | {"awaiting_reference", "awaiting_resume"}:
                    run["status"] = "interrupted"
                    run["error"] = {"code": "server_restarted", "message": "服务上次在执行中停止。原请求可能已计费；不会自动重发。", "outcome_unknown": True}
                    self._event(run, "interrupted", run["error"]["message"])
                    write_json(path, run)
                self.run_directories[run["id"]] = path.parent.resolve()
                self.runs[run["id"]] = run
                if run.get('agent_resource_base'):
                    self._resource_host().recover(run)
                if (run.get("error") or {}).get("code") == "server_restarted":
                    self._seal_run_handoff(run["id"])
            except (ValueError, KeyError, TypeError, OSError):
                # Unreadable files remain untouched; never silently replace them.
                continue
        # Directory order is not authority. All children and their recovery
        # intents must be loaded before a parent can be proved safely waiting.
        from .resource_parent_recovery import safe_waiting_parent
        for run, path in waiting_parents:
            if safe_waiting_parent(self, run):
                continue
            run['status'] = 'interrupted'
            run['error'] = {'code': 'server_restarted', 'message': '服务上次在执行中停止。原请求可能已计费；不会自动重发。', 'outcome_unknown': True}
            self._event(run, 'interrupted', run['error']['message'])
            write_json(path, run)
            self._seal_run_handoff(run['id'])

    def _save(self, run):
        self._project_committed_run(run)
        run["updated_at"] = now()
        write_json(self.run_directory(run["id"]) / "run.json", run)

    def _event(self, run, stage, message):
        run["stage"] = stage
        run.setdefault("events", []).append({"seq": len(run.get("events", [])) + 1, "time": now(), "stage": stage, "message": message})

    def _update(self, identifier, stage, message, **values):
        with self.lock:
            run = self._get(identifier)
            run.update(values)
            self._event(run, stage, message)
            self._save(run)

    def _project_committed_run(self, run):
        """Rebuild committed Run fields from SQL and verified legacy history."""
        if self.project_repository is None or not run.get("project_id"):
            return run
        state = self.project_repository.state_store(run["project_id"])
        try:
            legacy = state.legacy_run_projection(run["id"])
            if run.get('agent_resource_base'):
                from .agent_resources import hydrate
                with state._connection() as connection:
                    hydrate(run, state, connection=connection)
                    receipts = state.receipts_for_run(run['id'], connection=connection)
                    evaluations = state.evaluation_receipts_for_run(run['id'], connection=connection)
            else:
                receipts = state.receipts_for_run(run["id"])
                evaluations = state.evaluation_receipts_for_run(run["id"])
        except StateStoreError as error:
            raise ProjectError(str(error), error.code, error.status) from None
        committed = [row for row in receipts if row.get("kind") in {"builder", "initial"}]
        sql_rows = []
        for receipt in committed:
            if receipt["kind"] == "builder":
                row = copy.deepcopy(receipt["run_commit"])
                row.update(working_version=receipt["working_version"],
                           project_revision=receipt["project_revision"],
                           time=receipt["committed_at"])
                sql_rows.append(row)
        sql_keys = {(row["revision_id"], row["project_revision"]) for row in sql_rows}
        run["commits"] = [row for row in legacy["commits"]
                          if (row["revision_id"], row["project_revision"]) not in sql_keys]
        run["commits"].extend(sql_rows)
        initial = next((row for row in reversed(committed) if row["kind"] == "initial"), None)
        if initial:
            run["initial_working_commit"] = {
                "working_version": initial["working_version"],
                "time": initial["committed_at"],
                "operation_id": initial["operation_id"]}
        elif legacy["initial_working_commit"] is not None:
            run["initial_working_commit"] = legacy["initial_working_commit"]
        else:
            run.pop("initial_working_commit", None)
        if committed:
            latest = committed[-1]
            run["working_scene"] = copy.deepcopy(latest["target_selection"])
            run["expected_project_revision"] = latest["project_revision"]
        elif legacy["trusted"]:
            run["working_scene"] = copy.deepcopy(legacy["working_scene"])
            run["expected_project_revision"] = legacy["expected_project_revision"]
        run["scene_commit_receipts"] = [
            {"operation_id": row["operation_id"], "kind": row["kind"],
             "global_revision_id": row["global_revision_id"],
             "working_version": row["working_version"],
             "project_revision": row["project_revision"]} for row in committed]
        sql_evaluations = {
            row["evaluation_id"]: copy.deepcopy(row["projection"])
            for row in evaluations if isinstance(row.get("projection"), dict)}
        history = []
        for row in run.get("judge_history", []):
            if not isinstance(row, dict):
                continue
            evaluation_id = ((row.get("judgement") or {}).get("evaluation_id")
                             if isinstance(row.get("judgement"), dict) else None)
            if evaluation_id in sql_evaluations or row.get("input_id") in sql_evaluations:
                continue
            item = copy.deepcopy(row)
            if not item.get("input_id"):
                item["input_integrity"] = "legacy_unknown"
                item["unknown"] = True
                item.setdefault("reason", "legacy_unknown")
            history.append(item)
        history.extend(sql_evaluations.values())
        if history:
            history.sort(key=lambda item: item.get("time") or "")
            run["judge_history"] = history
            latest = history[-1]
            run["judge_status"] = latest.get("status", "not_requested")
            run["judgement"] = copy.deepcopy(latest.get("judgement"))
        if local_round_enabled(run):
            run["budget_progress"] = budget_progress(run, state)
        self._project_asset_run(run)
        return run

    def _project_asset_run(self, run):
        if not self.project_repository or not run.get("project_id"):
            return
        from .asset_run_store import AssetRunStore
        from .asset_job_store import AssetJobStore
        from .asset_provider import safe_projection
        store = AssetRunStore(self.project_repository.state_store(run["project_id"]))
        cap, doc = store.capability(run["id"]), store.request(run["id"])
        run.pop("asset_capability", None)
        run.pop("asset_request", None)
        if cap:
            run["asset_capability"] = {"generation_limit":1, "upload_limit":0,
                "profile_id":cap["profile"]["id"], "profile_revision":cap["profile"]["revision"]}
            run['budget']['max_asset3d_generations'] = 1
        elif run.get('operation') == 'edit':
            run['budget']['max_asset3d_generations'] = 0
        if doc:
            # A waiting asset request is not authority to erase a real worker
            # failure/unknown outcome. Director consumes that original result;
            # the open request and SQL attempt still prevent automatic replay.
            terminal_outcome = run.get('status') in {'completed','failed','interrupted','cancelled'}
            # A later complete resource checkpoint owns its pause. An earlier
            # asset continuation can still be started while this worker exits;
            # that claim must not project the new human wait back to running.
            resource_wait = (run.get('status') == 'awaiting_resume' and run.get('stage') == 'resource_wait'
                and (run.get('resource_request') or {}).get('continuation',{}).get('state') in {'waiting_approval','ready','blocked'})
            builder_owns_state = False
            run["asset_request"] = {k:copy.deepcopy(doc[k]) for k in
                ("id","version","request_revision","round_id","phase","closed","job_id")}
            if doc["job_id"]:
                job = AssetJobStore(store.project).read(doc["job_id"])
                run["asset_request"]["job"] = safe_projection(job)
                # Each approved local retry is a new immutable Job. Changing
                # the current attempt must never refund previous usage.
                prior=[a['document'] for a in doc.get('attempt_history',[])]
                run["usage"]["asset3d_calls"] = job["creation_attempts"] + sum(
                    AssetJobStore(store.project).read(a['job_id'])['creation_attempts'] for a in prior)
            if doc.get("import_receipt"):
                receipt = doc["import_receipt"]
                rd = store.project.local_round(run["id"],round_id=doc["round_id"])
                sql_agent = receipt['agent_state'] if doc.get('bootstrap') else rd["agent_state"]
                if (doc.get('builder_mode') and not doc['closed'] and not doc.get('import_history')
                        and doc['phase'] == 'pending_review'):
                    continuation = store.continuation(doc['id'])
                    worker = self.workers.get(run['id'])
                    builder_owns_state = bool(continuation and (
                        continuation['state'] == 'completed' or
                        (continuation['state'] == 'started' and worker and worker.is_alive())))
                known = {r['id'] for r in (run.get('agent') or {}).get('revisions',[])}
                imported = doc['import_intent']['revision_id']
                if (not doc.get('import_history') and (imported not in known or (not doc['closed'] and doc['phase'] == 'pending_review'
                        and (run.get('agent') or {}).get('pending_revision') == imported
                        and sql_agent.get('pending_revision') is None))):
                    run["agent"] = copy.deepcopy(sql_agent)
                for artifact in receipt["run_artifacts"]:
                    if not any(a["id"] == artifact["id"] for a in run["artifacts"]):
                        run["artifacts"].append(copy.deepcopy(artifact))
            elif not doc["import_intent"] and doc['phase'] in {'approval','generation','ready'}:
                run["agent"] = copy.deepcopy(doc["checkpoint"]["agent_state"])
            if (not terminal_outcome and not resource_wait and not doc["closed"] and doc['phase'] not in {'continuing','builder_continuing'}
                    and not builder_owns_state):
                run["status"], run["stage"] = "awaiting_resume", "asset_" + doc["phase"]
                run["error"] = None
            elif builder_owns_state and not resource_wait and run['status'] == 'awaiting_resume':
                # A live claimed Builder owns its active draft. Repair paused
                # projections only from an ordinary confirmed SQL checkpoint;
                # unknown/dead-started recovery retains its asset wait.
                checkpoint = (rd or {}).get('checkpoint') or {}
                if ((doc.get('bootstrap') or rd['status'] == 'active') and worker and worker.is_alive()
                        and not run.get('cancel_requested') and not run.get('error')):
                    run['status'] = 'running'
                    if run.get('stage','').startswith('asset_'):
                        run['stage'] = 'agent_tool'
                elif (rd and rd['status'] == 'paused'
                        and checkpoint.get('type') != 'asset_wait'
                        and checkpoint.get('revision_id') == rd['selected_revision']
                        and not rd['pending_revision']
                        and not any(s['status'] in {'scheduled','unknown'} for s in rd['steps'])):
                    run['status'], run['stage'] = 'awaiting_resume', 'awaiting_resume'
                    run['error'] = None
            if doc['closed'] and not terminal_outcome:
                reason = doc.get('close_reason')
                if not reason:
                    rd = store.project.local_round(run['id'],round_id=doc['round_id'])
                    reason = ('formal_saved' if rd and rd['status'] == 'committed' else
                              'cancelled' if run.get('cancel_requested') else
                              'cost_anomaly' if doc['phase'] == 'cost_anomaly' else 'user_end')
                if reason in {'cancelled','user_end','cost_anomaly'}:
                    run['status'] = 'cancelled' if reason == 'cancelled' else 'interrupted'
                    run['stage'] = 'asset_' + reason
                    run['cancel_requested'] = True

    def _close_asset_wait(self, identifier, *, reason=None, phase='ended'):
        """Local SQL stop must work after restart without a trusted asset host."""
        from .asset_run_store import AssetRunStore
        from .asset_job_store import AssetJobStore
        with self.lock:
            run = self.runs[identifier]
            if not run.get('project_id') or not self.project_repository:
                return None
            store = AssetRunStore(self.project_repository.state_store(run['project_id']))
            doc = store.close(identifier,reason=reason,phase=phase)
            if doc and doc['job_id']:
                AssetJobStore(store.project).stop_local(doc['job_id'])
            if doc:
                self._save(run)
            return doc

    def _asset_project_blocked(self, project_id, *, run_id=None):
        if not project_id or not self.project_repository:
            return False
        from .asset_run_store import AssetRunStore
        store = AssetRunStore(self.project_repository.state_store(project_id))
        if store.barrier(except_run=run_id):
            return True
        own = store.request(run_id) if run_id else None
        return bool(own and not own["closed"] and own["phase"] not in {"pending_review","continuing","builder_continuing"})

    def _assert_asset_write(self, project_id, *, run_id=None):
        if any(r.get('project_id') == project_id and r['id'] != run_id
               and (r.get('resource_request') or {}).get('continuation', {}).get('state') in {'waiting_approval', 'ready', 'running', 'blocked'}
               for r in self.runs.values()):
            raise AppError('原资源申请的同Run施工claim尚未结束。', 'resource_project_waiting', 409)
        if self._asset_project_blocked(project_id, run_id=run_id):
            raise AppError("项目正在等待资产；先续做或明确结束该等待。", "asset_project_waiting", 409)
        if self._director_write_blocked(project_id,run_id):
            raise AppError('存在未结束的可信委派写入claim。','director_project_waiting',409)

    def _director_write_blocked(self, project_id, run_id=None):
        host=getattr(self,'_director_runtime',None)
        return bool(host and host.write_blocked(project_id,run_id))

    def _asset_host(self):
        if self.asset_host is None:
            from .asset_provider import AssetJobError
            raise AssetJobError("asset_trusted_host_unavailable")
        if getattr(self.asset_host,'reference_resolver',None) is None:
            from .project_references import reference_resolver
            self.asset_host.reference_resolver=reference_resolver
        return self.asset_host

    def create_asset_run(self, value):
        self._asset_host()
        return self.create(value, _asset_opt_in=True)

    def request_asset(self, identifier, body, *, agent_state, exchange):
        return self._asset_host().request(self, identifier, body, agent_state=agent_state, exchange=exchange)

    def get_asset_request(self, identifier):
        self.get(identifier)
        if self.asset_host is None:
            return None
        return self.asset_host.public(self, identifier)

    def approve_asset_request(self, identifier, reference):
        return self._asset_host().approve(self, identifier, reference)

    def advance_asset(self, identifier):
        return self._asset_host().advance(self, identifier)

    def resume_asset(self, identifier, resume_id):
        return self._asset_host().resume(self, identifier, resume_id)

    def end_asset_wait(self, identifier):
        with self.lock:
            self._get(identifier)
            self._close_asset_wait(identifier,reason='user_end')
            result = copy.deepcopy(self._get(identifier).get('asset_request'))
        if not any(w.is_alive() for w in self.workers.values()):
            self._worker_finished(identifier)
        return result

    def _get(self, identifier):
        if identifier not in self.runs:
            raise AppError("任务不存在。", "not_found", 404)
        return self._project_committed_run(self.runs[identifier])

    def get(self, identifier):
        with self.lock:
            run = copy.deepcopy(self._get(identifier))
            if run.get('operation') == 'conversation':
                run['director'] = self._director_host().detail(run)
            return run

    def _director_host(self):
        if not hasattr(self, '_director_runtime'):
            from .director_runtime import DirectorRuntime
            self._director_runtime = DirectorRuntime(self)
        return self._director_runtime

    def _resource_host(self):
        if not hasattr(self, '_resource_runtime'):
            from .agent_resources import ResourceRuntime
            self._resource_runtime = ResourceRuntime(self)
        return self._resource_runtime

    def get_resource_request(self, identifier):
        with self.lock:
            return self._resource_host().view(identifier)

    def resource_action(self, identifier, action, body, *, source):
        return self._resource_host().action(identifier, action, body, source=source)

    def director_action(self, identifier, action, body):
        with self.lock:
            return self._director_host().action(identifier,action,body)

    def director_action_receipt(self, identifier, action_id, query):
        if (set(query) != {'project_id','task_id','action'}
                or query['action'] not in {'recover','resume','policy'}
                or any(not isinstance(query[key],str) or not re.fullmatch(r'[0-9a-f]{32}',query[key])
                       for key in ('project_id','task_id')) or not re.fullmatch(r'[0-9a-f]{32}',action_id)):
            raise AppError('原动作只读查询身份无效。','invalid_query')
        with self.lock:
            return self._director_host().action_receipt(identifier,action_id,query)

    def director_timeline(self, identifier, *, before=None, limit=40):
        with self.lock:
            run=self._get(identifier)
            if run.get('operation')!='conversation':raise AppError('过程只属于Director父请求。','director_unbound',409)
            from .director_timeline import page
            return page(self._director_host().store(run['project_id']),identifier,self.runs,before=before,limit=limit)

    def list(self, project_id=None):
        with self.lock:
            if project_id is not None:
                self.get_project(project_id)
            selected = [run for run in self.runs.values() if project_id is None or run.get("project_id") == project_id]
            return [copy.deepcopy(self._project_committed_run(run)) for run in sorted(
                selected, key=lambda run: run["created_at"], reverse=True)[:100]]

    @staticmethod
    def _run_read_cursor(project_id, boundary):
        raw = json.dumps([project_id, *boundary], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def list_page(self, project_id=None, *, limit=100, before=None):
        """Read an immutable time/id boundary; concurrent newer Runs stay above it."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AppError("历史查询数量必须为1–100。", "invalid_limit")
        if project_id is not None and (not isinstance(project_id, str) or not re.fullmatch(r"[0-9a-f]{32}", project_id)):
            raise AppError("项目筛选无效。", "invalid_project")
        with self.lock:
            if project_id is not None:
                self.get_project(project_id)
            boundary = None
            if before is not None:
                try:
                    if not isinstance(before, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,2048}", before):
                        raise ValueError("invalid cursor encoding")
                    raw = base64.b64decode(before + "=" * (-len(before) % 4), altchars=b"-_", validate=True)
                    value = json.loads(raw.decode("utf-8"))
                    if (not isinstance(value, list) or len(value) != 3 or value[0] != project_id
                            or not isinstance(value[1], str) or not 1 <= len(value[1]) <= 80
                            or not isinstance(value[2], str) or not re.fullmatch(r"[0-9a-f]{32}", value[2])):
                        raise ValueError("invalid cursor boundary")
                    boundary = tuple(value[1:])
                    anchor = self.runs.get(boundary[1])
                    if (before != self._run_read_cursor(project_id, boundary) or anchor is None
                            or anchor.get("created_at") != boundary[0]
                            or (project_id is not None and anchor.get("project_id") != project_id)):
                        raise ValueError("cursor does not belong to this history")
                except (ValueError, UnicodeDecodeError):
                    raise AppError("历史分页游标无效，请重新读取历史。", "invalid_cursor") from None
            selected = [run for run in self.runs.values()
                        if (project_id is None or run.get("project_id") == project_id)
                        and (boundary is None or (run["created_at"], run["id"]) < boundary)]
            selected.sort(key=lambda run: (run["created_at"], run["id"]), reverse=True)
            has_more = len(selected) > limit
            rows = selected[:limit]
            cursor = self._run_read_cursor(project_id, (rows[-1]["created_at"], rows[-1]["id"])) if has_more else None
            return {"runs": [copy.deepcopy(self._project_committed_run(run)) for run in rows],
                    "page": {"limit": limit, "has_more": has_more, "next_cursor": cursor}}

    def find_by_idempotency_key(self, project_id, key):
        """Resolve an accepted submission in its project, with no dispatch or replay."""
        if not isinstance(project_id, str) or not re.fullmatch(r"[0-9a-f]{32}", project_id):
            raise AppError("提交恢复查询需要有效项目。", "invalid_project")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{12,128}", key):
            raise AppError("提交标识无效。", "invalid_idempotency_key")
        with self.lock:
            self.get_project(project_id)
            rows = [run for run in self.runs.values()
                    if run.get("project_id") == project_id and run.get("idempotency_key") == key]
            rows.sort(key=lambda run: (run["created_at"], run["id"]), reverse=True)
            return [copy.deepcopy(self._project_committed_run(run)) for run in rows[:1]]

    def health(self):
        from .agent_resources import BOOTSTRAP
        modes = {}
        for mode in ("headless", "attached"):
            try:
                adapter = self.blender.create_adapter(mode=mode, blender_path=self.discovery.get("path"))
                cap = adapter.capabilities
                if callable(cap):
                    cap = cap()
                available = bool(cap.get("available", self.discovery.get("available") if mode == "headless" else False))
                modes[mode] = {"available": available, "reason": cap.get("reason", cap.get("error", ""))}
            except Exception:
                modes[mode] = {"available": False, "reason": "连接尚不可用。"}
        return {"ok": True, "blender": {**self.discovery, "modes": modes},
                "capabilities": {"cancel": True, "new_scene": True, "existing_scene_edit": True, "asset3d_in_pipeline": False,
                                 "monetary_budget": False, "paid_auto_retry": False, "visual_agent": True, "projects": self.project_repository is not None,
                                 "continue_scene": True, "import_scene": hasattr(self.blender.BlenderAdapter if hasattr(self.blender, "BlenderAdapter") else object, "import_scene")},
                "call_limits": {"image": 1, "brain": 1, "asset3d": 0, "retries": 0},
                "visual_agent_defaults": limits_for(3),
                "local_round_budget_policy": policy_preview(),
                "agent_resource_policy": {"schema_version": 1, "policy_id": "agent-resources-v1",
                    "bootstrap": BOOTSTRAP.copy(),
                    "initial_publications": 1}}

    def _execution_profile(self, profile):
        """Resolve a bound secret into a short-lived copy, never the saved run."""
        resolved = dict(profile)
        if profile.get("credential_mode") == "stored":
            if self.profile_repository is None:
                raise AppError("本地服务未连接密钥配置库，请重启后端。", "configuration_unavailable", 503)
            resolved["_resolved_credential"] = self.profile_repository.resolve_key(profile)
        return resolved

    def _preflight(self, request, kinds=("brain", "image")):
        for kind in kinds:
            self.providers.validate_profile(self._execution_profile(request["profiles"][kind]), kind)
        if request.get("project_id"):
            self._project_environment(request)
            return
        mode = request["execution"]["mode"]
        mode_info = self.health()["blender"]["modes"][mode]
        if not mode_info["available"]:
            raise AppError(f"Blender {mode} 尚不可用。{mode_info.get('reason', '')}", "blender_unavailable")

    def _judge_profile(self, run):
        settings = run.get("judge") or {}
        profile = copy.deepcopy(settings.get("profile") if settings.get("mode") == "profile" else run["profiles"]["brain"])
        if not profile:
            raise AppError("裁判配置不可用。", "configuration_required")
        # Validate the stored snapshot before applying a per-run execution override.
        profile = self._execution_profile(profile)
        effort = settings.get("reasoning_effort")
        if effort is not None:
            profile["reasoning_effort"] = effort
            profile["thinking_budget"] = None
        return profile

    def _assert_idle(self):
        if (self._scene_workers_active()
                or any(run.get("queue_state") == "waiting" and run.get("status") == "queued"
                       and not self._asset_project_blocked(run.get('project_id'))
                       for run in self.runs.values())):
            raise AppError("已有任务正在执行，请等待完成或停止后续步骤。", "busy", 409)

    def _scene_workers_active(self, *, exclude=None):
        return any(thread.is_alive() and rid!=exclude and not (
            hasattr(self,'_director_runtime') and self._director_runtime.is_readonly(self.runs[rid]))
            for rid,thread in self.workers.items())

    def _start(self, run, target):
        self._handoff_finished.discard(run["id"])
        def runner():
            try:
                target(run["id"])
            finally:
                self._worker_finished(run["id"])
        thread = threading.Thread(target=runner, daemon=True, name=f"sceneloop-{run['id'][:8]}")
        self.workers[run["id"]] = thread
        thread.start()

    def _seal_run_handoff(self, identifier):
        """Auxiliary only: read the durable terminal result, then seal own experience."""
        from .run_handoff import outgoing
        try:
            with self.lock:
                run = self._get(identifier)
                if run.get('operation') == 'conversation':
                    return
                if not run.get("project_id") or run.get("status") not in TERMINAL:
                    return
                path = self.run_directory(identifier) / "run.json"
                durable = json.loads(path.read_text(encoding="utf-8"))
                if durable.get("status") not in TERMINAL or durable.get("updated_at") != run.get("updated_at"):
                    return
                self._project_committed_run(durable)
                project = self._projects().get(run["project_id"])
                sealed = outgoing(durable, project, now())
                durable.setdefault("run_handoff", {})["outgoing"] = sealed
                write_json(path, durable)
                run.setdefault("run_handoff", {})["outgoing"] = sealed
        except Exception:
            # Do not undo a committed Working, mask terminal errors or block the queue.
            return

    def _freeze_run_handoff(self, run):
        from .run_handoff import freeze, empty_incoming, predecessor, scene_provenance
        holder = run.get("run_handoff")
        if holder is not None and (not isinstance(holder, dict) or "incoming" in holder):
            return
        timestamp = now()
        project = None
        try:
            project = self._projects().get(run["project_id"])
            previous = [item for item in self.runs.values()
                        if item["id"] != run["id"] and item.get("project_id") == run["project_id"]
                        and item.get('operation') != 'conversation'
                        and item.get("status") in TERMINAL
                        and (item["id"] in self._handoff_finished or not
                             (self.workers.get(item["id"]) and self.workers[item["id"]].is_alive()))]
            nearest = predecessor(previous)
            if nearest is not None:
                # The sealed file, not a stale in-memory copy, is the source.
                try:
                    persisted = json.loads((self.run_directory(nearest["id"]) / "run.json").read_text(encoding="utf-8"))
                    if persisted.get("id") != nearest["id"] or persisted.get("project_id") != run["project_id"] or persisted.get("status") not in TERMINAL:
                        raise ValueError("predecessor identity changed")
                    previous = [persisted]
                except (OSError, ValueError, TypeError):
                    previous = [{**nearest, "run_handoff": {}}]
            incoming = freeze(run, project, previous, timestamp)
            durable_path = self.run_directory(run["id"]) / "run.json"
            durable = json.loads(durable_path.read_text(encoding="utf-8"))
            durable.setdefault("run_handoff", {})["incoming"] = incoming
            durable["run_handoff"]["activation_scene"] = scene_provenance(project)
            write_json(durable_path, durable)
        except Exception:
            incoming = empty_incoming("freeze_unavailable", timestamp)
        # Keep the failure frozen too; a later request must not quietly pick another source.
        run.setdefault("run_handoff", {})["incoming"] = incoming
        if project is not None:
            run["run_handoff"]["activation_scene"] = scene_provenance(project)

    def _worker_finished(self, identifier):
        if not ((self.runs[identifier].get("asset_request") or {}).get("closed") is False):
            self._handoff_finished.add(identifier)
        self._seal_run_handoff(identifier)
        if hasattr(self, '_director_runtime'):
            try:
                with self.lock:
                    if self._director_runtime.finished(identifier):
                        return
            except Exception as error:
                # Preserve the child's durable outcome; report parent scheduling failure.
                record = self._director_runtime.binding(identifier,self.runs[identifier].get('project_id'))
                if record:
                    self._fail(record['parent_id'], error)
                elif self.runs[identifier].get('operation') == 'conversation':
                    self._fail(identifier,error)
                return
        queued = None
        with self.lock:
            waiting = sorted((run for run in self.runs.values()
                              if run.get("status") == "queued" and run.get("queue_state") == "waiting"
                              and not run.get("cancel_requested")
                              and not self._asset_project_blocked(run.get("project_id"))
                              and not self._director_write_blocked(run.get('project_id'))), key=lambda item: item["created_at"])
            if waiting:
                queued = waiting[0]
                queued["queue_state"] = "starting"
                self._event(queued, "queued", "前一条场景写入已结束；正在基于最新 Working Scene 启动排队请求。")
                self._save(queued)
        if queued is not None:
            self._start(queued, self._queued_edit)

    def _queued_edit(self, identifier):
        try:
            if self._cancelled(identifier):
                return
            with self.lock:
                run = self._get(identifier)
                if self._asset_project_blocked(run["project_id"]) or self._director_write_blocked(run['project_id']):
                    run.update(status="queued", queue_state="waiting")
                    self._save(run)
                    return
                project = self.get_project(run["project_id"])
                current = project.get("current_scene")
                if not current:
                    raise AppError("排队请求启动时没有可用 Working Scene。", "working_scene_missing", 409)
                run["base_scene"] = {"run_id": current["run_id"], "revision_id": current.get("revision_id")}
                run["working_scene"] = copy.deepcopy(run["base_scene"])
                run["expected_project_revision"] = project["revision"]
                run["scene_id"] = current["scene_id"]
                run["queue_state"] = "running"
                version = self.scene_version(**run["base_scene"])
                run["design_context"] = self._design_context(version["run"])
                self._event(run, "queued", f"排队请求已绑定 Working Version {project['workspace']['working_version']}。")
                self._save(run)
            self._edit_scene(identifier)
        except Exception as error:
            self._fail(identifier, error)

    def create(self, value, *, _asset_opt_in=False, _director_delegation=None):
        if isinstance(value,dict) and value.get('operation') == 'conversation':
            return self._director_host().create(value)
        bootstrap=bool(_director_delegation and _director_delegation['brief'].get('bootstrap'))
        request = normalize_request(value,_bootstrap=bootstrap)
        _asset_opt_in = _asset_opt_in or bool(request.get('asset_generation'))
        if _asset_opt_in:
            self._asset_host()
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self.lock:
            for run in self.runs.values():
                if run["idempotency_key"] == request["idempotency_key"]:
                    matches_legacy = ("explicit_constraints" not in run and run["request_fingerprint"] ==
                        hashlib.sha256(json.dumps({k: v for k, v in request.items() if k != "explicit_constraints"},
                                                  sort_keys=True, ensure_ascii=False).encode()).hexdigest())
                    if run["request_fingerprint"] != fingerprint and not matches_legacy:
                        raise AppError("此提交标识已用于另一份需求。", "idempotency_conflict", 409)
                    if _asset_opt_in and not run.get("asset_capability"):
                        raise AppError("历史Run不能补发资产额度。", "asset_host_opt_in_required", 409)
                    return copy.deepcopy(run)
            active = self._scene_workers_active()
            if _director_delegation is not None:
                trusted = self._director_host().binding(_director_delegation['child_id'],request['project_id'])
                from .director_contract import digest
                if (trusted != _director_delegation or trusted['state'] != 'reserved'
                        or trusted.get('submission_digest') != digest(request)):
                    raise AppError('委派SQL身份或提交内容失效。','director_unbound',409)
                active = False
                if any(r['purpose']=='scene' for r in trusted['brief'].get('reference_snapshots',[])):
                    request['project_scene_reference']=True
            blocked = self._asset_project_blocked(request.get("project_id")) or self._director_write_blocked(
                request.get('project_id'),_director_delegation['child_id'] if _director_delegation else None)
            if blocked and request["operation"] != "edit":
                self._assert_asset_write(request["project_id"])
            active = active or blocked
            if active and request["operation"] != "edit":
                raise AppError("已有任务正在执行；新场景任务需等待当前写入结束。", "busy", 409)
            project, base_version = self._project_request(request)
            self._preflight(request, kinds=("brain",) if request["operation"] == "edit" or bootstrap else ("brain", "image"))
            if request["judge"]["enabled"]:
                self.providers.validate_profile(self._judge_profile(request), "brain")
            identifier = _director_delegation['child_id'] if _director_delegation else uuid.uuid4().hex
            run = {**request, "id": identifier, "request_fingerprint": fingerprint, "created_at": now(), "status": "queued" if active else "running",
                   "stage": "queued", "events": [], "artifacts": [], "cancel_requested": False,
                   "usage": {"image_calls": 0, "brain_calls": 0, "judge_calls": 0, "judge_tool_calls": 0,
                             "asset3d_calls": 0, "retries": 0, "cost": None},
                   "quality_status": "not_evaluated", "reference": None, "round": 1,
                   "scene_id": project["current_scene"]["scene_id"] if base_version else uuid.uuid4().hex,
                   "queue_state": "waiting" if active else "running"}
            if project is not None and run.get("expected_project_revision") is None:
                run["expected_project_revision"] = project["revision"]
            if base_version:
                run["design_context"] = self._design_context(base_version["run"])
                run["scene_kind"] = base_version["plan"].get("scene_kind", "parametric")
            if _director_delegation:
                run['director_delegation'] = copy.deepcopy(_director_delegation)
                continuation=_director_delegation['brief'].get('creative_continuation')
                if continuation:run['scene_id']=continuation['source']['scene_id']
                run['reference_snapshots']=copy.deepcopy(_director_delegation['brief'].get('reference_snapshots',[]))
                if _asset_opt_in and _director_delegation['brief'].get('creation_authorization',{}).get('approved',{}).get('local_profile_snapshot'):
                    from .director_creation import ensure_local_authority
                    run['local_asset_authorization']=ensure_local_authority(self._director_host(),self.runs[_director_delegation['parent_id']])
            from .agent_resources import freeze as freeze_resources
            if run.get('project_id'):
                freeze_resources(run, self._projects().state_store(run['project_id']))
            message = (f"任务已建立：生图最多1次；采用后最多 {request['iteration']['max_rounds']} 轮修改、{limits_for(request['iteration']['max_rounds'])['max_brain_calls']} 次大脑调用，不自动重试。"
                       if request["iteration"]["enabled"] and not local_round_enabled(run) else "任务已建立：最多 1 次生图、1 次看图规划，不自动重试。费用以服务商账单为准。")
            if run.get('bootstrap') and run.get('agent_resource_base'):
                from .agent_resources import project as resource_budget
                from .agent_resource_store import ResourceStore
                state = self._projects().state_store(run['project_id'])
                frozen = ResourceStore(state).base(identifier)
                vector = {r['resource']: r['effective_limit'] for r in resource_budget(run, state)['resources'] if r['scope'] == 'run'}
                message = (f"初建候选：最多 {vector['builder_calls']} 次 Builder 调用、{vector['draft_attempts']} 次编辑草稿尝试、"
                           f"{vector['judge_calls']} 次 Judge；初版发布最多 {frozen['initial_publications']} 次。候选检查点不结束或发布。")
            if request["operation"] == "edit":
                local_limits = resolve_snapshot(run) if local_round_enabled(run) else None
                message = (f"局部草稿轮：最多 {local_limits['saved_round_limit']} 次正式保存，"
                           f"每轮最多 {local_limits['max_edit_attempts_per_round']} 次草稿尝试，"
                           f"本Run最多 {local_limits['max_edit_attempts']} 次；"
                           "草稿修改不提前改变Working。"
                           if local_round_enabled(run) else
                           f"修改当前场景：不重新生图，最多 {run['iteration']['max_rounds']} 轮修改；每个成功修改轮会自动提交为 Working Scene。")
            self._event(run, "queued", message)
            if request["profiles"].get("asset3d"):
                self._event(run, "queued", "本轮使用参数化白模工具；已选的 3D 生成配置保留，本轮不调用。")
            self.runs[identifier] = run
            self.run_directories[identifier] = (self.project_repository.runs_root(run["project_id"]) if project else self.root) / identifier
            if _asset_opt_in:
                try:
                    self._asset_host().freeze(self, run)
                except Exception:
                    self.runs.pop(identifier, None)
                    self.run_directories.pop(identifier, None)
                    raise
            self._save(run)
            if active:
                self._event(run, "queued", "已有场景写入正在执行；本请求已排队，启动前会重新读取最新 Working Scene。")
                self._save(run)
            elif _director_delegation is None:
                self._start(run, self._edit_scene if request["operation"] == "edit" else self._concept)
            return copy.deepcopy(run)

    def _reserve_call(self, identifier, kind):
        with self.lock:
            run = self._get(identifier)
            if kind == 'asset3d':
                raise AppError('资产创建必须经过宿主批准和SQL作业gate。','asset_host_only',409)
            self._assert_asset_write(run.get('project_id'),run_id=identifier)
            if self._cancelled(identifier):
                return False
            self._verify_project_baseline(run)
            counter = f"{kind}_calls"
            run["usage"].setdefault(counter, 0)
            resource = 'builder_calls' if kind == 'brain' else 'judge_calls' if kind == 'judge' else None
            row = next((r for r in (run.get('resource_budget') or {}).get('resources', [])
                        if r['resource'] == resource and r['scope'] == 'run'), None)
            if row:
                cap = row['effective_limit']
            elif kind == "judge":
                cap = (resolve_snapshot(run)["max_judge_calls"] if local_round_enabled(run)
                       else (run.get("judge") or {}).get("max_calls", run["iteration"]["max_rounds"]))
            elif kind == "brain" and local_round_enabled(run):
                cap = resolve_snapshot(run)["max_brain_calls"]
            elif kind == "brain" and run.get("iteration", {}).get("enabled"):
                cap = limits_for(run["iteration"]["max_rounds"])["max_brain_calls"]
            else:
                cap = 1
            if run["usage"][counter] >= cap:
                raise AppError("本轮调用次数已用完；不会重复提交。", "call_limit")
            run["usage"][counter] += 1
            self._save(run)
            return True

    def _artifact(self, identifier, path, kind, name=None, metadata=None, *, revision_id=None, view_id=None, observation_id=None, observation=None):
        path = Path(path).resolve()
        directory = self.run_directory(identifier)
        if not path.is_relative_to(directory) or not path.is_file():
            raise AppError("产物路径无效。", "invalid_artifact", 500)
        relative = path.relative_to(directory).as_posix()
        item = {"id": uuid.uuid4().hex, "kind": kind, "name": name or path.name, "path": relative,
                "url": f"/api/runs/{identifier}/artifacts/{relative}", "size": path.stat().st_size,
                "sha256": file_sha256(path)}
        for key, value in (("revision_id", revision_id), ("view_id", view_id), ("observation_id", observation_id)):
            if value is not None:
                item[key] = value
        if observation is not None:
            from .observation_contract import validate_observation_metadata
            item["observation"] = copy.deepcopy(validate_observation_metadata(observation))
        if metadata:
            item["source"] = {key: metadata[key] for key in ("provider_id", "model", "mime_type", "request_id") if key in metadata}
        with self.lock:
            self._get(identifier)["artifacts"].append(item)
            self._save(self._get(identifier))
        return item

    def _cancelled(self, identifier):
        with self.lock:
            run = self._get(identifier)
            if not run["cancel_requested"]:
                return False
            run["status"] = "cancelled"
            self._event(run, "cancelled", "已停止后续步骤；已提交的请求可能已计费，已返回的产物仍保留。")
            self._save(run)
            return True

    def _fail(self, identifier, error):
        known = isinstance(error, (AppError, RefinementError, RoundError, ProfileError, CredentialError, ProjectError, self.providers.ProviderError, blender_adapter.BlenderError))
        message = str(error) if known else "本地执行失败，已有产物已保留。请检查 Blender 环境和任务计划。"
        unknown = bool(getattr(error, "outcome_unknown", False))
        details = {"code": getattr(error, "code", "execution_failed"), "message": message, "outcome_unknown": unknown}
        if not known:
            from .local_diagnostics import describe
            details['local_diagnostics'] = describe(error)
            if isinstance(error, OSError):
                details['code'] = 'local_io_failed'
                message = '本地文件保存或读取失败；已有场景和产物已保留，未自动重做施工。'
                details['message'] = message
        pending = getattr(error, "pending_candidate", None)
        if isinstance(pending, dict):
            details["pending_candidate"] = {key: pending[key] for key in
                                            ("run_id", "revision_id", "workspace", "operations")
                                            if key in pending}
        if getattr(error, "outcome", None) == "not_scheduled" or getattr(error, "scheduled", True) is False:
            # The request never reached Blender: express that no write happened.
            details["scheduled"] = False
            forked = getattr(error, "forked_candidate", None)
            if isinstance(forked, dict):
                details["forked_candidate"] = {key: forked[key] for key in
                                               ("run_id", "revision_id", "workspace") if key in forked}
        if isinstance(error, self.providers.ProviderError):
            diagnostics = self._response_diagnostics(identifier, error)
            if diagnostics:
                details["diagnostics"] = diagnostics
            if type(getattr(error, "http_status", None)) is int:
                details["http_status"] = error.http_status
        self._update(identifier, "interrupted" if unknown else "failed", message,
                     status="interrupted" if unknown else "failed", error=details)

    def _agent_step(self, identifier, profile, messages, tools, *, role, timeout):
        """Observe numeric request usage without changing provider or budget contracts."""
        capture = nullcontext()
        index = None
        try:
            counter = "director_calls" if role == "director" else "judge_calls" if role == "judge" else "brain_calls"
            with self.lock:
                index = self._get(identifier)["usage"][counter]
            capture = context_usage.Capture(self.run_directory(identifier), identifier, role, index)
        except Exception:
            pass
        ledger = None
        with self.lock:
            observed_run = self.runs[identifier]
            if observed_run.get('operation') == 'conversation' or observed_run.get('director_delegation'):
                ledger = self._director_host().model_attempt(observed_run,role,index,profile)
        started = time.perf_counter()
        caught = None
        try:
            with capture:
                return self._captured_agent_step(identifier,profile,messages,tools,role,index,timeout)
        except Exception as error:
            caught = error
            raise
        finally:
            if ledger:
                usage = getattr(capture,'record',None)
                try:
                    # A completely received zero-tool length response is a
                    # known metered response, not an unknown external request.
                    # Missing usage still retains the original conservative
                    # settlement barrier. Other errors are unchanged.
                    known_length = (isinstance(caught, providers.OutputLengthExhausted)
                        and not caught.outcome_unknown and isinstance(usage, dict)
                        and usage.get('input_source') == 'response.usage.prompt_tokens'
                        and type(usage.get('input_tokens')) is int
                        and type(usage.get('completion_tokens')) is int)
                    ledger[0].settle(ledger[1],usage,None if known_length else caught,time.perf_counter()-started)
                except Exception:
                    # The original response/error survives; no subsequent charge or write.
                    with self.lock:
                        self.runs[identifier]['director_ledger_unknown'] = True
                    try:
                        ledger[0].pause('ledger_write_unknown',{'attempt_id':ledger[1]['attempt_id']})
                    except Exception:
                        pass

    def _captured_agent_step(self, identifier, profile, messages, tools, role, index, timeout):
        try:
            if role=='director' and getattr(self.providers,'DIRECTOR_ARGUMENT_FEEDBACK',False):
                extra = {}
                if getattr(self.providers,'DIRECTOR_REPORT_FEEDBACK',False):
                    from .director_reply_correction import window
                    parent=self.runs[identifier];store=self._director_host().store(parent['project_id'])
                    repair=window(store,parent,{'result':store.session(identifier).get('result')})
                    if repair and repair['remaining']:extra['director_report_only']=True
                return self.providers.agent_step(profile,messages,tools,timeout=timeout,director_arguments=True,**extra)
            if role=='builder' and getattr(self.providers,'BUILDER_ARGUMENT_CORRECTION',False):
                protocol=getattr(self,'_builder_tool_protocols',{}).get(identifier)
                if protocol is None or protocol.run_id!=identifier:raise providers.ProviderError('tool_correction_unbound','Builder纠错协议未绑定当前Run。')
                extra = {}
                if getattr(self.providers, 'BUILDER_LENGTH_RECOVERY', False):
                    extra['builder_recovery'] = getattr(self, '_builder_recoveries', {}).get(identifier)
                    if extra['builder_recovery'] is None:
                        raise providers.ProviderError('builder_recovery_unbound', '原Builder恢复协议未绑定当前Run。')
                return self.providers.agent_step(profile,messages,tools,timeout=timeout,builder_correction=protocol, **extra)
            return self.providers.agent_step(profile, messages, tools, timeout=timeout)
        except providers.ProviderError as error:
            try:
                from . import transport_diagnostics
                safe = providers.sanitize_agent_diagnostics(error.diagnostics)
                transport = transport_diagnostics.bind(safe.get('provider_transport'),identifier,role,index)
                if transport:
                    error.diagnostics = {**safe,'provider_transport':transport}
            except Exception:
                pass
            raise

    def _response_diagnostics(self, identifier, error, *, role=None, input_id=None):
        """Best-effort bounded evidence. Diagnostic failures never replace the provider error."""
        try:
            diagnostics = providers.sanitize_agent_diagnostics(getattr(error, "diagnostics", None))
            if 'provider_transport' in diagnostics:
                from . import transport_diagnostics
                with self.lock:
                    run = self._get(identifier)
                    original = diagnostics['provider_transport']
                    selected_role = role or original.get('role') or (
                        'image' if run.get('stage') == 'generating_concept' else
                        'planning' if run.get('stage') == 'planning' else 'builder')
                    counter = 'director' if selected_role == 'director' else 'judge' if selected_role == 'judge' else 'image' if selected_role == 'image' else 'brain'
                    index = (original.get('call_index') if original.get('run_id') == identifier
                             and original.get('role') == selected_role else None)
                    if index is None: index = (run.get('usage') or {}).get(counter+'_calls')
                    transport = transport_diagnostics.bind(original,identifier,selected_role,index,input_id=input_id)
                    safe = {**diagnostics,'provider_transport':transport}
                    try:
                        records = run.setdefault('provider_diagnostics',[])
                        if transport not in records: records.append(copy.deepcopy(transport))
                        del records[:-response_diagnostics.MAX_SNAPSHOTS_PER_RUN]
                        self._save(run)
                    except Exception:
                        pass
                return providers.sanitize_agent_diagnostics(safe)
            kind = "provider_http" if "provider_http" in diagnostics else "response_parse"
            parse = diagnostics.get(kind) if isinstance(diagnostics, dict) else None
            if not isinstance(parse, dict):
                return diagnostics or {}
            enriched = dict(parse)
            try:
                run = self._get(identifier)
            except Exception:
                run = None
            if isinstance(run, dict):
                enriched["run_id"] = identifier
                if kind == "provider_http":
                    selected_role = role or ("image" if run.get("stage") == "generating_concept" else
                                             "planning" if run.get("stage") == "planning" else "builder")
                    enriched["role"] = selected_role
                    counter = "judge" if selected_role == "judge" else "image" if selected_role == "image" else "brain"
                    enriched["call_index"] = (run.get("usage") or {}).get(counter + "_calls")
                    if input_id:
                        enriched["input_id"] = input_id
                else:
                    brain_call = (run.get("usage") or {}).get("brain_calls")
                    if type(brain_call) is int:
                        enriched["brain_call"] = brain_call
                    brain = (run.get("profiles") or {}).get("brain") or {}
                    if isinstance(brain, dict):
                        if isinstance(brain.get("model"), str):
                            enriched["model"] = brain["model"]
                        if type(brain.get("revision")) is int:
                            enriched["profile_revision"] = brain["revision"]
            status, snapshot_id, reason = "not_saved", None, None
            snapshot = getattr(error, "response_snapshot", None)
            if kind == "provider_http" and isinstance(snapshot, dict):
                snapshot = {**snapshot, **response_diagnostics.sanitize_provider_http(enriched)}
            if kind == "response_parse" and parse.get("source") != "provider_response":
                status, reason = "not_saved", "local_history"
            else:
                try:
                    status, snapshot_id, reason = response_diagnostics.write_snapshot(
                        self.run_directory(identifier), snapshot)
                except Exception:
                    status, snapshot_id, reason = "failed", None, "write_failed"
            enriched["snapshot_status"] = status
            if snapshot_id:
                enriched["snapshot_id"] = snapshot_id
            if reason:
                enriched["snapshot_reason"] = reason
            omitted = snapshot.get("content_omitted_reason") if isinstance(snapshot, dict) else None
            if omitted and "content_omitted_reason" not in enriched:
                enriched["content_omitted_reason"] = omitted
            diagnostics = dict(diagnostics)
            diagnostics[kind] = enriched
            safe = providers.sanitize_agent_diagnostics(diagnostics)
            if kind == "provider_http" and isinstance(run, dict):
                # A Judge failure can be handled without _fail. Keep its association in Run too.
                try:
                    with self.lock:
                        stored = self._get(identifier)
                        records = stored.setdefault("provider_diagnostics", [])
                        records.append(copy.deepcopy(safe[kind]))
                        del records[:-response_diagnostics.MAX_SNAPSHOTS_PER_RUN]
                        self._save(stored)
                except Exception:
                    pass
            return safe
        except Exception:
            return {}

    def _concept(self, identifier):
        try:
            if self._cancelled(identifier):
                return
            run = self.get(identifier)
            self._update(identifier, "generating_concept", "正在调用生图服务，生成场景概念图。")
            profile = self._execution_profile(run["profiles"]["image"])
            if not self._reserve_call(identifier, "image"):
                return
            prompt = concept_prompt(run)
            result = self.providers.generate_concept(profile, prompt, self.run_directory(identifier) / "concept")
            self._artifact(identifier, result["path"], "concept", metadata=result)
            if self._cancelled(identifier):
                return
            self._update(identifier, "awaiting_reference", "概念图已生成。采用后才会把它作为白模建场参考，并发送给所选大脑。", status="awaiting_reference")
        except Exception as error:
            self._fail(identifier, error)

    def adopt(self, identifier):
        with self.lock:
            run = self._get(identifier)
            if run["reference"] is not None:
                return copy.deepcopy(run)
            if run["status"] != "awaiting_reference":
                raise AppError("当前任务没有等待采用的概念图。", "invalid_state", 409)
            self._assert_idle()
            # The concept is already saved; its provider/key will not be used again.
            self._preflight(run, kinds=("brain",))
            concept = next(item for item in run["artifacts"] if item["kind"] == "concept")
            run["reference"] = {"artifact_id": concept["id"], "sha256": concept["sha256"], "adopted_at": now(), "basis": "user_action"}
            run["status"] = "running"
            self._event(run, "reference_adopted", "已采用当前概念图；参考版本已固定。")
            self._save(run)
            self._start(run, self._scene)
            return copy.deepcopy(run)

    def _scene(self, identifier):
        try:
            if self._cancelled(identifier):
                return
            run = self.get(identifier)
            concept = next(item for item in run["artifacts"] if item["id"] == run["reference"]["artifact_id"])
            image = self.run_directory(identifier) / concept["path"]
            if file_sha256(image) != run["reference"]["sha256"]:
                raise AppError("采用的概念图发生变化，已停止建场。", "reference_changed")
            self.bind_refinement_goal(identifier)
            run = self.get(identifier)
            if run.get("iteration", {}).get("enabled"):
                VisualAgent(self, identifier).execute()
                return
            self._update(identifier, "planning", "正在把已采用的概念图发送给大脑，规划物体、布局和观察视角。")
            profile = self._execution_profile(run["profiles"]["brain"])
            if not self._reserve_call(identifier, "brain"):
                return
            plan = self.providers.plan_scene(profile, run["prompt"], image, run["constraints"])
            plan = self.blender.validate_plan(plan) or plan
            plan_path = self.run_directory(identifier) / "scene-plan.json"
            write_json(plan_path, plan)
            self._artifact(identifier, plan_path, "plan")
            if self._cancelled(identifier):
                return
            adapter = self._adapter_for(run)
            if run.get("project_id"):
                self._scene_operation(identifier, None, "initial")
            workspace = adapter.create_workspace(self.run_directory(identifier) / "candidate")
            self._update(identifier, "building", f"正在独立候选版本中建立 {len(plan['objects'])} 个场景对象。")
            adapter.execute_plan(plan, workspace)
            self._artifact(identifier, Path(workspace) / "scene.blend", "blend")
            if self._cancelled(identifier):
                return
            self._update(identifier, "rendering", "正在渲染主视角与辅助视角。")
            adapter.render(workspace)
            for filename, kind in (("main.png", "render"), ("auxiliary.png", "render"), ("inspection.json", "inspection")):
                self._artifact(identifier, Path(workspace) / filename, kind)
            if self._cancelled(identifier):
                return
            self.commit_initial_working_scene(identifier, None)
            self._update(identifier, "completed", "白模和两个视角已生成；若项目此前没有场景，此版本已成为 Working Scene。请继续检查关键形状、构图与功能。",
                         status="completed", quality_status="needs_user_review")
        except Exception as error:
            self._fail(identifier, error)

    def cancel(self, identifier):
        with self.lock:
            run = self._get(identifier)
            self._resource_host().cancel(run)
            if run.get("asset_request"):
                self._close_asset_wait(identifier,reason='cancelled')
                run = self._get(identifier)
                if not any(w.is_alive() for w in self.workers.values()):
                    self._worker_finished(identifier)
            if run["status"] in TERMINAL:
                return copy.deepcopy(run)
            run["cancel_requested"] = True
            if run.get('operation') == 'conversation':
                self._director_host().cancel(run)
            if not any(event.get('stage')=='cancel_requested' for event in run.get('events',[])):
                self._event(run, "cancel_requested", "已请求停止后续步骤。当前请求可能继续执行或计费；不会再提交下一步。")
            self._save(run)
            worker = self.workers.get(identifier)
            if run["status"] == "awaiting_reference" or run.get("queue_state") == "waiting" or not (worker and worker.is_alive()):
                self._cancelled(identifier)
                self._seal_run_handoff(identifier)
                if (run.get('asset_request') or run.get('resource_request')) and not any(w.is_alive() for w in self.workers.values()):
                    self._worker_finished(identifier)
            return copy.deepcopy(run)

    def resume_local_round(self, identifier, body):
        """Recover only a confirmed not-started dispatch for the same receipt ID."""
        request = validate_resume(body)
        with self.lock:
            run = self._get(identifier)
            self._assert_asset_write(run.get("project_id"), run_id=identifier)
            if not local_round_enabled(run):
                raise RoundError("此Run没有局部轮。", "round_not_resumable", 409)
            store = self._projects().state_store(run["project_id"])
            prior = store.local_resume(request["resume_id"])
            if prior:
                receipt, _, dispatch = store.reserve_local_resume(
                    identifier, prior["round_id"], request)
            else:
                document = store.local_round(identifier)
                if document is None or document["status"] != "paused":
                    raise RoundError("局部轮没有安全暂停点。", "round_not_resumable", 409)
                if run["status"] not in {"awaiting_resume", "interrupted", "cancelled"}:
                    raise RoundError("Run没有在安全暂停点。", "round_not_resumable", 409)
                view = self.get_local_round(identifier)
                if not view["resume_ready"]:
                    raise RoundError("续做条件不满足。", view["resume_block_reason"] or "round_not_resumable", 409)
                self._assert_idle()
                receipt, _, dispatch = store.reserve_local_resume(
                    identifier, document["id"], request)
            document = store.local_round(identifier, round_id=receipt["round_id"])
            if dispatch != "accepted" or document["status"] != "active":
                return {"run": copy.deepcopy(self._get(identifier)), "resume": receipt,
                        "dispatch_state": dispatch}
            worker = self.workers.get(identifier)
            if worker and worker.is_alive():
                return {"run": copy.deepcopy(self._get(identifier)), "resume": receipt,
                        "dispatch_state": "accepted"}
            self._assert_idle()
            if self.asset_host and run.get('asset_request'):
                asset_store = self.asset_host.store(self,identifier)
                asset_request = asset_store.request(identifier)
                if not asset_request['closed'] and asset_request['phase'] == 'pending_review':
                    asset_store.update(asset_request,phase='continuing')
            run["cancel_requested"] = False
            run["status"] = "running"
            run["error"] = None
            self._event(run, "resuming", "从已核实草稿恢复同一Run；不重放旧工具包。")
            self._save(run)
            self._start(run, lambda run_id: self._resume_local_scene(run_id, request["resume_id"]))
            return {"run": copy.deepcopy(run), "resume": receipt, "dispatch_state": "accepted"}

    def _resume_local_scene(self, identifier, resume_id):
        try:
            run = self.get(identifier)
            if not self._projects().state_store(run["project_id"]).mark_local_resume_started(
                    identifier, resume_id):
                return
            self._verify_project_baseline(run)
            self._local_round_state(run)
            VisualAgent(self, identifier, resume=True).execute()
        except Exception as error:
            self._fail(identifier, error)

    def artifact_path(self, identifier, relative):
        with self.lock:
            run = self._get(identifier)
            if not any(item["path"] == relative for item in run["artifacts"]):
                raise AppError("产物不存在。", "not_found", 404)
            path = (self.run_directory(identifier) / relative).resolve()
            if not path.is_relative_to(self.run_directory(identifier)) or not path.is_file():
                raise AppError("产物不存在。", "not_found", 404)
            return path
