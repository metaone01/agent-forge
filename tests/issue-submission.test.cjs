'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { parseIssue, prepareRecord, packagePath, encodedComponent, TARGET_BRANCH } = require('../tools/issue_submission.cjs');

const base = { schemaVersion: 2, id: '工具.example', name: '中文工具 🧪', version: '1.0.0', type: 'mcp' };
const json = (record = base) => JSON.stringify(record, null, 2);
const form = (payload, tail = '') => `Intro\n\n### Canonical package JSON\n\n${payload}${tail}`;
const rejects = (body, code) => assert.throws(() => parseIssue(body), (error) => error.code === code);
const decode = (encoded) => decodeURIComponent(encoded.replace(/~/g, '%'));

test('template plain JSON preserves identity and arbitrary fields without mutating', () => {
  const record = { ...base, _meta: { text: '### heading and ```json and \\"quoted"' } };
  assert.deepEqual(parseIssue(form(json(record))), { record, notes: '' });
  assert.equal(TARGET_BRANCH, 'packages');
});

test('optional notes: absent, empty, no response, and meaningful text', () => {
  for (const tail of ['', '\n\n### Notes', '\n\n### Submission notes\n', '\n\n### Submission notes\n\n_No response_']) {
    assert.equal(parseIssue(form(json(), tail)).notes, '');
  }
  assert.equal(parseIssue(form(json(), '\n\n### Submission notes\n\nPublic source links.')).notes, 'Public source links.');
});

test('raw complete JSON fallback including BOM and CRLF', () => {
  assert.deepEqual(parseIssue(`\ufeff  ${json()}\r\n`), { record: base, notes: '' });
  rejects(`${json()}\n### Notes\nnot a raw-body JSON`, 'JSON');
});

