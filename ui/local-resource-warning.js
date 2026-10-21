/* One local job's memory-risk consent. All mutations use the protected HTTP API. */
(() => {
  "use strict";
  const KEY="sceneloop.local-resource-warning-action.v1:",validId=v=>typeof v==="string"&&/^[A-Za-z0-9_.-]{1,128}$/.test(v);
  const actionId=v=>typeof v==="string"&&/^[A-Za-z0-9_-]{12,128}$/.test(v);
  const canonical=v=>JSON.stringify(v,(_k,x)=>x&&typeof x==='object'&&!Array.isArray(x)?Object.fromEntries(Object.keys(x).sort().map(k=>[k,x[k]])):x);
  const reasonText={low_ram:"可用内存低于建议",low_commit_headroom:"提交内存余量低于建议",low_vram:"可用显存低于建议",monitor_unavailable:"部分资源余量无法获取"};
  function parse(value) {
    if(value==null)return null;
    if(!validId(value.id)||!["pending","approved","cancelled"].includes(value.status)||typeof value.can_continue!=="boolean"||!Array.isArray(value.reasons)||value.reasons.length>4||new Set(value.reasons).size!==value.reasons.length||value.reasons.some(r=>!Object.hasOwn(reasonText,r)))throw Error("告警身份或状态无法核对");
    const numbers=(raw,keys)=>Object.fromEntries(keys.map(k=>{const n=raw?.[k];if(n!==null&&!(typeof n==='number'&&Number.isFinite(n)&&n>=0))throw Error("余量数据无法核对");return [k,n];}));
    let approval=null;if(value.approval!=null){const a=value.approval;if(!actionId(a.action_id)||typeof a.approved_at!=="string"||a.approved_at.length>64||!Number.isFinite(Date.parse(a.approved_at)))throw Error("告警回执无法核对");approval={action_id:a.action_id,approved_at:a.approved_at};}
    return {id:value.id,status:value.status,snapshot:numbers(value.snapshot,["available_ram_mib","commit_headroom_mib","available_vram_mib","total_vram_mib"]),recommended:numbers(value.recommended,["available_ram_mib","commit_headroom_mib","available_vram_mib"]),reasons:[...value.reasons],can_continue:value.can_continue,approval};
  }
  const binding=d=>({project_id:d.project_id,run_id:d.run_id,request_id:d.id,request_revision:d.request_revision??null,profile_id:d.profile_id,profile_revision:d.profile_revision,model:d.model,input_kind:d.input_kind,job_id:d.job?.job_id??null,reference_id:d.reference?.reference_id??null,reference_sha256:d.reference?.sha256??null,warning_id:d.resource_warning.id});
  const reviewed=d=>({binding:binding(d),version:d.version,warning:d.resource_warning});
  const signature=d=>canonical(reviewed(d));
  const digest=async value=>Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",new TextEncoder().encode(canonical(value)))),b=>b.toString(16).padStart(2,"0")).join("");
  const bodyFor=(r,action,key)=>action==='continue'?{request_id:r.binding.request_id,expected_request_version:r.version,warning_id:r.binding.warning_id,action_id:key,acknowledged_risk:true}:{};
  function sameScope(d,p){const b=binding(d),old=p.reviewed.binding;if(old.job_id===null)b.job_id=null;return canonical(b)===canonical(old);}
  function create(hooks){
    const entries=new Map(),node=(tag,text,cls)=>{const n=document.createElement(tag);if(text!=null)n.textContent=text;if(cls)n.className=cls;return n;};
    const current=e=>hooks.isCurrent(e.context)&&entries.get(e.targetId)===e;
    const view=e=>hooks.view(e.targetId),doc=e=>view(e)?.doc;
    const target=e=>hooks.runs().find(r=>r.id===e.targetId&&r.project_id===e.context.id);
    const owner=e=>hooks.runs().find(r=>r.id===e.ownerId&&r.project_id===e.context.id);
    const related=e=>{const a=owner(e),b=target(e);return a&&b&&hooks.target(a)?.id===b.id&&(!hooks.director.parent(a)||a.active_child_id===b.id);};
    function load(e,d){
      e.storageKey=KEY+JSON.stringify([d.project_id,d.run_id]);
      try{const raw=window.sessionStorage.getItem(e.storageKey);if(!raw)return;const p=JSON.parse(raw);if(raw.length>24000||!["continue","cancel"].includes(p.action)||!actionId(p.action_id)||!p.reviewed||!Number.isSafeInteger(p.reviewed.version)||p.reviewed.version<1||!sameScope(d,p)||!validId(p.reviewed.binding.request_id)||!validId(p.reviewed.binding.warning_id)||!/^[0-9a-f]{64}$/.test(p.digest||""))throw Error();e.pending={...p,verified:false};}
      catch{e.pending={blocked:true};}
    }
    async function verify(e){const p=e.pending;if(!p||p.blocked||p.verified)return;try{parse(p.reviewed.warning);if(canonical(p.body)!==canonical(bodyFor(p.reviewed,p.action,p.action_id))||await digest([p.action,p.reviewed,p.body,p.action_id])!==p.digest)throw Error();if(current(e)&&e.pending===p)p.verified=true;}catch{if(current(e)&&e.pending===p)p.blocked=true;}}
    function settle(e){const p=e.pending,d=doc(e);if(!p?.verified||!d?.resource_warning||!sameScope(d,p))return;
      const accepted=p.action==='continue'?d.resource_warning.status==='approved'&&d.resource_warning.approval?.action_id===p.action_id:d.resource_warning.status==='cancelled'||target(e)?.cancel_requested===true||['cancelled','canceled'].includes(target(e)?.status);
      if(accepted){try{window.sessionStorage.removeItem(e.storageKey);e.pending=null;e.error="";}catch{e.error="动作已受理，恢复记录未能清除；请保留页面核对。";}}
    }
    function can(e,action='continue',ownLock=false){const d=doc(e),t=target(e),a=owner(e);return current(e)&&related(e)&&!e.loading&&(!e.lock||ownLock)&&!e.pending&&!view(e)?.readError&&!hooks.busy()&&d?.resource_warning?.status==='pending'&&hooks.local(d)&&!d.closed&&!hooks.unknown(d)&&!t?.cancel_requested&&!a?.cancel_requested&&!t?.error?.outcome_unknown&&!a?.error?.outcome_unknown&&!hooks.director.resultUncertain(a)&&!hooks.director.terminal(t)&&!hooks.director.terminal(a)&&hooks.matches(d)&&(action==='cancel'||d.resource_warning.can_continue===true&&d.local_process?.stage==='resource_warning'&&d.local_process.evidence==='host_preflight'&&[undefined,'prepared'].includes(d.job?.dispatch));}
    async function read(e,manual=false){if(!current(e)||e.reading||!manual&&e.failures>=3)return false;e.loading=true;hooks.changed();
      e.reading=(async()=>{try{const ok=await hooks.read(e.targetId);if(!current(e))return false;if(!ok)throw Error();await verify(e);if(!current(e))return false;settle(e);e.failures=0;if(!e.pending)e.error="";return true;}catch{if(current(e)){e.failures++;e.error="余量告警暂未能核对；保留原动作，只读刷新后再处理。";}return false;}finally{if(current(e)){e.loading=false;e.reading=null;hooks.changed();}}})();return e.reading;
    }
    async function act(e,action){if(!current(e)||e.lock)return;if(e.pending){await read(e,true);return;}if(!can(e,action))return;
      const before=signature(doc(e));e.lock=true;hooks.changed();
      try{if(!await read(e,true)||!current(e)||!await hooks.confirm(e.ownerId,e.targetId,e.context)||!current(e))return;
        if(!can(e,action,true))return;
        const d=doc(e);if(before!==signature(d)){e.error="余量、配置或请求身份已变化，请核对新告警后再选择。";return;}
        const r=reviewed(d),key=crypto.randomUUID(),body=bodyFor(r,action,key),p={action,action_id:key,reviewed:r,body};p.digest=await digest([action,r,body,key]);
        if(!current(e)||before!==signature(doc(e))||!can(e,action,true))return;
        try{window.sessionStorage.setItem(e.storageKey,JSON.stringify(p));}catch{e.error="无法保存本次动作身份；尚未发送，请启用本标签页会话存储。";return;}
        p.verified=true;e.pending=p;hooks.changed();
        try{const response=await hooks.api(`/api/runs/${encodeURIComponent(e.targetId)}/${action==='continue'?'asset-request/resource-continue':'cancel'}`,body);if(!current(e))return;
          if(action==='continue')hooks.accept(e.targetId,response,e.context);else hooks.acceptRun(response,e.targetId,e.context);
          settle(e);await read(e,true);
        }catch{if(current(e)){e.error="本次动作结果尚未确认；只读核对原动作，不会换身份或再次提交。";await read(e,true);}}
      }catch{if(current(e))e.error="本次告警动作未能核对，请只读刷新原任务。";}
      finally{e.lock=false;if(current(e))hooks.changed();}
    }
    function make(a,t){const e={ownerId:a.id,targetId:t.id,context:hooks.scope(),pending:null,initialized:false,failures:0,error:"",lock:false,loading:false};
      e.node=node('details',null,'local-resource-warning');e.node.open=true;e.summary=node('summary');e.reason=node('p');e.values=node('p');e.note=node('p','数据为本次预检快照，启动前余量仍可能变化。继续可能发生内存不足（OOM）或系统变慢。建议值不是内存上限保证，也不是模型最低要求；实际失败会保留现场，不自动重跑。');e.scope=node('p','仅确认本次作业的内存风险，不增加生成次数、阶段使用许可或云端费用授权。');e.status=node('p',null,'field-help');e.status.setAttribute('role','status');
      const actions=node('div',null,'local-resource-warning-actions');e.allow=node('button','了解风险，继续本次生成','button button-quiet');e.cancel=node('button','取消本次任务','button button-quiet');e.refresh=node('button','刷新处理状态','text-button');
      for(const [b,fn]of[[e.allow,()=>act(e,'continue')],[e.cancel,()=>act(e,'cancel')],[e.refresh,()=>read(e,true)]]){b.type='button';b.addEventListener('click',fn);actions.append(b);}
      e.node.append(e.summary,e.reason,e.values,e.note,e.scope,actions,e.status);entries.set(t.id,e);return e;
    }
    function mount(a){const t=hooks.target(a);if(!t)return null;let e=entries.get(t.id);if(!e)e=make(a,t);
      const d=doc(e),w=d?.resource_warning;if(!e.requested){e.requested=true;void Promise.resolve().then(()=>read(e));}
      if(!w||!hooks.local(d))return e.pending?e.node:null;
      if(!e.initialized){e.initialized=true;load(e,d);void verify(e).then(()=>{if(current(e)){settle(e);hooks.changed();}});}
      settle(e);e.summary.textContent='Hunyuan 内存余量提醒 · '+(e.pending?'原动作待核对':w.status==='approved'?'本次风险已确认':w.status==='cancelled'||target(e)?.cancel_requested?'已请求取消本次任务':'等待你选择');
      e.reason.textContent=w.reasons.map(r=>reasonText[r]).join('；');const mib=n=>n===null?'无法获取':`${n} MiB`;
      e.values.textContent=[['可用内存（RAM）','available_ram_mib'],['提交内存余量','commit_headroom_mib'],['可用显存','available_vram_mib']].map(([name,k])=>`${name}：本次预检 ${mib(w.snapshot[k])} · 建议 ${mib(w.recommended[k])}`).concat(`设备总显存：${mib(w.snapshot.total_vram_mib)}`).join('\n');
      e.node.classList.toggle('is-pending',w.status==='pending');e.allow.hidden=e.cancel.hidden=w.status!=='pending'||target(e)?.cancel_requested===true;
      e.allow.disabled=!can(e);e.cancel.disabled=!can(e,'cancel');e.refresh.disabled=e.loading||e.lock;e.refresh.textContent=e.pending?'核对原动作':'刷新处理状态';
      e.status.textContent=e.pending?.blocked?'原动作缓存无法核验，只能核对原任务。':e.error||(view(e)?.readError?'原资产请求读取失败，告警操作暂不可用。':e.pending?'原动作尚待匹配回执；只读核对，不会再次提交。':!related(e)?'原施工关系已变化，请只读核对原任务。':!hooks.matches(d)?'所选配置与原作业不一致；请核对原任务。':hooks.unknown(d)?'生成结果未知，请先只读核对原任务。':'');
      return e.node;
    }
    return {mount,refresh:async()=>{await Promise.all([...entries.values()].filter(e=>current(e)&&e.failures<3&&(e.pending||!doc(e)?.closed&&(!doc(e)||hooks.local(doc(e))))).map(e=>read(e)));},waiting:()=>[...entries.values()].some(e=>current(e)&&e.failures<3&&Boolean(e.pending||doc(e)?.resource_warning?.status==='pending')),reset:()=>entries.clear()};
  }
  window.SceneLoopLocalResourceWarning={parse,create};
})();
