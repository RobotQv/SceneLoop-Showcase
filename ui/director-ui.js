/* Narrow Director request/projection contract. No model calls or authority inference. */
(() => {
  "use strict";
  const terminal = run => ["completed", "failed", "cancelled", "canceled", "interrupted"].includes(run?.status);
  const parent = run => run?.operation === "conversation";
  const readonly = run => parent(run) && (run.conversation_mode === "read_only" || run.request_protocol === "natural_v1"
    && Boolean(run.readonly_accepted || run.director_decision?.as_of) && projection(run,"request_scope")?.max_children === 0
    && ["discuss","inspect","clarify"].includes(projection(run,"request_scope")?.intent));
  function projection(run, key) {
    const outer = run?.[key], inner = run?.director?.[key];
    if (outer != null && inner != null && JSON.stringify(outer) !== JSON.stringify(inner)) return null;
    return outer ?? inner ?? null;
  }
  function policyFields(value) {
    const result = { mode: value?.mode, autonomous: value?.autonomous };
    if (value?.mode === "strict") { result.currency = value.currency; result.limit = value.limit; }
    if (Object.hasOwn(value || {}, "total_saved_limit")) result.total_saved_limit = value.total_saved_limit;
    if (value?.continuous_scope) result.continuous_scope = { proposal_id:value.continuous_scope.proposal_id, scope_digest:value.continuous_scope.scope_digest };
    return result;
  }
  function validPolicy(value) {
    return value && typeof value.autonomous === "boolean" && ["statistics_only", "strict"].includes(value.mode)
      && (value.mode !== "strict" || ["CNY", "USD"].includes(value.currency) && Number.isFinite(value.limit) && value.limit > 0 && value.limit <= 100000)
      && (!Object.hasOwn(value, "total_saved_limit") || Number.isInteger(value.total_saved_limit) && value.total_saved_limit >= 1 && value.total_saved_limit <= 100000)
      && (!value.continuous_scope || /^[0-9a-f]{32}$/.test(value.continuous_scope.proposal_id) && /^[0-9a-f]{64}$/.test(value.continuous_scope.scope_digest));
  }
  function eligibility(project, mode, base, baseKey) {
    if (!project) return { supported: false, reason: "先选择项目" };
    if (mode !== "headless") return { supported: false, reason: "Director 当前不支持 Attached 施工；请选择 Headless 后发送" };
    if (!project.current_scene) return { supported: true, reason: "直接描述目标，Director 会判断讨论、观察或修改" };
    if (!base || base.key !== baseKey || base.loading) return { pending: true, supported: false, reason: "正在核对 Working Scene 类型" };
    if (base.error) return { supported: false, retryable:true, reason: base.error };
    if (base.kind !== "parametric") return { supported: false, reason: base.kind === "imported" ? "普通导入场景的 Director 施工暂不支持；聊天仍由 Director 接收" : "当前场景类型的 Director 施工暂不支持；聊天仍由 Director 接收" };
    if (base.resourceDeclared && !base.resourceVerified) return { supported: false, retryable:base.capabilityState!=="unsupported",reason:base.capabilityState === "unsupported" ? "当前场景的施工能力不受支持；聊天仍由 Director 接收" : "场景资源资格尚未确认；可重新检测环境，聊天仍由 Director 接收" };
    return { supported: true, reason: "Director 接收对话并决定讨论、观察或委派；资源资格由运行时核验，未知依赖会暂停" };
  }
  function taskView(records, projectId) {
    const candidates = records.filter(run => parent(run) && run.project_id === projectId && run.director?.task);
    candidates.sort((a, b) => (b.director.task.revision || 0) - (a.director.task.revision || 0) || String(b.created_at || "").localeCompare(String(a.created_at || "")));
    if (!candidates.length) return null;
    const run = candidates[0], task = run.director.task;
    if (typeof task.task_id !== "string" || !/^[0-9a-f]{32}$/.test(task.task_id) || !Number.isInteger(task.revision) || task.revision < 1 || !Number.isInteger(task.policy_version) || task.policy_version < 1 || !validPolicy(task.policy)) return { error: "持续任务政策身份尚未确认" };
    if (candidates.some(row => row.director.task.task_id !== task.task_id || row.director.task.revision === task.revision && JSON.stringify(policyFields(row.director.task.policy)) !== JSON.stringify(policyFields(task.policy)))) return { error: "持续政策投影不一致，请重新核对" };
    return { run, task };
  }
  function bindings(run) {
    if (!parent(run) || readonly(run) || typeof run.director?.task?.task_id !== "string") return [];
    return (run.director?.delegations || []).filter(row => row.parent_id === run.id && row.task_id === run.director?.task?.task_id && typeof row.child_id === "string" && typeof row.delegation_id === "string");
  }
  function childFor(run, records, requested = null) {
    const rows = bindings(run), id = requested || run.active_child_id || rows.at(-1)?.child_id;
    const binding = rows.find(row => row.child_id === id), child = records.find(row => row.id === id && row.project_id === run.project_id);
    if (!binding || !child || !(child.operation === "edit" || child.operation === "generate" && child.bootstrap === true) || child.director_delegation?.child_id !== child.id || child.director_delegation?.parent_id !== run.id || child.director_delegation?.task_id !== binding.task_id || child.director_delegation?.delegation_id !== binding.delegation_id) return null;
    return child;
  }
  const parentFor = (child, records) => records.find(run => parent(run) && childFor(run, [child], child.id));
  const writeOwner = (records, projectId) => records.find(run => run.project_id === projectId && parent(run) && !readonly(run) && !terminal(run))
    || records.find(run => run.project_id === projectId && !parent(run) && !terminal(run) && run.status !== "awaiting_reference");
  function resultUncertain(run) {
    const recovery = run?.director?.recovery;
    return run?.error?.outcome_unknown === true || displaysTaskPause(run) && /unknown|conflict/.test(run?.director?.task?.pause?.reason || "")
      || (terminal(run) || run?.status === "awaiting_resume") && ["started", "unknown"].includes(recovery?.decision_state);
  }
  const settledReadonly=run=>readonly(run) && run.status==="completed" && !run.error && !run.turn_record_pending && !run.director?.recovery?.turn_pending;
  function displaysTaskPause(run) {
    if(settledReadonly(run))return false;
    if(run?.request_protocol === "natural_v2" && run.status === "completed" && !run.error && !run.turn_record_pending && !run.director?.recovery?.turn_pending){
      const authorization=projection(run,"authorization");
      if(authorization?.pause_applies === false && authorization.blocked_reason === null)return false;
    }
    return run?.request_protocol!=="natural_v1" || ["awaiting_resume","failed","interrupted","cancelled","canceled"].includes(run.status) || Boolean(run.error);
  }
  function pauseApplies(run) {
    if(!displaysTaskPause(run))return false;
    const authorization = projection(run,"authorization");
    return authorization?.pause_applies !== false || authorization.blocked_reason !== null;
  }
  function pauseMessage(run) {
    if (resultUncertain(run)) return "结果尚未确认，已保留原请求；请先核对任务详情，暂不重发。";
    const access=projection(run,"authorization"),blocked=access?.execution_policy_version === 2 && access.scope_approval_required === false && access.runtime_access === "blocked";
    const pause = run?.director?.task?.pause;
    if ((!pause || !pauseApplies(run)) && !blocked) return "";
    const blocking=access?.blocking;
    if (["stop_origin_unverified","active_writer","model_or_cost_unresolved","working_reconciliation","working_commit_unresolved","historical_operation_unverified","external_asset_pending","observation_unresolved"].includes(blocking?.category)
      && (blocking.source_run_id===null || /^[0-9a-f]{32}$/.test(blocking.source_run_id)) && typeof blocking.reason==="string" && blocking.reason.trim()) return blocking.reason.trim().slice(0,500);
    if(!pause)return "";
    if (pause.reason === "user_cancelled") return pause.evidence?.parent_run_id && pause.evidence.parent_run_id!==run.id ? "另一项请求已停止；本次请求暂受该暂停记录影响，需要核对后继续。" : "任务已按你的要求停止，已保存的工作版保留。";
    if (pause.reason === "decision_boundary_invalid") return "原任务的工具调用格式未通过检查；当前请求已暂停，等待处理。";
    if (/economy|budget|price/.test(pause.reason || "")) return "费用或预算需要核对，任务已暂停。";
    return "任务已暂停，需要核对本次请求后再继续。";
  }
  function resourceWaiting(run,child=null,request=undefined) {
    if(!run || terminal(run) || run.cancel_requested || run.error || child?.error?.outcome_unknown || child?.cancel_requested)return false;
    return (request === undefined ? [run.resource_request,child?.resource_request] : [request]).some(r=>r?.status === "pending"
      && (!r.project_id || r.project_id === run.project_id) && (!r.run_id || r.run_id === run.id || child && r.run_id === child.id && !terminal(child)));
  }
  const resourceWaitingText=request=>request?.kind === "local_asset_permission" ? "等待你确认本轮 Hunyuan 使用许可" : "等待你批准追加资源";
  function naturalContinue(run) {
    const capability=run?.director?.recovery?.natural_execution,info=run?.director?.task;
    return parent(run) && run.request_protocol === "natural_v1" && !readonly(run) && ["awaiting_approval","awaiting_resume"].includes(run.status)
      && /^[0-9a-f]{32}$/.test(info?.task_id || "") && Number.isInteger(info?.revision) && info.revision > 0 && Number.isInteger(info?.policy_version) && info.policy_version > 0 && validPolicy(info.policy)
      && capability?.version === 2 && capability.can_continue === true && !resultUncertain(run) && !run.cancel_requested;
  }
  function state(run, child = null, resourceRequest = undefined) {
    if (!parent(run)) return "";
    if (resultUncertain(run)) return "结果需要核对，暂不重新发送";
    if (run.status === "completed") return "本次处理已结束";
    if (terminal(run)) return run.status === "failed" ? "本次任务未完成，请查看详情" : run.status === "interrupted" ? "本次任务已中断，请查看详情" : "本次任务已停止";
    if(resourceWaiting(run,child,resourceRequest))return resourceWaitingText(resourceRequest ?? run.resource_request ?? child?.resource_request);
    if (run.cancel_requested) return readonly(run) ? "正在停止本次问答，施工任务保持原状态" : "正在停止本次任务";
    if(naturalContinue(run))return "原请求可继续，已有资源和成果将保留";
    const access=projection(run,"authorization");
    if(access?.execution_policy_version === 2 && access.scope_approval_required === false){
      if(access.runtime_access === "read_only")return "正在分析当前场景与已有进展";
      if(access.runtime_access === "blocked")return "任务暂时受阻，请查看处理原因";
      if(access.runtime_access === "executing" && !child)return "正在推进当前目标";
    }
    if (run.status === "awaiting_resume") return "任务已暂停，等待处理";
    if (run.status === "awaiting_approval") return "原请求的执行状态需要核对";
    if (run.status === "queued") { const queue = projection(run,"queue"); return "正在排队" + (Number.isInteger(queue?.position) && queue.position > 0 ? ` · 第 ${queue.position} 项` : ""); }
    if (run.status === "waiting_child" && child && terminal(child)) return child.error?.outcome_unknown ? "施工结果需要核对，暂不重复操作" : child.status === "completed" ? "施工已结束，正在整理结果" : "施工已停止，正在核对结果";
    const activity = projection(run,"activity"), queue = projection(run,"queue");
    const phases = { interpreting:"正在理解需求", observing:"正在观察场景", planning:"正在规划下一步", queued:"正在排队", building:"正在施工", asset_approval:"等待你批准资产生成", judging:"独立裁判正在检查修改结果", completed:"本次处理已结束", needs_input:"需要你处理后再继续", paused:"任务已暂停，等待处理", failed:"本次任务未完成，请查看详情", cancelled:"本次任务已停止", interrupted:"本次任务已中断，请查看详情" };
    if (run.status === "waiting_child" && child) {
      if (child?.error?.outcome_unknown) return "施工结果需要核对，暂不重复操作";
      if (child && terminal(child)) return child.status === "completed" ? "施工已结束，正在整理结果" : "施工已停止，正在核对结果";
      if (child?.stage === "asset_approval") return "等待你批准资产生成";
      if (child?.stage === "judging") return "独立裁判正在检查修改结果";
      if (child?.stage === "observing" || child?.stage === "rendering") return "正在观察施工结果";
      if (child?.status === "awaiting_resume") return "施工已暂停，等待处理";
      return "正在施工";
    }
    if (phases[activity?.phase]) return phases[activity.phase] + (activity.phase === "queued" && Number.isInteger(queue?.position) && queue.position > 0 ? ` · 第 ${queue.position} 项` : "");
    if (run.status === "waiting_child") return "正在施工";
    return readonly(run) ? "正在查看已有场景与施工进度" : "正在理解需求并规划下一步";
  }
  function failureMessage(run) {
    if(!run?.error)return "";
    if(resultUncertain(run))return "结果尚未确认，已保留原请求；请先核对任务记录，暂不重发。";
    const messages={invalid_judge_settings:"任务的裁判预算配置不一致，请等待修复后再试。",invalid_brain_settings:"模型输出或思考设置无效，请检查所选模型设置。",
      configuration_required:"模型配置缺失或不完整，请检查并重新选择配置。",provider_error:"模型请求失败，请检查模型配置与服务状态。",credential_error:"模型凭据需要检查，请打开模型配置。",
      invalid_json:"模型响应格式未通过检查，请核对模型配置。",invalid_response:"模型响应格式未通过检查，请核对模型配置。",incomplete_response:"模型响应不完整，请检查模型输出上限。",
      execution_failed:"本地执行失败，请核对Blender环境与任务记录。",blender_unavailable:"Blender环境不可用，请检查本地环境。",profile_conflict:"模型配置已改变，请重新读取当前配置。",
      working_scene_missing:"当前工作版未能读取，请重新核对项目状态。",server_restarted:"服务重启导致本次任务中断，原请求不会自动重发。"};
    const code=run.error.code;
    return typeof code === "string" && Object.hasOwn(messages,code) ? messages[code] : "本次任务未能完成，服务未提供可公开的具体原因。"+(typeof code === "string" && /^[a-z][a-z0-9_]{0,63}$/.test(code) ? `错误代码：${code}。` : "")+"请保留本次任务记录以便核查。";
  }
  function feedback(run, child = null, resourceRequest = undefined) {
    const lines = [`工作进展：${state(run,child,resourceRequest)}`], pause = pauseMessage(run);
    if (pause) lines.push(pause);
    const failure=failureMessage(run);if(failure && !resultUncertain(run))lines.push(`系统状态：${failure}`);
    const scope = projection(run,"request_scope");
    if (typeof scope?.goal === "string" && scope.goal.trim()) lines.push(`已理解任务：${scope.goal.trim().slice(0,300)}`);
    if (scope?.intent === "edit" && !bindings(run).length) lines.push("尚未委派施工。");
    if (terminal(run)) lines.push("尚无正式回复。");
    return lines.join("\n");
  }
  function recoveryAllowed(run, action) {
    if(!["resume","recover"].includes(action))return false;
    const info = run?.director?.task;
    if (!info || !/^[0-9a-f]{32}$/.test(info.task_id) || !Number.isInteger(info.revision) || info.revision < 1
        || !Number.isInteger(info.policy_version) || info.policy_version < 1 || !validPolicy(info.policy)) return false;
    if (!parent(run) || !run.director?.task || !["awaiting_resume", "awaiting_approval", "interrupted", "failed", "completed"].includes(run.status)) return false;
    const recovery = run.director.recovery, pause = run.director.task.pause;
    if (!recovery || recovery.state === "legacy_m1" || ["started", "unknown"].includes(recovery.decision_state) || /unknown|conflict/.test(pause?.reason || "") || run.error?.outcome_unknown) return false;
    if(run.request_protocol === "natural_v2" && run.status === "completed" && !run.error && !run.turn_record_pending && !recovery.turn_pending && !(pause && pauseApplies(run)))return false;
    if(recovery.state === "child_creation_rejected" && ["resume","recover"].includes(action))return false;
    if(run.status === "awaiting_approval" && run.request_protocol === "natural_v1" && projection(run,"request_scope")?.intent === "continuous")return action === "resume" && naturalContinue(run);
    if(action === "resume" && naturalContinue(run))return true;
    if (readonly(run)) return action === "recover" && ["returned", "applied"].includes(recovery.decision_state);
    if (recovery.state === "waiting_child") return false;
    return action === "recover" ? ["returned", "applied"].includes(recovery.decision_state) : Boolean(pause) && recovery.decision_state === "prepared";
  }
  function request(state, project, target, asset) {
    return { operation: "conversation", request_protocol:"natural_v2", project_id: project.id, expected_project_revision: project.revision,
      prompt: state.prompt, profiles: { brain: state.profiles.brain, builder: state.profiles.brain }, constraints: state.constraints,
      execution: { mode: "headless" }, iteration: { mode: "local_round_v1", enabled: true, max_rounds: target },
      judge: { enabled: true, mode: state.judge?.mode || "follow_brain", profile: state.judge?.profile || null,
        reasoning_effort: state.judge?.reasoning_effort ?? null, trigger_rounds: state.judge?.trigger_rounds ?? 3 },
      ...(asset ? { asset_generation: { enabled: true, profile_id: asset } } : {}) };
  }
  function safeBody(value, profile, preservePending = false) {
    const body = { operation: "conversation", prompt: value.prompt, project_id: value.project_id, expected_project_revision: value.expected_project_revision,
      profiles: { brain: profile(value.profiles?.brain), builder: profile(value.profiles?.builder || value.profiles?.brain) },
      constraints: { style: value.constraints?.style || "", preserve: value.constraints?.preserve || "", key_objects_recognizable: value.constraints?.key_objects_recognizable ?? true },
      execution: { mode: value.execution?.mode }, iteration: { mode: value.iteration?.mode, enabled: value.iteration?.enabled, max_rounds: value.iteration?.max_rounds },
      judge: { enabled: value.judge?.enabled, mode: value.judge?.mode, profile: profile(value.judge?.profile), reasoning_effort: value.judge?.reasoning_effort ?? null, trigger_rounds: value.judge?.trigger_rounds },
      conversation_mode: value.conversation_mode || (value.request_protocol === "natural_v1" ? "auto" : "write") };
    if (value.request_protocol === "natural_v2") {
      if(Object.hasOwn(value,"conversation_mode") || Object.hasOwn(value,"director_policy"))throw new Error("自然聊天不接受旧权限模式；原请求不会升级或重发");
      body.request_protocol="natural_v2";delete body.conversation_mode;
    } else if (Object.hasOwn(value,"request_protocol")) {
      if (value.request_protocol !== "natural_v1" || !["auto","read_only"].includes(body.conversation_mode)) throw new Error("原请求协议无法核对；不会转换或重新发送");
      body.request_protocol = "natural_v1";
    } else {
      body.director_policy = policyFields(value.director_policy);
      // Legacy UI submissions never carried a continuous grant; only protected policy actions do.
      delete body.director_policy.continuous_scope;
    }
    if (preservePending && value.budget) body.budget = Object.fromEntries(["currency","limit","max_image_generations","max_asset3d_generations","max_retries"].filter(k=>Object.hasOwn(value.budget,k) && (value.budget[k] == null || ["string","number","boolean"].includes(typeof value.budget[k]))).map(k=>[k,value.budget[k]]));
    if (body.conversation_mode !== "read_only" && value.asset_generation?.enabled === true) body.asset_generation = { enabled: true, profile_id: value.asset_generation.profile_id };
    if (["creation","references","local_resources"].some(k=>Object.hasOwn(value,k))) {
      if (!window.SceneLoopCreation || body.conversation_mode === "read_only") throw new Error("创作或参考图片入口未能核对，不会重新发送。");
      Object.assign(body,window.SceneLoopCreation.safeFields(value));
    }
    return body;
  }
  function create(hooks) {
    const $ = hooks.get;
    let savedTask = null, actionBusy = false, actionError = "", freshnessError = "", actionFeedback = null;
    const actionOwner = run => JSON.stringify([run?.project_id,run?.id]);
    const feedbackFor = run => actionFeedback?.owner === actionOwner(run) ? actionFeedback : null;
    const setFeedback = (run,kind,execution=null,code=null,record=null) => {actionFeedback={owner:actionOwner(run),kind,execution,code,record:record ? {...record} : null};};
    const hasConfirmedResult = (run,actions) => {
      const feedback=feedbackFor(run),record=feedback?.record;
      return feedback?.kind === "accepted" && run.project_id === hooks.project()?.id && actions.includes(record?.action) && originalRecord(run,record,record.action);
    };
    const cannotContinue = run => feedbackFor(run)?.kind === "accepted" && feedbackFor(run).execution?.state === "failed" && feedbackFor(run).execution.code === "invalid_judge_settings";
    const executionUncertain = run => feedbackFor(run)?.kind === "accepted" && (feedbackFor(run).execution?.state === "unknown" || feedbackFor(run).execution?.outcome_unknown === true);
    function actionMessage(run) {
      const feedback=feedbackFor(run);
      if(!feedback)return ["policy","resume","recover"].some(action=>pending(run,action)) ? "此操作结果尚未确认，请核对原操作，暂勿重复提交。" : "";
      if(feedback.kind === "unsent")return "本次操作未发送，原任务未能完成核对；处理说明已保留，请刷新进展后核对。";
      if(feedback.kind === "rejected")return "本次操作已被拒绝，处理说明已保留。"+({invalid_session:"请刷新工作台会话后核对。",invalid_host:"工作台服务地址未通过检查。",invalid_origin:"工作台页面来源未通过检查。",director_resume_basis_unchanged:"请补充本次已核实的情况与处理决定。"}[feedback.code] || "请核对任务记录。");
      if(feedback.kind === "legacy")return "原操作存在旧版记录，但缺少完整身份绑定，无法确认归属。已保留原操作，请保留记录供核查；不会重新提交。";
      if(feedback.kind === "not_found")return "暂未查到原操作回执，仍不能确定是否已接收。原操作身份已保留，请继续只读核对，暂勿重复提交。";
      if(feedback.kind === "accepted") {
        const execution=feedback.execution;
        if(execution?.outcome_unknown === true || execution?.state === "unknown")return "原操作已接收；后续执行结果尚未确认，请核对任务记录，暂不重发。";
        if(execution?.state === "failed")return "原操作已接收，但后续执行未完成。"+(execution.code === "invalid_judge_settings" ? "此任务冻结的裁判预算配置不兼容，不能继续原任务；请保留记录，在修复后另行明确提交新请求。" : failureMessage({error:{code:execution.code}}));
        return "原操作已接收。"+({not_dispatched:"尚未调度后续执行。",scheduled:"后续执行已排队。",running:"后续执行正在进行。",waiting:"后续执行正在等待处理。",completed:"后续执行已结束。"}[execution?.state] || "执行进展请查看任务记录。");
      }
      return "此操作结果尚未确认，请核对原操作，暂勿重复提交。";
    }
    function task() {
      const view = taskView(hooks.runs(), hooks.project()?.id);
      if (view?.error) return view;
      if (view && (!savedTask || view.task.revision >= savedTask.task.revision)) savedTask = view;
      return savedTask;
    }
    function taskContext() {
      const view=task(),hasHistory=hooks.runs().some(run=>parent(run) && run.project_id === hooks.project()?.id);
      return {ready:Boolean(hooks.historyReady() && !view?.error && (view?.task || !hasHistory && !hooks.historyIncomplete?.() && !freshnessError)),existing:Boolean(view?.task),error:view?.error || ""};
    }
    const route = () => eligibility(hooks.project(), $("execution-mode").value, hooks.base(), hooks.baseKey());
    const mode = () => "auto";
    const policy = () => task()?.task?.policy || null;
    // Natural requests inherit authority and pauses from SQL, never from browser controls.
    const problems = () => [];
    function render() {
      const supported = route(), view = task();
      const ready=Boolean(hooks.project() && hooks.historyReady());
      $("director-route").textContent = !ready ? "" : supported.pending ? supported.reason : supported.supported ? "" : supported.reason;
      $("director-route").hidden = !$("director-route").textContent;
      $("director-settings").hidden = !ready;
      $("director-policy-state").textContent = view?.task?.policy?.mode === "strict"
        ? `沿用已保存预算 ${view.task.policy.limit} ${view.task.policy.currency}；新消息不会修改它。`
        : "模型费用仅统计，不是金额硬上限；资源追加与生成许可仍需另行确认。";
      $("director-settings-error").textContent = freshnessError ? "部分任务进展未能同步，可刷新后核对。" : "";
      $("director-settings-error").hidden = !freshnessError;
    }
    const pendingKey = (run, action) => `sceneloop.director-action.v1:${run.project_id}:${run.id}:${action}`;
    function pending(run, action) {
      try { const raw = window.sessionStorage?.getItem(pendingKey(run, action)); return raw ? JSON.parse(raw) : null; }
      catch { return { blocked: true }; }
    }
    function settlePending(run,action,record) {
      const key = pendingKey(run,action), current = pending(run,action);
      if (!record?.action_id || !current || ["action_id","task_id","parent_id","project_id","action","expected_task_version","expected_policy_version","execution_policy_version"].some(key=>current[key] !== record[key])) return false;
      // Real Storage supports removeItem; get/set-only adapters use a null tombstone.
      if (typeof window.sessionStorage.removeItem === "function") window.sessionStorage.removeItem(key);
      else window.sessionStorage.setItem(key,"null");
      return !pending(run,action);
    }
    function confirmedAck(data,run,body,action) {
      const receipt = data?.director_action, returned = data?.run, info = returned?.director?.task;
      if(["receipt_state","project_id","parent_id","action"].some(key=>Object.hasOwn(receipt || {},key)))return confirmedReceipt(receipt,run,{...body,parent_id:run.id,project_id:run.project_id,action},action,false);
      const taskVersion = body.expected_task_version + (action === "recover" ? 0 : 1);
      const policyVersion = body.expected_policy_version + (action === "policy" ? 1 : 0);
      return parent(returned) && returned.id === run.id && returned.project_id === run.project_id
        && info?.task_id === body.task_id && Number.isInteger(info.revision) && info.revision >= taskVersion
        && info.policy_version === policyVersion && validPolicy(info.policy)
        && receipt?.accepted === true && receipt.action_id === body.action_id && receipt.task_id === body.task_id
        && receipt.task_version === taskVersion && receipt.policy_version === policyVersion
        && receipt.source === `protected_human_${action}` && validPolicy(receipt.policy)
        && JSON.stringify(policyFields(receipt.policy)) === JSON.stringify(policyFields(info.policy))
        && (action !== "policy" || JSON.stringify(policyFields(receipt.policy)) === JSON.stringify(policyFields(body.policy)))
        && ["running","local_reply_repaired","child_started","result_checked","decision_scheduled","queue_retained","already_accepted"].includes(data.dispatch_state);
    }
    function originalRecord(run,record,action) {
      return record?.parent_id === run?.id && record.project_id === run.project_id && record.action === action && /^[0-9a-f]{32}$/.test(record.task_id)
        && (record.execution_policy_version === undefined || action === "resume" && record.execution_policy_version === 2)
        && (!run.director?.task?.task_id || record.task_id === run.director.task.task_id)
        && typeof record.action_id === "string" && record.action_id.length > 0
        && Number.isInteger(record.expected_task_version) && record.expected_task_version >= 1 && Number.isInteger(record.expected_policy_version) && record.expected_policy_version >= 1;
    }
    function confirmedReceipt(receipt,run,record,action,lookup=true) {
      return originalRecord(run,record,action) && (receipt?.receipt_state === "accepted" || !lookup && receipt?.receipt_state === undefined) && receipt?.accepted === true
        && ["action_id","project_id","parent_id","task_id","action"].every(key=>receipt[key] === record[key])
        && receipt.source === `protected_human_${action}` && receipt.task_version === record.expected_task_version + (action === "recover" ? 0 : 1)
        && receipt.policy_version === record.expected_policy_version + (action === "policy" ? 1 : 0);
    }
    function executionResult(data) {
      const execution=data.execution;
      if(execution && ["not_dispatched","scheduled","running","waiting","completed","failed","unknown"].includes(execution.state) && typeof execution.outcome_unknown === "boolean")return {state:execution.outcome_unknown ? "unknown" : execution.state,code:execution.code,outcome_unknown:execution.outcome_unknown};
      // Original POST compatibility has a verified full Run; a new receipt alone cannot prove execution.
      if(!Object.hasOwn(data.director_action || {},"project_id") && !execution && data.run){
        const returned=data.run;
        return {state:resultUncertain(returned) ? "unknown" : returned.status === "failed" ? "failed" : returned.status === "completed" ? "completed" : "waiting",code:returned.error?.code,outcome_unknown:resultUncertain(returned)};
      }
      return {state:"unknown",outcome_unknown:true};
    }
    function accepted(run,action,record,data) {
      if(!settlePending(run,action,record))throw new Error("原操作身份已变化，不能确认另一项操作");
      const execution=executionResult(data);
      setFeedback(run,"accepted",execution,null,record);
      if(!["failed","unknown"].includes(execution?.state) && execution?.outcome_unknown !== true)$("director-action-basis").value="";
      actionError="";
    }
    function renderDetails(run) {
      $("run-director").hidden = !parent(run); if (!parent(run)) return;
      const view = run.director, info = view?.task;
      $("run-director-state").textContent = [state(run, childFor(run, hooks.runs())),failureMessage(run)].filter(Boolean).join("；");
      $("run-director-policy").textContent = info && validPolicy(info.policy) ? `原任务 ${info.task_id} · 政策 v${info.policy_version}：${JSON.stringify(info.policy)}` : `任务政策尚未完整确认；原记录 ${JSON.stringify(info?.policy || null)}`;
      $("run-director-pause").textContent = pauseMessage(run);
      $("run-director-pause").hidden = !$("run-director-pause").textContent;
      $("run-director-pause-diagnostic").textContent = `暂停记录：${JSON.stringify(info?.pause || null)}`;
      const asOf = run.director_decision?.as_of || run.readonly_accepted;
      $("run-director-readonly").textContent = readonly(run) ? `${projection(run,"request_scope")?.intent === "inspect" ? "本次分析场景" : "本次回答进度"} · 工作版 ${asOf?.working?.working_version ?? run.director_decision?.working_version ?? "待读取"} · 读取时间 ${asOf?.at || "正在同步"}；未委派施工，观察图片以实际记录为准。` : "施工进展显示在对话附件中，最终回复保存在项目对话里。";
      const children = $("run-director-children"); children.replaceChildren();
      for (const [index,binding] of bindings(run).entries()) { const child = childFor(run, hooks.runs(), binding.child_id); const control = document.createElement("button"); control.type = "button"; control.className = "text-button"; control.textContent = child ? `查看第 ${index+1} 项施工的详情与产物` : `核对第 ${index+1} 项施工记录`; control.addEventListener("click",()=>hooks.openChild(run.id,binding.child_id)); children.append(control); }
      const summary = view?.cost_summary;
      $("run-director-cost").textContent = `模型费用按原单位/证据等级：${JSON.stringify(summary?.units || {})}；未结算 ${summary?.unsettled_count ?? "—"}，未知 ${summary?.unknown_count ?? "—"}；不自动跨币种换算。`;
      $("run-director-assets").textContent = `资产 SQL 历史（非批准报价）：${view?.assets?.job_count ?? 0} Job · ${JSON.stringify(view?.assets?.units || {})}；当前执行 ${view?.assets?.execution?.status || "未核对"}。逐Job批准从可信活动子请求读取。`;
      $("run-director-claim").textContent = `恢复 claim：${view?.recovery?.state || "未知"} / 决策 ${view?.recovery?.decision_state || "未知"}；原事件 ${view?.recovery?.event_id || "未确认"}。`;
      const auth = projection(run,"authorization"), cachedPolicy = pending(run,"policy");
      $("director-continuous-title").textContent = "历史操作核对";
      $("director-continuous").hidden = !cachedPolicy && !hasConfirmedResult(run,["policy"]);
      $("director-continuous-goal").textContent = "只读取原操作回执，不重新提交或更改施工权限。";
      $("director-save-policy").hidden = !cachedPolicy;
      $("director-save-policy").textContent = "核对上次操作结果";
      $("director-save-policy").disabled = actionBusy || !cachedPolicy;
      $("run-director-projection").textContent = `公共进展记录：${JSON.stringify({activity:projection(run,"activity"),request_scope:projection(run,"request_scope"),authorization:auth,queue:projection(run,"queue")})}`;
      const basis = $("director-action-basis").value.trim();
      const actionableProblem = (naturalContinue(run) || info?.pause && pauseApplies(run) || run.error || run.turn_record_pending || view?.recovery?.turn_pending)
        && !cannotContinue(run) && !executionUncertain(run) && ["resume","recover"].some(action=>recoveryAllowed(run,action));
      $("director-human-action").hidden = !actionableProblem && !pending(run,"resume") && !pending(run,"recover");
      $("director-manual-basis").hidden = naturalContinue(run);
      for (const [action,id] of [["resume","director-resume"],["recover","director-recover"]]) {
        const cached = pending(run,action); $(id).textContent = cached ? "核对上次操作结果" : action === "resume" ? naturalContinue(run) ? "继续此请求" : "继续此任务" : "核对已知结果";
        $(id).hidden = action === "recover" && naturalContinue(run) && !cached;
        $(id).disabled = actionBusy || !cached && (!(action === "resume" && naturalContinue(run)) && !basis || !recoveryAllowed(run,action) || cannotContinue(run) || executionUncertain(run));
      }
      $("run-director-error").textContent = feedbackFor(run) ? actionError : ""; $("run-director-error").hidden = !$("run-director-error").textContent;
      $("director-action-status").textContent = actionMessage(run);
      $("director-action-status").hidden = !$("director-action-status").textContent;
    }
    async function act(action) {
      const old = hooks.detailParent(); if (!parent(old) || actionBusy) return;
      const context = hooks.scope(), session = hooks.dialogSession();
      const active=()=>hooks.isCurrent(context) && session === hooks.dialogSession() && $("execution-dialog").open && hooks.detailParent()?.id === old.id;
      const cached = pending(old,action);
      if (cached) {
        actionBusy=true;renderDetails(old);
        try {
          if(!originalRecord(old,cached,action))throw new Error("原动作身份记录不完整或不匹配，不能确认归属");
          const path=`/api/runs/${encodeURIComponent(old.id)}/director/actions/${encodeURIComponent(cached.action_id)}?project_id=${encodeURIComponent(cached.project_id)}&task_id=${encodeURIComponent(cached.task_id)}&action=${encodeURIComponent(action)}`;
          const data=await hooks.api(path);
          if(!active())return;
          if(confirmedReceipt(data?.director_action,old,cached,action)) {
            accepted(old,action,cached,data);
            await hooks.refresh().catch(()=>{});
            if(active())await hooks.readParent(old.id).catch(()=>{});
          } else {
            const receipt=data?.director_action;
            const bound=["action_id","project_id","parent_id","task_id","action"].every(key=>receipt?.[key] === cached[key]);
            setFeedback(old,bound && receipt.receipt_state === "legacy_unverifiable" && receipt.accepted === null ? "legacy" : bound && receipt.receipt_state === "not_found" ? "not_found" : "unknown");
            actionError="原动作回执尚未通过完整身份与版本核对";
          }
        } catch (error) { if (active()) {
          actionError = `${error.message}；原人类动作仍待只读核对，不自动重发`;setFeedback(old,"unknown");
        } }
        finally{actionBusy=false;if(active()){render();renderDetails(hooks.detailParent());hooks.changed();}}
        return; // Query confirmation never dispatches another human action.
      }
      if(action === "policy")return; // Historical policy actions are query-only; ordinary chat never grants a new policy.
      const migration=action === "resume" && naturalContinue(old),basis=migration ? "通过继续此请求按钮继续原目标，沿用原成果与资源。" : $("director-action-basis").value.trim();
      if (!basis || executionUncertain(old) || !recoveryAllowed(old,action) || cannotContinue(old)) return;
      const identityBefore = JSON.stringify([old.id,old.director.task.task_id,old.director.task.revision,old.director.task.policy_version,old.director.task.pause?.pause_id]);
      actionBusy = true; actionError = "";actionFeedback=null;let posted=false; renderDetails(old);
      try {
        const fresh = await hooks.readParent(old.id);
        if (!active())return;
        if (!fresh || fresh.project_id !== old.project_id || identityBefore !== JSON.stringify([fresh.id,fresh.director?.task?.task_id,fresh.director?.task?.revision,fresh.director?.task?.policy_version,fresh.director?.task?.pause?.pause_id]) || !recoveryAllowed(fresh,action))throw new Error("原任务记录已更新，请重新核对后决定");
        if(migration !== (action === "resume" && naturalContinue(fresh)))throw new Error("原请求的继续条件已变化，请重新核对");
        const info = fresh.director.task, body = { action_id: crypto.randomUUID().replace(/-/g,""), task_id: info.task_id, expected_task_version: info.revision, expected_policy_version: info.policy_version, expected_pause_id: info.pause?.pause_id || null, basis,...(migration ? {execution_policy_version:2} : {}) };
        if (!window.sessionStorage) throw new Error("无法保存原人类动作身份；本次未发送");
        const record = { action_id:body.action_id,task_id:body.task_id,parent_id:fresh.id,project_id:fresh.project_id,action,
          expected_task_version:body.expected_task_version,expected_policy_version:body.expected_policy_version,...(migration ? {execution_policy_version:2} : {}) };
        window.sessionStorage.setItem(pendingKey(fresh,action),JSON.stringify(record));
        posted=true;const data = await hooks.api(`/api/runs/${encodeURIComponent(fresh.id)}/director/${action}`,body);
        if (!hooks.isCurrent(context) || session !== hooks.dialogSession() || !$("execution-dialog").open) return;
        if (!confirmedAck(data,fresh,body,action)) throw new Error("原人类动作的成功回执尚未完整确认");
        accepted(fresh,action,record,data);await hooks.refresh().catch(()=>{});
      } catch (error) { if (hooks.isCurrent(context) && session === hooks.dialogSession() && $("execution-dialog").open) {
        const rejected = error.outcomeUnknown !== true && [400,403,409,422].includes(error.status)
          // Other Director errors can also follow SQL admission; their code alone is not a rejection receipt.
          && ["invalid_host","invalid_origin","invalid_session","director_resume_basis_unchanged"].includes(error.code);
        const record = pending(old,action);
        if(!posted){actionError=error.message;setFeedback(old,"unsent");}
        else if (rejected && record && settlePending(old,action,record)) {
          setFeedback(old,"rejected",null,error.code);
          actionError = `${error.message}；原动作已明确拒绝，没有自动重发。核对后需另一次明确人类动作`;
        } else {actionError = `${error.message}；核对原人类动作，不自动重发或换身份`;setFeedback(old,"unknown");}
        await hooks.refresh().catch(()=>{});
      } }
      finally { actionBusy = false; if (hooks.isCurrent(context)) { render(); renderDetails(hooks.detailParent()); hooks.changed(); } }
    }
    function reset() { savedTask = null; actionError = ""; freshnessError = "";actionFeedback=null; $("director-action-basis").value = ""; }
    function initialize() {
      reset();
      $("director-action-basis").addEventListener("input",()=>renderDetails(hooks.detailParent()));
      $("director-save-policy").addEventListener("click",()=>act("policy"));
      for (const [action,id] of [["resume","director-resume"],["recover","director-recover"]]) $(id).addEventListener("click",()=>act(action));
      $("director-refresh").addEventListener("click",()=>hooks.refresh());
    }
    return { route, mode, policy, problems, render, renderDetails, initialize, reset, task, taskContext, hasConfirmedResult,
      readFailed: () => {freshnessError="任务进展暂未同步；请刷新后核对，原输入保留";}, readSucceeded:()=>{freshnessError="";} };
  }
  window.SceneLoopDirectorUI = { parent, readonly, terminal, eligibility, policyFields, validPolicy, taskView, bindings, childFor, parentFor, writeOwner, state, feedback, failureMessage, resultUncertain, resourceWaiting, resourceWaitingText, pauseMessage, projection, naturalContinue, recoveryAllowed, request, safeBody, create };
})();
