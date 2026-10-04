(function () {
  "use strict";
  const UI = window.ForgeUI, Core = window.ForgeSubmission, Source = window.ForgeSource;
  const byId = (id) => document.getElementById(id);
  const own = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);
  UI.addMessages({
    "zh-CN": {
      "submit.title": "上传包 · Agent Forge", "submit.heading": "填写元数据，交由 GitHub 审核。", "submit.intro": "表单自动生成 JSON；最终由你在 GitHub 确认创建 Issue。本页面不上传二进制文件，也不要求访问 token。",
      "submit.modeLabel": "提交方式", "submit.formTab": "可视化填写", "submit.jsonTab": "Agent / JSON", "submit.boundary": "这里只整合元数据。来源、兼容性、发行地址和标签均不是安全保证。", "submit.loading": "正在加载 Schema…", "submit.ready": "Schema v2 已加载。必填字段标有 *。新表单的 createdAt 是元数据草稿创建时间，不是上游发布时间。", "submit.loadError": "Schema 加载失败，请刷新重试。不会在未校验时创建提交链接。",
      "submit.agentHelp": "粘贴或导入完整 Schema v2 记录，所有合法字段和扩展数据会原样保留。Agent 也可以通过已授权的 GitHub 工具直接创建带 package-submission 标签的 Issue。", "submit.schemaLink": "机器可读 Schema", "submit.contractLink": "Agent 提交契约", "submit.exampleLink": "完整示例", "submit.import": "导入 JSON 文件", "submit.jsonLabel": "完整 package JSON", "submit.notes": "Submission Notes（可选）", "submit.notesHelp": "可补充来源或兼容性说明。不填也能提交；它不属于 package JSON。",
      "submit.preview": "生成的 JSON", "submit.checkBoundary": "浏览器检查当前 Schema 的字段约束；跨文件和 Bundle 等语义仍由 CI 最终校验。", "submit.copy": "复制 JSON", "submit.download": "下载 JSON", "submit.create": "前往 GitHub 确认", "submit.longURL": "记录较长，不会放进 URL 或被截断。请复制 JSON，在 GitHub 表单的 Canonical package JSON 字段粘贴，然后确认提交。", "submit.copyIssue": "复制完整 Issue 内容", "submit.blankIssue": "打开 GitHub JSON 表单 ↗", "submit.footer": "只在浏览器保存填写草稿；请勿填入 token、密码或其他秘密。", "submit.reference": "Package 字段参考",
      "submit.deepData": "深层扩展数据已完整保留；请使用 Agent / JSON 入口编辑这一部分。", "submit.optional": "添加可选字段", "submit.add": "添加", "submit.remove": "移除", "submit.item": "条目 {number}", "submit.addKey": "添加扩展属性", "submit.keyPrompt": "属性名（扩展 _meta 的键需符合命名空间约束）", "submit.keyError": "属性名为空或已存在。", "submit.type": "值类型", "submit.null": "未提供（null）", "submit.valid": "字段校验通过；GitHub / CI 仍会进行最终审核。", "submit.copied": "已复制。", "submit.copyFailed": "自动复制不可用。请在 JSON 预览或输入框中手动选择复制。", "submit.jsonError": "JSON 无法解析：{message}", "submit.objectError": "请输入一个 JSON 对象记录。", "submit.restore": "已恢复本浏览器中的草稿。", "submit.saved": "草稿保留在此浏览器；GitHub 提交仍需你确认。", "submit.noStorage": "当前浏览器无法保存草稿，请在离开前复制或下载 JSON。", "submit.typeChanged": "已切换包类型；原类型字段保留在本次会话草稿中，切回可恢复。", "submit.switchError": "JSON 无法解析，暂时不能切换到可视化表单。请先修正。", "submit.fileError": "无法读取 JSON 文件。", "submit.fileTooLarge": "文件超过 2 MiB，请缩小记录后重试。", "submit.popup": "若 GitHub 窗口没有打开，请允许本站打开新窗口；当前草稿不会丢失。",
      "error.required": "必填字段", "error.type": "类型应为 {expected}", "error.const": "固定值应为 {expected}", "error.enum": "请选择合法值：{expected}", "error.minLength": "至少 {expected} 个字符", "error.maxLength": "最多 {expected} 个字符", "error.pattern": "格式不符合 Schema 约束", "error.format": "应符合 {expected} 格式", "error.minimum": "不得小于 {expected}", "error.maximum": "不得大于 {expected}", "error.minItems": "至少 {expected} 条", "error.maxItems": "最多 {expected} 条", "error.minProperties": "至少填写 {expected} 个属性", "error.maxProperties": "最多 {expected} 个属性", "error.uniqueItems": "存在重复条目", "error.additionalProperties": "Schema 不允许此字段", "error.oneOf": "必须满足且仅满足一种字段结构", "error.anyOf": "不符合任何允许的字段结构", "error.not": "此类型不允许这组字段", "error.false": "此字段不被允许",
      "field.name": "包名称", "field.id": "包 ID", "field.version": "版本", "field.type": "包类型 / 发行类型", "field.description": "描述", "field.license": "许可证", "field.targets": "目标 Agent", "field.distributions": "发行候选", "field.agentId": "Agent ID", "field.compatibilityStatus": "兼容性声明", "field.agentVersionRange": "Agent 版本范围", "field.compatibilityNote": "兼容性说明", "field.versionScheme": "版本规则", "field.url": "地址", "field.mcpDetails": "MCP 信息", "field.pluginDetails": "Plugin 信息", "field.skillDetails": "Skill 信息", "field.generalDetails": "General 信息", "field.bundleDetails": "Bundle 信息", "field.registryType": "注册表类型", "field.identifier": "上游标识", "field.transport": "传输方式", "field.registryBaseUrl": "注册表地址", "field.manifestPath": "Manifest 路径", "field.skillPath": "Skill 路径", "field.toolType": "工具性质", "field.agentUse": "Agent 用途", "field.members": "Bundle 成员", "field.memberType": "成员类型", "field.memberId": "成员 ID", "field.links": "文档与项目链接", "field.repository": "仓库", "field.homepage": "项目首页", "field.readme": "README", "field.documentation": "文档", "field.issues": "Issue 地址", "field.facets": "受控 facet（未核验）", "field.customFacets": "用户自定义 tag（未核验）", "field.keywords": "关键词 tag", "field.capabilities": "能力", "field.effects": "影响", "field.dataPractices": "数据处理", "field.permissions": "权限", "field.runtime": "运行环境", "field.integrations": "集成", "field.other": "其它", "field.displayName": "补充显示名", "field.subtype": "子类型", "field._meta": "命名空间扩展数据", "field.publishedAt": "上游发布时间", "field.updatedAt": "更新时间", "field.createdAt": "元数据记录创建时间", "field.releaseNotes": "版本说明", "field.status": "状态", "field.priority": "优先级", "field.ref": "上游 Git ref", "field.registry": "注册表", "field.notes": "说明"
    },
    en: {
      "submit.title": "Submit package · Agent Forge", "submit.heading": "Describe metadata. Review it on GitHub.", "submit.intro": "The form generates JSON for you. You confirm the Issue on GitHub. No binaries or access tokens are uploaded here.", "submit.modeLabel": "Submission method", "submit.formTab": "Visual form", "submit.jsonTab": "Agent / JSON", "submit.boundary": "Metadata only. Sources, compatibility, distributions and labels are not safety guarantees.", "submit.loading": "Loading Schema…", "submit.ready": "Schema v2 loaded. Required fields are marked *. New-form createdAt records metadata draft creation, not upstream publication.", "submit.loadError": "Schema could not be loaded. Refresh to retry. No submission link is created without validation.",
      "submit.agentHelp": "Paste or import one complete Schema v2 record. Valid advanced fields and extension data are preserved. Agents can also create an Issue labeled package-submission with their own authorized GitHub tools.", "submit.schemaLink": "Machine-readable Schema", "submit.contractLink": "Agent submission contract", "submit.exampleLink": "Complete example", "submit.import": "Import JSON file", "submit.jsonLabel": "Complete package JSON", "submit.notes": "Submission Notes (optional)", "submit.notesHelp": "Optionally explain sources or compatibility. Notes are not part of the package JSON.", "submit.preview": "Generated JSON", "submit.checkBoundary": "The browser checks field constraints from this Schema. CI performs final cross-file and Bundle semantic checks.", "submit.copy": "Copy JSON", "submit.download": "Download JSON", "submit.create": "Review on GitHub", "submit.longURL": "This record is too long for our URL handoff. Nothing is truncated. Copy JSON into the Canonical package JSON field in the GitHub form and confirm submission.", "submit.copyIssue": "Copy complete Issue body", "submit.blankIssue": "Open GitHub JSON form ↗", "submit.footer": "Drafts stay in this browser. Do not enter tokens, passwords or other secrets.", "submit.reference": "Package field reference",
      "submit.deepData": "Deep extension data is preserved. Use the Agent / JSON editor to change this section.", "submit.optional": "Add optional fields", "submit.add": "Add", "submit.remove": "Remove", "submit.item": "Item {number}", "submit.addKey": "Add extension property", "submit.keyPrompt": "Property name (_meta keys must use a namespace)", "submit.keyError": "Property name is empty or already exists.", "submit.type": "Value type", "submit.null": "Not provided (null)", "submit.valid": "Field checks passed. GitHub / CI still performs final review.", "submit.copied": "Copied.", "submit.copyFailed": "Automatic copy is unavailable. Select and copy the preview or JSON input manually.", "submit.jsonError": "Could not parse JSON: {message}", "submit.objectError": "Enter one JSON object record.", "submit.restore": "Restored the draft from this browser.", "submit.saved": "The draft stays in this browser. You must still confirm submission on GitHub.", "submit.noStorage": "Draft storage is unavailable. Copy or download JSON before leaving.", "submit.typeChanged": "Package type changed. Previous type-specific fields stay in this session; switch back to restore them.", "submit.switchError": "Fix the JSON syntax before switching to the visual form.", "submit.fileError": "Could not read the JSON file.", "submit.fileTooLarge": "File exceeds 2 MiB. Reduce the record size and try again.", "submit.popup": "If GitHub did not open, allow new windows for this site. Your draft is preserved.",
      "error.required": "Required field", "error.type": "Expected {expected}", "error.const": "Must equal {expected}", "error.enum": "Choose one of: {expected}", "error.minLength": "At least {expected} characters", "error.maxLength": "At most {expected} characters", "error.pattern": "Does not match the Schema pattern", "error.format": "Expected {expected} format", "error.minimum": "Must be at least {expected}", "error.maximum": "Must be at most {expected}", "error.minItems": "At least {expected} items", "error.maxItems": "At most {expected} items", "error.minProperties": "At least {expected} properties", "error.maxProperties": "At most {expected} properties", "error.uniqueItems": "Duplicate items", "error.additionalProperties": "Field is not allowed", "error.oneOf": "Must match exactly one allowed structure", "error.anyOf": "Does not match an allowed structure", "error.not": "This type does not allow these fields", "error.false": "Field is not allowed",
      "field.media": "Display images (external, unverified)", "field.icon": "Icon", "field.previews": "Static previews", "field.alt": "Image description", "field.theme": "Preview theme", "field.name": "Package name", "field.id": "Package ID", "field.version": "Version", "field.type": "Package / distribution type", "field.description": "Description", "field.license": "License", "field.targets": "Agent targets", "field.distributions": "Distribution candidates", "field.agentId": "Agent ID", "field.compatibilityStatus": "Compatibility", "field.agentVersionRange": "Agent version range", "field.compatibilityNote": "Compatibility note", "field.versionScheme": "Version scheme", "field.url": "URL", "field.mcpDetails": "MCP details", "field.pluginDetails": "Plugin details", "field.skillDetails": "Skill details", "field.generalDetails": "General details", "field.bundleDetails": "Bundle details", "field.registryType": "Registry type", "field.identifier": "Upstream identifier", "field.transport": "Transport", "field.registryBaseUrl": "Registry base URL", "field.manifestPath": "Manifest path", "field.skillPath": "Skill path", "field.toolType": "Tool nature", "field.agentUse": "Agent use", "field.members": "Bundle members", "field.memberType": "Member type", "field.memberId": "Member ID", "field.links": "Project and document links", "field.repository": "Repository", "field.homepage": "Homepage", "field.readme": "README", "field.documentation": "Documentation", "field.issues": "Issues URL", "field.facets": "Controlled facets (unverified)", "field.customFacets": "Custom tags (unverified)", "field.keywords": "Keyword tags", "field.capabilities": "Capabilities", "field.effects": "Effects", "field.dataPractices": "Data practices", "field.permissions": "Permissions", "field.runtime": "Runtime", "field.integrations": "Integrations", "field.other": "Other", "field.displayName": "Additional display name", "field.subtype": "Subtype", "field._meta": "Namespaced extension data", "field.publishedAt": "Upstream publication time", "field.updatedAt": "Updated at", "field.createdAt": "Metadata record created at", "field.releaseNotes": "Release notes", "field.status": "Status", "field.priority": "Priority", "field.ref": "Upstream Git ref", "field.registry": "Registry", "field.notes": "Notes"
    }
  });
  UI.addMessages({
    "zh-CN": { "field.media": "展示图片（外链，未核验）", "field.icon": "图标", "field.previews": "静态预览图", "field.alt": "图片文字描述", "field.theme": "预览主题" }
  });
  UI.addMessages({
  "zh-CN": {
    "source.heading": "从来源自动填写",
    "source.help": "输入公开 GitHub 仓库或 HTTPS JSON 地址。离开输入框后自动读取；先选择下方包类型。只读取元数据，不执行代码，也不加载图片。",
    "source.url": "来源地址",
    "source.fetch": "读取来源",
    "source.cancel": "取消",
    "source.options": "分支与子目录（可选）",
    "source.ref": "分支 / tag / commit",
    "source.path": "子目录 / manifest 路径",
    "source.boundary": "读取会向来源站点发送无凭据请求。现有手动内容优先；自动填入且未修改的字段可随来源刷新；无法确认的 ID、版本、Agent 兼容性仍需填写。README 图片只从仓库根 README 提取；根 README 缺失时不向子目录兜底。没有图标时不会用截图补成图标。",
    "source.loading": "正在读取公开元数据…你仍可编辑，手动修改会保留。",
    "source.done": "已填入 {filled} 个字段，保留 {kept} 个已有或已编辑字段。请检查来源声明并补齐必填项。",
    "source.cancelled": "已取消读取，草稿未被此次请求修改。",
    "source.failed": "读取失败：{reason}。草稿已保留，可重试或手动填写。",
    "source.candidates": "README 图片候选（用途未确认，不自动写入）",
    "source.addPreview": "加入 previews",
    "source.added": "已加入",
    "source.limit": "预览最多 12 张，请先移除不需要的条目。",
    "source.report": "读取文件 {count} 个；跳过徽章 {skipped} 张；待处理问题 {issues} 项。",
    "source.conflicts": "保留字段：{paths}",
    "source.issue": "问题：{issues}",
    "source.reason.network": "网络或 CORS 不允许读取",
    "source.reason.rate-limit": "GitHub 限流或拒绝访问",
    "source.reason.not-found": "公开来源不存在或不可读取",
    "source.reason.ambiguous-ref": "此 tree/blob 地址需明确填写分支名，或改用仓库地址",
    "source.reason.partial-source": "部分文件读取失败，已保留上次草稿，请重试",
    "source.reason.multiple-manifests": "发现多个 manifest，请在路径中指定所需 JSON 文件",
    "source.reason.type-mismatch": "JSON 的包类型与当前表单不同，请切换类型后重试",
    "source.reason.too-large": "单文件超过 1 MiB",
    "source.reason.json": "来源不是有效 JSON",
    "source.reason.source-url": "请使用无凭据 HTTPS 地址或 owner/repo",
    "source.reason.source-query": "来源地址不能包含 query 或 fragment",
    "source.reason.source-path": "路径仅用于子目录、JSON manifest 或 SKILL.md，README 始终从仓库根读取",
    "source.reason.source-json": "非 GitHub 来源必须是 HTTPS .json 地址，且支持 CORS",
    "source.reason.metadata-object": "JSON 必须是元数据对象",
    "source.reason.timeout": "读取超时",
    "source.reason.generic": "来源格式不受支持或响应不完整"
  },
  "en": {
    "source.heading": "Fill from a source",
    "source.help": "Enter a public GitHub repository or HTTPS JSON URL. Leaving the input starts reading; select the package type below first. Only metadata is read; code and images are not loaded.",
    "source.url": "Source URL",
    "source.fetch": "Read source",
    "source.cancel": "Cancel",
    "source.options": "Branch and subdirectory (optional)",
    "source.ref": "Branch / tag / commit",
    "source.path": "Subdirectory / manifest path",
    "source.boundary": "Reading sends credential-free requests to the source. Existing manual content wins; untouched auto-filled fields refresh with the source. Missing IDs, versions and Agent compatibility need your input. README images come only from the repository root, with no subdirectory fallback. A screenshot never becomes a missing icon.",
    "source.loading": "Reading public metadata… You can keep editing; your changes will be preserved.",
    "source.done": "Filled {filled} fields and preserved {kept} existing or edited fields. Review source claims and complete required fields.",
    "source.cancelled": "Reading cancelled; this request did not change the draft.",
    "source.failed": "Could not read: {reason}. Your draft is preserved; retry or fill manually.",
    "source.candidates": "README image candidates (unconfirmed, not added automatically)",
    "source.addPreview": "Add to previews",
    "source.added": "Added",
    "source.limit": "At most 12 previews are allowed. Remove an unwanted entry first.",
    "source.report": "Read {count} files; skipped {skipped} badges; {issues} issues need review.",
    "source.conflicts": "Preserved fields: {paths}",
    "source.issue": "Issues: {issues}",
    "source.reason.network": "Network or CORS prevented reading",
    "source.reason.rate-limit": "GitHub rate limit or access denied",
    "source.reason.not-found": "Public source not found or inaccessible",
    "source.reason.ambiguous-ref": "Specify the branch for this tree/blob URL, or use the repository URL",
    "source.reason.partial-source": "Some files could not be read; your previous draft is preserved. Retry reading",
    "source.reason.multiple-manifests": "Multiple manifests found; specify the intended JSON file in the path",
    "source.reason.type-mismatch": "JSON type differs from the current form; change type and retry",
    "source.reason.too-large": "A file exceeds 1 MiB",
    "source.reason.json": "Source is not valid JSON",
    "source.reason.source-url": "Use a credential-free HTTPS URL or owner/repo",
    "source.reason.source-query": "Source URLs must not contain queries or fragments",
    "source.reason.source-path": "Use a subdirectory, JSON manifest or SKILL.md path; README is always read from the repository root",
    "source.reason.source-json": "Other sources must be HTTPS .json URLs with CORS support",
    "source.reason.metadata-object": "JSON must be a metadata object",
    "source.reason.timeout": "Reading timed out",
    "source.reason.generic": "Unsupported source format or incomplete response"
  }
});
  let schema, record = Core.initialRecord(), mode = "form", jsonError = "", touched = false, saving;
  const expandedSections = new Set(), sourceDirty = new Set();
  let sourceRequest = 0, sourceController, sourceResult, sourceMerge, sourceStatus, sourceActiveKey, sourceState;
  const typeDrafts = {}, storageKey = "agent-forge.submission-draft.v1";
  const t = (key, vars) => UI.t(key, vars);
  const fieldName = (key) => { const name = t(`field.${key}`); return name === `field.${key}` ? key : `${name} · ${key}`; };
  const element = (tag, text, cls) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (cls) node.className = cls; return node; };
  const button = (text, action, cls = "schema-add") => { const node = element("button", text, cls); node.type = "button"; node.addEventListener("click", action); return node; };
  function setValue(container, key, value) { Object.defineProperty(container, key, { value, writable: true, enumerable: true, configurable: true }); }
  function effective(raw, value) {
    const result = { ...Core.resolve(raw, schema) };
    result.properties = { ...(result.properties || {}) }; result.required = [...(result.required || [])];
    for (const sub of result.allOf || []) if (sub.if) {
      const passes = !Core.validate(value, { ...sub.if, $defs: schema.$defs }).length;
      const branch = passes ? sub.then : sub.else;
      if (branch) { result.required.push(...(branch.required || [])); Object.assign(result.properties, branch.properties || {}); }
    }
    return result;
  }
  function defaultValue(raw) {
    const s = Core.resolve(raw, schema);
    if (Object.prototype.hasOwnProperty.call(s, "const")) return s.const;
    if (s.oneOf || s.anyOf) return defaultValue((s.oneOf || s.anyOf)[0]);
    if (s.enum) return s.enum[0];
    const type = Array.isArray(s.type) ? s.type[0] : s.type;
    if (type === "object") {
      const value = {};
      for (const key of s.required || []) setValue(value, key, defaultValue((s.properties || {})[key] || {}));
      const e = effective(s, value);
      for (const key of e.required) if (!own(value, key)) setValue(value, key, defaultValue(e.properties[key] || {}));
      return value;
    }
    if (type === "array") return Array.from({ length: s.minItems || 0 }, () => defaultValue(s.items || {}));
    if (type === "null") return null;
    if (type === "boolean") return false;
    if (type === "integer" || type === "number") return s.minimum || 0;
    return "";
  }
  function persist() {
    clearTimeout(saving);
    saving = setTimeout(() => {
      try { localStorage.setItem(storageKey, JSON.stringify({ record, mode, json: byId("json-input").value, notes: byId("submission-notes").value, source: { url: byId("source-url").value, ref: byId("source-ref").value, path: byId("source-path").value }, sourceDirty: [...sourceDirty], sourceState })); }
      catch (_) { byId("submission-status").textContent = t("submit.noStorage"); }
    }, 250);
  }
  function changed(render = false) { jsonError = ""; touched = true; if (render) renderForm(); if (mode === "form") byId("json-input").value = JSON.stringify(record, null, 2); updatePreview(); persist(); }
  function changeType(type) {
    const old = record.type;
    for (const name of ["mcp", "plugin", "skill", "general", "bundle"]) if (own(record, `${name}Details`)) { typeDrafts[name] = Core.clone(record[`${name}Details`]); delete record[`${name}Details`]; }
    if (old !== "bundle" && record.distributions) typeDrafts.distributions = Core.clone(record.distributions);
    record.type = type;
    record[`${type}Details`] = typeDrafts[type] || Core.initialRecord(type)[`${type}Details`];
    if (type === "bundle") delete record.distributions;
    else if (!record.distributions) record.distributions = typeDrafts.distributions || Core.initialRecord(type).distributions;
    changed(true); byId("submission-status").textContent = t("submit.typeChanged");
  }
  function editor(raw, value, update, path, key, required = false, depth = 0) {
    let s = effective(raw, value);
    let choices = s.oneOf || s.anyOf;
    if (choices) {
      const match = choices.find((part) => !Core.validate(value, { ...part, $defs: schema.$defs }).length) || choices.find((part) => {
        const type = Core.resolve(part, schema).type; return type === "array" ? Array.isArray(value) : type === "object" ? value && typeof value === "object" : typeof value === type;
      }) || choices[0];
      s = effective(match, value);
    }
    let actual = value === null ? "null" : Array.isArray(value) ? "array" : typeof value;
    if (actual === "undefined") actual = Array.isArray(s.type) ? s.type[0] : s.type || "string";
    const type = Array.isArray(s.type) ? (s.type.includes(actual) ? actual : s.type[0]) : s.type || actual;
    const group = type === "object" || type === "array";
    const wrap = element(group ? "fieldset" : "div", undefined, group ? "schema-group" : "schema-field");
    wrap.dataset.path = path;
    const label = element(group ? "legend" : "label", `${fieldName(key)}${required ? " *" : ""}`);
    const inputId = `field-${encodeURIComponent(path || "root")}`;
    if (group) { wrap.id = inputId; wrap.tabIndex = -1; }
    else label.htmlFor = inputId;
    wrap.append(label);
    const nullableChoices = choices ? choices.map((part) => Core.resolve(part, schema).type) : Array.isArray(s.type) ? s.type : !raw.type && !raw.$ref && !raw.properties && !raw.enum && !own(raw, "const") ? ["string", "number", "boolean", "object", "array", "null"] : null;
    if (nullableChoices && nullableChoices.length > 1) {
      const picker = element("select"); picker.setAttribute("aria-label", `${fieldName(key)} — ${t("submit.type")}`);
      for (const kind of nullableChoices) { const option = element("option", kind === "null" ? t("submit.null") : kind); option.value = kind; option.selected = kind === actual; picker.append(option); }
      picker.addEventListener("change", () => { const part = choices ? choices.find((item) => Core.resolve(item, schema).type === picker.value) : { ...s, type: picker.value }; update(defaultValue(part)); changed(true); });
      wrap.append(picker);
    }
    if (group && depth >= 8) {
      wrap.append(element("p", t("submit.deepData"), "schema-hint"));
      const preview = element("pre", JSON.stringify(value, null, 2), "schema-deep-preview"); wrap.append(preview); return wrap;
    }
    if (type === "object" && value && !Array.isArray(value)) {
      const props = s.properties || {}, present = new Set(Object.keys(value));
      const keys = [...new Set([...(s.required || []), ...Object.keys(value)])];
      if (path === "") {
        const order = ["schemaVersion", "name", "id", "version", "type", "description", "license", "targets", `${record.type}Details`, "distributions", "facets", "customFacets", "keywords", "links"];
        keys.sort((a, b) => { const ai = order.indexOf(a), bi = order.indexOf(b); return (ai < 0 ? 100 : ai) - (bi < 0 ? 100 : bi); });
      }
      for (const child of keys) {
        if (path === "" && child === "schemaVersion") continue;
        const childSchema = own(props, child) ? props[child] : typeof s.additionalProperties === "object" ? s.additionalProperties : {};
        const childPath = `${path}/${child.replace(/~/g, "~0").replace(/\//g, "~1")}`;
        const needed = s.required.includes(child) || (path === "" && (child === `${record.type}Details` || (record.type !== "bundle" && child === "distributions")));
        if (!own(value, child)) setValue(value, child, defaultValue(childSchema));
        const childEditor = editor(childSchema, value[child], (v) => setValue(value, child, v), childPath, child, needed, depth + 1);
        if (!needed) childEditor.append(button(t("submit.remove"), () => { delete value[child]; changed(true); }, "schema-remove"));
        if (needed) wrap.append(childEditor);
        else {
          const section = element("details", undefined, "schema-present");
          section.dataset.editorPath = childPath; section.open = expandedSections.has(childPath);
          section.append(element("summary", fieldName(child)), childEditor);
          section.addEventListener("toggle", () => { if (section.open) expandedSections.add(childPath); else expandedSections.delete(childPath); });
          wrap.append(section);
        }
      }
      const absent = Object.keys(props).filter((child) => !present.has(child) && !keys.includes(child) && !(path === "" && child.endsWith("Details")));
      if (absent.length || s.additionalProperties === true || typeof s.additionalProperties === "object") {
        const optional = element("details", undefined, "schema-optional"); optional.append(element("summary", t("submit.optional")));
        const buttons = element("div", undefined, "schema-add-fields");
        for (const child of absent) buttons.append(button(`+ ${fieldName(child)}`, () => { setValue(value, child, defaultValue(props[child])); expandedSections.add(`${path}/${child.replace(/~/g, "~0").replace(/\//g, "~1")}`); changed(true); }));
        if (s.additionalProperties === true || typeof s.additionalProperties === "object") buttons.append(button(t("submit.addKey"), () => { const child = window.prompt(t("submit.keyPrompt")); if (child === null) return; if (!child.trim() || own(value, child)) { byId("submission-status").textContent = t("submit.keyError"); return; } setValue(value, child, defaultValue(typeof s.additionalProperties === "object" ? s.additionalProperties : {})); changed(true); }));
        optional.append(buttons); wrap.append(optional);
      }
    } else if (type === "array" && Array.isArray(value)) {
      value.forEach((item, i) => {
        const row = element("div", undefined, "schema-item"); row.append(element("span", t("submit.item", { number: i + 1 }), "schema-number"));
        row.append(editor(s.items || {}, item, (v) => { value[i] = v; }, `${path}/${i}`, String(i + 1), true, depth + 1));
        row.append(button(t("submit.remove"), () => { value.splice(i, 1); changed(true); }, "schema-remove")); wrap.append(row);
      });
      wrap.append(button(`+ ${t("submit.add")}`, () => { value.push(defaultValue(s.items || {})); changed(true); }));
    } else {
      let input;
      if (s.enum) {
        input = element("select");
        if (!s.enum.some((item) => item === value)) { const option = element("option", String(value)); option.value = String(value); option.selected = true; input.append(option); }
        for (const item of s.enum) { const option = element("option", String(item)); option.value = String(item); option.selected = item === value; input.append(option); }
      } else if (type === "null") { input = element("input"); input.value = t("submit.null"); input.readOnly = true; }
      else if (type === "boolean") { input = element("select"); for (const v of [false, true]) { const option = element("option", String(v)); option.value = String(v); option.selected = v === value; input.append(option); } }
      else if (type === "object" || type === "array") { input = element("textarea"); input.value = JSON.stringify(value, null, 2); }
      else { input = element(["description", "releaseNotes", "agentUse", "compatibilityNote", "summary", "impact", "notes"].includes(key) ? "textarea" : "input"); input.value = value == null ? "" : String(value); if (type === "number" || type === "integer") { input.type = "number"; input.step = type === "integer" ? "1" : "any"; } }
      input.id = inputId; input.dataset.path = path; if (required) input.setAttribute("aria-required", "true");
      if (own(s, "const")) input.readOnly = true;
      input.addEventListener(s.enum || type === "boolean" ? "change" : "input", () => {
        let next = type === "boolean" ? input.value === "true" : type === "null" ? null : type === "number" || type === "integer" ? input.value === "" ? "" : Number(input.value) : input.value;
        if (path === "/type") { changeType(next); return; }
        update(next);
        if (key === "compatibilityStatus") {
          const parentPath = path.split("/").slice(1, -1).map((part) => part.replace(/~1/g, "/").replace(/~0/g, "~"));
          let target = record; for (const part of parentPath) target = target[part];
          if (next === "unknown") target.agentVersionRange = null;
          else if (target.agentVersionRange === null) target.agentVersionRange = "";
          changed(true);
        } else changed();
      });
      wrap.append(input);
    }
    return wrap;
  }
  function renderForm() {
    if (!schema) return;
    const focused = document.activeElement && document.activeElement.dataset.path;
    const fields = byId("schema-fields"); fields.replaceChildren(editor(schema, record, (v) => { record = v; }, "", "package", true));
    if (focused) document.getElementById(`field-${encodeURIComponent(focused)}`)?.focus({ preventScroll: true });
  }
  function updatePreview() {
    if (!schema) return;
    byId("source-type").value = record.type;
    byId("json-preview").textContent = mode === "json" && jsonError ? byId("json-input").value : JSON.stringify(record, null, 2);
    const errors = jsonError ? [] : Core.validate(record, schema);
    const box = byId("submission-errors"); box.replaceChildren();
    document.querySelectorAll('[aria-invalid="true"]').forEach((node) => node.removeAttribute("aria-invalid"));
    if (jsonError) box.append(element("p", t("submit.jsonError", { message: jsonError }), "submission-error-list"));
    else if (errors.length && touched) {
      const list = element("ul", undefined, "submission-error-list");
      for (const error of errors) {
        const row = element("li"); row.append(button(`${error.path || "/"}: ${t(`error.${error.keyword}`, { expected: error.expected })}`, () => {
          if (mode === "json") { byId("json-input").focus(); return; }
          const input = document.getElementById(`field-${encodeURIComponent(error.path || "root")}`);
          if (input) {
            let ancestor = input.parentElement;
            while (ancestor) { if (ancestor.tagName === "DETAILS") ancestor.open = true; ancestor = ancestor.parentElement; }
            input.scrollIntoView({ block: "center", behavior: "smooth" }); input.focus({ preventScroll: true });
          }
        }, "")); list.append(row);
        const input = document.getElementById(`field-${encodeURIComponent(error.path || "root")}`); if (input) input.setAttribute("aria-invalid", "true");
      }
      box.append(list);
    } else if (!errors.length) box.append(element("p", t("submit.valid"), "submission-ok"));
    const valid = !jsonError && !errors.length;
    byId("copy-json").disabled = !!jsonError; byId("download-json").disabled = !!jsonError; byId("create-issue").disabled = !valid;
    byId("submission-fallback").hidden = !valid || !Core.issueURL(record, byId("submission-notes").value).tooLong;
  }
  function parseInput() {
    touched = true;
    try { const value = Core.parseRecord(byId("json-input").value); if (!value || Array.isArray(value) || typeof value !== "object") throw new Error(t("submit.objectError")); record = value; for (const key of Object.keys(value)) sourceDirty.add("/" + key.replace(/~/g, "~0").replace(/\//g, "~1")); jsonError = ""; }
    catch (error) { jsonError = error.message; }
    updatePreview(); persist();
  }
  function switchMode(next) {
    if (next === "form" && jsonError) { byId("submission-status").textContent = t("submit.switchError"); return; }
    mode = next;
    for (const kind of ["form", "json"]) { const active = kind === mode; byId(`${kind}-tab`).setAttribute("aria-selected", String(active)); byId(`${kind}-tab`).tabIndex = active ? 0 : -1; byId(`${kind}-panel`).hidden = !active; }
    if (mode === "json") byId("json-input").value = JSON.stringify(record, null, 2);
    else renderForm();
    updatePreview(); persist();
  }
  function sourceMessage(key, vars) { sourceStatus = { key, vars }; byId("source-status").textContent = t(key, vars); }
  function cancelSource(message = true) {
    sourceRequest++; sourceController?.abort(); sourceController = null;
    byId("cancel-source").hidden = true; byId("fetch-source").disabled = !schema;
    if (message) sourceMessage("source.cancelled");
  }
  function renderSourceReport() {
    const report = byId("source-report"), candidates = byId("source-candidates");
    report.replaceChildren(); candidates.replaceChildren();
    if (!sourceResult) return;
    report.append(element("p", t("source.report", { count: sourceResult.observations.length, skipped: sourceResult.skipped, issues: sourceResult.issues.length }), "source-report muted"));
    for (const observation of sourceResult.observations) report.append(element("p", observation.url || observation.repository + " @ " + observation.revision + " / " + observation.path, "source-report muted"));
    if (sourceMerge?.conflicts.length) report.append(element("p", t("source.conflicts", { paths: sourceMerge.conflicts.join(", ") }), "source-report muted"));
    if (sourceResult.issues.length) report.append(element("p", t("source.issue", { issues: [...new Set(sourceResult.issues)].join(", ") }), "source-report muted"));
    if (sourceResult.candidates.length) candidates.append(element("h3", t("source.candidates")));
    for (const candidate of sourceResult.candidates) {
      const row = element("div", undefined, "source-candidate"), text = element("span");
      text.append(element("strong", candidate.alt), element("p", candidate.url, "muted"));
      const exists = record.media?.previews?.some(image => image.url === candidate.url);
      const add = button(t(exists ? "source.added" : "source.addPreview"), () => {
        if (jsonError) { sourceMessage("submit.switchError"); return; }
        const previews = record.media?.previews || [];
        if (previews.length >= Source.MAX_PREVIEWS) { sourceMessage("source.limit"); return; }
        if (previews.some(image => image.url === candidate.url)) return;
        record.media = { ...(record.media || {}), previews: [...previews, { url: candidate.url, alt: candidate.alt }] };
        sourceDirty.add("/media/previews"); expandedSections.add("/media");
        if (mode === "json") byId("json-input").value = JSON.stringify(record, null, 2);
        changed(true); renderSourceReport();
      });
      add.disabled = !!exists; row.append(text, add); candidates.append(row);
    }
  }
  async function fetchSource() {
    if (!schema || !Source) return;
    if (jsonError) { sourceMessage("submit.switchError"); return; }
    const input = byId("source-url").value.trim(), ref = byId("source-ref").value.trim(), path = byId("source-path").value.trim(), type = record.type;
    const key = JSON.stringify([type, input, ref, path]);
    if (sourceController && !sourceController.signal.aborted && key === sourceActiveKey) return;
    cancelSource(false); const request = sourceRequest, controller = new AbortController(); sourceController = controller; sourceActiveKey = key;
    const baseline = Core.initialRecord(type, record.createdAt), timeout = setTimeout(() => controller.abort("timeout"), 25000);
    sourceResult = null; sourceMerge = null; renderSourceReport();
    byId("cancel-source").hidden = false; sourceMessage("source.loading"); persist();
    try {
      const result = await Source.acquire(input, { schema, type, ref, path, signal: controller.signal });
      if (request !== sourceRequest || controller.signal.aborted) return;
      if (record.type !== type || jsonError) { sourceMessage("source.cancelled"); return; }
      if (sourceState && result.issues.some(issue => /^(?:readme-(?:unavailable|invalid)|manifest-invalid)/.test(issue))) { sourceResult = result; renderSourceReport(); throw Error("partial-source"); }
      sourceMerge = Source.mergeAcquired(record, result.fields, { baseline, dirty: [...sourceDirty], previous: sourceState?.managed });
      record = sourceMerge.record; sourceResult = result;
      const sourceMeta = record._meta?.["org.agentforge/source-acquisition"];
      const metaPath = "/_meta/org.agentforge~1source-acquisition";
      const protectedMeta = [...sourceDirty].some(path => path === metaPath || metaPath.startsWith(path + "/") || path.startsWith(metaPath + "/"));
      const ownedMeta = sourceMeta === undefined || sourceState?.provenance && JSON.stringify(sourceMeta) === JSON.stringify(sourceState.provenance);
      sourceState = { managed: sourceMerge.managed };
      if (!protectedMeta && ownedMeta && (record._meta === undefined || record._meta && typeof record._meta === "object" && !Array.isArray(record._meta))) {
        record._meta ||= {};
        sourceState.provenance = { observations: result.observations, fields: Object.keys(sourceMerge.managed) };
        record._meta["org.agentforge/source-acquisition"] = Core.clone(sourceState.provenance);
      } else sourceMerge.conflicts.push(metaPath);
      expandedSections.add("/media");
      if (mode === "json") byId("json-input").value = JSON.stringify(record, null, 2);
      changed(true); renderSourceReport(); sourceMessage("source.done", { filled: sourceMerge.filled.length, kept: sourceMerge.conflicts.length });
    } catch (error) {
      if (request !== sourceRequest) return;
      const key = "source.reason." + (controller.signal.reason === "timeout" ? "timeout" : error.message);
      sourceMessage("source.failed", { reason: t(key) === key ? t("source.reason.generic") : t(key) });
    } finally {
      clearTimeout(timeout);
      if (request === sourceRequest) { sourceController = null; byId("cancel-source").hidden = true; }
    }
  }
  byId("source-type").addEventListener("change", () => { if (jsonError) { sourceMessage("submit.switchError"); byId("source-type").value = record.type; return; } cancelSource(false); sourceDirty.add("/type"); changeType(byId("source-type").value); if (mode === "json") byId("json-input").value = JSON.stringify(record, null, 2); });
  byId("fetch-source").addEventListener("click", fetchSource);
  byId("cancel-source").addEventListener("click", () => cancelSource());
  for (const id of ["source-url", "source-ref", "source-path"]) {
    byId(id).addEventListener("input", () => { if (sourceController) cancelSource(); sourceResult = null; renderSourceReport(); persist(); });
    byId(id).addEventListener("change", () => { if (byId("source-url").value.trim()) fetchSource(); });
    byId(id).addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); fetchSource(); } });
  }
  for (const event of ["input", "change", "click"]) byId("package-form").addEventListener(event, action => {
    if (event === "click" && action.target.tagName !== "BUTTON") return;
    const path = action.target.dataset.path || action.target.closest("[data-path]")?.dataset.path;
    if (path) sourceDirty.add(path);
    if (path === "/type") cancelSource(false);
  }, true);
  async function copy(text) {
    try { if (!navigator.clipboard) throw new Error("Clipboard unavailable"); await navigator.clipboard.writeText(text); byId("submission-status").textContent = t("submit.copied"); }
    catch (_) { byId("submission-status").textContent = t("submit.copyFailed"); }
  }
  for (const kind of ["form", "json"]) {
    byId(`${kind}-tab`).addEventListener("click", () => switchMode(kind));
    byId(`${kind}-tab`).addEventListener("keydown", (event) => { if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) { event.preventDefault(); const next = event.key === "Home" ? "form" : event.key === "End" ? "json" : kind === "form" ? "json" : "form"; switchMode(next); byId(`${mode}-tab`).focus(); } });
  }
  byId("package-form").addEventListener("submit", (event) => event.preventDefault());
  byId("json-input").addEventListener("input", parseInput);
  byId("submission-notes").addEventListener("input", () => { updatePreview(); persist(); });
  byId("json-file").addEventListener("change", async () => {
    const file = byId("json-file").files[0]; if (!file) return;
    if (file.size > 2 * 1024 * 1024) { byId("submission-status").textContent = t("submit.fileTooLarge"); return; }
    try { byId("json-input").value = await file.text(); mode = "json"; parseInput(); byId("json-panel").hidden = false; byId("form-panel").hidden = true; byId("form-tab").setAttribute("aria-selected", "false"); byId("form-tab").tabIndex = -1; byId("json-tab").setAttribute("aria-selected", "true"); byId("json-tab").tabIndex = 0; }
    catch (_) { byId("submission-status").textContent = t("submit.fileError"); }
  });
  byId("copy-json").addEventListener("click", () => copy(JSON.stringify(record, null, 2)));
  byId("copy-issue").addEventListener("click", () => copy(Core.issueBody(record, byId("submission-notes").value)));
  byId("download-json").addEventListener("click", () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(record, null, 2) + "\n"], { type: "application/json" })); const link = element("a"); link.href = url; link.download = "package.json"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  byId("create-issue").addEventListener("click", () => {
    touched = true; updatePreview(); if (byId("create-issue").disabled) return;
    const issue = Core.issueURL(record, byId("submission-notes").value);
    if (issue.tooLong) { byId("submission-fallback").hidden = false; byId("submission-fallback").scrollIntoView({ block: "center", behavior: "smooth" }); return; }
    window.open(issue.href, "_blank", "noopener,noreferrer"); byId("submission-status").textContent = t("submit.popup");
  });
  window.addEventListener("forge:localechange", () => { if (sourceStatus) byId("source-status").textContent = t(sourceStatus.key, sourceStatus.vars); renderSourceReport(); document.title = t("submit.title"); UI.apply(); updateReference(); renderForm(); updatePreview(); byId("schema-status").textContent = t(schema ? "submit.ready" : "submit.loadError"); byId("submission-status").textContent = ""; });
  function updateReference() {
    document.querySelector(".footer a").href = new URL(`docs/schema/package.schema${UI.locale === "en" ? "_en" : ""}.html`, UI.siteBase).href;
  }
  UI.apply(); document.title = t("submit.title"); updateReference();
  fetch(new URL("package.schema.json", UI.siteBase)).then((response) => { if (!response.ok) throw new Error(String(response.status)); return response.json(); }).then((value) => {
    Core.assertSupported(value); schema = value;
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey) || "null");
      if (saved && saved.record && typeof saved.record === "object" && !Array.isArray(saved.record)) { sourceState = saved.sourceState; record = saved.record; byId("submission-notes").value = typeof saved.notes === "string" ? saved.notes : ""; mode = saved.mode === "json" ? "json" : "form"; byId("json-input").value = typeof saved.json === "string" ? saved.json : JSON.stringify(record, null, 2); if (mode === "json") parseInput(); byId("submission-status").textContent = t("submit.restore"); for (const path of saved.sourceDirty || Object.keys(record).map(key => "/" + key.replace(/~/g,"~0").replace(/\//g,"~1"))) sourceDirty.add(path); for (const key of ["url", "ref", "path"]) if (typeof saved.source?.[key] === "string") byId("source-" + key).value = saved.source[key]; }
    } catch (_) { /* Storage is optional; the form remains usable. */ }
    byId("schema-status").removeAttribute("data-i18n");
    byId("schema-status").textContent = t("submit.ready");
    byId("fetch-source").disabled = false;
    renderForm();
    if (mode === "json" && jsonError) { byId("form-panel").hidden = true; byId("json-panel").hidden = false; byId("form-tab").setAttribute("aria-selected", "false"); byId("json-tab").setAttribute("aria-selected", "true"); }
    else switchMode(mode);
    updatePreview();
  }).catch(() => { byId("schema-status").textContent = t("submit.loadError"); byId("schema-status").removeAttribute("data-i18n"); });
})();
