/* Set FORGE_PREVIEW_ROOT, FORGE_PLAYWRIGHT_PATH and FORGE_BROWSER to inspect the built real catalog. */
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const enabled = process.env.FORGE_PREVIEW_ROOT && process.env.FORGE_PLAYWRIGHT_PATH && process.env.FORGE_BROWSER;

test("built real catalog: names, tags, lazy detail, bilingual navigation and mobile layouts", { skip: !enabled, timeout: 120000 }, async () => {
  const { chromium } = require(process.env.FORGE_PLAYWRIGHT_PATH);
  const root = path.resolve(process.env.FORGE_PREVIEW_ROOT);
  const server = http.createServer((req, res) => {
    let route;
    try { route = decodeURIComponent(new URL(req.url, "http://localhost").pathname).replace(/^\/agent-forge\//, ""); }
    catch (_) { res.writeHead(400); res.end(); return; }
    const file = path.resolve(root, route.endsWith("/") ? route + "index.html" : route || "index.html");
    if (!file.startsWith(root + path.sep)) { res.writeHead(403); res.end(); return; }
    fs.readFile(file, (error, data) => {
      if (error) { res.writeHead(404); res.end(); return; }
      res.setHeader("Content-Type", ({ ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css", ".json": "application/json; charset=utf-8" })[path.extname(file)] || "application/octet-stream"); res.end(data);
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  let browser;
  try {
    browser = await chromium.launch({ executablePath: process.env.FORGE_BROWSER, headless: true });
    const context = await browser.newContext({ locale: "en-US", colorScheme: "light", viewport: { width: 1365, height: 900 } });
    const page = await context.newPage(), requests = [], errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("request", (request) => requests.push(request.url()));
    await context.route("https://github.com/**", (route) => route.abort());
    const origin = `http://127.0.0.1:${server.address().port}/agent-forge`;
    await page.goto(origin + "/");
    await page.waitForFunction(() => document.querySelectorAll(".result-card").length > 0);
    assert.equal(await page.locator("html").getAttribute("lang"), "zh-CN");
    assert.equal(await page.locator(".result-card").count(), 50);
    assert.equal(await page.locator("[data-forge-theme-option]").count(), 3);
    assert.equal((await page.locator("[data-forge-theme]").innerText()).trim(), "");
    assert.equal(await page.locator('[data-forge-theme-option="system"]').getAttribute("aria-checked"), "true");
    assert.equal(await page.locator('[data-forge-theme-option="dark"]').getAttribute("title"), "暗色");
    assert.equal(await page.locator('[data-forge-locale] [data-flag="cn"]').count(), 1);
    assert.equal((await page.locator("[data-forge-locale]").innerText()).trim(), "简体中文");
    await page.locator("[data-forge-locale]").focus();
    await page.locator("[data-forge-locale]").press("ArrowDown");
    assert.equal(await page.locator("[data-forge-locale]").getAttribute("aria-expanded"), "true");
    assert.equal(await page.locator('[data-forge-locale-option="en"] [data-flag="us"]').count(), 1);
    await page.keyboard.press("ArrowDown");
    assert.equal(await page.evaluate(() => document.activeElement.dataset.forgeLocaleOption), "en");
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("[data-forge-locale]").getAttribute("aria-expanded"), "false");
    assert.equal(await page.evaluate(() => document.activeElement.hasAttribute("data-forge-locale")), true);
    await page.locator("[data-forge-locale]").click();
    await page.locator("h1").click();
    assert.equal(await page.locator("[data-forge-locale]").getAttribute("aria-expanded"), "false");
    assert.equal(requests.filter((url) => /\/data\/.*\/packages\//.test(url)).length, 0, "cards only use lightweight indexes");
    assert.ok(await page.locator(".result-card .package-id").count() > 0);
    await page.locator("#query").fill("filesystem");
    await page.waitForFunction(() => document.querySelector("#query").value === "filesystem" && document.querySelectorAll(".result-card").length > 0);
    await page.waitForTimeout(250);
    const before = await page.locator("#result-count").innerText();
    await page.locator("[data-forge-locale]").click();
    await page.locator('[data-forge-locale-option="en"]').click();
    assert.equal(await page.locator("#query").inputValue(), "filesystem");
    assert.equal(await page.locator('[data-forge-locale] [data-flag="us"]').count(), 1);
    assert.equal((await page.locator("[data-forge-locale]").innerText()).trim(), "English");
    assert.equal(await page.locator("#result-count").innerText(), before);
    await page.locator(".result-link").first().click();
    await page.waitForSelector(".detail-heading .package-id");
    assert.ok(requests.filter((url) => /\/data\/.*\/packages\//.test(url)).length > 0);
    await page.locator("a[data-doc-entry]").click();
    await page.waitForURL(/\/docs\/index_en\.html/);
    assert.equal(await page.locator("html").getAttribute("lang"), "en");
    await page.goto(origin + "/docs/");
    await page.waitForURL(/\/docs\/index_en\.html/);
    assert.equal(await page.locator("html").getAttribute("lang"), "en");
    await page.goto(origin + "/submit/");
    await page.waitForSelector('[data-path="/name"] input');
    assert.equal(await page.locator("[data-forge-locale]").getAttribute("data-locale"), "en");
    assert.match(await page.locator(".footer a").getAttribute("href"), /package\.schema_en\.html$/);
    await page.locator(".footer a").click();
    await page.waitForURL(/package\.schema_en\.html$/);
    assert.equal(await page.locator("html").getAttribute("lang"), "en");
    await page.locator("[data-forge-locale]").click();
    await page.locator('[data-forge-locale-option="zh-CN"]').click();
    await page.waitForURL(/package\.schema\.html$/);
    assert.equal(await page.locator("html").getAttribute("lang"), "zh-CN");
    await page.goto(origin + "/");
    await page.waitForSelector(".result-card");
    if (process.env.FORGE_SCREENSHOTS) {
      fs.mkdirSync(process.env.FORGE_SCREENSHOTS, { recursive: true });
      await page.screenshot({ path: path.join(process.env.FORGE_SCREENSHOTS, "catalog-desktop-light.png") });
    }
    await page.locator('[data-forge-theme-option="dark"]').click();
    const colors = await page.locator(".result-card").first().evaluate((card) => ({ background: getComputedStyle(card).backgroundColor, color: getComputedStyle(card).color, transition: getComputedStyle(card).transitionProperty }));
    assert.equal(colors.background, "rgb(27, 39, 48)");
    assert.equal(colors.color, "rgb(229, 237, 242)");
    assert.ok(!colors.transition.split(",").some((value) => ["all", "background", "background-color", "color"].includes(value.trim())), "theme colors must change atomically without white-card flash");
    if (process.env.FORGE_SCREENSHOTS) await page.screenshot({ path: path.join(process.env.FORGE_SCREENSHOTS, "catalog-desktop-dark.png") });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await page.locator("[data-forge-locale]").click();
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), "language popup fits narrow screens");
    if (process.env.FORGE_SCREENSHOTS) await page.screenshot({ path: path.join(process.env.FORGE_SCREENSHOTS, "catalog-mobile-dark.png") });
    await page.keyboard.press("Escape");
    assert.deepEqual(errors, []);
    await context.close();
  } finally { if (browser) await browser.close(); await new Promise((resolve) => server.close(resolve)); }
});
