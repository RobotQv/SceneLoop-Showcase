/* Public resource applications. Human actions stay bound to one persisted request. */
(() => {
  "use strict";
  const KEY="sceneloop.resource-action.v1:",id=v=>typeof v === "string" && /^[A-Za-z0-9_.-]{1,128}$/.test(v);
  const count=v=>v === null || Number.isSafeInteger(v) && v >= 0;
  const labels={builder_calls:"Builder 模型调用",judge_calls:"Judge 模型调用",draft_attempts:"候选草稿尝试",saved_rounds:"正式保存",local_generations:"本地生成",tool_calls:"工具调用",observations:"截图观察"};
  const scopeOK=(r,s)=>Object.hasOwn(labels,r) && (r === "saved_rounds" ? ["run","task"].includes(s) : s === (r === "local_generations" ? "task" : "run"));
  const amounts=rows=>rows.map(r=>({resource:r.resource,scope:r.scope,delta:r.requested_delta}));
  const kind=d=>d.kind === undefined ? "resource_extension" : d.kind;
  const localPermission=d=>kind(d) === "local_asset_permission";
  const resumableStatus=d=>d.status === "approved" || d.status === "rejected" && ["local_asset_permission","creative_resources"].includes(kind(d));
  const legacySignature=d=>[d.id,d.version,d.project_id,d.task_id,d.parent_run_id,d.run_id,d.requester_role,d.reason,d.next_step,d.resources,d.checkpoint];
  const signature=d=>JSON.stringify([...legacySignature(d),kind(d),d.permission ?? null,...(d.continuation_alias ? [d.continuation_alias] : [])]);
  const digest=async value=>Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",new TextEncoder().encode(value))),b=>b.toString(16).padStart(2,"0")).join("");
  const canonical=v=>JSON.stringify(v,(_key,value)=>value && typeof value === "object" && !Array.isArray(value) ? Object.fromEntries(Object.keys(value).sort().map(k=>[k,value[k]])) : value);
  const exact=(v,keys)=>v && typeof v === "object" && !Array.isArray(v) && canonical(Object.keys(v).sort()) === canonical([...keys].sort());
  const aliasKeys=["request_parent_run_id","source_parent_run_id","source_run_id","project_id","task_id","request_id","continuation_id"];
  function frozenContinuation(owner,D) {
    const scope=D.projection(owner,"request_scope"),c=scope?.creative_continuation,s=c?.source;
    return D.parent(owner) && !D.readonly(owner) && scope?.intent === "edit" && c?.mode === "same_run" && c.task_id === owner.director?.task?.task_id
      && /^[0-9a-f]{64}$/.test(c.identity || "") && s && [s.parent_id,s.run_id,s.delegation_id,s.scene_id,s.revision_id].every(id) ? c : null;
  }
  function aliasOrigin(a,d) {
    return exact(a,aliasKeys) && aliasKeys.slice(0,6).every(k=>id(a[k])) && /^[0-9a-f]{64}$/.test(a.continuation_id || "")
      && a.project_id === d.project_id && a.task_id === d.task_id && a.source_parent_run_id === d.parent_run_id && a.source_run_id === d.run_id && a.request_id === d.id;
  }
  function checkedAlias(value,owner,records,D,d) {
    const a=value?.continuation_alias;
    if(d.parent_run_id === owner.id){if(a != null)throw new Error("本请求不应替换原申请父身份");return null;}
    const c=frozenContinuation(owner,D),s=c?.source;
    if(!c || !aliasOrigin(a,d) || a.request_parent_run_id !== owner.id || a.continuation_id !== c.identity || a.task_id !== c.task_id
      || a.source_parent_run_id !== s.parent_id || a.source_run_id !== s.run_id || d.run_id === owner.id || owner.active_child_id && owner.active_child_id !== d.run_id)throw new Error("续做消息与原申请的冻结身份无法核对");
    const child=records.find(r=>r.id === d.run_id),sourceParent=records.find(r=>r.id === d.parent_run_id),binding=child?.director_delegation;
    if(child && (child.project_id !== d.project_id || !["edit","generate"].includes(child.operation) || child.operation === "generate" && child.bootstrap === false)
      || binding && (binding.parent_id !== s.parent_id || binding.child_id !== s.run_id || binding.task_id !== d.task_id || binding.delegation_id !== s.delegation_id)
      || sourceParent && (sourceParent.project_id !== d.project_id || !D.parent(sourceParent) || sourceParent.director?.task?.task_id && sourceParent.director.task.task_id !== d.task_id)
      || sourceParent?.director?.delegations?.some(b=>b.child_id === s.run_id && (b.parent_id !== s.parent_id || b.task_id !== d.task_id || b.delegation_id !== s.delegation_id)))throw new Error("可见原任务与续做来源身份冲突");
    return Object.fromEntries(aliasKeys.map(k=>[k,a[k]]));
  }
  function permission(value,runId) {
    if(!exact(value,["provider_id","scope_kind","scope_id","generation_count","purpose","profile","reference"])
      || value.provider_id !== "hunyuan" || !["bootstrap","local_round"].includes(value.scope_kind) || !id(value.scope_id)
      || value.scope_kind === "bootstrap" && value.scope_id !== runId || value.generation_count !== 1
      || typeof value.purpose !== "string" || !value.purpose.trim() || value.purpose.length > 2000
      || !exact(value.profile,["id","revision","model"]) || !id(value.profile.id) || !Number.isSafeInteger(value.profile.revision) || value.profile.revision < 1
      || typeof value.profile.model !== "string" || !value.profile.model.trim() || value.profile.model.length > 200
      || !exact(value.reference,["artifact_id","sha256"]) || !/^[0-9a-f]{32}$/.test(value.reference.artifact_id || "") || !/^[0-9a-f]{64}$/.test(value.reference.sha256 || ""))throw new Error("本轮本地许可的阶段、配置或参考身份无法核对");
    return {provider_id:value.provider_id,scope_kind:value.scope_kind,scope_id:value.scope_id,generation_count:1,purpose:value.purpose,
      profile:{id:value.profile.id,revision:value.profile.revision,model:value.profile.model},reference:{artifact_id:value.reference.artifact_id,sha256:value.reference.sha256}};
  }
  function samePermission(doc,p) {
    try {const s=JSON.parse(p.reviewed),k=s.length === 11 ? "resource_extension" : s[11];
      return [11,13,14].includes(s.length) && k === kind(doc) && (k !== "local_asset_permission" || canonical(s[12]) === canonical(doc.permission));
    } catch {return false;}
  }
  function sameReviewed(doc,p) {
    if(!samePermission(doc,p))return false;
    return JSON.parse(p.reviewed).length === 11 ? JSON.stringify(legacySignature(doc)) === p.reviewed : signature(doc) === p.reviewed;
  }
  function actionBody(d,action,actionId) {
    const body={request_id:d.id,expected_version:d.version,action_id:actionId};
    if(action === "approve"){body.approved=localPermission(d) ? [] : amounts(d.resources);body.basis=localPermission(d) ? "允许本阶段按本申请用途使用1次本地Hunyuan，不追加任务总次数，继续原Builder。" : "批准本申请明列的资源追加，并继续原任务。";}
    if(action === "reject")body.basis=localPermission(d) ? "本阶段不使用本申请的本地Hunyuan，继续原Builder。" : "不追加本申请所列的资源。";
    return body;
  }
  function rows(value,request,allowEmpty=false) {
    if(!Array.isArray(value) || value.length > 16 || request && !value.length && !allowEmpty)throw new Error("资源明细无法核对");
    const keys=new Set();
    return value.map(r=>{
      const key=JSON.stringify([r?.resource,r?.scope]);
      if(!r || !scopeOK(r.resource,r.scope) || keys.has(key) || !["used","base_limit","effective_limit"].every(k=>count(r[k]))
        || request && (!Number.isSafeInteger(r.requested_delta) || r.requested_delta <= 0 || !count(r.proposed_limit))
        || !request && !count(r.remaining))throw new Error("资源单位或申请范围无法核对");
      keys.add(key);return Object.fromEntries(["resource","scope","used","base_limit","effective_limit",...(request ? ["requested_delta","proposed_limit"] : ["remaining"])].map(k=>[k,r[k]]));
    });
  }
  function publicView(value,owner,records,D) {
    const d=value?.resource_request,b=value?.resource_budget;
    const director=D.parent(owner),self=!director && !owner.director_delegation && ["edit","generate"].includes(owner.operation);
    const alias=d && checkedAlias(value,owner,records,D,d);
    if(!d || !id(d.id) || !Number.isSafeInteger(d.version) || d.version < 1 || d.project_id !== owner.project_id || !alias && d.parent_run_id !== owner.id
      || d.task_id !== (director ? owner.director?.task?.task_id : owner.id) || !id(d.task_id) || !id(d.run_id) || !director && !self || D.readonly(owner)
      || d.run_id !== owner.id && !alias && (!director || !D.childFor(owner,records,d.run_id))
      || !["director","builder"].includes(d.requester_role) || !["pending","approved","rejected","cancelled","blocked"].includes(d.status)
      || !["waiting_approval","ready","running","completed","blocked","cancelled"].includes(d.continuation?.state)
      || typeof d.continuation.can_resume !== "boolean" || !(d.continuation.blocker === null || typeof d.continuation.blocker === "string")
      || ![d.reason,d.next_step].every(v=>typeof v === "string" && v.trim() && v.length <= 10000))throw new Error("资源申请的项目、父子任务或状态未确认");
    if(!b || b.schema_version !== 1 || !id(b.policy_id) || !Number.isSafeInteger(b.grants_revision) || b.grants_revision < 0)throw new Error("资源预算身份无法核对");
    if(!["resource_extension","local_asset_permission","creative_resources"].includes(kind(d)) || localPermission(d) && (!Array.isArray(d.resources) || d.resources.length)
      || !localPermission(d) && d.permission != null)throw new Error("申请种类或本轮许可明细无法核对");
    const doc={...Object.fromEntries(["id","version","project_id","task_id","parent_run_id","run_id","requester_role","status","reason","next_step"].map(k=>[k,d[k]])),
      kind:kind(d),...(localPermission(d) ? {permission:permission(d.permission,d.run_id)} : {}),...(alias ? {continuation_alias:alias} : {}),
      resources:rows(d.resources,true,localPermission(d)),checkpoint:d.checkpoint ? {id:d.checkpoint.id,revision_id:d.checkpoint.revision_id,verified:d.checkpoint.verified === true} : null,
      continuation:{state:d.continuation.state,can_resume:d.continuation.can_resume,blocker:d.continuation.blocker},
      action_receipts:Array.isArray(d.action_receipts) ? d.action_receipts.map(r=>Object.fromEntries(["action_id","action","expected_version","resulting_version","status"].map(k=>[k,r[k]]))) : []};
    return {doc,budget:{schema_version:1,policy_id:b.policy_id,grants_revision:b.grants_revision,resources:rows(b.resources,false)}};
  }
  function receipt(doc,p) {
    return p && !p.blocked && samePermission(doc,p) && doc.id === p.request_id && doc.project_id === p.project_id && doc.task_id === p.task_id && doc.parent_run_id === p.parent_run_id && doc.run_id === p.run_id
      && doc.action_receipts.some(r=>r.action_id === p.body.action_id && r.action === p.action && r.expected_version === p.body.expected_version && r.status === "accepted"
        && Number.isSafeInteger(r.resulting_version) && r.resulting_version > r.expected_version && r.resulting_version <= doc.version);
  }
  function create(hooks) {
    const D=hooks.director,entries=new Map();let serial=0;
    const ownerFor=e=>hooks.runs().find(r=>r.id === e.ownerId && r.project_id === e.context.id);
    const current=e=>hooks.isCurrent(e.context) && entries.get(e.ownerId) === e;
    const storageKey=d=>KEY+JSON.stringify([d.project_id,d.parent_run_id,d.id]);
    const node=(tag,text,cls)=>{const n=document.createElement(tag);if(text != null)n.textContent=text;if(cls)n.className=cls;return n;};
    const tell=()=>hooks.changed();
    function pending(doc) {
      try {const raw=window.sessionStorage.getItem(storageKey(doc));if(!raw)return null;if(raw.length > 64000)throw new Error();const p=JSON.parse(raw);
        if(p.project_id !== doc.project_id || p.parent_run_id !== doc.parent_run_id || p.run_id !== doc.run_id || p.task_id !== doc.task_id || p.request_id !== doc.id
          || !["approve","reject","resume"].includes(p.action) || !/^[0-9a-f]{64}$/.test(p.content_digest || "")
          || typeof p.reviewed !== "string" || p.reviewed.length > 48000 || p.display_parent_run_id != null && !id(p.display_parent_run_id)
          || p.continuation_id != null && !/^[0-9a-f]{64}$/.test(p.continuation_id)
          || !/^[A-Za-z0-9_-]{12,128}$/.test(p.body?.action_id || "") || p.body.request_id !== p.request_id || !Number.isSafeInteger(p.body.expected_version) || p.body.expected_version < 1)throw new Error();
        return {...p,verified:false};
      } catch {return {blocked:true};}
    }
    async function verifyPending(e) {
      const p=e.pending;if(!p)return;if(p.verified || p.blocked)return;
      try {
        const s=JSON.parse(p.reviewed);
        if(!Array.isArray(s) || ![11,13,14].includes(s.length) || s[0] !== p.request_id || s[1] !== p.body.expected_version || s[2] !== p.project_id || s[3] !== p.task_id || s[4] !== p.parent_run_id || s[5] !== p.run_id)throw new Error();
        if(s.length === 14){
          if(!aliasOrigin(s[13],{id:p.request_id,project_id:p.project_id,task_id:p.task_id,parent_run_id:p.parent_run_id,run_id:p.run_id}) || s[13].request_parent_run_id !== p.display_parent_run_id || s[13].continuation_id !== p.continuation_id)throw new Error();
        }else if(p.display_parent_run_id != null && p.display_parent_run_id !== p.parent_run_id || p.continuation_id != null)throw new Error();
        const k=s.length === 11 ? "resource_extension" : s[11];if(!["resource_extension","local_asset_permission","creative_resources"].includes(k))throw new Error();
        const d={id:s[0],version:s[1],kind:k,resources:rows(s[9],true,k === "local_asset_permission")};
        if(localPermission(d)){if(d.resources.length)throw new Error();d.permission=permission(s[12],s[5]);}
        if(canonical(p.body) !== canonical(actionBody(d,p.action,p.body.action_id)) || await digest(canonical([p.action,p.reviewed,p.body])) !== p.content_digest)throw new Error();
        if(current(e) && e.pending === p)p.verified=true;
      } catch {if(current(e) && e.pending === p){p.blocked=true;e.error="原操作缓存校验失败；禁止按该缓存重发，请核对原任务。";}}
    }
    function settle(e) {
      if(!e.pending?.verified)return;
      if(!receipt(e.doc,e.pending))return;
      try {const stored=pending(e.doc);if(stored && (stored.blocked || stored.body.action_id !== e.pending.body.action_id || stored.content_digest !== e.pending.content_digest)){e.pending=stored;return;}window.sessionStorage.removeItem(storageKey(e.doc));e.pending=null;}
      catch {e.error="本次动作已受理，但恢复记录未能清除；请保留页面核对。";}
    }
    function accept(e,value) {
      const owner=ownerFor(e);if(!owner)throw new Error("原任务尚未载入");
      const result=publicView(value,owner,hooks.runs(),D),d=result.doc;
      if(e.doc && (d.id !== e.doc.id && e.pending || d.id === e.doc.id && d.version < e.doc.version))throw new Error("原申请尚未确认，不能使用其它申请或旧版本替代");
      if(e.doc?.id !== d.id){e.pending=pending(d);e.rows.clear();e.node.open=d.status === "pending";e.completed=false;}
      else if(!e.pending)e.pending=pending(d);
      e.doc=d;e.budgetSnapshot=result.budget;e.error="";e.notice="";settle(e);
      if(d.continuation.state === "completed" && !e.completed){e.node.open=false;e.completed=true;}
      e.failures=0;
    }
    async function read(e,manual=false) {
      if(!current(e))return false;if(e.reading)return e.reading;if(!manual && e.failures >= 3)return false;
      e.hasRequested=true;e.loading=true;tell();
      const token=++e.readSequence;
      e.reading=(async()=>{try {const value=await hooks.api(`/api/runs/${encodeURIComponent(e.ownerId)}/resource-request`);
        if(!current(e) || token !== e.readSequence)return false;
        accept(e,value);await verifyPending(e);if(!current(e) || token !== e.readSequence)return false;settle(e);return true;
      } catch {if(current(e) && token === e.readSequence){e.failures++;e.error="资源申请暂未能核对；已读明细和原动作保留，请只读刷新。";}return false;}
      finally {if(current(e) && token === e.readSequence){e.loading=false;e.reading=null;tell();}}})();
      return e.reading;
    }
    function can(e,action,original=false) {
      const d=e.doc,owner=ownerFor(e);
      let alias;try{if(d && owner)alias=checkedAlias({continuation_alias:d.continuation_alias},owner,hooks.runs(),D,d);}catch{return false;}
      const target=d?.run_id === owner?.id ? owner : d && owner && (alias ? hooks.runs().find(r=>r.id === d.run_id) : D.childFor(owner,hooks.runs(),d.run_id));
      if(!d || !owner || e.error || !original && e.pending || e.loading || hooks.busy() || owner.cancel_requested || target?.cancel_requested || owner.error?.outcome_unknown || target?.error?.outcome_unknown || !alias && d.run_id !== owner.id && owner.active_child_id !== d.run_id)return false;
      if(localPermission(d) && (action === "approve" || action === "resume" && d.status === "approved")) {
        const quota=e.budgetSnapshot?.resources.find(r=>r.resource === "local_generations" && r.scope === "task");
        if(!quota || [quota.used,quota.effective_limit,quota.remaining].some(v=>v === null))return false;
      }
      if(action === "resume")return resumableStatus(d) && d.continuation.state === "ready" && d.continuation.can_resume && d.continuation.blocker === null;
      return d.status === "pending" && d.continuation.state === "waiting_approval" && d.continuation.blocker === null && d.checkpoint?.verified !== false;
    }
    async function act(e,action) {
      if(!current(e) || e.lock)return;
      if(e.pending){await read(e,true);return;}
      if(!can(e,action))return;
      e.lock=true;const viewed=signature(e.doc);tell();
      try {
        if(!await read(e,true) || !current(e))return;
        if(viewed !== signature(e.doc)){e.notice="申请明细已更新，请核对当前明细后再次选择。";return;}
        if(!can(e,action))return;
        const d=e.doc,body=actionBody(d,action,crypto.randomUUID()),content_digest=await digest(canonical([action,viewed,body]));
        if(!current(e) || viewed !== signature(e.doc) || !can(e,action))return;
        const p={project_id:d.project_id,task_id:d.task_id,parent_run_id:d.parent_run_id,display_parent_run_id:e.ownerId,continuation_id:d.continuation_alias?.continuation_id ?? null,run_id:d.run_id,request_id:d.id,action,content_digest,reviewed:viewed,body};
        try {window.sessionStorage.setItem(storageKey(d),JSON.stringify(p));}catch{e.error="无法保存动作身份；本次未发送，请启用本标签页会话存储后再试。";return;}
        p.verified=true;e.pending=p;tell();
        try {const value=await hooks.api(`/api/runs/${encodeURIComponent(d.continuation_alias ? e.ownerId : d.run_id)}/resource-request/${action}`,body);
          if(current(e))accept(e,value);
        } catch {if(current(e))e.error="本次操作结果尚未确认；原 action_id 已保留，只读核对后再处理。";}
      } catch {if(current(e))e.error=e.pending ? "本次操作结果尚未确认；请核对原申请，不会重复发送。" : "无法准备动作身份；本次未发送，请刷新后再试。";}
      finally {e.lock=false;if(current(e))tell();}
    }
    async function retry(e) {
      const p=e.pending;if(!current(e) || e.lock || !p || p.blocked)return;
      e.lock=true;tell();
      try {
        if(!await read(e,true) || !current(e) || e.pending !== p)return;
        if(!p.verified || p.blocked || !sameReviewed(e.doc,p) || !can(e,p.action,true)) {e.notice="原版本、明细或恢复条件已变化，只能核对原操作，不能重发。";return;}
        const value=await hooks.api(`/api/runs/${encodeURIComponent(p.continuation_id ? p.display_parent_run_id : p.run_id)}/resource-request/${p.action}`,p.body);
        if(current(e))accept(e,value);
      } catch {if(current(e))e.error="原操作结果仍未确认；保留相同 action_id，请只读核对。";}
      finally {e.lock=false;if(current(e))tell();}
    }
    function state(e) {
      if(e.pending)return "本次操作结果待核对";
      const d=e.doc;if(!d)return "资源申请待读取";
      if(localPermission(d)) {
        if(d.status === "pending")return "待批准本轮使用";
        if(["approved","rejected"].includes(d.status))return (d.status === "approved" ? "本轮许可已批准，" : "本轮使用已拒绝，")+({waiting_approval:"等待原Builder恢复状态确认",ready:"等待继续原Builder",running:"原Builder正在继续",completed:"原Builder本次处理已结束",blocked:"原Builder恢复受阻",cancelled:"原任务已停止"}[d.continuation.state]);
        return d.status === "cancelled" ? "本轮许可申请已取消" : "本轮许可受阻";
      }
      if(kind(d) === "creative_resources" && d.status === "rejected")return "未追加，"+({waiting_approval:"等待原任务恢复状态确认",ready:"等待继续原任务反馈",running:"原任务正在继续",completed:"原任务本次处理已结束",blocked:"原任务恢复受阻",cancelled:"原任务已停止"}[d.continuation.state]);
      if(d.status !== "approved")return {pending:"待批准",rejected:"未追加",cancelled:"申请已取消",blocked:"资源申请受阻"}[d.status];
      return {waiting_approval:"已批准，等待恢复状态确认",ready:"已批准，原任务尚未启动",running:"已批准，原任务执行中",completed:"原任务本次续做已结束",blocked:"已批准，原任务恢复受阻",cancelled:"已批准，原任务已取消"}[d.continuation.state];
    }
    function render(e) {
      const d=e.doc,permit=d && localPermission(d);e.summary.textContent=`${d ? (d.requester_role === "director" ? "Director" : "Builder")+(permit ? " 申请本轮 Hunyuan 使用许可" : " 申请追加资源") : "资源申请"} · ${state(e)}`;
      e.reason.textContent=d ? `申请理由：${d.reason}` : "正在核对原任务的资源申请。";
      e.next.textContent=d ? `接下来：${d.next_step}` : "";
      e.blocker.textContent=e.pending?.blocked ? "原动作缓存无法核验，操作受限。" : e.pending ? "原操作尚未有匹配回执；先只读核对。仅原版本与明细仍匹配时，可手动重试同一操作。" : d?.continuation.blocker || "";e.blocker.hidden=!e.blocker.textContent;
      const ordered=[];for(const r of d?.resources || []){
        const key=JSON.stringify([r.resource,r.scope]);let row=e.rows.get(key);
        if(!row){row={node:node("tr"),cells:Array.from({length:7},()=>node("td"))};row.node.append(...row.cells);e.rows.set(key,row);}
        [labels[r.resource]+"（次）",r.scope === "run" ? "本 Run" : "持续任务（跨 Run）",r.used,r.base_limit,r.effective_limit,r.requested_delta,r.proposed_limit].forEach((v,i)=>row.cells[i].textContent=v === null ? "未知" : i === 5 ? `+${v}` : String(v));
        ordered.push(row.node);
      }
      hooks.reconcile(e.tbody,ordered);e.table.hidden=!d || permit;
      const budget=e.budgetSnapshot;
      e.permissionHost.hidden=!permit;
      if(permit) {
        const p=d.permission,q=budget?.resources.find(r=>r.resource === "local_generations" && r.scope === "task");
        e.permissionStage.textContent=`当前${p.scope_kind === "bootstrap" ? "初建阶段" : "局部轮"}使用 1 次 · 阶段标识 ${p.scope_id}；不跨阶段继承。`;
        e.permissionPurpose.textContent=`用途：${p.purpose}`;
        e.permissionProfile.textContent=`本地配置：${p.profile.model} · ${p.profile.id} · 修订 ${p.profile.revision}`;
        e.permissionReference.textContent=`资产参考：${p.reference.artifact_id}\nSHA-256：${p.reference.sha256}`;
        e.permissionQuota.textContent=q ? `任务总次数 ${q.effective_limit ?? "未知"} · 已用 ${q.used ?? "未知"} · 剩余 ${q.remaining ?? "未知"}${q.remaining === 0 ? "；总额度已用完，许可不会补发额度。" : ""}` : "任务总次数尚未确认，不能据此开始本地生成。";
      }
      e.feeNote.textContent=permit ? "仅允许本阶段按此用途使用1次，不追加任务总次数。批准后由原Builder自行决定，仍受任务余额与资源检查；后续阶段须另行许可，云端费用独立批准。" : "额外模型调用按当前配置计费，本地生成不授权云端收费。";
      if(budget)e.budgetLine.textContent="当前后台额度："+budget.resources.map(r=>`${labels[r.resource]} · ${r.scope === "run" ? "本 Run" : "持续任务"} 已用 ${r.used ?? "未知"} / 总额 ${r.effective_limit ?? "未知"} / 剩余 ${r.remaining ?? "未知"}`).join("；");
      else e.budgetLine.textContent="";
      e.errorNode.textContent=e.error || e.notice || "";e.errorNode.hidden=!e.errorNode.textContent;
      for(const [action,button] of Object.entries(e.buttons)){button.disabled=e.lock || !can(e,action);button.hidden=!d || (action === "resume" ? !(resumableStatus(d) && d.continuation.can_resume && d.continuation.state === "ready") : d.status !== "pending");}
      e.buttons.approve.textContent=permit ? "允许本轮使用 1 次" : "批准追加并继续";e.buttons.reject.textContent=permit ? "本轮不使用，继续 Builder" : "不追加";
      e.refresh.disabled=e.lock || e.loading;e.refresh.textContent=e.pending ? "核对原申请" : "刷新申请";
      e.retry.hidden=!e.pending || e.pending.blocked;e.retry.disabled=e.lock || e.loading || !e.pending?.verified || Boolean(e.doc?.continuation.blocker) || e.doc && e.pending && !sameReviewed(e.doc,e.pending);
    }
    function make(owner) {
      const e={ownerId:owner.id,context:hooks.scope(),serial:++serial,doc:null,pending:null,rows:new Map(),failures:0,readSequence:0,error:"",loading:false,lock:false};
      e.node=node("details",null,"resource-request-card");e.node.open=true;e.summary=node("summary");e.reason=node("p");e.next=node("p");
      e.table=node("table");const head=node("thead"),line=node("tr");for(const t of ["资源","范围","已用","基础总额","申请时总额","申请追加","批准后总额"])line.append(node("th",t));head.append(line);e.tbody=node("tbody");e.table.append(head,e.tbody);
      const wrap=node("div",null,"resource-request-table");wrap.append(e.table);e.budgetLine=node("p",null,"field-help");e.blocker=node("p",null,"field-help");e.errorNode=node("p",null,"execution-error");e.errorNode.setAttribute("role","status");
      e.permissionHost=node("section",null,"resource-permission-details");for(const name of ["Stage","Purpose","Profile","Reference","Quota"]){e["permission"+name]=node("p");e.permissionHost.append(e["permission"+name]);}
      e.buttons={};const actions=node("div",null,"resource-request-actions");for(const [kind,label] of [["approve","批准追加并继续"],["reject","不追加"],["resume","继续原任务"]]){const b=node("button",label,"button button-quiet");b.type="button";b.dataset.resourceAction=kind;b.addEventListener("click",()=>act(e,kind));e.buttons[kind]=b;actions.append(b);}
      e.refresh=node("button","刷新申请","text-button");e.refresh.type="button";e.refresh.addEventListener("click",()=>read(e,true));actions.append(e.refresh);
      e.retry=node("button","重试原操作","text-button");e.retry.type="button";e.retry.addEventListener("click",()=>retry(e));actions.append(e.retry);
      const budgetDetails=node("details",null,"resource-budget-details");budgetDetails.append(node("summary","当前后台额度"),e.budgetLine);
      e.feeNote=node("p",null,"field-help");e.node.append(e.summary,e.reason,e.next,e.permissionHost,wrap,budgetDetails,e.feeNote,e.blocker,actions,e.errorNode);
      entries.set(owner.id,e);return e;
    }
    function mount(owner) {
      if(D.readonly(owner) || owner.project_id !== hooks.scope().id || !D.parent(owner) && (owner.director_delegation || !["edit","generate"].includes(owner.operation)))return null;
      const child=D.childFor(owner,hooks.runs()),hint=owner.resource_request || child?.resource_request || frozenContinuation(owner,D);
      let e=entries.get(owner.id);if(!hint && !e)return null;if(!e)e=make(owner);
      // Only rendering mutates DOM, inside the conversation's captured scroll position.
      render(e);if(!e.hasRequested){e.hasRequested=true;void Promise.resolve().then(()=>read(e));}return e.node;
    }
    const continuing=d=>d && resumableStatus(d) && !["completed","cancelled"].includes(d.continuation.state);
    async function refresh(){await Promise.all([...entries.values()].filter(e=>current(e) && e.failures < 3 && (e.pending || !e.doc || e.doc.status === "pending" || continuing(e.doc))).map(e=>read(e)));}
    return {mount,refresh,view:ownerId=>entries.get(ownerId)?.doc,reset:()=>{entries.clear();serial++;},waiting:()=>[...entries.values()].some(e=>current(e) && e.failures < 3 && (e.pending || e.doc?.status === "pending" || continuing(e.doc)))};
  }
  window.SceneLoopResourceRequests={create,publicView,receipt};
})();
