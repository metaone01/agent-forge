/* Live upload acceptance uses an existing Playwright runtime and Chromium executable, never installs dependencies.
 * FORGE_PLAYWRIGHT_PATH=<existing module> FORGE_BROWSER=<existing browser> node --test tests/submit-browser.test.cjs
 */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const root = path.resolve(__dirname, "..");
const runtime = process.env.FORGE_PLAYWRIGHT_PATH;
const browserPath = process.env.FORGE_BROWSER;
const Core = require("../site/assets/submission-core.js");
const example = require("../examples/package-mcp.json");

test("live upload: visual input, five types, JSON preservation, language, theme, draft and long-record fallback", { skip: !runtime || !browserPath, timeout: 120000 }, async () => {
  const { chromium } = require(runtime);
  const site = process.env.FORGE_PREVIEW_ROOT || path.join(root, "site");
  const server = http.createServer((req, res) => {
    let route;
    try { route = decodeURIComponent(new URL(req.url, "http://localhost").pathname).replace(/^\/agent-forge\//, ""); }
    catch (_) { res.writeHead(400); res.end(); return; }
    const base = /^(package|index|source|advisory)\.schema\.json$/.test(route) ? root : site;
    const file = path.resolve(base, route.endsWith("/") ? route + "index.html" : route || "index.html");
    if (!file.startsWith(base + path.sep)) { res.writeHead(403); res.end(); return; }
    fs.readFile(file, (error, data) => {
      if (error) { res.writeHead(404); res.end(); return; }
      res.setHeader("Content-Type", ({ ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css", ".json": "application/json; charset=utf-8" })[path.extname(file)] || "application/octet-stream");
      res.end(data);
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  let browser;
  try {
    browser = await chromium.launch({ executablePath: browserPath, headless: true });
    const context = await browser.newContext({ locale: "en-US", colorScheme: "dark", viewport: { width: 1365, height: 900 }, permissions: ["clipboard-read", "clipboard-write"] });
    const page = await context.newPage(), errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const cdp = await context.newCDPSession(page);
    await cdp.send("Emulation.setFocusEmulationEnabled", { enabled: true });
    await context.route("https://github.com/**", (route) => route.abort());
    const origin = `http://127.0.0.1:${server.address().port}`;
    await page.goto(origin + "/agent-forge/submit/");
    await page.waitForSelector('[data-path="/targets/0/agentId"] input');
    assert.equal(await page.locator("html").getAttribute("lang"), "zh-CN");
    assert.equal(await page.locator("html").getAttribute("data-theme"), "dark");
    assert.equal(await page.locator("[data-forge-theme-option][aria-checked=true]").getAttribute("data-forge-theme-option"), "system");
    await page.emulateMedia({ colorScheme: "light" });
    await page.waitForFunction(() => document.documentElement.dataset.theme === "light");
    assert.equal(await page.locator("html").getAttribute("data-theme"), "light");
    await page.locator('[data-forge-theme-option="dark"]').click();
    await page.emulateMedia({ colorScheme: "light" });
    assert.equal(await page.locator("html").getAttribute("data-theme"), "dark");
    const field = (pointer) => page.locator(`input[data-path="${pointer}"],textarea[data-path="${pointer}"],select[data-path="${pointer}"]`);
    await field("/name").fill("中文包 / visual");
    await field("/id").fill("mcp.visual/中文");
    await field("/version").fill("1.0.0");
    await field("/description").fill("Visual form, no hand-written JSON");
    await field("/license").fill("MIT");
    await field("/targets/0/agentId").fill("dsh");
    await field("/targets/0/compatibilityNote").fill("Upstream does not supply a range.");
    await field("/mcpDetails/identifier").fill("@example/tool");
    await field("/distributions/0/id").fill("source");
    await field("/distributions/0/url").fill("https://example.test/tool");
    await assertEnabled(page, "#create-issue");
    let visual = JSON.parse(await page.locator("#json-preview").innerText());
    assert.equal(visual.name, "中文包 / visual");
    assert.equal(visual.targets[0].agentVersionRange, null);
    assert.equal(visual.publishedAt, undefined);
    assert.deepEqual(Core.validate(visual, require("../package.schema.json")), []);
    await page.locator("[data-forge-locale]").click();
    await page.locator('[data-forge-locale-option="en"]').click();
    assert.equal(await field("/name").inputValue(), "中文包 / visual");
    assert.match(await page.locator("h1").innerText(), /GitHub/);
    await field("/targets/0/compatibilityStatus").selectOption("known");
    await field("/targets/0/agentVersionRange").fill("^1.0.0");
    await assertEnabled(page, "#create-issue");
    for (const type of ["plugin", "skill", "general", "bundle", "mcp"]) {
      await field("/type").selectOption(type);
      assert.equal(JSON.parse(await page.locator("#json-preview").innerText()).type, type);
      assert.equal(await page.locator(`fieldset[data-path="/${type}Details"]`).count(), 1);
      assert.equal(await page.locator('fieldset[data-path="/distributions"]').count(), type === "bundle" ? 0 : 1);
      if (type === "plugin") await field("/pluginDetails/manifestPath").fill("plugin.json");
      if (type === "skill") await field("/skillDetails/skillPath").fill("SKILL.md");
      if (type === "general") { await field("/generalDetails/toolType").fill("evaluation-harness"); await field("/generalDetails/agentUse").fill("Evaluate agent workflows."); }
      if (type === "bundle") await field("/bundleDetails/members/0/memberId").fill("mcp.member");
      await assertEnabled(page, "#create-issue");
    }
    assert.equal(await field("/mcpDetails/identifier").inputValue(), "@example/tool");
    await page.locator("#json-tab").click();
    const extended = Core.clone(example);
    extended.name = "Agent JSON 中文";
    extended.license = ["MIT", "Apache-2.0"];
    extended._meta = { "org.example/custom": { nested: [null, true, { number: 4 }], note: "### preserved\n```data" } };
    await page.locator("#json-input").fill(JSON.stringify(extended, null, 2));
    await assertEnabled(page, "#create-issue");
    await page.locator("#form-tab").click();
    assert.deepEqual(JSON.parse(await page.locator("#json-preview").innerText()), extended);
    await page.locator("#json-tab").click();
    assert.deepEqual(JSON.parse(await page.locator("#json-input").inputValue()), extended);
    await page.locator("#json-input").fill(JSON.stringify({ ...extended, keywords: ["duplicate", "duplicate"] }));
    await page.locator("#form-tab").click();
    assert.equal(await page.locator("#create-issue").isDisabled(), true);
    await page.locator(".submission-error-list button").filter({ hasText: "/keywords:" }).first().click();
    assert.equal(await page.evaluate(() => document.activeElement.dataset.path), "/keywords");
    assert.equal(await page.locator('details[data-editor-path="/keywords"]').getAttribute("open"), "");
    await page.locator("#json-tab").click();
    await page.locator("#json-input").fill(JSON.stringify(extended));
    await assertEnabled(page, "#create-issue");
    await page.locator("#submission-notes").fill("");
    await page.evaluate(() => { window.open = (href) => { window.__submissionHref = href; }; });
    await page.locator("#create-issue").click();
    const url = new URL(await page.evaluate(() => window.__submissionHref));
    assert.deepEqual(JSON.parse(url.searchParams.get("package_json")), extended);
    assert.equal(url.searchParams.has("rationale"), false);
    await page.locator("#copy-json").click();
    assert.deepEqual(JSON.parse(await page.evaluate(() => navigator.clipboard.readText())), extended);
    await page.locator("#submission-notes").fill("Optional notes 中文");
    await page.waitForTimeout(400);
    await page.reload();
    await page.waitForSelector("#schema-fields > fieldset", { state: "attached" });
    assert.equal(await page.locator("[data-forge-locale]").getAttribute("data-locale"), "en");
    assert.equal(await page.locator("[data-forge-theme-option][aria-checked=true]").getAttribute("data-forge-theme-option"), "dark");
    assert.equal(await page.locator("#submission-notes").inputValue(), "Optional notes 中文");
    assert.deepEqual(JSON.parse(await page.locator("#json-preview").innerText()), extended);
    await page.locator("#json-input").fill("{invalid");
    assert.equal(await page.locator("#create-issue").isDisabled(), true);
    await page.locator("#form-tab").click();
    assert.equal(await page.locator("#json-panel").isVisible(), true);
    assert.equal(await page.locator("#json-input").inputValue(), "{invalid");
    await page.waitForTimeout(400);
    await page.reload();
    await page.waitForSelector("#schema-fields > fieldset", { state: "attached" });
    assert.equal(await page.locator("#json-input").inputValue(), "{invalid");
    assert.equal(await page.locator("#create-issue").isDisabled(), true);
    await page.locator("#json-file").setInputFiles({ name: "record.json", mimeType: "application/json", buffer: Buffer.from(JSON.stringify(extended)) });
    await assertEnabled(page, "#create-issue");
    assert.equal(await page.locator("#json-tab").getAttribute("aria-selected"), "true");
    const large = Core.clone(extended); large._meta["org.example/large"] = "中文".repeat(5000);
    await page.locator("#json-input").fill(JSON.stringify(large));
    await assertEnabled(page, "#create-issue");
    assert.equal(await page.locator("#submission-fallback").isVisible(), true);
    await page.evaluate(() => { window.__submissionHref = null; });
    await page.locator("#create-issue").click();
    assert.equal(await page.evaluate(() => window.__submissionHref), null);
    assert.deepEqual(JSON.parse(await page.locator("#json-preview").innerText()), large);
    await page.locator("#json-input").fill(JSON.stringify(extended));
    await page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), "no horizontal page overflow");
    await page.locator("[data-forge-locale]").click();
    await page.locator('[data-forge-locale-option="zh-CN"]').click();
    assert.equal(await page.locator("#json-input").inputValue(), JSON.stringify(extended));
    if (process.env.FORGE_SCREENSHOTS) {
      fs.mkdirSync(process.env.FORGE_SCREENSHOTS, { recursive: true });
      await page.screenshot({ path: path.join(process.env.FORGE_SCREENSHOTS, "upload-mobile-dark.png"), fullPage: true });
      await page.setViewportSize({ width: 1365, height: 900 });
      await page.locator("#form-tab").click();
      await page.screenshot({ path: path.join(process.env.FORGE_SCREENSHOTS, "upload-desktop-dark.png"), fullPage: true });
    }
    assert.deepEqual(errors, []);
    const blocked = await browser.newContext();
    await blocked.addInitScript(() => { Object.defineProperty(window, "localStorage", { get() { throw new Error("Storage denied"); } }); });
    const blockedPage = await blocked.newPage();
    await blockedPage.goto(origin + "/agent-forge/submit/");
    await blockedPage.waitForSelector('[data-path="/name"] input');
    assert.equal(await blockedPage.locator("html").getAttribute("lang"), "zh-CN");
    await blockedPage.locator("#json-tab").click();
    await blockedPage.locator("#json-input").fill(JSON.stringify(example));
    await assertEnabled(blockedPage, "#create-issue");
    await blocked.close();
    await context.close();
  } finally { if (browser) await browser.close(); await new Promise((resolve) => server.close(resolve)); }
});
async function assertEnabled(page, selector) { await page.waitForFunction((value) => { const node = document.querySelector(value); return node && !node.disabled; }, selector); }
