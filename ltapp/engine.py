"""模型持有者 + 转录任务队列。模型只加载一次、常驻显存；所有推理串行经过同一把锁。"""

from __future__ import annotations

import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import transcribe  # noqa: E402  导入即注册 cuDNN DLL、设置 HF_HOME

from .library import Library  # noqa: E402
from .control import ACTIVE, JobCancelled, JobControl
from .models import ModelStore, ModelError


class ModelHolder:
    """Lazy local-only loader. Live and final inference share one model instance."""
    def __init__(self, store: ModelStore | None = None):
        self.store = store or ModelStore()
        self.model_name = "large-v3-turbo"
        self.live_model_name = self.model_name
        self.model_id = None
        self.model = self.live_model = None
        self.error = self.warning = ""
        self.device = "cpu"
        self.lock = threading.RLock()

    def ensure_main(self, model_id=None) -> bool:
        import os
        model_id = model_id or self.store.choose_asr()
        path = self.store.path(model_id) if model_id else None
        spec = self.store.spec(model_id) if model_id else None
        with self.lock:
            if not model_id:
                self.error = "尚未安装转录模型，可先录音保存或在模型管理中主动安装。"
                return False
            if self.model is not None and self.model_id == model_id:
                return True
            try:
                if path is None:
                    raise ModelError("转录模型未安装")
                self.release_main()
                requested = os.environ.get("LT_DEVICE", "auto")
                if spec["backend"] == "whisper.cpp":
                    from .metal import MetalModel
                    self.model = MetalModel(path)
                    self.device = self.model.device
                else:
                    import ctranslate2
                    cuda = False
                    if requested != "cpu":
                        try:
                            cuda = ctranslate2.get_cuda_device_count() > 0
                        except (RuntimeError, ValueError):
                            cuda = False
                    if requested == "cuda" and not cuda:
                        raise ModelError("当前设备的 CUDA 不可用，请选择 CPU 或检查已安装的 NVIDIA 运行组件。")
                    self.device = "cuda" if requested == "cuda" or (requested == "auto" and cuda) else "cpu"
                    try:
                        self.model = transcribe.load_model(str(path), device=self.device,
                                                          compute_type="float16" if self.device == "cuda" else "int8")
                    except RuntimeError as exc:
                        if requested != "auto" or self.device != "cuda":
                            raise
                        self.warning = f"CUDA 加载失败，已回退 CPU：{exc}"
                        self.device = "cpu"
                        self.model = transcribe.load_model(str(path), device="cpu", compute_type="int8")
                self.model_id = model_id
                self.model_name = "large-v3-turbo" if model_id == "asr-turbo" else "large-v3"
                self.live_model_name = self.model_name
                self.error = ""
                return True
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                return False

    def ensure_live(self, model_id=None):
        if self.ensure_main(model_id):
            self.live_model = self.model.model

    def release_live(self):
        self.live_model = None

    def release_main(self):
        with self.lock:
            self.model = self.live_model = None
            self.model_id = None
            import gc
            gc.collect()

    def unload(self, model_id):
        if self.model_id == model_id:
            self.release_main()

    @property
    def ready(self) -> bool:
        return self.model is not None

    def wait(self, timeout: float | None = None) -> bool:
        return self.ensure_main()


