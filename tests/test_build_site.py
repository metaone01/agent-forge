"""Build contract/security tests. All generated artifacts live in temporary roots."""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from tools.build_site import (
    DEFAULT_ROOT, DOCUMENTS, SCHEMAS, Document, Links, Markdown, build_site,
    cells, highlight, inline, main, render_document, safe_url,
)


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.ids = []
        self.refs = []
        self.tags = []
        self.attributes = []
        self.feed(text)

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        self.tags.append(tag)
        self.attributes.append((tag, attrs))
        if "id" in attrs:
            self.ids.append(attrs["id"])
        for key in ("href", "src"):
            if key in attrs:
                self.refs.append((tag, key, attrs[key]))


def digest_tree(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


class BuildSiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="forge-docs-test-")
        self.addCleanup(self.cleanup_temp)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.output = Path(self.temp.name) / "preview"
        for document in DOCUMENTS:
            target = self.root / document.source
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(DEFAULT_ROOT / document.source, target)
        for name in SCHEMAS:
            shutil.copyfile(DEFAULT_ROOT / (name + ".schema.json"), self.root / (name + ".schema.json"))
        for name in ("site/index.html", "site/dashboard/index.html", "site/.nojekyll",
                     "site/assets/styles.css", "site/assets/docs.css", "site/assets/docs.js", "site/README.md", "site/README_en.md"):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(DEFAULT_ROOT / name, target)
        (self.root / "site/assets/preferences.js").write_text("/* shared preferences fixture */", encoding="utf-8")
        (self.root / "data").mkdir()
        (self.root / "data/manifest.json").write_text('{"sources": []}', encoding="utf-8")
        (self.root / "sources").mkdir()
        (self.root / "sources/canonical.json").write_text('{"untouched": true}', encoding="utf-8")
        self.doc = DOCUMENTS[0]
        self.links = Links(self.root, self.doc, {"data/manifest.json"})

    def cleanup_temp(self):
        # Windows scanners can briefly hold freshly replaced Markdown artifacts.
        # Retry only this exact TemporaryDirectory; never ignore cleanup failure.
        for attempt in range(4):
            try:
                self.temp.cleanup()
                return
            except OSError:
                if attempt == 3:
                    raise
                time.sleep(0.1 * (attempt + 1))

    def build(self, **kwargs):
        return build_site(self.root, self.output, **kwargs)

    def test_catalog_pause_survives_build_and_document_navigation(self):
        self.build()
        landing = (self.output / "index.html").read_text(encoding="utf-8")
        self.assertIn('<meta http-equiv="refresh" content="0; url=dashboard/">', landing)
        self.assertIn('<body data-page="catalog-hidden" hidden>', landing)
        for doc in DOCUMENTS:
            page = Page((self.output / doc.target).read_text(encoding="utf-8"))
            self.assertFalse(any(attrs.get("data-i18n") == "docs.catalog" for _, attrs in page.attributes))
            self.assertTrue(any(attrs.get("href", "").endswith("dashboard/index.html") for _, attrs in page.attributes))

    def test_complete_build_copies_static_schemas_data_and_raw_sources(self):
        before = digest_tree(self.root)
        self.build()
        self.assertEqual(before, digest_tree(self.root), "Build must not mutate inputs/canonical")
        self.assertTrue((self.output / ".nojekyll").is_file())
        for name in SCHEMAS:
            self.assertEqual((self.root / (name + ".schema.json")).read_bytes(),
                             (self.output / (name + ".schema.json")).read_bytes())
        self.assertEqual(json.loads((self.output / "data/manifest.json").read_text()), {"sources": []})
        self.assertEqual(len(DOCUMENTS), 12)
        for doc in DOCUMENTS:
            text = (self.output / doc.target).read_text(encoding="utf-8")
            self.assertIn(f'<html lang="{doc.locale}">', text)
            self.assertIn('<article>', text)
            self.assertEqual((self.root / doc.source).read_bytes(), (self.output / ("docs/raw/" + doc.source)).read_bytes())
        self.assertTrue((self.output / "docs/schema/README.html").is_file(), "Issue contact URL remains valid")

    def test_paired_reference_anchors_map_to_real_translated_headings(self):
        doc = next(item for item in DOCUMENTS if item.target == "docs/schema/package.schema_en.html")
        page = Page(render_document(self.root, doc, set()))
        body = next(attrs for tag, attrs in page.attributes if tag == "body")
        anchors = json.loads(body["data-doc-anchors"])
        self.assertEqual("用途", anchors["purpose"])
        peer = next(item for item in DOCUMENTS if item.target == doc.peer)
        peer_page = Page(render_document(self.root, peer, set()))
        self.assertTrue(set(anchors.values()).issubset(set(peer_page.ids)))
    def test_every_generated_local_link_and_toc_anchor_exists(self):
        self.build()
        for doc in DOCUMENTS:
            path = self.output / doc.target
            page = Page(path.read_text(encoding="utf-8"))
            self.assertEqual(len(page.ids), len(set(page.ids)), doc.target)
            for tag, attribute, value in page.refs:
                parsed = urlsplit(value)
                if parsed.scheme or parsed.netloc:
                    continue
                destination = (path.parent / unquote(parsed.path)).resolve() if parsed.path else path
                self.assertTrue(destination.is_relative_to(self.output), value)
                if destination.is_dir():
                    destination /= "index.html"
                self.assertTrue(destination.is_file(), (doc.target, tag, attribute, value))
                if parsed.fragment and destination.suffix == ".html" and destination == path:
                    self.assertIn(unquote(parsed.fragment), page.ids, value)

    def test_english_navigation_and_body_use_english_document(self):
        self.build()
        for doc in DOCUMENTS:
            if doc.locale != "en":
                continue
            text = (self.output / doc.target).read_text(encoding="utf-8")
            self.assertIn('data-doc-locale="en"', text)
            self.assertIn('>Overview</h2>', text)
            self.assertIn('>Reference</h2>', text)
            self.assertIn('index_en.html', text)
            self.assertNotIn('>概览</h2>', text)
            self.assertNotIn('>参考</h2>', text)
            article = text.split('<article>', 1)[1].split('</article>', 1)[0]
            self.assertNotIn("本目录是四份", article)
            self.assertNotIn("面向 Agent 工具的开放", article)

    def test_preferences_loaded_synchronously_before_styles_and_local_script(self):
        text = render_document(self.root, DOCUMENTS[1], set())
        self.assertLess(text.index('/preferences.js'), text.index('/styles.css'))
        self.assertIn('<script src="../assets/preferences.js"></script>', text)
        self.assertNotIn('<script src="../assets/preferences.js" defer', text)
        self.assertIn('data-doc-peer="index.html"', text)
        self.assertIn('data-doc-language="zh-CN"', text)
        self.assertNotIn('src="https://', text.split('</head>')[0])

    def test_language_chrome_markers_do_not_translate_document_content(self):
        self.build()
        for doc in DOCUMENTS:
            text = (self.output / doc.target).read_text(encoding="utf-8")
            article = text.split('<article>', 1)[1].split('</article>', 1)[0]
            self.assertNotIn('data-i18n="docs.overview"', article)
            self.assertNotIn('data-i18n="docs.reference"', article)

    def test_additive_rebuild_is_deterministic_and_does_not_delete(self):
        self.build()
        marker = self.output / "unrelated/keep.txt"
        marker.parent.mkdir()
        marker.write_text("retain me", encoding="utf-8")
        first = digest_tree(self.output)
        self.build()
        self.assertEqual(first, digest_tree(self.output))
        self.assertEqual(marker.read_text(encoding="utf-8"), "retain me")

    def test_missing_data_is_supported_and_published_data_links_stay_valid(self):
        self.build(data=Path("not-generated"))
        self.assertFalse((self.output / "data").exists())
        text = (self.output / "docs/index.html").read_text(encoding="utf-8")
        self.assertIn('href="https://metaone01.github.io/agent-forge/data/manifest.json"', text)

    def test_missing_input_fails_before_any_write(self):
        (self.root / "README_en.md").unlink()
        with self.assertRaisesRegex(ValueError, "Missing"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_output_rejects_repository_ancestors_and_all_protected_sources(self):
        before = digest_tree(self.root)
        targets = [self.root, self.root.parent, self.root / "site", self.root / "site/assets/new",
                   self.root / "data", self.root / "docs/new", self.root / "sources/new",
                   self.root / "tools/new", self.root / "tests/new", self.root / ".github/new",
                   self.root / ".git/new", self.root / "package.schema.json", self.root / "README.md"]
        for target in targets:
            with self.subTest(target=target), self.assertRaises(ValueError):
                build_site(self.root, target)
        self.assertEqual(before, digest_tree(self.root))

    def test_output_cannot_contain_external_data_source(self):
        source = self.output / "data-input"
        source.mkdir(parents=True)
        (source / "keep.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "overlaps"):
            self.build(data=source)
        self.assertEqual(list(self.output.iterdir()), [source])

    def test_output_file_and_parent_collisions_fail_before_mutation(self):
        self.output.mkdir()
        (self.output / "assets").write_text("do not replace", encoding="utf-8")
        before = digest_tree(self.output)
        with self.assertRaisesRegex(ValueError, "directory"):
            self.build()
        self.assertEqual(before, digest_tree(self.output))

    def test_output_directory_destination_collision_fails_before_mutation(self):
        (self.output / "docs/index.html").mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "not a file"):
            self.build()
        self.assertFalse((self.output / "assets").exists())

    def directory_alias(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=True)
            self.addCleanup(link.unlink)
        except OSError as error:
            if os.name != "nt":
                self.skipTest(f"Symlink creation not available: {error}")
            # Directory junctions do not need Windows' symlink privilege.
            result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)],
                                    capture_output=True, text=True)
            if result.returncode:
                self.skipTest(f"Reparse point creation not available: {result.stderr}")
            self.addCleanup(os.rmdir, link)

    def test_symlink_destinations_are_rejected(self):
        external = Path(self.temp.name) / "outside"
        external.mkdir()
        self.output.mkdir()
        self.directory_alias(self.output / "assets", external)
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            self.build()
        self.assertEqual(list(external.iterdir()), [])

    def test_symlink_sources_are_rejected_before_writes(self):
        external = Path(self.temp.name) / "outside"
        external.mkdir()
        (external / 'private.json').write_text('{"do not copy":true}', encoding='utf-8')
        self.directory_alias(self.root / "site/assets/alias", external)
        with self.assertRaisesRegex(ValueError, "source tree"):
            self.build()
        self.assertFalse(self.output.exists())
        self.assertEqual((external / 'private.json').read_text(encoding='utf-8'), '{"do not copy":true}')

    def test_missing_shared_asset_rejected_before_writes(self):
        (self.root / "site/assets/preferences.js").unlink()
        with self.assertRaisesRegex(ValueError, "Missing site asset"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_own_published_markdown_links_and_encoded_traversal(self):
        self.assertEqual(self.links.resolve('https://metaone01.github.io/agent-forge/README_en.md#x'), 'index_en.html#x')
        self.assertEqual(self.links.resolve('https://metaone01.github.io/agent-forge/docs/schema/package.schema.md'), 'schema/package.schema.html')
        self.assertIsNone(self.links.resolve('https://metaone01.github.io/agent-forge/%2e%2e/outside'))
        self.assertEqual(self.links.resolve('https://metaone01.github.io/agent-forge/not-built.html'),
                         'https://metaone01.github.io/agent-forge/not-built.html')

    def test_complete_output_does_not_activate_malicious_source(self):
        (self.root / "README.md").write_text('# <script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n[x](javascript:alert(1))', encoding='utf-8')
        self.build()
        text = (self.output / "docs/index.html").read_text(encoding='utf-8')
        page = Page(text)
        scripts = [attrs['src'] for tag, attrs in page.attributes if tag == 'script']
        self.assertEqual(scripts, ['../assets/preferences.js', '../assets/docs.js'])
        self.assertNotIn('img', page.tags)
        self.assertFalse(any(key.startswith('on') for _, attrs in page.attributes for key in attrs))
        self.assertIn('<title>&lt;script&gt;alert(1)&lt;/script&gt;', text)

    def test_cli_relative_paths_are_root_relative(self):
        result = main(["--root", str(self.root), "--output", "staging", "--data", "data"])
        self.assertEqual(result, 0)
        self.assertTrue((self.root / "staging/docs/index.html").is_file())

    def test_cli_rejects_unsafe_output(self):
        with self.assertRaises(SystemExit) as raised:
            main(["--root", str(self.root), "--output", "site"])
        self.assertEqual(raised.exception.code, 2)

    def test_markdown_links_rewrite_by_source_path_not_output_path(self):
        self.assertEqual(self.links.resolve('docs/schema/package.schema.md#purpose'), 'schema/package.schema.html#purpose')
        self.assertEqual(self.links.resolve('README_en.md?view=1#agent-forge'), 'index_en.html?view=1#agent-forge')
        self.assertEqual(self.links.resolve('package.schema.json'), '../package.schema.json')
        self.assertEqual(self.links.resolve('data/manifest.json'), '../data/manifest.json')
        schema = Links(self.root, DOCUMENTS[4], set())
        self.assertEqual(schema.resolve('source.schema_en.md'), 'source.schema_en.html')
        self.assertEqual(schema.resolve('../../README.md'), '../index.html')
        self.assertEqual(schema.resolve('../../source.schema.json'), '../../source.schema.json')
        self.assertEqual(schema.resolve('../../tools/validate.py'), 'https://github.com/metaone01/agent-forge/blob/main/tools/validate.py')
        self.assertIsNone(schema.resolve('../../../outside.md'))

    def test_submission_query_prefill_is_preserved_including_encoded_json(self):
        url = ('https://github.com/metaone01/agent-forge/issues/new?template=package-submission.yml'
               '&package_json=%7B%0A%20%22path%22%3A%22C%3A%5C%5Ctools%22%0A%7D#notes')
        self.assertEqual(safe_url(url), url)
        self.assertEqual(self.links.resolve(url), url)
        attributes = Page(inline('[Submit](' + url + ')', self.links)).attributes
        self.assertEqual(attributes[0][1]['href'], url)

    def test_project_subpath_links_are_relative_and_own_published_links_local(self):
        self.assertEqual(self.links.resolve('https://metaone01.github.io/agent-forge/'), '../index.html')
        self.assertEqual(self.links.resolve('https://metaone01.github.io/agent-forge/dashboard/'), '../dashboard/')
        self.assertEqual(self.links.resolve('https://metaone01.github.io/agent-forge/data/manifest.json'), '../data/manifest.json')
        text = render_document(self.root, DOCUMENTS[4], set())
        for _, _, value in Page(text).refs:
            self.assertFalse(value.startswith('/'), value)

    def test_xss_url_schemes_encoded_variants_and_raw_html_are_inert(self):
        payloads = ['javascript:alert(1)', 'JaVaScRiPt:alert(1)', 'data:text/html,<script>x</script>',
                    'vbscript:run', '&#106;avascript:alert(1)', '%6aavascript:alert(1)',
                    'java%0ascript:alert(1)', '//attacker.example/a', r'\\attacker.example\a',
                    'https://user:password@attacker.example/', 'https://[invalid', '/%2fattack.example/a']
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertIsNone(safe_url(payload))
                self.assertIsNone(safe_url(payload, image=True))
        text = '''# Safe <script>alert(1)</script>

<img src=x onerror=alert(1)>

[attack](javascript:alert(1)) ![attack](data:text/html,x)

<a href="javascript:alert(1)">raw</a>

<details onclick="alert(1)"><summary>danger</summary></details>

```typescript
const x = "</code><script>alert(1)</script>";
```
'''
        rendered = Markdown(self.links).render(text)
        page = Page(rendered)
        self.assertNotIn('script', page.tags)
        self.assertNotIn('img', page.tags)
        self.assertNotIn('details', page.tags)
        for _, attrs in page.attributes:
            self.assertFalse(any(key.startswith('on') for key in attrs))
        self.assertIn('&lt;script&gt;', rendered)
        self.assertNotIn('href="javascript:', rendered)

    def test_inline_badges_escaped_attributes_code_and_emphasis(self):
        rendered = inline('[![Build](https://example.test/badge.svg)](https://example.test/build) '
                          '**strong** *em* ~~old~~ `a<b>` snake_case '
                          '[quoted](https://example.test/?x=&quot;)', self.links)
        self.assertIn('<a href="https://example.test/build"', rendered)
        self.assertIn('<img src="https://example.test/badge.svg"', rendered)
        self.assertIn('<strong>strong</strong>', rendered)
        self.assertIn('<em>em</em>', rendered)
        self.assertIn('<del>old</del>', rendered)
        self.assertIn('<code>a&lt;b&gt;</code>', rendered)
        self.assertIn('snake_case', rendered)
        page = Page(rendered)
        self.assertEqual(page.tags.count('a'), 2)
        self.assertFalse(any('onclick' in attrs for _, attrs in page.attributes))

    def test_heading_unicode_duplicates_and_toc_ids(self):
        parser = Markdown(self.links)
        text = parser.render('# Hello `JSON`\n## 中文 标题！\n## 中文 标题！\n## 中文 标题-1\n')
        self.assertEqual([h[1] for h in parser.headings], ['hello-json', '中文-标题', '中文-标题-1', '中文-标题-1-1'])
        self.assertEqual(len(Page(text).ids), len(set(Page(text).ids)))

    def test_pipe_tables_alignment_escaped_pipes_and_code_pipes(self):
        self.assertEqual(cells('| a\\|b | `x|y` |'), ['a\\|b', '`x|y`'])
        text = Markdown(self.links).render('| Name | Value |\n| :--- | ---: |\n| `a|b` | x\\|y |\n')
        self.assertIn('<table>', text)
        self.assertIn('class="align-right"', text)
        self.assertIn('<code>a|b</code>', text)
        self.assertIn('x|y', text)
        self.assertEqual(Page(text).tags.count('td'), 2)

    def test_lists_alerts_and_safe_details(self):
        text = Markdown(self.links).render('''> [!WARNING]
> **Review** first.

3. Third
4. Fourth
   - Child
   - Next

<details>
<summary>Safe **summary**</summary>

## Inside

Paragraph <script>inert</script>
</details>
''')
        self.assertIn('alert-warning', text)
        self.assertIn('<ol start="3">', text)
        self.assertIn('<ul>', text)
        self.assertIn('<details><summary>Safe <strong>summary</strong></summary>', text)
        self.assertNotIn('script', Page(text).tags)
        self.assertIn('id="inside"', text)

    def test_highlighting_is_lexical_and_preserves_code_exactly(self):
        samples = {
            'json': '{"url": "https://example.test/a", "count": 42, "ok": true}',
            'typescript': 'const example = { url: "https://example.test/a", count: 2 }; // note\n/* block */',
            'css': ':root { --accent: #abcdef; color: var(--accent); } /* style */',
            'style': 'p { color: "<unsafe>"; margin: 10px; }',
        }
        class Text(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.parts = []
            def handle_data(self, data):
                self.parts.append(data)
        for language, code in samples.items():
            with self.subTest(language=language):
                rendered = highlight(code, language)
                parser = Text()
                parser.feed(rendered)
                self.assertEqual(''.join(parser.parts), code)
                self.assertIn('tok-property', rendered)
        rendered = highlight(samples['typescript'], 'typescript')
        self.assertIn('tok-keyword', rendered)
        self.assertIn('tok-comment', rendered)
        self.assertIn('tok-number', rendered)
        self.assertIn('tok-string', rendered)
        self.assertIn('https://example.test/a</span>', rendered.replace('&quot;', ''))

    def test_fences_copy_targets_and_plain_mermaid_fallback(self):
        parser = Markdown(self.links)
        text = parser.render('```json\n{"x": true}\n```\n\n```mermaid\ngraph TD; A-->B\n```\n')
        page = Page(text)
        self.assertIn('code-1', page.ids)
        self.assertIn('code-2', page.ids)
        self.assertIn('data-copy="code-1"', text)
        self.assertIn('graph TD; A--&gt;B', text)
        self.assertNotIn('<svg', text)
        self.assertIn('language-mermaid', text)
        self.assertEqual(highlight('<script>x</script>', 'unrecognized'), '&lt;script&gt;x&lt;/script&gt;')

    @unittest.skipUnless(os.environ.get('FORGE_DOCS_BROWSER') == '1', 'Set FORGE_DOCS_BROWSER=1 for existing Edge live acceptance')
    def test_live_browser_subpath_language_theme_copy_and_narrow_layout(self):
        edge = Path(os.environ.get('FORGE_DOCS_EDGE', r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'))
        if not edge.is_file() or not shutil.which('node'):
            self.skipTest('Existing Edge/Node unavailable; no installation performed')
        preferences = DEFAULT_ROOT / 'site/assets/preferences.js'
        self.assertTrue(preferences.is_file(), 'Live acceptance requires the provided shared preferences')
        shutil.copyfile(preferences, self.root / 'site/assets/preferences.js')
        self.build()
        harness = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const {spawn} = require('node:child_process');
const [edge, output, profile, screenshots] = process.argv.slice(1);
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const mount = '/preview/agent-forge/';
const server = http.createServer((req, res) => {
  const pathname = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
  if (!pathname.startsWith(mount)) {res.writeHead(404).end(); return}
  let file = path.resolve(output, pathname.slice(mount.length) || 'index.html');
  if (!file.startsWith(path.resolve(output) + path.sep)) {res.writeHead(403).end(); return}
  if (fs.existsSync(file) && fs.statSync(file).isDirectory()) file = path.join(file, 'index.html');
  if (!fs.existsSync(file)) {res.writeHead(404).end(); return}
  const types = {'.html':'text/html', '.js':'text/javascript', '.css':'text/css', '.json':'application/json', '.md':'text/plain'};
  res.setHeader('Content-Type', (types[path.extname(file)] || 'application/octet-stream') + '; charset=utf-8');
  fs.createReadStream(file).pipe(res);
});
let browser, socket;
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}${mount}`;
  browser = spawn(edge, ['--headless=new', '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
    '--user-data-dir=' + profile, '--no-first-run', '--no-default-browser-check', '--disable-background-networking', 'about:blank'],
    {windowsHide: true, stdio: 'ignore'});
  const portFile = path.join(profile, 'DevToolsActivePort');
  for (let attempt = 0; !fs.existsSync(portFile); attempt++) {
    if (attempt > 150) throw Error('Edge DevTools startup timeout');
    await delay(100);
  }
  const port = fs.readFileSync(portFile, 'utf8').split('\n')[0].trim();
  const target = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, {method:'PUT'})).json();
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve,reject) => {socket.addEventListener('open', resolve, {once:true}); socket.addEventListener('error', reject, {once:true})});
  let id = 0;
  const pending = new Map(), errors = [];
  socket.addEventListener('message', event => {
    const value = JSON.parse(event.data);
    if (value.id) {
      const pair = pending.get(value.id);
      if (pair) {pending.delete(value.id); value.error ? pair.reject(Error(JSON.stringify(value.error))) : pair.resolve(value.result)}
    } else if (value.method === 'Runtime.exceptionThrown') errors.push(value.params.exceptionDetails);
  });
  const cdp = (method, params = {}) => new Promise((resolve,reject) => {
    const key = ++id; pending.set(key,{resolve,reject}); socket.send(JSON.stringify({id:key,method,params}));
  });
  const evaluate = async expression => {
    const response = await cdp('Runtime.evaluate', {expression, returnByValue:true, awaitPromise:true});
    if (response.exceptionDetails) throw Error(JSON.stringify(response.exceptionDetails));
    return response.result.value;
  };
  await cdp('Page.enable'); await cdp('Runtime.enable'); await cdp('Network.enable');
  await cdp('Emulation.setFocusEmulationEnabled', {enabled:true});
  await cdp('Network.setBlockedURLs', {urls:['*github.com*','*github.io*']});
  const waitFor = async expression => {
    for (let n = 0; n < 100; n++) {try {if (await evaluate(expression)) return} catch {} await delay(50)}
    throw Error('Timeout waiting for ' + expression);
  };
  const visit = async (relative, locale) => {
    await cdp('Page.navigate', {url:base + relative});
    await waitFor(`location.href === ${JSON.stringify(base + relative)} && document.readyState === 'complete' && document.body?.dataset.docLocale === ${JSON.stringify(locale)} && !!window.ForgeUI`);
  };
  await visit('docs/index.html', 'zh-CN');
  assert.equal(await evaluate('ForgeUI.locale'), 'zh-CN');
  assert.equal(await evaluate('ForgeUI.theme'), 'system');
  assert.equal(await evaluate('document.querySelectorAll(".forge-preferences").length'), 1);
  await visit('docs/index_en.html', 'en');
  assert.equal(await evaluate('ForgeUI.locale'), 'en');
  assert.equal(await evaluate('document.documentElement.lang'), 'en');
  assert.equal(await evaluate('document.querySelector("article h1").textContent.replace(/#$/, "")'), 'Agent Forge');
  assert.ok((await evaluate('document.querySelector("article").textContent')).includes('open metadata catalog'));
  assert.ok(!(await evaluate('document.querySelector("article").textContent')).includes('面向 Agent 工具'));
  await visit('docs/schema/package.schema_en.html#purpose', 'en');
  await cdp('Emulation.setDeviceMetricsOverride', {width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  assert.ok(await evaluate('document.documentElement.scrollWidth <= innerWidth + 1'));
  await evaluate('ForgeUI.setTheme("dark")');
  assert.equal(await evaluate('document.documentElement.dataset.theme'), 'dark');
  assert.equal(await evaluate('getComputedStyle(document.querySelector("pre")).overflowX'), 'auto');
  assert.ok(await evaluate('document.querySelectorAll(".tok-property").length > 50'));
  if (screenshots) {
    await evaluate('window.scrollTo(0, 0)');
    fs.mkdirSync(screenshots,{recursive:true});
    fs.writeFileSync(path.join(screenshots,'docs-desktop-dark.png'),Buffer.from((await cdp('Page.captureScreenshot',{format:'png'})).data,'base64'));
  }
  await evaluate('ForgeUI.setTheme("light")');
  await cdp('Emulation.setDeviceMetricsOverride', {width:390,height:844,deviceScaleFactor:1,mobile:false});
  assert.ok(await evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), 'Narrow page must not horizontally overflow');
  assert.ok(await evaluate('[...document.querySelectorAll(".table-scroll")].some(node => node.scrollWidth > node.clientWidth)'), 'Tables must scroll inside their wrapper');
  assert.equal(await evaluate('getComputedStyle(document.querySelector(".table-scroll")).overflowX'), 'auto');
  await evaluate('window.scrollTo(0, document.querySelector(".table-scroll").getBoundingClientRect().top + scrollY - document.querySelector(".topbar").offsetHeight - 16)');
  if (screenshots) fs.writeFileSync(path.join(screenshots,'docs-mobile-table.png'),Buffer.from((await cdp('Page.captureScreenshot',{format:'png'})).data,'base64'));
  // Return to code and copy through an actual user gesture on a granted localhost clipboard.
  await cdp('Browser.grantPermissions',{origin:new URL(base).origin,permissions:['clipboardReadWrite','clipboardSanitizedWrite']});
  await evaluate('window.scrollTo(0, document.querySelector(".code-block").getBoundingClientRect().top + scrollY - document.querySelector(".topbar").offsetHeight - 16)');
  if (screenshots) fs.writeFileSync(path.join(screenshots,'docs-mobile-code.png'),Buffer.from((await cdp('Page.captureScreenshot',{format:'png'})).data,'base64'));
  await cdp('Page.bringToFront');
  const copyResult = await cdp('Runtime.evaluate', {expression:'document.querySelector("[data-copy]").click()', userGesture:true});
  assert.ok(!copyResult.exceptionDetails);
  await waitFor('document.querySelector("[data-copy]").textContent === "Copied"');
  // Windows clipboard is CRLF; DOM code text is LF. Content must otherwise match exactly.
  await waitFor("(async()=> (await navigator.clipboard.readText()).replace(/\\r\\n/g, \"\\n\") === document.querySelector(\"pre code\").textContent)()");
  assert.equal((await evaluate('navigator.clipboard.readText()')).replace(/\r\n/g, "\n"), await evaluate('document.querySelector("pre code").textContent'));
  await evaluate('ForgeUI.setLocale("zh-CN")');
  await waitFor('location.pathname.endsWith("/docs/schema/package.schema.html") && document.readyState === "complete" && document.body.dataset.docLocale === "zh-CN"');
  assert.equal(await evaluate('decodeURIComponent(location.hash)'), '#用途');
  assert.equal(await evaluate('document.getElementById(decodeURIComponent(location.hash.slice(1))).tagName'), 'H2');
  assert.equal(await evaluate('ForgeUI.locale'), 'zh-CN');
  await evaluate('ForgeUI.setLocale("en")');
  await waitFor('location.pathname.endsWith("/docs/schema/package.schema_en.html") && document.readyState === "complete" && document.body.dataset.docLocale === "en"');
  assert.equal(await evaluate('location.hash'), '#purpose');
  assert.equal(errors.length, 0, JSON.stringify(errors));
  console.log('Live Edge acceptance passed: subpath/default locale/English URL/theme/copy/mobile/language hash');
})().catch(error => {console.error(error); process.exitCode=1}).finally(async () => {
  if (socket && socket.readyState === 1) socket.send(JSON.stringify({id:999999,method:"Browser.close"}));
  if (browser) {
    await Promise.race([new Promise(resolve => browser.once("exit",resolve)),delay(4000)]);
    if (browser.exitCode === null) browser.kill();
    await delay(500);
  }
  if (socket) socket.close();
  server.close();
});
'''
        result = subprocess.run(['node', '-e', harness, str(edge), str(self.output),
                                 str(Path(self.temp.name) / 'edge-profile'), os.environ.get('FORGE_DOCS_SCREENSHOTS', '')],
                                capture_output=True, text=True, timeout=90, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Live Edge acceptance passed', result.stdout)

    @unittest.skipUnless(shutil.which('node'), 'Node unavailable; Python build remains dependency-free')
    def test_docs_script_locale_events_hash_clipboard_with_shared_contract(self):
        # Existing Node's VM simulates only browser primitives, without npm libraries.
        harness = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
function setup(pageLocale, savedLocale, hasUI = true, clipboardFails = false) {
  const handlers = {}, navigations = [], messages = {}, writes = [];
  const links = ['zh-CN', 'en'].map(locale => ({
    dataset: {docLanguage: locale},
    href: locale === 'en' ? 'package.schema_en.html' : 'package.schema.html',
    getAttribute() {return this.href}, setAttribute(key, value) {this[key] = value}
  }));
  const button = {dataset: {copy: 'code-1'}, disabled: false, setAttribute() {}, addEventListener(name, fn) {this[name] = fn}};
  const code = {textContent: 'const value = "<script>"; // exact'};
  const location = {href: 'https://example.test/mounted/project/docs/schema/package.schema' + (pageLocale === 'en' ? '_en' : '') + '.html#fields',
    hash: '#fields', origin: 'https://example.test', assign(value) {navigations.push(value)}};
  const document = {
    body: {dataset: {docLocale: pageLocale, docPeer: pageLocale === 'en' ? 'package.schema.html' : 'package.schema_en.html'}, append() {}},
    documentElement: {}, querySelectorAll(selector) {return selector === '[data-copy]' ? [button] : links},
    getElementById() {return code}, createElement() {return {style: {}, setAttribute() {}, select() {}, remove() {}}},
    execCommand() {writes.push('fallback'); return !clipboardFails},
    createRange() {return {selectNodeContents(value) {assert.equal(value, code)}}}
  };
  const ui = {locale: savedLocale, siteBase: new URL('https://example.test/mounted/project/'),
    addMessages(value) {Object.assign(messages, value)},
    t(key) {return messages[this.locale][key]}, apply() {},
    setLocale(value) {this.locale = value; (handlers['forge:localechange'] || (() => {}))({detail: {locale: value}})}
  };
  const window = {location, ForgeUI: hasUI ? ui : undefined,
    addEventListener(name, fn) {handlers[name] = fn}, setTimeout(fn) {this.reset = fn},
    getSelection() {return {removeAllRanges() {}, addRange() {}}}};
  vm.runInNewContext(source, {window, document, navigator: {clipboard: {writeText: async text => {if (clipboardFails) throw Error('denied'); writes.push(text)}}}, URL});
  return {window, document, ui, handlers, navigations, links, button, code, writes};
}
(async () => {
  const en = setup('en', 'zh-CN');
  assert.equal(en.ui.locale, 'en');
  assert.equal(en.document.documentElement.lang, 'en');
  assert.equal(en.navigations.length, 0, 'English URL must not redirect to Chinese on initialization');
  assert.ok(en.links.every(link => link.href.endsWith('#fields')));
  en.ui.setLocale('zh-CN');
  assert.equal(en.navigations[0], 'https://example.test/mounted/project/docs/schema/package.schema.html#fields');
  const zh = setup('zh-CN', 'en');
  assert.equal(zh.ui.locale, 'zh-CN');
  zh.ui.setLocale('en');
  assert.equal(zh.navigations[0], 'https://example.test/mounted/project/docs/schema/package.schema_en.html#fields');
  const fresh = setup('en', 'en');
  fresh.ui.setLocale('en');
  assert.equal(fresh.navigations.length, 0);
  await fresh.button.click();
  assert.equal(fresh.writes[0], fresh.code.textContent);
  assert.equal(fresh.button.textContent, 'Copied');
  fresh.window.reset();
  assert.equal(fresh.button.textContent, 'Copy code');
  fresh.window.location.hash = '#new-heading';
  fresh.handlers.hashchange();
  assert.ok(fresh.links.every(link => link.href.endsWith('#new-heading')));
  const denied = setup('en', 'en', true, true);
  await denied.button.click();
  assert.equal(denied.writes[0], 'fallback');
  assert.equal(denied.button.disabled, false);
  assert.equal(denied.button.textContent, 'Copy failed; select the code to copy');
  const noUI = setup('zh-CN', 'zh-CN', false);
  await noUI.button.click();
  assert.equal(noUI.button.textContent, '已复制');
  console.log('locale/hash/copy checks passed');
})().catch(error => {console.error(error); process.exitCode = 1});
'''
        result = subprocess.run(['node', '-e', harness, str(DEFAULT_ROOT / 'site/assets/docs.js')],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('locale/hash/copy checks passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
