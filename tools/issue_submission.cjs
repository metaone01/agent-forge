'use strict';

const { createHash } = require('node:crypto');

const TARGET_BRANCH = 'packages';
const TYPES = new Set(['mcp', 'plugin', 'skill', 'general', 'bundle']);
const CANONICAL_HEADING = /^### Canonical package JSON[ \t]*$/;
const CONTROLS = /[\u0000-\u001f\u007f]/u;

function fail(code, message) {
  const error = new Error(message);
  error.code = code;
  throw error;
}

function wellFormed(value) {
  // Buffer/encodeURIComponent must never silently replace an unpaired surrogate.
  for (let i = 0; i < value.length; i++) {
    const unit = value.charCodeAt(i);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = value.charCodeAt(++i);
      if (!(next >= 0xdc00 && next <= 0xdfff)) return false;
    } else if (unit >= 0xdc00 && unit <= 0xdfff) return false;
  }
  return true;
}

function validateIdentity(record) {
  if (!record || typeof record !== 'object' || Array.isArray(record)) {
    fail('IDENTITY', 'The canonical record must be a JSON object.');
  }
  if (record.schemaVersion !== 2 || !TYPES.has(record.type)) {
    fail('IDENTITY', 'Expected schemaVersion=2 and type mcp, plugin, skill, general, or bundle.');
  }
  for (const field of ['id', 'name', 'version']) {
    const value = record[field];
    if (typeof value !== 'string' || !/\S/u.test(value) || CONTROLS.test(value) || !wellFormed(value)) {
      fail('IDENTITY', `${field} must be a nonblank, well-formed Unicode string without control characters.`);
    }
    if (field !== 'version' && [...value].length > 4096) {
      fail('IDENTITY', `${field} exceeds the canonical 4096-code-point limit.`);
    }
  }
  if (/[\s\\]/u.test(record.id)) {
    fail('IDENTITY', 'id must not contain whitespace or literal backslashes (package.schema.json identifier).');
  }
  return record;
}

function skipSpace(text, offset) {
  while (offset < text.length && /[ \t\r\n]/.test(text[offset])) offset++;
  return offset;
}

function jsonValue(text, offset) {
  const start = offset;
  const stack = [];
  let end;
  if (text[offset] !== '{' && text[offset] !== '[') {
    fail('JSON', 'Expected one complete canonical JSON object.');
  }
  for (; offset < text.length; offset++) {
    const char = text[offset];
    if (char === '"') {
      const stringStart = offset++;
      let closed = false;
      for (; offset < text.length; offset++) {
        if (text[offset] === '\\') offset++;
        else if (text[offset] === '"') { closed = true; break; }
      }
      if (!closed) fail('JSON', 'Invalid JSON: unterminated string.');
      const frame = stack.at(-1);
      if (frame && frame.kind === '{' && text[skipSpace(text, offset + 1)] === ':') {
        let key;
        try { key = JSON.parse(text.slice(stringStart, offset + 1)); }
        catch (error) { fail('JSON', `Invalid JSON key: ${error.message}`); }
        if (frame.keys.has(key)) fail('JSON', `Duplicate JSON key: ${JSON.stringify(key)}.`);
        frame.keys.add(key);
      }
    } else if (char === '{' || char === '[') {
      stack.push({ kind: char, keys: new Set() });
    } else if (char === '}' || char === ']') {
      const frame = stack.pop();
      if (!frame || (char === '}' ? frame.kind !== '{' : frame.kind !== '[')) {
        fail('JSON', 'Invalid JSON: mismatched closing delimiter.');
      }
      if (!stack.length) { end = offset + 1; break; }
    }
  }
  if (end === undefined) fail('JSON', 'Invalid JSON: incomplete object or array.');
  let record;
  try { record = JSON.parse(text.slice(start, end)); }
  catch (error) { fail('JSON', `Invalid JSON: ${error.message}`); }
  return { record, end };
}

function canonicalHeadings(text) {
  const headings = [];
  let offset = 0;
  let fence;
  for (const rawLine of text.split('\n')) {
    const line = rawLine.replace(/\r$/, '');
    if (fence) {
      const close = line.match(/^ {0,3}(`{3,}|~{3,})[ \t]*$/);
      if (close && close[1][0] === fence[0] && close[1].length >= fence.length) fence = undefined;
    } else {
      const open = line.match(/^ {0,3}(`{3,}|~{3,})([^\r\n]*)$/);
      if (open) fence = open[1];
      else if (CANONICAL_HEADING.test(line)) headings.push(offset + rawLine.length + 1);
    }
    offset += rawLine.length + 1;
  }
  return headings;
}

/**
 * Returns {record, notes}; no filesystem/network access. validateCanonical is
 * synchronous: return true/undefined on success, false on failure, or throw.
 * This identity guard is NOT a replacement for the Python canonical validator.
 */
