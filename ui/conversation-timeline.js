/* Public projection presentation only. No authority or model execution. */
(() => {
  "use strict";
  const roles=new Set(["director","builder","judge","system"]),kinds=new Set(["commentary","tool","phase"]),statuses=new Set(["running","completed","failed","blocked","unknown"]);
  const compare=(a,b)=>{for(let i=0;i<Math.min(a.length,b.length);i++)if(a[i]!==b[i])return a[i]-b[i];return a.length-b.length;};
  const createState=()=>({items:new Map(),received:false,hasMore:false,cursor:null,paged:false,final:false,open:true});
  function merge(state,doc,parentId,older=false){
    if(!doc||doc.schema_version!==1||doc.parent_run_id!==parentId||!Array.isArray(doc.entries)||doc.entries.length>500||typeof doc.has_more!=="boolean"||doc.has_more&&typeof doc.before_cursor!=="string")return false;
    for(const row of doc.entries){
      if(!row||typeof row.id!=="string"||!row.id||row.id.length>256||!Array.isArray(row.order)||!row.order.length||row.order.length>16||!row.order.every(Number.isSafeInteger)||!roles.has(row.role)||!kinds.has(row.kind)||!statuses.has(row.status)||typeof row.text!=="string")continue;
      const previous=state.items.get(row.id);if(previous&&compare(previous.order,row.order)!==0)continue;
      state.items.set(row.id,{id:row.id,order:[...row.order],kind:row.kind,role:row.role,status:row.status,text:row.text.slice(0,16000)});
    }
    state.received=true;
    if(older||!state.paged){state.hasMore=doc.has_more;state.cursor=doc.has_more?doc.before_cursor:null;}
    if(older)state.paged=true;
    return true;
  }
  const entries=state=>[...state.items.values()].sort((a,b)=>compare(a.order,b.order)||a.id.localeCompare(b.id));
  function finalize(state,hasFinal){if(hasFinal&&!state.final){state.open=false;state.final=true;}}
  function stopTarget(records,projectId,readonly){
    const rows=records.filter(r=>r.project_id===projectId&&!r.director_delegation),terminal=r=>["completed","failed","cancelled","canceled","interrupted"].includes(r.status);
    const sorted=[...rows].sort((a,b)=>String(b.created_at||"").localeCompare(String(a.created_at||""))||a.id.localeCompare(b.id));
    const active=sorted.filter(r=>!terminal(r)&&!["awaiting_resume","awaiting_reference","awaiting_approval"].includes(r.status));
    return active.find(r=>r.operation==="conversation"&&!readonly(r)&&r.status!=="queued")||active.find(r=>r.operation!=="conversation")||active.find(r=>!readonly(r))||active[0]
      ||null;
  }
  function bindInput(input,submit){
    let composing=false,compositionBoundary=false;
    input.addEventListener("compositionstart",()=>{composing=true;});
    input.addEventListener("compositionend",()=>{composing=false;compositionBoundary=true;});
    input.addEventListener("keyup",e=>{if(e.key==="Enter"||e.keyCode===229)compositionBoundary=false;});
    input.addEventListener("keydown",e=>{
      if(e.key!=="Enter"){compositionBoundary=false;return;}
      if(e.shiftKey||e.altKey||e.isComposing||composing||compositionBoundary||e.keyCode===229)return;
      if(e.repeat){e.preventDefault();return;}
      e.preventDefault();if(!input.disabled&&input.value.trim())return submit();
    });
  }
  window.SceneLoopConversation={createState,merge,entries,finalize,stopTarget,bindInput};
})();
