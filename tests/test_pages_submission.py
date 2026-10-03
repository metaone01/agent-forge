import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tools.project import build_projection
from tools.validate import ROOT, build_validator


class PagesSubmissionTests(unittest.TestCase):
    def test_frontend_constraints_match_canonical_fixtures(self):
        if not shutil.which("node"):
            self.skipTest("Node is required for browser-validator parity")
        examples = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((ROOT / "examples").glob("package-*.json"))]
        cases = list(examples)
        mutations = [
            lambda r: r.pop("description"),
            lambda r: r.update(keywords=["duplicate", "duplicate"]),
            lambda r: r.update(license=[]),
            lambda r: r.update(id="bad\\identity"),
            lambda r: r.update(id="with space"),
            lambda r: r.update(publishedAt="2026-02-30T00:00:00Z"),
            lambda r: r.update(publishedAt="2026-10-03T00:00:00+00:99"),
            lambda r: r.update(links={"homepage": "relative-path"}),
            lambda r: r.update(extra="not permitted"),
            lambda r: r.update(targets=[]),
            lambda r: r.update(targets=[{"agentId": "dsh", "compatibilityStatus": "unknown", "agentVersionRange": "*"}]),
            lambda r: r.update(targets=[{"agentId": "dsh", "compatibilityStatus": "known"}]),
            lambda r: r.update(_meta={"org.example/data": {"arbitrary": [1, None, True]}}),
            lambda r: r.update(customFacets={"other": ["中文"]}),
        ]
        for original in examples:
            for mutate in mutations:
                value = copy.deepcopy(original)
                mutate(value)
                cases.append(value)
        script = "const c=require('./site/assets/submission-core.js'),s=require('./package.schema.json'); let b='';process.stdin.on('data',d=>b+=d);process.stdin.on('end',()=>console.log(JSON.stringify(JSON.parse(b).map(v=>c.validate(v,s).length===0))));"
        process = subprocess.run(["node", "-e", script], input=json.dumps(cases), text=True, capture_output=True, cwd=ROOT, check=True)
        actual = json.loads(process.stdout)
        validator = build_validator("package.schema.json", submission_formats=True)
        self.assertEqual([validator.is_valid(value) for value in cases], actual)

    def test_submission_format_gate_is_explicit_and_rejects_bad_new_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "submission.json"
            record = json.loads((ROOT / "examples/package-mcp.json").read_text(encoding="utf-8"))
            for timestamp, url, expected in [
                ("2026-10-03T00:00:00Z", "https://example.test/tool", 0),
                ("2026-02-30T00:00:00Z", "https://example.test/tool", 1),
                ("2026-10-03T00:00:00+00:99", "https://example.test/tool", 1),
                ("2026-10-03T00:00:00Z", "relative-path", 1),
            ]:
                record["publishedAt"] = timestamp
                record["links"]["homepage"] = url
                path.write_text(json.dumps(record), encoding="utf-8")
                process = subprocess.run([sys.executable, str(ROOT / "tools/validate.py"), str(path), "--submission-formats"], capture_output=True, text=True)
                self.assertEqual(expected, process.returncode, process.stderr)
    def test_labels_identity_and_timestamp_are_in_latest_lightweight_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sources/mcp/packages"
            source.mkdir(parents=True)
            value = json.loads((ROOT / "examples/package-mcp.json").read_text(encoding="utf-8"))
            value["name"] = "中文工具"
            value["customFacets"] = {"other": ["社区"]}
            (source / "record.json").write_text(json.dumps(value), encoding="utf-8")
            now = datetime(2026, 10, 3, tzinfo=timezone.utc)
            output = root / "data"
            build_projection(root, output, "pages-test", now, now, "https://example.test/data", False)
            index = json.loads((output / "dsh/mcp/index.json").read_text(encoding="utf-8"))
            self.assertTrue(build_validator("index.schema.json").is_valid(index))
            entry = index["packages"][value["name"]]
            for key in ("id", "facets", "customFacets", "keywords"):
                self.assertEqual(value[key], entry[key])
            self.assertEqual(value["publishedAt"], entry["updatedAt"])

    def test_issue_notes_optional_and_machine_contract_matches_template(self):
        import yaml
        template = yaml.safe_load((ROOT / ".github/ISSUE_TEMPLATE/package-submission.yml").read_text(encoding="utf-8"))
        contract = json.loads((ROOT / "site/submit/contract.json").read_text(encoding="utf-8"))["issue"]
        fields = {item["id"]: item for item in template["body"] if "id" in item}
        self.assertFalse(fields[contract["notesField"]]["validations"]["required"])
        self.assertTrue(fields[contract["jsonField"]]["validations"]["required"])
        self.assertEqual(contract["jsonHeading"], fields[contract["jsonField"]]["attributes"]["label"])
        self.assertIn(contract["label"], template["labels"])
        self.assertTrue(build_validator("package.schema.json").is_valid(json.loads((ROOT / "site/submit/example.json").read_text(encoding="utf-8"))))


if __name__ == "__main__":
    unittest.main()
