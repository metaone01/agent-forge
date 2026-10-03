/* Shared preferences run synchronously before CSS, including on nested Pages routes. */
(function () {
  "use strict";
  const script = document.currentScript;
  const siteBase = new URL("../", script.src);
  const messages = { "zh-CN": {}, en: {} };
  const localeKey = "forge:locale";
  const themeKey = "forge:theme";
  function read(key, fallback) { try { return localStorage.getItem(key) || fallback; } catch (_) { return fallback; } }
  function save(key, value) { try { localStorage.setItem(key, value); } catch (_) { /* Session preferences still work. */ } }
  let locale = read(localeKey, "zh-CN") === "en" ? "en" : "zh-CN";
  let theme = read(themeKey, "system");
  if (!["light", "dark", "system"].includes(theme)) theme = "system";
  const media = typeof window.matchMedia === "function" ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  function applyTheme() {
    const resolved = theme === "system" ? (media && media.matches ? "dark" : "light") : theme;
    document.documentElement.dataset.theme = resolved;
    document.documentElement.dataset.themeMode = theme;
    document.documentElement.style.colorScheme = resolved;
    return resolved;
  }
  function t(key, vars = {}) {
    const text = messages[locale][key] ?? messages["zh-CN"][key] ?? key;
    return String(text).replace(/\{([^{}]+)\}/g, (match, name) => Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : match);
  }
  const bindings = { "data-i18n": null, "data-i18n-placeholder": "placeholder", "data-i18n-title": "title", "data-i18n-aria-label": "aria-label", "data-i18n-content": "content", "data-i18n-alt": "alt" };
  function apply(root = document) {
    for (const [attribute, target] of Object.entries(bindings)) {
      const selector = `[${attribute}]`;
      const nodes = Array.from(root.querySelectorAll(selector));
      if (root.matches && root.matches(selector)) nodes.unshift(root);
      for (const node of nodes) {
        const text = t(node.getAttribute(attribute));
        if (target) node.setAttribute(target, text); else node.textContent = text;
      }
    }
    root.querySelectorAll("[data-doc-entry]").forEach((node) => {
      node.href = new URL(locale === "en" ? "docs/index_en.html" : "docs/", siteBase).href;
    });
    root.querySelectorAll("[data-forge-locale]").forEach((node) => {
      node.dataset.locale = locale;
      node.innerHTML = flag(locale) + '<span>' + languageName(locale) + '</span>' + icon("chevron");
      node.setAttribute("aria-label", t("preferences.locale") + ": " + languageName(locale));
      node.setAttribute("title", t("preferences.locale"));
    });
    root.querySelectorAll("[data-forge-locale-option]").forEach((node) => {
      node.setAttribute("aria-checked", String(node.dataset.forgeLocaleOption === locale));
    });
    root.querySelectorAll("[data-forge-theme]").forEach((node) => { node.setAttribute("aria-label", t("preferences.theme")); });
    root.querySelectorAll("[data-forge-theme-option]").forEach((node) => {
      const mode = node.dataset.forgeThemeOption;
      node.setAttribute("aria-label", t("theme." + mode));
      node.setAttribute("title", t("theme." + mode));
      node.setAttribute("aria-checked", String(mode === theme));
      node.tabIndex = mode === theme ? 0 : -1;
    });
  }
  function addMessages(additions) {
    for (const language of ["zh-CN", "en"]) Object.assign(messages[language], additions[language] || {});
  }
  function setLocale(value) {
    const next = value === "en" ? "en" : "zh-CN";
    save(localeKey, next);
    if (locale === next) return;
    locale = next;
    document.documentElement.lang = locale;
    apply();
    window.dispatchEvent(new CustomEvent("forge:localechange", { detail: { locale } }));
  }
  function setTheme(value) {
    const next = ["light", "dark", "system"].includes(value) ? value : "system";
    save(themeKey, next);
    if (theme === next) return;
    theme = next;
    const resolved = applyTheme();
    apply();
    window.dispatchEvent(new CustomEvent("forge:themechange", { detail: { theme, resolved } }));
  }
  window.ForgeUI = { get locale() { return locale; }, get theme() { return theme; }, siteBase, t, addMessages, apply, setLocale, setTheme };
  document.documentElement.lang = locale;
  applyTheme();
  const systemChanged = () => {
    if (theme !== "system") return;
    const resolved = applyTheme();
    window.dispatchEvent(new CustomEvent("forge:themechange", { detail: { theme, resolved } }));
  };
  if (media) {
    if (media.addEventListener) media.addEventListener("change", systemChanged);
    else if (media.addListener) media.addListener(systemChanged);
  }
  addMessages({
    "zh-CN": { "nav.catalog": "目录", "nav.dashboard": "数据总览", "nav.docs": "文档", "nav.submit": "上传包", "nav.label": "主导航", "preferences.locale": "语言", "preferences.theme": "主题", "theme.system": "跟随系统", "theme.light": "亮色", "theme.dark": "暗色", "tags.controlled": "受控标签", "tags.custom": "自定义标签", "tags.keywords": "关键词", "tags.more": "另有 {count} 个标签" },
    en: { "nav.catalog": "Catalog", "nav.dashboard": "Dashboard", "nav.docs": "Docs", "nav.submit": "Submit package", "nav.label": "Main navigation", "preferences.locale": "Language", "preferences.theme": "Theme", "theme.system": "System", "theme.light": "Light", "theme.dark": "Dark", "tags.controlled": "Controlled facets", "tags.custom": "Custom facets", "tags.keywords": "Keywords", "tags.more": "{count} more tags" }
  });
  addMessages({
  "zh-CN": {
    "METADATA CATALOG": "元数据目录",
    "Agent Forge · 元数据目录": "Agent Forge · 元数据目录",
    "Agent Forge · Dashboard": "Agent Forge · 数据总览",
    "Agent Forge · Agent Dashboard": "Agent Forge · Agent 数据",
    "Agent Forge metadata catalog": "Agent Forge 元数据目录",
    "Agent Forge catalog dashboard": "Agent Forge 目录数据总览",
    "Agent Forge agent dashboard": "Agent Forge Agent 数据总览",
    "READ-ONLY METADATA INDEX": "只读元数据索引",
    "为 Agent 找到合适的工具。": "为 Agent 找到合适的工具。",
    "按 Agent、类型、版本范围和 facet 检索整合后的公开元数据。安装地址由每个包提供，目录本身不代理二进制文件。": "按 Agent、类型、版本范围和标签检索整合后的公开元数据。安装地址由每个包提供，目录本身不代理二进制文件。",
    "正在读取索引…": "正在读取索引…",
    "搜索和筛选": "搜索和筛选",
    "搜索名称、描述、关键词或 facet": "搜索名称、描述、关键词或标签",
    "全部 Agent": "全部 Agent",
    "类型": "类型",
    "全部类型": "全部类型",
    "Plugin": "插件",
    "Skill": "技能",
    "General": "通用",
    "Bundle": "组合包",
    "Subtype": "子类型",
    "全部 subtype": "全部子类型",
    "清除筛选": "清除筛选",
    "个结果": "个结果",
    "正在加载…": "正在加载…",
    "排序": "排序",
    "相关性": "相关性",
    "最近更新": "最近更新",
    "名称": "名称",
    "结果分页": "结果分页",
    "上一页": "上一页",
    "下一页": "下一页",
    "第": "第",
    "页码": "页码",
    "没有匹配的包": "没有匹配的包",
    "尝试减少筛选条件，或者查看 Dashboard 了解当前数据范围。": "尝试减少筛选条件，或者查看数据总览了解当前数据范围。",
    "打开 Dashboard": "打开数据总览",
    "Agent Forge 只整合元数据，不验证来源、兼容性或安全性。": "Agent Forge 只整合元数据，不验证来源、兼容性或安全性。",
    "CATALOG OBSERVATORY": "目录观测",
    "数据总览": "数据总览",
    "按 Agent 查看目录规模、类型分布和元数据使用情况。": "按 Agent 查看目录规模、类型分布和元数据使用情况。",
    "正在读取…": "正在读取…",
    "总览指标": "总览指标",
    "包搜索暂时关闭，当前仅提供 Dashboard 数据总览。": "包搜索暂时关闭，当前仅提供数据总览。",
    "AGENT COVERAGE": "Agent 覆盖",
    "Agent 覆盖": "Agent 覆盖",
    "包数量": "包数量",
    "TYPE MIX": "类型分布",
    "类型分布": "类型分布",
    "FACETS": "受控标签",
    "常见 facet": "常见标签",
    "COMPATIBILITY": "兼容性",
    "兼容性声明": "兼容性声明",
    "版本范围是提交者提供的元数据，unknown 不代表不兼容。": "版本范围是提交者提供的元数据，unknown 不代表不兼容。",
    "PER-AGENT": "按 Agent 查看",
    "Agent 数据": "Agent 数据",
    "选择一个 Agent 查看明细": "选择一个 Agent 查看明细",
    "数据已整合但未核验。Agent Forge 不提供安全、可信度或兼容性保证。": "数据已整合但未核验。Agent Forge 不提供安全、可信度或兼容性保证。",
    "Agent Forge · 静态 Dashboard": "Agent Forge · 静态数据总览",
    "← 返回总览": "← 返回总览",
    "AGENT PROFILE": "Agent 概况",
    "Agent 指标": "Agent 指标",
    "正在读取 Agent 投影…": "正在读取 Agent 投影…",
    "RECENT UPDATES": "最近更新",
    "未知时间": "未知时间",
    "未发布 revision": "未发布 revision",
    "暂无描述，打开详情查看来源与安装候选。": "暂无描述，打开详情查看来源与安装候选。",
    "暂无描述": "暂无描述",
    "← 返回目录": "← 返回目录",
    "兼容范围未知": "兼容范围未知",
    "元数据记录": "元数据记录",
    "包信息": "包信息",
    "Package ID": "包 ID",
    "最新版本": "最新版本",
    "Agent 版本范围": "Agent 版本范围",
    "未指定": "未指定",
    "未提供": "未提供",
    "标签": "标签",
    "类型详情": "类型详情",
    "安装候选": "安装候选",
    "发行来源": "发行来源",
    "未提供地址": "未提供地址",
    "暂无发行来源。Agent Forge 不托管插件文件。": "暂无发行来源。Agent Forge 不托管插件文件。",
    "文档链接": "文档链接",
    "暂无链接": "暂无链接",
    "已生成公开投影": "已生成公开投影",
    "包": "包",
    "跨所有类型": "跨所有类型",
    "版本": "版本",
    "元数据版本记录": "元数据版本记录",
    "包含嵌套统计": "包含嵌套统计",
    "Agent 包数量": "Agent 包数量",
    "暂无 Agent 统计": "暂无 Agent 统计",
    "类型数量": "类型数量",
    "暂无 facet 统计": "暂无标签统计",
    "已声明范围": "已声明范围",
    "范围未知": "范围未知",
    "该 Agent 投影": "该 Agent 投影",
    "未提供 Agent 版本范围": "未提供 Agent 版本范围",
    "暂无类型统计": "暂无类型统计",
    "暂无最近更新记录": "暂无最近更新记录",
    "catalog.records": "· {count} 条静态索引记录",
    "catalog.empty": "· 当前没有已发布记录",
    "catalog.versions": "{count} 个版本",
    "catalog.version": "版本 {version}",
    "catalog.pages": " / {count} 页",
    "agent.packages": "{count} 个包",
    "agent.types": "{count} 类",
    "agent.description": "Agent {id} 的静态元数据投影。",
    "detail.missing": "未找到该包，或详情地址无效。"
  },
  "en": {
    "METADATA CATALOG": "METADATA CATALOG",
    "Agent Forge · 元数据目录": "Agent Forge · Metadata catalog",
    "Agent Forge · Dashboard": "Agent Forge · Dashboard",
    "Agent Forge · Agent Dashboard": "Agent Forge · Agent dashboard",
    "Agent Forge metadata catalog": "Agent Forge metadata catalog",
    "Agent Forge catalog dashboard": "Agent Forge catalog dashboard",
    "Agent Forge agent dashboard": "Agent Forge agent dashboard",
    "READ-ONLY METADATA INDEX": "READ-ONLY METADATA INDEX",
    "为 Agent 找到合适的工具。": "Find the right tools for your Agent.",
    "按 Agent、类型、版本范围和 facet 检索整合后的公开元数据。安装地址由每个包提供，目录本身不代理二进制文件。": "Search public metadata by Agent, type, version range and facets. Packages provide installation URLs; the catalog does not proxy binaries.",
    "正在读取索引…": "Reading indexes…",
    "搜索和筛选": "Search and filters",
    "搜索名称、描述、关键词或 facet": "Search names, descriptions, keywords or facets",
    "全部 Agent": "All Agents",
    "类型": "Type",
    "全部类型": "All types",
    "Plugin": "Plugin",
    "Skill": "Skill",
    "General": "General",
    "Bundle": "Bundle",
    "Subtype": "Subtype",
    "全部 subtype": "All subtypes",
    "清除筛选": "Clear filters",
    "个结果": "results",
    "正在加载…": "Loading…",
    "排序": "Sort",
    "相关性": "Relevance",
    "最近更新": "Recent updates",
    "名称": "Name",
    "结果分页": "Result pages",
    "上一页": "Previous page",
    "下一页": "Next page",
    "第": "Page",
    "页码": "Page number",
    "没有匹配的包": "No matching packages",
    "尝试减少筛选条件，或者查看 Dashboard 了解当前数据范围。": "Try fewer filters, or check the Dashboard for available data.",
    "打开 Dashboard": "Open Dashboard",
    "Agent Forge 只整合元数据，不验证来源、兼容性或安全性。": "Agent Forge aggregates metadata only; it does not verify sources, compatibility or security.",
    "CATALOG OBSERVATORY": "CATALOG OBSERVATORY",
    "数据总览": "Dashboard",
    "按 Agent 查看目录规模、类型分布和元数据使用情况。": "Explore catalog size, type distribution and metadata usage by Agent.",
    "正在读取…": "Loading…",
    "总览指标": "Overview metrics",
    "包搜索暂时关闭，当前仅提供 Dashboard 数据总览。": "Package search is temporarily disabled; only the Dashboard overview is available.",
    "AGENT COVERAGE": "AGENT COVERAGE",
    "Agent 覆盖": "Agent coverage",
    "包数量": "Package count",
    "TYPE MIX": "TYPE MIX",
    "类型分布": "Type distribution",
    "FACETS": "FACETS",
    "常见 facet": "Common facets",
    "COMPATIBILITY": "COMPATIBILITY",
    "兼容性声明": "Compatibility declarations",
    "版本范围是提交者提供的元数据，unknown 不代表不兼容。": "Version ranges are submitter-provided metadata. Unknown does not mean incompatible.",
    "PER-AGENT": "PER-AGENT",
    "Agent 数据": "Agent data",
    "选择一个 Agent 查看明细": "Select an Agent to view details",
    "数据已整合但未核验。Agent Forge 不提供安全、可信度或兼容性保证。": "Aggregated data is unverified. Agent Forge makes no security, trust or compatibility guarantees.",
    "Agent Forge · 静态 Dashboard": "Agent Forge · Static dashboard",
    "← 返回总览": "← Back to overview",
    "AGENT PROFILE": "AGENT PROFILE",
    "Agent 指标": "Agent metrics",
    "正在读取 Agent 投影…": "Loading Agent projection…",
    "RECENT UPDATES": "RECENT UPDATES",
    "未知时间": "Unknown date",
    "未发布 revision": "No published revision",
    "暂无描述，打开详情查看来源与安装候选。": "No description. Open details for sources and installation candidates.",
    "暂无描述": "No description",
    "← 返回目录": "← Back to catalog",
    "兼容范围未知": "Compatibility range unknown",
    "元数据记录": "Metadata record",
    "包信息": "Package information",
    "Package ID": "Package ID",
    "最新版本": "Latest version",
    "Agent 版本范围": "Agent version range",
    "未指定": "Not specified",
    "未提供": "Not provided",
    "标签": "Tags",
    "类型详情": "Type details",
    "安装候选": "Installation candidates",
    "发行来源": "Distribution source",
    "未提供地址": "URL not provided",
    "暂无发行来源。Agent Forge 不托管插件文件。": "No distributions provided. Agent Forge does not host plugin files.",
    "文档链接": "Documentation links",
    "暂无链接": "No links",
    "已生成公开投影": "Public projections generated",
    "包": "Packages",
    "跨所有类型": "Across all types",
    "版本": "Versions",
    "元数据版本记录": "Metadata version records",
    "包含嵌套统计": "Including nested statistics",
    "Agent 包数量": "Package count by Agent",
    "暂无 Agent 统计": "No Agent statistics",
    "类型数量": "Type counts",
    "暂无 facet 统计": "No facet statistics",
    "已声明范围": "Declared range",
    "范围未知": "Unknown range",
    "该 Agent 投影": "This Agent projection",
    "未提供 Agent 版本范围": "No Agent version range provided",
    "暂无类型统计": "No type statistics",
    "暂无最近更新记录": "No recent updates",
    "catalog.records": "· {count} static index records",
    "catalog.empty": "· No published records",
    "catalog.versions": "{count} versions",
    "catalog.version": "Version {version}",
    "catalog.pages": " / {count} pages",
    "agent.packages": "{count} packages",
    "agent.types": "{count} types",
    "agent.description": "Static metadata projection for Agent {id}.",
    "detail.missing": "Package not found, or invalid detail URL."
  }
});
  const star = "M0-1 .224-.309 .951-.309 .363.118 .588.809 0 .382-.588.809-.363.118-.951-.309-.224-.309Z";
  function languageName(value) { return value === "en" ? "English" : "简体中文"; }
  function flag(value) {
    let artwork;
    if (value === "en") {
      const stripes = Array.from({ length: 7 }, (_, i) => '<rect y="' + i * 40 / 13 + '" width="30" height="' + 20 / 13 + '" fill="#b22234"/>').join("");
      const stars = Array.from({ length: 9 }, (_, row) => Array.from({ length: row % 2 ? 5 : 6 }, (_, col) => '<path d="' + star + '" transform="translate(' + (1 + col * 2 + row % 2) + ' ' + (0.8 + row * 1.12) + ') scale(.43)" fill="#fff"/>').join("")).join("");
      artwork = '<rect width="30" height="20" fill="#fff"/>' + stripes + '<rect width="12" height="' + 140 / 13 + '" fill="#3c3b6e"/>' + stars;
    } else {
      const stars = [[5, 5, 3, 0], [10, 2, 1, 30], [12, 4, 1, -10], [12, 7, 1, 20], [10, 9, 1, -30]];
      artwork = '<rect width="30" height="20" fill="#de2910"/>' + stars.map(([x, y, size, rotation]) => '<path d="' + star + '" transform="translate(' + x + ' ' + y + ') rotate(' + rotation + ') scale(' + size + ')" fill="#ffde00"/>').join("");
    }
    // Inline vector flags stay visible on platforms without flag-emoji glyphs.
    return '<svg class="language-flag" viewBox="0 0 30 20" aria-hidden="true" focusable="false" data-flag="' + (value === "en" ? "us" : "cn") + '">' + artwork + '</svg>';
  }
  function icon(name) {
    const paths = {
      light: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M4.93 4.93l1.41 1.41m11.32 11.32 1.41 1.41M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/>',
      dark: '<path d="M20.9 13.1A9 9 0 0 1 10.9 3.1a9 9 0 1 0 10 10Z"/>',
      system: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M12 16v4m-4 0h8"/>',
      chevron: '<path d="m6 9 6 6 6-6"/>'
    };
    return '<svg class="preference-icon' + (name === "chevron" ? ' preference-chevron' : '') + '" viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' + paths[name] + '</svg>';
  }
  function installControls() {
    document.querySelectorAll(".topnav").forEach((nav, index) => {
      if (nav.querySelector(".forge-preferences")) return;
      const controls = document.createElement("div");
      controls.className = "forge-preferences";
      const menuId = "forge-language-menu-" + index;
      controls.innerHTML = '<div class="language-picker"><button type="button" class="language-trigger" data-forge-locale aria-haspopup="menu" aria-expanded="false" aria-controls="' + menuId + '"></button><div class="language-menu" id="' + menuId + '" role="menu" data-i18n-aria-label="preferences.locale" hidden>' + ["zh-CN", "en"].map((language) => '<button type="button" role="menuitemradio" tabindex="-1" aria-checked="false" data-forge-locale-option="' + language + '">' + flag(language) + '<span>' + languageName(language) + '</span></button>').join("") + '</div></div><div class="theme-icons" data-forge-theme role="radiogroup">' + ["light", "dark", "system"].map((mode) => '<button type="button" role="radio" aria-checked="false" data-forge-theme-option="' + mode + '">' + icon(mode) + '</button>').join("") + '</div>';
      const trigger = controls.querySelector("[data-forge-locale]");
      const menu = controls.querySelector(".language-menu");
      const choices = Array.from(menu.querySelectorAll("button"));
      function closeMenu(focus = false) { menu.hidden = true; trigger.setAttribute("aria-expanded", "false"); if (focus) trigger.focus(); }
      function openMenu() { menu.hidden = false; trigger.setAttribute("aria-expanded", "true"); choices.find((node) => node.dataset.forgeLocaleOption === locale).focus(); }
      trigger.addEventListener("click", () => { if (menu.hidden) openMenu(); else closeMenu(); });
      trigger.addEventListener("keydown", (event) => { if (["ArrowDown", "ArrowUp"].includes(event.key)) { event.preventDefault(); openMenu(); } });
      choices.forEach((node) => node.addEventListener("click", () => { closeMenu(true); setLocale(node.dataset.forgeLocaleOption); }));
      menu.addEventListener("keydown", (event) => {
        if (event.key === "Escape") { event.preventDefault(); closeMenu(true); }
        else if (event.key === "Tab") closeMenu(true);
        else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
          event.preventDefault();
          const current = choices.indexOf(document.activeElement);
          const next = event.key === "Home" ? 0 : event.key === "End" ? choices.length - 1 : (current + (event.key === "ArrowDown" ? 1 : -1) + choices.length) % choices.length;
          choices[next].focus();
        }
      });
      document.addEventListener("click", (event) => { if (!controls.contains(event.target)) closeMenu(); });
      controls.querySelector(".language-picker").addEventListener("focusout", (event) => { if (!event.currentTarget.contains(event.relatedTarget)) closeMenu(); });
      const themes = Array.from(controls.querySelectorAll("[data-forge-theme-option]"));
      themes.forEach((node, position) => {
        node.addEventListener("click", () => setTheme(node.dataset.forgeThemeOption));
        node.addEventListener("keydown", (event) => {
          if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
          event.preventDefault();
          const next = event.key === "Home" ? 0 : event.key === "End" ? themes.length - 1 : (position + (["ArrowRight", "ArrowDown"].includes(event.key) ? 1 : -1) + themes.length) % themes.length;
          setTheme(themes[next].dataset.forgeThemeOption); themes[next].focus();
        });
      });
      nav.appendChild(controls);
    });
    apply();
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", installControls, { once: true });
  else installControls();
}());
