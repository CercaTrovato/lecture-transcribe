import io
import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import httpx

from ltapp import server
from ltapp.config import PORT
from ltapp.engine import JobManager, ModelHolder
from ltapp.library import Library
from ltapp.models import ModelStore
from ltapp.mt import MTEngine


class FileRecorder:
    state, rid, elapsed, peak = "idle", None, 0.5, 0
    def status(self):
        return {"state": self.state, "rid": self.rid, "elapsed": self.elapsed, "peak": 0}
    def start(self, rid, path, device=None, on_audio=None):
        self.state, self.rid = "recording", rid
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
            audio.writeframes(b"\0\0" * 8000)
    def stop(self):
        self.state, self.rid = "idle", None


class APITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("LT_TEST_TEMP"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ModelStore(self.root / "models", metadata_provider=lambda _: self.fail("unexpected model download"))
        self.lib = Library(self.root / "library")
        self.holder = ModelHolder(self.store)
        with patch.object(JobManager, "_loop"):
            self.jobs = JobManager(self.holder, self.lib, MTEngine(self.store))
        for name, value in (("models", self.store), ("holder", self.holder), ("jobs", self.jobs),
                            ("lib", self.lib), ("LIB_DIR", self.lib.root), ("mt", MTEngine(self.store)),
                            ("recorder", FileRecorder()), ("live", None), ("recording_models", []),
                            ("SETTINGS_PATH", self.root / "settings.json")):
            replacement = patch.object(server, name, value)
            replacement.start(); self.addCleanup(replacement.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url=f"http://127.0.0.1:{PORT}")
        self.addAsyncCleanup(self.client.aclose)

    def external_asr(self):
        directory = self.root / "external"
        directory.mkdir()
        file = directory / "model.bin"
        file.write_bytes(b"external")
        return directory if self.store.spec("asr-turbo")["backend"] == "ctranslate2" else file

    async def test_startup_status_and_history_without_models(self):
        response = await self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["model"]["installed"])
        self.assertIsNone(self.holder.model)
        self.assertEqual((await self.client.get("/api/recordings")).json(), [])
        models = (await self.client.get("/api/models")).json()["models"]
        self.assertEqual(len(models), 4)
        self.assertTrue(all(m["impact"] and m["purpose"] and not m["installed"] for m in models))

    async def test_no_model_recording_stops_as_saved_audio_without_job(self):
        start = await self.client.post("/api/recorder/start", json={"name": "no-model", "live": False})
        self.assertEqual(start.status_code, 200, start.text)
        rid = start.json()["id"]
        stop = await self.client.post("/api/recorder/stop")
        self.assertEqual(stop.status_code, 200, stop.text)
        self.assertIsNone(stop.json()["job"])
        self.assertEqual(self.lib.get(rid)["status"], "recorded")
        self.assertTrue(self.lib.audio(rid).is_file())
        self.assertIsNone(self.holder.model)

    async def test_requiring_live_model_does_not_start_or_download_it(self):
        response = await self.client.post("/api/recorder/start", json={"live": True})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["action"], "manage_models")
        self.assertEqual(self.lib.list(), [])
        self.assertEqual(server.recording_models, [])

    async def test_import_without_model_is_kept_and_not_queued(self):
        audio = io.BytesIO()
        with wave.open(audio, "wb") as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000); out.writeframes(b"\0\0" * 16000)
        response = await self.client.post("/api/import", files={"file": ("sample.wav", audio.getvalue(), "audio/wav")})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["job"])
        self.assertTrue(self.lib.audio(response.json()["recording"]["id"]).exists())

    async def test_external_reference_removed_without_file_deletion(self):
        external = self.external_asr()
        response = await self.client.post("/api/models/asr-turbo/reference", json={"path": str(external)})
        self.assertEqual(response.status_code, 200)
        response = await self.client.delete("/api/models/asr-turbo")
        self.assertEqual(response.json()["action"], "reference_removed")
        self.assertTrue(external.exists())

    async def test_queued_model_use_blocks_uninstall_until_cancel(self):
        external = self.external_asr()
        self.store.reference("asr-turbo", external)
        record = self.lib.create("queued", "import")
        job = self.jobs.submit(record["id"])
        response = await self.client.delete("/api/models/asr-turbo")
        self.assertEqual(response.status_code, 409)
        self.jobs.control(job["id"], "cancel")
        self.assertEqual((await self.client.delete("/api/models/asr-turbo")).status_code, 200)

    async def test_storage_change_is_persisted_without_moving_history(self):
        record = self.lib.create("kept-history", "import")
        path = self.root / "user-selected-model-location"
        response = await self.client.post("/api/models/storage", json={"path": str(path)})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue((self.root / "settings.json").exists())
        self.assertIsNotNone(self.lib.get(record["id"]))
        self.assertEqual(self.store.root, path)

    async def test_foreign_origin_cannot_trigger_model_download(self):
        response = await self.client.post("/api/models/asr-turbo/download", headers={"Origin": "https://another-site.invalid"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.store._tasks, {})


if __name__ == "__main__":
    unittest.main()