function parseIssue(body, { validateCanonical } = {}) {
  if (typeof body !== 'string' || !body.trim()) fail('BODY', 'The issue body is empty.');
  const text = body.replace(/^\ufeff/, '').trim();
  let start = 0;
  let fenced;
  let rawBody = text[0] === '{' || text[0] === '[';
  if (!rawBody) {
    const headings = canonicalHeadings(text);
    if (!headings.length) fail('SECTION', 'The ### Canonical package JSON section is missing.');
    // Only inspect the prelude here; the JSON scanner owns the payload boundary.
    start = skipSpace(text, headings[0]);
    if (text[start] === '`' || text[start] === '~') {
      const opening = text.slice(start).match(/^(`{3,}|~{3,})[ \t]*(?:json)?[ \t]*\r?\n/i);
      if (!opening) fail('FENCE', 'Use an untagged or json Markdown fence on its own line.');
      fenced = opening[1];
      start += opening[0].length;
      start = skipSpace(text, start);
    }
  }
  const { record, end } = jsonValue(text, start);
  let tail = text.slice(end);
  if (fenced) {
    const closerStart = skipSpace(tail, 0);
    const whitespace = tail.slice(0, closerStart);
    const indentation = whitespace.slice(whitespace.lastIndexOf('\n') + 1);
    const closing = tail.slice(closerStart).match(new RegExp(`^${fenced[0]}{${fenced.length},}[ \\t]*(?:\\r?\\n|$)`));
    if (!whitespace.includes('\n') || !/^ {0,3}$/.test(indentation) || !closing) {
      fail('FENCE', 'Missing or mismatched closing Markdown fence on its own line.');
    }
    tail = tail.slice(closerStart + closing[0].length);
  }
  tail = tail.trim();
  let notes = '';
  if (rawBody && tail) fail('JSON', 'Raw-body fallback must contain only one complete JSON object.');
  if (!rawBody && tail) {
    const notesHeading = tail.match(/^### (?:Submission notes|Notes)[ \t]*(?:\r?\n|$)/i);
    if (!notesHeading) fail('SECTION', 'Unexpected text after JSON; only the optional Submission notes/Notes section is allowed.');
    notes = tail.slice(notesHeading[0].length).trim();
    if (canonicalHeadings(notes).length) fail('SECTION', 'Duplicate Canonical package JSON section.');
    if (/^_No response_$/i.test(notes)) notes = '';
  }
  validateIdentity(record);
  if (validateCanonical !== undefined) {
    if (typeof validateCanonical !== 'function') fail('VALIDATION', 'validateCanonical must be a synchronous function.');
    let result;
    try { result = validateCanonical(record); }
    catch (error) { fail('VALIDATION', `Canonical validation failed: ${error.message || String(error)}`); }
    if (result !== true && result !== undefined) {
      fail('VALIDATION', 'Canonical validation failed or returned an unsupported result; async validators are not supported.');
    }
  }
  return { record, notes };
}

/** Add catalog record-creation time only when no user timestamp exists. */
function prepareRecord(record, issueCreatedAt) {
  validateIdentity(record);
  // Date.parse alone normalizes impossible dates (e.g. February 30).
  const match = typeof issueCreatedAt === 'string' && issueCreatedAt.match(
    /^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:[Zz]|[+-](\d{2}):(\d{2}))$/
  );
  if (!match) fail('TIMESTAMP', 'GitHub issue.created_at must be a valid RFC 3339 creation timestamp.');
  const [, yearText, monthText, dayText, hourText, minuteText, secondText, offsetHour = '0', offsetMinute = '0'] = match;
  const [year, month, day, hour, minute, second] = [yearText, monthText, dayText, hourText, minuteText, secondText].map(Number);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (year < 1 || month < 1 || month > 12 || day < 1 || day > days[month - 1] ||
      hour > 23 || minute > 59 || second > 59 || Number(offsetHour) > 23 || Number(offsetMinute) > 59) {
    fail('TIMESTAMP', 'GitHub issue.created_at contains an invalid calendar date, time, or offset.');
  }
  if (['createdAt', 'updatedAt', 'publishedAt'].some((field) => Object.hasOwn(record, field))) return record;
  // This is catalog record creation, NOT a claim about upstream publication.
  return { ...record, createdAt: issueCreatedAt };
}

function encodedComponent(value) {
  if (typeof value !== 'string' || !wellFormed(value)) fail('PATH', 'Path input must be well-formed Unicode.');
  // Same safe bytes and tilde escapes as tools.identity.encoded_component.
  return [...Buffer.from(value, 'utf8')].map((byte) => {
    const char = String.fromCharCode(byte);
    return /^[A-Za-z0-9@._+\-]$/.test(char) ? char : `~${byte.toString(16).toUpperCase().padStart(2, '0')}`;
  }).join('') || '_';
}

function encodedPrefix(value, limit) {
  let prefix = '';
  // Unlike a raw slice, this preserves both escape tokens and UTF-8 characters.
  for (const char of value) {
    const encoded = encodedComponent(char);
    if (prefix.length + encoded.length > limit) break;
    prefix += encoded;
  }
  return prefix;
}

function packagePath(record) {
  validateIdentity(record);
  const hash = (value) => createHash('sha256').update(value, 'utf8').digest('hex').slice(0, 12);
  // Mirrors projected_record_path: hashes distinguish case and clipped prefixes.
  return `sources/${record.type}/packages/${encodedPrefix(record.name, 110)}--${hash(record.name)}/${encodedPrefix(record.version, 64)}--${hash(record.version)}.json`;
}

module.exports = { parseIssue, prepareRecord, packagePath, encodedComponent, TARGET_BRANCH };
