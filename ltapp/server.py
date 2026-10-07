"""FastAPI 服务：REST API + 静态前端。agent 用 lt.py 走同一套 API。"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import LIB_DIR, WEB_DIR, PORT, SETTINGS_PATH
from .engine import JobManager, ModelHolder
from .control import ACTIVE
from .library import Library, load_courses
from .live import LiveTranscriber
from . import procs
from .mt import MTEngine, load_glossary
from .recorder import Recorder, list_input_devices
from .models import ModelStore, ModelError, atomic_json

app = FastAPI(title="lecture-transcribe")
lib = Library()
models = ModelStore()
holder = ModelHolder(models)
mt = MTEngine(models)
jobs = JobManager(holder, lib, mt)
recorder = Recorder()
live: LiveTranscriber | None = None
finalizing_rid: str | None = None
recording_models: list[str] = []
_ctl = threading.Lock()
jobs.is_recording = lambda: recorder.state != "idle" or finalizing_rid is not None
models.on_unload = lambda model_id: (holder.unload(model_id), mt.stop() if
                                    (model_id == "mt-live" and mt.mode == "live") or
                                    (model_id == "mt-final" and mt.mode == "final") else None)


@app.exception_handler(ModelError)
async def model_error(_, exc):
    return JSONResponse(status_code=409, content={"detail": str(exc), "action": "manage_models"})


@app.get("/api/models")
def model_list():
    return {"models": models.list(), "asr_model": models.choose_asr(), "device": holder.device,
            "translation_runtime": mt.available("live") or mt.available("final"), "runtime_error": mt.missing("live"),
            "storage": str(models.root)}


@app.post("/api/models/{model_id}/download")
def model_download(model_id: str):
    return models.download(model_id)


@app.post("/api/models/{model_id}/cancel")
def model_cancel(model_id: str):
    models.cancel(model_id)
    return {"ok": True}


@app.delete("/api/models/{model_id}")
def model_uninstall(model_id: str):
    return models.uninstall(model_id)


@app.delete("/api/models/{model_id}/partial")
def model_discard_download(model_id: str):
    models.clear_partial(model_id)
    return {"ok": True}


class ModelReferenceReq(BaseModel):
    path: str


@app.post("/api/models/storage")
def model_storage(req: ModelReferenceReq):
    models.change_storage(req.path)
    atomic_json(SETTINGS_PATH, {"model_dir": str(models.root)})
    return {"storage": str(models.root)}


@app.post("/api/models/{model_id}/reference")
def model_reference(model_id: str, req: ModelReferenceReq):
    models.reference(model_id, req.path)
    return {"ok": True}


def recorder_status():
    result = recorder.status()
    if finalizing_rid is not None:
        result = {**result, "state": "finalizing", "rid": finalizing_rid}
    return result


@app.on_event("startup")
def _recover_stale():
    """服务被强杀会留下"导入中/转录中"的僵尸记录。放在 startup 事件里（不是模块导入时），
    这样 import ltapp.server 的测试不会动到真实历史库。"""
    for m in lib.recover():
        print(f"[recover] {m['id']} → {m['status']}：{m['error']}")


# ---------------------------------------------------------------- 状态
@app.get("/api/status")
def status():
    return {
        "model": {"name": holder.model_name, "ready": holder.ready, "error": holder.error,
                  "installed": models.choose_asr() is not None, "device": holder.device, "warning": holder.warning},
        "recorder": recorder_status(),
        "live": {"active": live is not None, "count": len(live.words) if live else 0, "busy": bool(live and live.busy),
                 "latency": live.last_latency if live else 0, "model": holder.live_model_name if holder.live_model else holder.model_name},
        "jobs": [j for j in list(jobs.jobs.values()) if j["status"] in ACTIVE],
        "mt": {"mode": mt.mode, "error": mt.error, "live_available": mt.available("live"), "final_available": mt.available("final"),
               "missing": {k: mt.missing(k) for k in ("live", "final") if not mt.available(k)}},
    }


@app.post("/api/shutdown")
def shutdown():
    """从页面关闭服务（桌面快捷方式启动的是无窗口后台进程，没地方点关闭）。录音或任务进行中拒绝。"""
    if recorder.state != "idle" or finalizing_rid is not None:
        raise HTTPException(409, "正在录音，先结束录音")
    running = [j for j in list(jobs.jobs.values()) if j["status"] in ACTIVE]
    if running:
        raise HTTPException(409, f"还有 {len(running)} 个任务在跑，完成后再退出")
    if procs.active():
        raise HTTPException(409, "正在导入音频，完成或取消后再退出")
    if any(m["download"].get("state") in ("downloading", "cancelling", "verifying") for m in models.list()):
        raise HTTPException(409, "模型正在下载，请先取消下载或等下载完成。")

    def bye():
        time.sleep(0.4)      # 让响应先发出去
        mt.stop()            # 顺带收掉 llama-server 子进程
        os._exit(0)
    threading.Thread(target=bye, daemon=True).start()
    return {"ok": True}


@app.get("/api/courses")
def courses():
    return load_courses()


@app.get("/api/devices")
def devices():
    return list_input_devices()


# ---------------------------------------------------------------- 录音
class StartReq(BaseModel):
    name: str = ""
    device: int | None = None
    hotwords: str = ""
    lang: Literal["auto", "zh", "en"] = "auto"
    live: bool = False
    course: str = ""
    live_translate: bool = False
    translate: bool = False
    model_id: Literal["asr-turbo", "asr-large"] | None = None


@app.post("/api/recorder/start")
def rec_start(req: StartReq):
    global live, recording_models
    with _ctl:
        if recorder.state != "idle":
            raise HTTPException(409, "已在录音")
        asr_id = req.model_id or models.choose_asr()
        wanted = ([asr_id or "asr-turbo"] if req.live else [])
        use_mt = req.live and req.live_translate and req.lang != "zh"
        if use_mt:
            wanted.append("mt-live")
        if req.translate and req.lang != "zh":
            wanted.append("mt-final")
        recording_models = []
        try:
            for model_id in wanted:
                models.acquire(model_id)
                recording_models.append(model_id)
            if req.live:
                holder.ensure_live(asr_id)
                if holder.live_model is None:
                    raise ModelError(holder.error)
            if use_mt and not mt.ensure("live"):
                raise ModelError(mt.error)
            meta = lib.create(req.name or "课堂录音", "recording", hotwords=req.hotwords, lang=req.lang, course=req.course,
                              translate=req.translate and req.lang != "zh", live=req.live)
            lib.update(meta["id"], asr_model_id=asr_id)
            live = LiveTranscriber(holder, req.lang, req.hotwords, mt=mt if use_mt else None,
                                   glossary=load_glossary(req.course) if use_mt else None) if req.live else None
            recorder.start(meta["id"], lib.audio(meta["id"]), req.device, on_audio=live.feed if live else None)
        except Exception as e:  # noqa: BLE001
            if live:
                live.stop()
                live = None
            for model_id in recording_models:
                models.release(model_id)
            recording_models = []
            if "meta" in locals():
                lib.delete(meta["id"])
            if isinstance(e, ModelError):
                raise
            raise HTTPException(500, f"无法打开麦克风: {e}")
        return meta


@app.post("/api/recorder/pause")
def rec_pause():
    recorder.pause()
    return recorder.status()


@app.post("/api/recorder/resume")
def rec_resume():
    recorder.resume()
    return recorder.status()


@app.post("/api/recorder/stop")
def rec_stop():
    """结束录音 → 归一化 → 排队整文件转录。"""
    global live, finalizing_rid, recording_models
    with _ctl:
        rid = recorder.rid
        if recorder.state == "idle" or not rid:
            raise HTTPException(409, "未在录音")
        finalizing_rid = rid
        try:
            lib.update(rid, status="finalizing")
            recorder.stop()
            if live:
                segs = live.stop()
                lib.save_segments(rid, segs, live=True)
                lib.write_live_bilingual(rid)
                live = None
            lib.finalize_recording(rid)
            meta = lib.get(rid)
            job = jobs.submit(rid, model_id=meta.get("asr_model_id")) if models.choose_asr() else None
            return {"recording": lib.get(rid), "job": job}
        except Exception as e:
            lib.update(rid, status="error", error=f"录音收尾失败: {e}")
            raise HTTPException(500, f"录音收尾失败，原音频已保留: {e}") from e
        finally:
            finalizing_rid = None
            for model_id in recording_models:
                models.release(model_id)
            recording_models = []


@app.get("/api/recorder/live")
def rec_live(since: int = 0, sent_since: int = 0):
    """已提交的词（增量）+ 未提交的灰字 + 已成句的翻译（sent_since 起的句子；前端把 sent_since 设为最早还没译文的那句）。"""
    session = live
    words = list(session.words) if session else []
    sents = list(session.sents) if session else []
    return {"words": words[since:], "total": len(words), "pending": session.pending if session else "",
            "sents": [{"k": k, **s} for k, s in enumerate(sents) if k >= sent_since][-200:],
            "sent_total": len(sents), "mt_error": session.mt_error if session else "", "recorder": recorder_status()}


# ---------------------------------------------------------------- 历史库
@app.get("/api/recordings")
def recordings():
    return lib.list()


@app.get("/api/recordings/{rid}")
def recording(rid: str):
    meta = lib.get(rid)
    if meta is None:
        raise HTTPException(404)
    segs = lib.segments(rid)
    provisional = False
    if not segs:
        segs = lib.segments(rid, live=True)
        provisional = bool(segs)
    return {**meta, "segments": segs, "live_segments": lib.segments(rid, live=True), "provisional": provisional, "dropped": lib.dropped(rid),
            "files": lib.transcript_paths(rid), "audio": f"/library/{rid}/{lib.audio(rid).name}",
            "job": jobs.for_recording(rid), "translate_job": jobs.for_recording(rid, "translate"),
            "translation": lib.translation(rid)}


@app.post("/api/recordings/{rid}/translate")
def recording_translate(rid: str):
    """（重新）做最终版翻译。"""
    meta = lib.get(rid)
    if meta is None:
        raise HTTPException(404)
    if not lib.segments(rid):
        raise HTTPException(400, "还没有最终转录")
    if not mt.available("final"):
        raise ModelError(mt.missing("final"))
    return jobs.submit_translate(rid)


class TranscribeReq(BaseModel):
    hotwords: str | None = None
    lang: Literal["auto", "zh", "en"] | None = None
    translate: bool | None = None
    model_id: Literal["asr-turbo", "asr-large"] | None = None


@app.post("/api/recordings/{rid}/transcribe")
def recording_transcribe(rid: str, req: TranscribeReq | None = None):
    if lib.get(rid) is None:
        raise HTTPException(404)
    if recorder.rid == rid or finalizing_rid == rid:
        raise HTTPException(409, "正在录音或收尾")
    lib.cancel_import(rid)   # 正在导入的先掐掉 ffmpeg，否则它会继续往已删掉的目录里写
    req = req or TranscribeReq()
    return jobs.submit(rid, req.hotwords, req.lang, req.translate, req.model_id)


class ExportReq(BaseModel):
    module: str
    overwrite: bool = False


@app.post("/api/recordings/{rid}/export")
def recording_export(rid: str, req: ExportReq):
    """复制 transcript.txt 到课程知识库的 transcripts/M0N-transcript.txt。"""
    if lib.get(rid) is None:
        raise HTTPException(404)
    try:
        return {"path": str(lib.export_to_vault(rid, req.module.strip(), req.overwrite))}
    except FileExistsError as e:
        raise HTTPException(409, str(e))
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))


class RenameReq(BaseModel):
    name: str


@app.post("/api/recordings/{rid}/rename")
def recording_rename(rid: str, req: RenameReq):
    return lib.update(rid, name=req.name.strip() or rid)


@app.post("/api/recordings/{rid}/cancel-import")
def recording_cancel_import(rid: str):
    """取消正在进行的导入（杀掉 ffmpeg，记录随之删除）。"""
    r = lib.cancel_import(rid)
    if r == "none":
        raise HTTPException(409, "这条记录没有在导入")
    return {"ok": True, "result": r}


@app.delete("/api/recordings/{rid}")
def recording_delete(rid: str):
    if recorder.rid == rid or finalizing_rid == rid:
        raise HTTPException(409, "正在录音或收尾")
    if any(j["rid"] == rid and j["status"] in ACTIVE for j in list(jobs.jobs.values())):
        raise HTTPException(409, "任务尚未结束，请先取消任务再删除录音")
    lib.delete(rid)
    return {"ok": True}


@app.get("/api/recordings/{rid}/file/{kind}")
def recording_file(rid: str, kind: str):
    if kind not in ("md", "srt", "txt", "zh_md", "live_md"):
        raise HTTPException(400)
    fname = "live-bilingual.md" if kind == "live_md" else "transcript.zh.md" if kind == "zh_md" else f"transcript.{kind}"
    p = lib.dir(rid) / fname
    if not p.exists():
        raise HTTPException(404)
    suffix = ".zh.md" if kind == "zh_md" else "." + kind
    name = f"{lib.get(rid)['name']}{suffix}"
    return FileResponse(p, filename=name, media_type="text/plain; charset=utf-8")


@app.post("/api/import")
def import_file(file: UploadFile = File(...), name: str = Form(""), hotwords: str = Form(""), lang: Literal["auto", "zh", "en"] = Form("auto"), course: str = Form(""), translate: bool = Form(False)):
    # 同步路由由 FastAPI 在线程池执行；文件复制与 ffmpeg 双遍归一化
    # 不能放在 async 路由里直接执行，否则整个服务在导入期间停止响应。
    suffix = Path(file.filename or "audio").suffix or ".bin"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=LIB_DIR) as tmp:
            tmp_path = Path(tmp.name)
            shutil.copyfileobj(file.file, tmp)
        meta = lib.import_audio(tmp_path, name or Path(file.filename or "").stem, hotwords=hotwords, lang=lang, course=course, translate=translate and lang != "zh")
    except subprocess.CalledProcessError:
        raise HTTPException(400, "无法处理音频，请确认文件未损坏、包含音轨，并检查磁盘剩余空间。")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"导入失败: {e}")
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    return {"recording": meta, "job": jobs.submit(meta["id"]) if models.choose_asr() else None}


class ImportPathReq(BaseModel):
    path: str
    name: str = ""
    hotwords: str = ""
    lang: Literal["auto", "zh", "en"] = "auto"
    course: str = ""
    translate: bool = False


@app.post("/api/import-path")
def import_path(req: ImportPathReq):
    """本机路径导入（agent / CLI 用，省去上传）。"""
    src = Path(req.path)
    if not src.is_file():
        raise HTTPException(404, f"找不到 {src}")
    meta = lib.import_audio(src, req.name or src.stem, hotwords=req.hotwords, lang=req.lang, course=req.course, translate=req.translate and req.lang != "zh")
    return {"recording": meta, "job": jobs.submit(meta["id"]) if models.choose_asr() else None}


@app.get("/api/jobs/{jid}")
def job(jid: str):
    j = jobs.jobs.get(jid)
    if j is None:
        raise HTTPException(404)
    return jobs.snapshot(j)


@app.post("/api/jobs/{jid}/{action}")
def job_control(jid: str, action: Literal["pause", "resume", "cancel"]):
    try:
        return jobs.control(jid, action)
    except KeyError:
        raise HTTPException(404, "任务不存在")
    except ValueError as e:
        raise HTTPException(409, str(e))


# ---------------------------------------------------------------- 静态
@app.middleware("http")
async def _no_cache_web(request, call_next):
    """前端文件每次都让浏览器重新验证，改了代码刷新即生效；音频仍可缓存。"""
    allowed = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
    if request.headers.get("host") not in allowed:
        return JSONResponse(status_code=403, content={"detail": "只接受本机应用连接"})
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin not in {f"http://{host}" for host in allowed}:
        return JSONResponse(status_code=403, content={"detail": "拒绝其他网页触发本地操作"})
    resp = await call_next(request)
    if not request.url.path.startswith(("/library", "/api")):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


app.mount("/library", StaticFiles(directory=LIB_DIR), name="library")  # 音频走这里，支持 Range 拖进度条
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")


@app.exception_handler(Exception)
async def _err(_, exc):
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})
