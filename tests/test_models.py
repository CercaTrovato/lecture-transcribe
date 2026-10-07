"""Behaviour tests with tiny verified files and a real local HTTP server."""
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ltapp.models import ModelStore, ModelError

TASK_TEMP = Path(os.environ.get("LT_TEST_TEMP", tempfile.gettempdir()))


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TASK_TEMP)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = b"verified model content" * 65536
        self.requests = []
        self.slow = False
        test = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                header = self.headers.get("Range")
                test.requests.append(header)
                start = int(header.split("=")[1].split("-")[0]) if header else 0
                self.send_response(206 if header else 200)
                self.send_header("Content-Length", str(len(test.payload) - start))
                if header:
                    self.send_header("Content-Range", f"bytes {start}-{len(test.payload)-1}/{len(test.payload)}")
                self.end_headers()
                try:
                    for offset in range(start, len(test.payload), 16384):
                        self.wfile.write(test.payload[offset:offset + 16384])
                        self.wfile.flush()
                        if test.slow:
                            time.sleep(0.005)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.manifest = {"revision": "test-pinned", "files": [{"name": "Hy-MT2-1.8B-Q4_K_M.gguf", "size": len(self.payload),
                         "digest": hashlib.sha256(self.payload).hexdigest(), "algorithm": "sha256",
                         "url": f"http://127.0.0.1:{self.http.server_port}/model.bin"}]}
        self.store = ModelStore(self.root / "models", metadata_provider=lambda _: self.manifest)

    def wait_download(self, expected):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            result = next(x for x in self.store.list() if x["id"] == "mt-live")
            if result["download"].get("state") == expected:
                return result
            time.sleep(0.01)
        self.fail(str(result))

    def test_empty_store_does_not_fetch_or_install(self):
        self.assertTrue(all(not m["installed"] for m in self.store.list()))
        self.assertIsNone(self.store.choose_asr())
        self.assertFalse(self.requests)
        self.assertFalse(self.store.root.exists())

    def test_explicit_install_is_verified_and_uninstall_preserves_history(self):
        history = self.root / "library" / "note.txt"
        history.parent.mkdir()
        history.write_text("preserved", encoding="utf-8")
        self.store.download("mt-live")
        result = self.wait_download("installed")
        self.assertTrue(result["installed"])
        self.assertEqual(self.store.path("mt-live").read_bytes(), self.payload)
        self.assertGreater(self.store.uninstall("mt-live")["freed_bytes"], 0)
        self.assertIsNone(self.store.path("mt-live"))
        self.assertEqual(history.read_text(encoding="utf-8"), "preserved")

    def test_cancel_and_retry_resume_actual_download(self):
        self.slow = True
        self.store.download("mt-live")
        until = time.monotonic() + 3
        while self.store._tasks["mt-live"]["bytes"] < 65536 and time.monotonic() < until:
            time.sleep(0.01)
        self.store.cancel("mt-live")
        self.wait_download("cancelled")
        self.store.download("mt-live")
        self.wait_download("installed")
        self.assertTrue(any(header and header != "bytes=0-" for header in self.requests))

    def test_corrupt_download_never_becomes_installed(self):
        self.manifest["files"][0]["digest"] = "0" * 64
        self.store.download("mt-live")
        result = self.wait_download("error")
        self.assertFalse(result["installed"])
        self.assertIn("校验失败", result["download"]["error"])

    def test_in_use_uninstall_and_reference_change_are_rejected(self):
        self.store.download("mt-live")
        self.wait_download("installed")
        with self.store.using("mt-live"):
            with self.assertRaises(ModelError):
                self.store.uninstall("mt-live")
        self.store.uninstall("mt-live")

    def test_external_reference_removal_never_deletes_original(self):
        external = self.root / "external"
        external.write_bytes(b"external content")
        self.store.reference("mt-live", external)
        with self.store.using("mt-live"):
            with self.assertRaises(ModelError):
                self.store.reference("mt-live", external)
        self.assertEqual(self.store.uninstall("mt-live")["action"], "reference_removed")
        self.assertEqual(external.read_bytes(), b"external content")

    def test_switching_storage_preserves_original_managed_files(self):
        self.store.download("mt-live")
        self.wait_download("installed")
        original = self.store.path("mt-live")
        with self.store.using("mt-live"):
            with self.assertRaises(ModelError):
                self.store.change_storage(self.root / "new-location")
        self.store.change_storage(self.root / "new-location")
        self.assertIsNone(self.store.path("mt-live"))
        self.assertEqual(original.read_bytes(), self.payload)

    def test_unknown_id_cannot_delete_another_directory(self):
        with self.assertRaises(ModelError):
            self.store.uninstall("../library")

    def test_missing_external_reference_does_not_trigger_implicit_replacement(self):
        external = self.root / "external-model"
        external.write_bytes(b"external")
        self.store.reference("mt-live", external)
        external.unlink()
        with self.assertRaises(ModelError):
            self.store.download("mt-live")
        self.assertEqual(self.store.uninstall("mt-live")["action"], "reference_removed")


if __name__ == "__main__":
    unittest.main()
