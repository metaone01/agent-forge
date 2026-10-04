/* Run: node --test tests/pages-ui.test.cjs
 * Optional live acceptance with an existing Chromium browser (no npm packages):
 * FORGE_UI_BROWSER=<absolute executable path> node --test tests/pages-ui.test.cjs
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const root = path.resolve(__dirname, '..');
const preferences = fs.readFileSync(path.join(root, 'site/assets/preferences.js'), 'utf8');
const appSource = fs.readFileSync(path.join(root, 'site/assets/app.js'), 'utf8');

class NodeStub {
  constructor(attrs = {}) { this.attrs = { ...attrs }; this.dataset = {}; this.style = {}; this.children = []; this.value = ''; this.textContent = ''; this.innerHTML = ''; this.listeners = {}; }
  getAttribute(key) { return this.attrs[key] ?? null; }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  removeAttribute(key) { delete this.attrs[key]; }
  matches(selector) { return selector.startsWith('[') && selector.endsWith(']') && Object.hasOwn(this.attrs, selector.slice(1, -1)); }
  querySelectorAll(selector) { return this.children.flatMap((child) => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]); }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  appendChild(child) { this.children.push(child); }
}
function environment({ stored = {}, storageThrows = false, dark = false, legacyMedia = false, page = 'catalog', fetch = async () => ({ ok: false }) } = {}) {
  const document = new NodeStub();
  const nodes = new Map();
  document.currentScript = { src: 'https://example.test/project/assets/preferences.js' };
  document.documentElement = new NodeStub();
  document.body = new NodeStub(); document.body.dataset.page = page;
  document.readyState = 'loading';
  document.getElementById = (id) => nodes.get(id) || null;
  document.querySelector = (selector) => selector === 'main' ? nodes.get('main') : null;
  const events = [];
  const listeners = {};
  const media = { matches: dark };
  const mediaListener = (name, fn) => { media.listener = fn; };
  if (legacyMedia) media.addListener = (fn) => { media.listener = fn; }; else media.addEventListener = mediaListener;
  const window = { matchMedia: () => media, dispatchEvent: (event) => { events.push(event); (listeners[event.type] || []).forEach((fn) => fn(event)); }, addEventListener: (name, fn) => { (listeners[name] ||= []).push(fn); }, scrollTo() {} };
  const localStorage = { getItem(key) { if (storageThrows) throw Error('blocked'); return stored[key]; }, setItem(key, value) { if (storageThrows) throw Error('quota'); stored[key] = value; } };
  const context = vm.createContext({ window, document, localStorage, navigator: { language: 'en-US' }, CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } }, URL, URLSearchParams, Intl, console, fetch, location: { search: '', hash: '', pathname: '/project/' }, setTimeout, clearTimeout });
  vm.runInContext(preferences, context);
  return { context, ui: window.ForgeUI, window, document, nodes, media, events, stored };
}
function appEnvironment(options) {
  const env = environment(options);
  const exposed = appSource.replace('  if (page === "dashboard") startDashboard(); else if (page === "agent-dashboard") startAgentDashboard(); else startCatalog();', '  globalThis.app = { state, normalizeIndex, dataPathFor, safeHref, safeImageURL, imageSlot, renderGallery, renderPackageTags, hydrate, renderResults, renderDetail, normalizeDashboard, renderDashboard, renderAgentDashboard, startAgentDashboard };');
  vm.runInContext(exposed, env.context);
  env.app = env.context.app;
  return env;
}
function addNodes(env, ids) { for (const id of ids) env.nodes.set(id, new NodeStub()); }

test('defaults ignore browser language, resolve system theme synchronously, and derive project root', () => {
  const { ui, document } = environment({ dark: true });
  assert.equal(ui.locale, 'zh-CN'); assert.equal(ui.theme, 'system');
  assert.equal(document.documentElement.lang, 'zh-CN'); assert.equal(document.documentElement.dataset.theme, 'dark');
  assert.equal(document.documentElement.style.colorScheme, 'dark'); assert.ok(ui.siteBase instanceof URL);
  assert.equal(ui.siteBase.href, 'https://example.test/project/');
});
 test('explicit choices persist, language and theme events contain current choices', () => {
  const env = environment(); env.ui.setLocale('en'); env.ui.setTheme('dark');
  assert.deepEqual(env.stored, { 'forge:locale': 'en', 'forge:theme': 'dark' });
  assert.deepEqual(env.events.map((event) => event.type), ['forge:localechange', 'forge:themechange']);
  assert.equal(env.events[0].detail.locale, 'en'); assert.equal(env.events[1].detail.resolved, 'dark');
  assert.equal(environment({ stored: env.stored }).ui.locale, 'en');
  assert.equal(environment({ stored: env.stored }).ui.theme, 'dark');
});
 test('invalid persisted values and blocked storage safely fall back', () => {
  const invalid = environment({ stored: { 'forge:locale': 'fr', 'forge:theme': 'invalid' } });
  assert.equal(invalid.ui.locale, 'zh-CN'); assert.equal(invalid.ui.theme, 'system');
  const env = environment({ storageThrows: true });
  env.ui.setLocale('en'); env.ui.setTheme('dark'); assert.equal(env.ui.locale, 'en'); assert.equal(env.document.documentElement.dataset.theme, 'dark');
  env.ui.setLocale('invalid'); env.ui.setTheme('invalid'); assert.equal(env.ui.locale, 'zh-CN'); assert.equal(env.ui.theme, 'system');
});
 test('system changes update only system mode, including legacy media listeners', () => {
  for (const legacyMedia of [false, true]) {
    const env = environment({ legacyMedia });
    env.media.matches = true; env.media.listener(); assert.equal(env.document.documentElement.dataset.theme, 'dark');
    env.ui.setTheme('light'); env.media.listener(); assert.equal(env.document.documentElement.dataset.theme, 'light');
    env.ui.setTheme('system'); assert.equal(env.document.documentElement.dataset.theme, 'dark');
  }
});
 test('translation registration, interpolation and fallback are safe plain text', () => {
  const { ui } = environment();
  ui.addMessages({ 'zh-CN': { hello: '你好 {name}', onlyChinese: '回退' }, en: { hello: 'Hello {name}' } });
  assert.equal(ui.t('hello', { name: '<img>' }), '你好 <img>'); ui.setLocale('en');
  assert.equal(ui.t('hello', { name: 'world' }), 'Hello world'); assert.equal(ui.t('onlyChinese'), '回退'); assert.equal(ui.t('missing'), 'missing');
});
 test('apply translates roots, placeholders, titles, aria labels, metadata and alt without changing input values', () => {
  const { ui } = environment();
  const node = new NodeStub({ 'data-i18n': 'nav.catalog', 'data-i18n-placeholder': 'nav.docs', 'data-i18n-title': 'nav.submit', 'data-i18n-aria-label': 'nav.label', 'data-i18n-content': 'nav.dashboard', 'data-i18n-alt': 'nav.docs' });
  node.value = 'draft'; ui.setLocale('en'); ui.apply(node);
  assert.equal(node.textContent, 'Catalog'); assert.equal(node.attrs.placeholder, 'Docs'); assert.equal(node.attrs.title, 'Submit package');
  assert.equal(node.attrs['aria-label'], 'Main navigation'); assert.equal(node.attrs.content, 'Dashboard'); assert.equal(node.attrs.alt, 'Docs'); assert.equal(node.value, 'draft');
});
 test('preference controls use vector flags, endonyms, and icon-only theme labels', () => {
  const { ui } = environment();
  const root = new NodeStub();
  const locale = new NodeStub({ 'data-forge-locale': '' });
  const group = new NodeStub({ 'data-forge-theme': '' });
  const options = ['light', 'dark', 'system'].map((mode) => {
    const node = new NodeStub({ 'data-forge-theme-option': mode }); node.dataset.forgeThemeOption = mode; return node;
  });
  root.children = [locale, group, ...options];
  ui.apply(root);
  assert.equal(locale.dataset.locale, 'zh-CN');
  assert.match(locale.innerHTML, /data-flag="cn"/); assert.match(locale.innerHTML, /简体中文/);
  assert.equal(group.getAttribute('aria-label'), '主题');
  assert.deepEqual(options.map((node) => node.getAttribute('aria-checked')), ['false', 'false', 'true']);
  assert.deepEqual(options.map((node) => node.tabIndex), [-1, -1, 0]);
  assert.equal(options[1].getAttribute('title'), '暗色'); assert.equal(options[1].textContent, '');
  ui.setLocale('en'); ui.setTheme('dark'); ui.apply(root);
  assert.match(locale.innerHTML, /data-flag="us"/); assert.match(locale.innerHTML, /English/);
  assert.equal(group.getAttribute('aria-label'), 'Theme'); assert.equal(options[1].getAttribute('aria-label'), 'Dark');
  assert.deepEqual(options.map((node) => node.getAttribute('aria-checked')), ['false', 'true', 'false']);
});
 test('all three HTML pages load ordinary preferences before CSS, and expose local docs/submit navigation', () => {
  for (const page of ['site/index.html', 'site/dashboard/index.html', 'site/dashboard/agent/index.html']) {
    const html = fs.readFileSync(path.join(root, page), 'utf8');
    const script = html.match(/<script src="([^"]*preferences\.js)"([^>]*)><\/script>/);
    assert.ok(script); assert.equal(script[2], ''); assert.ok(html.indexOf(script[0]) < html.indexOf('<link rel="stylesheet"'));
    const pageUrl = new URL(page.replace(/^site\//, ''), 'https://example.test/project/');
    assert.equal(new URL('../', new URL(script[1], pageUrl)).href, 'https://example.test/project/');
    for (const destination of ['docs/', 'submit/']) assert.ok(html.includes(destination));
    assert.doesNotMatch(html, /<(title|option)[^>]*>\s*<span/);
    const { ui } = environment();
    for (const [, key] of html.matchAll(/data-i18n(?:-[a-z-]+)?="([^"]+)"/g)) {
      // A Chinese key can equal its default translation; English must not silently fall back.
      ui.setLocale('en'); assert.doesNotMatch(ui.t(key), /[\u3400-\u9fff]/, key);
    }
  }
});
 test('normalizeIndex separates canonical id, internal key, name and legacy name-key routes', () => {
  const env = appEnvironment();
  const [entry] = env.app.normalizeIndex({ agentId: 'dsh', type: 'mcp', packages: { 'route/工具': { id: 'canonical.tool', name: '真实 name', displayName: '展示标题', path: 'p.json', customFacets: { team: ['core'] } } } }, 'https://example.test/project/data/dsh/mcp/index.json');
  assert.equal(entry.id, 'canonical.tool'); assert.equal(entry.packageId, 'canonical.tool'); assert.equal(entry.key, 'dsh:mcp:route/工具');
  assert.equal(entry.name, '真实 name'); assert.equal(entry.routeName, 'route/工具'); assert.equal(entry.customFacets.team[0], 'core');
  assert.equal(entry.path, 'https://example.test/project/data/dsh/mcp/p.json');
  const [noId] = env.app.normalizeIndex({ packages: { anonymous: {} } }, 'https://example.test/project/data/index.json');
  assert.equal(noId.packageId, '');
});
 test('data paths preserve /data-relative contracts and links reject unsafe protocols', () => {
  const { app } = appEnvironment();
  assert.equal(app.dataPathFor('dsh/mcp/index.json'), 'https://example.test/project/data/dsh/mcp/index.json');
  assert.equal(app.dataPathFor('/data/dsh/mcp/index.json'), 'https://example.test/project/data/dsh/mcp/index.json');
  assert.equal(app.safeHref('javascript:alert(1)'), '#'); assert.equal(app.safeHref('https://example.test/'), 'https://example.test/');
});
 test('capsules preserve controlled/custom/keywords order, escape values, omit empty groups and limit cards only', () => {
  const { app, ui } = appEnvironment();
  const record = { facets: { runtime: ['node', 'python'], empty: [] }, customFacets: { team: ['<script>', null, ''] }, keywords: ['fast', 'free'] };
  const full = app.renderPackageTags(record); assert.ok(full.indexOf('tag-controlled') < full.indexOf('tag-custom')); assert.ok(full.indexOf('tag-custom') < full.indexOf('tag-keywords'));
  assert.match(full, /&lt;script&gt;/); assert.doesNotMatch(full, /empty:|null/); assert.equal((full.match(/class="tag tag-/g) || []).length, 5);
  ui.setLocale('en'); const card = app.renderPackageTags(record, 3); assert.match(card, /2 more tags/); assert.doesNotMatch(card, />fast</); assert.equal(app.renderPackageTags({ facets: { empty: [] }, keywords: [''] }), '');
});
 test('cards prefer displayName, fall back to name, hide canonical IDs and preserve pagination', () => {
  const env = appEnvironment(); addNodes(env, ['results', 'empty-state', 'result-count', 'catalog-status', 'revision-stamp', 'pagination', 'page-number', 'page-total', 'page-range', 'previous-page', 'next-page', 'query']);
  const entries = env.app.normalizeIndex({ agentId: 'dsh', type: 'mcp', packages: Object.fromEntries(Array.from({ length: 80 }, (_, i) => ['name-' + i, { id: 'canonical-' + i, displayName: i === 50 ? '展示 <包名>' : i === 51 ? '' : undefined, keywords: ['test'] }])) }, 'https://example.test/project/data/dsh/mcp/index.json');
  env.app.state.entries = entries; env.app.state.filtered = entries; env.app.state.resultPage = 1; env.nodes.get('query').value = 'unchanged';
  env.app.renderResults(); env.ui.setLocale('en'); env.app.renderResults();
  assert.equal(env.app.state.resultPage, 1); assert.equal(env.nodes.get('query').value, 'unchanged'); assert.equal(env.nodes.get('page-number').value, 2);
  assert.match(env.nodes.get('results').innerHTML, /<strong>展示 &lt;包名&gt;<\/strong>/); assert.match(env.nodes.get('results').innerHTML, /<strong>name-51<\/strong>/); assert.match(env.nodes.get('results').innerHTML, /<strong>name-52<\/strong>/); assert.doesNotMatch(env.nodes.get('results').innerHTML, /package-id|canonical-/); assert.match(env.nodes.get('results').innerHTML, /#\/package\/dsh\/mcp\/name-50/);
  assert.match(env.nodes.get('page-total').textContent, /pages/);
});
 test('hydrate shares concurrent requests and preserves canonical ID, name and displayName', async () => {
  let calls = 0;
  const env = appEnvironment({ fetch: async () => { calls++; return { ok: true, json: async () => ({ id: 'canonical.hydrated', name: 'package name', displayName: '展示标题' }) }; } });
  const entry = { key: 'dsh:mcp:route', packageId: 'canonical.index', name: 'route', path: 'https://example.test/p.json' };
  const [first, second] = await Promise.all([env.app.hydrate(entry), env.app.hydrate(entry)]);
  assert.equal(calls, 1); assert.equal(first, second); assert.equal(first.packageId, 'canonical.hydrated'); assert.equal(first.name, 'package name'); assert.equal(first.displayName, '展示标题');
  await env.app.hydrate(entry); assert.equal(calls, 1);
});
 test('detail and recent cards restore display names while keeping canonical identity separate', async () => {
  const record = { id: 'plugin.hash', name: 'owner/repo', displayName: '<真实包名>', version: '1.0.0', pluginDetails: {} };
  const env = appEnvironment({ fetch: async () => ({ ok: true, json: async () => record }) });
  addNodes(env, ['main', 'agent-title', 'agent-description', 'global-metrics', 'agent-type-chart', 'agent-facet-chart', 'recent-packages']);
  const [entry] = env.app.normalizeIndex({ agentId: 'dsh', type: 'plugin', packages: { 'owner/repo': { ...record, path: 'p.json' } } }, 'https://example.test/project/data/dsh/plugin/index.json');
  env.app.state.entries = [entry];
  await env.app.renderDetail('dsh', 'plugin', 'owner/repo');
  assert.match(env.nodes.get('main').innerHTML, /<h1>&lt;真实包名&gt;<\/h1>/);
  assert.doesNotMatch(env.nodes.get('main').innerHTML, /class="package-id"/);
  assert.match(env.nodes.get('main').innerHTML, /<dt>包名<\/dt><dd>owner\/repo<\/dd>/);
  assert.doesNotMatch(env.nodes.get('main').innerHTML, /plugin.hash|Package ID/);
  env.ui.setLocale('en');
  await env.app.renderDetail('dsh', 'plugin', 'owner/repo');
  assert.match(env.nodes.get('main').innerHTML, /<dt>Package name<\/dt><dd>owner\/repo<\/dd>/);
  assert.doesNotMatch(env.nodes.get('main').innerHTML, /plugin.hash|Package ID/);
  assert.equal(env.app.state.detailCache.get(entry.key).id, 'plugin.hash');
  assert.equal(entry.routeName, 'owner/repo');
  env.app.renderAgentDashboard({ id: 'dsh', global: {}, agent: { counts: {}, byType: {}, facets: {}, recentPackages: [record, { ...record, displayName: '' }] } });
  const recent = env.nodes.get('recent-packages').innerHTML;
  assert.match(recent, /<strong>&lt;真实包名&gt;<\/strong>/);
  assert.match(recent, /<strong>owner\/repo<\/strong>/);
  assert.doesNotMatch(recent, /plugin.hash|package-id/);
});
 test('package-name facts escape names and fall back to the route without exposing internal IDs', async () => {
  for (const packageName of ['owner/<repo>', undefined]) {
    const env = appEnvironment({ fetch: async () => ({ ok: true, json: async () => ({ id: 'plugin.internal-hash', ...(packageName ? { name: packageName } : {}), displayName: 'Display title', pluginDetails: {} }) }) });
    addNodes(env, ['main']);
    const [entry] = env.app.normalizeIndex({ agentId: 'dsh', type: 'plugin', packages: { 'owner/route': { id: 'plugin.internal-hash', path: 'p.json' } } }, 'https://example.test/project/data/dsh/plugin/index.json');
    env.app.state.entries = [entry];
    await env.app.renderDetail('dsh', 'plugin', 'owner/route');
    assert.match(env.nodes.get('main').innerHTML, packageName ? /<dt>包名<\/dt><dd>owner\/&lt;repo&gt;<\/dd>/ : /<dt>包名<\/dt><dd>owner\/route<\/dd>/);
    assert.doesNotMatch(env.nodes.get('main').innerHTML, /plugin.internal-hash|Package ID|<dd>Display title/);
    assert.equal(env.app.state.detailCache.get(entry.key).id, 'plugin.internal-hash');
    assert.equal(entry.routeName, 'owner/route');
  }
});
 test('stale detail responses cannot replace a newer route', async () => {
  let resolve;
  const env = appEnvironment({ fetch: () => new Promise((done) => { resolve = done; }) }); addNodes(env, ['main']);
  env.app.state.entries = [{ key: 'x', routeName: 'name', name: 'name', agentId: 'dsh', type: 'mcp', path: 'https://example.test/p.json' }];
  const request = env.app.renderDetail('dsh', 'mcp', 'name'); env.app.state.detailRoute++;
  env.nodes.get('main').innerHTML = 'newer route'; resolve({ ok: true, json: async () => ({ id: 'canonical', name: 'name' }) }); await request;
  assert.equal(env.nodes.get('main').innerHTML, 'newer route');
});
 test('dashboard and agent dashboard retain rendered data when language changes', async () => {
  const raw = { agents: [{ id: 'dsh', packageCount: 2 }], counts: { packages: 2, versions: 3 }, byType: { mcp: 2 } };
  const env = appEnvironment({ fetch: async () => ({ ok: true, json: async () => raw }) });
  addNodes(env, ['dashboard-updated', 'global-metrics', 'agent-chart', 'agent-table', 'type-chart', 'type-table', 'facet-chart', 'compat-chart', 'agent-cards', 'agent-title', 'agent-description', 'agent-type-chart', 'agent-facet-chart', 'recent-packages']);
  env.app.state.dashboard = env.app.normalizeDashboard(raw); env.app.state.ready = true; env.app.renderDashboard(env.app.state.dashboard);
  env.ui.setLocale('en'); assert.match(env.nodes.get('global-metrics').innerHTML, /Packages/); assert.match(env.nodes.get('agent-table').innerHTML, /Package count by Agent/);
  assert.match(env.nodes.get('agent-cards').innerHTML, /https:\/\/example.test\/project\/dashboard\/agent\/\?id=dsh/);
  env.app.state.dashboard = null; await env.app.startAgentDashboard(); assert.equal(env.nodes.get('agent-title').textContent, 'dsh');
  env.ui.setLocale('zh-CN'); assert.match(env.nodes.get('agent-description').textContent, /Agent dsh/); assert.match(env.nodes.get('recent-packages').innerHTML, /暂无最近更新记录/);
});

test('live Chromium acceptance: controls, routing, persistence, themes, nested paths and mobile layout', { skip: !process.env.FORGE_UI_BROWSER, timeout: 60000 }, async () => {
  const http = require('node:http');
  const os = require('node:os');
  const { spawn } = require('node:child_process');
  const timers = require('node:timers/promises');
  const recordTags = { facets: { capability: ['a', 'b', 'c'], runtime: ['node', 'python'] }, customFacets: { team: ['owner', 'UI'] }, keywords: ['fast', 'free', 'public'] };
  const mediaIndex = { icon: { url: 'https://media.example.test/icon.png', alt: 'Skin icon' }, previews: [{ url: 'https://media.example.test/dark.png', alt: 'Dark skin preview', theme: 'dark' }] };
  const mediaRequests = [], unexpectedImageRequests = [], sourceRequests = [], sourceGenerations = {};
  const packages = Object.fromEntries(Array.from({ length: 123 }, (_, i) => {
    const name = 'tool/工具-' + String(i).padStart(3, '0');
    return [name, { id: 'canonical.tool-' + i, name, displayName: i % 2 === 0 ? '展示工具-' + String(i).padStart(3, '0') : undefined, latest: '1.0.0', versions: ['1.0.0'], path: 'p/' + i + '.json', summary: 'Searchable package', ...recordTags, subtype: 'skin', media: [0, 50].includes(i) ? mediaIndex : i === 1 ? { icon: { url: 'javascript:alert(1)', alt: 'Invalid' } } : undefined }];
  }));
  const requests = [];
  const agentDashboard = { counts: { packages: 123, versions: 123 }, byType: { plugin: 123 }, facets: { node: 123 }, recentPackages: [{ id: 'canonical.recent', name: 'recent name', displayName: '最近展示包名', version: '1.0.0', ...recordTags }] };
  const server = http.createServer((req, res) => {
    const pathname = new URL(req.url, 'http://localhost').pathname;
    if (!pathname.startsWith('/project/')) { res.writeHead(404).end(); return; }
    const relative = pathname.slice('/project/'.length);
    const builtPrefix = relative.startsWith('sample/') ? 'sample/' : relative.startsWith('full/') ? 'full/' : '';
    const builtSite = builtPrefix === 'sample/' ? process.env.FORGE_MEDIA_SAMPLE_SITE : process.env.FORGE_MEDIA_FULL_SITE;
    if (builtPrefix && builtSite) {
      const sampleRoot = path.resolve(builtSite);
      const route = decodeURIComponent(relative.slice(builtPrefix.length));
      const file = path.resolve(sampleRoot, route.endsWith('/') || !route ? route + 'index.html' : route);
      if (!file.startsWith(sampleRoot + path.sep)) { res.writeHead(403).end(); return; }
      try { res.setHeader('Content-Type', ({ '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.html': 'text/html' })[path.extname(file)] || 'application/octet-stream'); res.end(fs.readFileSync(file)); } catch (_) { res.writeHead(404).end(); }
      return;
    }
    if (relative.startsWith('data/')) {
      requests.push(relative);
      let data;
      if (relative === 'data/manifest.json') data = { revision: 'test', sources: [{ path: 'dsh/plugin/index.json' }] };
      else if (relative === 'data/dsh/plugin/index.json') data = { agentId: 'dsh', type: 'plugin', packages };
      else if (/^data\/dsh\/plugin\/p\/\d+\.json$/.test(relative)) {
        const i = Number(relative.match(/(\d+)\.json$/)[1]); data = { ...Object.values(packages)[i], version: '1.0.0', description: 'Searchable package', media: [0, 50].includes(i) ? { ...mediaIndex, previews: [...mediaIndex.previews, { url: 'https://media.example.test/light.png', alt: 'Light skin preview', theme: 'light' }, { url: 'https://media.example.test/broken.png', alt: 'Unavailable preview' }] } : undefined, links: { docs: 'javascript:alert(1)' } };
      } else if (relative === 'data/dashboard.json') data = { ...agentDashboard, agents: [{ id: 'dsh', name: 'dsh', packageCount: 123, types: { plugin: 123 } }] };
      else if (relative === 'data/agents/dsh/dashboard.json') data = agentDashboard;
      if (data) { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(data)); } else res.writeHead(404).end();
      return;
    }
    if (relative === 'package.schema.json') { res.setHeader('Content-Type', 'application/json'); res.end(fs.readFileSync(path.join(root, relative))); return; }
    if (relative === 'submit-real/') { res.setHeader('Content-Type', 'text/html'); res.end(fs.readFileSync(path.join(root, 'site/submit/index.html'))); return; }
    // Reuse contract on future pages without writing pages owned by the main task.
    if (relative === 'docs/' || relative === 'submit/') {
      res.setHeader('Content-Type', 'text/html');
      res.end('<!doctype html><html><head><script src="../assets/preferences.js"></script><link rel="stylesheet" href="../assets/styles.css"></head><body><nav class="topnav"><a data-i18n="nav.docs">文档</a></nav><input id="draft" value="draft"></body></html>'); return;
    }
    const local = path.resolve(root, 'site', relative.endsWith('/') || !relative ? relative + 'index.html' : relative);
    if (!local.startsWith(path.resolve(root, 'site') + path.sep)) { res.writeHead(403).end(); return; }
    try { res.setHeader('Content-Type', local.endsWith('.js') ? 'application/javascript' : local.endsWith('.css') ? 'text/css' : 'text/html'); res.end(fs.readFileSync(local)); } catch (_) { res.writeHead(404).end(); }
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const base = 'http://127.0.0.1:' + server.address().port + '/project/';
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'forge-pages-ui-'));
  const child = spawn(process.env.FORGE_UI_BROWSER, ['--headless=new', '--no-first-run', '--no-default-browser-check', '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'], { windowsHide: true, stdio: 'ignore' });
  let socket;
  let send, acceptanceFailure;
  try {
    let debug;
    for (let i = 0; i < 100; i++) {
      try { debug = fs.readFileSync(path.join(profile, 'DevToolsActivePort'), 'utf8').trim().split(/\r?\n/); break; } catch (_) { await timers.setTimeout(100); }
    }
    assert.ok(debug, 'Browser did not expose a debugging port');
    socket = new WebSocket('ws://127.0.0.1:' + debug[0] + debug[1]);
    await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }); });
    let next = 0;
    const pending = new Map();
    const exceptions = [];
    socket.addEventListener('message', (event) => {
      const message = JSON.parse(event.data);
      if (message.method === 'Runtime.exceptionThrown') exceptions.push(message.params.exceptionDetails.text);
      if (message.method === 'Fetch.requestPaused') {
        const params = message.params;
        if (params.request.url.startsWith('https://api.github.com/repos/author/')) {
          sourceRequests.push(params.request);
          const url = new URL(params.request.url), parts = url.pathname.split('/'), repo = parts[3], route = '/' + parts.slice(4).join('/'), commit = 'a'.repeat(40);
          const sourceFile = (name, text) => ({ type: 'file', path: name, encoding: 'base64', size: Buffer.byteLength(text), content: Buffer.from(text).toString('base64') });
          let data, status = 200;
          if (repo === 'limited') { status = 403; data = { message: 'API rate limit exceeded' }; }
          else if (route === '/') { sourceGenerations[repo] = (sourceGenerations[repo] || 0) + 1; data = { default_branch: 'main', description: 'Upstream description', license: { spdx_id: 'MIT' } }; }
          else if (route === '/commits/main') data = { sha: commit };
          else if (route === '/contents/') data = [{ type: 'file', name: 'plugin.json', path: 'plugin.json' }, { type: 'file', name: repo === 'ambiguous' ? 'manifest.json' : 'README.md', path: repo === 'ambiguous' ? 'manifest.json' : 'README.md' }];
          else if (route === '/contents/skin-gallery') data = [{ type: 'file', name: 'plugin.json', path: 'skin-gallery/plugin.json' }, { type: 'file', name: 'README.md', path: 'skin-gallery/README.md' }];
          else if (['/contents/plugin.json', '/contents/skin-gallery/plugin.json', '/contents/manifest.json'].includes(route)) data = sourceFile(route.slice('/contents/'.length), JSON.stringify({ name: repo === 'other' ? 'Other skin' : 'Fetched skin', version: repo === 'refresh' ? sourceGenerations[repo] + '.0.0' : '1.2.3', description: 'Declared description', ...(['no-icon', 'other'].includes(repo) ? {} : { icon: { url: 'https://media.example.test/icon.png', alt: 'Declared icon' } }), preview: repo === 'other' ? 'https://media.example.test/other.png' : 'https://media.example.test/dark.png' }));
          else if (route === '/readme' && repo === 'partial') { data = {}; status = 500; }
          else if (route === '/readme') data = sourceFile('README.md', repo === 'other' ? '# Preview\n![Other](https://media.example.test/other.png)' : '# Preview\n![Light screenshot](https://media.example.test/light.png)\n\n# Development\n![Diagram](https://media.example.test/diagram.png)\n![badge](https://img.shields.io/badge/status-ok)');
          else { data = {}; status = 404; }
          const fulfill = () => send('Fetch.fulfillRequest', { requestId: params.requestId, responseCode: status, responseHeaders: [{ name: 'Content-Type', value: 'application/json' }, { name: 'Access-Control-Allow-Origin', value: '*' }], body: Buffer.from(JSON.stringify(data)).toString('base64') }, message.sessionId).catch(() => {});
          if (repo === 'slow') setTimeout(fulfill, 250); else fulfill();
          return;
        }
        if (!params.request.url.startsWith('https://media.example.test/')) {
          unexpectedImageRequests.push(params.request.url);
          send('Fetch.failRequest', { requestId: params.requestId, errorReason: 'BlockedByClient' }, message.sessionId).catch(() => {});
          return;
        }
        mediaRequests.push(params.request);
        const failure = params.request.url.endsWith('/broken.png');
        const dark = params.request.url.endsWith('/dark.png');
        const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540"><rect width="960" height="540" fill="' + (dark ? '#18202c' : '#edf2f7') + '"/><rect x="24" y="24" width="180" height="492" rx="12" fill="' + (dark ? '#273345' : '#d9e4ef') + '"/><rect x="230" y="80" width="690" height="110" rx="14" fill="#4876a7"/><rect x="230" y="210" width="500" height="80" rx="14" fill="' + (dark ? '#33445b' : '#c5d6e6') + '"/><text x="240" y="130" fill="white" font-family="sans-serif" font-size="28">Static skin preview fixture</text></svg>';
        send('Fetch.fulfillRequest', { requestId: params.requestId, responseCode: failure ? 404 : 200, responseHeaders: [{ name: 'Content-Type', value: failure ? 'text/plain' : 'image/svg+xml' }], body: Buffer.from(failure ? 'missing image' : svg).toString('base64') }, message.sessionId).catch(() => {});
        return;
      }
      if (!message.id) return;
      const callback = pending.get(message.id); pending.delete(message.id);
      if (message.error) callback.reject(Error(JSON.stringify(message.error))); else callback.resolve(message.result);
    });
    send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
      const id = ++next; pending.set(id, { resolve, reject }); socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    });
    const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
    const cdp = (method, params) => send(method, params, sessionId);
    await cdp('Runtime.enable'); await cdp('Page.enable');
    await cdp('Emulation.setDeviceMetricsOverride', { width: 1365, height: 1000, deviceScaleFactor: 1, mobile: false });
    await cdp('Fetch.enable', { patterns: [{ urlPattern: 'https://*', requestStage: 'Request' }] });
    const evaluate = async (expression) => {
      const result = await cdp('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
      if (result.exceptionDetails) throw Error(result.exceptionDetails.text + ': ' + result.result.description);
      return result.result.value;
    };
    const waitFor = async (expression) => {
      for (let i = 0; i < 200; i++) { if (await evaluate(expression)) return; await timers.setTimeout(50); }
      assert.fail('Timed out: ' + expression + ' | source status: ' + await evaluate('document.getElementById("source-status")?.textContent'));
    };
    const screenshot = async (name) => {
      if (!process.env.FORGE_MEDIA_SCREENSHOTS) return;
      const dir = path.resolve(process.env.FORGE_MEDIA_SCREENSHOTS); fs.mkdirSync(dir, { recursive: true });
      let options = { format: 'png' };
      if (name.startsWith('source-submit-')) {
        const box = await evaluate('(()=>{const r=document.querySelector(".source-import").getBoundingClientRect();return {x:r.x+scrollX,y:r.y+scrollY,width:r.width,height:r.height}})()');
        options = { ...options, captureBeyondViewport: true, clip: { ...box, scale: 1 } };
      } else if (name.includes('detail')) {
        const metrics = await cdp('Page.getLayoutMetrics');
        const size = metrics.cssContentSize;
        options = { ...options, captureBeyondViewport: true, clip: { x: 0, y: 0, width: size.width, height: size.height, scale: 1 } };
      }
      const image = await cdp('Page.captureScreenshot', options);
      fs.writeFileSync(path.join(dir, name), Buffer.from(image.data, 'base64'));
    };
    const navigate = async (suffix, ready, reload = false) => {
      const url = new URL(suffix, base).href;
      await cdp('Page.navigate', { url });
      if (reload) {
        await evaluate('globalThis.__forgeReloadMarker = true');
        await cdp('Page.reload', { ignoreCache: true });
      }
      // Navigation can leave the old document visible briefly. Probe only the target
      // URL after its parser finishes, and let missing async-rendered nodes mean not ready.
      await waitFor((reload ? 'globalThis.__forgeReloadMarker !== true && ' : '') + 'location.href === ' + JSON.stringify(url) + ' && document.readyState === "complete" && (' + ready + ')');
    };
    await cdp('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: 'dark' }] });
    await navigate('', 'document.querySelectorAll(".result-card").length === 50');
    assert.deepEqual(await evaluate('({locale:ForgeUI.locale,theme:ForgeUI.theme,resolved:document.documentElement.dataset.theme,base:ForgeUI.siteBase.href,controls:document.querySelectorAll(".forge-preferences").length})'), { locale: 'zh-CN', theme: 'system', resolved: 'dark', base, controls: 1 });
    assert.equal(await evaluate('document.querySelector(".result-title strong").textContent'), '展示工具-000');
    assert.equal(await evaluate('document.querySelectorAll(".package-id").length'), 0);
    assert.equal(await evaluate('document.querySelector(".result-card .tag-more").textContent'), '另有 2 个标签');
    assert.equal(await evaluate('document.querySelector(".result-card .tag-list").children.length'), 9);
    assert.equal(mediaRequests.length, 0);
    assert.equal(await evaluate('document.querySelectorAll(".media-slot img").length'), 0);
    await evaluate('document.querySelector("[data-media-consent]").click()');
    await waitFor('Array.from(document.querySelectorAll(".media-slot img")).some(img => img.complete && img.naturalWidth > 0)');
    assert.ok(mediaRequests.length > 0);
    assert.ok(mediaRequests.every(request => !Object.keys(request.headers).some(key => key.toLowerCase() === 'referer')));
    assert.equal(await evaluate('document.querySelectorAll("img[src^=javascript]").length'), 0);
    await screenshot('media-list-desktop-dark.png');
    await evaluate('document.querySelector("[data-media-consent]").click()');
    assert.equal(await evaluate('document.querySelectorAll(".media-slot img").length'), 0);
    await evaluate('document.getElementById("query").value="Searchable"; document.getElementById("query").dispatchEvent(new Event("input",{bubbles:true}));');
    await timers.setTimeout(200);
    await evaluate('document.getElementById("next-page").click()');
    assert.equal(await evaluate('document.getElementById("page-number").value'), '2');
    const beforeLocaleRequests = requests.length;
    await evaluate('document.querySelector("[data-forge-locale]").click(); document.querySelector("[data-forge-locale-option=en]").click();');
    assert.equal(await evaluate('document.getElementById("query").value'), 'Searchable');
    assert.equal(await evaluate('document.getElementById("page-number").value'), '2');
    assert.equal(await evaluate('document.getElementById("query").placeholder'), 'Search names, descriptions, keywords or facets');
    assert.equal(requests.length, beforeLocaleRequests);
    await evaluate('document.querySelector("[data-forge-locale]").focus()');
    await cdp('Input.dispatchKeyEvent', { type: 'keyDown', key: '/', code: 'Slash' });
    await cdp('Input.dispatchKeyEvent', { type: 'keyUp', key: '/', code: 'Slash' });
    assert.equal(await evaluate('document.activeElement.hasAttribute("data-forge-locale")'), true);
    await evaluate('document.querySelector("[data-forge-theme-option][aria-checked=true]").focus()');
    await cdp('Input.dispatchKeyEvent', { type: 'keyDown', key: 'ArrowDown', code: 'ArrowDown', windowsVirtualKeyCode: 40 });
    await cdp('Input.dispatchKeyEvent', { type: 'keyUp', key: 'ArrowDown', code: 'ArrowDown', windowsVirtualKeyCode: 40 });
    assert.equal(await evaluate('ForgeUI.theme'), 'light');
    await evaluate('ForgeUI.setTheme("system")');
    const route = await evaluate('document.querySelector(".result-link").getAttribute("href")');
    await evaluate('document.querySelector(".result-link").click()');
    await waitFor('!!document.querySelector(".detail-heading")');
    assert.equal(await evaluate('document.querySelector(".detail-heading h1").textContent'), '展示工具-050');
    assert.equal(await evaluate('document.querySelectorAll(".detail-heading .package-id").length'), 0);
    assert.deepEqual(await evaluate('Array.from(document.querySelectorAll(".facts dt")).map(node=>[node.textContent,node.nextElementSibling.textContent])[0]'), ['Package name', 'tool/工具-050']);
    assert.doesNotMatch(await evaluate('document.querySelector(".facts").textContent'), /canonical.tool-50|Package ID/);
    assert.equal(await evaluate('document.querySelector(".detail-main .tag-list").children.length'), 10);
    assert.equal(await evaluate('document.querySelector(".external-link").getAttribute("href")'), '#');
    assert.equal(await evaluate('document.querySelectorAll(".preview-gallery figure").length'), 3);
    assert.equal(await evaluate('document.querySelectorAll(".media-slot img").length'), 0);
    await evaluate('document.querySelector("[data-media-consent]").click()');
    await waitFor('!!document.querySelector(".media-slot img[hidden]") && document.querySelector(".media-slot img:not([hidden])")?.naturalWidth > 0');
    assert.equal(await evaluate('document.querySelector(".media-slot img[hidden]").hasAttribute("src")'), false);
    assert.match(await evaluate('document.querySelector(".media-slot img[hidden]").parentElement.textContent'), /unavailable/i);
    assert.deepEqual(await evaluate('Array.from(document.querySelectorAll(".preview-gallery figcaption")).map(node => node.textContent)'), ['Dark skin preview · dark', 'Light skin preview · light', 'Unavailable preview']);
    await screenshot('media-detail-desktop-dark.png');
    await evaluate('ForgeUI.setTheme("light")');
    await screenshot('media-detail-desktop-light.png');
    const beforeDetailLocale = requests.length;
    await evaluate('ForgeUI.setLocale("zh-CN")'); await waitFor('document.querySelector(".back-link")?.textContent.includes("返回目录")');
    assert.equal(requests.length, beforeDetailLocale);
    await evaluate('document.querySelector(".back-link").click()'); await waitFor('!!document.getElementById("query")');
    assert.equal(await evaluate('document.getElementById("query").value'), 'Searchable'); assert.equal(await evaluate('document.getElementById("page-number").value'), '2');
    // Initial detail entry must still register hashchange and support return/navigation.
    await navigate(route, '!!document.querySelector(".detail-heading")', true);
    await evaluate('location.hash="#"'); await waitFor('!!document.getElementById("query")');
    await evaluate('location.hash="' + route + '"'); await waitFor('!!document.querySelector(".detail-heading")');
    await evaluate('ForgeUI.setTheme("light")');
    await cdp('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: 'dark' }] });
    assert.equal(await evaluate('document.documentElement.dataset.theme'), 'light');
    await evaluate('ForgeUI.setTheme("system")'); await waitFor('document.documentElement.dataset.theme === "dark"');
    await cdp('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: 'light' }] }); await waitFor('document.documentElement.dataset.theme === "light"');
    await evaluate('ForgeUI.setLocale("en"); ForgeUI.setTheme("dark")');
    await navigate('dashboard/', 'document.getElementById("global-metrics")?.children.length === 4');
    assert.equal(await evaluate('ForgeUI.locale'), 'en'); assert.equal(await evaluate('ForgeUI.theme'), 'dark');
    assert.match(await evaluate('document.getElementById("global-metrics").textContent'), /Packages/);
    const beforeDashboardLocale = requests.length;
    await evaluate('ForgeUI.setLocale("zh-CN")'); assert.match(await evaluate('document.getElementById("global-metrics").textContent'), /元数据版本记录/); assert.equal(requests.length, beforeDashboardLocale);
    await navigate('dashboard/agent/?id=dsh', 'document.querySelectorAll("#recent-packages .result-card").length === 1');
    assert.equal(await evaluate('ForgeUI.siteBase.href'), base); assert.equal(await evaluate('document.querySelector("#recent-packages strong").textContent'), '最近展示包名');
    assert.equal(await evaluate('document.querySelectorAll("#recent-packages .package-id").length'), 0);
    await evaluate('ForgeUI.setLocale("en")'); assert.match(await evaluate('document.getElementById("agent-description").textContent'), /Agent dsh/);
    await cdp('Emulation.setDeviceMetricsOverride', { width: 360, height: 780, deviceScaleFactor: 1, mobile: false });
    assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
    const colors = await evaluate('({background:getComputedStyle(document.body).backgroundColor,control:getComputedStyle(document.querySelector(".language-trigger")).backgroundColor})');
    assert.notEqual(colors.background, 'rgb(246, 248, 247)'); assert.notEqual(colors.control, 'rgb(255, 255, 255)');
    // Full gallery on mobile, including fixed failure slots and consent after reload.
    await navigate(route, '!!document.querySelector(".detail-heading")');
    assert.equal(await evaluate('document.querySelectorAll(".media-slot img").length'), 0);
    await evaluate('document.querySelector("[data-media-consent]").click()');
    await waitFor('!!document.querySelector(".media-slot img[hidden]")');
    assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
    await screenshot('media-detail-mobile-dark.png');
    await evaluate('ForgeUI.setTheme("light")'); await screenshot('media-detail-mobile-light.png');
    // Real schema-driven submission: import, visual editing, language, type change and draft.
    await navigate('submit-real/', `!!document.querySelector('[data-path="/name"] input')`);
    // Optional media can be created visually without importing JSON.
    await evaluate('Array.from(document.querySelectorAll(".schema-add-fields button")).find(button => button.textContent.endsWith("· media")).click()');
    await evaluate('Array.from(document.querySelectorAll(".schema-add-fields button")).find(button => button.textContent.endsWith("· icon")).click()');
    await evaluate(`for (const [field,value] of [["url","https://media.example.test/icon.png"],["alt","Visually added icon"]]) { const input=document.querySelector('[data-path="/media/icon/'+field+'"] input');input.value=value;input.dispatchEvent(new Event("input",{bubbles:true})); }`);
    assert.equal(await evaluate('JSON.parse(document.getElementById("json-preview").textContent).media.icon.alt'), 'Visually added icon');
    await evaluate('Array.from(document.querySelectorAll(".schema-add-fields button")).find(button => button.textContent.endsWith("· previews")).click()');
    assert.equal(await evaluate('JSON.parse(document.getElementById("json-preview").textContent).media.previews.length'), 1);
    const submitted = JSON.parse(fs.readFileSync(path.join(root, 'examples/package-agent-plugin.json'), 'utf8'));
    submitted.media = { ...mediaIndex, previews: [...mediaIndex.previews, { url: 'https://media.example.test/light.png', alt: 'Light skin preview', theme: 'light' }] };
    await evaluate('document.getElementById("json-tab").click();document.getElementById("json-input").value=' + JSON.stringify(JSON.stringify(submitted)) + ';document.getElementById("json-input").dispatchEvent(new Event("input",{bubbles:true}));document.getElementById("form-tab").click()');
    await waitFor(`!!document.querySelector('[data-path="/media/icon/url"] input')`);
    assert.equal(await evaluate('JSON.parse(document.getElementById("json-preview").textContent).media.previews.length'), 2);
    await evaluate(`document.querySelector('[data-editor-path="/media"]').open=true;document.querySelector('[data-editor-path="/media/icon"]').open=true;const input=document.querySelector('[data-path="/media/icon/alt"] input');input.value="Updated icon description";input.dispatchEvent(new Event("input",{bubbles:true}));ForgeUI.setLocale("en")`);
    assert.equal(await evaluate('JSON.parse(document.getElementById("json-preview").textContent).media.icon.alt'), 'Updated icon description');
    assert.equal(await evaluate('document.getElementById("create-issue").disabled'), false);
    assert.equal(await evaluate('document.querySelectorAll(".media-slot img").length'), 0);
    await evaluate(`document.querySelector('[data-editor-path="/media"]').scrollIntoView({block:"center"})`);
    await screenshot('media-submit-mobile-light.png');
    await evaluate(`const type=document.querySelector('[data-path="/type"] select');type.value="skill";type.dispatchEvent(new Event("change",{bubbles:true}));`);
    assert.equal(await evaluate('JSON.parse(document.getElementById("json-preview").textContent).media.icon.alt'), 'Updated icon description');
    await timers.setTimeout(350); await cdp('Page.reload', { ignoreCache: true });
    await waitFor('document.getElementById("json-preview")?.textContent.includes("Updated icon description")');
    for (const suffix of ['docs/', 'submit/']) {
      await navigate(suffix, '!!document.querySelector(".forge-preferences")');
      assert.equal(await evaluate('ForgeUI.siteBase.href'), base); assert.equal(await evaluate('document.querySelectorAll(".forge-preferences").length'), 1);
      await evaluate('document.getElementById("draft").value="unsaved draft"; ForgeUI.setLocale("zh-CN"); ForgeUI.setLocale("en")');
      assert.equal(await evaluate('document.getElementById("draft").value'), 'unsaved draft');
    }
    // Actual Pages source acquisition: no credentials/images, protected edits and manual review.
    const resetSourceForm = async () => {
      await timers.setTimeout(300);
      await evaluate('localStorage.removeItem("agent-forge.submission-draft.v1")');
      await navigate('submit-real/', '!!document.querySelector("#schema-fields input") && !document.getElementById("fetch-source").disabled', true);
      await evaluate('{ const type=document.querySelector(\'[data-path="/type"] select\');type.value="plugin";type.dispatchEvent(new Event("change",{bubbles:true}));ForgeUI.setLocale("zh-CN"); }');
    };
    const enterSource = async (repo) => evaluate('{ const input=document.getElementById("source-url");input.value=' + JSON.stringify('https://github.com/author/' + repo) + ';input.dispatchEvent(new Event("input",{bubbles:true}));input.dispatchEvent(new Event("change",{bubbles:true})); }');
    await resetSourceForm();
    await evaluate('{ const input=document.querySelector(\'[data-path="/description"] textarea\');input.value="My description";input.dispatchEvent(new Event("input",{bubbles:true})); }');
    await enterSource('skin');
    await waitFor('document.getElementById("source-status").textContent.includes("已填入")');
    let acquired = await evaluate('JSON.parse(document.getElementById("json-preview").textContent)');
    assert.equal(acquired.description, 'My description'); assert.equal(acquired.name, 'Fetched skin');
    assert.equal(acquired.media.icon.alt, 'Declared icon'); assert.equal(acquired.media.previews.length, 2);
    assert.equal(acquired.pluginDetails.manifestPath, 'plugin.json'); assert.equal(acquired.distributions[0].ref, 'a'.repeat(40));
    assert.equal(acquired.id, ''); assert.equal(acquired.targets[0].agentId, '');
    assert.equal(await evaluate('document.querySelectorAll("#source-candidates .source-candidate").length'), 1);
    assert.equal(await evaluate('document.querySelectorAll("#source-candidates img").length'), 0);
    await evaluate('document.querySelector("#source-candidates button").click()');
    assert.equal(await evaluate('JSON.parse(document.getElementById("json-preview").textContent).media.previews.length'), 3);
    await evaluate('{ document.querySelector(\'[data-editor-path="/media"]\').open=true;document.querySelector(\'[data-editor-path="/media/icon"]\').open=true;const input=document.querySelector(\'[data-path="/media/icon/alt"] input\');input.value="My icon description";input.dispatchEvent(new Event("input",{bubbles:true})); }');
    await evaluate('document.getElementById("fetch-source").click()');
    await waitFor('document.getElementById("cancel-source").hidden && document.getElementById("source-status").textContent.includes("已填入")');
    assert.equal(await evaluate('JSON.parse(document.getElementById("json-preview").textContent).media.icon.alt'), 'My icon description');
    assert.equal(await evaluate('getComputedStyle(document.getElementById("cancel-source")).display'), 'none');
    await evaluate('ForgeUI.setLocale("en")');
    assert.ok((await evaluate('document.getElementById("source-heading").textContent')).includes('Fill from a source'));
    await cdp('Emulation.setDeviceMetricsOverride', { width: 1365, height: 1000, deviceScaleFactor: 1, mobile: false });
    await evaluate('ForgeUI.setTheme("dark");document.querySelector(".source-import").scrollIntoView()');
    await screenshot('source-submit-desktop-dark.png');
    await cdp('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: false });
    await evaluate('ForgeUI.setTheme("light");window.scrollTo(0,0)');
    assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
    await screenshot('source-submit-mobile-light.png');
    await timers.setTimeout(300);
    const requestsBeforeRestore = sourceRequests.length;
    await cdp('Page.reload', { ignoreCache: true });
    await waitFor('document.getElementById("source-url")?.value.endsWith("/skin") && !!document.querySelector("#schema-fields input")');
    assert.equal(sourceRequests.length, requestsBeforeRestore);
    assert.equal(await evaluate('JSON.parse(document.getElementById("json-preview").textContent).media.icon.alt'), 'My icon description');
    await resetSourceForm(); await evaluate('ForgeUI.setLocale("zh-CN")'); await enterSource('no-icon');
    await waitFor('document.getElementById("source-status").textContent.includes("已填入")');
    assert.equal(await evaluate('JSON.parse(document.getElementById("json-preview").textContent).media.icon'), undefined);
    assert.equal(await evaluate('JSON.parse(document.getElementById("json-preview").textContent).media.previews.length'), 2);
    await resetSourceForm(); await enterSource('slow');
    await waitFor('!document.getElementById("cancel-source").hidden');
    await evaluate('{ const input=document.querySelector(\'[data-path="/description"] textarea\');input.value="Edited while reading";input.dispatchEvent(new Event("input",{bubbles:true})); }');
    await waitFor('document.getElementById("source-status").textContent.includes("已填入")');
    assert.equal(await evaluate('JSON.parse(document.getElementById("json-preview").textContent).description'), 'Edited while reading');
    await resetSourceForm(); await enterSource('slow');
    await evaluate('document.getElementById("cancel-source").click()');
    await timers.setTimeout(400);
    assert.equal(await evaluate('JSON.parse(document.getElementById("json-preview").textContent).name'), '');
    await enterSource('limited');
    await waitFor('document.getElementById("source-status").textContent.includes("限流")');
    assert.equal(await evaluate('JSON.parse(document.getElementById("json-preview").textContent).name'), '');
    await resetSourceForm(); await enterSource('refresh');
    await waitFor('document.getElementById("source-status").textContent.includes("已填入")');
    assert.equal(await evaluate('JSON.parse(document.getElementById("json-preview").textContent).version'), '1.0.0');
    await evaluate('document.getElementById("fetch-source").click()');
    await waitFor('document.getElementById("cancel-source").hidden && document.getElementById("source-status").textContent.includes("已填入")');
    assert.equal(await evaluate('JSON.parse(document.getElementById("json-preview").textContent).version'), '2.0.0');
    await evaluate(`{const input=document.querySelector('[data-path="/description"] textarea');input.value="Keep manual";input.dispatchEvent(new Event("input",{bubbles:true}));}`);
    await enterSource('other');
    await waitFor('document.getElementById("cancel-source").hidden && document.getElementById("source-status").textContent.includes("已填入")');
    acquired = await evaluate('JSON.parse(document.getElementById("json-preview").textContent)');
    assert.equal(acquired.name, 'Other skin'); assert.equal(acquired.description, 'Keep manual'); assert.equal(acquired.media.icon, undefined);
    assert.equal(acquired.media.previews.length, 1); assert.ok(acquired.media.previews[0].url.endsWith('/other.png'));
    assert.ok(acquired.distributions[0].url.endsWith('/author/other'));
    assert.ok(acquired._meta['org.agentforge/source-acquisition'].observations.every(item => item.repository === 'author/other'));
    assert.equal(acquired._meta['org.agentforge/source-acquisition'].fields.includes('/description'), false);
    await timers.setTimeout(300); const savedRequestCount = sourceRequests.length;
    await cdp('Page.reload', { ignoreCache: true });
    await waitFor('document.getElementById("source-url")?.value.endsWith("/other") && !!document.querySelector("#schema-fields input")');
    assert.equal(sourceRequests.length, savedRequestCount);
    await enterSource('no-icon');
    await waitFor('document.getElementById("cancel-source").hidden && document.getElementById("source-status").textContent.includes("已填入")');
    acquired = await evaluate('JSON.parse(document.getElementById("json-preview").textContent)');
    assert.equal(acquired.name, 'Fetched skin'); assert.equal(acquired.description, 'Keep manual'); assert.equal(acquired.media.previews.length, 2);
    assert.ok(acquired._meta['org.agentforge/source-acquisition'].observations.every(item => item.repository === 'author/no-icon'));
    const beforePartial = await evaluate('document.getElementById("json-preview").textContent');
    await enterSource('partial');
    await waitFor('document.getElementById("source-status").textContent.includes("部分文件读取失败")');
    assert.equal(await evaluate('document.getElementById("json-preview").textContent'), beforePartial);
    await resetSourceForm(); await enterSource('ambiguous');
    await waitFor('document.getElementById("source-status").textContent.includes("多个 manifest")');
    assert.equal(await evaluate('JSON.parse(document.getElementById("json-preview").textContent).name'), '');
    await evaluate('document.getElementById("source-path").value="plugin.json";document.getElementById("fetch-source").click()');
    await waitFor('document.getElementById("cancel-source").hidden && document.getElementById("source-status").textContent.includes("已填入")');
    assert.equal(await evaluate('JSON.parse(document.getElementById("json-preview").textContent).name'), 'Fetched skin');
    await resetSourceForm(); const pendingStart = sourceRequests.length; await enterSource('slow');
    await evaluate('document.getElementById("fetch-source").click();document.getElementById("fetch-source").click()');
    await waitFor('document.getElementById("cancel-source").hidden && document.getElementById("source-status").textContent.includes("已填入")');
    assert.equal(sourceRequests.slice(pendingStart).filter(request => new URL(request.url).pathname === '/repos/author/slow').length, 1);
    await resetSourceForm();
    await evaluate('document.getElementById("source-path").value="skin-gallery"');
    const nestedRequestStart = sourceRequests.length;
    await enterSource('skin');
    await waitFor('document.getElementById("source-status").textContent.includes("已填入")');
    acquired = await evaluate('JSON.parse(document.getElementById("json-preview").textContent)');
    assert.equal(acquired.pluginDetails.manifestPath, 'skin-gallery/plugin.json');
    assert.ok(acquired.media.previews.some(image => image.url.endsWith('/light.png')));
    const nestedRequests = sourceRequests.slice(nestedRequestStart);
    assert.ok(nestedRequests.some(request => request.url.includes('/readme?ref=' + 'a'.repeat(40))));
    assert.equal(nestedRequests.some(request => /\/contents\/.*readme/i.test(request.url)), false);
    assert.match(await evaluate(`document.querySelector('[data-i18n="source.boundary"]').textContent`), /仓库根 README/);
    assert.equal(await evaluate(`document.querySelector('[data-i18n="source.path"]').textContent.includes("README")`), false);
    assert.ok(sourceRequests.length > 0);
    for (const request of sourceRequests) { assert.equal(Object.keys(request.headers).some(key => /^(authorization|cookie|referer)$/i.test(key)), false); }
    await evaluate('ForgeUI.setLocale("en")');
    if (process.env.FORGE_MEDIA_SAMPLE_SITE) {
      await navigate('sample/', 'document.querySelectorAll(".result-card").length === 3');
      assert.ok(await evaluate('document.querySelectorAll(".media-thumbnail").length') > 0);
      assert.equal(await evaluate('document.querySelectorAll(".media-slot img").length'), 0);
      const sampleName = await evaluate('document.querySelector(".result-title strong").textContent');
      await evaluate('document.querySelector(".result-link").click()');
      await waitFor('!!document.querySelector(".preview-gallery figure")');
      assert.equal(await evaluate('document.querySelector(".detail-heading h1").textContent'), sampleName);
      assert.equal(await evaluate('document.querySelectorAll(".media-slot img").length'), 0);
      await screenshot('media-real-sample-mobile.png');
    }
    if (process.env.FORGE_MEDIA_FULL_SITE) {
      await navigate('full/', 'document.querySelectorAll(".result-card").length === 50');
      assert.ok(await evaluate('Number(document.getElementById("result-count").textContent.replace(/[^0-9]/g,""))') > 17000);
      assert.equal(await evaluate('document.querySelectorAll(".media-slot img").length'), 0);
      await evaluate('const select=document.getElementById("type-filter");select.value="plugin";select.dispatchEvent(new Event("change",{bubbles:true}))');
      await waitFor('document.querySelector(".result-title .pill")?.textContent === "PLUGIN"');
      assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
      const fullName = await evaluate('document.querySelector(".result-title strong").textContent');
      await evaluate('document.querySelector(".result-link").click()');
      await waitFor('!!document.querySelector(".detail-heading")');
      assert.equal(await evaluate('document.querySelector(".detail-heading h1").textContent'), fullName);
      assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true);
    }
    assert.deepEqual(unexpectedImageRequests, []);
    assert.deepEqual(exceptions, []);
  } catch (error) { acceptanceFailure = error; throw error; } finally {
    if (socket && socket.readyState === WebSocket.OPEN) {
      await Promise.race([send('Browser.close').catch(() => {}), timers.setTimeout(1000)]); socket.close();
    }
    if (child.exitCode === null && child.signalCode === null) {
      const exited = new Promise((resolve) => child.once("exit", resolve));
      await Promise.race([exited, timers.setTimeout(4000)]);
      if (child.exitCode === null && child.signalCode === null) { child.kill(); await Promise.race([exited, timers.setTimeout(4000)]); }
    }
    await new Promise((resolve) => server.close(resolve));
    // Delete only our exact mkdtemp directory, never an unchecked computed path.
    const target = path.resolve(profile), parent = path.resolve(os.tmpdir());
    assert.equal(path.dirname(target), parent); assert.ok(path.basename(target).startsWith('forge-pages-ui-'));
    await timers.setTimeout(200);
    let cleanupFailure;
    for (let attempt = 0; attempt < 6; attempt++) {
      try { fs.rmSync(target, { recursive: true, force: true, maxRetries: 10, retryDelay: 150 }); cleanupFailure = null; break; }
      catch (error) { cleanupFailure = error; if (error.code !== 'EPERM') break; await timers.setTimeout(500); }
    }
    if (cleanupFailure) { if (acceptanceFailure) console.error('Browser cleanup also failed:', cleanupFailure.message); else throw cleanupFailure; }
  }
});
test('media references survive index normalization but never become requests before consent', () => {
  const env = appEnvironment();
  const media = { icon: { url: 'https://images.example/icon.png', alt: 'Icon' }, previews: [{ url: 'https://images.example/dark.png', alt: '<script>alert(1)</script>', theme: 'dark' }, { url: 'https://images.example/light.png', alt: 'Light', theme: 'light' }] };
  const [entry] = env.app.normalizeIndex({ agentId: 'dsh', type: 'plugin', packages: { skin: { latest: '1', path: 'skin.json', subtype: 'skin', media } } }, 'data/dsh/plugin/index.json');
  assert.equal(JSON.stringify(entry.media), JSON.stringify(media));
  let html = env.app.renderGallery({ ...entry, type: 'plugin' });
  assert.doesNotMatch(html, /<img|<script/); assert.match(html, /&lt;script&gt;/); assert.match(html, /加载外链图片/);
  env.app.state.mediaEnabled = true;
  html = env.app.renderGallery({ ...entry, type: 'plugin' });
  assert.equal((html.match(/<img /g) || []).length, 3);
  assert.equal((html.match(/referrerpolicy="no-referrer"/g) || []).length, 3);
  assert.match(html, /loading="lazy"/); assert.doesNotMatch(html, /onerror=/);
  assert.ok(html.indexOf('dark.png') < html.indexOf('light.png'));
  env.ui.setLocale('en'); assert.match(env.app.renderGallery({ ...entry, type: 'plugin' }), /Hide external images/);
  for (const url of ['javascript:alert(1)', 'data:image/svg+xml,<svg/>', 'file:///x', '//images.example/x', 'https://user:password@images.example/x', 'https://images.example/" onerror="alert(1)', 'https://images.example/\\x', 'https://images.example:99999/x']) {
    assert.equal(env.app.safeImageURL(url), '', url);
    assert.doesNotMatch(env.app.imageSlot({ url, alt: 'Unsafe' }, 'icon', 'P'), /<img/);
  }
});
