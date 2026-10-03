/* Shared preferences translate chrome only; the document's URL owns its content language. */
(() => {
  "use strict";
  const body = document.body;
  const pageLocale = body.dataset.docLocale === "en" ? "en" : "zh-CN";
  const ui = window.ForgeUI;
  const messages = {
    "zh-CN": {
      "docs.catalog": "目录", "docs.docs": "文档", "docs.submit": "提交记录",
      "docs.overview": "概览", "docs.reference": "参考", "docs.toc": "本页目录",
      "docs.raw": "Markdown 原文", "docs.repo": "main 分支原文", "docs.skip": "跳到正文",
      "docs.copy": "复制代码", "docs.copied": "已复制", "docs.copyFailed": "复制失败，请选择代码复制"
    },
    en: {
      "docs.catalog": "Catalog", "docs.docs": "Docs", "docs.submit": "Submit",
      "docs.overview": "Overview", "docs.reference": "Reference", "docs.toc": "On this page",
      "docs.raw": "Markdown source", "docs.repo": "Source on main", "docs.skip": "Skip to content",
      "docs.copy": "Copy code", "docs.copied": "Copied", "docs.copyFailed": "Copy failed; select the code to copy"
    }
  };
  const translate = (key) => ui ? ui.t(key) : messages[pageLocale][key];
  if (ui) {
    ui.addMessages(messages);
    // The generic /docs/ entry follows a saved preference; explicit language URLs own their language.
    if (pageLocale === "zh-CN" && ui.locale === "en" && new URL(window.location.href).pathname.endsWith("/docs/")) {
      const target = new URL(body.dataset.docPeer, window.location.href);
      target.hash = window.location.hash;
      if (target.origin === window.location.origin) { window.location.replace(target.href); return; }
    }
    // Opening an explicit English URL must not be redirected by the Chinese default.
    // Do this before subscribing: initialization is not a request to leave this page.
    if (ui.locale !== pageLocale) ui.setLocale(pageLocale);
    ui.apply();
  }
  document.documentElement.lang = pageLocale;

  const languageLinks = [...document.querySelectorAll("[data-doc-language]")];
  const anchorMap = JSON.parse(body.dataset.docAnchors || "{}");
  const peerURL = new URL(body.dataset.docPeer, window.location.href);
  const withHash = (href) => {
    const target = new URL(href, window.location.href);
    let anchor;
    try { anchor = decodeURIComponent(window.location.hash.slice(1)); } catch { anchor = ""; }
    target.hash = target.pathname === peerURL.pathname && Object.prototype.hasOwnProperty.call(anchorMap, anchor) ? anchorMap[anchor] : window.location.hash;
    return target;
  };
  const updateLanguageLinks = () => {
    for (const link of languageLinks) {
      link.href = withHash(link.getAttribute("href")).href;
      if (link.dataset.docLanguage === pageLocale) link.setAttribute("aria-current", "page");
    }
  };
  updateLanguageLinks();
  window.addEventListener("hashchange", updateLanguageLinks);
  window.addEventListener("forge:localechange", (event) => {
    const requested = event.detail?.locale ?? (typeof event.detail === "string" ? event.detail : ui?.locale);
    const locale = requested === "en" ? "en" : "zh-CN";
    if (locale === pageLocale) return;
    const target = withHash(body.dataset.docPeer);
    // Peer is generated, same-origin and constrained to the shared site mount.
    const base = ui?.siteBase ? new URL(ui.siteBase, window.location.href) : null;
    if (target.origin === window.location.origin && (!base || target.pathname.startsWith(base.pathname))) {
      window.location.assign(target.href);
    }
  });

  const fallbackCopy = (text) => {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("aria-hidden", "true");
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.append(textarea);
    textarea.select();
    let copied = false;
    try { copied = document.execCommand("copy"); }
    finally { textarea.remove(); }
    if (!copied) throw new Error("Clipboard unavailable");
  };
  for (const button of document.querySelectorAll("[data-copy]")) {
    button.setAttribute("aria-live", "polite");
    button.addEventListener("click", async () => {
      const code = document.getElementById(button.dataset.copy);
      if (!code) return;
      button.disabled = true;
      try {
        if (navigator.clipboard?.writeText) {
          try { await navigator.clipboard.writeText(code.textContent); }
          catch { fallbackCopy(code.textContent); }
        } else fallbackCopy(code.textContent);
        button.textContent = translate("docs.copied");
      } catch {
        button.textContent = translate("docs.copyFailed");
        const selection = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(code);
        selection.removeAllRanges();
        selection.addRange(range);
      } finally {
        button.disabled = false;
        window.setTimeout(() => { button.textContent = translate("docs.copy"); }, 2500);
      }
    });
  }
})();