test('json and untagged fences, long fences, tilde fences, and CRLF', () => {
  for (const fence of ['```json', '```', '````JSON', '~~~json', '~~~~']) {
    const marker = fence.match(/^[`~]+/)[0];
    const body = form(`${fence}\n${json()}\n${marker}\n`, '\n### Notes\n_No response_');
    assert.deepEqual(parseIssue(body), { record: base, notes: '' });
    assert.deepEqual(parseIssue(body.replace(/\n/g, '\r\n')), { record: base, notes: '' });
  }
});

test('JSON strings cannot impersonate fences, headings, or delimiter boundaries', () => {
  const record = { ...base, description: '### Canonical package JSON\n```\n### Notes\n} ] \\ " 🧪',
    _meta: { a: [{ b: '```json' }, { b: '### Notes' }] } };
  for (const payload of [json(record), `\`\`\`json\n${json(record)}\n\`\`\``]) {
    assert.deepEqual(parseIssue(form(payload, '\n\n### Notes\nkept')), { record, notes: 'kept' });
  }
});

test('a canonical heading inside a prelude/notes fence is not a real section', () => {
  const prelude = '```text\n### Canonical package JSON\n{}\n```\n';
  const notes = '```text\n### Canonical package JSON\n```';
  assert.equal(parseIssue(prelude + form(json(), `\n### Notes\n${notes}`)).notes, notes);
  rejects(prelude, 'SECTION');
});

test('rejects missing sections, blank responses, unexpected trailing text, duplicate sections', () => {
  rejects('', 'BODY');
  rejects(null, 'BODY');
  rejects('No canonical section.', 'SECTION');
  rejects(form('_No response_'), 'JSON');
  rejects(form(json(), '\nextra text'), 'SECTION');
  rejects(form(json(), `\n\n### Canonical package JSON\n${json()}`), 'SECTION');
  rejects(form(json(), `\n### Notes\nnotes\n### Canonical package JSON\n${json()}`), 'SECTION');
  rejects(form(`${json()}\n${json()}`), 'SECTION');
});

test('rejects malformed JSON, mismatched delimiters, comments, duplicates and scalars', () => {
  for (const payload of ['{"id":"x"', '{"a": [}', '{"a": "unterminated}', '{"a":1,}', '{/*comment*/"a":1}',
    '{"id":"first","id":"second"}', '{"id":"first","\\u0069d":"second"}',
    '{"nested":{"key":1,"key":2}}']) rejects(form(payload), 'JSON');
  rejects(form('[]'), 'IDENTITY');
  rejects(form('null'), 'JSON');
  const record = { ...base, _meta: { left: { key: 1 }, right: { key: 2 } } };
  assert.deepEqual(parseIssue(json(record)).record, record);
});

test('rejects bad fences and never accepts an unfinished or inline fence', () => {
  for (const payload of [`\`\`\`json\n${json()}`, `\`\`\`json\n${json()}\n~~~`,
    `\`\`\`\`json\n${json()}\n\`\`\``, `\`\`\`js\n${json()}\n\`\`\``,
    `\`\`\`json\n${json()}\`\`\``, `\`\`\`json\n${json()}\n\`\`\` extra`]) rejects(form(payload), 'FENCE');
});

test('basic canonical identity guard rejects wrong types and damaged identities', () => {
  for (const delta of [{ schemaVersion: '2' }, { schemaVersion: 1 }, { type: '../mcp' }, { type: 'unknown' },
    { id: '' }, { id: 'has space' }, { id: 'literal\\backslash' }, { id: '\ud800' },
    { name: '  ' }, { name: null }, { name: '\udfff' }, { name: 'a\nb' }, { name: 'a\u007fb' },
    { version: '' }, { version: '   ' }, { version: 1 }, { version: '1\n2' }, { name: 'a'.repeat(4097) },
    { id: 'a'.repeat(4097) }]) {
    const record = { ...base, ...delta };
    rejects(json(record), 'IDENTITY');
    assert.throws(() => packagePath(record), { code: 'IDENTITY' });
  }
  const astral = { ...base, name: '🧪'.repeat(4096) };
  assert.equal(parseIssue(json(astral)).record.name, astral.name);
});

test('canonical validation can be injected but cannot silently accept failures/Promises', () => {
  let called = 0;
  assert.equal(parseIssue(json(), { validateCanonical(record) { called++; assert.deepEqual(record, base); return true; } }).record.id, base.id);
  assert.equal(called, 1);
  assert.doesNotThrow(() => parseIssue(json(), { validateCanonical() {} }));
  for (const validateCanonical of [() => false, () => [], () => ({ valid: false }), () => Promise.resolve(true), 3]) {
    assert.throws(() => parseIssue(json(), { validateCanonical }), { code: 'VALIDATION' });
  }
  assert.throws(() => parseIssue(json(), { validateCanonical() { throw new Error('targets required'); } }), /targets required/);
  assert.throws(() => parseIssue(json({ ...base, id: '' }), { validateCanonical() { assert.fail('must not run'); } }), { code: 'IDENTITY' });
});

test('tilde UTF-8 encoding matches Python safe bytes and is fully reversible', () => {
  assert.equal(encodedComponent('汉/🧪 %~!()@._+-'), '~E6~B1~89~2F~F0~9F~A7~AA~20~25~7E~21~28~29@._+-');
  for (const value of ['中文工具 🧪', '../a\\b/%2F/~2F', 'é', 'e\u0301', 'CON', '@scope/package', 'a\u0000b']) {
    assert.equal(decode(encodedComponent(value)), value);
  }
  assert.throws(() => encodedComponent('\ud800'), { code: 'PATH' });
});

test('path matches projection layout, does not alter identity, and retains the type boundary', () => {
  const before = json();
  const hash = (s) => createHash('sha256').update(s).digest('hex').slice(0, 12);
  for (const type of ['mcp', 'plugin', 'skill', 'general', 'bundle']) {
    assert.equal(packagePath({ ...base, type }), `sources/${type}/packages/${encodedComponent(base.name)}--${hash(base.name)}/${encodedComponent(base.version)}--${hash(base.version)}.json`);
  }
  assert.equal(json(), before);
});

test('traversal, Windows devices, slashes, percent escapes and punctuation remain inside packages', () => {
  const root = path.resolve('catalog-test-root');
  for (const value of ['../../escape', '..\\..\\escape', '/absolute', 'C:\\Windows', 'CON', 'NUL', '.', '..',
    '@scope/tool', '%2F', '~2F', 'a?b:c*"<>|', '工具']) {
    const result = packagePath({ ...base, name: value, version: value });
    assert.match(result, /^sources\/mcp\/packages\/[A-Za-z0-9@._+~\-]+\/[A-Za-z0-9@._+~\-]+\.json$/);
    assert.equal(result.split('/').length, 5);
    const relative = path.relative(root, path.resolve(root, result));
    assert.ok(!relative.startsWith('..') && !path.isAbsolute(relative));
  }
});

test('hashes separate slug, case, separator, normalization and truncated-prefix collisions', () => {
  const values = ['汉', '字', 'Foo', 'foo', 'a/b', 'a-b', '%2F', '~2F', 'é', 'e\u0301',
    'a'.repeat(200) + 'x', 'a'.repeat(200) + 'y'];
  const names = values.map((name) => packagePath({ ...base, name }).toLowerCase());
  const versions = values.map((version) => packagePath({ ...base, version }).toLowerCase());
  assert.equal(new Set(names).size, values.length);
  assert.equal(new Set(versions).size, values.length);
});

test('long Unicode prefixes retain whole reversible characters and bounded components', () => {
  const record = { ...base, name: '汉🧪'.repeat(800), version: '版🧪'.repeat(500) };
  const parts = packagePath(record).split('/');
  const name = parts[3].split('--')[0];
  const version = parts[4].split('--')[0];
  assert.ok(record.name.startsWith(decode(name)));
  assert.ok(record.version.startsWith(decode(version)));
  assert.ok(parts[3].length <= 124 && parts[4].length <= 83);
});

// Read added/context lines from the handoff patch; no YAML library or git calls.
function proposalPatch(file) {
  const fs = require('node:fs');
  const patch = fs.readFileSync(path.join(__dirname, '../docs/pages-control-plane.patch'), 'utf8');
  const section = patch.split('diff --git ').find((part) => part.startsWith(`a/${file} b/${file}\n`));
  assert.ok(section, `missing patch for ${file}`);
  return section.split('\n').filter((line) => (line[0] === '+' && !line.startsWith('+++')) || line[0] === ' ')
    .map((line) => line.slice(1)).join('\n');
}

function workflowScripts() {
  const proposed = proposalPatch('.github/workflows/issue-to-pr.yml');
  const scripts = [];
  const lines = proposed.split('\n');
  for (let i = 0; i < lines.length; i++) {
    if (lines[i] !== '          script: |') continue;
    const script = [];
    while (i + 1 < lines.length && (lines[i + 1].startsWith('            ') || !lines[i + 1].trim())) {
      script.push(lines[++i].slice(12));
    }
    scripts.push(script.join('\n'));
  }
  assert.equal(scripts.length, 2);
  return scripts;
}

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
function mockWorkflow({ body = json(), stale = false, symlink = false, exists = false, altered = false, helperTarget = 'packages' } = {}) {
  const helper = { parseIssue, prepareRecord, packagePath, TARGET_BRANCH: helperTarget };
  const workspace = path.resolve('offline-workflow');
  const issueCreatedAt = '2026-10-03T00:00:00Z';
  const prepared = prepareRecord(base, issueCreatedAt);
  const sha = 'a'.repeat(40);
  const calls = [];
  const writes = [];
  const fs = {
    mkdirSync(directory) { writes.push(['mkdir', directory]); },
    lstatSync() { return { isSymbolicLink: () => symlink, isDirectory: () => true }; },
    writeFileSync(file, content, options) {
      if (exists) throw Object.assign(new Error('exists'), { code: 'EEXIST' });
      writes.push(['write', file, content, options]);
    },
    readFileSync() { return json(altered ? { ...prepared, description: 'changed' } : prepared) + '\n'; },
  };
  const localRequire = (name) => {
    if (name === 'node:fs') return fs;
    if (name === 'node:path') return path;
    if (name === 'node:child_process') return { execFileSync(command, args) {
      assert.equal(command, 'git');
      assert.deepEqual(args, ['-C', path.join(workspace, 'canonical'), 'rev-parse', 'HEAD']);
      return sha + '\n';
    } };
    assert.equal(name, path.join(workspace, 'control/tools/issue_submission.cjs'));
    return helper;
  };
  const rest = {
    git: {
      async getRef(args) { calls.push(['getRef', args]); return { data: { object: { sha: stale ? 'b'.repeat(40) : sha } } }; },
      async createRef(args) { calls.push(['createRef', args]); },
    },
    repos: { async createOrUpdateFileContents(args) { calls.push(['file', args]); } },
    issues: {
      async addLabels(args) { calls.push(['labels', args]); },
      async createComment(args) { calls.push(['comment', args]); },
    },
    pulls: { async create(args) { calls.push(['pr', args]); return { data: { number: 456, html_url: 'mock-pr' } }; } },
  };
  const context = { repo: { owner: 'owner', repo: 'agent-forge' }, payload: { issue: { body, created_at: issueCreatedAt, number: 123, user: { login: 'submitter' } } } };
  const process = { env: { GITHUB_WORKSPACE: workspace, STAGED_SUBMISSION: JSON.stringify({ path: packagePath(base), baseSha: sha }) } };
  return { calls, writes, process, context, run(script) { return new AsyncFunction('require', 'context', 'github', 'process', 'Buffer', script)(localRequire, context, { rest }, process, Buffer); } };
}

test('workflow preflight stages locally with wx and performs zero GitHub mutations', async () => {
  const [stage] = workflowScripts();
  const mock = mockWorkflow();
  const result = await mock.run(stage);
  assert.deepEqual(result, { path: packagePath(base), baseSha: 'a'.repeat(40) });
  assert.deepEqual(mock.calls, []);
  assert.equal(mock.writes.at(-1)[3].flag, 'wx');
  assert.deepEqual(JSON.parse(mock.writes.at(-1)[2]), prepareRecord(base, mock.context.payload.issue.created_at));
  assert.ok(mock.writes.at(-1)[2].endsWith('\n'));
});

test('workflow preflight rejects malformed bodies, symlinks, existing paths and non-packages targets', async () => {
  const [stage] = workflowScripts();
  for (const options of [{ body: 'invalid' }, { symlink: true }, { exists: true }, { helperTarget: 'main' }]) {
    const mock = mockWorkflow(options);
    await assert.rejects(mock.run(stage));
    assert.deepEqual(mock.calls, []);
  }
});

test('publisher rejects an advanced packages base or changed payload before remote mutations', async () => {
  const [, publish] = workflowScripts();
  for (const options of [{ stale: true }, { altered: true }, { helperTarget: 'main' }]) {
    const mock = mockWorkflow(options);
    await assert.rejects(mock.run(publish));
    assert.ok(mock.calls.every(([method]) => method === 'getRef'));
  }
  const mock = mockWorkflow();
  mock.process.env.STAGED_SUBMISSION = JSON.stringify({ path: '../bad', baseSha: 'a'.repeat(40) });
  await assert.rejects(mock.run(publish), /staged submission/);
  assert.deepEqual(mock.calls, []);
});

test('publisher uses the validated base, safe path and packages-only PR target', async () => {
  const [, publish] = workflowScripts();
  const mock = mockWorkflow({ body: form(json(), '\n### Notes\nPublic sources.') });
  await mock.run(publish);
  const ref = mock.calls.find(([method]) => method === 'createRef')[1];
  assert.equal(ref.sha, 'a'.repeat(40));
  assert.equal(ref.ref, 'refs/heads/proposal/issue-123');
  const file = mock.calls.find(([method]) => method === 'file')[1];
  assert.equal(file.path, packagePath(base));
  assert.equal(file.branch, 'proposal/issue-123');
  assert.deepEqual(JSON.parse(Buffer.from(file.content, 'base64')), prepareRecord(base, mock.context.payload.issue.created_at));
  const pr = mock.calls.find(([method]) => method === 'pr')[1];
  assert.equal(pr.base, 'packages');
  assert.match(pr.body, /final PR\/merge-group commit/);
  assert.match(pr.body, /Submission notes:\nPublic sources\./);
});

test('patch validates in the actual canonical checkout before token/branch creation', () => {
  const issue = proposalPatch('.github/workflows/issue-to-pr.yml');
  const validation = issue.indexOf('python tools/validate.py "$record_path"');
  assert.ok(validation > 0);
  assert.ok(issue.includes('working-directory: canonical'));
  assert.ok(issue.includes('python tools/validate.py --all'));
  assert.ok(validation < issue.indexOf('Create short-lived GitHub App token'));
  assert.ok(validation < issue.indexOf('github.rest.git.createRef'));
  const release = proposalPatch('.github/workflows/release.yml');
  assert.ok(release.includes('cp -R control/site/. site/'));
  assert.ok(release.includes('python tools/build_site.py --output pages-staging --data data --root .'));
  assert.ok(release.includes('[ "$last_control" != "$CONTROL_SHA" ]'));
  assert.ok(proposalPatch('.github/ISSUE_TEMPLATE/package-submission.yml').includes('required: false'));
});

test('patch overlays only selected control contracts/tools and gates formats on the new submission', () => {
  const issue = proposalPatch('.github/workflows/issue-to-pr.yml');
  assert.ok(issue.includes('cp control/tools/validate.py control/tools/identity.py canonical/tools/'));
  assert.ok(issue.includes('"canonical/${schema}.schema.json"'));
  assert.ok(issue.includes('python tools/validate.py "$record_path" --submission-formats'));
  assert.ok(!issue.includes('python tools/validate.py --all --submission-formats'));
  assert.ok(issue.indexOf('Overlay main submission validator') < issue.indexOf('Parse issue and stage'));
  const release = proposalPatch('.github/workflows/release.yml');
  for (const line of ['cp -R control/site/. site/', 'cp control/README.md control/README_en.md .',
    'cp -R control/docs/schema/. docs/schema/', 'cp control/tools/build_site.py control/tools/project.py control/tools/validate.py control/tools/identity.py tools/']) {
    assert.ok(release.includes(line), line);
  }
  assert.ok(!release.includes('--submission-formats'));
  assert.ok(!release.includes('cp -R control/tools'));
  const template = proposalPatch('.github/ISSUE_TEMPLATE/package-submission.yml');
  assert.ok(template.includes('Agent / JSON package submission'));
  assert.ok(template.includes('Agents may paste one complete Schema v2'));
  assert.ok(template.includes('Never include tokens'));
  assert.ok(proposalPatch('.github/ISSUE_TEMPLATE/config.yml').includes('Visual package upload / 可视化上传包'));
});

test('missing all timestamps adds deterministic GitHub record createdAt without changing submitted data', () => {
  const timestamp = '2026-10-03T01:02:03Z';
  const original = Object.freeze({ ...base });
  const prepared = prepareRecord(original, timestamp);
  assert.deepEqual(prepared, { ...base, createdAt: timestamp });
  assert.notEqual(prepared, original);
  assert.deepEqual(original, base);
  assert.equal(Object.hasOwn(prepared, 'publishedAt'), false);
  assert.equal(Object.hasOwn(prepared, 'updatedAt'), false);
  assert.deepEqual(prepareRecord(original, timestamp), prepared);
  assert.equal(packagePath(prepared), packagePath(base));
});

test('any existing user timestamp is preserved verbatim without adding other timestamps', () => {
  const issueTime = '2026-10-03T01:02:03Z';
  for (const field of ['createdAt', 'updatedAt', 'publishedAt']) {
    for (const value of ['2026-09-30T23:59:59+08:00', null, '', 'invalid']) {
      // Invalid user values are left to Python canonical checks, never repaired.
      const submitted = Object.freeze({ ...base, [field]: value });
      assert.equal(prepareRecord(submitted, issueTime), submitted);
      assert.deepEqual(prepareRecord(submitted, issueTime), submitted);
    }
  }
  const all = Object.freeze({ ...base, createdAt: '2026-01-01T00:00:00Z', updatedAt: '2026-02-01T00:00:00Z', publishedAt: '2026-03-01T00:00:00Z' });
  assert.equal(prepareRecord(all, issueTime), all);
});

test('GitHub creation time is fail-closed for invalid date, time, offset or missing value', () => {
  for (const invalid of [undefined, null, 0, '', 'today', '2026-10-03', '2026-02-30T00:00:00Z',
    '2025-02-29T00:00:00Z', '1900-02-29T00:00:00Z', '0000-01-01T00:00:00Z', '2026-13-01T00:00:00Z',
    '2026-01-00T00:00:00Z', '2026-10-03T24:00:00Z', '2026-10-03T00:60:00Z', '2026-10-03T00:00:60Z',
    '2026-10-03T00:00:00', '2026-10-03T00:00:00+24:00', '2026-10-03T00:00:00+08:60']) {
    assert.throws(() => prepareRecord(base, invalid), { code: 'TIMESTAMP' });
    assert.throws(() => prepareRecord({ ...base, publishedAt: '2026-01-01T00:00:00Z' }, invalid), { code: 'TIMESTAMP' });
  }
  for (const valid of ['2000-02-29T00:00:00Z', '2024-02-29T23:59:59+08:00', '2026-10-03T01:02:03.123Z']) {
    assert.equal(prepareRecord(base, valid).createdAt, valid);
  }
});

test('workflow stage and publish derive the same timestamp and reject invalid event time before mutation', async () => {
  const [stage, publish] = workflowScripts();
  for (const script of [stage, publish]) {
    assert.ok(script.includes('prepareRecord(submitted, issue.created_at)'));
    assert.ok(!script.includes('Date.now'));
    const mock = mockWorkflow();
    mock.context.payload.issue.created_at = '2026-02-30T00:00:00Z';
    await assert.rejects(mock.run(script), { code: 'TIMESTAMP' });
    assert.deepEqual(mock.calls, []);
    assert.deepEqual(mock.writes, []);
  }
  const mock = mockWorkflow();
  const staged = await mock.run(stage);
  mock.process.env.STAGED_SUBMISSION = JSON.stringify(staged);
  await mock.run(publish);
  const local = JSON.parse(mock.writes.at(-1)[2]);
  const remote = JSON.parse(Buffer.from(mock.calls.find(([method]) => method === 'file')[1].content, 'base64'));
  assert.deepEqual(remote, local);
  assert.equal(remote.createdAt, mock.context.payload.issue.created_at);
});
