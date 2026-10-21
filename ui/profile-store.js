/*
 * Pure profile data helpers. This module does not read or write browser storage.
 * Validation failures from saveProfile/selectProfile throw Error with an `errors`
 * field containing the same Chinese field messages as validateProfile().
 * Keep the legacy storage record until the caller has saved the migrated library.
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root && root.window === root) root.SceneLoopProfiles = api;
})(typeof globalThis === "object" ? globalThis : this, function () {
  "use strict";

  const KINDS = ["brain", "image", "asset3d"];
  const MODES = ["cloud_api", "local_http", "local_process"];
  const LABELS = { brain: "场景大脑", image: "图像生成", asset3d: "3D 生成" };
  const TEXT_FIELDS = ["name", "kind", "preset", "provider_id", "model", "base_url", "credential_env", "mode", "python_path", "model_dir"];
  const NUMBER_FIELDS = {
    max_output_tokens: { min: 1, max: 1048576, label: "单次输出上限" },
    request_timeout_seconds: { min: 30, max: 900, label: "单次响应等待秒数" },
  };
  const OPTIONAL_FIELDS = [...Object.keys(NUMBER_FIELDS), "reasoning_effort", "thinking_budget"];
  const REASONING_CAPABILITIES = {
    "deepseek::deepseek-flash": { efforts: ["none", "low", "high", "max"], budget_max: null },
    "zai::glm-5.3-flash": { efforts: ["low", "high", "max"], budget_max: null },
    "dashscope::qwen3.8-flash": { efforts: ["none", "low", "medium", "xhigh"], budget_max: 262144 },
  };
  function reasoningCapability(profile) {
    const key = `${String(profile.provider_id || "").trim().toLowerCase()}::${String(profile.model || "").trim().toLowerCase()}`;
    return REASONING_CAPABILITIES[key] || { efforts: [], budget_max: null };
  }
  const SECRET_FIELD = /^(?:api[_-]?key|secret|password|token|verification_token|access[_-]?token|authorization|encrypted[_-]?(?:key|secret)|protected_key|_resolved_credential|ciphertext)$/i;
  const CHECK_STATUSES = ["authenticated", "reachable", "failed", "inconclusive"];
  const CHECK_CODES = ["readonly_authenticated", "network_reachable", "local_paths_present", "unsupported_readonly_check",
    "authentication_rejected", "access_denied", "rate_limited", "service_error", "dns_failed", "tls_failed",
    "connection_failed", "unsafe_target", "check_timeout", "invalid_check_response", "local_path_missing", "missing_credential"];
  let idCounter = 0;

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function validCheckTime(value) {
    return typeof value === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(value)
      && Number.isFinite(Date.parse(value)) && new Date(value).toISOString().slice(0, 19) === value.slice(0, 19);
  }

  // Copy a bounded public conclusion, never supplier payloads or arbitrary copy.
  function normalizeConnectionCheck(value) {
    if (!isObject(value) || !CHECK_STATUSES.includes(value.status) || !CHECK_CODES.includes(value.code)
      || !validCheckTime(value.checked_at)) return null;
    const authenticated = value.code === "readonly_authenticated", reachable = ["network_reachable", "local_paths_present"].includes(value.code);
    if ((value.status === "authenticated") !== authenticated || (value.status === "reachable") !== reachable) return null;
    if (value.http_status != null && (!Number.isInteger(value.http_status) || value.http_status < 100 || value.http_status > 599)) return null;
    if (authenticated && value.http_status != null && value.http_status !== 200) return null;
    return { status: value.status, checked_at: value.checked_at, code: value.code, http_status: value.http_status ?? null };
  }

  function fail(message, errors) {
    const error = new Error(message);
    if (errors) error.errors = errors;
    throw error;
  }

  // Reject values JSON persistence would silently discard or change.
  function cloneJson(value, ancestors = new Set()) {
    if (value === null || typeof value === "string" || typeof value === "boolean") return value;
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (!isObject(value) && !Array.isArray(value)) fail("配置包含无法保存的数据类型。");
    if (ancestors.has(value)) fail("配置包含循环引用。");
    const prototype = Object.getPrototypeOf(value);
    if (!Array.isArray(value) && prototype !== Object.prototype && prototype !== null) fail("配置必须是普通 JSON 对象。");
    ancestors.add(value);
    const copy = Array.isArray(value)
      ? Array.from(value, (item) => cloneJson(item, ancestors))
      : Object.fromEntries(Object.entries(value).filter(([key]) => !SECRET_FIELD.test(key)).map(([key, item]) =>
        [key, key === "connection_check" ? normalizeConnectionCheck(item) : cloneJson(item, ancestors)]));
    ancestors.delete(value);
    return copy;
  }

  function emptyLibrary() {
    return { schema_version: 2, profiles: [], selected: { brain: null, image: null, asset3d: null } };
  }

  function safeCredential(value) {
    return /^[A-Za-z_][A-Za-z0-9_]*$/.test(value) && !/^(?:sk[-_]|AIza|eyJ|gh[pousr]_|xox[baprs]-)/.test(value);
  }

  function safeUrl(value) {
    try {
      const url = new URL(value);
      return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password && !/[?#]/.test(value);
    } catch { return false; }
  }

  function credentialMode(profile) {
    return profile.credential_mode || (profile.credential_env ? "env" : "none");
  }

  function nameKey(value) { return value.trim().toLowerCase(); }

  function validateProfile(profile, library, excludeId) {
    const errors = {};
    if (!isObject(profile)) return { errors: { profile: "配置必须是对象。" }, valid: false };
    const fields = {};
    for (const field of TEXT_FIELDS) {
      const value = profile[field];
      if (value !== undefined && typeof value !== "string") errors[field] = "请输入文本。";
      fields[field] = typeof value === "string" ? value.trim() : "";
    }
    for (const [field, label] of [["name", "配置名称"], ["provider_id", "服务商标识"], ["model", "模型名称"]]) {
      if (!fields[field]) errors[field] = `请填写${label}。`;
    }
    if (!KINDS.includes(fields.kind)) errors.kind = "请选择大脑、图像生成或 3D 生成。";
    for (const [field, rule] of Object.entries(NUMBER_FIELDS)) {
      const value = profile[field];
      if (value == null) continue;
      if (fields.kind !== "brain") errors[field] = "此参数仅适用于大脑配置。";
      else if (!Number.isSafeInteger(value) || value < rule.min || value > rule.max) errors[field] = `${rule.label}需为 ${rule.min}–${rule.max} 的整数，或留空使用默认值。`;
    }
    const capability = reasoningCapability(fields), effort = profile.reasoning_effort, budget = profile.thinking_budget;
    if (effort != null && (fields.kind !== "brain" || !capability.efforts.includes(effort))) errors.reasoning_effort = "此服务商/型号不支持所选思考强度，请选择有效档位或跟随供应商。";
    if (budget != null && (fields.kind !== "brain" || capability.budget_max === null || !Number.isSafeInteger(budget) || budget < 1 || budget > capability.budget_max)) errors.thinking_budget = "此型号不支持所填思考预算；Qwen3.8-Flash 需填 1–262144 的整数。";
    if (effort != null && budget != null) errors.thinking_budget = "思考强度与思考 token 预算不能同时设置。";
    if (fields.provider_id && !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(fields.provider_id)) {
      errors.provider_id = "服务商标识仅可包含英文字母、数字、点、下划线和连字符。";
    }
    if (fields.model && /[\u0000-\u001f\u007f]/.test(fields.model)) errors.model = "模型名称不能包含控制字符。";
    if (fields.credential_env && !safeCredential(fields.credential_env)) {
      errors.credential_env = "只填写环境变量名（如 DEEPSEEK_API_KEY），不要填写密钥。";
    }
    const mode = credentialMode(profile);
    if (!["stored", "env", "none"].includes(mode)) errors.credential_mode = "请选择凭据保存方式。";
    if (mode === "env" && !fields.credential_env) errors.credential_env = "请填写密钥环境变量名。";
    const needsUrl = fields.kind === "brain" || fields.kind === "image" ||
      (fields.kind === "asset3d" && ["cloud_api", "local_http"].includes(fields.mode));
    if (needsUrl && !fields.base_url) errors.base_url = "请填写接口地址。";
    if (fields.base_url && !safeUrl(fields.base_url)) {
      errors.base_url = "使用 HTTP 或 HTTPS 地址，且不要包含账号、密码、查询参数或片段。";
    }
    if (fields.kind === "asset3d") {
      if (!MODES.includes(fields.mode)) errors.mode = "请选择云端 API、本地服务或本地进程。";
      if (fields.mode === "cloud_api" && mode !== "stored" && !fields.credential_env) errors.credential_env = "请填写云端服务的密钥环境变量名。";
      if (fields.mode === "local_process") {
        if (!fields.python_path) errors.python_path = "请填写 Python 可执行文件路径。";
        if (!fields.model_dir) errors.model_dir = "请填写模型目录。";
        if (fields.provider_id === "hunyuan") {
          if (typeof profile.repo_dir !== "string" || !profile.repo_dir.trim()) errors.repo_dir = "请填写包含 hy3dgen/shapegen 的源码仓库目录。";
          if (fields.model !== "hunyuan3d-2mini") errors.model = "本地 Hunyuan 当前仅支持 hunyuan3d-2mini 图生 shape。";
          if (mode !== "none") errors.credential_mode = "本地 Hunyuan 无需密钥。";
        }
      }
    }
    if (fields.name && library && Array.isArray(library.profiles)) {
      const duplicate = library.profiles.some((item) => item.id !== excludeId && item.kind === fields.kind &&
        typeof item.name === "string" && nameKey(item.name) === nameKey(fields.name));
      if (duplicate) errors.name = "同一类型中已存在这个配置名称，请使用其他名称。";
    }
    return { errors, valid: Object.keys(errors).length === 0 };
  }

  function normalizeLibrary(raw) {
    if (!isObject(raw) || raw.schema_version !== 2 || !Array.isArray(raw.profiles) || !isObject(raw.selected)) {
      fail("无法识别配置库结构或版本，原记录应予保留。");
    }
    const library = cloneJson(raw);
    const ids = new Set();
    for (const profile of library.profiles) {
      if (!isObject(profile) || typeof profile.id !== "string" || !profile.id.trim() || ids.has(profile.id)) fail("配置 ID 缺失或重复。");
      for (const field of OPTIONAL_FIELDS) if (profile[field] === undefined) profile[field] = null;
      profile.credential_mode = credentialMode(profile);
      profile.has_secret = profile.has_secret === true;
      ids.add(profile.id);
      if (!Number.isSafeInteger(profile.revision) || profile.revision < 1) fail("配置修订号无效。");
      if (typeof profile.is_draft !== "boolean" || !["unverified", "verified"].includes(profile.verification)) fail("配置状态无效。");
      if (profile.connection_check) {
        profile.verification = profile.connection_check.status === "authenticated" ? "verified" : "unverified";
        profile.verified_at = profile.verification === "verified" ? profile.connection_check.checked_at : null;
      }
      if (!KINDS.includes(profile.kind)) fail("配置类型无效。");
      for (const field of TEXT_FIELDS) if (typeof profile[field] !== "string") fail(`配置字段 ${field} 的类型无效。`);
    }
    for (const kind of KINDS) {
      const selectedId = library.selected[kind];
      if (selectedId === null) continue;
      if (typeof selectedId !== "string") fail("已选配置的 ID 类型无效。");
      const profile = library.profiles.find((item) => item.id === selectedId);
      if (!profile || profile.kind !== kind) fail("已选配置不存在或类型不匹配。");
    }
    for (const profile of library.profiles) {
      const sameSource = { profiles: library.profiles.filter((item) => Boolean(item.server_managed) === Boolean(profile.server_managed)) };
      const validation = validateProfile(profile, sameSource, profile.id);
      if (!profile.is_draft && !validation.valid) fail("已保存配置未通过校验。", validation.errors);
    }
    return library;
  }

  function migrateLegacy(legacyState) {
    if (!isObject(legacyState)) fail("旧配置状态必须是对象，原记录应予保留。");
    const library = emptyLibrary();
    for (const kind of KINDS) {
      const source = legacyState[kind];
      if (source === undefined || source === null) continue;
      if (!isObject(source)) fail(`旧${LABELS[kind]}配置结构无效，原记录应予保留。`);
      const values = {};
      for (const field of TEXT_FIELDS) {
        const value = source[field];
        values[field] = typeof value === "string" || typeof value === "number" ? String(value) : "";
      }
      if (!TEXT_FIELDS.filter((field) => !["kind", "name"].includes(field)).some((field) => values[field].trim())) continue;
      let removedUnsafeValue = false;
      if (values.credential_env.trim() && !safeCredential(values.credential_env.trim())) {
        values.credential_env = "";
        removedUnsafeValue = true;
      }
      // Preserve ordinary invalid URL drafts, but never copy embedded credentials.
      let urlHasCredentials = false;
      try {
        const url = new URL(values.base_url);
        urlHasCredentials = Boolean(url.username || url.password);
      } catch { /* Keep non-URL draft text so it can be corrected later. */ }
      if (values.base_url && (urlHasCredentials || /[?#]/.test(values.base_url) || /\/\/[^/]*@/.test(values.base_url))) {
        values.base_url = "";
        removedUnsafeValue = true;
      }
      const profile = {
        ...values, ...Object.fromEntries(OPTIONAL_FIELDS.map((field) => [field, source[field] ?? null])), id: `legacy-${kind}`, revision: 1, kind,
        name: values.name.trim() || `${LABELS[kind]}（已迁移）`, verification: "unverified", is_draft: false,
      };
      profile.credential_mode = credentialMode(profile);
      profile.has_secret = false;
      profile.is_draft = removedUnsafeValue || !validateProfile(profile, library).valid;
      library.profiles.push(profile);
      library.selected[kind] = profile.id;
    }
    return library;
  }

  function newId(library) {
    let id;
    do {
      id = typeof globalThis.crypto?.randomUUID === "function"
        ? `profile-${globalThis.crypto.randomUUID()}`
        : `profile-${Date.now().toString(36)}-${(++idCounter).toString(36)}-${Math.random().toString(36).slice(2)}`;
    } while (library.profiles.some((profile) => profile.id === id));
    return id;
  }

  function saveProfile(library, draft, options = {}) {
    const result = normalizeLibrary(library);
    if (!isObject(draft)) fail("配置未通过校验。", { profile: "配置必须是对象。" });
    const id = options.id;
    const previous = id === undefined ? null : result.profiles.find((profile) => profile.id === id);
    if (id !== undefined && !previous) fail("要修改的配置不存在。", { id: "配置不存在。" });
    const profile = { ...(previous || {}), ...cloneJson(draft) };
    for (const field of TEXT_FIELDS) {
      if (profile[field] === undefined) profile[field] = "";
      else if (typeof profile[field] === "string") profile[field] = profile[field].trim();
    }
    for (const field of OPTIONAL_FIELDS) if (profile[field] === undefined) profile[field] = null;
    profile.credential_mode = credentialMode(profile);
    profile.has_secret = profile.has_secret === true;
    const validation = validateProfile(profile, result, id);
    if (!validation.valid) fail("配置未通过校验。", validation.errors);
    profile.id = previous ? previous.id : newId(result);
    profile.revision = previous ? previous.revision + 1 : 1;
    profile.verification = "unverified";
    profile.verified_at = null;
    profile.connection_check = null;
    profile.is_draft = false;
    if (previous) result.profiles[result.profiles.findIndex((item) => item.id === id)] = profile;
    else result.profiles.push(profile);
    for (const kind of KINDS) {
      if (result.selected[kind] === profile.id && kind !== profile.kind) result.selected[kind] = null;
    }
    return result;
  }

  function deleteProfile(library, id) {
    const result = normalizeLibrary(library);
    result.profiles = result.profiles.filter((profile) => profile.id !== id);
    for (const kind of KINDS) if (result.selected[kind] === id) result.selected[kind] = null;
    return result;
  }

  function selectProfile(library, kind, idOrNull) {
    const result = normalizeLibrary(library);
    if (!KINDS.includes(kind)) fail("配置类型无效。", { kind: "请选择有效的模型类型。" });
    if (idOrNull !== null) {
      const profile = result.profiles.find((item) => item.id === idOrNull);
      if (!profile || profile.kind !== kind) fail("配置不存在或类型不匹配。", { id: "请选择相应类型的已有配置。" });
      const source = { profiles: result.profiles.filter((item) => Boolean(item.server_managed) === Boolean(profile.server_managed)) };
      const validation = validateProfile(profile, source, profile.id);
      if (!validation.valid || profile.is_draft) {
        fail("请先补全并保存这份配置。", validation.valid ? { profile: "请先保存已迁移的草案。" } : validation.errors);
      }
    }
    result.selected[kind] = idOrNull;
    return result;
  }

  function selectedProfiles(library) {
    const result = { brain: null, image: null, asset3d: null };
    if (!isObject(library) || !Array.isArray(library.profiles) || !isObject(library.selected)) return result;
    for (const kind of KINDS) {
      const profile = library.profiles.find((item) => isObject(item) && item.kind === kind && item.id === library.selected[kind]);
      if (profile) result[kind] = cloneJson(profile);
    }
    return result;
  }

  return { reasoningCapability, REASONING_CAPABILITIES, emptyLibrary, credentialMode, normalizeConnectionCheck, sanitize: cloneJson, migrateLegacy, normalizeLibrary, validateProfile, saveProfile, deleteProfile, selectProfile, selectedProfiles };
});
