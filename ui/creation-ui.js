/* Project-owned references and bounded task configuration; execution follows natural-language intent. */
(() => {
  "use strict";
  const MAX_BYTES = 4 * 1024 * 1024, KEY = "sceneloop.project-reference.v1:";
  const integer = (value, low, high) => Number.isInteger(value) && value >= low && value <= high;
  const localProfile = p => p?.kind === "asset3d" && p.provider_id === "hunyuan" && p.model === "hunyuan3d-2mini"
    && p.mode === "local_process" && p.credential_mode === "none" && p.server_managed === true && !p.is_draft
    && integer(p.revision,1,Number.MAX_SAFE_INTEGER) && /^[A-Za-z0-9_-]{1,100}$/.test(p.id || "")
    && [p.python_path,p.repo_dir,p.model_dir].every(v=>typeof v === "string" && v.trim());
  function reference(value, projectId) {
    if (!value || value.project_id !== projectId || !/^[0-9a-f]{32}$/.test(value.artifact_id || "")
      || !/^[0-9a-f]{64}$/.test(value.sha256 || "") || !["scene","asset"].includes(value.purpose)
      || !["image/png","image/jpeg"].includes(value.media_type) || !integer(value.byte_size,1,MAX_BYTES)
      || value.url !== `/api/projects/${encodeURIComponent(projectId)}/references/${value.artifact_id}`) throw new Error("图片的项目、用途或保存身份无法核对，请重新添加。");
    return Object.fromEntries(["artifact_id","project_id","purpose","sha256","media_type","byte_size","url"].map(k=>[k,value[k]]));
  }
  const descriptor = r => ({artifact_id:r.artifact_id,sha256:r.sha256,purpose:r.purpose});
  function capabilityIdentity(value, project) {
    return Boolean(value?.schema_version === 1 && value.project_id === project?.id && value.project_revision === project.revision
      && value.selection?.run_id === project.current_scene?.run_id && (value.selection?.revision_id || null) === (project.current_scene?.revision_id || null)
      && integer(value.working_version,1,Number.MAX_SAFE_INTEGER) && value.working_version === project.workspace?.working_version
      && value.stage === project.workspace.stage);
  }
  function qualifiedCapability(value, project, features=[]) {
    return Boolean(capabilityIdentity(value,project)
      && value.supported === true && value.self_contained_verified === true && value.reason_code === "native_self_contained_supported"
      && ["blend_sha256","plan_sha256","inspection_sha256"].every(k=>/^[0-9a-f]{64}$/.test(value.evidence?.[k] || ""))
      && features.every(k=>value.verified_features?.[k] === true));
  }
  function safeFields(value) {
    const result = {};
    if (Object.hasOwn(value,"references")) {
      const refs=value.references;
      if (!Array.isArray(refs) || !integer(refs.length,1,2) || new Set(refs.map(r=>r?.purpose)).size !== refs.length
        || refs.some(r=>!r || !/^[0-9a-f]{32}$/.test(r.artifact_id || "") || !/^[0-9a-f]{64}$/.test(r.sha256 || "") || !["scene","asset"].includes(r.purpose))) throw new Error("参考图片身份未确认。");
      result.references=refs.map(descriptor);
    }
    if (Object.hasOwn(value,"creation")) {
      const c=value.creation,p=value.director_policy;
      const natural=value.request_protocol === "natural_v2";
      if ((!natural && (value.request_protocol !== "natural_v1" || value.conversation_mode !== "auto")) || natural && (Object.hasOwn(value,"conversation_mode") || Object.hasOwn(value,"director_policy")) || c?.enabled !== true || c.allow_new_objects !== true
        || !Array.isArray(c.stop_conditions) || !integer(c.stop_conditions.length,1,8) || c.stop_conditions.some(s=>typeof s !== "string" || !s.trim() || s.length > 1000)
        || natural && !["structure","appearance"].includes(c.review_stage) || !natural && (p?.mode !== "statistics_only" || p.autonomous !== true || !integer(p.total_saved_limit,1,100000))) throw new Error("任务的保存上限或停止条件无法核对。");
      result.creation={enabled:true,allow_new_objects:true,...(natural ? {review_stage:c.review_stage} : {}),stop_conditions:c.stop_conditions.map(s=>s.trim())};
      if(!natural)result.director_policy={mode:"statistics_only",autonomous:true,total_saved_limit:p.total_saved_limit};
    }
    if (Object.hasOwn(value,"local_resources")) {
      const r=value.local_resources;
      if (!result.creation || !/^[A-Za-z0-9_-]{1,100}$/.test(r?.profile_id || "") || !integer(r.max_generations,1,12)
        || !integer(r.job_timeout_seconds,60,3600) || r.max_in_flight !== 1) throw new Error("本地生成额度或单作业等待设置无效。");
      result.local_resources={profile_id:r.profile_id,max_generations:r.max_generations,job_timeout_seconds:r.job_timeout_seconds,max_in_flight:1};
      if(Object.hasOwn(r,"current_stage_permission")) {
        const p=r.current_stage_permission;
        if(!p || Object.keys(p).sort().join(",") !== "generation_count,scope" || p.scope !== "first_builder_stage" || p.generation_count !== 1)throw new Error("首阶段许可仅允许首个施工阶段使用1次本地生成。");
        result.local_resources.current_stage_permission={scope:"first_builder_stage",generation_count:1};
      }
    }
    return result;
  }
  const sha256 = async bytes => Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256",bytes)),b=>b.toString(16).padStart(2,"0")).join("");
  function create(hooks) {
    const $=hooks.get;
    let projectId=null,sequence=0,attachment=null,loading=false,failure="",stageBinding=null;
    const binding=()=>JSON.stringify([projectId,attachment ? descriptor(attachment) : null,hooks.profile()]);
    const current = (context,token) => hooks.isCurrent(context) && sequence === token && projectId === context.id;
    function store(r,id=projectId) {
      if (r) window.localStorage.setItem(KEY+id,JSON.stringify(r)); else window.localStorage.removeItem(KEY+id);
    }
    function render() {
      const context=hooks.taskContext();
      const stage=$("creation-stage-permission"),stageProblem=context.existing ? "本次沿用原任务资源；新阶段使用许可通过原申请确认。" : !integer(Number($("creation-local-generations").value),1,12) ? "先填写 1–12 的任务本地生成总次数。" : !localProfile(hooks.profile()) ? "先保存并选择本地 Hunyuan2mini 配置。" : attachment?.purpose !== "asset" ? "先添加一张已保存的资产参考图片。" : "";
      const stageAvailable=!stageProblem;
      if(stage.checked && (stageBinding !== binding() || !stageAvailable)){stage.checked=false;stageBinding=null;}
      const disabled=!projectId || hooks.busy() || !context.ready;
      stage.disabled=disabled || !stageAvailable;
      $("creation-stage-permission-help").textContent=stageProblem || "仅适用于首个对应Run/阶段，不增加任务总次数；后续阶段另行申请。";
      $("reference-file").disabled=$("reference-purpose").disabled=disabled || loading || !hooks.supported();
      $("remove-reference").disabled=disabled || loading;
      $("reference-chip").hidden=!attachment && !failure;
      if (attachment) {
        $("reference-image").src=attachment.url;
        $("reference-label").textContent=attachment.purpose === "asset" ? "资产参考 · 仅用于主体形状" : "场景参考 · 用于场景与验收";
      } else {$("reference-image").removeAttribute("src");$("reference-label").textContent=failure ? "图片尚未核对" : "";}
      $("reference-status").textContent=loading ? "正在保存并核对项目图片…" : failure || (attachment ? "图片已保存到当前项目，将随本条消息发送。" : projectId && !hooks.supported() ? "聊天图片附件需要受支持的 Headless 场景。" : "PNG / JPEG，最多 4 MiB、最长边 4096；用途在添加前选择。");
      $("reference-status").setAttribute("role",failure ? "alert" : "status");
      $("creation-settings").hidden=!projectId || !hooks.supported() || context.existing;
      for (const id of ["creation-total-saves","creation-local-generations","creation-job-timeout","creation-stop"]) $(id).disabled=disabled;
      $("creation-summary").textContent=`本任务最多正式保存 ${$("creation-total-saves").value} 次，任务本地生成总次数 ${$("creation-local-generations").value}；${stage.checked ? "另明确允许首个施工阶段使用1次，后续阶段另行申请。" : Number($("creation-local-generations").value) > 0 ? "如需本地生成，仍须确认当前阶段使用许可。" : "本次未开放本地生成。"}模型费用仅统计，云端资产仍独立批准。`;
    }
    const changed = () => {render();hooks.changed();};
    async function select(id) {
      if (id === projectId) return;
      projectId=id;const token=++sequence;attachment=null;loading=false;failure="";stageBinding=null;
      $("creation-stage-permission").checked=false;
      $("reference-file").value="";$("reference-purpose").value="scene";
      $("creation-total-saves").value="6";$("creation-local-generations").value="0";$("creation-job-timeout").value="900";
      $("creation-stop").value="达到本阶段目标或正式保存上限时停止\n本地生成额度用尽时不再生成\n无法确认保留要求或连续两轮无改善时停止";
      render();
      if (!id) return;
      const context=hooks.scope();
      try {
        const raw=window.localStorage.getItem(KEY+id);if (!raw) return;
        if (raw.length > 2000) throw new Error("图片记录无法核对，请移除后重新添加。");
        const r=reference(JSON.parse(raw),id);loading=true;changed();
        const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20000);
        let response,bytes;
        try {response=await fetch(r.url,{credentials:"same-origin",signal:controller.signal});
          if (!response.ok || response.headers.get("Content-Type")?.split(";")[0] !== r.media_type) throw new Error("项目图片读取失败，请移除后重新添加。");
          bytes=await response.arrayBuffer();
        } finally {clearTimeout(timer);}
        if (bytes.byteLength !== r.byte_size || bytes.byteLength > MAX_BYTES || await sha256(bytes) !== r.sha256) throw new Error("项目图片内容与原身份不符，请移除后重新添加。");
        if (current(context,token)) attachment=r;
      } catch (e) {if(current(context,token)) failure=e.message || "项目图片未能核对，请重新添加。";}
      finally {if(current(context,token)){loading=false;changed();}}
    }
    async function upload() {
      const file=$("reference-file").files?.[0];$("reference-file").value="";
      if (!file || !projectId || loading || hooks.busy() || !hooks.supported()) return;
      const context=hooks.scope(),token=++sequence,purpose=$("reference-purpose").value;
      $("creation-stage-permission").checked=false;stageBinding=null;
      loading=true;failure="";changed();
      try {
        if (!integer(file.size,1,MAX_BYTES) || !["image/png","image/jpeg"].includes(file.type) || !["scene","asset"].includes(purpose)) throw new Error("请选择不超过 4 MiB 的 PNG 或 JPEG 图片。");
        const bytes=await file.arrayBuffer(),sha=await sha256(bytes);
        if (!current(context,token)) return;
        let binary="";for (let i=0;i<bytes.byteLength;i+=16384) binary+=String.fromCharCode(...new Uint8Array(bytes,i,Math.min(16384,bytes.byteLength-i)));
        const result=await hooks.api(`/api/projects/${encodeURIComponent(context.id)}/references`,{purpose,media_type:file.type,content_base64:btoa(binary)});
        if (!current(context,token)) return;
        const r=reference(result.reference,context.id);
        if (r.sha256 !== sha || r.purpose !== purpose || r.media_type !== file.type || r.byte_size !== bytes.byteLength) throw new Error("图片保存响应与原文件不符，请重新添加；尚未创建任务。");
        store(r);attachment=r;
      } catch(e) {if(current(context,token)) failure=e.message || "图片上传失败；需求文字已保留，请重新添加图片。";}
      finally {if(current(context,token)){loading=false;changed();}}
    }
    function problem(operation="conversation") {
      if(loading)return "等待项目图片保存与身份核对";
      if(failure)return failure;
      if(operation !== "conversation")return attachment ? "此兼容入口不能采用聊天附件；请移除附件，或使用受支持的聊天入口。" : "";
      try {fields();return "";} catch(e){return e.message;}
    }
    function fields() {
      const result=attachment ? {references:[descriptor(attachment)]} : {};
      const context=hooks.taskContext();
      if(!context.ready)throw new Error(context.error || "等待项目任务与额度信息同步，请稍后刷新。");
      if(context.existing)return result;
      if(!hooks.supported())return result; // Conversation remains available; unverified construction gets no new capability fields.
      const count=Number($("creation-local-generations").value),profile=hooks.profile();
      if(!integer(count,0,12))throw new Error("本地生成上限应为 0–12 的整数。");
      if(!integer(Number($("creation-total-saves").value),1,12))throw new Error("本任务正式保存上限应为 1–12 的整数。");
      result.creation={enabled:true,allow_new_objects:true,review_stage:"structure",stop_conditions:$("creation-stop").value.split(/\r?\n/).filter(s=>s.trim())};
      if(count) {
        if(!localProfile(profile))throw new Error("先保存并选择 Hunyuan2mini 本地图生 shape 配置，或将本地生成上限设为 0。");
        if(attachment?.purpose !== "asset")throw new Error("本地图生 shape 需要一张已保存的资产参考图片。");
        result.local_resources={profile_id:profile.id,max_generations:count,job_timeout_seconds:Number($("creation-job-timeout").value),max_in_flight:1};
        if($("creation-stage-permission").checked)result.local_resources.current_stage_permission={scope:"first_builder_stage",generation_count:1};
        result.asset_generation={enabled:true,profile_id:profile.id};
      }
      else if($("creation-stage-permission").checked)throw new Error("首阶段许可需要正数任务总次数、已保存的本地配置和资产参考。");
      else if(localProfile(profile))result.asset_generation={enabled:true,profile_id:profile.id};
      safeFields({request_protocol:"natural_v2",...result});
      return result;
    }
    function accepted(body) {
      const id=body.project_id;
      // A late ACK must never clear a new project's draft or a replacement image.
      try {const raw=window.localStorage.getItem(KEY+id),r=raw && reference(JSON.parse(raw),id);
        if(r && body.references?.some(v=>JSON.stringify(descriptor(r))===JSON.stringify(descriptor(v))))store(null,id);
      } catch { /* Keep an unreadable recovery record; never write another project. */ }
      if(id !== projectId)return;
      if(body.creation || body.local_resources?.current_stage_permission){$("creation-stage-permission").checked=false;stageBinding=null;}
      if(attachment && body.references?.some(r=>JSON.stringify(descriptor(r))===JSON.stringify(descriptor(attachment))))attachment=null;
      render();
    }
    function initialize() {
      $("reference-file").addEventListener("change",upload);
      $("remove-reference").addEventListener("click",()=>{if(loading || hooks.busy())return;sequence++;attachment=null;failure="";$("creation-stage-permission").checked=false;stageBinding=null;try{store(null);}catch{failure="图片记录未能清除，请保留页面并重试。";}changed();});
      $("creation-stage-permission").checked=false;
      $("creation-stage-permission").addEventListener("change",()=>{stageBinding=$("creation-stage-permission").checked ? binding() : null;changed();});
      for(const id of ["creation-total-saves","creation-local-generations","creation-job-timeout","creation-stop"]) $(id).addEventListener("input",changed);
    }
    return {initialize,select,render,problem,fields,accepted,target:()=>Number($("creation-total-saves").value)};
  }
  window.SceneLoopCreation={create,reference,safeFields,localProfile,capabilityIdentity,qualifiedCapability};
})();
