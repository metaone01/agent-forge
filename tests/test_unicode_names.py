import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote
from urllib.request import urlopen

from tools.identity import encoded_component
from tools.import_community import Importer, base_record, source_ref
from tools.project import build_projection
from tools.restore_package_names import restore
from tools.validate import build_validator


class UnicodeNamesTests(unittest.TestCase):
    def test_new_identity_preserves_unicode_and_literal_percent_unambiguously(self):
        identities = ["author/project/\u4e2d\u6587", "author/project/\u6c49\u8bed", "author/project/%E4%B8%AD"]
        records = [base_record("plugin", value, value, value, "1", "dsh") for value in identities]
        self.assertEqual(3, len({record["id"] for record in records}))
        for value, record in zip(identities, records):
            self.assertEqual(value, record["name"])
            self.assertEqual(value, unquote(record["id"].removeprefix("plugin.")))
            self.assertEqual(record, json.loads(json.dumps(record, ensure_ascii=True)))

    def test_unicode_identifiers_are_valid_and_controls_are_rejected(self):
        validator = build_validator("package.schema.json")
        identifier = validator.evolve(schema=validator.schema["$defs"]["identifier"])
        self.assertEqual([], list(identifier.iter_errors("plugin.\u4e2d\u6587")))
        self.assertEqual([], list(identifier.iter_errors("plugin.%E4%B8%AD%E6%96%87")))
        self.assertTrue(list(identifier.iter_errors("plugin.bad\nname")))

    def test_filesystem_escapes_roundtrip_and_survive_http_decoding(self):
        for value in ("author/\u4e2d\u6587", "author/~2F", "author/%E4%B8%AD", "spaces and \u4e2d\u6587"):
            encoded = encoded_component(value)
            self.assertEqual(value, unquote(encoded.replace("~", "%")))
            self.assertEqual(encoded, unquote(encoded))
            self.assertNotIn("/", encoded)

    def test_restoration_preserves_id_and_updates_index_without_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sources/plugin"
            (source / "packages").mkdir(parents=True)
            original = "author/project/\u4e2d\u6587"
            record = base_record("plugin", original, original, original, "1", "dsh")
            record.update(id="plugin.established-id", name="author/project")
            record["pluginDetails"] = {"manifestPath": "\u4e2d\u6587/package.json", "sourceType": "github-repo"}
            record["links"] = {"repository": "https://github.com/author/project"}
            record["distributions"] = [{"id": "git", "type": "github-repo", "url": "https://github.com/author/project/tree/HEAD/" + quote("\u4e2d\u6587")}]
            (source / "packages/legacy.json").write_text(json.dumps(record), encoding="utf-8")
            (source / "index.json").write_text(json.dumps({"packages": {record["name"]: {"id": record["id"], "latest": "1", "versions": ["1"], "path": "packages/legacy.json"}}}), encoding="utf-8")
            self.assertEqual(1, restore(root, True)["changedRecords"])
            restored = json.loads((source / "packages/legacy.json").read_text(encoding="utf-8"))
            self.assertEqual(original, restored["name"])
            self.assertEqual(record["id"], restored["id"])
            index = json.loads((source / "index.json").read_text(encoding="utf-8"))
            self.assertEqual([original], list(index["packages"]))
            self.assertEqual(0, restore(root, True)["changedRecords"])
            index["packages"][record["name"]] = index["packages"].pop(original)
            (source / "index.json").write_text(json.dumps(index), encoding="utf-8")
            self.assertEqual(1, restore(root, True)["changedRecords"])
            self.assertEqual([original], list(json.loads((source / "index.json").read_text(encoding="utf-8"))["packages"]))
            importer = Importer(root, sources=[])
            importer.load_existing()
            importer.ingest_plugin({"repo": "author/project"}, source_ref("example/catalog", "abc", "plugins.json"), "\u4e2d\u6587/package.json", "\u4e2d\u6587", version="2")
            self.assertEqual({record["id"]}, {value["id"] for value in importer.records.values()})
            now = datetime(2100, 1, 1, tzinfo=timezone.utc)
            output = root / "data"
            build_projection(root, output, "test", now, now, "https://example.test/data", False)
            entry = json.loads((output / "dsh/plugin/index.json").read_text(encoding="utf-8"))["packages"][original]
            physical = output / "dsh/plugin" / unquote(entry["path"])
            self.assertEqual(original, json.loads(physical.read_text(encoding="utf-8"))["name"])
            class QuietHandler(SimpleHTTPRequestHandler):
                def log_message(self, *args):
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(output)))
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/dsh/plugin/{entry['path']}"
                with urlopen(url, timeout=5) as response:
                    self.assertEqual(original, json.load(response)["name"])
            finally:
                server.shutdown()
                server.server_close()
                worker.join()
