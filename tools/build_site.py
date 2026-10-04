"""Build the static catalog and bilingual documentation using only the stdlib.

Supported Markdown: ATX headings (stable Unicode/duplicate anchors), paragraphs,
inline code, emphasis/strong/strike, links/images, ordered/unordered nested lists,
quotes and GitHub alerts, pipe tables, thematic rules, fenced code, and the exact
<details>/<summary> markers used by our READMEs. Other HTML is escaped, never
passed through. No extensions, reference links, math, or executable Mermaid;
unknown fence languages (including Mermaid) are displayed as plain code.
JSON, TypeScript/JavaScript and CSS receive lexical, not semantic, highlighting.

Output is additive: only named build artifacts are replaced, never recursively
removed. Use a dedicated staging directory; obsolete unrelated files are kept.
Relative --output/--data paths are resolved against --root, not the caller's cwd.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import posixpath
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit, urlunsplit

REPOSITORY = "https://github.com/metaone01/agent-forge"
PUBLISHED = "https://metaone01.github.io/agent-forge/"
SCHEMAS = ("package", "source", "index", "advisory")
DEFAULT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Document:
    source: str
    target: str
    locale: str
    peer: str


def documents() -> list[Document]:
    pairs = [("README", "docs/index"), ("docs/schema/README", "docs/schema/README")]
    pairs += [(f"docs/schema/{name}.schema", f"docs/schema/{name}.schema") for name in SCHEMAS]
    result = []
    for source, target in pairs:
        for suffix, locale, peer_suffix in (("", "zh-CN", "_en"), ("_en", "en", "")):
            result.append(Document(source + suffix + ".md", target + suffix + ".html", locale,
                                   target + peer_suffix + ".html"))
    return result


DOCUMENTS = documents()
DOC_TARGETS = {doc.source: doc.target for doc in DOCUMENTS}


def escape(value: str) -> str:
    return html.escape(value, quote=True)


def relative(target: str, page: str) -> str:
    return posixpath.relpath(target, posixpath.dirname(page))


def safe_url(value: str, image: bool = False) -> str | None:
    """Validate both literal and decoded spelling; never allow scheme smuggling."""
    value = html.unescape(value.strip())
    decoded = value
    for _ in range(3):
        decoded = unquote(decoded)
    # Escaped JSON query values may contain newlines/backslashes. They cannot
    # alter the scheme/authority; reject decoded controls only in the URL prefix.
    prefix = decoded.split("?", 1)[0].split("#", 1)[0]
    if any(ord(c) < 32 or ord(c) == 127 for c in value + prefix) or "\\" in value + prefix:
        return None
    if decoded.startswith("//"):
        return None
    try:
        parsed = urlsplit(decoded)
        if parsed.scheme:
            allowed = {"http", "https"} if image else {"http", "https", "mailto"}
            if parsed.scheme.lower() not in allowed:
                return None
            if parsed.scheme.lower() in {"http", "https"} and (not parsed.hostname or parsed.username):
                return None
        elif ":" in decoded.split("/", 1)[0].split("#", 1)[0].split("?", 1)[0]:
            return None
    except ValueError:
        return None
    return value


class Links:
    def __init__(self, root: Path, document: Document, data_files: set[str]):
        self.root, self.document, self.data_files = root, document, data_files

    def resolve(self, value: str, image: bool = False) -> str | None:
        value = safe_url(value, image)
        if value is None:
            return None
        parsed = urlsplit(value)
        # Convert our own published URLs to relative URLs for preview/project mounts.
        if value.startswith(PUBLISHED):
            path = unquote(parsed.path[len(urlsplit(PUBLISHED).path):])
            normalized = posixpath.normpath(path)
            if normalized == ".." or normalized.startswith("../"):
                return None
            target = DOC_TARGETS.get(normalized, normalized)
            if not path:
                target = "index.html"
            elif path.endswith("/"):
                target = normalized + "/index.html"
            known = (target in DOC_TARGETS.values() or target in self.data_files or
                     target in {name + ".schema.json" for name in SCHEMAS} or
                     (self.root / "site" / target).is_file())
            if not known:
                return value
            result = relative(target, self.document.target)
            if path.endswith("/") and target.endswith("/index.html"):
                result = result[:-len("index.html")]
            return urlunsplit(("", "", result, parsed.query, parsed.fragment))
        if parsed.scheme:
            return value
        if not parsed.path:
            return value
        path = unquote(parsed.path)
        if path.startswith("/"):
            normalized = posixpath.normpath(path.lstrip("/"))
        else:
            normalized = posixpath.normpath(posixpath.join(posixpath.dirname(self.document.source), path))
        if normalized == ".." or normalized.startswith("../"):
            return None
        target = DOC_TARGETS.get(normalized)
        if target is None and normalized in {name + ".schema.json" for name in SCHEMAS}:
            target = normalized
        if target is None and normalized in self.data_files:
            target = normalized
        if target is None and normalized.startswith("site/") and (self.root / normalized).exists():
            target = normalized[5:]
        if target is not None:
            return urlunsplit(("", "", relative(target, self.document.target), parsed.query, parsed.fragment))
        # Repository files are not silently copied into Pages; link to real main.
        candidate = self.root / normalized
        tree = path.endswith("/") or candidate.is_dir()
        prefix = "tree" if tree else "blob"
        return urlunsplit(("https", "github.com", "/metaone01/agent-forge/" + prefix + "/main/" +
                           quote(normalized, safe="/"), parsed.query, parsed.fragment))


def _closing(text: str, start: int, opening: str, closing: str) -> int:
    depth = 1
    i = start + 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == opening:
            depth += 1
        elif text[i] == closing:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def inline(text: str, links: Links, depth: int = 0, allow_links: bool = True) -> str:
    if depth > 12:
        return escape(text)
    result = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] in r"\`*_{}[]()#+-.!|>~":
            result.append(escape(text[i + 1]))
            i += 2
            continue
        if text[i] == "`":
            marker = re.match(r"`+", text[i:]).group()
            end = text.find(marker, i + len(marker))
            if end >= 0:
                result.append("<code>" + escape(text[i + len(marker):end]) + "</code>")
                i = end + len(marker)
                continue
        is_image = text.startswith("![", i)
        bracket = i + 1 if is_image else i
        if (allow_links or is_image) and text[bracket:bracket + 1] == "[":
            end = _closing(text, bracket, "[", "]")
            if end >= 0 and text[end + 1:end + 2] == "(":
                close = _closing(text, end + 1, "(", ")")
                if close >= 0:
                    destination = text[end + 2:close].strip()
                    # Optional quoted titles are deliberately not rendered.
                    destination = re.sub(r'\s+["\'].*["\']$', "", destination)
                    if destination.startswith("<") and destination.endswith(">"):
                        destination = destination[1:-1]
                    url = links.resolve(destination, is_image)
                    label = text[bracket + 1:end]
                    if url is None:
                        result.append(escape(label) if is_image else inline(label, links, depth + 1, False))
                    elif is_image:
                        result.append(f'<img src="{escape(url)}" alt="{escape(label)}" loading="lazy" referrerpolicy="no-referrer">')
                    else:
                        result.append(f'<a href="{escape(url)}" rel="noreferrer">' + inline(label, links, depth + 1, False) + "</a>")
                    i = close + 1
                    continue
        if allow_links and text[i] == "<":
            end = text.find(">", i + 1)
            if end >= 0 and re.match(r"https?://", text[i + 1:end]):
                url = links.resolve(text[i + 1:end])
                if url:
                    result.append(f'<a href="{escape(url)}" rel="noreferrer">{escape(text[i + 1:end])}</a>')
                    i = end + 1
                    continue
        matched = False
        for marker, tag in (("**", "strong"), ("__", "strong"), ("~~", "del"), ("*", "em"), ("_", "em")):
            # Do not italicize snake_case paths or identifiers.
            if marker.startswith("_") and i and text[i - 1].isalnum():
                continue
            if text.startswith(marker, i):
                end = text.find(marker, i + len(marker))
                if end > i + len(marker):
                    result.append(f"<{tag}>" + inline(text[i + len(marker):end], links, depth + 1, allow_links) + f"</{tag}>")
                    i = end + len(marker)
                    matched = True
                    break
        if not matched:
            result.append(escape(text[i]))
            i += 1
    return "".join(result)


LEXER = re.compile(
    r'(?P<comment>//[^\n]*|/\*[\s\S]*?\*/)|'
    r'''(?P<string>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)|'''
    r'(?P<number>\b(?:0[xX][\da-fA-F]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\b)|'
    r'(?P<word>[$\w-]+)|(?P<punct>[{}\[\]():,;.=<>+*/!?|&%-])', re.UNICODE
)
KEYWORDS = set("const let var as interface type extends export import from return readonly keyof typeof new function string number boolean null true false undefined satisfies async await".split())


def highlight(code: str, language: str) -> str:
    language = language.lower()
    if language not in {"json", "ts", "typescript", "js", "javascript", "css", "style"}:
        return escape(code)
    result, previous = [], 0
    for match in LEXER.finditer(code):
        result.append(escape(code[previous:match.start()]))
        token, kind = match.group(), match.lastgroup
        if kind in {"string", "word"} and re.match(r"\s*:", code[match.end():]):
            kind = "property"
        elif kind == "word":
            if token in KEYWORDS:
                kind = "keyword"
            elif language in {"css", "style"} and token.startswith("--"):
                kind = "property"
            else:
                kind = "identifier"
        result.append(f'<span class="tok-{kind}">{escape(token)}</span>')
        previous = match.end()
    result.append(escape(code[previous:]))
    return "".join(result)


HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)(?:\s+#+\s*)?$")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
LIST = re.compile(r"^( {0,3})([-+*]|\d+\.)\s+(.+)$")
RULE = re.compile(r"^ {0,3}(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$")


def cells(line: str) -> list[str]:
    line = line.strip().strip("|")
    result, current, ticks = [], [], ""
    i = 0
    while i < len(line):
        if line[i] == "\\" and i + 1 < len(line):
            current.extend(line[i:i + 2])
            i += 2
            continue
        if line[i] == "`":
            marker = re.match(r"`+", line[i:]).group()
            ticks = "" if marker == ticks else marker if not ticks else ticks
            current.append(marker)
            i += len(marker)
            continue
        if line[i] == "|" and not ticks:
            result.append("".join(current).strip())
            current = []
        else:
            current.append(line[i])
        i += 1
    result.append("".join(current).strip())
    return result


class Markdown:
    def __init__(self, links: Links):
        self.links = links
        self.headings: list[tuple[int, str, str]] = []
        self.slugs: dict[str, int] = {}
        self.code_count = 0

    def render(self, text: str) -> str:
        return self.blocks(text.splitlines())

    def is_table(self, lines: list[str], i: int) -> bool:
        return i + 1 < len(lines) and "|" in lines[i] and all(
            re.fullmatch(r":?-{3,}:?", cell) for cell in cells(lines[i + 1]))

    def starts_block(self, lines: list[str], i: int) -> bool:
        line = lines[i]
        return bool(not line.strip() or HEADING.match(line) or FENCE.match(line) or LIST.match(line) or
                    RULE.match(line) or line.lstrip().startswith(">") or line.strip() == "<details>" or
                    self.is_table(lines, i))

    def blocks(self, lines: list[str], depth: int = 0) -> str:
        if depth > 24:
            return "<p>" + escape("\n".join(lines)) + "</p>"
        output = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            fence = FENCE.match(line)
            if fence:
                marker, language = fence.groups()
                language = language.strip().split()[0] if language.strip() else "text"
                code = []
                i += 1
                while i < len(lines) and not re.fullmatch(r" {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}\s*", lines[i]):
                    code.append(lines[i])
                    i += 1
                i += 1
                self.code_count += 1
                number = self.code_count
                copy = "Copy code" if self.links.document.locale == "en" else "复制代码"
                output.append(f'<div class="code-block"><div class="code-toolbar"><span>{escape(language)}</span>'
                              f'<button type="button" data-copy="code-{number}" data-i18n="docs.copy">{copy}</button></div>'
                              f'<pre><code id="code-{number}" class="language-{escape(language)}">' +
                              highlight("\n".join(code) + "\n", language) + "</code></pre></div>")
                continue
            heading = HEADING.match(line)
            if heading:
                level, title = len(heading[1]), heading[2]
                rendered = inline(title, self.links)
                plain = html.unescape(re.sub(r"<[^>]*>", "", rendered))
                base = re.sub(r"[^\w\s-]", "", plain.lower())
                base = re.sub(r"\s+", "-", base).strip("-") or "section"
                count = self.slugs.get(base, 0)
                slug = base if not count else f"{base}-{count}"
                while slug in self.slugs:
                    count += 1
                    slug = f"{base}-{count}"
                self.slugs[base] = count + 1
                self.slugs[slug] = max(self.slugs.get(slug, 0), 1)
                self.headings.append((level, slug, plain))
                output.append(f'<h{level} id="{escape(slug)}">{rendered}<a class="heading-anchor" href="#{escape(slug)}" aria-label="Permalink">#</a></h{level}>')
                i += 1
                continue
            if RULE.match(line):
                output.append("<hr>")
                i += 1
                continue
            if line.strip() == "<details>":
                end = next((j for j in range(i + 1, len(lines)) if lines[j].strip() == "</details>"), -1)
                summary = re.fullmatch(r"\s*<summary>(.*?)</summary>\s*", lines[i + 1]) if i + 1 < len(lines) else None
                if end >= 0 and summary:
                    output.append("<details><summary>" + inline(summary[1], self.links) + "</summary>" +
                                  self.blocks(lines[i + 2:end], depth + 1) + "</details>")
                    i = end + 1
                    continue
            if line.lstrip().startswith(">"):
                quoted = []
                while i < len(lines) and lines[i].lstrip().startswith(">"):
                    quoted.append(re.sub(r"^\s*> ?", "", lines[i]))
                    i += 1
                alert = re.fullmatch(r"\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]", quoted[0])
                title = ""
                if alert:
                    kind = alert[1].lower()
                    title = f'<p class="alert-title">{alert[1]}</p>'
                    quoted = quoted[1:]
                else:
                    kind = "quote"
                output.append(f'<blockquote class="alert alert-{kind}">' + title + self.blocks(quoted, depth + 1) + "</blockquote>")
                continue
            if self.is_table(lines, i):
                header, aligns = cells(line), cells(lines[i + 1])
                styles = ["center" if c.startswith(":") and c.endswith(":") else "right" if c.endswith(":") else "left" for c in aligns]
                def row(values: list[str], tag: str) -> str:
                    return "<tr>" + "".join(f'<{tag} class="align-{styles[n] if n < len(styles) else "left"}">' +
                        inline(values[n] if n < len(values) else "", self.links) + f"</{tag}>" for n in range(len(header))) + "</tr>"
                table = ['<div class="table-scroll" tabindex="0" role="region" aria-label="Table"><table><thead>', row(header, "th"), "</thead><tbody>"]
                i += 2
                while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                    table.append(row(cells(lines[i]), "td"))
                    i += 1
                output.append("".join(table) + "</tbody></table></div>")
                continue
            item = LIST.match(line)
            if item:
                ordered = item[2][0].isdigit()
                tag = "ol" if ordered else "ul"
                start = f' start="{int(item[2][:-1])}"' if ordered else ""
                items = []
                base_indent = len(item[1])
                while i < len(lines):
                    match = LIST.match(lines[i])
                    if not match or len(match[1]) != base_indent or match[2][0].isdigit() != ordered:
                        break
                    content_indent = len(match[1]) + len(match[2]) + 1
                    content = [match[3]]
                    i += 1
                    while i < len(lines):
                        next_item = LIST.match(lines[i])
                        if next_item and len(next_item[1]) <= base_indent:
                            break
                        if not lines[i].strip():
                            if i + 1 < len(lines) and lines[i + 1].startswith(" " * content_indent):
                                content.append("")
                                i += 1
                                continue
                            break
                        if not lines[i].startswith(" " * content_indent):
                            break
                        content.append(lines[i][content_indent:])
                        i += 1
                    items.append("<li>" + self.blocks(content, depth + 1) + "</li>")
                output.append(f"<{tag}{start}>" + "".join(items) + f"</{tag}>")
                continue
            paragraph = [line.strip()]
            i += 1
            while i < len(lines) and not self.starts_block(lines, i):
                paragraph.append(lines[i].strip())
                i += 1
            output.append("<p>" + inline("\n".join(paragraph), self.links) + "</p>")
        return "\n".join(output)


def render_document(root: Path, doc: Document, data_files: set[str]) -> str:
    parser = Markdown(Links(root, doc, data_files))
    content = parser.render((root / doc.source).read_text(encoding="utf-8-sig"))
    peer_doc = next(item for item in DOCUMENTS if item.target == doc.peer)
    peer_parser = Markdown(Links(root, peer_doc, data_files))
    peer_parser.render((root / peer_doc.source).read_text(encoding="utf-8-sig"))
    peer_ids = {slug for _, slug, _ in peer_parser.headings}
    anchor_map = {slug: slug for _, slug, _ in parser.headings if slug in peer_ids}
    # Our paired references share a hierarchy. Do not guess when translations diverge structurally.
    if [level for level, _, _ in parser.headings] == [level for level, _, _ in peer_parser.headings]:
        anchor_map.update({current[1]: peer[1] for current, peer in zip(parser.headings, peer_parser.headings)})
    anchor_json = escape(json.dumps(anchor_map, ensure_ascii=False, separators=(",", ":")))
    english = doc.locale == "en"
    words = {
        "catalog": "Catalog" if english else "目录", "docs": "Docs" if english else "文档",
        "submit": "Submit" if english else "提交记录", "overview": "Overview" if english else "概览",
        "reference": "Reference" if english else "参考", "toc": "On this page" if english else "本页目录",
        "raw": "Markdown source" if english else "Markdown 原文", "repo": "Source on main" if english else "main 分支原文",
        "skip": "Skip to content" if english else "跳到正文",
    }
    suffix = "_en" if english else ""
    def link(target: str, text: str, key: str | None = None) -> str:
        current = ' aria-current="page"' if target == doc.target else ""
        translation = f' data-i18n="docs.{key}"' if key else ""
        return f'<a href="{escape(relative(target, doc.target))}"{current}{translation}>{escape(text)}</a>'
    groups = '<h2 data-i18n="docs.overview">' + words["overview"] + "</h2>" + link(f"docs/index{suffix}.html", "Agent Forge")
    groups += '<h2 data-i18n="docs.reference">' + words["reference"] + "</h2>" + link(f"docs/schema/README{suffix}.html", "Schema")
    groups += "".join(link(f"docs/schema/{name}.schema{suffix}.html", name + ".schema.json") for name in SCHEMAS)
    toc = "".join(f'<li class="toc-level-{level}"><a href="#{escape(slug)}">{escape(title)}</a></li>' for level, slug, title in parser.headings)
    title = parser.headings[0][2] if parser.headings else "Agent Forge"
    asset = relative("assets", doc.target)
    raw = relative("docs/raw/" + doc.source, doc.target)
    peer = relative(doc.peer, doc.target)
    return f'''<!doctype html>
<html lang="{doc.locale}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} · Agent Forge</title>
<link rel="alternate" hreflang="{doc.locale}" href="{escape(PUBLISHED + doc.target)}">
<link rel="alternate" hreflang="{'zh-CN' if english else 'en'}" href="{escape(PUBLISHED + doc.peer)}">
<script src="{asset}/preferences.js"></script>
<link rel="stylesheet" href="{asset}/styles.css">
<link rel="stylesheet" href="{asset}/docs.css">
<script src="{asset}/docs.js" defer></script>
</head>
<body data-page="docs" data-doc-locale="{doc.locale}" data-doc-peer="{peer}" data-doc-anchors="{anchor_json}">
<a class="skip-link" href="#doc-content" data-i18n="docs.skip">{words['skip']}</a>
<header class="topbar"><a class="brand" href="{relative('index.html', doc.target)}">Agent Forge</a>
<nav class="topnav" aria-label="{'Main navigation' if english else '主导航'}">
{link('dashboard/index.html', 'Dashboard')}
<a href="{relative('docs/index' + suffix + '.html', doc.target)}" aria-current="page" data-i18n="docs.docs">{words['docs']}</a>
<a href="{REPOSITORY}/issues/new/choose" rel="noreferrer" data-i18n="docs.submit">{words['submit']}</a>
</nav></header>
<div class="docs-shell">
<aside class="docs-sidebar"><nav aria-label="{'Documentation' if english else '文档导航'}">{groups}</nav>
<div class="doc-language"><a href="{relative(doc.target if not english else doc.peer, doc.target)}" lang="zh-CN" hreflang="zh-CN" data-doc-language="zh-CN">中文</a>
<a href="{relative(doc.target if english else doc.peer, doc.target)}" lang="en" hreflang="en" data-doc-language="en">English</a></div></aside>
<main id="doc-content" class="doc-content" tabindex="-1">
<div class="doc-source"><a href="{raw}" data-i18n="docs.raw">{words['raw']}</a><a href="{REPOSITORY}/blob/main/{quote(doc.source, safe='/')}" rel="noreferrer" data-i18n="docs.repo">{words['repo']}</a></div>
<article>{content}</article>
</main>
<aside class="docs-toc"><nav aria-label="{words['toc']}"><h2 data-i18n="docs.toc">{words['toc']}</h2><ol>{toc}</ol></nav></aside>
</div>
</body></html>
'''


def overlaps(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def is_reparse(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def source_files(directory: Path) -> list[Path]:
    """Reject aliases using scandir's cached metadata, not per-file resolution."""
    if directory.resolve() != directory.absolute():
        raise ValueError(f"Symlink/junction in source tree: {directory}")
    result, pending = [], [directory]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                metadata = entry.stat(follow_symlinks=False)
                if is_reparse(metadata):
                    raise ValueError(f"Symlink/junction in source tree: {entry.path}")
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(Path(entry.path))
                elif stat.S_ISREG(metadata.st_mode):
                    result.append(Path(entry.path))
                else:
                    raise ValueError(f"Non-regular source file: {entry.path}")
    return sorted(result)


