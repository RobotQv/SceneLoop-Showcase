"use strict";

(() => {
  const P = window.SceneLoopProfiles;
  const $ = (id) => document.getElementById(id);
  const KINDS = ["brain", "image", "asset3d"];
  const LABELS = { brain: "大脑", image: "生图", asset3d: "3D 生成" };
  const KEYS = { legacy: "sceneloop.configuration-draft.v1", library: "sceneloop.model-profiles.v2", workspace: "sceneloop.workspace.v2" };
  const FIELDS = { name: "name", kind: "kind", preset: "preset", provider_id: "provider", model: "model", base_url: "base-url", credential_env: "credential", credential_mode: "credential-mode", mode: "mode", python_path: "python", model_dir: "model-dir", repo_dir: "repo-dir" };
  const NUMBER_FIELDS = { max_output_tokens: "max-output-tokens", request_timeout_seconds: "request-timeout-seconds", thinking_budget: "thinking-budget" };
  const LIMITS = [["max-images", "max_image_generations", 1, 50], ["max-asset3d", "max_asset3d_generations", 0, 50], ["max-retries", "max_retries", 0, 10]];
  let presets = { brain: [], image: [], asset3d: [] }, library = P.emptyLibrary(), workspace = emptyWorkspace();
  let libraryBlocked = false, workspaceBlocked = false, toastTimer;
  let activePicker = null, editingId = null, settingsReturnFocus = null;
  let previewKind = "render";
  let quickReasoningBusy = false, quickReasoningUncertain = false;
  let profileServiceReady = false, profileSync = null, profileMutation = false;
  let activeProjectId = null, activeProjectContext = null;
  let secretStorage = { available: false }, editorSession = 0, secretGeneration = 0;
  let verificationBusy = false, verificationReceipt = null, editorVersion = 0, editorVerifiedAt = null, editorReturnFocus = null;
  let verificationAttempt = null, editorConnectionCheck = null;
  let editingProfile = null, secretTyped = false, secretRevealed = false;

  function emptyWorkspace() {
    return {
      project_drafts: {}, project_constraints: {},
      iteration: { enabled: true, max_rounds: 3 },
      judge: { enabled: true, profile_id: "follow_brain", reasoning_effort: "", trigger_rounds: 3 },
      budget: { currency: "CNY", amount: "", max_image_generations: "2", max_asset3d_generations: "3", max_retries: "1" },
      constraints: { style: "", preserve: "", key_objects_recognizable: true }, chat: { draft: "", messages: [] },
    };
  }
  function toast(message) {
    clearTimeout(toastTimer); $("toast").textContent = message; $("toast").hidden = false;
    toastTimer = setTimeout(() => { $("toast").hidden = true; }, 6000);
  }
  function notice(message) {
    $("load-notice").textContent += `${$("load-notice").textContent ? " " : ""}${message}`; $("load-notice").hidden = false;
  }
  function readWorkspace(value, legacy = false) {
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Invalid workspace");
    if (!legacy) {
      const object = (item) => item && typeof item === "object" && !Array.isArray(item);
      if (!object(value.budget) || !object(value.constraints) || !object(value.chat)) throw new Error("Invalid workspace sections");
      for (const key of Object.keys(emptyWorkspace().budget)) if (typeof value.budget[key] !== "string") throw new Error(`Invalid budget ${key}`);
      if (!["CNY", "USD"].includes(value.budget.currency)) throw new Error("Invalid budget currency");
      if (typeof value.constraints.style !== "string" || typeof value.constraints.preserve !== "string" || typeof value.constraints.key_objects_recognizable !== "boolean") throw new Error("Invalid constraints");
      if (typeof value.chat.draft !== "string" || !Array.isArray(value.chat.messages)) throw new Error("Invalid chat");
      for (const message of value.chat.messages) {
        if (!object(message) || typeof message.text !== "string" || (message.created_at !== undefined && typeof message.created_at !== "string")) throw new Error("Invalid message");
        if (message.model_configs !== undefined) {
          if (!object(message.model_configs)) throw new Error("Invalid model snapshot");
          const snapshot = P.emptyLibrary();
          for (const kind of KINDS) {
            const profile = message.model_configs[kind];
            if (profile === null) continue;
            if (!object(profile) || profile.kind !== kind) throw new Error("Invalid model snapshot kind");
            snapshot.profiles.push(profile); snapshot.selected[kind] = profile.id;
          }
          P.normalizeLibrary(snapshot);
        }
      }
      if (value.project_constraints !== undefined && (!object(value.project_constraints) || Object.entries(value.project_constraints).some(([id, constraint]) => !/^[A-Za-z0-9_-]{1,80}$/.test(id) || !object(constraint) || typeof constraint.style !== "string" || typeof constraint.preserve !== "string" || typeof constraint.key_objects_recognizable !== "boolean"))) throw new Error("Invalid project constraints");
      if (value.project_drafts !== undefined && (!object(value.project_drafts) || Object.entries(value.project_drafts).some(([id, draft]) => !/^[A-Za-z0-9_-]{1,80}$/.test(id) || typeof draft !== "string"))) throw new Error("Invalid project drafts");
      if (value.iteration !== undefined && (!object(value.iteration) || typeof value.iteration.enabled !== "boolean" || !Number.isInteger(value.iteration.max_rounds) || value.iteration.max_rounds < 1 || value.iteration.max_rounds > 6)) throw new Error("Invalid iteration settings");
      if (value.judge !== undefined && (!object(value.judge) || typeof value.judge.enabled !== "boolean"
        || typeof value.judge.profile_id !== "string" || typeof value.judge.reasoning_effort !== "string"
        || !["", "none", "low", "medium", "high", "xhigh", "max"].includes(value.judge.reasoning_effort)
        || !Number.isInteger(value.judge.trigger_rounds) || value.judge.trigger_rounds < 1 || value.judge.trigger_rounds > 6)) throw new Error("Invalid judge settings");
      // Missing settings belong to the previous single-pass workflow; never increase its paid calls silently.
      const result = P.sanitize(value);
      if (!result.iteration) result.iteration = { enabled: false, max_rounds: 3 };
      if (!result.judge) result.judge = emptyWorkspace().judge;
      result.judge.enabled = true; result.judge.trigger_rounds = Math.min(3, result.judge.trigger_rounds);
      return result;
    }
    const result = emptyWorkspace();
    result.iteration.enabled = false;
    result.judge = emptyWorkspace().judge;
    for (const key of Object.keys(result.budget)) {
      const v = value.budget?.[key]; if (typeof v === "string" || typeof v === "number") result.budget[key] = String(v);
    }
    if (!["CNY", "USD"].includes(result.budget.currency)) result.budget.currency = "CNY";
    for (const key of ["style", "preserve"]) if (typeof value.constraints?.[key] === "string") result.constraints[key] = value.constraints[key];
    if (typeof value.constraints?.key_objects_recognizable === "boolean") result.constraints.key_objects_recognizable = value.constraints.key_objects_recognizable;
    if (typeof value.chat?.draft === "string") result.chat.draft = value.chat.draft;
    if (Array.isArray(value.chat?.messages)) result.chat.messages = value.chat.messages.filter((m) => m && typeof m.text === "string");
    return result;
  }
  function load() {
    let oldState = null, legacyInvalid = false, readingFailed = false;
    const read = (key) => {
      try { return { ok: true, raw: localStorage.getItem(key) }; }
      catch { readingFailed = true; return { ok: false, raw: null }; }
    };
    const lib = read(KEYS.library), work = read(KEYS.workspace);
    if ((lib.ok && !lib.raw) || (work.ok && !work.raw)) {
      const legacy = read(KEYS.legacy);
      if (legacy.raw) {
        try {
          const old = JSON.parse(legacy.raw);
          if (old.storage_version !== 1 || !old.state || typeof old.state !== "object" || Array.isArray(old.state)) throw new Error();
          oldState = old.state;
        } catch { legacyInvalid = true; notice("旧配置无法识别，原记录已保留，未自动覆盖。"); }
      }
    }
    if (readingFailed) {
      libraryBlocked = true; workspaceBlocked = true;
      notice("浏览器存储读取失败，已暂停写入，防止覆盖尚未读取的原记录。请恢复存储访问后刷新页面。");
    }
    let saveLibrary = false, saveTask = false;
    if (lib.ok && lib.raw) {
      try { library = P.normalizeLibrary(JSON.parse(lib.raw)); }
      catch { libraryBlocked = true; notice("配置库无法解析，已保留原记录，暂不写入配置库。"); }
    } else if (legacyInvalid) libraryBlocked = true;
    else if (lib.ok && !libraryBlocked) {
      try { library = oldState ? P.migrateLegacy(oldState) : P.emptyLibrary(); saveLibrary = true; }
      catch { libraryBlocked = true; notice("旧模型配置结构无法识别，原记录已保留，暂不写入配置库。"); }
    }
    if (work.ok && work.raw) {
      try {
        const saved = JSON.parse(work.raw);
        if (saved.schema_version !== 2) throw new Error();
        workspace = readWorkspace(saved.workspace);
      } catch { workspaceBlocked = true; notice("任务记录无法解析，已保留原记录，暂不自动保存任务。"); }
    } else if (legacyInvalid) workspaceBlocked = true;
    else if (work.ok && !workspaceBlocked) {
      try { workspace = oldState ? readWorkspace(oldState, true) : emptyWorkspace(); saveTask = true; }
      catch { workspaceBlocked = true; notice("旧任务记录结构无法识别，原记录已保留，暂不自动保存任务。"); }
    }
    if (workspaceBlocked) workspace.iteration.enabled = false;
    // Write only after both records have loaded. A failed migration write remains
    // retryable and never prevents displaying the other successfully read record.
    if (saveLibrary && !libraryBlocked) {
      try { localStorage.setItem(KEYS.library, JSON.stringify(library)); }
      catch { notice("配置库已载入本页，但未能保存；后续修改时会重试，旧记录仍保留。"); }
    }
    if (saveTask && !workspaceBlocked) {
      try { localStorage.setItem(KEYS.workspace, JSON.stringify({ schema_version: 2, workspace })); }
      catch { notice("任务记录已载入本页，但未能保存；后续修改时会重试，旧记录仍保留。"); }
    }
    if (oldState && !lib.raw && library.profiles.length) notice("旧模型配置已迁移到配置库；不完整的配置标为待补全。旧存储记录仍保留。");
  }
  function saveWorkspace() {
    if (workspaceBlocked) { $("save-status").textContent = "原任务记录已保留，自动保存暂停"; return false; }
    try {
      localStorage.setItem(KEYS.workspace, JSON.stringify({ schema_version: 2, workspace }));
      $("save-status").textContent = "任务记录已保存到此浏览器"; return true;
    } catch { $("save-status").textContent = "任务记录未能保存，请保留当前页面"; return false; }
  }
  function commitLibrary(next) {
    if (libraryBlocked) { toast("原配置库已保留，当前无法写入。请先备份并检查原记录。"); return false; }
    next = P.normalizeLibrary(next);
    try { localStorage.setItem(KEYS.library, JSON.stringify(next)); }
    catch { toast("配置未能保存，原配置保持不变。请检查浏览器存储空间。"); return false; }
    library = next; renderLibrary(); renderConfig(); $("save-status").textContent = "配置描述与选择已缓存到此浏览器";
    window.dispatchEvent(new CustomEvent("sceneloop:change")); return true;
  }
  // Server errors are mapped to fixed UI copy: never echo request bodies or key text.
  function profileError(status, code) {
    const known = {
      endpoint_key_confirmation_required: "服务商或接口地址已改变，请重新输入 API Key。",
      profile_revision_conflict: "配置已在其他页面更新，请刷新配置库后重新打开。",
      profile_snapshot_mismatch: "配置已发生变化，请刷新配置库后重新选择。",
      profile_name_conflict: "同类配置名称已存在，请换一个名称。",
      profile_id_conflict: "此配置 ID 已存在，请刷新并核对已有配置后再添加。",
      api_key_required: "请填写这份配置的 API Key。",
      invalid_api_key: "API Key 格式无效，请检查后重新填写。",
      stored_key_required: "此配置未保存可查看的密钥。",
      credential_mode_conflict: "密钥方式与所填内容不一致，请检查后重新保存。",
      profile_conflict: "配置已在其他页面更新，请刷新配置库后重新打开。",
      revision_conflict: "配置已在其他页面更新，请刷新配置库后重新打开。",
      duplicate_name: "同类配置名称已存在，请换一个名称。",
      secret_required: "请填写这份配置的 API Key。",
      credential_required: "请填写这份配置的 API Key。",
      secret_binding_changed: "服务商或接口地址已改变，请重新输入 API Key。",
      secret_storage_unavailable: "当前系统无法使用加密密钥保存，请改用环境变量方式。",
      verification_stale: "验证后配置已变更或回执已过期，请重新验证后保存。",
      verification_busy: "已有连接验证正在进行，请等待结果；不会自动重试。",
      provider_http_error: "验证请求未通过，请检查连接或凭据；本次未获得可信结论。",
      request_outcome_unknown: "本次未获得可信结论；不会自动重试。",
      invalid_verification_response: "验证响应不完整，本次未获得可信结论。",
      invalid_image_response: "验证响应不符合协议，本次未获得可信结论。",
      missing_credential: "配置的密钥不可用，请检查保存的密钥或环境变量。",
      unsupported_kind: "此类型没有已支持的只读检查，本次无法确认。",
      unsupported_provider: "此服务商没有已支持的只读检查，本次无法确认。",
      profile_not_found: "这份配置已被删除，请刷新配置库。",
    };
    return known[code] || (status === 409 ? "配置存在冲突，请刷新配置库并检查名称后重试。"
      : status === 404 ? "配置接口不可用或配置已删除；请刷新，确认后端已更新并重新启动。"
      : status === 403 ? "本机服务验证已失效，请刷新配置库后重试。"
      : status === 400 ? "配置未保存，请检查字段、密钥和接口地址；更改服务商或地址时需重新输入密钥。"
      : "本机配置服务暂不可用。请确认后端已启动；若刚保存过，请刷新确认结果后再操作。");
  }
  async function profileApi(path, body, waitMs = 20000, controller = new AbortController()) {
    const timeout = setTimeout(() => controller.abort(), waitMs);
    try {
      const options = { cache: "no-store", signal: controller.signal };
      if (body !== undefined) {
        const healthResponse = await fetch("/api/health", options);
        const health = await healthResponse.json();
        if (!healthResponse.ok || !health.csrf_token) throw new Error();
        options.method = "POST";
        options.headers = { "Content-Type": "application/json", "X-SceneLoop-Token": health.csrf_token };
        options.body = JSON.stringify(body);
      }
      const response = await fetch(path, options), data = await response.json().catch(() => null);
      if (!response.ok || !data) {
        const error = new Error(profileError(response.status, data?.error?.code)); error.profileSafe = true; throw error;
      }
      return data;
    } catch (error) {
      if (error.profileSafe) throw error;
      if (path === "/api/profiles/verify") throw new Error("本次未获得可信结论。请检查本机服务后再显式验证；不会自动重试。");
      throw new Error("无法完成本机配置请求。请确认后端已更新并启动；保存结果不确定时，先刷新配置库确认。");
    } finally { clearTimeout(timeout); }
  }
  function cacheServerLibrary(next) {
    library = P.normalizeLibrary(next);
    if (!libraryBlocked) {
      try { localStorage.setItem(KEYS.library, JSON.stringify(library)); }
      catch { toast("配置已同步到本页，但浏览器未能缓存当前选择。已保存配置仍保留在本机服务。"); }
    }
    renderLibrary(); renderConfig(); if (profileServiceReady) renderServiceStatus();
    window.dispatchEvent(new CustomEvent("sceneloop:change"));
  }
  function serverProfile(value) {
    const profile = {};
    for (const key of Object.keys(FIELDS)) if(key !== "repo_dir" || Object.hasOwn(value || {},key)) profile[key] = typeof value?.[key] === "string" ? value[key] : "";
    for (const key of [...Object.keys(NUMBER_FIELDS), "reasoning_effort"]) profile[key] = value?.[key] ?? null;
    profile.credential_mode = value.credential_mode || P.credentialMode(value);
    Object.assign(profile, { id: value.id, revision: value.revision, has_secret: value.has_secret === true,
      verification: value.verification === "verified" ? "verified" : "unverified", verified_at: value.verified_at || null, is_draft: false, server_managed: true });
    profile.connection_check = P.normalizeConnectionCheck(value.connection_check);
    if (profile.connection_check) {
      profile.verification = profile.connection_check.status === "authenticated" ? "verified" : "unverified";
      profile.verified_at = profile.verification === "verified" ? profile.connection_check.checked_at : null;
    }
    return profile;
  }
  function serviceStatus(text, failed = false) {
    $("profile-service-status").textContent = text; $("profile-service-status").classList.toggle("is-error", failed);
  }
  function renderServiceStatus() {
    const count = library.profiles.filter((item) => item.server_managed).length, drafts = library.profiles.length - count;
    serviceStatus(`本机配置库已连接 · ${count} 份配置${drafts ? ` · ${drafts} 份浏览器配置待导入` : ""}${secretStorage.available ? " · 密钥加密保存可用" : " · 当前系统仅支持环境变量凭据"}`);
  }
  async function refreshProfiles() {
    if (profileSync) return profileSync;
    if (profileMutation) return false;
    invalidateVerification();
    $("refresh-profiles").disabled = true;
    profileSync = (async () => {
      try {
        const data = await profileApi("/api/profiles");
        if (!Array.isArray(data.profiles) || !data.secret_storage || typeof data.secret_storage.available !== "boolean") throw new Error();
        const incoming = data.profiles.map(serverProfile), ids = new Set(incoming.map((item) => item.id));
        const next = P.sanitize(library);
        // Only replace managed cache entries. Browser-only drafts need an explicit import.
        const drafts = next.profiles.filter((item) => !item.server_managed);
        for (const draft of drafts) {
          if (ids.has(draft.id)) {
            const oldId = draft.id; draft.browser_original_id = oldId;
            draft.id = `browser-${crypto.randomUUID()}`;
            for (const kind of KINDS) if (next.selected[kind] === oldId) next.selected[kind] = draft.id;
          }
        }
        if (drafts.length && !libraryBlocked) {
          // Keep a sanitized original until the user explicitly imports the browser copy.
          const backupKey = "sceneloop.browser-profiles.backup.v2";
          if (!localStorage.getItem(backupKey)) localStorage.setItem(backupKey, JSON.stringify(P.sanitize(library)));
        }
        next.profiles = [...incoming, ...drafts];
        for (const kind of KINDS) if (!next.profiles.some((item) => item.id === next.selected[kind] && item.kind === kind)) next.selected[kind] = null;
        cacheServerLibrary(next); secretStorage = data.secret_storage; profileServiceReady = true;
        renderServiceStatus(); quickReasoningUncertain = false;
        renderCredentials(); return true;
      } catch (error) {
        profileServiceReady = false;
        serviceStatus(error.message?.startsWith("无法完成") || error.profileSafe ? error.message : "配置服务尚未连接或版本过旧。请更新并重启本地后端，再刷新配置库。浏览器原配置已保留。", true);
        renderCredentials(); return false;
      } finally { profileSync = null; $("refresh-profiles").disabled = false; renderQuickReasoning(); renderVerification(); window.dispatchEvent(new CustomEvent("sceneloop:change")); }
    })();
    return profileSync;
  }
  function clearSecret() {
    secretGeneration++; secretTyped = false; secretRevealed = false;
    $("profile-api-key").value = ""; $("profile-api-key").type = "password";
    $("toggle-profile-secret").textContent = "显示";
    $("toggle-profile-secret").setAttribute("aria-pressed", "false");
    $("toggle-profile-secret").setAttribute("aria-label", "显示 API Key");
    $("toggle-profile-secret").disabled = false;
  }
  function resetEditor() {
    editorSession++; invalidateVerification();
    verificationAttempt?.controller.abort(); verificationAttempt = null; verificationBusy = false;
    clearSecret(); editingProfile = null; editingId = null; closeProviderHelp();
  }
  let providerHelpPinned = false, providerHelpHover = false, providerHelpFocus = false;
  function refreshProviderHelp(visible) {
    $("provider-help-text").hidden = !visible;
    $("provider-help").setAttribute("aria-expanded", String(visible));
  }
  function updateProviderHelp() { refreshProviderHelp(providerHelpPinned || providerHelpHover || providerHelpFocus); }
  function closeProviderHelp() { providerHelpPinned = false; providerHelpHover = false; providerHelpFocus = false; refreshProviderHelp(false); }
  function consumeProviderHelpEscape(event) {
    // A prevented keydown has no default cancel afterwards, so no flag is
    // left behind: the next independent Escape is evaluated on its own.
    if (event.key !== "Escape" || $("provider-help-text").hidden) return false;
    event.preventDefault(); event.stopPropagation();
    closeProviderHelp();
    return true;
  }
  function consumeProviderHelpCancel(event) {
    // Only a cancel arriving while help is still visible is consumed. A
    // cancel after an already-prevented keydown never happens, and an
    // independent later cancel must exit the dialog normally.
    if ($("provider-help-text").hidden) return false;
    event.preventDefault(); event.stopPropagation();
    closeProviderHelp();
    return true;
  }
  function bindProviderHelp() {
    const button = $("provider-help");
    button.addEventListener("click", () => {
      // Explicit activation toggles the pinned state; unpinning also clears
      // residual hover/focus so the second activation really closes help.
      // Fresh mouseenter/focus re-arms display, and a later click reopens.
      if (providerHelpPinned) closeProviderHelp();
      else { providerHelpPinned = true; updateProviderHelp(); }
    });
    button.addEventListener("mouseenter", () => { providerHelpHover = true; updateProviderHelp(); });
    button.addEventListener("mouseleave", () => { providerHelpHover = false; updateProviderHelp(); });
    button.addEventListener("focus", () => { providerHelpFocus = true; updateProviderHelp(); });
    button.addEventListener("blur", () => { providerHelpFocus = false; updateProviderHelp(); });
    $("profile-editor").addEventListener("keydown", (event) => {
      if (event.key === "Escape" && consumeProviderHelpEscape(event)) return;
    });
    $("settings-dialog").addEventListener("keydown", (event) => {
      if (event.key === "Escape" && consumeProviderHelpEscape(event)) return;
    });
  }
  function sameCredentialBinding() {
    return editingProfile && editingProfile.provider_id === $("profile-provider").value.trim()
      && editingProfile.base_url === $("profile-base-url").value.trim();
  }
  function renderCredentials() {
    const mode = $("profile-credential-mode").value || "stored";
    $("profile-secret-field").hidden = mode !== "stored"; $("profile-env-field").hidden = mode !== "env";
    const retained = editingProfile?.server_managed && editingProfile.has_secret;
    $("profile-api-key").placeholder = retained ? "已安全保存，留空保留；输入新密钥可替换" : "输入此配置使用的 API Key";
    $("profile-api-key-help").textContent = !profileServiceReady ? "请先连接已更新的本机服务。密钥不会保存到浏览器。"
      : !secretStorage.available ? "当前系统不支持加密密钥保存，请选择环境变量方式。"
      : retained && !sameCredentialBinding() ? "服务商或接口地址已改变，请重新输入 API Key 后保存。"
      : retained ? "留空保留已保存密钥；点击右侧“显示”可查看。关闭或切换编辑器后清除本页明文。"
      : "密钥仅发送至本机服务加密保存，不写入浏览器配置、导出文件或任务记录。";
  }
  async function toggleSecret() {
    const input = $("profile-api-key"), toggle = $("toggle-profile-secret");
    if (input.type === "text") {
      input.type = "password";
      if (secretRevealed && !secretTyped) { input.value = ""; secretRevealed = false; }
      toggle.textContent = "显示"; toggle.setAttribute("aria-pressed", "false"); toggle.setAttribute("aria-label", "显示 API Key"); return;
    }
    if (!input.value && editingProfile?.server_managed && editingProfile.has_secret) {
      const session = editorSession, generation = ++secretGeneration, previous = editingProfile;
      toggle.disabled = true;
      try {
        const data = await profileApi(`/api/profiles/${encodeURIComponent(previous.id)}/reveal`, { expected_revision: previous.revision });
        if (session !== editorSession || generation !== secretGeneration || $("profile-editor").hidden || !$("settings-dialog").open) return;
        if (typeof data.api_key !== "string") throw new Error();
        input.value = data.api_key; secretRevealed = true; secretTyped = false;
      } catch (error) {
        if (session === editorSession && generation === secretGeneration) editorError(error.message || "暂时无法查看已保存密钥，请刷新配置库。");
        return;
      } finally { if (session === editorSession && generation === secretGeneration) toggle.disabled = false; }
    }
    input.type = "text"; toggle.textContent = "隐藏"; toggle.setAttribute("aria-pressed", "true"); toggle.setAttribute("aria-label", "隐藏 API Key");
  }
  function editorError(text = "") { $("profile-editor-error").textContent = text; $("profile-editor-error").hidden = !text; }
  function setProfileBusy(value) {
    profileMutation = value; $("save-profile").disabled = value; $("save-profile").textContent = value ? "保存中…" : "保存配置";
    for (const node of $("profile-form").querySelectorAll("input, select")) node.disabled = value;
    if (!value) $("profile-kind").disabled = Boolean(editingId);
  }
  function verificationLabel(profile) {
    const check = P.normalizeConnectionCheck(profile?.connection_check);
    if (check) {
      const reasons = {
        local_paths_present: "路径存在，未加载/验证模型", unsupported_readonly_check: "没有已支持的只读检查",
        authentication_rejected: "凭据被拒绝", access_denied: "访问受限，需检查权限", rate_limited: "服务限流，请稍后手动验证",
        service_error: "服务发生错误", dns_failed: "域名解析失败", tls_failed: "安全连接失败", connection_failed: "连接失败",
        unsafe_target: "检查目标不符合安全要求", check_timeout: "本次未获得可信结论", invalid_check_response: "检查响应不完整或不符合协议",
        local_path_missing: "本地路径不存在", missing_credential: "凭据不可用",
      };
      const label = { authenticated: "连接及鉴权通过", reachable: "服务可达，凭据未验证", failed: "验证失败", inconclusive: "无法确认" }[check.status];
      return `${label}${reasons[check.code] ? ` · ${reasons[check.code]}` : ""} · ${new Date(check.checked_at).toLocaleString()}`;
    }
    const timestamp = profile?.verified_at;
    return timestamp && Number.isFinite(Date.parse(timestamp)) ? `旧验证记录 · ${new Date(timestamp).toLocaleString()}` : "尚未验证";
  }
  function renderVerification() {
    $("profile-verification-status").textContent = verificationBusy ? "正在验证…" : verificationLabel({ connection_check: editorConnectionCheck, verified_at: editorVerifiedAt });
    $("verify-profile").disabled = verificationBusy || profileMutation || Boolean(profileSync) || !profileServiceReady;
    $("verify-profile").textContent = verificationBusy ? "验证中…" : "验证连接";
    $("profile-verification-help").textContent = "仅检查连接及可确认的凭据，不生成内容，不证明模型能力或额度；不自动重试。验证不自动保存草稿。";
    $("save-profile").disabled = verificationBusy || profileMutation;
  }
  function invalidateVerification() {
    editorVersion++; verificationReceipt = null; editorVerifiedAt = null; editorConnectionCheck = null; renderVerification();
  }
  async function verifyEditor() {
    if (verificationBusy || profileMutation || profileSync || !profileServiceReady) { editorError("请等待配置服务就绪或当前操作结束。"); return; }
    const draft = editorDraft(), validation = P.validateProfile(draft, library, editingId);
    if (draft.credential_mode === "stored" && (!$("profile-api-key").value.trim() && !(editingProfile?.server_managed && editingProfile.has_secret && sameCredentialBinding())
        || editingProfile?.has_secret && !sameCredentialBinding() && !secretTyped)) validation.errors.api_key = "请填写 API Key；更改接口或服务商后需要重新输入。";
    showErrors(validation.errors); if (Object.keys(validation.errors).length) return;
    const body = { profile: draft }, session = editorSession, version = editorVersion;
    if (editingProfile) body.profile.id = editingProfile.id;
    if (editingProfile?.server_managed) body.expected_revision = editingProfile.revision;
    if (draft.credential_mode === "stored" && secretTyped) body.api_key = $("profile-api-key").value.trim();
    const attempt = { controller: new AbortController() }; verificationAttempt = attempt;
    const current = () => verificationAttempt === attempt && session === editorSession && version === editorVersion && !$("profile-editor").hidden;
    verificationBusy = true; verificationReceipt = null; editorVerifiedAt = null; editorConnectionCheck = null; editorError(); renderVerification();
    try {
      const result = await profileApi("/api/profiles/verify", body, 25000, attempt.controller);
      if (!current() || attempt.controller.signal.aborted) return;
      const check = P.normalizeConnectionCheck(result.connection_check);
      if (!check || typeof result.verification_token !== "string" || !/^[A-Za-z0-9._~-]{1,4096}$/.test(result.verification_token)
        || result.verification !== (check.status === "authenticated" ? "verified" : "unverified")
        || (check.status === "authenticated" ? result.verified_at !== check.checked_at : result.verified_at != null)) {
        throw new Error("验证响应不完整，本次未获得可信结论。请检查本机服务版本后再显式验证。");
      }
      verificationReceipt = result.verification_token; editorConnectionCheck = check;
    } catch (error) {
      if (current()) { editorError(error.message); editorVerifiedAt = null; editorConnectionCheck = null; verificationReceipt = null; }
    } finally {
      if (verificationAttempt === attempt) { verificationAttempt = null; verificationBusy = false; renderVerification(); }
    }
  }
  async function saveEditor(event) {
    event.preventDefault(); if (profileMutation || verificationBusy) return;
    if (profileSync) { editorError("正在同步配置库，请稍后保存。"); return; }
    editorError(); const draft = editorDraft(), validation = P.validateProfile(draft, library, editingId);
    if (draft.credential_mode === "stored") {
      if (!profileServiceReady) validation.errors.api_key = "请先连接已更新的本机配置服务。";
      else if (!secretStorage.available) validation.errors.api_key = "当前系统无法加密保存密钥，请改用环境变量方式。";
      else if (!$("profile-api-key").value.trim() && !(editingProfile?.server_managed && editingProfile.has_secret && sameCredentialBinding())) validation.errors.api_key = "请填写 API Key；更改服务商或接口地址后需重新输入。";
      else if (!sameCredentialBinding() && editingProfile?.has_secret && !secretTyped) validation.errors.api_key = "服务商或接口地址已改变，请重新输入 API Key。";
    }
    showErrors(validation.errors);
    if (Object.keys(validation.errors).length) { $("profile-form").querySelector('[aria-invalid="true"]')?.focus(); return; }
    if (!profileServiceReady) { editorError("请先连接已更新的本机配置服务，再保存配置。"); return; }
    const previous = editingProfile, session = editorSession;
    const body = { profile: draft };
    if (previous) body.profile.id = previous.id;
    if (previous?.server_managed) body.expected_revision = previous.revision;
    if (draft.credential_mode === "stored" && secretTyped && $("profile-api-key").value.trim()) body.api_key = $("profile-api-key").value.trim();
    if (verificationReceipt) body.verification_token = verificationReceipt;
    clearSecret(); setProfileBusy(true);
    try {
      const data = await profileApi(previous?.server_managed ? `/api/profiles/${encodeURIComponent(previous.id)}` : "/api/profiles", body);
      const saved = serverProfile(data.profile), next = P.sanitize(library);
      next.profiles = next.profiles.filter((item) => item.id !== saved.id && item.id !== previous?.id);
      next.profiles.push(saved);
      for (const kind of KINDS) if (previous && next.selected[kind] === previous.id) next.selected[kind] = saved.kind === kind ? saved.id : null;
      cacheServerLibrary(next);
      if (session === editorSession) showManagement();
      toast("配置已保存到本机服务，可从输入框左下方选择。");
    } catch (error) {
      if (session === editorSession) editorError(`${error.message} 为保护密钥，本页输入已清除；请确认保存状态后再填写。`);
      else toast("配置保存请求未确认，请刷新配置库检查结果。");
    } finally { setProfileBusy(false); }
  }
  async function removeProfile(profile) {
    if (profileMutation) return;
    if (profileSync) { toast("正在同步配置库，请稍后删除。"); return; }
    const selected = Object.values(library.selected).includes(profile.id);
    if (!await confirmAction(`删除配置“${profile.name}”？${profile.server_managed ? "本机服务中的配置和密钥也会删除。" : "这份浏览器配置尚未导入。"}${selected ? "当前选择会清空。" : ""}历史需求记录仍保留。`)) return;
    if (profileMutation) return;
    profileMutation = true;
    try {
      if (profile.server_managed) await profileApi(`/api/profiles/${encodeURIComponent(profile.id)}/delete`, { expected_revision: profile.revision });
      const next = P.deleteProfile(library, profile.id);
      if (profile.server_managed) cacheServerLibrary(next); else if (!commitLibrary(next)) return;
      toast("配置已删除。");
    } catch (error) { toast(error.message); }
    finally { profileMutation = false; }
  }
  function confirmAction(description) {
    const dialog = $("confirm-dialog"), previous = document.activeElement;
    $("confirm-description").textContent = description; dialog.returnValue = "cancel";
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => { if (previous?.isConnected) previous.focus(); resolve(dialog.returnValue === "confirm"); }, { once: true });
      dialog.showModal();
    });
  }
  function button(text, handler, className = "button button-quiet") {
    const b = document.createElement("button"); b.type = "button"; b.textContent = text; b.className = className;
    b.addEventListener("click", handler); return b;
  }
  function profileValid(profile) {
    const source = { profiles: library.profiles.filter((item) => Boolean(item.server_managed) === Boolean(profile.server_managed)) };
    return !profile.is_draft && P.validateProfile(profile, source, profile.id).valid;
  }
  function renderLibrary() {
    const shown = library.profiles.filter((p) => $("profile-filter").value === "all" || p.kind === $("profile-filter").value);
    const list = $("profile-list"); list.replaceChildren(); $("profile-list-empty").hidden = shown.length > 0;
    for (const profile of shown) {
      const item = document.createElement("article"); item.className = "profile-list-item";
      const info = document.createElement("div"); info.className = "profile-list-info";
      const title = document.createElement("strong"); title.textContent = profile.name;
      const detail = document.createElement("p"); detail.textContent = `${LABELS[profile.kind]} · ${profile.provider_id || "待填写服务"} · ${profile.model || "待填写模型"}`;
      const status = document.createElement("span"); status.className = "status-chip";
      status.textContent = `${library.selected[profile.kind] === profile.id ? "当前选用 · " : ""}${profile.server_managed ? (profile.has_secret ? "密钥已加密 · " : "已保存到本机 · ") : "浏览器配置待导入 · "}${profileValid(profile) ? verificationLabel(profile) : "待补全"}`;
      info.append(title, detail, status);
      const actions = document.createElement("div"); actions.className = "profile-list-actions";
      const edit = button("修改", () => beginEditor(profile.kind, profile)); edit.setAttribute("aria-label", `修改 ${profile.name}`);
      const remove = button("删除", () => removeProfile(profile));
      if (!profile.server_managed) {
        const migrate = button("导入到本机服务", () => beginEditor(profile.kind, profile));
        migrate.setAttribute("aria-label", `导入 ${profile.name} 到本机服务`); actions.append(migrate);
      }
      remove.setAttribute("aria-label", `删除 ${profile.name}`); actions.append(edit, remove); item.append(info, actions); list.append(item);
    }
    for (const kind of KINDS) renderPicker(kind);
    renderQuickReasoning();
  }
  function renderQuickReasoning() {
    const profile = P.selectedProfiles(library).brain, select = $("quick-reasoning");
    const capability = P.reasoningCapability(profile || {});
    const labels = { none: "关闭", low: "低", medium: "中", high: "高", xhigh: "极高", max: "最高" };
    select.replaceChildren(new Option("默认", ""));
    for (const effort of capability.efforts) select.add(new Option(labels[effort], effort));
    if (profile?.thinking_budget != null) { const option = new Option(`预算 ${profile.thinking_budget}`, "budget"); option.disabled = true; select.add(option); }
    select.value = profile?.thinking_budget != null ? "budget" : profile?.reasoning_effort || "";
    select.disabled = !profile?.server_managed || !profileServiceReady || !capability.efforts.length || quickReasoningBusy || profileMutation || Boolean(profileSync);
    select.title = !profile ? "请先选择大脑配置" : !profile.server_managed ? "请先将配置导入本机服务"
      : !capability.efforts.length ? "此型号尚未登记可调整的思考档位" : `保存到 ${profile.name}；默认表示跟随供应商`;
    $("quick-reasoning-status").textContent = quickReasoningBusy ? "正在保存思考强度，请稍候再发送。"
      : quickReasoningUncertain ? "配置状态尚未同步，请刷新配置库后再发送。" : select.title;
  }
  async function changeQuickReasoning() {
    const previous = P.selectedProfiles(library).brain, effort = $("quick-reasoning").value || null;
    if (quickReasoningBusy || profileMutation || profileSync || !profileServiceReady || !previous?.server_managed) { renderQuickReasoning(); return; }
    if (effort !== null && !P.reasoningCapability(previous).efforts.includes(effort)) { renderQuickReasoning(); return; }
    if (previous.reasoning_effort === effort && previous.thinking_budget == null) return;
    const profile = { ...previous, reasoning_effort: effort, thinking_budget: null };
    quickReasoningBusy = true; profileMutation = true; renderQuickReasoning();
    window.dispatchEvent(new CustomEvent("sceneloop:change"));
    try {
      const data = await profileApi(`/api/profiles/${encodeURIComponent(previous.id)}`, { profile, expected_revision: previous.revision });
      const saved = serverProfile(data.profile), next = P.sanitize(library);
      next.profiles = next.profiles.map(item => item.id === saved.id ? saved : item);
      cacheServerLibrary(next); quickReasoningUncertain = false;
      toast("思考强度已保存到当前大脑配置。");
    } catch (error) {
      profileMutation = false;
      quickReasoningUncertain = !(await refreshProfiles());
      toast(`${error.message} 未直接重试保存，请核对当前显示的思考设置。`);
    } finally {
      profileMutation = false; quickReasoningBusy = false; renderQuickReasoning();
      window.dispatchEvent(new CustomEvent("sceneloop:change"));
    }
  }
  function renderPicker(kind) {
    const menu = $(`picker-${kind}`); menu.replaceChildren();
    const selected = library.profiles.find((p) => p.id === library.selected[kind] && p.kind === kind);
    $(`selected-${kind}-label`).textContent = selected?.name || "未选择";
    $(`select-${kind}`).title = `${LABELS[kind]}：${selected?.name || "未选择配置"}`;
    const choose = (id) => {
      try { if (commitLibrary(P.selectProfile(library, kind, id))) closePickers(true); } catch (error) { toast(error.message); }
    };
    const clear = button("不选择配置", () => choose(null), "picker-option");
    clear.setAttribute("role", "menuitemradio"); clear.setAttribute("aria-checked", String(!selected)); menu.append(clear);
    for (const profile of library.profiles.filter((p) => p.kind === kind)) {
      const option = button("", () => choose(profile.id), "picker-option");
      option.setAttribute("role", "menuitemradio"); option.setAttribute("aria-checked", String(selected?.id === profile.id));
      const name = document.createElement("span"); name.textContent = profile.name;
      const desc = document.createElement("small"); desc.textContent = `${profile.model || "未填写模型"}${profileValid(profile) ? "" : " · 待补全"}`;
      option.append(name, desc); option.disabled = !profileValid(profile); menu.append(option);
    }
    const add = button(`＋ 添加${LABELS[kind]}配置`, () => { closePickers(); openSettings(); beginEditor(kind); }, "picker-option picker-action");
    add.setAttribute("role", "menuitem"); menu.append(add);
    const manage = button("配置管理", () => { closePickers(); openSettings(); $("profile-filter").value = kind; renderLibrary(); }, "picker-option picker-action");
    manage.setAttribute("role", "menuitem"); menu.append(manage);
    if (activePicker === kind) positionPicker();
  }
  function closePickers(returnFocus = false) {
    const previous = activePicker;
    $("select-brain").closest(".composer-area")?.classList.toggle("picker-open", false);
    for (const kind of KINDS) { $(`picker-${kind}`).hidden = true; $(`select-${kind}`).setAttribute("aria-expanded", "false"); }
    activePicker = null; if (returnFocus && previous) $(`select-${previous}`).focus();
  }
  function positionPicker() {
    if (!activePicker) return;
    const trigger = $(`select-${activePicker}`), menu = $(`picker-${activePicker}`);
    const rect = trigger.getBoundingClientRect(), viewport = window.visualViewport;
    const left = viewport?.offsetLeft || 0, top = viewport?.offsetTop || 0;
    const width = viewport?.width || window.innerWidth, height = viewport?.height || window.innerHeight;
    const edge = 12, gap = 8, bottom = top + height;
    const composer = trigger.closest(".composer-area").getBoundingClientRect();
    if (rect.bottom <= Math.max(top, composer.top) || rect.top >= Math.min(bottom, composer.bottom)) { closePickers(); return; }
    const above = Math.max(0, rect.top - top - edge - gap);
    const below = Math.max(0, bottom - rect.bottom - edge - gap);
    const opensUp = above >= Math.min(160, below);
    menu.style.width = `${Math.max(0, Math.min(280, width - edge * 2))}px`;
    menu.style.maxHeight = `${Math.min(340, opensUp ? above : below)}px`;
    menu.style.left = `${Math.max(left + edge, Math.min(rect.left, left + width - menu.offsetWidth - edge))}px`;
    // Measure after constraining height; fixed positioning escapes composer scrolling.
    menu.style.top = `${opensUp ? rect.top - gap - menu.offsetHeight : rect.bottom + gap}px`;
  }
  function focusPickerOption(option) {
    if (!option || !activePicker) return;
    option.focus({ preventScroll: true });
    const menu = $(`picker-${activePicker}`), bounds = menu.getBoundingClientRect(), rect = option.getBoundingClientRect();
    if (rect.top < bounds.top + 6) menu.scrollTop -= bounds.top + 6 - rect.top;
    else if (rect.bottom > bounds.bottom - 6) menu.scrollTop += rect.bottom - bounds.bottom + 6;
  }
  function togglePicker(kind) {
    const wasOpen = activePicker === kind; closePickers(); if (wasOpen) return;
    $(`select-${kind}`).closest(".composer-area").classList.toggle("picker-open", true);
    activePicker = kind; $(`picker-${kind}`).hidden = false; $(`select-${kind}`).setAttribute("aria-expanded", "true");
    const menu = $(`picker-${kind}`);
    positionPicker();
    focusPickerOption(menu.querySelector('[aria-checked="true"]:not(:disabled)') || menu.querySelector('button:not(:disabled)'));
  }
  function editorTab(editing) {
    for (const [id, active] of [["add-profile", editing], ["manage-profiles", !editing]]) { $(id).classList.toggle("is-active", active); $(id).setAttribute("aria-pressed", String(active)); }
  }
  function openSettings() {
    settingsReturnFocus = document.activeElement; closePickers(); showManagement();
    if (!$("settings-dialog").open) $("settings-dialog").showModal();
    refreshProfiles();
  }
  function showManagement() {
    if ($("profile-edit-dialog").open) $("profile-edit-dialog").close();
    resetEditor(); $("profile-management").hidden = false; $("profile-editor").hidden = true; editorTab(false); renderLibrary();
  }
  function presetKey(preset) { return preset.preset_id || `${preset.provider_id}::${preset.model_id}`; }
  function populateEditorPresets(saved = $("profile-preset").value) {
    const select = $("profile-preset");
    select.replaceChildren(new Option("自定义配置", ""));
    for (const p of presets[$("profile-kind").value] || []) select.add(new Option(`${p.provider_label} / ${p.label || p.model_id}`, presetKey(p)));
    if (saved && !(presets[$("profile-kind").value] || []).some(p => presetKey(p) === saved)) select.add(new Option("已保存的自定义预设", saved));
    select.value = saved;
  }
  function beginEditor(kind = "brain", profile = null) {
    editorReturnFocus = document.activeElement;
    resetEditor(); editingProfile = profile ? P.sanitize(profile) : null; editingId = profile?.id || null; $("profile-id").value = editingId || "";
    $("profile-management").hidden = !profile; $("profile-editor").hidden = false; editorTab(!profile);
    $(profile ? "profile-edit-host" : "profile-editor-home").append($("profile-editor"));
    $("close-profile-editor").hidden = !profile;
    $("profile-editor-title").textContent = profile ? (profile.server_managed ? "修改配置" : "导入浏览器配置") : "添加配置";
    for (const [key, suffix] of Object.entries(FIELDS)) if (key !== "preset") $(`profile-${suffix}`).value = profile?.[key] ?? (key === "kind" ? kind : key === "mode" ? "cloud_api" : "");
    for (const [key, suffix] of Object.entries(NUMBER_FIELDS)) $(`profile-${suffix}`).value = profile?.[key] == null ? "" : String(profile[key]);
    $("profile-credential-mode").value = profile ? P.credentialMode(profile) : "stored";
    editorError(); $("profile-kind").disabled = Boolean(profile); populateEditorPresets(profile?.preset || "");
    showErrors({}); renderEditorMode();
    renderReasoning(profile?.thinking_budget != null ? "budget" : profile?.reasoning_effort || "");
    $("profile-thinking-budget").value = profile?.thinking_budget == null ? "" : String(profile.thinking_budget);
    editorVerifiedAt = profile?.verification === "verified" ? profile.verified_at : null;
    editorConnectionCheck = P.normalizeConnectionCheck(profile?.connection_check);
    renderVerification(); renderPresetInfo();
    if (profile && !$("profile-edit-dialog").open) $("profile-edit-dialog").showModal();
    $("profile-name").focus();
  }
  function renderEditorMode() {
    const is3D = $("profile-kind").value === "asset3d", process = is3D && $("profile-mode").value === "local_process";
    $("profile-brain-fields").hidden = $("profile-kind").value !== "brain";
    renderReasoning(); renderVerification();
    $("profile-mode-field").hidden = !is3D; $("profile-http-fields").hidden = process; $("profile-process-fields").hidden = !process;
    $("profile-repo-dir-field").hidden = !process || $("profile-provider").value.trim() !== "hunyuan"; renderCredentials();
  }
  function renderReasoning(requested) {
    const capability = P.reasoningCapability({ provider_id: $("profile-provider").value, model: $("profile-model").value });
    const select = $("profile-reasoning-effort"), previous = requested ?? select.value;
    const labels = { none: "关闭思考", low: "低 · low", medium: "中 · medium", high: "高 · high", xhigh: "极高 · xhigh", max: "最高 · max" };
    select.replaceChildren(new Option("跟随供应商", ""));
    for (const effort of capability.efforts) select.add(new Option(labels[effort], effort));
    if (capability.budget_max !== null) select.add(new Option("自定义思考预算…", "budget"));
    select.value = capability.efforts.includes(previous) || (previous === "budget" && capability.budget_max !== null) ? previous : "";
    select.disabled = !capability.efforts.length;
    const budgetMode = select.value === "budget";
    $("profile-thinking-budget-field").hidden = !budgetMode;
    $("profile-thinking-budget").disabled = !budgetMode;
    if (!budgetMode) $("profile-thinking-budget").value = "";
    $("profile-reasoning-help").textContent = capability.efforts.length
      ? "跟随供应商不发送思考参数；强度与自定义预算互斥。更高强度可能增加耗时和费用。"
      : "此服务商与型号尚未登记思考参数，使用供应商默认设置。";
  }
  function editorDraft() {
    const draft = {}; for (const [key, suffix] of Object.entries(FIELDS)) draft[key] = $(`profile-${suffix}`).value.trim();
    for (const [key, suffix] of Object.entries(NUMBER_FIELDS)) {
      const input = $(`profile-${suffix}`), raw = input.value.trim();
      draft[key] = draft.kind !== "brain" ? null : input.validity?.badInput ? "invalid" : raw === "" ? null : /^\d+$/.test(raw) ? Number(raw) : raw;
    }
    const reasoning = $("profile-reasoning-effort").value;
    draft.reasoning_effort = draft.kind === "brain" && reasoning && reasoning !== "budget" ? reasoning : null;
    if (draft.kind !== "brain" || reasoning !== "budget") draft.thinking_budget = null;
    else if (draft.thinking_budget === null) draft.thinking_budget = "";
    if (draft.kind !== "asset3d") draft.mode = "cloud_api";
    if (draft.kind === "asset3d" && draft.mode === "local_process") { draft.base_url = ""; draft.credential_env = ""; draft.credential_mode = "none"; }
    else { draft.python_path = ""; draft.model_dir = ""; }
    if (draft.kind !== "asset3d" || draft.provider_id !== "hunyuan" || draft.mode !== "local_process") delete draft.repo_dir;
    if (draft.credential_mode !== "env") draft.credential_env = "";
    return draft;
  }
  function showErrors(errors) {
    for (const [key, suffix] of Object.entries({ ...FIELDS, ...NUMBER_FIELDS, reasoning_effort: "reasoning-effort", api_key: "api-key" })) {
      $(`profile-${suffix}`).setAttribute("aria-invalid", errors[key] ? "true" : "false");
      const error = $(`profile-${suffix}-error`); if (error) { error.textContent = errors[key] || ""; error.hidden = !errors[key]; }
    }
  }
  function renderPresetInfo() {
    const box = $("profile-preset-info"), draft = editorDraft(); box.replaceChildren();
    const preset = (presets[draft.kind] || []).find((p) => presetKey(p) === draft.preset);
    const matches = preset && preset.provider_id === draft.provider_id && preset.model_id === draft.model && preset.base_url === draft.base_url && (!preset.mode || preset.mode === draft.mode);
    if (preset?.preset_id && !matches) $("profile-preset").value = "";
    box.hidden = !matches; if (!matches) return;
    const text = document.createElement("p"); text.textContent = draft.provider_id === "hunyuan" ? "本地 Hunyuan2mini 仅支持图片生成主体 shape。填写解释器、源码仓库与权重路径；保存或路径检测不代表推理成功。本地生成需在聊天中明确确认额度与停止条件。" : "预设来自公开文档；连接验证不证明模型能力或额度。" + (draft.kind === "asset3d" ? "可在执行设置中开放单资产请求；云端生成仍需逐次批准。" : ""); box.append(text);
    if (preset.documentation_url?.startsWith("https://")) {
      const link = document.createElement("a"); link.href = preset.documentation_url; link.target = "_blank"; link.rel = "noopener noreferrer"; link.textContent = "服务商文档 ↗"; box.append(link);
    }
  }
  function parseBudget() {
    const raw = workspace.budget.amount.trim(); if (!raw) return { value: null, error: "" };
    const value = Number(raw);
    if (!/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(raw) || !Number.isFinite(value) || value <= 0 || value > 100000) return { value: null, error: "金额必须大于 0，且不超过 100,000。" };
    return { value, error: "" };
  }
  function renderBudget() {
    const { value, error } = parseBudget();
    $("budget-display").textContent = error ? "待修正" : value === null ? "未设置" : `${value.toLocaleString("zh-CN", { maximumSignificantDigits: 15 })} ${workspace.budget.currency}`;
    $("budget-error").textContent = error; $("budget-error").hidden = !error; $("budget-amount").setAttribute("aria-invalid", String(Boolean(error))); $("clear-budget").disabled = !workspace.budget.amount;
  }
  function makeConfig() {
    return {
      schema_version: "0.2", iteration: { ...workspace.iteration }, judge: judgeRequest(), model_profiles: P.sanitize(library.profiles), selected_profile_ids: { ...library.selected }, selected_profiles: P.selectedProfiles(library),
      budget: { currency: workspace.budget.currency, limit: parseBudget().value, max_image_generations: Number(workspace.budget.max_image_generations), max_asset3d_generations: Number(workspace.budget.max_asset3d_generations), max_retries: Number(workspace.budget.max_retries) },
      quality: { key_objects_recognizable: currentConstraints().key_objects_recognizable },
      project_constraints: { design_intent_and_style: currentConstraints().style, preserve: currentConstraints().preserve, evaluation: { reference_only_when_explicit: true, otherwise: "before_after", preserve_composition_style_function: true, low_confidence: "ask_user" } }, execution_status: "requires_runtime_validation",
    };
  }
  function selectedJudgeProfile() {
    if (workspace.judge.profile_id === "follow_brain") return P.selectedProfiles(library).brain;
    return library.profiles.find((profile) => profile.kind === "brain" && profile.id === workspace.judge.profile_id) || null;
  }
  function judgeRequest() {
    const profile = selectedJudgeProfile();
    return {
      enabled: true,
      mode: workspace.judge.profile_id === "follow_brain" ? "follow_brain" : "profile",
      profile: workspace.judge.profile_id === "follow_brain" ? null : profile,
      reasoning_effort: workspace.judge.reasoning_effort || null,
      trigger_rounds: workspace.judge.trigger_rounds,
    };
  }
  function renderJudge() {
    const picker = $("judge-profile"), chosen = workspace.judge.profile_id;
    picker.replaceChildren();
    const follow = document.createElement("option"); follow.value = "follow_brain"; follow.textContent = "跟随当前大脑（独立请求）"; picker.append(follow);
    for (const profile of library.profiles.filter((item) => item.kind === "brain" && profileValid(item))) {
      const option = document.createElement("option"); option.value = profile.id; option.textContent = profile.name; picker.append(option);
    }
    const pickerOptions = Array.from(picker.options || picker.children || []);
    if (!pickerOptions.some((option) => option.value === chosen)) workspace.judge.profile_id = "follow_brain";
    picker.value = workspace.judge.profile_id;
    $("judge-trigger-rounds").value = String(workspace.judge.trigger_rounds);
    const profile = selectedJudgeProfile(), supported = new Set(P.reasoningCapability(profile || {}).efforts || []);
    const reasoning = $("judge-reasoning");
    for (const option of Array.from(reasoning.options || reasoning.children || [])) option.disabled = Boolean(option.value) && !supported.has(option.value);
    if (workspace.judge.reasoning_effort && !supported.has(workspace.judge.reasoning_effort)) workspace.judge.reasoning_effort = "";
    reasoning.value = workspace.judge.reasoning_effort;
    for (const node of [picker, reasoning, $("judge-trigger-rounds")]) node.disabled = false;
  }
  function renderConfig() {
    renderJudge();
    const missing = [], invalid = [], selected = P.selectedProfiles(library);
    for (const kind of KINDS) {
      if (!selected[kind] && kind !== "asset3d") missing.push(`${LABELS[kind]}：尚未选择已保存配置。`);
      else if (selected[kind] && !profileValid(selected[kind])) missing.push(`${LABELS[kind]}：所选配置待补全，请在配置管理中修改。`);
    }
    const budget = parseBudget(); if (budget.error) invalid.push(budget.error);
    for (const [id, key, min, max] of LIMITS) {
      const raw = workspace.budget[key], value = Number(raw), error = !/^\d+$/.test(raw) || value < min || value > max ? `请输入 ${min}–${max} 之间的整数。` : "";
      $(`${id}-error`).textContent = error; $(`${id}-error`).hidden = !error; $(id).setAttribute("aria-invalid", String(Boolean(error))); if (error) invalid.push(error);
    }
    if (!currentConstraints().key_objects_recognizable) missing.push("关键物体可辨识要求未启用，与首轮验收要求不一致。");
    missing.push("配置需由本地后端在运行时验证；本轮按调用次数限制执行，货币上限暂不能核算。3D 生成配置本轮不调用。"); $("missing-list").replaceChildren();
    for (const text of [...invalid, ...missing]) { const li = document.createElement("li"); li.textContent = text; $("missing-list").append(li); }
    $("config-preview").textContent = JSON.stringify(makeConfig(), null, 2);
    $("validation-summary").textContent = invalid.length ? `有 ${invalid.length} 项任务配置需要修正。` : `配置库有 ${library.profiles.length} 套配置，当前选用 ${KINDS.filter((k) => selected[k]).length} 类。保存不代表服务已验证。`;
    $("validation-summary").classList.toggle("is-error", invalid.length > 0); $("export-button").disabled = invalid.length > 0;
  }
  function renderIteration() {
    const { enabled, max_rounds: rounds } = workspace.iteration;
    const editing = Boolean(activeProjectContext?.current_scene), effectiveRounds = enabled ? rounds : 1;
    $("iteration-enabled").checked = enabled;
    $("iteration-rounds").value = String(rounds); $("iteration-rounds").disabled = !enabled;
    $("iteration-rounds-output").textContent = `${rounds} 轮`;
    $("iteration-rounds").setAttribute("aria-valuetext", `${rounds} 轮修改`);
    $("iteration-limits").textContent = editing ? "已有场景采用局部草稿轮；按拟正式保存目标冻结资源，达到上限不代表质量通过。" : enabled
      ? `本次上限：生图 ${editing ? 0 : 1} 次，大脑 ${3 * effectiveRounds + 3} 次${editing ? "" : "（含初始建场）"}，截图 ${2 * (effectiveRounds + 1) + 4} 张，工具调用 ${2 * (3 * effectiveRounds + 3)} 次。最多 ${effectiveRounds} 轮修改；累计活动时间仅统计。`
      : "本次上限：生图 1 次，采用后大脑规划 1 次，Blender 主辅截图 2 张；不自动迭代。";
    $("iteration-limits-note").textContent = "3D 生成 0 次，自动收费重试 0 次。模型和Blender调用各有单次等待设置；达到次数上限不代表质量通过。";
    $("new-scene-limits").textContent = enabled
      ? `另建上限：生图 1 次，大脑 ${3 * rounds + 3} 次（含初始建场），截图 ${2 * (rounds + 1) + 4} 张，工具调用 ${2 * (3 * rounds + 3)} 次；最多 ${rounds} 轮修改，累计活动时间仅统计。3D 生成 0 次，自动收费重试 0 次。`
      : "另建上限：生图 1 次，采用后大脑规划 1 次，主辅截图 2 张，模型工具调用 0 次（固定建场流程）；不自动迭代。3D 生成 0 次，自动收费重试 0 次。";

  }
  function changedWorkspace() { renderIteration(); renderBudget(); renderConfig(); saveWorkspace(); window.dispatchEvent(new CustomEvent("sceneloop:change")); }
  function fillWorkspace() {
    renderIteration();
    renderJudge();
    $("budget-amount").value = workspace.budget.amount; $("budget-currency").value = workspace.budget.currency;
    for (const [id, key] of LIMITS) $(id).value = workspace.budget[key];
    $("project-style").value = currentConstraints().style; $("project-preserve").value = currentConstraints().preserve; $("key-objects").checked = currentConstraints().key_objects_recognizable;
    $("task-input").value = workspace.chat.draft; $("record-message").disabled = !workspace.chat.draft.trim();
  }
  function renderMessages(scroll = false) {
    const list = $("legacy-message-list"); list.replaceChildren();
    for (const entry of workspace.chat.messages) {
      const article = document.createElement("article"); article.className = "message";
      const heading = document.createElement("div"); heading.className = "message-heading"; heading.textContent = "你";
      const text = document.createElement("div"); text.className = "message-content"; text.textContent = entry.text;
      const status = document.createElement("p"); status.className = "message-status"; status.textContent = "本地记录 · 尚未发送给模型";
      if (entry.model_configs) status.textContent += ` · ${KINDS.map((k) => `${LABELS[k]}：${entry.model_configs[k]?.name || "未选择"}`).join(" / ")}`;
      article.append(heading, text, status); list.append(article);
    }
    $("legacy-notes").hidden = !workspace.chat.messages.length;
    $("clear-messages").disabled = !workspace.chat.messages.length; if (scroll) $("message-scroll").scrollTop = $("message-scroll").scrollHeight;
  }
  function currentConstraints() {
    if (!activeProjectId) return workspace.constraints;
    if (Object.hasOwn(workspace.project_constraints || {}, activeProjectId)) return workspace.project_constraints[activeProjectId];
    const source = activeProjectContext?.design_context?.constraints || {};
    return { style: typeof source.style === "string" ? source.style : "", preserve: typeof source.preserve === "string" ? source.preserve : "", key_objects_recognizable: typeof source.key_objects_recognizable === "boolean" ? source.key_objects_recognizable : true };
  }
  function editableConstraints() {
    if (!activeProjectId) return workspace.constraints;
    workspace.project_constraints ||= {};
    if (!Object.hasOwn(workspace.project_constraints, activeProjectId)) workspace.project_constraints[activeProjectId] = { ...currentConstraints() };
    return workspace.project_constraints[activeProjectId];
  }
  function persistDraft(value) {
    if (activeProjectId) { workspace.project_drafts ||= {}; workspace.project_drafts[activeProjectId] = value; }
    else workspace.chat.draft = value;
    saveWorkspace();
  }
  function setProjectContext(project) {
    if ((project?.id || null) !== activeProjectId) {
      renderPreview();
      persistDraft($("task-input").value);
      activeProjectId = project?.id || null;
      $("task-input").value = activeProjectId ? workspace.project_drafts?.[activeProjectId] || "" : workspace.chat.draft;
      $("record-message").disabled = !$("task-input").value.trim();
    }
    activeProjectContext = project;
    const constraints = currentConstraints();
    $("project-style").value = constraints.style; $("project-preserve").value = constraints.preserve; $("key-objects").checked = constraints.key_objects_recognizable;
    renderIteration(); renderConfig();
    window.dispatchEvent(new CustomEvent("sceneloop:change"));
  }
  function clearSubmittedDraft(projectId, prompt) {
    const stored = projectId ? workspace.project_drafts?.[projectId] : workspace.chat.draft;
    if (stored?.trim() === prompt) {
      if (projectId) workspace.project_drafts[projectId] = ""; else workspace.chat.draft = "";
    }
    if (activeProjectId === projectId && $("task-input").value.trim() === prompt) {
      $("task-input").value = ""; $("record-message").disabled = true;
    }
    saveWorkspace(); window.dispatchEvent(new CustomEvent("sceneloop:change"));
  }
  function renderPreview() {
    for (const b of document.querySelectorAll("[data-preview-kind]")) { const active = b.dataset.previewKind === previewKind; b.classList.toggle("is-active", active); b.setAttribute("aria-pressed", String(active)); }
    $("preview-empty-text").textContent = { render: "当前任务暂无可预览图片。", candidates: "暂无候选版本。" }[previewKind];
    window.dispatchEvent(new CustomEvent("sceneloop:preview"));
  }
  function showSidePanel(preview) {
    $("constraints-pane").hidden = preview; $("preview-pane").hidden = !preview;
    $("toggle-preview").setAttribute("aria-expanded", String(preview)); $("toggle-preview").classList.toggle("is-active", preview);
    for (const [id, active] of [["show-preview", preview], ["show-constraints", !preview]]) { $(id).setAttribute("aria-pressed", String(active)); $(id).classList.toggle("is-active", active); } renderPreview();
  }

  load(); fillWorkspace(); renderLibrary(); renderBudget(); renderConfig(); renderMessages(true);
  $("save-status").textContent = "配置由本机服务保存，选择与任务草案保留在此浏览器";
  $("open-settings").addEventListener("click", openSettings);
  for (const id of ["close-settings", "done-settings"]) $(id).addEventListener("click", () => $("settings-dialog").close());
  $("settings-dialog").addEventListener("close", () => { resetEditor(); (settingsReturnFocus?.isConnected ? settingsReturnFocus : $("open-settings")).focus(); });
  $("settings-dialog").addEventListener("cancel", (event) => { if (consumeProviderHelpCancel(event)) return; clearSecret(); });
  $("refresh-profiles").addEventListener("click", refreshProfiles);
  $("toggle-profile-secret").addEventListener("click", toggleSecret);
  $("profile-api-key").addEventListener("input", () => { secretGeneration++; secretTyped = true; secretRevealed = false; $("toggle-profile-secret").disabled = false; });
  $("profile-credential-mode").addEventListener("change", () => { clearSecret(); showErrors({}); renderCredentials(); });
  $("add-profile").addEventListener("click", () => beginEditor()); $("manage-profiles").addEventListener("click", showManagement); $("cancel-profile").addEventListener("click", showManagement);
  $("profile-filter").addEventListener("change", renderLibrary);
  $("quick-reasoning").addEventListener("change", changeQuickReasoning);
  $("profile-kind").addEventListener("change", () => {
    clearSecret(); $("profile-credential-mode").value = "stored";
    for (const key of ["provider", "model", "base-url", "credential", "python", "model-dir", "repo-dir", "reasoning-effort", ...Object.values(NUMBER_FIELDS)]) $(`profile-${key}`).value = "";
    $("profile-mode").value = "cloud_api"; populateEditorPresets(""); showErrors({}); renderEditorMode(); renderPresetInfo();
  });
  $("profile-mode").addEventListener("change", () => { clearSecret(); showErrors({}); renderEditorMode(); renderPresetInfo(); });
  $("profile-preset").addEventListener("change", () => {
    clearSecret();
    const options = presets[$("profile-kind").value] || [], p = options.find((v) => presetKey(v) === $("profile-preset").value);
    const previous = options.find((v) => v.provider_id === $("profile-provider").value && v.model_id === $("profile-model").value && v.base_url === $("profile-base-url").value);
    if (p) {
      if (!$("profile-name").value.trim() || p.preset_id && previous?.preset_id && $("profile-name").value === (previous.label || previous.model_id)) $("profile-name").value = p.label || p.model_id;
      $("profile-provider").value = p.provider_id; $("profile-model").value = p.model_id; $("profile-base-url").value = p.base_url;
      if (p.mode) $("profile-mode").value = p.mode;
      if (p.mode === "cloud_api" && $("profile-credential-mode").value === "none") $("profile-credential-mode").value = "stored";
    }
    renderEditorMode(); renderPresetInfo();
  });
  for (const id of ["profile-provider", "profile-model", "profile-base-url"]) $(id).addEventListener("input", () => {
    if (secretRevealed && !secretTyped) clearSecret(); renderReasoning(); renderEditorMode(); renderPresetInfo(); renderCredentials();
  });
  $("profile-reasoning-effort").addEventListener("change", () => { renderReasoning(); showErrors({}); });
  $("verify-profile").addEventListener("click", verifyEditor);
  $("profile-form").addEventListener("input", invalidateVerification);
  $("profile-form").addEventListener("change", invalidateVerification);
  $("close-profile-editor").addEventListener("click", () => $("profile-edit-dialog").close());
  bindProviderHelp();
  $("profile-edit-dialog").addEventListener("close", () => { showManagement(); if (editorReturnFocus?.isConnected) editorReturnFocus.focus(); });
  $("profile-edit-dialog").addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (consumeProviderHelpEscape(event)) return;
      event.preventDefault(); event.stopPropagation(); clearSecret(); $("profile-edit-dialog").close();
    }
  });
  $("profile-edit-dialog").addEventListener("cancel", (event) => { if (consumeProviderHelpCancel(event)) return; event.preventDefault(); event.stopPropagation(); clearSecret(); $("profile-edit-dialog").close(); });
  $("profile-form").addEventListener("submit", saveEditor);
  for (const kind of KINDS) {
    $(`select-${kind}`).addEventListener("click", () => togglePicker(kind));
    $(`select-${kind}`).addEventListener("keydown", (e) => { if (["ArrowUp", "ArrowDown"].includes(e.key)) { e.preventDefault(); if (activePicker !== kind) togglePicker(kind); } });
    $(`picker-${kind}`).addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closePickers(true); return; }
      const buttons = [...$(`picker-${kind}`).querySelectorAll("button:not(:disabled)")]; let index = buttons.indexOf(document.activeElement);
      if (e.key === "ArrowDown") index = (index + 1) % buttons.length; else if (e.key === "ArrowUp") index = (index - 1 + buttons.length) % buttons.length;
      else if (e.key === "Home") index = 0; else if (e.key === "End") index = buttons.length - 1; else return;
      e.preventDefault(); focusPickerOption(buttons[index]);
    });
  }
  document.addEventListener("click", (e) => { if (!e.target.closest(".profile-picker")) closePickers(); });
  window.addEventListener("resize", positionPicker);
  document.addEventListener("scroll", (event) => {
    if (activePicker && !$(`picker-${activePicker}`).contains(event.target)) positionPicker();
  }, true);
  window.visualViewport?.addEventListener("resize", positionPicker);
  window.visualViewport?.addEventListener("scroll", positionPicker);
  if (window.ResizeObserver) {
    const pickerLayout = new ResizeObserver(positionPicker);
    pickerLayout.observe($("message-form").parentElement);
    pickerLayout.observe($("message-form"));
  }

  $("iteration-enabled").addEventListener("change", (e) => { workspace.iteration.enabled = e.target.checked; changedWorkspace(); });
  $("iteration-rounds").addEventListener("input", (e) => {
    const rounds = Number(e.target.value);
    if (!Number.isInteger(rounds) || rounds < 1 || rounds > 6) return;
    workspace.iteration.max_rounds = rounds; changedWorkspace();
  });
  $("budget-amount").addEventListener("input", (e) => { workspace.budget.amount = e.target.value; changedWorkspace(); });
  $("budget-currency").addEventListener("change", (e) => { workspace.budget.currency = e.target.value; changedWorkspace(); });
  $("clear-budget").addEventListener("click", () => { workspace.budget.amount = ""; $("budget-amount").value = ""; changedWorkspace(); });
  for (const [id, key] of LIMITS) $(id).addEventListener("input", (e) => { workspace.budget[key] = e.target.value; changedWorkspace(); });
  for (const [id, key] of [["project-style", "style"], ["project-preserve", "preserve"]]) $(id).addEventListener("input", (e) => { editableConstraints()[key] = e.target.value; changedWorkspace(); });
  $("key-objects").addEventListener("change", (e) => { editableConstraints().key_objects_recognizable = e.target.checked; changedWorkspace(); });
  $("judge-profile").addEventListener("change", (e) => { workspace.judge.profile_id = e.target.value; workspace.judge.reasoning_effort = ""; changedWorkspace(); });
  $("judge-reasoning").addEventListener("change", (e) => { workspace.judge.reasoning_effort = e.target.value; changedWorkspace(); });
  $("judge-trigger-rounds").addEventListener("change", (e) => { const rounds = Number(e.target.value); if (!Number.isInteger(rounds) || rounds < 1 || rounds > 3) { renderJudge(); return; } workspace.judge.trigger_rounds = rounds; changedWorkspace(); });
  $("task-input").addEventListener("input", (e) => { persistDraft(e.target.value); $("record-message").disabled = !e.target.value.trim(); });
  window.SceneLoopConversation?.bindInput($("task-input"),()=>$("message-form").requestSubmit());
  $("record-message").addEventListener("click", (e) => {
    e.preventDefault(); const text = $("task-input").value.trim(); if (!text) return;
    workspace.chat.messages.push({ text, created_at: new Date().toISOString(), model_configs: JSON.parse(JSON.stringify(P.selectedProfiles(library))) });
    persistDraft(""); $("task-input").value = ""; $("record-message").disabled = true; renderMessages(true); window.dispatchEvent(new CustomEvent("sceneloop:change"));
    toast(saveWorkspace() ? "需求与本次模型选择已记录，尚未发送给模型。" : "需求已显示，但未能保存。");
  });
  $("clear-messages").addEventListener("click", async () => { if (await confirmAction("清除本地需求记录？配置库、项目约束和输入框草稿会保留。")) { workspace.chat.messages = []; renderMessages(); saveWorkspace(); } });
  $("toggle-preview").addEventListener("click", () => showSidePanel($("preview-pane").hidden));
  $("show-preview").addEventListener("click", () => showSidePanel(true)); $("show-constraints").addEventListener("click", () => showSidePanel(false));
  $("close-preview").addEventListener("click", () => { showSidePanel(false); $("toggle-preview").focus(); });
  for (const b of document.querySelectorAll("[data-preview-kind]")) b.addEventListener("click", () => { previewKind = b.dataset.previewKind; renderPreview(); });
  window.addEventListener("pagehide", () => { clearSecret(); });
  $("config-form").addEventListener("submit", (e) => e.preventDefault());
  $("export-button").addEventListener("click", () => {
    renderConfig(); if ($("export-button").disabled) return;
    const url = URL.createObjectURL(new Blob([`${JSON.stringify(makeConfig(), null, 2)}\n`], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = "sceneloop.config.draft.json"; document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000); toast("已请求下载配置库与任务选择草案。");
  });
  async function loadPresets() {
    try {
      const response = await fetch("./model-presets.json", { cache: "no-cache" }); if (!response.ok) throw new Error(); const data = await response.json();
      for (const kind of ["brain", "image"]) { if (!Array.isArray(data[kind])) throw new Error(); presets[kind] = data[kind]; }
      presets.asset3d = Array.isArray(data.asset3d) ? data.asset3d : [];
      populateEditorPresets(); renderPresetInfo();
    } catch { notice("官方预设暂未载入；已保存配置仍可使用，也可添加自定义配置。"); }
  }
  window.SceneLoopWorkbench = {
    assetProfileIssue: () => !profileServiceReady || profileSync || profileMutation ? "等待本机 3D 配置库读取或保存完成" : "",
    setProjectContext, clearSubmittedDraft,
    configurationIssue: (kind) => { if (kind === "brain" && (quickReasoningBusy || quickReasoningUncertain)) return quickReasoningBusy ? "等待大脑思考配置保存完成" : "请刷新配置库以确认大脑设置"; const profile = P.selectedProfiles(library)[kind]; return profile && !profileValid(profile) ? `${LABELS[kind]}配置待补全，请在设置中修改并保存` : ""; },
    snapshot: () => JSON.parse(JSON.stringify({ prompt: $("task-input").value.trim(), profiles: P.selectedProfiles(library), judge: judgeRequest(), constraints: currentConstraints(), iteration: workspace.iteration, budget: makeConfig().budget })),
    previewState: () => ({ kind: previewKind, open: !$("preview-pane").hidden }),
    openPreview: (kind = "render") => { previewKind = kind === "candidates" ? "candidates" : "render"; showSidePanel(true); },
  };
  window.dispatchEvent(new CustomEvent("sceneloop:ready"));
  loadPresets(); refreshProfiles();
})();
