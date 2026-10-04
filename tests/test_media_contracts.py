import json
import unittest
from pathlib import Path

from tests.test_contracts import valid_package
from tools.media import HTTPS_PATTERN, safe_image_url
from tools.validate import build_validator

ROOT = Path(__file__).resolve().parents[1]


def sample():
    return valid_package("plugin")


def image(name="preview", **extra):
    return {"url": f"https://images.example/{name}.png", "alt": name, **extra}


class MediaContractTests(unittest.TestCase):
    def test_optional_media_and_all_types_remain_valid(self):
        validator = build_validator("package.schema.json", submission_formats=True)
        for kind in ("mcp", "plugin", "skill", "general", "bundle"):
            value = valid_package(kind)
            self.assertTrue(validator.is_valid(value))
            value["media"] = {"icon": image("icon"), "previews": [image(theme="dark")]}
            self.assertEqual([], list(validator.iter_errors(value)), kind)

    def test_strict_image_shape_and_urls(self):
        validator = build_validator("package.schema.json", submission_formats=True)
        for media in ({}, {"previews": []}, {"icon": {"url": image()["url"]}}, {"icon": image(theme="dark")},
                      {"previews": [image(theme="invalid")]}, {"icon": image(alt=" ")}, {"html": "<img>"},
                      {"previews": [image(str(i)) for i in range(13)]}):
            with self.subTest(media=media):
                value = sample(); value["media"] = media
                self.assertFalse(validator.is_valid(value))
        for url in ("http://images.example/a.png", "javascript:alert(1)", "data:image/png;base64,x", "file:///a.png", "//images.example/a.png", "https://user:password@images.example/a.png", 'https://images.example/" onerror="alert(1)', "https://images.example/\\a.png"):
            value = sample(); value["media"] = {"icon": {"url": url, "alt": "icon"}}
            self.assertFalse(validator.is_valid(value), url)
            self.assertFalse(safe_image_url(url), url)

    def test_index_summary_cannot_include_a_gallery(self):
        validator = build_validator("index.schema.json")
        value = json.loads((ROOT / "examples/index.json").read_text(encoding="utf-8")) if (ROOT / "examples/index.json").exists() else {
            "schemaVersion": 2, "sourceId": "test", "sourceManifest": "source.json", "agentId": "dsh", "type": "plugin", "revision": "test", "generatedAt": "2026-10-03T00:00:00Z", "packages": {"skin": {"latest": "1", "versions": ["1"], "path": "packages/skin.json"}}}
        entry = next(iter(value["packages"].values()))
        entry["media"] = {"icon": image("icon"), "previews": [image("one")]}
        self.assertEqual([], list(validator.iter_errors(value)))
        entry["media"]["previews"].append(image("two"))
        self.assertFalse(validator.is_valid(value))

    def test_import_and_schema_share_the_https_policy(self):
        schema = json.loads((ROOT / "package.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["$defs"]["imageUrl"]["pattern"], HTTPS_PATTERN)
        value = sample(); value["media"] = {"icon": image(url="https://images.example/a\x00b.png")}
        self.assertFalse(build_validator("package.schema.json").is_valid(value))
        self.assertFalse(safe_image_url(value["media"]["icon"]["url"]))
        self.assertFalse(safe_image_url("https://images.example/bad%ZZ.png"))

    def test_closed_old_schema_rejects_new_field(self):
        from jsonschema import Draft202012Validator
        schema = json.loads((ROOT / "package.schema.json").read_text(encoding="utf-8"))
        schema["properties"].pop("media")
        value = sample(); value["media"] = {"icon": image()}
        self.assertFalse(Draft202012Validator(schema).is_valid(value))



if __name__ == "__main__":
    unittest.main()
