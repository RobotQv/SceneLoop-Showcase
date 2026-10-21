/* Real backend execution. Configuration and local chat remain owned by app.js. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const D = window.SceneLoopDirectorUI;
  const S = window.SceneLoopCreation;
  const R = window.SceneLoopResourceRequests;
  const C = window.SceneLoopConversation;
  const W = window.SceneLoopLocalResourceWarning;
  const timelines = new Map(), stopAttempts = new Map();
  const TERMINAL = new Set(["completed", "failed", "cancelled", "canceled", "interrupted"]);
  const STATUS = {
    queued: "等待执行", running: "正在执行", generating_image: "正在生成概念图", generating_concept: "正在生成概念图",
    awaiting_reference: "概念图已生成，等待采用", planning: "正在规划场景",
    agent_thinking: "大脑正在决定下一步", agent_tool: "正在执行场景工具", observing: "正在截图观察", reviewing: "正在检查场景效果", judging: "独立裁判正在验收",
    importing: "正在导入场景副本", importing_scene: "正在导入场景副本",
    building: "Blender 正在建立白模", rendering: "正在渲染", cancelling: "正在取消",
    completed: "白模已生成，待检查", awaiting_resume: "草稿已暂停，等待显式续做或结束", failed: "执行失败", cancelled: "后续步骤已停止", canceled: "后续步骤已停止", interrupted: "执行已中断，请检查日志",
  };
  let projects = [], project = null, projectEpoch = 0, projectDialogSession = 0, importDialogSession = 0;
  let runSelectionEpoch = 0;
  const observedConversationRuns = new Map();
  const conversationRows = new Map(), retainedTurns = new Map(), runWrites = new Map();
  let runReadSequence = 0, runMutation = 0, historyPage = null, historyLoading = false;
  let followConversation = true, lastConversationTop = 0, readerPosition = null, conversationObserver = null;
  let submissionReadSequence = 0;
  const submissionStorageIssues = new Map();
  let executionDialogSession = 0, executionReturnFocus = null, executionReturnRunId = null;
  let requestControlIntent = null;
  const requestDisclosureButtons = new Map();
  const executionCloseIntents = [];
  let conversationSignature = null, projectLoading = false, projectsAvailable = false;
  let initialConversationPending = true, conversationScrollVersion = 0;
  let projectListSignature = null, legacyDialogSession = 0;
  let walkthroughBusy = false;
  const PROJECT_KEY = "sceneloop.selected-project.v1", pendingSubmissions = new Map(), pendingImports = new Map();
  const SUBMISSION_KEY = "sceneloop.pending-submission.v1:";
  const ASSET_ACTION_KEY = "sceneloop.asset-action.v1:";
  const assetViews = new Map(), assetAttempts = new Map();
  let assetBase = null, assetBaseRead = 0, assetOptContext = "", assetFormIdentity = "", assetActionBusy = false;
  let directorDetailParentId = null;
  const directorChildReadErrors = new Map();
  const directorReadSequence = new Map(), directorStatusPending = new Set();
  const director = D?.create({ get: $, project:()=>project, runs:()=>runs, snapshot:()=>snapshot(), base:()=>assetBase, baseKey:()=>assetBaseKey(),
    historyReady:()=>Boolean(historyPage), historyIncomplete:()=>Boolean(historyPage?.has_more), scope:()=>scope(), isCurrent:context=>isCurrent(context), api:(path,body)=>api(path,body),
    detailParent:()=>directorDetailParent(), dialogSession:()=>executionDialogSession, readParent:id=>readDirectorParent(id),
    openParent:id=>openExecutionDetails(id), openChild:(parentId,childId)=>openDirectorChild(parentId,childId), refresh:()=>refreshRuns(), changed:()=>updateReadiness() });
  const creation = S?.create({get:$,scope:()=>scope(),isCurrent:context=>isCurrent(context),busy:()=>busy || projectLoading,
    supported:()=>Boolean(director?.route().supported),taskContext:()=>director?.taskContext() || {ready:false,existing:false},profile:()=>assetProfile(),api:(path,body)=>api(path,body),changed:()=>updateReadiness()});
  const resourceRequests=R?.create({director:D,scope:()=>scope(),isCurrent:context=>isCurrent(context),runs:()=>runs,busy:()=>busy || projectLoading,
    api:(path,body)=>api(path,body),reconcile:(parent,nodes)=>reconcileChildren(parent,nodes),changed:()=>{renderConversation();schedulePoll();}});
  const localWarnings=W?.create({director:D,scope:()=>scope(),isCurrent:context=>isCurrent(context),runs:()=>runs,busy:()=>busy||projectLoading,
    target:a=>{const t=D.parent(a)?D.childFor(a,runs):a;return t&&assetRequested(t)&&!D.readonly(a)?t:null;},
    view:id=>assetViews.get(id),read:id=>refreshAssetRequest(id),unknown:assetUnknown,local:d=>assetLocal(d),matches:assetConfigurationMatches,api:(p,b)=>api(p,b),
    confirm:async(ownerId,targetId,context)=>{const owner=runs.find(r=>r.id===ownerId);if(D.parent(owner)){const p=await readDirectorParent(ownerId);if(!isCurrent(context)||!p||p.active_child_id!==targetId)return false;return Boolean(await readDirectorChild(p,targetId));}const r=await api(`/api/runs/${encodeURIComponent(targetId)}`);if(!isCurrent(context)||r.id!==targetId||r.project_id!==context.id)return false;mergeConversationRuns([r]);return true;},
    accept:(id,data,context)=>{const e=assetViews.get(id),d=assetDocument(data.asset_request,id,context);if(!e||!d||e.doc&&d.version<e.doc.version)throw Error('Asset identity');e.sequence++;e.doc=d;e.readError='';e.confirmed=true;},
    acceptRun:(r,id,context)=>{if(r.id!==id||r.project_id!==context.id)throw Error('Run identity');mergeConversationRuns([r]);},changed:()=>{renderConversation();schedulePoll();}});
  const pickFields = (value, fields) => Object.fromEntries(fields.filter(key => value && Object.hasOwn(value, key) && (value[key] == null || ["string", "number", "boolean"].includes(typeof value[key]))).map(key => [key, value[key]]));
  function safeSubmissionBody(value, preservePending = false) {
    const profile = value => {
      if (value == null) return null;
      const result = pickFields(value, ["id", "revision", "name", "kind", "preset", "provider_id", "model", "base_url", "credential_env", "credential_mode", "has_secret", "server_managed", "mode", "python_path", "model_dir", "repo_dir", "max_output_tokens", "request_timeout_seconds", "reasoning_effort", "thinking_budget"]);
      if (result.base_url) { try { const url = new URL(result.base_url); if (url.username || url.password || url.search || url.hash) delete result.base_url; } catch { delete result.base_url; } }
      if (result.credential_env && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(result.credential_env)) delete result.credential_env;
      return result;
    };
    if (D?.parent(value)) return D.safeBody(value,profile,preservePending);
    const body = pickFields(value, ["prompt", "project_id", "operation", "expected_project_revision", "cost_policy"]);
    body.profiles = Object.fromEntries(["brain", "image", "asset3d"].map(kind => [kind, profile(value.profiles?.[kind])]));
    body.constraints = pickFields(value.constraints, ["style", "preserve", "key_objects_recognizable"]);
    body.budget = pickFields(value.budget, ["currency", "limit", "max_image_generations", "max_asset3d_generations", "max_retries"]);
    for (const [key, fields] of [["iteration", ["enabled", "max_rounds", "mode"]], ["execution", ["mode"]], ["evaluation", ["mode"]], ["base_scene", ["run_id", "revision_id"]]]) if (value[key]) body[key] = pickFields(value[key], fields);
    body.judge = pickFields(value.judge, ["enabled", "mode", "trigger_rounds", "reasoning_effort"]);
    if (Object.hasOwn(value.judge || {}, "profile")) body.judge.profile = profile(value.judge.profile);
    if (value.asset_generation?.enabled === true && typeof value.asset_generation.profile_id === "string")
      body.asset_generation = { enabled: true, profile_id: value.asset_generation.profile_id };
    return body;
  }
  function submissionFor(id) {
    if (!id) return null;
    if (pendingSubmissions.has(id)) return pendingSubmissions.get(id);
    try {
      const raw = window.sessionStorage?.getItem(SUBMISSION_KEY + id);
      if (!raw) { submissionStorageIssues.delete(id); return null; }
      if (raw.length > 100000) throw new Error("Oversized submission envelope");
      const value = JSON.parse(raw);
      if (value?.project_id !== id || !/^[A-Za-z0-9_-]{12,128}$/.test(value.key || "") || value.body?.project_id !== id || !["edit", "generate", "conversation"].includes(value.body?.operation)) throw new Error("Invalid submission envelope");
      const body = safeSubmissionBody(value.body,true);
      const record = { project_id: id, key: value.key, body, signature: JSON.stringify(body), created_at: typeof value.created_at === "string" ? value.created_at : "", run_id: typeof value.run_id === "string" ? value.run_id : null, state: "unknown" };
      pendingSubmissions.set(id, record); submissionStorageIssues.delete(id); return record;
    } catch { submissionStorageIssues.set(id, "无法读取待确认提交身份；为避免重复执行，发送暂不可用。"); return null; }
  }
  function saveSubmission(record) {
    pendingSubmissions.set(record.project_id, record);
    try {
      if (!window.sessionStorage) throw new Error("Storage unavailable");
      window.sessionStorage.setItem(SUBMISSION_KEY + record.project_id, JSON.stringify({ ...pickFields(record, ["project_id", "key", "created_at", "run_id", "state"]), body: record.body }));
      submissionStorageIssues.delete(record.project_id); return true;
    } catch {
      submissionStorageIssues.set(record.project_id, record.run_id ? "原请求已接受，但未能保存恢复身份；请在离开页面前核对原任务，避免重复提交。" : record.state === "unknown" ? "提交结果未知且恢复身份未能保存；请保留页面并核对原请求，不要盲目重发。" : "无法保存提交身份；本次未发送，启用本标签页会话存储后再试。");
      return false;
    }
  }
  function clearSubmission(id) {
    pendingSubmissions.delete(id);
    try { window.sessionStorage?.removeItem(SUBMISSION_KEY + id); submissionStorageIssues.delete(id); }
    catch { submissionStorageIssues.set(id, "已确认原任务，但会话记录未能清除；请重新核对后发送。"); }
  }
  function acceptedRun(value, context, body = null) {
    return Boolean(value && typeof value.id === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(value.id)
      && (!value.project_id || value.project_id === context.id) && (body || value.project_id === context.id));
  }
  async function recoverSubmission(context = scope()) {
    const record = submissionFor(context.id); if (!record || !isCurrent(context)) return false;
    const token = ++submissionReadSequence;
    try {
      const data = await api(`/api/runs?project_id=${encodeURIComponent(context.id)}&idempotency_key=${encodeURIComponent(record.key)}`);
      if (!isCurrent(context) || token !== submissionReadSequence || pendingSubmissions.get(context.id)?.key !== record.key) return false;
      const found = Array.isArray(data.runs) && data.runs.length === 1 ? data.runs[0] : null;
      if (!acceptedRun(found, context) || found.idempotency_key !== record.key) { record.state = "unknown"; renderConversation(); return false; }
      record.run_id = found.id; record.confirmed = true;
      mergeConversationRuns([found]); clearSubmission(context.id);
      window.SceneLoopWorkbench?.clearSubmittedDraft?.(context.id, record.body.prompt);
      creation?.accepted(record.body);
      renderConversation(); renderRunPicker(); updateReadiness(); return true;
    } catch { if (isCurrent(context) && token === submissionReadSequence) { record.state = "unknown"; error("提交结果仍无法确认；原请求身份和输入已保留，请核对原请求。不会自动重发。"); renderConversation(); } return false; }
  }
  let imageItems = [], imageIndex = 0, imageReturnFocus = null;
  let health = null, runs = [], current = null, busy = false, polling = false, timer = null;
  let previewKind = "render", showGenerated = false, previewSignature = null;
  let pollingFailures = 0, initialized = false, modeChosen = false;
  let viewedRevision = null, revisionSignature = null;
  let localRound = null, localRoundBudget = null, localRoundRunId = null, localRoundLoad = 0, localRoundError = "", roundActionBusy = false;
  let budgetChoiceProjectId = null;
  let refinement = null, refinementProjectId = null, refinementLoad = 0, refinementError = "";
  const pendingRoundActions = new Map();
  const STEP_STATUS = { scheduled: "写入已调度", pending: "待自检", selected: "Builder 已确认草稿",
    rejected: "草稿未保留", needs_user_review: "需人工确认", failed: "写入已确认失败",
    unknown: "写入结果未知" };
  const ROUND_BLOCK = {
    round_not_resumable: "当前没有安全暂停点；可核对状态或结束未保存草稿。",
    round_working_changed: "Working 已变化；旧草稿不能续做，可核对后明确结束。",
    round_goal_changed: "目标已变化；旧草稿不能续做，可明确结束后新建任务。",
    round_criteria_changed: "受评标准已变化；旧草稿不能续做，可明确结束。",
    round_operation_unknown: "草稿写入结果未知；不要重放，请核对文件与记录，可明确结束。",
    round_evidence_invalid: "起点或恢复点证据已变化；不能续做，可明确结束。",
    round_resource_exhausted: "累计资源已用完；可结束草稿并检查已保存 Working。",
    round_judge_required: "需要先完成当前 Working 的独立验收。",
    round_busy: "Run 仍在执行；请等待后核对状态。",
    round_version_conflict: "轮版本已变化；请重新读取状态后再操作。",
    round_end_conflict: "此轮已经由另一操作结束；请核对状态。",
  };
  const REVISION_STATUS = { selected: "保留版本", pending: "等待比较", rejected: "未保留", superseded: "历史版本" };
  const VERDICT = { improved: "有所改善", accepted: "保留", better: "有所改善", worse: "变差", rejected: "不保留", uncertain: "无法确定", unchanged: "无明显改善", equivalent: "无明显改善", satisfactory: "模型认为已达到目标" };
  const PRESERVATION = { pass: "保留要求满足", violation: "违反保留要求", unknown: "保留要求待确认" };
  const STOP = {
    brain_limit: "达到大脑调用上限", tool_limit: "达到工具调用上限", needs_user_review: "需要你检查当前结果",
    no_progress: "连续修改未取得可靠改善", goal_reached: "模型认为已达到本阶段目标", stage_passed: "独立裁判已通过当前阶段范围", capability_limit: "当前工具或模型能力不足，需要你检查",
    max_rounds: "达到修改轮数上限", round_limit: "达到修改轮数上限", max_brain_calls: "达到大脑调用上限", brain_call_limit: "达到大脑调用上限",
    max_observations: "达到截图上限", observation_limit: "达到截图上限", max_tool_calls: "达到工具调用上限", tool_call_limit: "达到工具调用上限",
    max_seconds: "历史执行达到旧Run软时限", time_limit: "历史执行达到旧Run软时限", target_reached: "模型认为已达到本阶段目标",
    uncertain: "模型无法高置信判断，需要你检查", low_confidence: "模型无法高置信判断，需要你检查", no_improvement: "未发现可靠改善",
    model_finished: "模型已结束本轮迭代", completed: "模型已结束本轮迭代", cancelled: "用户已停止后续步骤", interrupted: "执行中断，需要检查当前结果",
  };

  const snapshot = () => window.SceneLoopWorkbench?.snapshot?.() || null;
  const inProgress = (run) => Boolean(run && !TERMINAL.has(run.status) && !["awaiting_reference", "awaiting_resume"].includes(run.status));
  const localMode = (run) => (run?.iteration?.mode || run?.request?.iteration?.mode) === "local_round_v1";
  const validRoundTarget = () => {
    const value = Number($("local-round-target").value);
    return Number.isInteger(value) && value >= 1 && value <= 12 ? value : null;
  };
  const budgetPreset = () => health?.local_round_budget_policy?.presets?.[String(validRoundTarget())] || null;
  function loadBudgetChoice() {
    if (budgetChoiceProjectId === project?.id) return;
    budgetChoiceProjectId = project?.id || null;
    $("local-round-target").value = "3";
    if (!budgetChoiceProjectId) return;
    try {
      const saved = JSON.parse(window.sessionStorage?.getItem(`sceneloop.local-budget-choice.v1:${budgetChoiceProjectId}`) || "null");
      if (Number.isInteger(saved?.target) && saved.target >= 1 && saved.target <= 12) $("local-round-target").value = String(saved.target);
      if (saved) saveBudgetChoice();
    } catch { /* The in-page selection remains available. */ }
  }
  function saveBudgetChoice() {
    const target = validRoundTarget();
    if (!project?.id || target === null) return;
    try { window.sessionStorage?.setItem(`sceneloop.local-budget-choice.v1:${project.id}`, JSON.stringify({ target })); }
    catch { /* Browser storage may be unavailable. */ }
  }
  function pendingKey(runId, action) { return `sceneloop.local-round-action.v1:${runId}:${action}`; }
  function pendingAction(runId, action) {
    const key = pendingKey(runId, action);
    if (pendingRoundActions.has(key)) return pendingRoundActions.get(key);
    try {
      const parsed = JSON.parse(window.sessionStorage?.getItem(key) || "null");
      if (parsed && parsed.run_id === runId && parsed.action === action && parsed.body
          && Number.isInteger(parsed.body.expected_round_version)) {
        pendingRoundActions.set(key, parsed); return parsed;
      }
    } catch { /* Browser storage may be unavailable; the in-page identity still works. */ }
    return null;
  }
  function rememberAction(record) {
    const key = pendingKey(record.run_id, record.action);
    pendingRoundActions.set(key, record);
    try { window.sessionStorage?.setItem(key, JSON.stringify(record)); } catch { /* In-page identity remains. */ }
  }
  function forgetAction(runId, action) {
    const key = pendingKey(runId, action);
    pendingRoundActions.delete(key);
    try { window.sessionStorage?.removeItem(key); } catch { /* Nothing else to clear. */ }
  }
  const message = (value) => typeof value === "string" ? value : value?.message || "请求失败，请查看本地服务日志。";
  function error(text = "") { $("execution-error").textContent = text; $("execution-error").hidden = !text; }
  function button(text, handler, className = "button button-quiet") {
    const node = document.createElement("button"); node.type = "button"; node.textContent = text; node.className = className;
    node.addEventListener("click", handler); return node;
  }
  function safeArtifactUrl(value) {
    try { const url = new URL(value, location.href); return url.origin === location.origin && /^\/api\/runs\//.test(url.pathname) ? url.href : null; }
    catch { return null; }
  }
  async function api(path, body, timeoutMs = 20000) {
    const controller = new AbortController(), timeout = timeoutMs === null ? null : setTimeout(() => controller.abort(), timeoutMs);
    try {
      const options = { cache: "no-store", signal: controller.signal };
      if (body !== undefined) {
        options.method = "POST"; options.headers = { "Content-Type": "application/json", "X-SceneLoop-Token": health?.csrf_token || "" };
        options.body = JSON.stringify(body);
      }
      const response = await fetch(path, options);
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        const failure = new Error(message(data?.error || data?.message || (response.status === 404 ? "执行服务未连接，请按 README 启动本地后端。" : `请求失败（${response.status}）`)));
        failure.code = data?.error?.code || null; failure.status = response.status;
        failure.outcomeUnknown = Boolean(data?.error?.outcome_unknown);
        throw failure;
      }
      if (!data) throw new Error("执行服务返回了无法识别的数据。");
      return data;
    } catch (e) {
      if (e.name === "AbortError") throw new Error("执行服务响应超时。若请求已经发送，请先刷新执行记录确认状态。");
      if (e instanceof TypeError) throw new Error("无法连接本地执行服务，请确认后端已启动。");
      throw e;
    } finally { clearTimeout(timeout); }
  }
  function modeAvailable(mode) {
    const environment = project?.blender || health?.blender, modes = environment?.modes;
    if (modes?.[mode]) return Boolean(modes[mode].available);
    return mode === "headless" && Boolean(environment?.available);
  }
  const scope = () => ({ id: project?.id || null, epoch: projectEpoch });
  const isCurrent = (context) => context.id === (project?.id || null) && context.epoch === projectEpoch;
  const primaryOperation = () => director ? "conversation" : project?.current_scene ? "edit" : "generate";
  const resourceView = run => resourceRequests?.view(D?.parent(run) ? run.id : D?.parentFor(run,runs)?.id || run.id);
  const assetBaseKey = () => JSON.stringify([projectEpoch, project?.id, project?.revision, project?.current_scene,project?.workspace?.working_version,project?.workspace?.stage]);
  const assetRequested = run => !D?.parent(run) && Boolean(run?.asset_request || run?.asset_generation?.enabled || run?.request?.asset_generation?.enabled || assetViews.get(run?.id)?.doc);
  function assetControlsRun(run) {
    if (!assetRequested(run)) return false;
    const entry = assetViews.get(run.id), doc = entry?.doc || run.asset_request;
    const paused = run.status === "awaiting_resume", assetStage = run.stage?.startsWith("asset_");
    const checkpoint = localRoundRunId === run.id ? localRound?.checkpoint : null;
    if (!paused && !assetStage) return false; // Capability never hides normal Builder/Judge or cancel.
    if (assetStage) return true;
    if (doc?.closed === true) return false;
    if (checkpoint?.type === "asset_wait") return true;
    if (entry?.readError) return true;
    if (!doc) return !entry?.confirmed;
    if (assetUnknown(doc)) return true;
    const phase = run.asset_request?.version >= doc.version ? run.asset_request.phase : doc.phase;
    return !["continuing", "builder_continuing", "pending_review"].includes(phase)
      && !["started", "completed"].includes(doc.continuation?.state);
  }
  const assetWaiting = run => assetControlsRun(run) && !TERMINAL.has(run?.status);
  function assetProfile() { return snapshot()?.profiles?.asset3d; }
  function assetProfileProblem() {
    const profile = assetProfile();
    if (!profile) return "先选择已保存的 3D 配置";
    if (!profile.server_managed || profile.is_draft || !Number.isInteger(profile.revision)) return "3D 配置须先保存到本机配置库";
    if (S?.localProfile(profile)) return window.SceneLoopWorkbench?.assetProfileIssue?.() || "";
    if (profile.provider_id === "hunyuan") return "Hunyuan2mini 需要已保存的本地图生 shape 配置、Python、源码仓库和权重目录";
    if (profile.provider_id !== "tripo" || profile.model !== "v3.1-20260211" || !["https://openapi.tripo3d.com/v3", "https://openapi.tripo3d.ai/v3"].includes(profile.base_url?.replace(/\/$/, ""))) return "单资产请求仅支持 Tripo H3.1 官方 .com / .ai 配置";
    if (profile.mode !== "cloud_api" || !(profile.credential_mode === "env" && profile.credential_env || profile.credential_mode === "stored" && profile.has_secret)) return "为所选 3D 配置保存可用的凭据";
    return window.SceneLoopWorkbench?.assetProfileIssue?.() || window.SceneLoopWorkbench?.configurationIssue?.("asset3d") || "";
  }
  function assetCapabilityProblem() {
    if (!project?.current_scene) return "需要已有原生 Working Scene；新场景不开放此能力";
    if (!health?.ok || projectLoading) return "等待本地服务与当前项目载入";
    if ($("execution-mode").value !== "headless" || !modeAvailable("headless")) return "单资产请求需要可用的 Headless 运行方式";
    if (assetBase?.key !== assetBaseKey() || assetBase.loading) return "正在核对 Working Scene 的原生 Plan";
    if (assetBase.error) return assetBase.error;
    if (assetBase.kind !== "parametric") return "当前 Working 是普通导入场景；单资产请求需要原生场景";
    if(assetBase.resourceDeclared && !assetBase.resourceVerified)return assetBase.capabilityState === "unsupported" ? "当前场景的施工与资产能力不受支持" : "场景施工与资产能力尚未核实；可重新检测环境";
    if (!validRoundTarget()) return "先修正局部轮正式保存目标（1–12）";
    if (snapshot()?.judge?.enabled === false) return "单资产请求需要独立 Judge 开启";
    return assetProfileProblem();
  }
  async function refreshAssetBase() {
    const key = assetBaseKey(); if (!project?.current_scene || assetBase?.key === key) return;
    const context = scope(), head = { ...project.current_scene }, read = ++assetBaseRead;
    assetBase = { key, loading: true };
    try {
      const run = await api(`/api/runs/${encodeURIComponent(head.run_id)}`);
      if (!isCurrent(context) || key !== assetBaseKey() || read !== assetBaseRead) return;
      if (run?.id !== head.run_id || run.project_id !== context.id) throw new Error("Working 身份未确认");
      const artifact = (run.artifacts || []).find(item => item.kind === "plan" && (item.revision_id || null) === (head.revision_id || null));
      const url = artifact && safeArtifactUrl(artifact.url);
      if (!url || !new URL(url).pathname.startsWith(`/api/runs/${encodeURIComponent(head.run_id)}/artifacts/`)) throw new Error("Working 缺少可核对的 Plan");
      const plan = await api(new URL(url).pathname);
      if (!isCurrent(context) || key !== assetBaseKey() || read !== assetBaseRead) return;
      // Existing native plans omit scene_kind; imported plans always declare it.
      if (!plan || !Array.isArray(plan.objects)) throw new Error("Working Plan 无法识别");
      const declared=["external_dependencies","libraries","textures","resource_paths"].some(field=>Object.hasOwn(plan,field) && JSON.stringify(plan[field]) !== "[]" && JSON.stringify(plan[field]) !== "{}");
      const features=[...(Object.hasOwn(plan,"surface") ? ["surface"] : []),...(plan.assets?.instances?.length ? ["assets"] : [])];
      let verified=false,capabilityState=null,error=null;
      if(features.length && !declared && (!plan.scene_kind || plan.scene_kind === "parametric")) {
        try {const result=await api(`/api/projects/${encodeURIComponent(context.id)}/director-capability`);
          if (!isCurrent(context) || key !== assetBaseKey() || read !== assetBaseRead) return;
          verified=Boolean(S?.qualifiedCapability(result.capability,project,features));
          capabilityState=verified ? "verified" : !S?.capabilityIdentity(result.capability,project) ? "stale" : result.capability.supported === false ? "unsupported" : "unverified";
          if(capabilityState === "stale")error="场景施工核验与当前版本不匹配；可重新检测环境";
        } catch {capabilityState="error";error="场景施工能力查询失败；可重新检测环境";}
      }
      if (!isCurrent(context) || key !== assetBaseKey() || read !== assetBaseRead) return;
      assetBase = { key, kind: plan.scene_kind || "parametric", resourceDeclared:declared || features.length > 0,resourceVerified:verified,capabilityState,error, loading: false };
    } catch { if (!isCurrent(context) || key !== assetBaseKey() || read !== assetBaseRead) return;
      assetBase = { key, error: "Working 的原生 Plan 尚未确认；请重新检测环境", loading: false }; }
    updateReadiness();
  }
  function resetAssetForm() { assetFormIdentity = ""; $("asset-no-hard-cap").checked = false; $("asset-max-credits").value = ""; }
  function renderAssetSettings() {
    refreshAssetBase();
    const profile = assetProfile(), identity = JSON.stringify([assetBaseKey(), $("execution-mode").value, profile?.id, profile?.revision, profile?.model, profile?.base_url]);
    const problem = director?.route().supported && director.mode() === "read_only" ? "只读进度问答不开放资产能力；逐次批准只属于原施工子请求" : assetCapabilityProblem();
    if (assetOptContext !== identity) $("asset-generation-enabled").checked = false;
    assetOptContext = identity; $("asset-generation-enabled").disabled = Boolean(problem);
    $("asset-generation-help").textContent = problem || `使用 ${profile.name || "所选 3D 配置"} · Headless / 局部草稿轮 / 独立 Judge。开放能力不代表批准费用。`;
  }
  function assetDocument(value, runId, context) {
    if (value === null) return null;
    if (!value || typeof value.id !== "string" || !/^[A-Za-z0-9_.-]{1,128}$/.test(value.id) || value.run_id !== runId || value.project_id !== context.id || !Number.isInteger(value.version) || value.version < 1) throw new Error("资产请求身份未确认");
    const doc = pickFields(value, ["id", "project_id", "run_id", "version", "request_revision", "phase", "closed", "prompt", "reason", "model", "endpoint", "profile_id", "profile_revision", "input_kind"]);
    if(value.local_process)doc.local_process=pickFields(value.local_process,["stage","evidence","elapsed_seconds","reason_code","monitoring"]);
    if(value.resource_warning!=null&&W)doc.resource_warning=W.parse(value.resource_warning);
    if(value.reference)doc.reference=pickFields(value.reference,["reference_id","sha256"]);
    doc.estimate = pickFields(value.estimate, ["status", "credits", "unit", "policy", "supplier_hard_cap", "checked_at", "expires_at", "reason_code"]);
    doc.approval = pickFields(value.approval, ["state", "approval_id", "policy", "max_credits"]);
    doc.continuation = pickFields(value.continuation, ["state", "resume_id", "blocked_reason"]);
    doc.actions = Object.fromEntries(["can_approve", "can_end", "can_resume"].map(key => [key, value.actions?.[key] === true]));
    doc.error = pickFields(value.error, ["code", "message", "outcome_unknown"]);
    doc.job = value.job ? pickFields(value.job, ["job_id", "task_id", "dispatch", "remote", "preparation", "local", "cost", "credits", "safe_error"]) : null;
    return doc;
  }
  function assetUnknown(doc) {
    if (!doc) return false;
    return doc.local_process?.stage === "outcome_unknown" || !["waiting", "accepted", "started", "completed", "blocked"].includes(doc.continuation?.state)
      || doc.job?.dispatch === "outcome_unknown" || doc.error?.outcome_unknown === true
      || Boolean(doc.job?.dispatch && !["prepared", "submitting", "accepted", "rejected"].includes(doc.job.dispatch))
      || Boolean(doc.job?.remote && !["unknown", "queued", "running", "succeeded", "failed", "banned", "expired", "cancelled"].includes(doc.job.remote))
      || Boolean(doc.job?.preparation && !["not_started", "downloading", "raw_available", "ready", "blocked", "failed"].includes(doc.job.preparation));
  }
  function assetState(doc) {
    if (!doc) return "尚无已确认的资产请求；刷新只读取状态";
    if (assetUnknown(doc)) return "资产创建或续做结果未知；只能核对状态或结束等待，不会自动重试";
    if (doc.phase === "failed" || ["failed", "banned", "expired"].includes(doc.job?.remote) || doc.job?.preparation === "failed") return "资产请求失败；请核对执行记录";
    if (doc.closed || ["stopped", "stop_requested"].includes(doc.job?.local) || doc.job?.remote === "cancelled") return assetLocal(doc) ? doc.local_process?.stage === "cancelled" && doc.local_process.evidence === "runner_terminal" ? "本地资产生成已停止" : "本地资产等待已结束；生成进程停止情况以终态记录为准" : "资产等待已结束；远端取消和退款未确认";
    if (doc.continuation?.state === "blocked" && !assetLocal(doc)) return `资产续做受限：${doc.continuation.blocked_reason || "等待后端确认"}`;
    if (doc.continuation?.state === "completed") return "资产续做步骤已结束；导入、Working 保存和质量以各自记录为准";
    if (doc.continuation?.state === "started") return "Builder 已开始原资产续做；尚未据此确认导入或正式保存";
    if(assetLocal(doc))return localAssetState(doc);
    if (doc.job?.preparation === "ready" || doc.phase === "ready") return "资产 Ready，等待原 Builder 调度；尚未确认导入、保存或质量通过";
    if (doc.job?.preparation === "blocked") return "资产准备受限；请核对状态，尚未确认导入";
    if (doc.job?.remote === "succeeded") return "远端生成已完成，等待资产准备；尚未确认导入或正式保存";
    if (["queued", "running"].includes(doc.job?.remote) || doc.job?.dispatch === "accepted") return "单资产生成进行中；请等待后端推进";
    if (doc.approval?.state === "approved") return "单次批准已受理，等待后端生成调度";
    if (doc.approval?.state === "pending" && doc.phase === "approval") return "Builder 请求一个资产，等待你查看并批准";
    return "资产状态尚未确认；操作受限，请核对状态";
  }
  const assetLocal = doc => doc?.model === "hunyuan3d-2mini" && doc.input_kind === "image" && doc.estimate?.policy === "local_resources";
  function localAssetState(doc) {
    const p=doc.local_process;
    const stages={awaiting_authorization:["等待本任务的本地生成授权",["host_sql"]],queued:["本地资产等待调度",["host_sql","job_sql"]],
      resource_wait:["本地资源暂不可用，等待占用或运行条件解除；尚未开始生成",["host_preflight"]],resource_warning:["本地资源余量存在风险，等待你确认本次生成或取消",["host_preflight"]],starting:["正在启动本地资产生成",["job_sql"]],processing:["本地资产处理中，等待阶段记录",["job_sql"]],
      loading_model:["正在加载本地模型",["runner_progress"]],model_loaded:["本地模型已加载，等待生成",["runner_progress"]],inference:["正在生成本地主体形状",["runner_progress"]],
      exporting_raw:["正在导出本地 shape，等待资源校验",["runner_progress"]],completed:["本地生成已结束，等待资源准备",["runner_terminal"]],ready:["本地资产已准备好，等待 Builder 导入",["job_sql"]],
      failed:["本地资产生成失败，请核对原任务",["runner_terminal"]],cancelled:["本地资产生成已停止",["runner_terminal"]],outcome_unknown:["本地生成结果未知；只读核对，不会重新生成",["job_sql"]]};
    const row=stages[p?.stage];
    return row?.[1].includes(p.evidence) ? row[0] : "本地资产处理中，具体阶段待核对";
  }
  const assetActualUsage = doc => doc?.approval?.policy === "actual_usage";
  const assetLegacyPolicy = doc => doc && [undefined,"catalog_estimate"].includes(doc.approval.policy);
  function assetConsumption(doc){
    if(assetLocal(doc))return "本地资源调用；额度使用以本任务记录为准。";
    const job=doc?.job,credits=job?.credits,valid=typeof credits==="number"&&Number.isFinite(credits)&&credits>=0;
    if(job?.cost==="consumed"&&valid)return `实际消耗：${credits} credits（供应商已报告）`;
    if(job?.cost==="no_charge_confirmed"&&credits===0)return "实际消耗：0 credits（供应商已确认）";
    if(job?.cost==="released"&&valid)return `实际消耗待核对；供应商已释放 ${credits} credits，不作为新增消耗累计。`;
    if(job?.cost==="not_dispatched"||!job)return "实际消耗：尚未创建作业，暂无供应商消费记录。";
    return "实际消耗：未知，等待供应商记录；不按 0 计。";
  }
  const assetIdentity = doc => JSON.stringify([doc?.id, doc?.version, doc?.request_revision, doc?.profile_id, doc?.profile_revision, doc?.model, doc?.endpoint,doc?.approval?.policy,doc?.approval?.max_credits,doc?.estimate?.policy, assetBaseKey(), assetProfile()?.id, assetProfile()?.revision, assetProfile()?.base_url, assetProfile()?.model]);
  const assetActionKey = (doc, action) => ASSET_ACTION_KEY + JSON.stringify([doc.project_id, doc.run_id, doc.id, doc.version, action, action === "resume" ? doc.continuation?.resume_id : null]);
  function assetAttempt(doc, action) {
    const key = assetActionKey(doc, action); if (assetAttempts.has(key)) return assetAttempts.get(key);
    try {
      const raw = window.sessionStorage?.getItem(key); if (!raw) return null;
      const value = JSON.parse(raw);
      if (value.request_id !== doc.id || value.version !== doc.version || typeof value.key !== "string") throw new Error();
      const record = pickFields(value, ["request_id", "version", "key"]); assetAttempts.set(key, record); return record;
    } catch { return { blocked: true }; }
  }
  function rememberAssetAttempt(doc, action, key) {
    const record = { request_id: doc.id, version: doc.version, key }, storageKey = assetActionKey(doc, action);
    if (!window.sessionStorage) throw new Error("无法保存动作身份；本次未发送，请启用会话存储");
    window.sessionStorage.setItem(storageKey, JSON.stringify(record)); assetAttempts.set(storageKey, record);
  }
  function assetConfigurationMatches(doc) {
    const profile = assetProfile();
    return !assetProfileProblem() && profile.id === doc.profile_id && profile.revision === doc.profile_revision && profile.model === doc.model && (assetLocal(doc) ? Boolean(S?.localProfile(profile)) : profile.base_url?.replace(/\/$/, "") === doc.endpoint?.replace(/\/$/, ""));
  }
  function assetCanApprove(doc) {
    return doc?.actions.can_approve && !doc.closed && !assetUnknown(doc) && doc.phase === "approval" && doc.approval.state === "pending" && assetConfigurationMatches(doc)
      && (assetActualUsage(doc) && doc.approval.max_credits==null && [undefined,"actual_usage"].includes(doc.estimate.policy) || assetLegacyPolicy(doc) && [undefined,"catalog_estimate"].includes(doc.estimate.policy) && doc.estimate.status === "available" && doc.estimate.unit === "credits" && Number.isFinite(doc.estimate.credits) && doc.estimate.credits >= 0);
  }
  const assetAmountValid = doc => assetActualUsage(doc) || Boolean($("asset-max-credits").value.trim()) && Number.isFinite(Number($("asset-max-credits").value)) && Number($("asset-max-credits").value)>=doc.estimate.credits;
  function assetCanResume(doc) {
    return doc?.actions.can_resume && !doc.closed && !assetUnknown(doc) && ["waiting", "accepted"].includes(doc.continuation?.state) && typeof doc.continuation.resume_id === "string" && /^[A-Za-z0-9_.-]{1,128}$/.test(doc.continuation.resume_id) && assetConfigurationMatches(doc);
  }
  function renderAssetRequest() {
    const entry = assetViews.get(current?.id), doc = entry?.doc, visible = assetRequested(current);
    $("run-asset-request").hidden = !visible; if (!visible) { resetAssetForm(); return; }
    const identity = assetIdentity(doc);
    if (assetFormIdentity !== identity) { resetAssetForm(); assetFormIdentity = identity; if (assetLegacyPolicy(doc) && doc?.estimate.status === "available") $("asset-max-credits").value = String(doc.estimate.credits); }
    $("run-asset-state").textContent = assetState(doc);
    $("run-asset-prompt").textContent = doc ? `请求：${doc.prompt || "未提供"}` : "请求详情待读取";
    $("run-asset-reason").textContent = doc ? `需求理由：${doc.reason || "未提供"}` : "";
    $("run-asset-provider").textContent = doc ? assetLocal(doc) ? "本地 Hunyuan2mini · 图生主体 shape" : `站点：${doc.endpoint || "未确认"} · 模型：${doc.model || "未确认"}` : "";
    const legacyCap=Number.isFinite(doc?.approval.max_credits)?` · 原批准上限 ${doc.approval.max_credits} credits`:"";
    $("run-asset-estimate").textContent = assetLocal(doc) ? "本地生成受任务剩余次数与当前阶段许可共同限制；许可不增加任务总次数，云端费用独立批准。" : assetActualUsage(doc) ? "批准单次生成；实际消耗以供应商记录为准，不提供金额硬上限。" : !assetLegacyPolicy(doc) ? "资产批准政策尚未确认，请只读核对原请求。" : doc.estimate.status === "available" ? `历史目录政策 · 目录估价：${doc.estimate.credits} credits${legacyCap} · 原金额准入约束保持，不保证供应商实际硬上限` : `历史目录政策 · 目录估价不可用${doc.estimate.reason_code ? `：${doc.estimate.reason_code}` : "；请核对原请求政策"}${legacyCap}`;
    $("asset-legacy-amount").hidden=!doc || !assetLegacyPolicy(doc) || doc.approval.state!=="pending";
    $("run-asset-consumption").textContent=assetConsumption(doc);
    const attempt = doc && assetAttempt(doc, "approve"), resumeAttempt = doc && assetAttempt(doc, "resume");
    const failedRead = Boolean(entry?.readError), locked = assetActionBusy || busy || failedRead;
    $("asset-approval-fields").hidden = !doc || assetLocal(doc) || doc.approval.state !== "pending";
    $("asset-max-credits").disabled = $("asset-no-hard-cap").disabled = locked || Boolean(attempt) || !assetCanApprove(doc);
    $("approve-asset-request").textContent = attempt ? "核对原批准请求" : "批准单次生成";
    $("approve-asset-request").disabled = locked || !doc || (attempt ? false : !assetCanApprove(doc) || !$("asset-no-hard-cap").checked || !assetAmountValid(doc));
    $("resume-asset-request").textContent = resumeAttempt ? "核对原资产续做" : "恢复原资产等待";
    $("resume-asset-request").disabled = locked || !doc || (resumeAttempt ? false : !assetCanResume(doc));
    $("end-asset-request").disabled = assetActionBusy || busy || failedRead || !doc?.actions.can_end || Boolean(doc.closed);
    $("refresh-asset-request").disabled = assetActionBusy;
    const problem = entry?.readError || entry?.actionError || (doc && !assetConfigurationMatches(doc) ? "资产配置已变化或不可用；旧请求不能批准或续做，请核对原任务" : "");
    dialogError("run-asset-error", problem);
    $("run-asset-audit").textContent = doc ? `请求 ${doc.id} · v${doc.version} · 阶段 ${doc.phase || "未确认"}；批准 ${doc.approval.state || "未确认"}${doc.approval.approval_id ? ` / ${doc.approval.approval_id}` : ""}；Job ${doc.job?.job_id || "未创建"} · 创建 ${doc.job?.dispatch || "未确认"} · 远端 ${doc.job?.remote || "未确认"} · 准备 ${doc.job?.preparation || "未确认"}；续做 ${doc.continuation.state || "未确认"}` : "尚无资产执行记录";
  }
  async function refreshAssetRequest(runId = current?.id) {
    const context = scope(); if (!runId || !context.id) return false;
    const entry = assetViews.get(runId) || { doc: null, sequence: 0 }; assetViews.set(runId, entry);
    const sequence = ++entry.sequence;
    try {
      const data = await api(`/api/runs/${encodeURIComponent(runId)}/asset-request`);
      if (!isCurrent(context) || assetViews.get(runId) !== entry || sequence !== entry.sequence) return false;
      const doc = assetDocument(data.asset_request, runId, context);
      if (entry.doc && doc && doc.version < entry.doc.version) throw new Error("资产请求版本倒退");
      entry.doc = doc; entry.readError = ""; entry.confirmed = true;
      if (doc?.approval.state === "approved" && doc.approval.approval_id || ["started", "completed"].includes(doc?.continuation.state)) entry.actionError = "";
      if (current?.id === runId) { renderAssetRequest(); renderLocalRound(); } renderConversation(); return true;
    } catch (e) {
      if (!isCurrent(context) || assetViews.get(runId) !== entry || sequence !== entry.sequence) return false;
      entry.readError = `资产状态读取失败：${e.message}；保留上次投影，操作暂不可用`;
      if (current?.id === runId) renderAssetRequest(); renderConversation(); return false;
    }
  }
  async function assetAction(action) {
    const runId = current?.id, context = scope(), entry = assetViews.get(runId), doc = entry?.doc;
    if (!runId || !doc || assetActionBusy || busy) return;
    if (action !== "end" && assetAttempt(doc, action)) { await refreshAssetRequest(runId); return; }
    const identity = assetIdentity(doc), selection = runSelectionEpoch, session = executionDialogSession;
    const amount = Number($("asset-max-credits").value), acknowledged = $("asset-no-hard-cap").checked;
    if (action === "approve" && (!assetCanApprove(doc) || !acknowledged || !assetAmountValid(doc))) return;
    assetActionBusy = true; renderAssetRequest();
    try {
      if (!await refreshAssetRequest(runId)) return;
      const fresh = assetViews.get(runId)?.doc;
      if (!isCurrent(context) || selection !== runSelectionEpoch || session !== executionDialogSession || current?.id !== runId || identity !== assetIdentity(fresh) || !$("execution-dialog").open) return;
      const parent = D?.parentFor(current,runs);
      if (parent) {
        const actual = await readDirectorParent(parent.id);
        if (!actual || actual.active_child_id !== runId || !D.childFor(actual,[current],runId) || !isCurrent(context) || selection !== runSelectionEpoch || session !== executionDialogSession || !$("execution-dialog").open) return;
      }
      let suffix, body;
      if (action === "approve") {
        if (!assetCanApprove(fresh) || !$("asset-no-hard-cap").checked || !assetActualUsage(fresh) && amount < fresh.estimate.credits) return;
        const key = crypto.randomUUID(); rememberAssetAttempt(fresh, action, key);
        suffix = "asset-request/approve"; body = { request_id: fresh.id, expected_request_version: fresh.version, approval_key: key, ...(assetActualUsage(fresh)?{cost_policy:"actual_usage"}:{max_credits:amount}), acknowledged_no_hard_cap: true };
      } else if (action === "resume") {
        if (!assetCanResume(fresh)) return;
        rememberAssetAttempt(fresh, action, fresh.continuation.resume_id);
        suffix = "asset-request/resume"; body = { request_id: fresh.id, resume_id: fresh.continuation.resume_id };
      } else {
        if (!fresh?.actions.can_end || fresh.closed) return;
        suffix = "cancel"; body = {};
      }
      resetAssetForm();
      const data = await api(`/api/runs/${encodeURIComponent(runId)}/${suffix}`, body);
      if (!isCurrent(context)) return;
      if (action !== "end") entry.doc = assetDocument(data.asset_request, runId, context);
      entry.actionError = ""; await refreshAssetRequest(runId);
      if (current?.id === runId) await loadRun(runId);
    } catch (e) {
      if (!isCurrent(context)) return;
      entry.actionError = `${e.message}${e.status ? `（HTTP ${e.status}）` : ""}；先核对原动作和 Job 状态，不会重发批准或续做`;
      await refreshAssetRequest(runId);
    } finally { assetActionBusy = false; if (isCurrent(context)) { renderAssetRequest(); renderConversation(); } }
  }
  function blockers(forAdopt = false, operation = primaryOperation(), explicitNew = false) {
    const state = snapshot(), reasons = [];
    if (!health?.ok) reasons.push("先连接本地执行服务");
    if (!projectsAvailable && health?.ok) reasons.push("项目服务未就绪，请更新后端并重新检测");
    if (!project) reasons.push("选择开发项目和文件夹");
    if (!forAdopt && project?.id && submissionStorageIssues.has(project.id)) reasons.push(submissionStorageIssues.get(project.id));
    if (projectLoading) reasons.push("等待项目载入");
    if (!state) reasons.push("工作台配置尚未就绪");
    if (!forAdopt && !state?.prompt?.trim()) reasons.push("输入场景需求或修改要求");
    if (!forAdopt && !state?.profiles?.brain) reasons.push("选择多模态大脑配置");
    if (!forAdopt && operation === "generate" && !state?.profiles?.image) reasons.push("选择生图配置（新场景需要）");
    if (!forAdopt) for (const kind of ["edit","conversation"].includes(operation) ? ["brain"] : ["brain", "image"]) {
      const problem = window.SceneLoopWorkbench?.configurationIssue?.(kind); if (problem) reasons.push(problem);
    }
    if (!forAdopt && operation !== "conversation" && !modeAvailable($("execution-mode").value)) reasons.push("准备可用的 Blender 环境或切换运行方式");
    if (!forAdopt && operation === "conversation" && $("execution-mode").value !== "headless") reasons.push("Director 当前不支持 Attached 施工；请选择 Headless 后发送，不会切为直接 Builder");
    if (!forAdopt && operation === "conversation") reasons.push(...director.problems());
    if (!forAdopt) {const problem=creation?.problem(operation);if(problem)reasons.push(problem);}
    if (!forAdopt && operation === "generate" && state?.budget) {
      const count = Number(state.budget.max_image_generations);
      if (!Number.isInteger(count) || count < 1 || count > 50) reasons.push("修正图像生成次数");
    }
    if (!forAdopt && operation !== "conversation" && document.querySelector('[aria-invalid="true"]#budget-amount')) reasons.push("修正预算金额");
    if (!forAdopt && operation === "generate" && state?.iteration && (typeof state.iteration.enabled !== "boolean" || !Number.isInteger(state.iteration.max_rounds) || state.iteration.max_rounds < 1 || state.iteration.max_rounds > 6)) reasons.push("修正旧模式自动迭代轮数（1–6 轮）");
    if (!forAdopt && (operation === "edit" || operation === "conversation" && director?.taskContext().existing) && !validRoundTarget()) reasons.push("局部轮拟正式保存目标必须为 1–12");
    const frequency = state?.judge?.trigger_rounds ?? 3;
    if (!forAdopt && (!Number.isInteger(frequency) || frequency < 1 || frequency > 3)) reasons.push("Judge 验收频率必须为 1–3 次正式保存");
    if (!forAdopt && operation === "edit" && runs.some((run) => run.status === "awaiting_resume" && localMode(run)))
      reasons.push("先对已暂停的局部草稿显式续做或结束");
    if (!forAdopt && !explicitNew && operation !== "conversation" && !project?.current_scene) {
      if (runs.some((run) => run.status === "awaiting_reference")) reasons.push("先采用已生成的概念图；可在对话记录中查看");
      else if (runs.some((run) => run.status === "completed")) reasons.push("检查已有结果并设为当前场景，再继续对话");
    }
    if (runs.some(inProgress) && (forAdopt || !["edit","conversation"].includes(operation))) reasons.push("等待当前执行结束");
    return reasons;
  }
  function resolveBlocker(reason) {
    if (/项目和文件夹/.test(reason)) { openProjectDialog(); return; }
    if (/配置/.test(reason)) { $("open-settings").click(); return; }
    if (/输入场景/.test(reason)) { $("task-input").focus(); return; }
    if (/概念图|已有结果/.test(reason)) {
      const run = runs.find((item) => item.status === "awaiting_reference") || runs.find((item) => item.status === "completed");
      if (run) loadRun(run.id, true); return;
    }
    $("show-execution-settings").click();
    if (/拟正式保存目标/.test(reason)) {
      $("local-round-target").scrollIntoView({ behavior: "smooth", block: "center" });
      $("local-round-target").focus({ preventScroll: true });
    }
  }
  function updateReadiness() {
    renderEditRoundSettings();
    renderAssetSettings(); renderAssetRequest();
    director?.render(); director?.renderDetails(directorDetailParent());
    creation?.render();
    const operation = primaryOperation(), reasons = blockers();
    for (const id of ["task-input", "project-style", "project-preserve", "key-objects"]) $(id).disabled = !project || projectLoading;
    $("start-run").disabled = busy || reasons.length > 0;
    $("start-run").textContent = operation === "conversation" ? "发送 ↑" : operation === "edit" ? "发送修改 ↑" : "发送并创建场景 ↑";
    $("start-run").title = reasons.join("；");
    const target = mainStopTarget();
    const targetText=(target?.prompt || "当前请求").replace(/\s+/g," ").trim();
    $("composer-stop-target").textContent = target ? `终止目标：${targetText.slice(0,60)}${targetText.length>60?"…":""}` : "";
    $("composer-stop-target").hidden = !target;
    if (target) {
      const pending = stopAttempts.get(target.id), blocked = stopProblem(target);
      $("start-run").textContent = target.cancel_requested ? "正在终止…" : pending === "unknown" ? "核对终止结果" : pending ? "正在终止…" : "终止";
      $("start-run").disabled = Boolean(blocked || busy || projectLoading || pending && pending !== "unknown" || target.cancel_requested);
      $("start-run").title = blocked || "仅停止此请求的后续步骤；已发出的原子操作可能继续返回。执行中仍可用 Enter 发送问题。";
    }
    $("execution-readiness").textContent = busy ? "正在提交，请稍候…" : reasons.length ? `暂不能发送：${reasons.length} 项需要处理` : operation === "edit" ? (runs.some(inProgress) ? "当前请求执行中；新修改将排队并从最新 Working Scene 开始" : `将继续当前 Working Scene：${project.current_scene.title || project.name} · 局部草稿轮`) : "已准备：生成概念图，采用后建场";
    if (!busy && !reasons.length && operation === "conversation") $("execution-readiness").textContent = runs.some(inProgress) ? "可以询问进度或提出下一个需求；施工按项目顺序安排" : "直接描述问题或修改目标；Director 会在已有资源范围内安排。";
    const list = $("execution-blockers"); list.replaceChildren(); list.hidden = !reasons.length;
    for (const reason of reasons) { const item = document.createElement("li"); item.append(button(reason, () => resolveBlocker(reason), "text-button")); list.append(item); }
    const adoptionReasons = blockers(true);
    $("adopt-reference").disabled = busy || adoptionReasons.length > 0;
    $("adopt-reference").title = adoptionReasons.join("；");
    $("adopt-readiness").textContent = adoptionReasons.join(" · "); $("adopt-readiness").hidden = !adoptionReasons.length;
    $("cancel-run").disabled = busy || current?.cancel_requested || current?.status === "cancelling" || D?.parent(current) && D.projection(current,"activity")?.safe_to_cancel === false;
    if (assetControlsRun(current)) $("cancel-run").disabled = true;
    $("new-scene-run").disabled = busy || blockers(false, "generate", true).length > 0;
    $("new-scene-run").title = blockers(false, "generate", true).join("；") || "根据输入框需求另外生成 1 张概念图，当前场景保持不变";
    $("continue-evaluation").hidden = !["edit","conversation"].includes(operation);
    $("continue-reference").disabled = operation === "conversation";
    $("open-import-scene").disabled = $("empty-import-scene").disabled = busy || projectLoading || !project || !health?.ok || runs.some(inProgress);
    $("open-legacy-runs").disabled = busy || projectLoading || !project || !health?.ok;
    $("empty-generate-scene").disabled = busy || projectLoading || !project || runs.some(inProgress);
    $("new-scene-run").textContent = project?.current_scene ? "另建新场景" : "生成新场景";
    renderProjectStart();
    renderSceneAction();
    renderWorkspaceState();
  }
  function mainStopTarget() { return C?.stopTarget(runs,project?.id,run=>Boolean(D?.readonly(run))) || null; }
  function stopProblem(run) {
    const pending = submissionFor(project?.id);
    if (pending && ["unknown","submitting"].includes(pending.state) && !pending.run_id) return "先核对原提交身份；不会猜测终止目标。";
    if (D?.projection(run,"activity")?.safe_to_cancel === false) return "当前操作不可安全终止，请核对当前请求。";
    const child = D?.parent(run) && D.childFor(run,runs);
    if (D?.parent(run) && run.active_child_id && (!child || directorChildReadErrors.has(run.active_child_id))) return "活动子请求尚未核对，请先刷新本请求。";
    if (assetControlsRun(run) || child && assetControlsRun(child)) return "资产操作待核对，请从本请求的资产入口明确处理。";
    return "";
  }
  async function stopMainRequest() {
    const target=mainStopTarget(),context=scope();
    if (!target || busy || stopProblem(target) || target.cancel_requested || stopAttempts.get(target.id) && stopAttempts.get(target.id)!=="unknown") return;
    const queryOnly=stopAttempts.get(target.id)==="unknown";let posted=false,acknowledged=false;
    stopAttempts.set(target.id,"reading");updateReadiness();
    try {
      const fresh=await api(`/api/runs/${encodeURIComponent(target.id)}`);
      if (!isCurrent(context) || fresh?.id!==target.id || fresh.project_id!==context.id) return;
      if(fresh.operation!==target.operation || D?.parent(target) && fresh.director?.task?.task_id!==target.director?.task?.task_id) {stopAttempts.delete(target.id);return;}
      mergeConversationRuns([fresh]);
      if(current?.id===fresh.id){current=fresh;renderRun();}
      if (TERMINAL.has(fresh.status) || fresh.cancel_requested) {stopAttempts.delete(target.id);return;}
      if (queryOnly) {stopAttempts.set(target.id,"unknown");error("终止回执尚未确认；请核对原请求，不会自动重发。");return;}
      if (D?.parent(fresh) && fresh.active_child_id) await readDirectorChild(fresh);
      if (!isCurrent(context)) return;
      if (mainStopTarget()?.id!==target.id || stopProblem(fresh)) {stopAttempts.delete(target.id);return;}
      if (!D?.parent(fresh) && fresh.status === "awaiting_resume") {
        await conversationRunAction(fresh.id,"end",context);stopAttempts.delete(target.id);return;
      }
      stopAttempts.set(target.id,"stopping");updateReadiness();
      posted=true;await api(`/api/runs/${encodeURIComponent(target.id)}/cancel`,{});
      acknowledged=true;
      if (!isCurrent(context)) return;
      const confirmed=await api(`/api/runs/${encodeURIComponent(target.id)}`);
      if (!isCurrent(context) || confirmed?.id!==target.id || confirmed.project_id!==context.id) return;
      mergeConversationRuns([confirmed]);
      if(current?.id===confirmed.id){current=confirmed;renderRun();}
      if (confirmed.cancel_requested || TERMINAL.has(confirmed.status)) stopAttempts.delete(target.id);
      renderConversation();
    } catch(e) {
      if (!isCurrent(context)) return;
      if (!posted && !queryOnly) {stopAttempts.delete(target.id);error("尚未发送终止，读取暂不可用，可重试。");return;}
      if (posted && !acknowledged && e.status && e.status>=400 && e.status<500 && !e.outcomeUnknown) stopAttempts.delete(target.id);
      else stopAttempts.set(target.id,"unknown");
      error("终止请求需要核对；已保留原任务，未创建新的执行。");
    } finally {
      if(!posted && stopAttempts.get(target.id)==="reading")stopAttempts.delete(target.id);
      if(posted && !isCurrent(context) && stopAttempts.get(target.id)==="stopping")stopAttempts.set(target.id,"unknown");
      if(isCurrent(context)){updateReadiness();schedulePoll();}
    }
  }
  function renderEditRoundSettings() {
    const natural=Boolean(director),editing=natural ? director.taskContext().existing : Boolean(project?.current_scene);
    loadBudgetChoice();
    $("local-round-target-field").hidden = !editing;
    $("new-run-resources").hidden = !editing;
    $("legacy-iteration-settings").hidden = natural || editing;
    const preset = budgetPreset();
    $("local-round-budget-preview").textContent = !editing ? "" : !preset
      ? "资源上限待后端确认；目标不保证达到。"
      : `后端预设 · 正式保存最多 ${preset.saved_round_limit ?? "—"} 次；每局部轮草稿 ${preset.max_edit_attempts_per_round ?? "—"} 次、Run草稿总尝试 ${preset.max_edit_attempts ?? "—"} 次；Builder模型请求 ${preset.max_brain_calls ?? "—"} 次、工具 ${preset.max_tool_calls ?? "—"} 次、观察 ${preset.max_observations ?? "—"} 张。累计活动时间仅统计；${natural ? "同任务总额与累计消耗沿用后台记录，本次目标不追加额度。" : "新Run由后端冻结额度，目标不保证达到。"}`;
  }
  function renderEnvironment() {
    const connected = Boolean(health?.ok), blender = project?.blender || health?.blender;
    $("execution-connection").textContent = connected ? "● 本地执行服务已连接" : "○ 本地执行服务未连接";
    $("execution-connection").classList.toggle("is-connected", connected);
    const mode = $("execution-mode");
    for (const option of mode.options) option.disabled = !modeAvailable(option.value);
    if (!modeChosen && !modeAvailable(mode.value) && !project?.current_scene) mode.value = modeAvailable("headless") ? "headless" : modeAvailable("attached") ? "attached" : "headless";
    const selected = mode.value || "headless";
    $("execution-mode-help").textContent = selected === "attached"
      ? (blender?.modes?.attached?.reason || "连接已打开的 Blender；自动比较读取保存的候选版本，手动修改暂不进入本轮比较。")
      : "后台建立独立的 .blend 文件和渲染，不修改已打开的原场景。";
    $("execution-environment").textContent = !connected ? "执行服务未连接。按 README 启动本地后端后，点击重新检测。"
      : blender?.available ? `检测到 Blender${blender.version ? ` · ${blender.version}` : ""}${blender.path ? `\n${blender.path}` : ""}`
      : "未检测到 Blender。安装 Blender，或设置 BLENDER_PATH 后重新启动后端。";
    if (connected && !modeAvailable("attached")) {
      const note = document.createElement("span"); note.className = "field-help";
      note.textContent = `Live 模式：${blender?.modes?.attached?.reason || "尚未连接 Blender 桥接服务"}`;
      $("execution-environment").append(note);
    }
    updateReadiness();
  }
  function dialogError(id, text = "") { $(id).textContent = text; $(id).hidden = !text; }
  function renderProject() {
    const list = $("project-list"), signature = JSON.stringify(projects.map((item) => [item.id, item.name]));
    if (signature !== projectListSignature) {
      projectListSignature = signature; list.replaceChildren();
      for (const item of projects) {
        const row = button(item.name || "未命名项目", () => selectProject(item.id), "project-list-row");
        row.dataset.projectId = item.id; row.title = item.name || "未命名项目"; list.append(row);
      }
    }
    for (const row of list.children) {
      const selected = row.dataset.projectId === project?.id;
      row.classList.toggle("is-selected", selected); row.setAttribute("aria-pressed", String(selected));
    }
    $("project-list-empty").hidden = projects.length > 0;
    $("project-actions").hidden = !project;
    $("open-import-scene").textContent = project?.current_scene ? "导入其他 .blend 场景（预览 / 采用）" : "导入 .blend";
    $("project-path").textContent = project?.path || "先选择场景的开发文件夹。";
    $("project-breadcrumb").textContent = `${project?.name || "场景项目"} /`;
    $("project-scene-label").textContent = !project ? "尚未选择项目" : project.current_scene ? `当前场景：${project.current_scene.title || project.current_scene.scene_id || "已采用版本"}` : "尚未设置当前场景";
    window.SceneLoopWorkbench?.setProjectContext?.(project);
    creation?.select(project?.id || null);
    renderWorkspaceState();
    renderRefinement();
    renderEnvironment();
  }
  function renderProjectStart() {
    $("welcome").hidden = projectLoading || Boolean(project?.current_scene);
    $("welcome-title").textContent = project ? "从想法开始" : "先打开一个开发项目";
    $("welcome-description").textContent = project
      ? "当前项目尚无 Working Scene。可以直接讨论场景想法，或描述要创建、修改和继续完善的目标。可添加参考图片，也可导入已有 .blend 作为起点。"
      : "从左侧打开 / 创建项目，选择场景的开发文件夹。";
    $("scene-initialization").hidden = $("welcome-hint").hidden = !project;
  }
  const REVIEW_REASON = {
    goal_changed: "目标已变化", criteria_changed: "受评标准已变化", linkage_changed: "问题关联已变化",
    scene_changed: "场景版本已变化", working_version_changed: "Working版本已变化", stage_changed: "阶段已变化",
    input_unavailable: "受评输入不可用", evidence_unavailable: "证据不可用", files_changed: "证据文件已变化",
    reference_changed: "参考已变化", candidate_not_working: "候选尚非Working", no_explicit_review: "尚无显式逐项复查",
  };
  const reviewReasons = (reasons) => (reasons || []).map((reason) => REVIEW_REASON[reason] || `待核对原因：${reason}`).join("、");
  const sceneLabel = (selection) => selection ? `${selection.run_id || "未知Run"} / ${selection.revision_id || "导入版"}` : "未知场景";
  function refinementLine(parent, text, tag = "p") {
    const line = document.createElement(tag); line.textContent = text; parent.append(line); return line;
  }
  // Only this view owns these signatures and disclosure keys; project epochs still own requests.
  let refinementRenderSignature = null, refinementSyncing = false, refinementReturnFocus = null;
  function refinementText(id, text) { if ($(id).textContent !== text) $(id).textContent = text; }
  function refinementFold(parent, key, label) {
    const node = document.createElement("details"); node.dataset.refinementKey = key;
    const summary = refinementLine(node, label, "summary"); summary.dataset.refinementFocus = key;
    parent.append(node); return node;
  }
  function refinementAudit(parent, key, value) {
    const fold = refinementFold(parent, key, "来源与审计信息");
    refinementLine(fold, JSON.stringify(value, null, 2), "pre"); return fold;
  }
  function closeRefinementForProject() {
    refinementReturnFocus = null;
    if ($("refinement-dialog").open) $("refinement-dialog").close();
    for (const fold of $("refinement-dialog").querySelectorAll("[data-refinement-key]")) fold.open = false;
    $("refinement-detail-scroll").scrollTop = 0;
  }
  function renderRefinement() {
    $("project-refinement").hidden = !project;
    const view = refinementProjectId === project?.id ? refinement : null;
    const sync = refinementError || (refinementSyncing ? (view ? "正在同步；显示上次读取。" : "正在读取计划与质量…") : view ? "" : "尚未读取计划与质量。");
    refinementText("refinement-state", sync); refinementText("refinement-detail-state", sync);
    $("open-refinement-details").disabled = !project;
    const binding = current?.project_id === project?.id ? current?.refinement_method : null;
    const preview = currentRevision();
    const signature = JSON.stringify([project?.id, view, project?.current_scene, project?.workspace?.working_version,
      current?.project_id, current?.id, preview?.id, binding]);
    if (signature === refinementRenderSignature) return;
    refinementRenderSignature = signature;
    const chatPosition = conversationPosition(), dialog = $("refinement-dialog"), scroll = $("refinement-detail-scroll");
    const top = scroll.scrollTop, opened = new Set([...dialog.querySelectorAll("[data-refinement-key]")].filter((node) => node.open).map((node) => node.dataset.refinementKey));
    const focusKey = dialog.contains(document.activeElement) ? document.activeElement?.dataset?.refinementFocus : null;
    const items = $("refinement-items"), issues = $("refinement-issues"), historicalIssues = $("refinement-historical-issues"), history = $("refinement-history");
    items.replaceChildren(); issues.replaceChildren(); historicalIssues.replaceChildren(); history.replaceChildren();
    for (const id of ["refinement-goal", "refinement-focus", "refinement-quality", "refinement-judge", "refinement-counts", "refinement-detail-goal", "refinement-source", "refinement-method", "refinement-raw"]) refinementText(id, "");
    if (!project || !view) {
      if (project) { refinementText("refinement-goal", "当前目标：尚未读取"); refinementText("refinement-quality", "独立质量：待Judge / 待复查"); }
      restoreConversationPosition(chatPosition); return;
    }
    const plan = view.plan, evaluation = view.evaluation || {}, selection = evaluation.current_scene;
    const samePreview = !current || (current.project_id === project.id && current.id === selection?.run_id
      && (preview?.id ?? null) === (selection?.revision_id ?? null));
    const sameWorking = Boolean(project.current_scene?.run_id && selection?.run_id && typeof evaluation.working_version === "number")
      && project.workspace?.working_version === evaluation.working_version
      && project.current_scene?.run_id === selection?.run_id
      && (project.current_scene?.revision_id ?? null) === (selection?.revision_id ?? null);
    const showCurrent = samePreview && sameWorking;
    const entries = Array.isArray(evaluation.evaluations) ? evaluation.evaluations : [];
    const latest = showCurrent ? entries.filter((entry) => entry.current === true).at(-1) : null;
    const planItems = Array.isArray(plan?.items) ? plan.items : [];
    const ordered = (plan?.order || []).map((id) => planItems.find((item) => item.id === id)).filter(Boolean);
    const orderedItems = [...new Set([...ordered, ...planItems])];
    const allIssues = Array.isArray(evaluation.issues) ? evaluation.issues : [];
    const unresolved = Array.isArray(evaluation.unresolved_issues) ? evaluation.unresolved_issues : allIssues.filter((issue) => issue.current_resolved !== true || issue.review_required === true);
    const unresolvedIds = new Set(unresolved.map((issue) => issue.id));
    const pending = planItems.filter((item) => !showCurrent || item.facts?.judge_passed !== true).length;
    const goal = plan ? `当前目标：${plan.goal?.content?.request || "未记录"}` : "尚无Plan；不会从整体裁判结论补造逐项通过。";
    refinementText("refinement-goal", goal); refinementText("refinement-detail-goal", goal);
    refinementText("refinement-focus", `当前焦点：${planItems.find((item) => item.id === plan?.focus_item_id)?.title || "未指定"}`);
    refinementText("refinement-quality", !showCurrent ? "独立质量：当前预览或工作版待同步；历史结论不表示当前通过。"
      : latest ? `整体Working：Judge ${latest.result?.verdict === "pass" ? "通过" : latest.result?.verdict || "待复查"} · 逐项明确通过 ${planItems.length - pending}/${planItems.length}` : "独立质量：待Judge / 待复查；逐项另看显式结论。");
    const brief = latest?.result;
    refinementText("refinement-judge", latest ? `最近有效Judge：${brief?.summary || brief?.description || (brief?.next_focus || []).join("；") || brief?.verdict || "未说明"}` : "最近有效Judge：暂无适用于当前查看范围的评审。");
    refinementText("refinement-counts", `${showCurrent ? "待处理 / 待复查工作项" : "待核对工作项"} ${pending} · ${showCurrent ? "未解决 / 待复查问题" : "上次登记未解决问题"} ${unresolved.length}`);
    refinementText("refinement-source", `投影对应 Working Version ${evaluation.working_version ?? "—"} · ${sceneLabel(selection)}。`
      + (!samePreview ? ` 当前预览 ${current?.id || "—"} / ${preview?.id || "—"} 属于其他版本；以下Working结论不表示该预览通过。`
        : !sameWorking ? " 工作版身份尚未同步，请重新读取；暂不显示当前通过。" : " 当前预览与Working一致。"));
    refinementText("refinement-method", binding ? `所选Run方法来源：${binding.source || "旧记录"} · ${binding.status || "未知"} · ${binding.sha256 || binding.version || "未绑定历史标识"}` : "");
    refinementText("refinement-raw", JSON.stringify({ ...view, method_binding: binding }, null, 2));
    for (const [index, item] of orderedItems.entries()) {
      const row = document.createElement("li"), facts = item.facts || {}, key = `item:${item.id ?? index}`;
      row.dataset.itemId = item.id; row.dataset.quality = !showCurrent ? "historical" : facts.judge_passed === true ? "passed" : "review-required";
      refinementLine(row, `${item.title || "未命名工作项"}${plan.focus_item_id === item.id ? " · 当前焦点" : ""}`, "h4");
      refinementLine(row, `规划状态：${item.status || "未记录"}`);
      refinementLine(row, !showCurrent ? "独立质量：查看其他范围或工作版待同步，当前通过标签不适用于该预览。"
        : facts.judge_passed === true ? "独立质量：当前Working该项已显式通过。" : "独立质量：该项待处理或待复查；整体PASS与遗漏均不补为通过。");
      const detail = refinementFold(row, key, "工作项详情");
      refinementLine(detail, `期望：${item.desired || "未记录"} · 依据：${item.basis || "未记录"} · 标准：${(item.criteria || []).join("；")}`);
      refinementLine(detail, `Builder自检：${item.self_check || "未提供"}（规划状态：${item.status || "未记录"}，不作为独立质量结论）。`);
      refinementLine(detail, `下一步：${item.next_step || "待明确"}`);
      refinementLine(detail, facts.saved === true ? `正式保存：有 ${facts.operation_receipts?.length || 0} 条场景回执；保存不代表质量通过。` : "正式保存：该项尚无关联回执。");
      const observations = refinementFold(detail, `${key}:observations`, `观察记录（${item.observations?.length || 0}）`);
      for (const observation of item.observations || []) {
        refinementLine(observations, `观察：${observation.text || "未说明"}${observation.review_required ? ` · 待复查：${reviewReasons(observation.review_reasons)}` : ""}`);
        refinementAudit(observations, `${key}:observation:${observation.id ?? observations.children.length}`, observation);
      }
      const reviews = refinementFold(detail, `${key}:reviews`, `逐项Judge复查（${facts.item_reviews?.length || 0}）`);
      for (const [reviewIndex, review] of (facts.item_reviews || []).entries()) {
        const reviewKey = `${key}:review:${review.evaluation_id ?? reviewIndex}`;
        const fold = refinementFold(reviews, reviewKey, `逐项Judge ${review.verdict || "未知"} · ${showCurrent && review.current ? "服务端当前适用" : "历史/待复查"}`);
        refinementLine(fold, `改善：${review.improved || "未说明"}；剩余：${review.remaining || "未说明"}；期望变化：${review.expected_effect || "未说明"}`);
        const source = entries.find((entry) => entry.evaluation_id === review.evaluation_id);
        refinementLine(refinementAudit(fold, `${reviewKey}:audit`, { review, source }), `Working Version ${source?.working_version ?? "—"} · ${sceneLabel(source?.selection)} · Evaluation ${review.evaluation_id} · ${reviewReasons(review.reasons)}`);
      }
      const audit = refinementAudit(detail, `${key}:audit`, item);
      for (const receipt of facts.operation_receipts || []) refinementLine(audit, `保存来源：Working Version ${receipt.working_version ?? "—"} · ${sceneLabel(receipt.selection)} · ${receipt.operation_id}`, "small");
      items.append(row);
    }
    if (!planItems.length) refinementLine(items, "尚无逐项工作记录。", "li");
    for (const [index, issue] of allIssues.entries()) {
      const row = document.createElement("li"), key = `issue:${issue.id ?? index}`; row.dataset.issueId = issue.id;
      refinementLine(row, `${issue.description || "待处理问题"} · ${showCurrent && issue.current_resolved === true ? "当前明确解决" : "待处理/待复查"}`);
      const reviews = refinementFold(row, `${key}:reviews`, `问题复查（${issue.reviews?.length || 0}）`);
      for (const [reviewIndex, review] of (issue.reviews || []).entries()) {
        const reviewKey = `${key}:review:${review.evaluation_id ?? reviewIndex}`;
        const fold = refinementFold(reviews, reviewKey, `复查 ${review.verdict || "未知"} · ${showCurrent && review.current ? "服务端当前适用" : "历史/待复查"}`);
        refinementLine(fold, `改善：${review.improved || "未说明"}；剩余：${review.remaining || "未说明"}；期望变化：${review.expected_effect || "未说明"}`);
        refinementAudit(fold, `${reviewKey}:audit`, review);
      }
      refinementLine(refinementAudit(row, `${key}:audit`, issue), reviewReasons(issue.applicability_reasons));
      (unresolvedIds.has(issue.id) ? issues : historicalIssues).append(row);
    }
    if (!unresolved.length) refinementLine(issues, "尚无未解决登记问题；不代表已验收。", "li");
    if (!historicalIssues.children.length) refinementLine(historicalIssues, "尚无已解决或历史问题。", "li");
    for (const [index, entry] of [...entries].reverse().entries()) {
      const row = document.createElement("li"), key = `evaluation:${entry.evaluation_id ?? index}`;
      const fold = refinementFold(row, key, `独立Judge ${entry.result?.verdict || "未知"} · ${showCurrent && entry.current ? "基础范围当前适用；逐项另看各自行" : "历史/待复查"}`);
      refinementLine(fold, JSON.stringify(entry.result || {}, null, 2), "pre");
      refinementLine(refinementAudit(fold, `${key}:audit`, entry), `${entry.application_status || "未记录"} · Working Version ${entry.working_version ?? "—"} · ${sceneLabel(entry.selection)} · ${reviewReasons(entry.reasons)} · Evaluation ${entry.evaluation_id}`);
      history.append(row);
    }
    if (!entries.length) refinementLine(history, "尚无独立评审记录。", "li");
    for (const fold of dialog.querySelectorAll("[data-refinement-key]")) fold.open = opened.has(fold.dataset.refinementKey);
    if (focusKey && dialog.open) [...dialog.querySelectorAll("[data-refinement-focus]")].find((node) => node.dataset.refinementFocus === focusKey)?.focus({ preventScroll: true });
    scroll.scrollTop = top; restoreConversationPosition(chatPosition);
  }
  async function refreshRefinement() {
    const context = scope(), token = ++refinementLoad;
    refinementSyncing = Boolean(project); refinementError = ""; renderRefinement();
    if (!project) return;
    try {
      const data = await api(`/api/projects/${encodeURIComponent(context.id)}/refinement`);
      if (!isCurrent(context) || token !== refinementLoad) return;
      refinement = data; refinementProjectId = context.id; refinementError = "";
    } catch (e) {
      if (!isCurrent(context) || token !== refinementLoad) return;
      refinementError = `同步失败：${e.message}；${refinementProjectId === context.id && refinement ? "显示上次读取，请重新读取。" : "尚无已读取内容，请重试。"}`;
    }
    refinementSyncing = false; renderRefinement();
  }
  function judgementApplication(judgement, workspace, judgeStatus) {
    const version = judgement?.working_version;
    const workspaceVersion = workspace?.working_version;
    const versionChanged = typeof version === "number" && typeof workspaceVersion === "number"
      && version !== workspaceVersion;
    const stageChanged = Boolean(workspace) && judgement?.target?.stage !== undefined
      && judgement.target.stage !== workspace.stage;
    const batchChanged = Boolean(workspace) && judgement?.target?.batch?.id !== undefined
      && judgement.target.batch.id !== (workspace.batch_start?.id ?? null);
    let state = "none";
    if (judgement) {
      if (judgement.stale === true || judgeStatus === "stale" || judgement.invalid === true) {
        state = "unapplied";
      } else if (judgement.superseded_by_working_version !== undefined || judgement.current === true) {
        state = "applied";
      } else if (judgement.current === false) {
        state = "unapplied";
      } else {
        state = "unknown";
      }
    }
    return { state, version, versionChanged, stageChanged, batchChanged,
      superseded: state === "applied" && (versionChanged || stageChanged || batchChanged) };
  }
  function renderWorkspaceState() {
    const workspace = project?.workspace, version = workspace?.working_version ?? 0;
    const walkthroughReason = walkthroughBusy ? "正在准备独立 Blender 展示窗口…"
      : projectLoading ? "等待项目载入"
      : !project?.current_scene ? "先建立正式 Working Scene"
      : !health?.ok ? "执行服务未连接"
      : !(project?.blender || health?.blender)?.available ? "未找到可运行的 Blender"
      : project.scene_availability?.status === "unavailable" ? "当前工作版文件不完整，请检查项目状态" : "";
    $("open-walkthrough").disabled = Boolean(walkthroughReason);
    $("open-walkthrough").title = walkthroughReason || `在独立 Blender 窗口漫游 Working Version ${version} · ${project.current_scene.revision_id || "导入版"}`;
    $("working-version").textContent = project?.current_scene ? `Working Version ${version}` : "Working Version —";
    $("working-scene-state").textContent = project?.current_scene
      ? `${project.current_scene.title || project.current_scene.scene_id} · ${project.current_scene.revision_id || "导入版"}`
      : "尚未建立工作版。";
    const judgement = workspace?.latest_judgement;
    const application = judgementApplication(judgement, workspace);
    const failureText = `裁判待验收 · Working Version ${judgement?.working_version} · ${judgement?.message || "执行失败"}`;
    $("judge-state").textContent = !judgement ? "尚无裁判结论。"
      : application.state === "unapplied"
        ? (judgement.invalid === true
          ? `裁判登记被拒绝（未应用）：${judgement.invalid_reason || judgement.message || "无效判决"}`
          : `历史裁判（未应用）：${judgement.verdict || "uncertain"} · 对应 Working Version ${judgement.working_version}`)
        : application.superseded
          ? (judgement.status === "failed"
            ? `已应用的失败记录 · 当时针对 Working Version ${judgement.working_version}，当前版本/Stage/批次已变化，需重新验收`
            : `已应用的历史裁判：${judgement.verdict || "uncertain"} · 当时针对 Working Version ${judgement.working_version}，当前版本/Stage/批次已变化，需重新验收`)
          : application.state === "unknown"
            ? (judgement.status === "failed"
              ? `裁判记录（归属未标记）· Working Version ${judgement.working_version} · ${judgement.message || "执行失败"}，请重新验收`
              : `裁判记录（归属未标记）：${judgement.verdict || "uncertain"} · Working Version ${judgement.working_version}，需重新验收`)
            : judgement.status === "failed"
              ? failureText
              : `最新裁判：${judgement.verdict} · 对应 Working Version ${judgement.working_version}`;
    const checkpoint = workspace?.checkpoints?.at(-1);
    $("checkpoint-state").textContent = checkpoint
      ? `最近检查点：${checkpoint.title} · Working Version ${checkpoint.working_version}`
      : "尚无手动检查点。";
    const picker = $("checkpoint-picker"), chosen = picker.value;
    picker.replaceChildren();
    const empty = document.createElement("option"); empty.value = ""; empty.textContent = "选择检查点…"; picker.append(empty);
    for (const item of workspace?.checkpoints || []) {
      const option = document.createElement("option"); option.value = item.id;
      option.textContent = `${item.title} · Working Version ${item.working_version}`; picker.append(option);
    }
    picker.value = (workspace?.checkpoints || []).some((item) => item.id === chosen) ? chosen : (checkpoint?.id || "");
    const running = runs.some(inProgress);
    $("save-checkpoint").disabled = busy || !project?.current_scene || running;
    $("undo-working").disabled = busy || !workspace?.recoveries?.length || running;
    $("restore-batch").disabled = busy || !workspace?.batch_start || running;
    picker.disabled = busy || !workspace?.checkpoints?.length || running;
    $("restore-checkpoint").disabled = picker.disabled || !picker.value;
    $("workspace-stage").value = workspace?.stage || "structure";
    $("recovery-limit").value = String(workspace?.recovery_limit || 1);
    $("workspace-stage").disabled = $("recovery-limit").disabled = busy || !project || running;
  }
  async function openWalkthrough() {
    if ($("open-walkthrough").disabled || walkthroughBusy || !project?.current_scene) return;
    const context = scope(), sourceProject = project;
    walkthroughBusy = true; renderWorkspaceState();
    $("walkthrough-status").textContent = `正在准备 Working Version ${sourceProject.workspace?.working_version} 的独立展示副本…`;
    if (!$("walkthrough-dialog").open) $("walkthrough-dialog").showModal();
    try {
      const data = await api(`/api/projects/${encodeURIComponent(sourceProject.id)}/walkthrough`, {
        expected_project_revision: sourceProject.revision,
        expected_working_version: sourceProject.workspace?.working_version,
      }, 310000); // This new action includes a Blender preparation with its own 300s timeout.
      if (!isCurrent(context)) return;
      $("walkthrough-status").textContent = data.message || `已启动 Blender 展示窗口 · Working Version ${data.source?.working_version}；请在视口手动开始步行导航。`;
      if (data.controls) $("walkthrough-controls").textContent = data.controls;
    } catch (failure) {
      if (isCurrent(context)) $("walkthrough-status").textContent = `${failure.message} 若响应丢失，先确认是否已有新 Blender 窗口，再手动重试。`;
    } finally { walkthroughBusy = false; renderWorkspaceState(); }
  }
  async function workspaceAction(action, extra = {}) {
    if (!project || busy) return;
    const context = scope(); busy = true; error(); updateReadiness();
    try {
      const data = await api(`/api/projects/${encodeURIComponent(project.id)}/workspace/${action}`,
        { expected_revision: project.revision, ...extra });
      if (!isCurrent(context)) return;
      project = data.project; projects = projects.map((item) => item.id === project.id ? project : item);
      await refreshRuns(); renderProject();
    } catch (e) { if (isCurrent(context)) { error(e.message); await refreshRuns().catch(() => {}); } }
    finally { busy = false; updateReadiness(); renderWorkspaceState(); }
  }
  function rememberProject(id) {
    try { if (id) localStorage.setItem(PROJECT_KEY, id); else localStorage.removeItem(PROJECT_KEY); }
    catch { dialogError("project-error", "本次项目已打开，但浏览器未保存选择；下次请重新选择。"); }
  }
  async function selectProject(id) {
    const epoch = ++projectEpoch; dialogError("project-error"); error();
    initialConversationPending = true; conversationScrollVersion++;
    observedConversationRuns.clear(); executionDialogSession++;
    director?.reset(); directorDetailParentId = null;
    resourceRequests?.reset();
    localWarnings?.reset();
    directorChildReadErrors.clear(); directorReadSequence.clear(); directorStatusPending.clear();
    assetViews.clear(); assetBase = null; assetBaseRead++; assetOptContext = ""; $("asset-generation-enabled").checked = false; resetAssetForm();
    conversationRows.clear(); retainedTurns.clear(); runWrites.clear(); runReadSequence++; submissionReadSequence++;
    historyPage = null; historyLoading = false; runMutation = 0; followConversation = true; readerPosition = null;
    conversationObserver?.disconnect(); conversationObserver = null;
    closeExecutionDetails();
    requestDisclosureButtons.clear();
    $("project-actions").open = false;
    if ($("import-scene-dialog").open) $("import-scene-dialog").close();
    if ($("legacy-runs-dialog").open) $("legacy-runs-dialog").close();
    refinement = null; refinementProjectId = null; refinementError = ""; refinementLoad++; refinementSyncing = false; closeRefinementForProject();
    clearTimeout(timer); if ($("image-dialog").open) $("image-dialog").close(); current = null; runs = []; project = null; viewedRevision = null; showGenerated = false;
    previewSignature = null; revisionSignature = null; conversationSignature = null;
    $("continue-reference").checked = false;
    projectLoading = Boolean(id); renderProject(); renderConversation(); renderRun();
    if (!id) { rememberProject(null); return; }
    try {
      const data = await api(`/api/projects/${encodeURIComponent(id)}`);
      if (epoch !== projectEpoch) return;
      project = data.project; projectLoading = false; rememberProject(project.id); renderProject();
      if (submissionFor(project.id)) renderConversation();
      installConversationObserver();
      await refreshRuns();
    } catch (e) { if (epoch === projectEpoch) dialogError("project-error", e.message); }
    finally { if (epoch === projectEpoch) { projectLoading = false; updateReadiness(); } }
  }
  async function loadProjects() {
    const epoch = projectEpoch;
    const data = await api("/api/projects");
    if (epoch !== projectEpoch) return false;
    projects = Array.isArray(data.projects) ? data.projects : []; projectsAvailable = true;
    const selectedId = project?.id;
    if (selectedId && projects.some((item) => item.id === selectedId)) { renderProject(); await refreshRuns(); return; }
    let saved = null; try { saved = localStorage.getItem(PROJECT_KEY); } catch { /* Explicit project selection remains available. */ }
    if (saved && projects.some((item) => item.id === saved)) await selectProject(saved);
    else { if (project) await selectProject(null); else renderProject(); }
  }
  async function detectEnvironment() {
    $("refresh-environment").disabled = true;
    try { health = await api("/api/health"); renderEnvironment(); }
    catch (e) { health = null; renderEnvironment(); error(e.message); $("refresh-environment").disabled = false; return; }
    const epoch = projectEpoch;
    try { if (await loadProjects() !== false) dialogError("project-error"); }
    catch (e) { if (epoch === projectEpoch) { projectsAvailable = false; dialogError("project-error", `项目服务未连接：${e.message}`); updateReadiness(); } }
    finally { $("refresh-environment").disabled = false; }
  }
  function openProjectDialog() {
    projectDialogSession++; dialogError("project-dialog-error"); $("project-dialog").showModal(); $("project-folder").focus();
  }
  async function choosePath(kind) {
    const folder = kind === "folder", session = folder ? projectDialogSession : importDialogSession;
    const id = folder ? "pick-project-folder" : "pick-scene-file", errorId = folder ? "project-dialog-error" : "import-scene-error";
    $(id).disabled = true;
    try {
      const data = await api(`/api/projects/${folder ? "pick-folder" : "pick-scene-file"}`, {}, null);
      if (session !== (folder ? projectDialogSession : importDialogSession)) return;
      if (data.path) $(folder ? "project-folder" : "import-scene-path").value = data.path;
    } catch (e) { if (session === (folder ? projectDialogSession : importDialogSession)) dialogError(errorId, `${e.message} 也可手动填写绝对路径。`); }
    finally { $(id).disabled = false; }
  }
  async function saveProject(event) {
    event.preventDefault(); if ($("save-project").disabled) return;
    const path = $("project-folder").value.trim(), name = $("project-name").value.trim(), session = projectDialogSession;
    if (!path) { dialogError("project-dialog-error", "请选择开发文件夹或填写绝对路径。"); return; }
    $("save-project").disabled = true;
    try {
      const data = await api("/api/projects", { path, ...(name ? { name } : {}) });
      if (session !== projectDialogSession) return;
      projects = [...projects.filter((item) => item.id !== data.project.id), data.project]; projectsAvailable = true;
      $("project-dialog").close(); await selectProject(data.project.id);
    } catch (e) { if (session === projectDialogSession) dialogError("project-dialog-error", e.message); }
    finally { $("save-project").disabled = false; }
  }
  async function loadRun(id, reveal = false, allowForeign = false) {
    if(requestControlIntent && requestControlIntent.id!==id)closeExecutionDetails();
    const context = scope(), selection = ++runSelectionEpoch;
    try {
      const run = await api(`/api/runs/${encodeURIComponent(id)}`);
      if (!run || run.id !== id) throw new Error("执行记录身份不匹配，请重新读取。");
      if (!isCurrent(context) || selection !== runSelectionEpoch) return;
      if (!allowForeign && run.project_id && run.project_id !== project?.id) return;
      mergeRun(run, reveal, allowForeign);
      if (reveal) openArtifact(run.artifacts?.some((item) => item.kind === "render") ? "render" : "concept");
      return run;
    } catch (e) { if (isCurrent(context) && selection === runSelectionEpoch) error(e.message); }
  }
  function conversationPosition() {
    const scroll = $("message-scroll");
    const viewport = scroll.getBoundingClientRect?.();
    const anchor = viewport && [...$("message-list").children].find(node => node.getBoundingClientRect().bottom > viewport.top);
    const entry = anchor && [...anchor.querySelectorAll(".timeline-entry")].find(node=>node.getBoundingClientRect?.().bottom>viewport.top && node.getBoundingClientRect().height>0);
    return { top: scroll.scrollTop, height: scroll.scrollHeight, atBottom: scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 40 || followConversation && Math.abs(scroll.scrollTop - lastConversationTop) < 1,
      anchor: anchor?.dataset.conversationKey, offset: anchor ? anchor.getBoundingClientRect().top - viewport.top : null,
      entryAnchor:entry?.dataset.timelineId,entryOffset:entry?entry.getBoundingClientRect().top-viewport.top:null };
  }
  function restoreConversationPosition(position) {
    const scroll = $("message-scroll"), context = scope(), version = ++conversationScrollVersion;
    const restore = () => {
      const anchor = position.anchor && conversationRows.get(position.anchor)?.node;
      const entry = position.entryAnchor && conversationRows.get(position.anchor)?.timelineNodes?.get(position.entryAnchor)?.node;
      if (position.atBottom) scroll.scrollTop = scroll.scrollHeight;
      else if(entry?.getBoundingClientRect?.().height>0 && scroll.getBoundingClientRect) scroll.scrollTop+=entry.getBoundingClientRect().top-scroll.getBoundingClientRect().top-position.entryOffset;
      else if (anchor?.getBoundingClientRect && scroll.getBoundingClientRect) scroll.scrollTop += anchor.getBoundingClientRect().top - scroll.getBoundingClientRect().top - position.offset;
      else scroll.scrollTop = position.prepend ? position.top + scroll.scrollHeight - position.height : position.top;
      lastConversationTop = scroll.scrollTop; followConversation = position.atBottom;
      readerPosition = conversationPosition();
    };
    restore();
    const expectedTop = scroll.scrollTop;
    if (window.requestAnimationFrame) window.requestAnimationFrame(() => {
      if (isCurrent(context) && version === conversationScrollVersion && Math.abs(scroll.scrollTop - expectedTop) < 1) restore();
      else if (isCurrent(context) && version === conversationScrollVersion) followConversation = false;
    });
  }
  function installConversationObserver() {
    if (!window.ResizeObserver || conversationObserver) return;
    const context = scope();
    conversationObserver = new ResizeObserver(() => {
      if (!isCurrent(context)) return;
      const scroll = $("message-scroll");
      if (Math.abs(scroll.scrollTop - lastConversationTop) > 1) { followConversation = conversationPosition().atBottom; readerPosition = conversationPosition(); }
      if (followConversation) restoreConversationPosition({ top: 0, atBottom: true });
      else if (readerPosition) restoreConversationPosition({ ...readerPosition, atBottom: false });
    });
    conversationObserver.observe($("message-list"));
    conversationObserver.observe($("message-scroll"));
  }
  function finishInitialConversation() {
    if (!initialConversationPending || !project) return;
    initialConversationPending = false;
    restoreConversationPosition({ top: 0, atBottom: true });
  }
  const projectTurns = () => {
    for (const turn of project?.workspace?.turns || []) if (turn && turnRunId(turn)) retainedTurns.set(turnRunId(turn), turn);
    return [...retainedTurns.values()];
  };
  const turnRunId = (turn) => turn?.run_id || turn?.id;
  const conversationTime = (value) => { const time = Date.parse(value); return Number.isFinite(time) ? time : 0; };
  function mergeConversationRuns(records, startedAt = Infinity) {
    const known = new Map(runs.map(run => [run.id, run]));
    for (const run of records) {
      if (!acceptedRun(run, scope()) || (runWrites.get(run.id) || 0) > startedAt) continue;
      const previous=known.get(run.id);
      if(previous?.updated_at && run.updated_at && conversationTime(run.updated_at)<conversationTime(previous.updated_at))continue;
      if(D?.parent(run) && previous?.director && previous.status!==run.status)directorStatusPending.add(run.id);
      known.set(run.id, { ...previous, ...run, ...(previous?.director && !run.director ? {director:previous.director}:{}) }); runWrites.set(run.id, ++runMutation); observeConversationRun(run);
    }
    runs = [...known.values()].sort((a,b) => conversationTime(b.created_at) - conversationTime(a.created_at) || b.id.localeCompare(a.id));
  }
  async function loadConversationHistory() {
    if (!project || historyLoading || !historyPage?.has_more || !historyPage.next_cursor) return;
    const context = scope(), cursor = historyPage.next_cursor, startedAt = runMutation;
    const position = { ...conversationPosition(), atBottom: false, prepend: true }; historyLoading = true; renderConversation(position);
    try {
      const data = await api(`/api/runs?project_id=${encodeURIComponent(context.id)}&limit=100&before=${encodeURIComponent(cursor)}`);
      if (!isCurrent(context)) return;
      mergeConversationRuns(data.runs || [], startedAt); historyPage = data.page || { has_more: false };
      await refreshDirectorChildren(context);
      if (!isCurrent(context)) return;
      director?.readSucceeded();
      renderConversation(position); renderRunPicker();
    } catch { if (isCurrent(context)) error("更早记录读取失败，已加载记录保持；请重新读取。"); }
    finally { if (isCurrent(context)) { historyLoading = false; renderConversation(conversationPosition()); } }
  }
  function actualObservationMode(run, artifact = null) {
    const renders = (run?.artifacts || []).filter(item => item.kind === "render" && safeArtifactUrl(item.url));
    const observations = run?.agent?.observations || [];
    const observation = artifact
      ? observations.find(row => artifact.observation_id && row?.id === artifact.observation_id)
      : [...observations].reverse().find(row => row && (Object.hasOwn(row, "observation_mode") || renders.some(image => image.observation_id && image.observation_id === row.id)));
    const image = artifact || (observation ? renders.find(item => item.observation_id && item.observation_id === observation.id) : renders.at(-1));
    const receipt = (artifact ? [image?.observation, observation] : [observation, image?.observation]).find(row => row && Object.hasOwn(row, "observation_mode"));
    if (receipt) return ["appearance", "structure"].includes(receipt.observation_mode) ? receipt.observation_mode : "unknown";
    return image ? "appearance" : null;
  }
  function observationModeLabel(mode) {
    return mode === "structure" ? "结构白模观察" : mode === "appearance" ? "外观观察" : mode === "unknown" ? "观察模式未确认" : "";
  }
  function observationImageTitle(run, item) {
    if (item.kind === "concept") return `概念图${item.name ? ` · ${item.name}` : ""}`;
    const view = ({ main: "固定主视角", auxiliary: "固定辅助视角" })[item.view_id] || (item.view_id ? `诊断视角 · ${item.view_id}` : "场景视角");
    return `${observationModeLabel(actualObservationMode(run, item))} · ${view}${item.name ? ` · ${item.name}` : ""}`;
  }
  function safeConversationProgress(run) {
    let stage = run.status === "running" ? run.stage : run.status;
    const tools = { inspect_scene: "检查场景", inspect_geometry: "检查几何", observe_scene: "截图观察", create_scene: "创建场景", apply_edit: "编辑候选", review_candidate: "检查候选效果", finish: "整理结果" };
    let text = STATUS[stage] || "正在处理请求", sequence = 0;
    for (const event of run.events || []) {
      if (Number.isInteger(event.seq)) sequence = Math.max(sequence, event.seq);
      if (!TERMINAL.has(run.status) && Object.hasOwn(STATUS, event.stage)) { stage = event.stage; text = STATUS[event.stage]; }
      const match = typeof event.message === "string" && event.message.match(/^(?:正在执行工具 ([a-z_]+)。|工具 ([a-z_]+) 已返回(结果|需要修正的参数)。)$/);
      const tool = event.tool_name || event.tool || (match && (match[1] || match[2]));
      if (!TERMINAL.has(run.status) && Object.hasOwn(tools, tool || "")) text = `${tools[tool]}${match?.[3] === "结果" ? " · 工具已返回" : match?.[3] ? " · 参数待修正" : " · 处理中"}`;
    }
    const mode = observationModeLabel(actualObservationMode(run));
    if (!TERMINAL.has(run.status) && mode) text += ` · ${mode}`;
    return { text, sequence };
  }
  function conversationRunState(run) {
    if (D?.parent(run)) return `${D.feedback(run,D.childFor(run,runs),resourceView(run))}${run.turn_record_pending || run.director?.recovery?.turn_pending ? "；正在同步最终回复，请勿重复发送" : ""}`;
    if(D?.resourceWaiting(run,null,resourceView(run)))return `工作进展：${D.resourceWaitingText(resourceView(run))}。`;
    const failures = { server_restarted: "服务重启导致执行中断；原请求不会自动重发", invalid_json: "模型响应格式无效，请检查模型配置或缩短需求", invalid_response: "模型响应格式无效，请检查模型配置", incomplete_response: "模型响应不完整，请检查模型输出上限", execution_failed: "本地执行失败，请核对Blender环境与任务计划", provider_error: "模型请求失败，请检查模型配置与服务状态", credential_error: "模型凭据需要检查，请打开模型配置", configuration_required: "模型配置缺失或不完整，请重新选择配置", round_operation_unknown: "草稿写入结果未知，请核对原任务文件" };
    if (["failed", "interrupted"].includes(run.status)) {
      const prior = [...(run.events || [])].reverse().find(event => !TERMINAL.has(event.stage) && Object.hasOwn(STATUS, event.stage));
      return `${failures[run.error?.code] || `${prior ? STATUS[prior.stage] + "阶段：" : ""}${STATUS[run.status]}，请核对原任务记录`}${run.error?.outcome_unknown ? "；执行结果未知" : ""}。已保留原请求和状态，请核对后再决定下一步。`;
    }
    if (TERMINAL.has(run.status)) return run.context_index_status === "failed" ? "最终对话记录未写入；原请求与执行状态已保留。" : awaitingConversationTurn(run) ? "执行已完成，正在同步最终对话记录…" : `${run.status === "completed" ? "请求已完成" : STATUS[run.status] || "执行已结束"}；缺少持久最终回复，原请求与执行状态已保留。`;
    const progress = safeConversationProgress(run);
    return `${progress.text}${progress.sequence ? ` · 进度 ${progress.sequence}` : ""}`;
  }
  function submissionRejection(failure) {
    const messages = {
      "生成任务需要 1–16,000 字的场景需求；较长内容仍可作为本地记录保存。": "需求需为1–16000字，请补充或缩短需求后再次发送。",
      "需求与项目约束合计过长，请缩短后再生成。": "需求与项目约束合计过长，请缩短后再发送。",
      "预算格式无效。": "预算格式无效，请检查金额和币种。",
      "预算金额需要大于 0，且不超过 100,000。": "预算金额需大于0且不超过100000，请修改预算设置。",
      "暂仅支持 CNY 或 USD 预算记录。": "预算币种应为CNY或USD，请修改预算设置。",
      "自动迭代设置格式无效。": "迭代设置格式无效，请检查执行设置。",
      "请选择新建或修改当前场景。": "任务类型无效，请选择新建或修改当前场景。",
      "项目标识无效。": "项目身份无效，请重新打开项目。",
      "项目版本无效。": "项目版本无效，请重新读取项目。",
      "修改需要项目、当前场景和预期项目版本。": "续改缺少正式工作版或项目版本，请重新读取项目。",
      "评价模式需要为意图比较或明确采用参考。": "评价模式无效，请检查沿用参考的执行设置。",
      "项目约束格式无效。": "项目约束格式无效，请检查右侧项目约束。",
      "设计风格或保留要求过长。": "风格或保留要求过长，请缩短右侧项目约束。",
      "首轮需要启用“关键物体具有可辨识形状”。": "首轮需启用关键物体可辨识，请检查右侧项目约束。",
      "请选择有效的 Blender 连接模式。": "Blender连接模式无效，请检查执行设置。",
      "配置字段类型或长度无效。": "模型配置字段无效，请在模型配置中重新选择或保存。",
    };
    const codes = { configuration_required: "模型配置未选择或尚未补全，请打开模型配置。", invalid_revision: "模型配置修订号失效，请重新选择配置。", invalid_brain_settings: "模型输出或思考设置无效，请检查所选模型设置。", invalid_judge_settings: "Judge设置无效，请检查模型与验收频率。", invalid_round_mode: "局部轮设置不支持当前任务，请检查工作版与保存目标。", credential_in_task: "请求包含密钥字段，请只选择已保存的模型配置。", invalid_credential_mode: "密钥保存方式无效，请在模型配置中重新保存。", idempotency_conflict: "原提交身份对应另一份请求，请核对原任务，不要换身份盲目重发。", busy: "已有任务执行中，请等待后再创建新场景。", blender_unavailable: "Blender环境不可用，请重新检测环境或检查连接模式。", profile_conflict: "模型配置已改变，请重新选择当前配置。", working_scene_missing: "当前工作版缺失，请重新读取项目或建立工作版。" };
    return messages[failure.message] || codes[failure.code] || "请求参数未通过校验，服务未提供可安全显示的具体原因；请检查需求长度、模型和项目设置，详细信息可在本地服务核对。";
  }
  function observeConversationRun(run) {
    if (run?.project_id === project?.id && !TERMINAL.has(run.status) && !observedConversationRuns.has(run.id))
      observedConversationRuns.set(run.id, { reads: 0 });
  }
  function awaitingConversationTurn(run) {
    const observed = observedConversationRuns.get(run.id);
    if (D?.parent(run) && (run.turn_record_pending || run.director?.recovery?.turn_pending)) return true;
    return Boolean(observed && run.status === "completed" && run.context_index_status !== "failed"
      && observed.reads < 3 && !projectTurns().some((turn) => turnRunId(turn) === run.id && typeof turn.assistant === "string" && turn.assistant.trim()));
  }
  function executionFacts(run, turn) {
    if (D?.parent(run)) {
      const child = D.childFor(run,runs), facts = child && executionFacts(child,null);
      return facts ? { ...facts, type:"Builder 受委派施工", status:D.state(run,child,resourceView(run)) } : null;
    }
    if ((turn || TERMINAL.has(run.status)) && (["discussion", "clarification"].includes(turn?.kind) || ["discussion", "clarification"].includes(run.agent?.stop_reason))) return null;
    const receipts = (run.scene_commit_receipts || []).filter((row) => ["builder", "initial"].includes(row.kind));
    const commits = [...receipts, ...(run.commits || []), ...(run.initial_working_commit ? [run.initial_working_commit] : [])];
    const versions = [...new Set(commits.map((row) => row.working_version).filter((version) => Number.isInteger(version) && version > 0))];
    const used = run.agent?.used || {};
    const executed = ["edit", "inspection", "import", "generate"].includes(turn?.kind) || ["import", "generate"].includes(run.operation)
      || versions.length || run.agent?.revisions?.length || run.artifacts?.length || used.tool_calls > 0 || used.observations > 0 || used.edit_rounds > 0
      || ["failed", "cancelled", "canceled", "interrupted", "awaiting_resume"].includes(run.status);
    const live = !turn && !TERMINAL.has(run.status);
    if (!executed && !live) return null;
    const type = run.operation === "import" ? "场景导入" : turn?.kind === "inspection" ? "场景检查" : run.operation === "generate" ? "场景生成" : turn?.kind === "edit" || versions.length || used.edit_rounds > 0 ? "场景修改" : "场景任务";
    const status = run.cancel_requested && !TERMINAL.has(run.status) ? "已请求停止" : D?.resourceWaiting(run,null,resourceView(run)) ? D.resourceWaitingText(resourceView(run)) : ({ completed: "已完成", failed: "失败", cancelled: "已停止", canceled: "已停止", interrupted: "已中断", awaiting_resume: "已暂停", awaiting_reference: "等待采用参考", queued: "等待执行" })[run.status] || "进行中";
    const head = project?.current_scene;
    const latestVersion = versions.length ? Math.max(...versions) : null;
    const isHead = head?.run_id === run.id && head.revision_id && project.workspace?.working_version === latestVersion
      && commits.some(row => row.working_version === latestVersion && (row.global_revision_id || row.revision_id) === head.revision_id);
    const saved = versions.length ? `正式保存 ${versions.length} 次 · Working Version ${latestVersion}（${isHead ? "当前工作版" : "历史提交"}）` : `${!executed && live ? "尚无场景执行结果 · " : ""}本 Run 正式保存未确认`;
    const judgement = run.judgement || run.agent?.judgement;
    const application = judgementApplication(judgement, project?.workspace, run.judge_status);
    let judge = run.judge_status === "pending" || run.status === "judging" ? "Judge · 待验收" : run.judge_status === "failed" ? "Judge · 验收失败" : "Judge · 尚无适用结论";
    if (judgement) judge = judgement.kind === "candidate" || judgement.working_version == null ? "Judge · 候选结论，当前 Working 质量未确认"
      : application.state !== "applied" || application.superseded ? "Judge · 历史或适用性未确认，需核对"
        : judgement.status === "failed" ? "Judge · 验收失败"
          : judgement.verdict === "pass" ? "Judge · 当前验收范围通过；逐项质量见计划" : "Judge · 当前验收未通过";
    return { type, status, saved, judge };
  }
  function conversationMessage(role, text) {
    const article = document.createElement("article"); article.className = `message conversation-${role}`;
    const heading = document.createElement("div"); heading.className = "message-heading"; heading.textContent = role === "user" ? "你" : "Assistant";
    const content = document.createElement("div"); content.className = "message-content"; content.textContent = typeof text === "string" ? text : "";
    article.append(heading, content); return article;
  }
  function directorDetailParent() {
    if (D?.parent(current)) return current;
    return runs.find(run=>run.id === directorDetailParentId && run.project_id === project?.id && D?.parent(run)) || null;
  }
  async function readDirectorParent(id) {
    const context = scope(), sequence=(directorReadSequence.get(id)||0)+1,startedAt=runMutation;directorReadSequence.set(id,sequence);
    const run = await api(`/api/runs/${encodeURIComponent(id)}`);
    if (!isCurrent(context) || run?.id !== id || run.project_id !== context.id || !D?.parent(run) || !run.director?.task) return null;
    const known=runs.find(r=>r.id===id);
    if(directorReadSequence.get(id)!==sequence || known?.updated_at && run.updated_at && conversationTime(run.updated_at)<conversationTime(known.updated_at)
      || (runWrites.get(id)||0)>startedAt && known?.status!==run.status && (TERMINAL.has(known?.status)||["awaiting_resume","awaiting_approval"].includes(known?.status)))return known?.director?.task?known:null;
    mergeConversationRuns([run]); return run;
  }
  async function readDirectorChild(parent, requested = null) {
    if (!D || D.readonly(parent)) return null;
    const context = scope(), binding = D.bindings(parent).find(row=>row.child_id === (requested || parent.active_child_id || D.bindings(parent).at(-1)?.child_id));
    if (!binding) return null;
    try {
      const child = await api(`/api/runs/${encodeURIComponent(binding.child_id)}`);
      if (!isCurrent(context)) return null;
      if (!D.childFor(parent,[child],binding.child_id)) { directorChildReadErrors.set(binding.child_id,"子执行关联身份尚未确认"); return null; }
      mergeConversationRuns([child]); directorChildReadErrors.delete(child.id); return child;
    } catch (e) { if (isCurrent(context)) directorChildReadErrors.set(binding.child_id,e.message); return null; }
  }
  async function refreshDirectorChildren(context) {
    if (!D) return;
    const originals = runs.filter(run=>D.parent(run));
    const wanted=new Set(),add=run=>{if(run && wanted.size<8)wanted.add(run.id);};
    add(originals.find(r=>r.id===$("execution-dialog").dataset.ownerRunId && $("execution-dialog").open));
    originals.filter(r=>!TERMINAL.has(r.status) && !["awaiting_resume","awaiting_approval"].includes(r.status)).slice(0,4).forEach(add);
    originals.filter(r=>directorStatusPending.has(r.id)).slice(0,2).forEach(add);
    originals.slice(0,2).filter(r=>!r.director || !TERMINAL.has(r.status) || awaitingConversationTurn(r) || directorStatusPending.has(r.id)).forEach(add);
    const viewport=$("message-scroll").getBoundingClientRect?.();
    originals.filter(r=>{const row=conversationRows.get(r.id),rect=row?.node.getBoundingClientRect?.();return (!r.director?.timeline || !TERMINAL.has(r.status) || awaitingConversationTurn(r)) && row?.process?.open && (!viewport||!rect||rect.bottom>viewport.top && rect.top<viewport.bottom);}).slice(0,3).forEach(add);
    const reads = originals.filter(r=>wanted.has(r.id));
    const parentResults = await Promise.allSettled(reads.map(run=>readDirectorParent(run.id)));
    if (!isCurrent(context)) return;
    if (parentResults.some(result=>result.status === "rejected" || !result.value)) throw new Error("持续任务详情读取失败；不猜政策或委派关系");
    reads.forEach(r=>directorStatusPending.delete(r.id));
    const parents = runs.filter(run=>wanted.has(run.id) && D.parent(run) && !D.readonly(run));
    const results = await Promise.allSettled(parents.map(parent=>readDirectorChild(parent)));
    if (!isCurrent(context)) return;
    for (const result of results) if (result.status === "rejected") error("可信子执行读取失败；保留原投影，操作前需核对");
  }
  async function openDirectorChild(parentId,childId, approve = false) {
    if(approve)return openRequestControls(childId,"asset",document.activeElement,parentId);
    const context = scope(), parent = await readDirectorParent(parentId);
    if (!parent || !isCurrent(context) || D.readonly(parent) || approve && parent.active_child_id !== childId) return;
    const child = await readDirectorChild(parent,childId);
    if (!child || !isCurrent(context)) return;
    if(approve)await openRequestControls(child.id,"asset");
    else await selectConversationRun(child.id,context);
    if (isCurrent(context) && current?.id === child.id && approve) { await refreshAssetRequest(child.id); $("run-asset-request").scrollIntoView({block:"nearest"}); }
    return isCurrent(context) && current?.id === child.id ? child : null;
  }
  async function openDirectorArtifact(parentId,childId,kind) {
    const context = scope(), child = await openDirectorChild(parentId,childId);
    if (child && isCurrent(context) && current?.id === child.id) openArtifact(kind,true);
  }
  function compactDirectorExecution(run,turn) {
    const context = scope(), child = D.childFor(run,runs);
    const card = (child && compactExecution(child,null,true)) || document.createElement("section");
    card.className = child ? "compact-execution" : "director-conversation-controls"; card.dataset.runId = run.id;
    if (child) {
      const actions = card.children[card.children.length - 1];
      for (const node of [...(actions?.children || [])]) if (["停止后续步骤","查看/批准资产请求"].includes(node.textContent)) actions.removeChild(node);
      if (run.active_child_id === child.id && assetRequested(child)) actions?.append(requestControlsButton("查看/批准子资产请求",child.id,"asset",context,run.id));
      if (directorChildReadErrors.has(child.id)) for (const node of actions?.children || []) if (node.tagName === "button" && !node.textContent.includes("查看执行详情")) node.disabled = true;
    }
    const actions = document.createElement("div"); actions.className = "compact-execution-actions";
    if(hasDirectorPending(run,["policy"]) || director?.hasConfirmedResult(run,["policy"]))
      actions.append(requestControlsButton("核对历史操作",run.id,"policy",context));
    const completedReadonly=D.readonly(run) && run.status==="completed" && typeof turn?.assistant==="string" && turn.assistant.trim();
    if(D.resultUncertain(run) || run.status === "awaiting_resume" && run.director?.recovery?.state !== "child_creation_rejected" || run.turn_record_pending || run.director?.recovery?.turn_pending || !completedReadonly && ["resume","recover"].some(action=>D.recoveryAllowed(run,action)) || hasDirectorPending(run,["resume","recover"]))
      actions.append(requestControlsButton(D.naturalContinue(run) ? "继续此请求" : "核对本次请求",run.id,"recovery",context));
    if(D.bindings(run).length>1)D.bindings(run).forEach((binding,index)=>actions.append(button(`查看第 ${index+1} 项施工产物`,()=>isCurrent(context)&&openDirectorArtifact(run.id,binding.child_id,"render"),"text-button")));
    appendArtifactActions(actions,run,child,context);
    card.append(actions); return card;
  }
  function hasDirectorPending(run,actions){try{return actions.some(action=>Boolean(window.sessionStorage?.getItem(`sceneloop.director-action.v1:${run.project_id}:${run.id}:${action}`)));}catch{return false;}}
  function appendArtifactActions(actions,run,child,context){
    const source=child||run;
    const open=kind=>{if(!isCurrent(context))return;return child?openDirectorArtifact(run.id,child.id,kind):(async()=>{const selected=await selectConversationRun(run.id,context);if(selected&&isCurrent(context))openArtifact(kind,true);})();};
    if((source.artifacts||[]).some(a=>a.kind==="render"&&safeArtifactUrl(a.url)))actions.append(button("查看场景渲染",()=>open("render"),"text-button"));
    if((source.artifacts||[]).some(a=>a.kind==="concept"&&safeArtifactUrl(a.url)))actions.append(button("查看概念图",()=>open("concept"),"text-button"));
    if(source.agent?.revisions?.length)actions.append(button("查看候选版本",()=>open("candidates"),"text-button"));
    for(const artifact of source.artifacts||[]){if(!["blend","plan","inspection"].includes(artifact.kind))continue;const url=safeArtifactUrl(artifact.url);if(!url)continue;const link=document.createElement("a");link.href=url;link.textContent=artifactLabel(artifact);if(artifact.kind==="blend")link.download=artifact.name||"scene.blend";else{link.target="_blank";link.rel="noopener";}actions.append(link);}
  }
  async function selectConversationRun(id, context) {
    if (!isCurrent(context)) return null;
    const selection = runSelectionEpoch + 1, run = await loadRun(id);
    return isCurrent(context) && selection === runSelectionEpoch && current?.id === id && run?.id === id && run.project_id === context.id ? { run, selection } : null;
  }
  async function openExecutionDetails() { return false; }
  function requestControlsButton(label,id,purpose,context,parentId=null){
    const key=JSON.stringify([context.id,id,purpose]);
    if(!requestDisclosureButtons.has(key)){
      const entry=button(label,event=>isCurrent(context)&&openRequestControls(id,purpose,event.target,requestDisclosureButtons.get(key).parentId),"text-button request-disclosure");
      entry.id=`request-disclosure-${projectEpoch}-${requestDisclosureButtons.size}`;
      entry.setAttribute("aria-controls","request-controls");
      requestDisclosureButtons.set(key,{entry,id,purpose,parentId});
    }
    const {entry}=requestDisclosureButtons.get(key);
    requestDisclosureButtons.get(key).parentId=parentId;
    entry.textContent=label;
    entry.setAttribute("aria-expanded",String(requestControlIntent?.id===id&&requestControlIntent.purpose===purpose));
    return entry;
  }
  function syncRequestDisclosures(){
    for(const {entry,id,purpose} of requestDisclosureButtons.values())entry.setAttribute("aria-expanded",String(requestControlIntent?.id===id&&requestControlIntent.purpose===purpose));
  }
  async function openRequestControls(id,purpose,trigger=document.activeElement,parentId=null){
    if(!["asset","round","policy","recovery"].includes(purpose))return;
    if(requestControlIntent?.id===id&&requestControlIntent.purpose===purpose){closeExecutionDetails();return;}
    closeExecutionDetails();
    const context=scope(),session=++executionDialogSession;
    const known=runs.find(run=>run.id===id),owner=D?.parentFor(known,runs)||known,row=conversationRows.get(owner?.id);
    if(!row)return;
    const intent={id,purpose};requestControlIntent=intent;syncRequestDisclosures();
    const panel=$("request-controls"),body=$("request-controls-body"),loading=$("request-controls-loading");
    if(!row.process&&!row.node.contains(row.auditHost))row.node.append(row.auditHost);
    row.auditHost.append(panel);panel.hidden=false;panel.open=true;
    panel.dataset.ownerRunId=owner.id;panel.dataset.selectedRunId=id;panel.dataset.purpose=purpose;
    panel.setAttribute("aria-labelledby",trigger?.id||"");panel.setAttribute("aria-busy","true");loading.hidden=false;
    let ready=false;
    try{
      if(parentId){
        const parent=await readDirectorParent(parentId);
        if(!isCurrent(context)||session!==executionDialogSession||!parent||D.readonly(parent)||parent.active_child_id!==id)return;
        const child=await readDirectorChild(parent,id);
        if(!child||!isCurrent(context)||session!==executionDialogSession)return;
      }
      // Original-action lookup and confirmed-result display remain reachable when Run detail fails.
      const receiptActions=purpose==="policy"?["policy"]:["resume","recover"];
      const originalOnly=!parentId && D?.parent(known) && known.project_id===context.id && ["policy","recovery"].includes(purpose)
        && (hasDirectorPending(known,receiptActions) || director?.hasConfirmedResult(known,receiptActions));
      let selected;
      if(originalOnly){runSelectionEpoch++;mergeRun(known);selected={run:known};}
      else selected=await selectConversationRun(id,context);
      if(!selected||session!==executionDialogSession||!isCurrent(context))return;
      directorDetailParentId=D?.parent(selected.run)?selected.run.id:D?.parentFor(selected.run,runs)?.id||null;
      if(!originalOnly&&D?.parent(selected.run)&&!D.readonly(selected.run)){
        const child=await readDirectorChild(selected.run);
        if(!isCurrent(context)||session!==executionDialogSession||current?.id!==id)return;
        if(child)renderRun();
      }
      if(purpose==="round"){
        await refreshLocalRound(id);
        if(!isCurrent(context)||session!==executionDialogSession||current?.id!==id)return;
      }
      const ids=purpose==="asset"?["run-asset-request"]:purpose==="round"?["run-local-round"]:purpose==="policy"?["director-continuous","director-human-action","director-refresh","director-action-status"]:["run-director-pause","director-human-action","director-refresh","director-action-status"];
      for(const controlId of ids)body.append($(controlId));
      $("execution-dialog").dataset.ownerRunId=owner.id;$("execution-dialog").dataset.selectedRunId=id;$("execution-dialog").open=true;
      panel.setAttribute("aria-busy","false");loading.hidden=true;ready=true;
      director?.renderDetails(directorDetailParent());
      if(purpose==="asset")await refreshAssetRequest(id);
    }finally{if(!ready&&requestControlIntent===intent)closeExecutionDetails();}
  }
  function closeExecutionDetails(preview = null) {
    executionDialogSession++;
    requestControlIntent=null;syncRequestDisclosures();
    resetAssetForm();
    const body=$("request-controls-body");for(const child of [...body.children])$("request-control-parking").append(child);
    $("request-controls").hidden=true;$("request-controls").open=false;$("execution-parking").append($("request-controls"));
    $("execution-dialog").open=false;$("execution-dialog").hidden=true;
    $("execution-parking").append($("execution-dialog"));
  }
  function finishExecutionClose() {
    resetAssetForm();
    const intent=executionCloseIntents.shift();
    if(intent&&(!isCurrent(intent.context)||intent.session!==executionDialogSession||$("execution-dialog").open))return;
    if(intent?.preview){(document.querySelector(`[data-preview-kind="${intent.preview}"]`)||$("close-preview")).focus({preventScroll:true});restoreConversationPosition(intent.position);return;}
    const card=conversationRows.get(executionReturnRunId)?.node;
    const replacement=card&&Array.from(card.querySelectorAll("button")).find(node=>node.textContent===executionReturnFocus?.textContent);
    (executionReturnFocus?.isConnected&&!$("execution-dialog").contains(executionReturnFocus)?executionReturnFocus:replacement||$("open-execution-history")).focus({preventScroll:true});
  }
  async function conversationRunAction(id, action, context) {
    if (!isCurrent(context) || busy || roundActionBusy) return;
    const selected = await selectConversationRun(id, context); if (!selected) return;
    if (D?.parent(current) && action !== "cancel") return;
    if (D?.parent(current) && action === "cancel" && D.projection(current,"activity")?.safe_to_cancel === false) return;
    const owningParent = D?.parentFor(current,runs);
    if (owningParent && ["resume","end"].includes(action)) {
      const freshParent = await readDirectorParent(owningParent.id);
      if (!freshParent || freshParent.active_child_id !== id || !D.childFor(freshParent,[current],id)) return;
    }
    if (assetControlsRun(current) && ["resume", "end", "cancel"].includes(action)) return;
    if (["resume", "end"].includes(action) && !await refreshLocalRound(id)) {
      if (isCurrent(context) && current?.id === id) error(localRoundError || "局部轮状态尚未确认，请重新读取。");
      return;
    }
    if (!isCurrent(context) || selected.selection !== runSelectionEpoch || current?.id !== id) return;
    if (action === "adopt" && current.status !== "awaiting_reference") return;
    if (action === "cancel" && (TERMINAL.has(current.status) || current.status === "awaiting_resume" && !D?.parent(current))) return;
    if (["resume", "end"].includes(action)) {
      await localRoundAction(action);
      if (isCurrent(context) && current?.id === id && localRoundError) error(localRoundError);
    } else await runAction(action);
  }
  function compactExecution(run, turn, delegated = false) {
    if (D?.parent(run)) return compactDirectorExecution(run,turn);
    const facts = executionFacts(run, turn); if (!facts) return null;
    const card = document.createElement("section"); card.className = "compact-execution"; card.dataset.runId = run.id;
    const terminal = TERMINAL.has(run.status); card.dataset.state = terminal ? "terminal" : "live";
    const lines = terminal ? [`${facts.type} · ${facts.status}`, [facts.saved.includes("Working Version") ? facts.saved : "", facts.judge.includes("尚无适用结论") ? "" : facts.judge].filter(Boolean).join(" · ") || "未确认正式保存或质量验收"] : [`${facts.type} · ${facts.status}`, facts.saved, facts.judge];
    for (const text of lines) { const line = document.createElement("p"); line.textContent = text; card.append(line); }
    const assetEntry = assetViews.get(run.id);
    if (assetRequested(run)) {
      let doc = assetEntry?.doc;
      if (!doc && run.asset_request) { try { doc = assetDocument(run.asset_request, run.id, scope()); } catch { /* Compact hint still opens the authoritative read. */ } }
      const line = document.createElement("p"); line.textContent = doc ? `${assetState(doc)}${doc.job?`\n${assetConsumption(doc)}`:""}` : run.asset_request ? "资产请求详情待核对" : assetEntry?.confirmed ? "单资产能力已开放；尚无资产请求" : "单资产请求状态待只读核对"; card.append(line);
    }
    const actions = document.createElement("div"); actions.className = "compact-execution-actions"; const context = scope();
    const add = (label, action) => { const control = button(label, () => conversationRunAction(run.id, action, context), "text-button"); control.disabled = busy || roundActionBusy; actions.append(control); };
    if (run.status === "awaiting_reference") add("采用参考并建场", "adopt");
    else if (!assetControlsRun(run) && !TERMINAL.has(run.status) && run.status !== "awaiting_resume") add("停止后续步骤", "cancel");
    const round = current?.id === run.id && localRoundRunId === run.id ? localRound : null;
    if(localMode(run) && !assetControlsRun(run) && ["awaiting_resume","interrupted","cancelled"].includes(run.status))actions.append(requestControlsButton("查看草稿处理",run.id,"round",context));
    if (!assetControlsRun(run) && round?.resume_ready && ["awaiting_resume", "interrupted", "cancelled"].includes(run.status)) add("续做草稿", "resume");
    if (!assetControlsRun(run) && round?.end_ready) add("结束草稿", "end");
    if (assetRequested(run)) actions.append(requestControlsButton("查看/批准资产请求",run.id,"asset",context));
    if(!delegated)appendArtifactActions(actions,run,null,context);
    card.append(actions); return card;
  }
  function reconcileChildren(parent, nodes) {
    for (const [index, node] of nodes.entries()) if (parent.children[index] !== node) parent.insertBefore(node, parent.children[index] || null);
    for (const child of [...parent.children]) if (!nodes.includes(child)) parent.removeChild(child);
  }
  async function loadEarlierTimeline(run,row,context) {
    if(!isCurrent(context)||row.timeline.loading||!row.timeline.hasMore||typeof row.timeline.cursor!=="string")return;
    const state=row.timeline,position=conversationPosition(),cursor=state.cursor;state.loading=true;
    try{
      const doc=await api(`/api/runs/${encodeURIComponent(run.id)}/director/timeline?before=${encodeURIComponent(cursor)}&limit=40`);
      if(!isCurrent(context)||conversationRows.get(run.id)!==row||state.cursor!==cursor)return;
      if(!C.merge(state,doc,run.id,true))throw new Error("Timeline identity mismatch");
      state.readError=false;
    }catch{if(isCurrent(context))state.readError=true;}
    finally{state.loading=false;if(isCurrent(context)){row.signature=null;renderConversation({...position,atBottom:false,prepend:true});}}
  }
  async function readProcess(id,row,context){
    const position=conversationPosition();
    try{const fresh=await readDirectorParent(id);if(!isCurrent(context)||conversationRows.get(id)!==row)return;if(!fresh)throw new Error("Identity unavailable");row.signature=null;renderConversation({...position,atBottom:false});}
    catch{if(isCurrent(context)&&conversationRows.get(id)===row){row.timeline.readError=true;row.signature=null;renderConversation({...position,atBottom:false});}}
  }
  function renderConversation(position = conversationPosition()) {
    const turns = new Map(projectTurns().map(turn => [turnRunId(turn), turn]));
    for (const [id,turn] of turns) if (typeof turn.assistant === "string" && turn.assistant.trim()) observedConversationRuns.delete(id);
    const delegatedChildren = new Set(runs.filter(run=>D?.parentFor(run,runs)).map(run=>run.id));
    const projection = runs.filter(run=>!delegatedChildren.has(run.id)).map(run => ({ key: run.id, run, turn: turns.get(run.id) }));
    for (const [key, turn] of turns) if (!delegatedChildren.has(key) && !runs.some(run => run.id === key)) projection.push({ key, turn });
    const pending = submissionFor(project?.id);
    if (pending && ["unknown", "submitting"].includes(pending.state) && !runs.some(run => run.id === pending.run_id || run.idempotency_key === pending.key)) projection.push({ key: `pending:${pending.key}`, pending });
    projection.sort((a,b) => (conversationTime(a.run?.created_at) || conversationTime(a.turn?.created_at) || conversationTime(a.pending?.created_at)) - (conversationTime(b.run?.created_at) || conversationTime(b.turn?.created_at) || conversationTime(b.pending?.created_at)) || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0));
    let changed = false; const nodes = [];
    for (const { key, turn, run, pending } of projection) {
      const user = turn ? turn.user : run ? run.prompt || run.request?.prompt || "导入场景副本" : pending.body.prompt;
      const hasFinal = typeof turn?.assistant === "string" && Boolean(turn.assistant.trim());
      const finalFailure=hasFinal && run?.error ? D?.failureMessage(run) || "本次任务未能完成，请保留原任务记录以便核查。" : "";
      const finalUncertain=hasFinal && !finalFailure && D?.parent(run) && (run.error?.outcome_unknown || ["started","unknown"].includes(run.director?.recovery?.decision_state));
      const live = finalUncertain ? `工作进展：${D.state(run,D.childFor(run,runs))}\n${D.pauseMessage(run)}` : !hasFinal && (run ? conversationRunState(run) : !turn ? "提交结果待确认；尚未确认已接受，原请求身份已保留。不会自动重发。" : "尚无正式回复；原请求已保留。");
      const round = run?.id === localRoundRunId ? localRound : null;
      const signature = JSON.stringify([user, turn?.assistant, turn?.kind, run && executionFacts(run, turn), live, finalFailure, run?.error?.code,run?.error?.outcome_unknown, round?.version, round?.resume_ready, round?.end_ready, run?.resource_request,run && resourceView(run),run?.asset_request, assetViews.get(run?.id)?.doc, assetViews.get(run?.id)?.readError, D?.parent(run) && [run.director?.timeline,run.director?.task?.pause,D.childFor(run,runs),run.director?.recovery,run.status,...["activity","request_scope","authorization","queue"].map(k=>D.projection(run,k))], busy, roundActionBusy, assetActionBusy]);
      let row = conversationRows.get(key);
      if (!row) {
        const node = document.createElement("div"); node.className = "conversation-turn"; node.dataset.conversationKey = key;
        const auditHost=document.createElement("div");auditHost.className="conversation-audit-host";
        row = { node, user: conversationMessage("user", user),auditHost }; conversationRows.set(key, row);
      }
      row.node.dataset.turnId = turn?.id || run?.id || key;
      const resourceCard=run && resourceRequests?.mount(run);
      const warningCard=run && localWarnings?.mount(run);
      if(resourceCard){row.resourceHost ||= document.createElement("div");row.resourceHost.className="conversation-resource-host";reconcileChildren(row.resourceHost,[resourceCard]);}
      if (signature !== row.signature || warningCard !== row.warningCard) {
        row.warningCard=warningCard;
        changed = true; row.signature = signature;
        const focused = row.node.contains(document.activeElement) ? document.activeElement?.textContent : null;
        row.user.children[1].textContent = typeof user === "string" ? user : "";
        const children = [row.user];
        const processChildren = [], destination = D?.parent(run) ? processChildren : children;
        if (live) {
          row.live ||= document.createElement("p"); row.live.className = "assistant-live-state"; row.live.setAttribute("role", "status"); row.live.textContent = live; destination.push(row.live);
        }
        if (run) {
          const card = compactExecution(run, turn);
          if (card) destination.push(card);
        }
        if(resourceCard)destination.push(row.resourceHost);
        if (D?.parent(run) && C) {
          const stateKey=`${run.project_id}:${run.id}`;
          if(!timelines.has(stateKey))timelines.set(stateKey,C.createState());
          row.timeline=timelines.get(stateKey);C.merge(row.timeline,run.director?.timeline,run.id);C.finalize(row.timeline,hasFinal);
          if(!row.process){
            row.process=document.createElement("details");row.process.className="conversation-process";row.process.dataset.parentRunId=run.id;
            row.summary=document.createElement("summary");row.timelineBody=document.createElement("div");row.timelineBody.className="conversation-timeline";
            row.timelineNodes=new Map();
            row.process.addEventListener("toggle",()=>{row.timeline.open=row.process.open;});
            row.summary.addEventListener("click",event=>{event.preventDefault();row.timeline.open=!row.process.open;row.process.open=row.timeline.open;if(row.timeline.open)readProcess(run.id,row,scope());});
            row.process.append(row.summary,row.timelineBody,row.auditHost);
          }
          row.process.open=row.timeline.open;
          const entries=C.entries(row.timeline);
          const pause=D.pauseMessage(run);
          row.summary.textContent=entries.length?`执行过程 · ${entries.length} 项`:"处理进展";
          const items=[];
          if(row.timeline.hasMore){const context=scope();const earlier=button("加载更早过程",()=>loadEarlierTimeline(run,row,context),"text-button");earlier.disabled=Boolean(row.timeline.loading);items.push(earlier);}
          for(const entry of entries){
            let nodes=row.timelineNodes.get(entry.id);
            if(!nodes){nodes={node:document.createElement("div"),label:document.createElement("small"),content:document.createElement("p")};nodes.node.append(nodes.label,nodes.content);row.timelineNodes.set(entry.id,nodes);}
            const item=nodes.node;item.className=`timeline-entry timeline-${entry.kind}`;item.dataset.timelineId=entry.id;
            const role={director:"Director",builder:"Builder",judge:"Judge",system:"系统"}[entry.role],status={running:"进行中",completed:"已返回",failed:"失败",blocked:"需处理",unknown:"结果待核对"}[entry.status];
            nodes.label.textContent=entry.kind==="commentary"?`${role} · 中途回复`:`${role} · ${status}`;
            nodes.content.textContent=entry.text;items.push(item);
          }
          if(!row.timeline.received){const fallback=document.createElement("p");fallback.className="field-help";fallback.textContent="仅显示当前状态；此记录尚无可读取的完整过程。";items.push(fallback);}
          if(row.timeline.readError){const failure=document.createElement("p");failure.textContent="更早过程读取失败，已加载的记录保留。";items.push(failure);}
          items.push(...processChildren);reconcileChildren(row.timelineBody,items);children.push(row.process);
        }
        else if(run && row.auditHost.children.length) children.push(row.auditHost);
        if(warningCard)children.push(warningCard);
        if (pending) {
          const actions = document.createElement("div"); actions.className = "pending-submission-actions";
          const context = scope();
          for (const [label, handler] of [["核对原请求", () => isCurrent(context) && recoverSubmission(context)], ["用原请求重试", () => isCurrent(context) && startRun(undefined, true)]]) { const control = button(label, handler, "text-button"); control.disabled = busy; actions.append(control); }
          children.push(actions);
        }
        if (D?.readonly(run)) {
          const asOf = run.director_decision?.as_of || run.readonly_accepted, stamp = document.createElement("p"); stamp.className="field-help";
          stamp.textContent = `读取时点 ${asOf?.at || "待正式回复"} · Working Version ${asOf?.working?.working_version ?? run.director_decision?.working_version ?? "未确认"}；未委派施工，观察图片以实际记录及来源版本为准。`; children.push(stamp);
        }
        if(finalFailure){row.failure ||= document.createElement("p");row.failure.className="system-run-failure field-help";row.failure.setAttribute("role","status");row.failure.textContent=`系统状态：${finalFailure}`;children.push(row.failure);}
        if (hasFinal) { row.assistant ||= conversationMessage("assistant", turn.assistant); row.assistant.children[0].textContent = D?.parent(run) || turn.kind === "director" ? "Director" : "Assistant"; row.assistant.children[1].textContent = turn.assistant; children.push(row.assistant); }
        reconcileChildren(row.node, children);
        if (focused) [...row.node.querySelectorAll("button")].find(node => node.textContent === focused)?.focus({ preventScroll: true });
      }
      nodes.push(row.node);
    }
    reconcileChildren($("message-list"), nodes);
    for (const key of conversationRows.keys()) if (!projection.some(row => row.key === key)) conversationRows.delete(key);
    $("open-execution-history").disabled = !runs.length;
    $("load-conversation-history").hidden = !historyPage?.has_more; $("load-conversation-history").disabled = historyLoading;
    $("conversation-history-state").textContent = project ? `已加载 ${runs.length} 条执行记录${historyPage?.has_more ? "，还有更早记录" : historyPage ? "" : "；历史范围待服务确认"}。缺少正式回复的记录只显示原请求和真实状态。` : "";
    if (changed && !position.atBottom && !initialConversationPending && !position.prepend) $("conversation-new-messages").hidden = false;
    if (position.atBottom) $("conversation-new-messages").hidden = true;
    renderProjectStart(); $("message-count").textContent = project ? `${project.name} · ${projection.length} 条项目记录` : "选择项目后开始对话";
    conversationSignature = JSON.stringify(projection.map(row => row.key)); restoreConversationPosition(position);
  }
  function renderSceneAction() {
    const revision = currentRevision(), artifacts = visibleArtifacts();
    const complete = current && TERMINAL.has(current.status) && artifacts.some((item) => item.kind === "blend") && artifacts.some((item) => item.kind === "render");
    const sameProject = Boolean(project && current && current.project_id === project.id);
    const alreadyCurrent = Boolean(sameProject && project?.current_scene?.run_id === current?.id && (project.current_scene.revision_id || null) === (revision?.id || null));
    $("use-current-scene").hidden = !project || !complete;
    $("use-current-scene").disabled = busy || alreadyCurrent || runs.some(inProgress);
    $("use-current-scene").textContent = !sameProject ? "将此版本复制到本项目" : alreadyCurrent ? "此版本是当前场景" : "将查看的版本设为当前场景";
    $("current-scene-help").textContent = !project || !current ? "" : !sameProject ? "这是其他项目或旧任务的结果。复制副本到本项目；项目尚无 Working 时会初始化，已有 Working 时需检查后明确采用。" : alreadyCurrent ? "这是当前 Working Scene。" : "正在预览历史或候选版本；这不会改变 Working Scene。使用恢复或设为当前场景后才会替换。";
  }
  function openImportScene() {
    if ($("open-import-scene").disabled) return;
    $("project-actions").open = false; importDialogSession++; dialogError("import-scene-error");
    $("import-scene-title").textContent = project.current_scene ? "导入其他场景供预览 / 采用" : "导入初始 Blender 场景";
    $("import-scene-description").textContent = project.current_scene
      ? "创建独立副本供预览，不会合并对象或替换当前 Working Scene。导入完成后，需明确采用才能切换当前场景；原文件不覆盖，本步骤不调用模型。"
      : "在项目内创建副本并生成预览，成功后成为初始 Working Scene。原文件不覆盖，本步骤不调用模型。";
    $("import-scene-dialog").showModal(); $("import-scene-path").focus();
  }
  async function importScene(source) {
    if (!project || busy) return;
    if (!modeAvailable($("execution-mode").value) || runs.some(inProgress)) { dialogError("import-scene-error", "先准备可用的 Blender，并等待当前任务结束。"); return; }
    const context = scope(), body = { ...source, expected_revision: project.revision, execution: { mode: $("execution-mode").value } };
    const signature = JSON.stringify(body), pending = pendingImports.get(project.id);
    const request = pending?.signature === signature ? pending : { signature, key: crypto.randomUUID() };
    pendingImports.set(project.id, request); body.idempotency_key = request.key;
    busy = true; updateReadiness(); dialogError("import-scene-error");
    try {
      const result = await api(`/api/projects/${encodeURIComponent(context.id)}/import-scene`, body);
      pendingImports.delete(context.id);
      if (!isCurrent(context)) return;
      if ($("import-scene-dialog").open) $("import-scene-dialog").close();
      await loadRun((result.run || result).id, true); pollingFailures = 0;
    } catch (e) { if (isCurrent(context)) { error(e.message); dialogError("import-scene-error", e.message); } }
    finally { busy = false; updateReadiness(); schedulePoll(); }
  }
  async function adoptCurrentScene() {
    if (!project || !current || busy || $("use-current-scene").disabled) return;
    const revision = currentRevision();
    if (current.project_id !== project.id) { await importScene({ run_id: current.id, revision_id: revision?.id || null }); return; }
    const context = scope(); busy = true; updateReadiness(); error();
    try {
      const result = await api(`/api/projects/${encodeURIComponent(project.id)}/current-scene`, { run_id: current.id, revision_id: revision?.id || null, expected_revision: project.revision });
      if (!isCurrent(context)) return;
      project = result.project; projects = projects.map((item) => item.id === project.id ? project : item);
      $("continue-reference").checked = false; renderProject(); renderSceneAction();
    } catch (e) { if (isCurrent(context)) { error(e.message); await refreshRuns().catch(() => {}); } }
    finally { busy = false; updateReadiness(); }
  }
  async function showLegacyRuns() {
    if ($("open-legacy-runs").disabled) return;
    const context = scope(), session = ++legacyDialogSession;
    $("project-actions").open = false; $("legacy-runs-dialog").showModal(); $("legacy-runs-list").replaceChildren(); dialogError("legacy-runs-error");
    try {
      const result = await api("/api/runs"); if (!isCurrent(context) || session !== legacyDialogSession || !$("legacy-runs-dialog").open) return;
      const available = (result.runs || []).filter((run) => run.project_id !== project?.id && TERMINAL.has(run.status));
      for (const run of available) $("legacy-runs-list").append(button(run.prompt || `${run.operation === "import" ? "导入场景" : "历史结果"} · ${run.id.slice(0, 8)}`, () => { $("legacy-runs-dialog").close(); loadRun(run.id, true, true); }));
      if (!available.length) $("legacy-runs-list").textContent = "没有可复制的其他项目或旧任务结果。";
    } catch (e) { if (isCurrent(context) && session === legacyDialogSession && $("legacy-runs-dialog").open) dialogError("legacy-runs-error", e.message); }
  }
  function renderRunPicker() {
    $("run-picker").replaceChildren();
    for (const run of runs) {
      const option = document.createElement("option"); option.value = run.id;
      const date = new Date(run.created_at); const dateLabel = Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
      option.textContent = `${dateLabel} · ${D?.parent(run) ? D.state(run,D.childFor(run,runs)) : STATUS[run.status] || "执行记录"}`;
      $("run-picker").append(option);
    }
    if (current && !runs.some((item) => item.id === current.id)) { const option = document.createElement("option"); option.value = current.id; option.textContent = "其他项目 / 旧任务结果 · 待复制到本项目"; $("run-picker").append(option); }
    if (current) $("run-picker").value = current.id;
  }
  function artifactLabel(artifact) {
    return ({ concept: "查看概念图", render: "查看场景渲染", blend: "下载 Blender 场景", plan: "查看场景计划", inspection: "查看场景检查" })[artifact.kind] || artifact.name || "查看产物";
  }
  function revisionList(run = current) { return Array.isArray(run?.agent?.revisions) ? run.agent.revisions : []; }
  function currentRevision(run = current) {
    const revisions = revisionList(run), agent = run?.agent;
    return revisions.find((item) => run === current && item.id === viewedRevision)
      || revisions.find((item) => item.id === agent?.selected_revision)
      || revisions.find((item) => item.id === agent?.pending_revision)
      || revisions.at(-1) || null;
  }
  function revisionLabel(revision) {
    if (localMode(current)) {
      const commit = (current?.commits || []).find((row) => row.revision_id === revision.id && row.local_round_id);
      const kind = revision.round === 0 ? "起点" : `第 ${revision.round} 次草稿`;
      return `${kind} · ${commit ? `正式保存至 Working Version ${commit.working_version}` : "未正式保存"} · ${revision.id}`;
    }
    return `${revision.round === 0 ? "初版" : `第 ${revision.round} 轮`} · ${REVISION_STATUS[revision.status] || "待检查"} · ${revision.id}`;
  }
  function visibleArtifacts(run = current) {
    const artifacts = run?.artifacts || [], revision = currentRevision(run);
    if (!run?.agent) return artifacts;
    return artifacts.filter((item) => item.kind === "concept" || (revision && (item.revision_id === revision.id || revision.artifact_ids?.includes(item.id))));
  }
  const budgetNumber = (value) => Number.isFinite(value) ? value : "—";
  function localBudgetLines() {
    const progress = (localRoundRunId === current?.id ? localRoundBudget : null) || current?.budget_progress;
    if (progress?.limits?.schema_version === 2) {
      const limits = progress.limits, used = progress.used || {}, remaining = progress.remaining || {};
      const amount = (name, maximum) => `${budgetNumber(used[name])} / ${budgetNumber(maximum)}（剩余 ${budgetNumber(remaining[name])}）`;
      return {
        counts: `冻结预算 ${limits.policy_id || "v2"} · 正式保存 ${budgetNumber(progress.saved_edit_rounds)} / ${budgetNumber(limits.saved_round_limit)}（剩余 ${budgetNumber(remaining.saved_edit_rounds)}）· 距上次Judge已保存 ${budgetNumber(progress.rounds_since_judge)} / ${budgetNumber(current.judge?.trigger_rounds)} · 当前轮草稿 ${budgetNumber(progress.round_edit_attempts)} / ${budgetNumber(limits.max_edit_attempts_per_round)}（剩余 ${budgetNumber(remaining.round_edit_attempts)}）· Run草稿 ${budgetNumber(progress.run_edit_attempts)} / ${budgetNumber(limits.max_edit_attempts)}（剩余 ${budgetNumber(remaining.run_edit_attempts)}）· 同操作额外重试 ${budgetNumber(progress.edit_retry_count)} 次。`,
        resources: `累计资源：Builder模型请求 ${amount("brain_calls", limits.max_brain_calls)}；Builder工具调用 ${amount("tool_calls", limits.max_tool_calls)}；观察 ${amount("observations", limits.max_observations)}；Judge模型请求 ${amount("judge_calls", limits.max_judge_calls)}；累计活动耗时（仅统计）${budgetNumber(used.active_seconds)} 秒。阶段 ${{ editing: "修改中", closing: "收尾中", stopped: "已停止" }[progress.phase] || progress.phase || "未知"}${progress.limiting_resource ? ` · 限制原因 ${({ brain_calls: "Builder模型请求", tool_calls: "Builder工具调用", observations: "观察", judge_calls: "Judge模型请求", round_edit_attempts: "本轮草稿", run_edit_attempts: "Run草稿", saved_edit_rounds: "正式保存", run_status: "Run已结束" })[progress.limiting_resource] || progress.limiting_resource}` : ""}。`,
      };
    }
    const limits = current?.local_round_limits;
    if (limits && limits.schema_version !== 2) {
      const saved = (current.commits || []).filter((row) => row.local_round_id).length;
      return {
        counts: `旧Run按原冻结快照 · 正式保存 ${saved} / ${budgetNumber(current.iteration?.max_rounds ?? current.request?.iteration?.max_rounds)} · 旧策略草稿尝试 ${budgetNumber(current.agent?.used?.edit_rounds)} / ${budgetNumber(limits.max_edit_attempts)}；本轮草稿与Run总草稿未分层登记，重试以原Run记录为准。`,
        resources: `旧快照资源：Builder模型请求 ${budgetNumber(current.usage?.brain_calls)} / ${budgetNumber(limits.max_brain_calls)}；Builder工具调用 ${budgetNumber(current.agent?.used?.tool_calls)} / ${budgetNumber(limits.max_tool_calls)}；观察 ${budgetNumber(current.agent?.used?.observations)} / ${budgetNumber(limits.max_observations)}；Judge模型请求 ${budgetNumber(current.usage?.judge_calls)} / ${budgetNumber(current.judge?.max_calls)}；累计活动耗时（仅统计）${budgetNumber(progress?.used?.active_seconds ?? current.local_round_active_seconds)} 秒。未提供的新预算剩余量不推算。`,
      };
    }
    return { counts: `此Run${limits?.schema_version === 2 ? "的v2冻结预算进度" : "的预算快照"}暂不可用；正式保存、轮内草稿及Run累计均待后端确认。`,
      resources: "资源用量与剩余暂不可用；不以当前新Run预设填充历史记录。" };
  }
  function agentSummary() {
    const { limits = {}, used = {} } = current?.agent || {};
    if (localMode(current)) {
      const budget = localBudgetLines(); return `${budget.counts} ${budget.resources}`;
    }
    const judge = current?.judge?.enabled
      ? ` · 裁判 ${current?.usage?.judge_calls || 0} / ${current.judge.max_calls ?? "—"} 次 · 裁判只读工具 ${current?.usage?.judge_tool_calls || 0} 次`
      : "";
    return `修改 ${used.edit_rounds || 0} / ${limits.max_rounds ?? "—"} 轮 · 大脑 ${current?.usage?.brain_calls || 0} / ${limits.max_brain_calls ?? "—"} 次 · 截图 ${used.observations || 0} / ${limits.max_observations ?? "—"} 张 · 工具 ${used.tool_calls || 0} / ${limits.max_tool_calls ?? "—"} 次${judge}`;
  }
  function renderJudgeSummary() {
    const panel = $("run-judge-summary"), judgement = current?.judgement;
    const enabled = Boolean(current?.judge?.enabled);
    panel.hidden = !enabled && !judgement;
    if (panel.hidden) return;
    const workspace = project?.workspace, target = judgement?.target;
    const version = judgement?.working_version;
    const application = judgementApplication(judgement, workspace, current?.judge_status);
    const isCandidate = judgement?.kind === "candidate" || (judgement && version == null);
    const revisionLabel = judgement?.selection?.revision_id ?? "—";
    // A candidate acceptance is judged against its own in-run identity: it is
    // "adopted" only when the real project pointer (not a synthetic workspace
    // field) selects the same run/revision, and it becomes history once the
    // Stage/scope changes afterwards.
    const candidateAdopted = isCandidate && Boolean(project?.current_scene) && Boolean(judgement?.selection)
      && project.current_scene.run_id === judgement.selection.run_id
      && project.current_scene.revision_id === judgement.selection.revision_id;
    const candidateStale = isCandidate && (judgement.stale === true || judgement.invalid === true
      || current?.judge_status === "stale");
    const candidateSuperseded = isCandidate && !candidateStale && candidateAdopted && application.stageChanged;
    const candidateState = candidateStale ? "unapplied"
      : candidateSuperseded ? "superseded"
        : candidateAdopted ? "adopted" : "candidate";
    const titleState = isCandidate
      ? (candidateState === "unapplied" ? "历史记录（未应用）"
        : candidateState === "superseded" ? "历史记录（已采用，需重新验收）"
          : judgement?.status === "failed" ? "验收失败" : (judgement?.verdict || "uncertain"))
      : application.state === "unapplied" ? "历史记录（未应用）"
      : application.superseded ? "历史记录（已应用，需重新验收）"
        : application.state === "unknown" ? "历史记录（归属未标记）"
          : judgement?.verdict || "uncertain";
    const candidateSuffix = candidateState === "adopted" ? "（已成为工作版）"
      : candidateState === "superseded" ? "（已采用，范围已变）" : "（未发布）";
    $("run-judge-title").textContent = judgement
      ? (isCandidate ? `独立裁判 · ${titleState} · 候选 ${revisionLabel}${candidateSuffix}`
        : `独立裁判 · ${titleState} · Working Version ${version ?? "—"}`)
      : "独立裁判 · 本轮未触发";
    let detail;
    if (isCandidate && candidateState === "unapplied") {
      detail = judgement?.invalid === true
        ? `登记入口拒绝（未应用于当前验收）：${judgement.invalid_reason || judgement.message || "无效判决"}。`
        : judgement?.status === "failed"
          ? `旧请求失败（未应用于当前验收）：${judgement.message || "未取得有效结论"}。`
          : `该候选结论对应的受评版本、Stage 或批次已变化（${judgement.stale_reason || "不适用当前候选"}），未应用到当前验收。`;
    } else if (isCandidate && candidateState === "superseded") {
      const scopeText = [target?.stage ? `Stage ${target.stage}` : null,
        target?.batch?.id ? "批次" : null].filter(Boolean).join("、");
      detail = `该候选当时已验收并采用为工作版${scopeText ? `（${scopeText}）` : ""}；当前 Stage/范围已变化，需重新验收。`;
    } else if (!isCandidate && application.state === "unapplied") {
      detail = judgement?.invalid === true
        ? `登记入口拒绝（未应用于当前验收）：${judgement.invalid_reason || judgement.message || "无效判决"}。`
        : judgement?.status === "failed"
          ? `旧请求失败（未应用于当前验收）：${judgement.message || "未取得有效结论"}；原受评 Working Version ${version ?? "未知"}。`
          : `该结论对应的受评版本、Stage 或批次已变化（${judgement.stale_reason || "不适用当前版本"}），未应用到当前验收；原受评 Working Version ${version ?? "未知"}。`;
    } else if (!isCandidate && application.superseded) {
      const scopeText = [target?.stage ? `Stage ${target.stage}` : null,
        target?.batch?.id ? "批次" : null].filter(Boolean).join("、");
      detail = judgement?.status === "failed"
        ? `该失败记录当时针对 Working Version ${version ?? "未知"}${scopeText ? `（${scopeText}）` : ""}；当前版本/Stage/批次已变化，需重新验收。`
        : `该结论当时已应用到 Working Version ${version ?? "未知"}${scopeText ? `（${scopeText}）` : ""}；当前版本/Stage/批次已变化，需重新验收。`;
    } else if (!isCandidate && application.state === "unknown") {
      detail = judgement?.status === "failed"
        ? `该失败记录的适用状态未标记（${judgement.message || "执行失败"}）；按历史记录显示，请重新验收。`
        : `该记录的适用状态未标记；按历史记录显示，请重新验收。`;
    } else if (judgement?.status === "failed") {
      detail = `验收失败：${judgement.message || "未取得有效结构化结论"}。${isCandidate ? "候选保留，未发布。" : "Working Scene 保持不变。"}`;
    } else if (judgement?.verdict === "rollback") {
      detail = isCandidate
        ? "裁判建议回滚；候选保留未发布，未自动恢复，也未指向其他工作版的恢复动作。"
        : "裁判建议回滚；当前现场已保留，未自动恢复。可在当前工作版中手动恢复批次起点。";
    } else if (judgement) {
      detail = [(isCandidate && candidateState === "adopted" ? "该候选已被采用为当前工作版；原候选验收身份保留。"
          : isCandidate ? "该结论针对本轮候选，不代表 Working Scene 通过。"
          : judgement.stage_passed ? "本 Stage 通过。" : "本 Stage 尚未通过。"),
        ...(judgement.next_focus || []).map((item) => `下一步：${item}`),
        ...(judgement.unknowns || []).map((item) => `无法判断：${item}`)].join(" ");
    } else {
      detail = "Builder 与 Judge 使用独立请求和调用预算；达到触发条件时验收。";
    }
    if (judgement?.format_error?.summary) detail += ` 格式记录：${judgement.format_error.summary}`;
    if (judgement?.format_corrections) detail += ` 已使用 ${judgement.format_corrections} 次独立裁判格式纠正。`;
    if (judgement?.normalization?.count) detail += ` 已安全规范化 ${judgement.normalization.count} 处字段差异。`;
    const triggers = (current?.judge_history || []).map((row) => row.trigger).filter(Boolean);
    if (judgement && triggers.length) detail += ` 轮中触发：${triggers.join("、")}。`;
    const preview = currentRevision();
    if (judgement && preview && (judgement.selection?.revision_id
      ? judgement.selection.run_id !== current?.id || judgement.selection.revision_id !== preview.id
      : project?.current_scene?.run_id !== current?.id || project?.current_scene?.revision_id !== preview.id))
      detail += ` 当前预览 ${preview.id} 与此判决的受评版本不同；此判决不代表当前预览通过。`;
    $("run-judge-detail").textContent = detail;
    const list = $("run-judge-issues"); list.replaceChildren();
    for (const issue of judgement?.issues || []) {
      const item = document.createElement("li");
      item.textContent = `[${issue.severity || "unknown"}] ${issue.category || "issue"}${issue.object_id ? ` · ${issue.object_id}` : ""}：${issue.description || "待处理"}`;
      list.append(item);
    }
  }
  function stopSummary() {
    const reason = current?.agent?.stop_reason;
    const label = localMode(current) && reason === "round_limit" ? "达到草稿编辑尝试上限" : STOP[reason] || reason;
    return reason ? `停止原因：${label}。停止不代表视觉质量已通过验收。` : "正式比较使用固定主、辅视角，额外诊断截图单独标明。";
  }
  function renderAgent() {
    const agent = current?.agent;
    $("run-agent-summary").hidden = !agent && !localMode(current); $("run-agent-stop").hidden = !agent?.stop_reason;
    $("run-agent-preview").hidden = !agent;
    if (!agent) { $("run-agent-summary").textContent = localMode(current) ? agentSummary() : ""; revisionSignature = null; return; }
    $("run-agent-summary").textContent = agentSummary(); $("run-preview-progress").textContent = agentSummary();
    $("run-agent-stop").textContent = stopSummary(); $("run-preview-stop").textContent = stopSummary();
    const revisions = revisionList(), selected = currentRevision(), signature = JSON.stringify([current.id, revisions, selected?.id]);
    if (signature === revisionSignature) return;
    revisionSignature = signature;
    const picker = $("revision-picker"), list = $("run-revisions"); picker.replaceChildren(); list.replaceChildren();
    picker.disabled = !revisions.length;
    for (const revision of revisions) {
      const option = document.createElement("option"); option.value = revision.id; option.textContent = revisionLabel(revision); picker.append(option);
      const item = document.createElement("li"); item.dataset.state = revision.status;
      const choose = button(revisionLabel(revision), () => { viewedRevision = revision.id; openArtifact("candidates"); }, "revision-button");
      choose.setAttribute("aria-pressed", String(revision.id === selected?.id)); item.append(choose);
      if (revision.assessment) {
        const assessment = document.createElement("small"), { verdict, reason, preservation } = revision.assessment;
        assessment.textContent = `模型评价：${VERDICT[verdict] || verdict || "待检查"}${preservation ? ` · ${PRESERVATION[preservation] || "保留要求待确认"}` : ""}${reason ? ` · ${reason}` : ""}`; item.append(assessment);
      }
      list.append(item);
    }
    if (selected) picker.value = selected.id;
  }
  function renderLocalRound() {
    const enabled = localMode(current), panel = $("run-local-round");
    panel.hidden = !enabled;
    if (!enabled) return;
    const record = localRoundRunId === current.id ? localRound : null;
    const status = { active: "进行中", paused: "已暂停", committed: "已正式保存", ended: "已明确结束（未保存）" };
    $("run-local-round-state").textContent = record
      ? `轮 ${record.id.slice(0, 8)} · v${record.version} · ${status[record.status] || record.status}`
      : "正在读取此 Run 的局部轮；模型可能尚未建立草稿轮。";
    $("run-local-round-goal").textContent = record ? `本轮目标：${record.local_goal}` : "本轮目标尚未登记。";
    $("run-local-round-criteria").textContent = record
      ? `整轮自检标准：${record.local_criteria} · 目标身份 ${record.goal_id} · Plan ${record.plan_id}`
      : "目标与标准只从实际局部轮记录读取。";
    const budget = localBudgetLines();
    $("run-local-round-counts").textContent = budget.counts;
    $("run-local-round-resources").textContent = budget.resources;
    const list = $("run-local-round-steps"); list.replaceChildren();
    for (const step of record?.steps || []) {
      const item = document.createElement("li");
      const verdict = step.assessment ? ` · Builder 自检 ${VERDICT[step.assessment.verdict] || step.assessment.verdict} / ${PRESERVATION[step.assessment.preservation] || step.assessment.preservation}` : "";
      item.textContent = `草稿 ${step.revision_id} · ${STEP_STATUS[step.status] || step.status} · ${step.operations?.length ?? 0} 项编辑操作${step.retry_index ? ` · 第 ${step.retry_index} 次重试` : ""}${step.reason ? ` · ${step.reason}` : ""}${verdict}；仅正式提交会推进 Working。`;
      list.append(item);
    }
    if (!list.children.length) list.textContent = "尚无登记的草稿编辑。";
    const observations = $("run-local-round-observations"); observations.replaceChildren();
    for (const observation of current.agent?.observations || []) {
      const item = document.createElement("li");
      item.textContent = `观察 ${observation.id} · 来源版本 ${observation.revision_id} · ${observation.view_id || "诊断视角"} · ${observation.comparison ? "固定比较" : "诊断观察"}；仅对应原版本。`;
      observations.append(item);
    }
    if (!observations.children.length) observations.textContent = "尚无登记的实际观察。";
    const viewed = currentRevision(), commit = viewed && (current.commits || []).find((row) => row.revision_id === viewed.id && row.local_round_id);
    $("run-local-round-view").textContent = viewed
      ? `当前预览 ${viewed.id}：${commit ? `已正式保存于 Working Version ${commit.working_version}` : "草稿/起点，未因该预览正式保存"}；${project?.current_scene?.run_id === current.id && project?.current_scene?.revision_id === viewed.id ? "也是当前项目 Working。" : "不是当前项目 Working。"}`
      : "尚无可预览版本。";
    const block = record?.resume_block_reason ? ROUND_BLOCK[record.resume_block_reason] || record.resume_block_reason : "";
    $("run-local-round-next").textContent = !record ? "等待模型建立局部轮；刷新只读取，不自动执行。"
      : record.status === "paused" ? (record.resume_ready
        ? `恢复点 ${record.checkpoint?.revision_id || "—"} 已确认；可显式续做同一 Run 或结束未保存草稿。`
        : `当前不可续做：${block || "没有安全恢复点"}；${record.end_ready ? "可明确结束未保存草稿。" : "请核对运行状态。"}`)
      : record.status === "active" ? (inProgress(current) ? "当前 Run 正在执行；请等待或核对状态。" : "此轮停止但未正式保存；核对写入结果后可明确结束。")
      : record.status === "committed" ? "本轮已正式保存；检查对应 Working 与独立 Judge 结论，再决定下一步。" : "本轮草稿已结束，Working 保持已正式保存的版本。";
    if (assetControlsRun(current)) $("run-local-round-next").textContent = "本轮正在等待资产状态确认；请使用上方资产请求入口核对、批准或结束。普通草稿续做暂不可用。";
    const resumePending = pendingAction(current.id, "resume"), endPending = pendingAction(current.id, "end");
    const readFailed = localRoundError.startsWith("局部轮读取失败");
    $("resume-local-round").disabled = assetControlsRun(current) || roundActionBusy || busy || !(resumePending || (!readFailed && record?.resume_ready && ["awaiting_resume", "interrupted", "cancelled"].includes(current.status)));
    $("end-local-round").disabled = assetControlsRun(current) || roundActionBusy || busy || !(endPending || (!readFailed && record?.end_ready));
    $("resume-local-round").textContent = resumePending ? "核对原续做请求（同一 ID）" : "显式续做同一 Run";
    $("end-local-round").textContent = endPending ? "核对原结束请求（同一 ID）" : "结束未保存草稿";
    $("run-local-round-error").textContent = localRoundError;
    $("run-local-round-error").hidden = !localRoundError;
  }
  async function refreshLocalRound(runId = current?.id, clearError = false) {
    if (!runId || !localMode(current) || current.id !== runId) return;
    const token = ++localRoundLoad, context = scope();
    let readSucceeded = false;
    localRoundBudget = null;
    try {
      const data = await api(`/api/runs/${encodeURIComponent(runId)}/local-round`);
      if (token !== localRoundLoad || !isCurrent(context) || current?.id !== runId) return;
      localRound = data.local_round; localRoundBudget = data.local_round?.budget_progress || data.budget_progress || null; localRoundRunId = runId;
      readSucceeded = true;
      if (clearError || localRoundError.startsWith("局部轮读取失败")) localRoundError = "";
    } catch (e) {
      if (token !== localRoundLoad || !isCurrent(context) || current?.id !== runId) return;
      localRoundError = `局部轮读取失败：${e.message}`;
    }
    renderLocalRound(); renderAgent(); renderConversation();
    return readSucceeded;
  }
  async function localRoundAction(action) {
    if (!current || assetControlsRun(current) || !localMode(current) || roundActionBusy || busy) return;
    const runId = current.id, record = localRoundRunId === runId ? localRound : null;
    const owner = D?.parentFor(current,runs), actionContext = scope(), selection = runSelectionEpoch;
    if (owner) {
      const actual = await readDirectorParent(owner.id);
      if (!actual || actual.active_child_id !== runId || !isCurrent(actionContext) || selection !== runSelectionEpoch || current?.id !== runId || !D.childFor(actual,[current],runId)) return;
    }
    let pending = pendingAction(runId, action);
    if (!pending) {
      if (!record || (action === "resume"
        ? !(record.resume_ready && ["awaiting_resume", "interrupted", "cancelled"].includes(current.status))
        : !record.end_ready)) return;
      const body = action === "resume"
        ? { resume_id: crypto.randomUUID(), expected_round_version: record.version }
        : { end_id: crypto.randomUUID(), round_id: record.id, expected_round_version: record.version,
            reason: "用户在工作台明确结束未保存的局部草稿" };
      pending = { run_id: runId, action, body }; rememberAction(pending);
    }
    const context = scope(); roundActionBusy = true; localRoundError = ""; renderLocalRound();
    try {
      const suffix = action === "resume" ? "resume" : "local-round/end";
      await api(`/api/runs/${encodeURIComponent(runId)}/${suffix}`, pending.body);
      forgetAction(runId, action); localRoundError = "";
      if (isCurrent(context) && current?.id === runId) await loadRun(runId);
    } catch (e) {
      if (isCurrent(context) && current?.id === runId) {
        const rejected = e.status === 409 && ["round_not_resumable", "round_working_changed",
          "round_goal_changed", "round_criteria_changed", "round_evidence_invalid",
          "round_resource_exhausted", "round_version_conflict", "round_end_conflict"].includes(e.code);
        if (rejected) forgetAction(runId, action);
        localRoundError = rejected
          ? `${ROUND_BLOCK[e.code] || e.message} 此请求已被明确拒绝；请重新读取状态后再决定下一步。`
          : `${ROUND_BLOCK[e.code] || e.message} 原请求身份已保留；先核对状态，需重试时仍使用同一 ID。`;
      }
    } finally {
      roundActionBusy = false;
      if (isCurrent(context) && current?.id === runId) { await refreshLocalRound(runId); renderLocalRound(); }
    }
  }
  function renderRun() {
    const chatPosition = conversationPosition();
    renderSaveReceipts();
    renderRefinement();
    $("execution-panel").hidden = !current;
    $("message-scroll").classList.toggle("has-execution", Boolean(current));
    if (!current) { renderAgent(); renderLocalRound(); renderJudgeSummary(); renderGeneratedPreview(); updateReadiness(); restoreConversationPosition(chatPosition); return; }
    $("run-status").textContent = current.cancel_requested && !TERMINAL.has(current.status) ? "已请求停止，等待当前步骤结束" : STATUS[current.status === "running" ? current.stage : current.status] || STATUS[current.status] || "正在执行";
    if (assetWaiting(current)) $("run-status").textContent = assetState(assetViews.get(current.id)?.doc);
    if (current.agent && current.status === "completed") $("run-status").textContent = "迭代已停止，待检查";
    if (current.operation === "import" && current.status === "completed") $("run-status").textContent = "场景副本已导入，待检查";
    if (D?.parent(current)) $("run-status").textContent = D.state(current,D.childFor(current,runs));
    $("execution-title").textContent = D?.parent(current) ? "Director 对话" : "场景执行";
    $("run-status").dataset.state = current.status;
    $("run-prompt").textContent = current.prompt || current.request?.prompt || "场景任务";
    $("cancel-run").hidden = Boolean(D?.parent(current)) || TERMINAL.has(current.status) || current.status === "awaiting_resume";
    $("cancel-run").textContent = D?.parent(current) && current.status === "awaiting_resume" ? "结束此暂停任务" : "停止后续步骤";
    $("run-adoption").hidden = current.status !== "awaiting_reference";
    $("run-review-note").hidden = current.status !== "completed" && current.status !== "awaiting_resume";
    if (D?.parent(current)) $("run-review-note").hidden = true;
    const plannedIteration = current.request?.iteration || current.iteration;
    const rounds = current.agent?.limits?.max_rounds || plannedIteration?.max_rounds || 3;
    const iterative = Boolean(current.agent || plannedIteration?.enabled);
    $("run-adoption-description").textContent = iterative
      ? `检查生成的概念图。采用后，大脑将以此图为固定参考建场并自动观察、修改，最多 ${rounds} 轮；大脑最多 ${current.agent?.limits?.max_brain_calls || 3 * rounds + 3} 次调用。`
      : "检查生成的概念图。采用后，会把此图作为本轮建模参考，调用大脑规划场景并由 Blender 建立白模。";
    $("adopt-reference").textContent = iterative ? "采用此图，建场并迭代" : "采用此图并建立白模";
    $("run-review-note").textContent = iterative
      ? "迭代已停止。请对照参考检查保留版本的关键物体、构图与通路；达到轮数或调用上限不代表视觉质量通过。"
      : "白模已生成，待你检查关键物体形状、构图与通路。技术执行完成不代表视觉质量已通过验收。";
    if (current.operation === "edit" && current.evaluation?.mode !== "reference") $("run-review-note").textContent = "修改已停止。请比较修改前后效果，并确认已有构图、风格与功能得到保留；停止不代表质量通过。";
    if (localMode(current)) $("run-review-note").textContent = current.status === "awaiting_resume"
      ? "草稿暂停不等于正式保存。请核对恢复点，显式续做或结束；Working 仍以正式回执为准。"
      : "草稿自检与正式保存分开；检查实际 Working 与独立 Judge，达到资源上限不代表质量通过。";
    if (current.operation === "import") $("run-review-note").textContent = "导入副本已就绪。本步骤没有调用模型；项目原先为空时它已自动成为初始 Working Scene，否则保持为历史预览。";
    $("run-error").textContent = current.error ? D?.failureMessage(current) || "本次任务未能完成，请保留原任务记录以便核查。" : "";
    $("run-error").hidden = !current.error;
    const artifactButtons = $("run-artifact-buttons"), previewKinds = new Set(); artifactButtons.replaceChildren();
    const artifactChild = D?.parent(current) && D.childFor(current,runs), artifactSource = artifactChild || current;
    const artifactParentId = current.id;
    const preview = kind => artifactChild ? openDirectorArtifact(artifactParentId,artifactChild.id,kind) : openArtifact(kind);
    for (const artifact of visibleArtifacts(artifactSource)) {
      const url = safeArtifactUrl(artifact.url); if (!url) continue;
      if (["concept", "render"].includes(artifact.kind)) {
        if (!previewKinds.has(artifact.kind)) artifactButtons.append(button(artifactLabel(artifact), () => preview(artifact.kind)));
        previewKinds.add(artifact.kind);
      }
      else {
        const link = document.createElement("a"); link.href = url; link.className = "button button-quiet"; link.textContent = artifactLabel(artifact);
        if (artifact.kind === "blend") link.download = artifact.name || "scene.blend";
        else { link.target = "_blank"; link.rel = "noopener"; }
        artifactButtons.append(link);
      }
    }
    if (revisionList(artifactSource).length) artifactButtons.append(button("查看候选版本", () => preview("candidates")));
    const events = $("run-events"), wasAtBottom = events.scrollHeight - events.scrollTop - events.clientHeight < 40;
    events.replaceChildren();
    for (const event of current.events || []) {
      const item = document.createElement("li"), time = document.createElement("time"), text = document.createElement("span");
      const date = new Date(event.time || event.created_at || "");
      time.textContent = Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString("zh-CN");
      text.textContent = event.message || event.stage || "状态更新"; item.append(time, text); events.append(item);
    }
    if (wasAtBottom) events.scrollTop = events.scrollHeight;
    renderRunPicker(); renderAgent(); renderLocalRound(); renderJudgeSummary(); renderGeneratedPreview(); renderConversation(chatPosition); updateReadiness();
    restoreConversationPosition(chatPosition);
  }
  function renderSaveReceipts() {
    const rows = [...(current?.scene_commit_receipts || []).filter(row => ["builder", "initial"].includes(row.kind)), ...(current?.commits || []), ...(current?.initial_working_commit ? [current.initial_working_commit] : [])];
    const list = $("run-save-receipt-list"); list.replaceChildren(); const seen = new Set();
    const identity = value => typeof value === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(value) ? value : "未标记";
    for (const row of rows) {
      if (!Number.isInteger(row.working_version) || row.working_version < 1) continue;
      const revision = identity(row.global_revision_id || row.revision_id), operation = identity(row.operation_id), key = JSON.stringify([row.working_version, revision, operation]);
      if (seen.has(key)) continue; seen.add(key);
      const line = document.createElement("li"); line.textContent = `Working Version ${row.working_version} · 版本 ${revision} · 操作 ${operation}${Number.isInteger(row.project_revision) ? ` · 项目修订 ${row.project_revision}` : ""}`; list.append(line);
    }
    $("run-save-receipts").hidden = !seen.size;
  }
  function openArtifact(kind, focusPreview = false) {
    const leavingDetails = $("execution-dialog").open;
    if (leavingDetails) closeExecutionDetails(kind === "candidates" ? "candidates" : "render");
    previewKind = kind === "candidates" ? "candidates" : "render"; showGenerated = true;
    if (window.SceneLoopWorkbench?.openPreview) window.SceneLoopWorkbench.openPreview(previewKind);
    else { $("show-preview").click(); document.querySelector(`[data-preview-kind="${previewKind}"]`)?.click(); }
    renderAgent(); renderLocalRound(); renderJudgeSummary(); renderGeneratedPreview(); renderSceneAction();
    renderRefinement();
    if (leavingDetails || focusPreview) (document.querySelector(`[data-preview-kind="${previewKind}"]`) || $("close-preview")).focus({ preventScroll: true });
  }
  function renderGeneratedPreview() {
    const state = window.SceneLoopWorkbench?.previewState?.();
    if (state) { previewKind = state.kind; showGenerated = state.open; }
    const visible = showGenerated && Boolean(current);
    $("run-preview").hidden = !visible; document.querySelector(".preview-viewer").hidden = visible;
    if (!visible) {
      previewSignature = null;
      if (!current) { $("run-preview-content").replaceChildren(); $("run-downloads").replaceChildren(); $("run-preview-title").textContent = ""; }
      return;
    }
    const revision = currentRevision();
    const artifacts = visibleArtifacts();
    const images = artifacts.filter((a) => safeArtifactUrl(a.url) && ["concept", "render"].includes(a.kind)).map(item => ({ ...item, imageTitle: observationImageTitle(current, item) }));
    const signature = JSON.stringify([current.id, previewKind, revision, current.artifacts, current.reference, current.available_reference, images.map(item => item.imageTitle)]);
    if (signature === previewSignature) return;
    previewSignature = signature;
    const content = $("run-preview-content"); content.replaceChildren();
    $("run-preview-title").textContent = `${revision ? revisionLabel(revision) : "本轮场景产物"} · ${current.id.slice(0, 8)}`;
    if (images.length) {
      for (const [index, artifact] of images.entries()) {
        if (artifact.kind === "concept") {
          const label = document.createElement("h3"); label.className = "run-preview-image-label"; label.textContent = "概念图"; content.append(label);
        }
        const link = button("", () => showImages(images, index, link), "preview-image-button"), img = document.createElement("img");
        img.src = safeArtifactUrl(artifact.url); img.alt = artifact.imageTitle;
        link.setAttribute("aria-label", `放大查看 ${img.alt}`);
        img.addEventListener("error", () => { img.hidden = true; link.textContent = "图片暂无法显示，点击查看或下载"; });
        link.append(img); content.append(link);
        const caption = document.createElement("small"); caption.className = "run-preview-caption";
        caption.textContent = artifact.kind === "concept"
          ? current.status === "awaiting_reference" ? "生成的概念图 · 等待采用为建模参考"
            : current.reference ? "已采用的参考图"
              : current.available_reference ? "项目保留的参考图 · 本次不用于评价" : "生成的概念图 · 未采用为建模参考"
          : artifact.imageTitle;
        content.append(caption);
      }
    } else {
      const empty = document.createElement("p"); empty.className = "empty-state";
      empty.textContent = previewKind === "candidates" ? "当前轮次的文件列在下方。" : "此轮尚无该类型产物。"; content.append(empty);
    }
    const downloads = $("run-downloads"); downloads.replaceChildren();
    for (const artifact of artifacts) {
      const url = safeArtifactUrl(artifact.url); if (!url) continue;
      const link = document.createElement("a"); link.href = url; link.textContent = artifact.name || artifactLabel(artifact);
      link.download = artifact.name || (artifact.kind === "blend" ? "scene.blend" : artifact.kind === "concept" || artifact.kind === "render" ? "scene-image.png" : "artifact");
      link.textContent = `下载 ${artifact.name || artifactLabel(artifact)}`;
      downloads.append(link);
    }
  }
  function showImages(items, index = 0, trigger = document.activeElement) {
    imageItems = items.filter((item) => safeArtifactUrl(item.url));
    if (!imageItems.length) return;
    imageIndex = Math.max(0, Math.min(index, imageItems.length - 1)); imageReturnFocus = trigger;
    renderImageDialog(); if (!$("image-dialog").open) $("image-dialog").showModal(); $("close-image-dialog").focus();
  }
  function renderImageDialog() {
    const item = imageItems[imageIndex]; if (!item) return;
    $("image-dialog-title").textContent = item.imageTitle || observationImageTitle(null, item);
    $("image-dialog-image").src = item.url; $("image-dialog-image").alt = $("image-dialog-title").textContent;
    $("image-dialog-image").hidden = false; $("image-dialog-error").hidden = true;
    $("image-dialog-counter").textContent = `${imageIndex + 1} / ${imageItems.length}`;
    $("image-dialog-previous").disabled = imageIndex === 0; $("image-dialog-next").disabled = imageIndex >= imageItems.length - 1;
    $("image-dialog-download").href = item.url; $("image-dialog-download").download = item.name || "scene-image.png";
  }
  function mergeRun(run, reveal = false, foreign = false) {
    const known=runs.find(row=>row.id===run.id);
    if(!foreign && known?.updated_at && run.updated_at && conversationTime(run.updated_at)<conversationTime(known.updated_at))run=known;
    if($("execution-dialog").open && $("execution-dialog").dataset.selectedRunId && $("execution-dialog").dataset.selectedRunId!==run.id)closeExecutionDetails();
    const old = current, previousArtifacts = old?.id === run.id ? new Set((old.artifacts || []).map((a) => a.id || a.url)) : new Set();
    if (old?.id !== run.id) { viewedRevision = null; revisionSignature = null; }
    observeConversationRun(run);
    current = run;
    localRoundBudget = null;
    if (old?.id !== run.id) { localRound = null; localRoundBudget = null; localRoundRunId = null; localRoundError = ""; localRoundLoad++; }
    if (!foreign) { mergeConversationRuns([run]); renderConversation(); }
    renderRun();
    if (!foreign) finishInitialConversation();
    if (localMode(run)) refreshLocalRound(run.id);
    if (assetRequested(run)) refreshAssetRequest(run.id);
    refreshRefinement();
    const freshImages = (run.artifacts || []).filter((a) => ["concept", "render"].includes(a.kind) && !previousArtifacts.has(a.id || a.url));
    if (reveal && freshImages.length) openArtifact(freshImages.at(-1).kind);
    schedulePoll();
  }
  async function refreshRuns() {
    if (!project) { runs = []; current = null; renderConversation(); renderRun(); return; }
    const context = scope(), selection = runSelectionEpoch, token = ++refinementLoad, read = ++runReadSequence, startedAt = runMutation;
    let listLoaded = false;
    refinementSyncing = true; refinementError = ""; renderRefinement();
    try {
      const [data, projectData] = await Promise.all([api(`/api/runs?project_id=${encodeURIComponent(context.id)}&limit=100`), api(`/api/projects/${encodeURIComponent(context.id)}`), recoverSubmission(context)]);
      if (!isCurrent(context) || read !== runReadSequence) return;
      const headChanged = JSON.stringify(project.current_scene) !== JSON.stringify(projectData.project.current_scene);
      if (typeof projectData.project?.revision !== "number" || typeof project.revision !== "number" || projectData.project.revision >= project.revision) project = projectData.project;
      if (headChanged) { $("continue-reference").checked = false; }
      const chatPosition = conversationPosition();
      mergeConversationRuns(Array.isArray(data.runs) ? data.runs : [], startedAt);
      listLoaded = true;
      await refreshDirectorChildren(context);
      if (!isCurrent(context) || read !== runReadSequence) return;
      director?.readSucceeded();
      if (!historyPage) historyPage = data.page || null;
      renderProject(); renderConversation(chatPosition); finishInitialConversation();
      await resourceRequests?.refresh();
      await localWarnings?.refresh();
      if (!isCurrent(context) || read !== runReadSequence) return;
      if (selection !== runSelectionEpoch) { renderRunPicker(); refreshRefinement(); return; }
      const selected = runs.find((run) => run.id === current?.id) || runs.find(inProgress) || runs[0];
      if (current && current.project_id !== project.id && !runs.some((run) => run.id === current.id)) { renderRun(); refreshRefinement(); return; }
      if (selected) {
        const detailAt = runMutation, run = await api(`/api/runs/${encodeURIComponent(selected.id)}`);
        if (isCurrent(context) && read === runReadSequence && selection === runSelectionEpoch && (runWrites.get(selected.id) || 0) <= detailAt && run?.id === selected.id && run.project_id === context.id) {
          const child = D?.childFor(run,runs);
          if (child && !$("execution-dialog").open) { directorDetailParentId=run.id; mergeRun(child); }
          else mergeRun(run);
        }
      } else { current = null; renderRun(); finishInitialConversation(); await refreshRefinement(); }
    } catch (e) {
      if (isCurrent(context)) director?.readFailed();
      if (listLoaded && isCurrent(context) && read === runReadSequence) { renderConversation(); finishInitialConversation(); renderRunPicker(); updateReadiness(); }
      if (isCurrent(context) && read === runReadSequence && token === refinementLoad) {
        refinementSyncing = false;
        refinementError = `同步失败：${e.message}；${refinementProjectId === context.id && refinement ? "显示上次读取，请重新读取。" : "尚无已读取内容，请重试。"}`;
        renderRefinement();
      }
      if (isCurrent(context) && read === runReadSequence) throw e;
    }
  }
  function schedulePoll() {
    clearTimeout(timer); timer = null;
    if ((!runs.some(inProgress) && !runs.some(awaitingConversationTurn) && !runs.some(assetWaiting) && !directorStatusPending.size && !resourceRequests?.waiting() && !localWarnings?.waiting()) || pollingFailures >= 3 || document.hidden) return;
    timer = setTimeout(poll, 1500);
  }
  async function poll() {
    if (polling) return; polling = true; const context = scope();
    for (const run of runs) if (awaitingConversationTurn(run) && observedConversationRuns.has(run.id)) observedConversationRuns.get(run.id).reads++;
    try {
      await refreshRuns(); if (!isCurrent(context)) return;
      pollingFailures = 0;
    } catch (e) { if (isCurrent(context)) { pollingFailures++; if (pollingFailures >= 3) error(`${e.message} 自动刷新已暂停，请重新检测环境恢复。`); } }
    finally { polling = false; renderConversation(); schedulePoll(); updateReadiness(); }
  }
  async function startRun(requestedOperation, retryOriginal = false) {
    const explicitNew = requestedOperation === "generate", operation = explicitNew ? "generate" : primaryOperation();
    const original = submissionFor(project?.id);
    const reasons = retryOriginal && original && D?.parent(original.body)
      ? !health?.ok || !project || projectLoading ? ["等待本地服务与项目载入，原请求身份保留"] : [] : blockers(false, operation, explicitNew);
    if (busy || reasons.length) { error(reasons.join("；")); updateReadiness(); return; }
    const state = snapshot(), context = scope();
    let body = { ...state, project_id: project.id, operation, iteration: { enabled: state.iteration?.enabled ?? false, max_rounds: state.iteration?.max_rounds ?? 3 }, judge: { ...state.judge, enabled: true, trigger_rounds: state.judge?.trigger_rounds ?? 3 }, execution: { mode: $("execution-mode").value }, cost_policy: "call_limits_only" };
    delete body.judge.max_calls;
    delete body.asset_generation;
    if (operation === "edit") body.iteration = { mode: "local_round_v1", enabled: true, max_rounds: validRoundTarget() };
    if (operation === "edit") {
      body.profiles = { ...state.profiles, image: null, asset3d: null };
      body.expected_project_revision = project.revision;
      body.base_scene = { run_id: project.current_scene.run_id, revision_id: project.current_scene.revision_id || null };
      body.evaluation = { mode: $("continue-reference").checked ? "reference" : "intent" };
      if ($("asset-generation-enabled").checked) {
        const problem = assetCapabilityProblem(); if (problem) { error(problem); updateReadiness(); return; }
        body.asset_generation = { enabled: true, profile_id: assetProfile().id };
      }
    }
    if (retryOriginal && original && D?.parent(original.body)) body=original.body;
    else if (operation === "conversation") {
      body = D.request(state,project,director.taskContext().existing ? validRoundTarget() : creation?.target(),$("asset-generation-enabled").checked && !assetCapabilityProblem() ? assetProfile()?.id : null);
      try {Object.assign(body,creation?.fields() || {});}catch(e){error(e.message);updateReadiness();return;}
    }
    const safeBody = safeSubmissionBody(body), signature = JSON.stringify(safeBody), pending = submissionFor(project.id);
    if (submissionStorageIssues.has(project.id)) { error(submissionStorageIssues.get(project.id)); return; }
    if (pending && (pending.signature !== signature || pending.run_id) && !retryOriginal) {
      await recoverSubmission(context);
      if (isCurrent(context)) error("先核对上次提交结果；新输入已保留。不会用新身份静默执行第二次请求。"); return;
    }
    if (pending && await recoverSubmission(context)) return;
    if (!isCurrent(context) || busy || pending && (pending.confirmed || pendingSubmissions.get(context.id)?.key !== pending.key)) return;
    const submission = pending || { project_id: project.id, signature, body: safeBody, key: crypto.randomUUID(), created_at: new Date().toISOString(), run_id: null };
    submission.state = "submitting";
    if (!saveSubmission(submission)) { error(submissionStorageIssues.get(context.id)); return; }
    const request = { ...submission.body, idempotency_key: submission.key };
    busy = true; error(); updateReadiness();
    renderConversation({ top: 0, atBottom: true });
    try {
      const result = await api("/api/runs", request), created = result.run || result;
      if (!acceptedRun(created, context, submission.body)) throw new Error("无法确认提交响应，原请求身份已保留。");
      submission.run_id = created.id; submission.state = "accepted"; saveSubmission(submission);
      window.SceneLoopWorkbench?.clearSubmittedDraft?.(context.id, submission.body.prompt);
      creation?.accepted(submission.body);
      if (!isCurrent(context)) return;
      observedConversationRuns.set(created.id, { reads: 0 }); runSelectionEpoch++;
      const acknowledged = { ...created, project_id: context.id, prompt: created.prompt || submission.body.prompt, operation: created.operation || submission.body.operation, status: created.status || "queued", created_at: created.created_at || submission.created_at, idempotency_key: submission.key };
      mergeRun(acknowledged, true); renderConversation({ top: 0, atBottom: true });
      const run = await api(`/api/runs/${encodeURIComponent(created.id)}`);
      if (!isCurrent(context)) return;
      if (!acceptedRun(run, context) || run.id !== created.id) throw new Error("原请求已接受，执行详情身份待核对。");
      clearSubmission(context.id);
      mergeRun(run, true); pollingFailures = 0;
      restoreConversationPosition({ top: 0, atBottom: true });
    } catch (e) {
      if (submission.confirmed || e.status >= 400 && e.status < 500 && !e.outcomeUnknown && !submission.run_id) clearSubmission(context.id);
      else { submission.state = submission.run_id ? "accepted" : "unknown"; saveSubmission(submission); }
      if (isCurrent(context)) error(submission.run_id ? "原请求已接受，执行详情暂未读到；请重新核对，消息已保留。" : e.status >= 400 && e.status < 500 && !e.outcomeUnknown ? `${submissionRejection(e)}（HTTP ${e.status}）输入已保留。` : "提交结果未知；原请求身份与输入已保留，请先核对。不会自动重发。");
    } finally { busy = false; if (isCurrent(context)) renderConversation(); updateReadiness(); schedulePoll(); }
  }
  async function runAction(action) {
    if (!current || busy) return;
    if (action === "cancel" && D?.parent(current) && D.projection(current,"activity")?.safe_to_cancel === false) return;
    if (assetControlsRun(current) && action === "cancel") return;
    if (action === "adopt" && blockers(true).length) { error(blockers(true).join("；")); return; }
    const runId = current.id, context = scope(); busy = true; error(); updateReadiness();
    try {
      await api(`/api/runs/${encodeURIComponent(runId)}/${action}`, {});
      if (!isCurrent(context)) return;
      await loadRun(runId, true); pollingFailures = 0;
    } catch (e) { if (isCurrent(context)) { error(e.message); try { await refreshRuns(); } catch { /* Preserve the action failure. */ } } }
    finally { busy = false; updateReadiness(); schedulePoll(); }
  }
  function initialize() {
    if (initialized || !window.SceneLoopWorkbench) return; initialized = true;
    if (!D) { error("Director 入口模块未载入；请刷新并核对服务源码，暂不发送。"); return; }
    director?.initialize();
    creation?.initialize();
    $("asset-generation-enabled").checked = false; resetAssetForm();
    $("asset-generation-enabled").addEventListener("change", updateReadiness);
    $("asset-max-credits").addEventListener("input", renderAssetRequest);
    $("asset-no-hard-cap").addEventListener("change", renderAssetRequest);
    $("approve-asset-request").addEventListener("click", () => assetAction("approve"));
    $("resume-asset-request").addEventListener("click", () => assetAction("resume"));
    $("end-asset-request").addEventListener("click", () => assetAction("end"));
    $("refresh-asset-request").addEventListener("click", () => refreshAssetRequest());
    $("load-conversation-history").addEventListener("click", loadConversationHistory);
    $("conversation-new-messages").addEventListener("click", () => { restoreConversationPosition({ top: 0, atBottom: true }); $("conversation-new-messages").hidden = true; });
    $("message-scroll").addEventListener("scroll", () => {
      const position = conversationPosition();
      if (Math.abs(position.top - lastConversationTop) > 1) { followConversation = position.atBottom; conversationScrollVersion++; }
      readerPosition = position; lastConversationTop = position.top;
      if (position.atBottom) $("conversation-new-messages").hidden = true;
    });
    $("open-execution-history").hidden=true;
    $("start-run").addEventListener("click", () => mainStopTarget() ? stopMainRequest() : startRun());
    $("message-form").addEventListener("submit", (event) => { event.preventDefault(); return startRun(); });
    $("new-scene-run").addEventListener("click", () => startRun("generate"));
    $("open-project").addEventListener("click", openProjectDialog);
    $("open-walkthrough").addEventListener("click", openWalkthrough);
    $("close-walkthrough").addEventListener("click", () => $("walkthrough-dialog").close());
    $("walkthrough-dialog").addEventListener("close", () => $("open-walkthrough").focus());
    $("close-project").addEventListener("click", () => $("project-dialog").close());
    $("project-dialog").addEventListener("close", () => { projectDialogSession++; $("open-project").focus(); });
    $("project-form").addEventListener("submit", saveProject);
    $("pick-project-folder").addEventListener("click", () => choosePath("folder"));
    $("open-import-scene").addEventListener("click", openImportScene);
    $("empty-import-scene").addEventListener("click", openImportScene);
    $("empty-generate-scene").addEventListener("click", () => {
      if ($("empty-generate-scene").disabled || project?.current_scene) return;
      $("task-input").scrollIntoView({ block: "center" }); $("task-input").focus();
    });
    $("close-import-scene").addEventListener("click", () => $("import-scene-dialog").close());
    $("import-scene-dialog").addEventListener("close", () => {
      importDialogSession++; (project?.current_scene ? $("project-actions-summary") : project ? $("empty-import-scene") : $("open-project")).focus();
    });
    $("pick-scene-file").addEventListener("click", () => choosePath("scene"));
    $("save-checkpoint").addEventListener("click", () => workspaceAction("checkpoint", {
      title: `手动检查点 · Working Version ${project?.workspace?.working_version || 0}`,
    }));
    $("undo-working").addEventListener("click", () => {
      const target = project?.workspace?.recoveries?.at(-1); if (!target) return;
      if (!window.confirm(`将 Working Version ${project.workspace.working_version} 替换为恢复点版本 ${target.working_version}，是否继续？`)) return;
      workspaceAction("undo", { preserve_current: $("preserve-before-restore").checked });
    });
    $("restore-batch").addEventListener("click", () => {
      const target = project?.workspace?.batch_start; if (!target) return;
      if (!window.confirm(`将 Working Version ${project.workspace.working_version} 恢复到批次起点版本 ${target.working_version}，是否继续？`)) return;
      workspaceAction("restore-batch", { preserve_current: $("preserve-before-restore").checked });
    });
    $("checkpoint-picker").addEventListener("change", renderWorkspaceState);
    $("restore-checkpoint").addEventListener("click", () => {
      const checkpointId = $("checkpoint-picker").value;
      const target = project?.workspace?.checkpoints?.find((item) => item.id === checkpointId); if (!target) return;
      if (!window.confirm(`将 Working Version ${project.workspace.working_version} 替换为检查点“${target.title}”（版本 ${target.working_version}），是否继续？`)) return;
      workspaceAction("restore-checkpoint", {
        checkpoint_id: checkpointId, preserve_current: $("preserve-before-restore").checked,
      });
    });
    for (const id of ["workspace-stage", "recovery-limit"]) $(id).addEventListener("change", () => workspaceAction("settings", {
      stage: $("workspace-stage").value, recovery_limit: Number($("recovery-limit").value),
    }));
    $("import-scene-form").addEventListener("submit", (event) => {
      event.preventDefault(); const path = $("import-scene-path").value.trim(), sceneName = $("import-scene-name").value.trim();
      if (!path) { dialogError("import-scene-error", "选择 .blend 文件或填写绝对路径。"); return; }
      return importScene({ path, ...(sceneName ? { scene_name: sceneName } : {}) });
    });
    $("use-current-scene").addEventListener("click", adoptCurrentScene);
    $("open-legacy-runs").addEventListener("click", showLegacyRuns);
    $("close-legacy-runs").addEventListener("click", () => $("legacy-runs-dialog").close());
    $("legacy-runs-dialog").addEventListener("close", () => { legacyDialogSession++; $("project-actions-summary").focus(); });
    $("close-image-dialog").addEventListener("click", () => $("image-dialog").close());
    $("image-dialog").addEventListener("cancel", (event) => { event.preventDefault(); $("image-dialog").close(); });
    $("image-dialog").addEventListener("close", () => {
      $("image-dialog-image").removeAttribute("src"); imageItems = [];
      (imageReturnFocus?.isConnected ? imageReturnFocus : $("task-input")).focus(); imageReturnFocus = null;
    });
    $("image-dialog-image").addEventListener("error", () => { $("image-dialog-image").hidden = true; $("image-dialog-error").hidden = false; });
    $("image-dialog-previous").addEventListener("click", () => { if (imageIndex > 0) { imageIndex--; renderImageDialog(); } });
    $("image-dialog-next").addEventListener("click", () => { if (imageIndex + 1 < imageItems.length) { imageIndex++; renderImageDialog(); } });
    $("adopt-reference").addEventListener("click", () => runAction("adopt"));
    $("cancel-run").addEventListener("click", () => runAction("cancel"));
    $("local-round-target").addEventListener("input", () => { saveBudgetChoice(); updateReadiness(); });
    $("resume-local-round").addEventListener("click", () => localRoundAction("resume"));
    $("end-local-round").addEventListener("click", () => localRoundAction("end"));
    $("refresh-local-round").addEventListener("click", () => refreshLocalRound(current?.id, true));
    $("refresh-refinement").addEventListener("click", refreshRefinement);
    $("open-refinement-details").addEventListener("click", () => {
      if (!project || $("refinement-dialog").open) return;
      refinementReturnFocus = { context: scope(), node: document.activeElement || $("open-refinement-details") };
      $("refinement-dialog").showModal(); $("close-refinement-details").focus({ preventScroll: true });
    });
    $("close-refinement-details").addEventListener("click", () => $("refinement-dialog").close());
    $("refinement-dialog").addEventListener("close", () => {
      const target = refinementReturnFocus; refinementReturnFocus = null;
      if (target && isCurrent(target.context) && target.node.isConnected) target.node.focus({ preventScroll: true });
    });
    $("refresh-environment").addEventListener("click", () => { pollingFailures = 0; assetBase = null; assetBaseRead++; error(); detectEnvironment(); });
    $("execution-mode").addEventListener("change", () => { modeChosen = true; renderEnvironment(); });
    $("show-execution-settings").addEventListener("click", () => {
      $("show-constraints").click(); $("execution-settings").open = true;
      $("execution-settings").scrollIntoView({ behavior: "smooth", block: "nearest" }); $("execution-mode").focus({ preventScroll: true });
    });
    $("task-input").addEventListener("input", updateReadiness);
    $("revision-picker").addEventListener("change", () => {
      const id = $("revision-picker").value;
      if (!revisionList().some((item) => item.id === id)) return;
      viewedRevision = id; openArtifact("candidates");
    });
    window.addEventListener("sceneloop:preview", renderGeneratedPreview);
    window.addEventListener("sceneloop:change", ()=>{updateReadiness();renderConversation();});
    document.addEventListener("visibilitychange", () => { if (!document.hidden && health?.ok) { pollingFailures = 0; refreshRuns().catch((e) => error(e.message)); } });
    renderProject(); renderConversation(); detectEnvironment();
  }
  window.addEventListener("sceneloop:ready", initialize);
  window.addEventListener("pagehide", () => clearTimeout(timer));
  initialize();
})();
