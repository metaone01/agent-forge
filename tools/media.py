"""Normalize declared image references only; never fetch assets or execute packages."""
from __future__ import annotations

import copy
import posixpath
import re
from typing import Any
from urllib.parse import quote, unquote, urlsplit

MAX_PREVIEWS = 12
PROVENANCE_KEY = "org.agentforge/media-provenance"
HTTPS_PATTERN = r'^https://[^\s/@?#\\\x00-\x1f\x7f]+(?:[/?#][^\s<>"{}|\\^`\x00-\x1f\x7f]*)?$'
MEDIA_FIELDS = ("media", "icon", "iconUrl", "preview", "previews", "screenshots")


def safe_image_url(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 4096 or not re.fullmatch(HTTPS_PATTERN, value) or re.search(r"%(?![0-9A-Fa-f]{2})", value):
        return False
    try:
        parsed = urlsplit(value)
        return bool(parsed.hostname and not parsed.username and not parsed.password and
                    (parsed.port is None or 1 <= parsed.port <= 65535))
    except ValueError:
        return False


def resolve_image_url(value: Any, context: dict[str, str] | None = None) -> str | None:
    if safe_image_url(value):
        return value
    if not isinstance(value, str) or not isinstance(context, dict) or len(value) > 4096:
        return None
    repo, revision, path = (context.get(key, "") for key in ("repository", "revision", "path"))
    if not all(isinstance(item, str) for item in (repo, revision, path)):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not re.fullmatch(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})", revision):
        return None
    if any(component in (".", "..") for component in repo.split("/")):
        return None
    if not path or path.startswith("/") or "\\" in path or ".." in path.split("/"):
        return None
    relative = unquote(value)
    try:
        relative_scheme = urlsplit(relative).scheme
    except ValueError:
        return None
    if (not relative or relative.startswith(("/", "//")) or relative_scheme or
            re.search(r'[\s<>"{}|\\^`?#\x00-\x1f\x7f]', relative)):
        return None
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), relative))
    if resolved in (".", "..") or resolved.startswith("../"):
        return None
    result = f"https://raw.githubusercontent.com/{repo}/{revision}/{quote(resolved, safe='/.-_~')}"
    return result if safe_image_url(result) else None


def normalize_media(metadata: dict[str, Any], context: dict[str, str] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Field precedence: canonical media, then explicit legacy fields in order."""
    media: dict[str, Any] = {}
    issues: list[dict[str, Any]] = []
    label = str(metadata.get("displayName") or metadata.get("name") or "Package")[:200]

    def asset(value: Any, field: str, preview: bool) -> dict[str, Any] | None:
        if not isinstance(value, (str, dict)):
            issues.append({"field": field, "reason": "image must be a URL or an image object"})
            return None
        supplied = value if isinstance(value, dict) else {"url": value}
        url = resolve_image_url(supplied.get("url"), context)
        if not url:
            issues.append({"field": field, "reason": "unsafe URL or relative URL without a pinned asset context", "value": supplied.get("url")})
            return None
        alt = supplied.get("alt", f"{label} {'preview' if preview else 'icon'}")
        if not isinstance(alt, str) or not alt.strip() or len(alt) > 500:
            issues.append({"field": field, "reason": "alt must be nonempty text of at most 500 characters"})
            return None
        result = {"url": url, "alt": alt}
        if "theme" in supplied:
            if not preview or supplied["theme"] not in ("light", "dark", "system"):
                issues.append({"field": field, "reason": "theme is only supported on previews: light, dark, system"})
                return None
            result["theme"] = supplied["theme"]
        return result

    canonical = metadata.get("media", {})
    if not isinstance(canonical, dict):
        issues.append({"field": "media", "reason": "media must be an object"})
        canonical = {}
    for field, value in [("media.icon", canonical.get("icon")), ("icon", metadata.get("icon")), ("iconUrl", metadata.get("iconUrl"))]:
        if value is None:
            continue
        image = asset(value, field, False)
        if image:
            if "icon" not in media:
                media["icon"] = image
            elif media["icon"] != image:
                issues.append({"field": field, "reason": "conflicting icon; earlier field retained", "value": image["url"]})
    previews: list[dict[str, Any]] = []
    for field, value in [("media.previews", canonical.get("previews")), ("preview", metadata.get("preview")), ("previews", metadata.get("previews")), ("screenshots", metadata.get("screenshots"))]:
        if value is None:
            continue
        values = value if isinstance(value, list) else [value]
        for index, candidate in enumerate(values):
            image = asset(candidate, f"{field}[{index}]", True)
            if not image:
                continue
            previous = next((item for item in previews if item["url"] == image["url"]), None)
            if previous:
                if previous != image:
                    issues.append({"field": field, "reason": "conflicting preview metadata; earlier field retained", "value": image["url"]})
            elif len(previews) < MAX_PREVIEWS:
                previews.append(image)
            else:
                issues.append({"field": field, "reason": "preview limit exceeded", "value": image["url"]})
    if previews:
        media["previews"] = previews
    return media, issues


def merge_media(record: dict[str, Any], media: dict[str, Any], source: dict[str, Any]) -> list[dict[str, Any]]:
    """Fill missing assets, append distinct previews, and retain existing claims."""
    if not media:
        return []
    provenance = record.get("_meta", {}).get(PROVENANCE_KEY)
    if provenance is not None and (not isinstance(provenance, dict) or not isinstance(provenance.get("sources"), list)):
        return [{"field": "_meta", "reason": "existing media provenance extension has an incompatible shape; record retained"}]
    current = record.setdefault("media", {})
    issues = []
    accepted = []
    if "icon" in media:
        if "icon" not in current:
            current["icon"] = copy.deepcopy(media["icon"])
        if current["icon"] == media["icon"]:
            accepted.append(("icon", media["icon"]["url"]))
        else:
            issues.append({"field": "icon", "reason": "existing icon retained", "value": media["icon"]["url"]})
    for image in media.get("previews", []):
        previews = current.setdefault("previews", [])
        previous = next((item for item in previews if item["url"] == image["url"]), None)
        if previous is None and len(previews) < MAX_PREVIEWS:
            previews.append(copy.deepcopy(image))
            previous = image
        if previous == image:
            accepted.append(("previews", image["url"]))
        else:
            issues.append({"field": "previews", "reason": "existing preview metadata retained or preview limit reached", "value": image["url"]})
    if accepted:
        observations = record.setdefault("_meta", {}).setdefault(PROVENANCE_KEY, {"sources": []})["sources"]
        for field, url in accepted:
            observation = {"field": field, "url": url, "source": copy.deepcopy(source)}
            if observation not in observations:
                observations.append(observation)
    return issues


def media_summary(record: dict[str, Any]) -> dict[str, Any]:
    media = record.get("media", {})
    summary = {}
    if media.get("icon"):
        summary["icon"] = copy.deepcopy(media["icon"])
    if media.get("previews"):
        summary["previews"] = copy.deepcopy(media["previews"][:1])
    return summary