class JobManager:
    """后台任务队列（串行）：kind=transcribe 整文件转录；kind=translate 最终版翻译。每个任务：{id, kind, rid, status, progress, phase, eta, log, error}。"""

    def __init__(self, holder: ModelHolder, lib: Library, mt=None):
        self.holder, self.lib, self.mt = holder, lib, mt
        self.is_recording = lambda: False   # server 注入：录音中则不卸载实时模型 / 不开始翻译
        self.jobs: dict[str, dict] = {}
        self.controls: dict[str, JobControl] = {}
        self._q: list[str] = []
        self._cv = threading.Condition()
        threading.Thread(target=self._loop, daemon=True).start()

    def submit(self, rid: str, hotwords: str | None = None, lang: str | None = None, translate: bool | None = None,
               model_id: str | None = None) -> dict:
        meta = self.lib.get(rid)
        if meta is None:
            raise KeyError(rid)
        # 同一录音已有排队/进行中的任务就直接返回它
        for j in self.jobs.values():
            if j["rid"] == rid and j["status"] in ACTIVE:
                return j
        model_id = model_id or self.holder.store.choose_asr()
        if model_id not in ("asr-turbo", "asr-large"):
            raise ModelError("尚未安装转录模型，请先主动安装 Turbo 或 large-v3。")
        self.holder.store.acquire(model_id)
        final_translation = meta.get("translate", False) if translate is None else translate
        reserve_mt = final_translation and (lang or meta.get("lang")) != "zh"
        if reserve_mt:
            try:
                self.holder.store.acquire("mt-final")
            except Exception:
                self.holder.store.release(model_id)
                raise
        job = self._new("transcribe", rid, model_id=model_id, hotwords=hotwords if hotwords is not None else meta.get("hotwords", ""),
                        lang=lang or meta.get("lang", "auto"),
                        translate=final_translation, mt_reserved=reserve_mt)
        if job["lang"] == "zh":
            job["translate"] = False
        self.lib.update(rid, status="queued", hotwords=job["hotwords"], lang=job["lang"], translate=job["translate"], error="")
        self._enqueue(job)
        return job

    def submit_translate(self, rid: str, target: str = "zh") -> dict:
        if self.lib.get(rid) is None:
            raise KeyError(rid)
        for j in self.jobs.values():
            if j["rid"] == rid and j["status"] in ACTIVE:
                return j
        self.holder.store.acquire("mt-final")
        job = self._new("translate", rid, model_id="mt-final", target=target)
        self.lib.update(rid, translation_status="queued")
        self._enqueue(job)
        return job

    def _new(self, kind: str, rid: str, **extra) -> dict:
        job = {"id": uuid.uuid4().hex[:8], "kind": kind, "rid": rid, "status": "queued", "progress": 0.0, "phase": "排队",
               "eta": None, "log": [], "error": "", "created": time.time(), "started": 0.0, **extra}
        self.jobs[job["id"]] = job
        self.controls[job["id"]] = JobControl()
        return job

    def _state(self, job, status):
        job["status"] = status
        if job["kind"] == "translate":
            self.lib.update(job["rid"], translation_status=status)
        else:
            self.lib.update(job["rid"], status="transcribing" if status == "running" else status)

    def control(self, jid: str, action: str) -> dict:
        job = self.jobs[jid]
        ctl = self.controls[jid]
        with ctl.cv:
            if job["status"] not in ACTIVE:
                raise ValueError("任务已结束")
            if action == "pause":
                ctl.paused = True
                self._state(job, "paused" if not job["started"] or job["status"] == "paused" else "pausing")
            elif action == "resume":
                ctl.paused = False
                if not job["started"]:
                    self._state(job, "queued")
                elif job["status"] == "pausing":
                    self._state(job, "running")
            elif action == "cancel":
                ctl.cancelled = True
                ctl.paused = False
                job["cancel_requested"] = True
                if not job["started"]:
                    self._state(job, "cancelled")
                    self.holder.store.release(job["model_id"])
                    if job.get("mt_reserved"):
                        self.holder.store.release("mt-final")
            else:
                raise ValueError("未知操作")
            ctl.cv.notify_all()
        with self._cv:
            self._cv.notify_all()
        return job

    def _checkpoint(self, job):
        self.controls[job["id"]].checkpoint(
            lambda: self._state(job, "paused"), lambda: self._state(job, "running"))

    def _enqueue(self, job: dict) -> None:
        with self._cv:
            self._q.append(job["id"])
            self._cv.notify()

    def for_recording(self, rid: str, kind: str = "transcribe") -> dict | None:
        js = [j for j in list(self.jobs.values()) if j["rid"] == rid and j["kind"] == kind]
        return self.snapshot(max(js, key=lambda j: j["created"])) if js else None

    def snapshot(self, job):
        result = dict(job)
        if job.get("active_started") is not None:
            result["active_elapsed"] = max(0, round(self.controls[job["id"]].clock() - job["active_started"]))
        return result

    def _loop(self):
        while True:
            with self._cv:
                while True:
                    self._q = [jid for jid in self._q if self.jobs[jid]["status"] in ACTIVE]
                    jid = next((jid for jid in self._q if not self.controls[jid].paused), None)
                    if jid is not None:
                        self._q.remove(jid)
                        break
                    self._cv.wait()
            job = self.jobs[jid]
            if job["kind"] == "translate":
                self._run_translate(job)
            else:
                self._run(job)

    def _run(self, job: dict):
        rid = job["rid"]
        job["status"] = "running"
        job["started"] = time.time()
        job["active_started"] = self.controls[job["id"]].clock()
        job["phase"] = "准备音频与模型"
        self.lib.update(rid, status="transcribing")
        try:
            self._checkpoint(job)
            # 录音进行中不跑整文件转录：large-v3 + 实时 turbo 挤在 8 GB 显存里会把实时转录拖慢好几倍
            waited = 0
            while self.is_recording():
                self._checkpoint(job)
                if waited == 0:
                    job["phase"] = "等待录音结束"
                    job["eta"] = None
                    self.lib.update(rid, status="queued")
                    print(f"[job {job['id']}] 录音进行中，转录推迟到录音结束")
                time.sleep(5)
                waited += 5
            if waited:
                self.lib.update(rid, status="transcribing")
            if self.mt is not None and self.mt.mode == "final":
                self.mt.stop()            # 上一个翻译任务留下的 7B 先卸掉，主模型才装得下
            if not self.holder.ensure_main(job["model_id"]):
                raise RuntimeError(self.holder.error or "主模型重载失败")
            d = self.lib.dir(rid)
            meta = self.lib.get(rid)
            if not self.is_recording():
                self.holder.release_live()

            def progress(frac, phase, eta):
                self._checkpoint(job)
                job["progress"] = round(frac, 3)
                job["phase"] = phase
                job["eta"] = None if eta is None else round(eta)
                job["updated"] = time.time()

            def log(msg):
                job["log"].append(msg)
                print(f"[job {job['id']}] {msg}")

            r = transcribe.transcribe_file(
                self.lib.audio(rid), self.holder.model, out_dir=d, stem="transcript", title=meta["name"],
                lang=job["lang"], hotwords=job["hotwords"] or None,
                normalize=False,  # 入库时已归一化
                model_name=self.holder.model_name, progress=progress, log=log,
                checkpoint=lambda: self._checkpoint(job), clock=self.controls[job["id"]].clock,
            )
            self.lib.save_segments(rid, r["segments"])
            self.lib.update(rid, status="done", duration=round(r["duration"], 2), error="", dropped=len(r["dropped"]),
                            transcribed=time.strftime("%Y-%m-%dT%H:%M:%S"), elapsed=round(r["elapsed"], 1),
                            detected_lang=r["language"])
            job["progress"] = 1.0
            job["status"] = "done"
            try:
                self.lib.compress_audio(rid)  # 转录完成后压缩音频，失败不影响结果
            except Exception as e:  # noqa: BLE001
                print(f"[job {job['id']}] 音频压缩失败（保留 wav）: {e}")
            if job.get("translate") and r["language"] != "zh" and self.mt is not None and self.mt.available("final"):
                self.submit_translate(rid)    # 转录完自动翻译（记录级开关 translate）
        except JobCancelled:
            self._state(job, "cancelled")
            job["eta"] = None
        except Exception as e:  # noqa: BLE001
            job["status"] = "error"
            job["error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
            self.lib.update(rid, status="error", error=job["error"])
        finally:
            self.holder.store.release(job["model_id"])
            if job.get("mt_reserved"):
                self.holder.store.release("mt-final")

    # ---------------------------------------------------------------- 最终版翻译
    def _run_translate(self, job: dict):
        from . import mt as mtmod
        from .paras import para_text, split_paragraphs

        rid = job["rid"]
        job["status"] = "running"
        job["started"] = time.time()
        job["active_started"] = self.controls[job["id"]].clock()

        def log(msg):
            job["log"].append(msg)
            print(f"[translate {job['id']}] {msg}")

        try:
            self._checkpoint(job)
            # 录音进行中不做最终版翻译（7B 与 turbo + 实时翻译挤不下），等录音结束
            waited = 0
            while self.is_recording():
                self._checkpoint(job)
                if waited == 0:
                    job["phase"] = "等待录音结束"
                    log("录音进行中，翻译推迟到录音结束")
                time.sleep(5)
                waited += 5
            self.lib.update(rid, translation_status="running")
            meta = self.lib.get(rid)
            segments = self.lib.segments(rid)
            if not segments:
                raise RuntimeError("还没有最终转录，无法翻译")
            target = job.get("target", "zh")
            gl = mtmod.load_glossary(meta.get("course"))
            paras = split_paragraphs(segments)
            log(f"{len(paras)} 段，术语表 {len(gl)} 条，模型 Hy-MT2-7B")

            job["phase"] = "加载翻译模型"
            self.holder.release_live()
            self.holder.release_main()          # 7B 需要 5.1 GB，先卸 Whisper
            if not self.mt.ensure("final"):
                raise RuntimeError(self.mt.error)

            job["phase"] = "翻译"
            clock = self.controls[job["id"]].clock
            out, t0 = [], clock()
            for k, p in enumerate(paras):
                self._checkpoint(job)
                src = para_text(segments, p)
                bg = " ".join(segments[i]["text"] for i in paras[k - 1]["idx"][-3:]) if k else ""
                terms = mtmod.glossary_hits(src, gl, 20)
                zh = ""
                for attempt in range(2):
                    self._checkpoint(job)
                    try:
                        zh = self.mt.translate(src, terms, bg, target, max_tokens=max(256, len(src.split()) * 4))
                        break
                    except Exception as e:  # noqa: BLE001
                        log(f"    第 {k + 1} 段第 {attempt + 1} 次失败: {e}")
                out.append({"start": p["start"], "end": p["end"], "idx": p["idx"], "en": src, "zh": zh, "terms": terms})
                done = k + 1
                job["progress"] = round(done / len(paras), 3)
                el = clock() - t0
                job["eta"] = round(el / done * (len(paras) - done))
                job["phase"] = f"翻译 {done}/{len(paras)} 段"
                job["updated"] = time.time()
            self._checkpoint(job)
            self.lib.save_translation(rid, {"model": "Hy-MT2-7B-Q4_K_M", "target": target, "course": meta.get("course", ""),
                                            "glossary_terms": len(gl), "created": time.strftime("%Y-%m-%dT%H:%M:%S"), "paras": out})
            self.lib.write_bilingual_md(rid)
            failed = sum(1 for p in out if not p["zh"])
            self.lib.update(rid, translation_status="done" if not failed else "partial",
                            translation_elapsed=round(clock() - t0, 1), translation_model="Hy-MT2-7B")
            log(f"完成：{len(out)} 段（失败 {failed}），用时 {clock() - t0:.0f}s")
            job["progress"] = 1.0
            job["status"] = "done"
        except JobCancelled:
            self._state(job, "cancelled")
            job["eta"] = None
        except Exception as e:  # noqa: BLE001
            job["status"] = "error"
            job["error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
            self.lib.update(rid, translation_status="error", translation_error=job["error"])
        finally:
            # 翻译完卸掉 7B、把主模型装回来（下次转录 / 实时回退都需要它）
            if self.mt is not None and self.mt.mode == "final":
                self.mt.stop()
            self.holder.store.release(job["model_id"])