def build_site(root: Path = DEFAULT_ROOT, output: Path = Path("pages-staging"), data: Path = Path("data")) -> Path:
    root = Path(root).resolve(strict=True)
    output_arg = Path(output)
    output = output_arg if output_arg.is_absolute() else root / output_arg
    # Reject output aliases as well as destination aliases, before any writes.
    if output.is_symlink() or output.absolute() != output.resolve():
        raise ValueError(f"Output must not contain symlinks/junctions: {output}")
    output = output.resolve()
    data = Path(data) if Path(data).is_absolute() else root / data
    data = data.resolve()
    if output == root or output in root.parents:
        raise ValueError("Output must not replace the repository or an ancestor")
    protected = [root / name for name in ("site", "docs", "tools", "tests", "sources", "snapshots", ".git", ".github", ".venv", "examples")]
    protected += [data] + [root / (name + ".schema.json") for name in SCHEMAS]
    protected += [root / "README.md", root / "README_en.md"]
    if any(overlaps(output, path.resolve()) for path in protected):
        raise ValueError("Output overlaps a protected source/data/canonical path")
    copies: dict[str, Path] = {}
    for required in ("index.html", "assets/styles.css", "assets/preferences.js", "assets/docs.css", "assets/docs.js"):
        if not (root / "site" / required).is_file():
            raise ValueError(f"Missing site asset: {required}")
    for path in source_files(root / "site"):
        copies[path.relative_to(root / "site").as_posix()] = path
    for name in SCHEMAS:
        path = root / (name + ".schema.json")
        if not path.is_file() or path.resolve() != path.absolute():
            raise ValueError(f"Missing or aliased schema: {path}")
        copies[path.name] = path
    data_files: set[str] = set()
    if data.exists():
        if not data.is_dir():
            raise ValueError("Data must be a directory when present")
        for path in source_files(data):
            key = "data/" + path.relative_to(data).as_posix()
            copies[key] = path
            data_files.add(key)
    generated: dict[str, bytes] = {}
    for doc in DOCUMENTS:
        source = root / doc.source
        if not source.is_file() or source.resolve() != source.absolute():
            raise ValueError(f"Missing or aliased document: {source}")
    for doc in DOCUMENTS:
        source = root / doc.source
        generated[doc.target] = render_document(root, doc, data_files).encode("utf-8")
        copies["docs/raw/" + doc.source] = source
    collision = copies.keys() & generated.keys()
    if collision:
        raise ValueError(f"Static/generated path collision: {sorted(collision)}")
    # Validate existing destinations without resolving 100k individual data files.
    # The output root is already resolved; each distinct ancestor is checked once.
    existing_output = output.exists()
    checked_parents = set()
    for key in list(copies) + list(generated):
        target = output / PurePosixPath(key)
        if ".." in PurePosixPath(key).parts or not target.is_relative_to(output):
            raise ValueError(f"Unsafe output destination: {target}")
        if not existing_output:
            continue
        for parent in target.parents:
            if parent == output.parent or parent in checked_parents:
                break
            if parent.resolve() != parent.absolute():
                raise ValueError(f"Unsafe output destination: {parent}")
            if parent.exists() and not parent.is_dir():
                raise ValueError(f"Output parent is not a directory: {parent}")
            checked_parents.add(parent)
        try:
            metadata = target.lstat()
        except FileNotFoundError:
            continue
        if is_reparse(metadata):
            raise ValueError(f"Unsafe output destination: {target}")
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"Output destination is not a file: {target}")
    output.mkdir(parents=True, exist_ok=True)
    created_parents = {output}
    for key in sorted(set(copies) | set(generated)):
        target = output / PurePosixPath(key)
        if target.parent not in created_parents:
            target.parent.mkdir(parents=True, exist_ok=True)
            created_parents.add(target.parent)
        fd, temporary = tempfile.mkstemp(prefix=".forge-build-", dir=target.parent)
        os.close(fd)
        try:
            if key in copies:
                shutil.copyfile(copies[key], temporary)
            else:
                Path(temporary).write_bytes(generated[key])
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="Repository root (default: script's repository)")
    parser.add_argument("--output", type=Path, default=Path("pages-staging"), help="Dedicated additive staging directory")
    parser.add_argument("--data", type=Path, default=Path("data"), help="Optional generated data directory")
    args = parser.parse_args(argv)
    try:
        output = build_site(args.root, args.output, args.data)
    except (OSError, ValueError) as error:
        parser.exit(2, f"build_site: {error}\n")
    print(f"Built static site and {len(DOCUMENTS)} documents: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
