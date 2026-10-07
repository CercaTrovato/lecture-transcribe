"""历史库：library/<YYYY-MM-DD_HH-MM-SS_名称>/ 下放 audio.wav（16k 单声道，已归一化）、meta.json、
segments.json（最终逐句）、live_segments.json（实时预览）、transcript.md/.srt/.txt。"""

from __future__ import annotations

import json
import time
import re
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import imageio_ffmpeg
import soundfile as sf

from . import procs
from .config import COURSES_PATH, LIB_DIR, SR

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from transcribe import normalize_audio  # 两遍线性响度归一化（与 CLI 共用）


def safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|]+', "_", (s or "").strip())
    s = re.sub(r"\s+", "_", s)
    return s[:60] or "untitled"


def ffmpeg_to_wav(src: Path, dst: Path, normalize: bool = True) -> None:
    """任意音视频 → 16k 单声道 wav（可选两遍线性响度归一化）。"""
    if normalize:
        normalize_audio(src, dst, SR)
        return
    procs.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-i", str(src), "-vn", "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", str(dst)], check=True)


def wav_duration(path: Path) -> float:
    with sf.SoundFile(str(path)) as f:
        return f.frames / f.samplerate


def load_courses() -> list[dict]:
    """courses.json：[{code, name, dir, transcripts_dir, next_module, hotwords[]}]。缺失或损坏返回 []。"""
    try:
        d = json.loads(COURSES_PATH.read_text(encoding="utf-8"))
        items = d["courses"] if isinstance(d, dict) else d
        out = []
        for c in items:
            if not c.get("code"):
                continue
            hw = c.get("hotwords") or []
            out.append({"code": c["code"], "name": c.get("name", ""), "dir": c.get("dir", ""),
                        "transcripts_dir": c.get("transcripts_dir") or (str(Path(c["dir"]) / "transcripts") if c.get("dir") else ""),
                        "next_module": c.get("next_module", ""), "hotwords": ", ".join(hw) if isinstance(hw, list) else str(hw),
                        "notes": c.get("notes", "")})
        return out
    except Exception:  # noqa: BLE001
        return []


class Library:
    def __init__(self, root: Path = LIB_DIR):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- 路径
    def dir(self, rid: str) -> Path:
        p = (self.root / rid).resolve()
        if p.parent != self.root.resolve():
            raise ValueError("非法 id")
        return p

    def audio(self, rid: str) -> Path:
        """录音/导入时是 audio.wav；转录完成后压成 audio.m4a 并删 wav（省 90% 磁盘），两者都可能存在。"""
        d = self.dir(rid)
        wav, m4a = d / "audio.wav", d / "audio.m4a"
        # 新录音 / 导入时两者都不存在 → 返回 wav 路径供写入；只有 wav 已被压掉时才指向 m4a
        return wav if wav.exists() or not m4a.exists() else m4a

    # ---------------------------------------------------------------- 增删查
    def create(self, name: str, source: str, **extra) -> dict:
        now = datetime.now()
        rid = f"{now:%Y-%m-%d_%H-%M-%S}_{safe_name(name)}"
        d = self.root / rid
        d.mkdir(parents=True, exist_ok=False)
        meta = {
            "id": rid,
            "name": name.strip() or rid,
            "created": now.isoformat(timespec="seconds"),
            "source": source,            # recording | import
            "status": "recording" if source == "recording" else "importing",
            "duration": 0.0,
            "lang": extra.get("lang", "auto"),
            "hotwords": extra.get("hotwords") or "",
            "course": extra.get("course") or "",
            "translate": bool(extra.get("translate", False)),
            "live": bool(extra.get("live", False)),
            "error": "",
        }
        self.save_meta(meta)
        return meta

    def save_meta(self, meta: dict) -> None:
        with self._lock:
            path = self.dir(meta["id"]) / "meta.json"
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)

    def get(self, rid: str) -> dict | None:
        p = self.dir(rid) / "meta.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def update(self, rid: str, **fields) -> dict:
        with self._lock:
            meta = self.get(rid)
            if meta is None:
                raise KeyError(rid)
            meta.update(fields)
            self.save_meta(meta)
            return meta

    def list(self) -> list[dict]:
        items = []
        for d in self.root.iterdir():
            if (d / "meta.json").exists():
                try:
                    items.append(json.loads((d / "meta.json").read_text(encoding="utf-8")))
                except json.JSONDecodeError:
                    continue
        return sorted(items, key=lambda m: m["created"], reverse=True)

    def delete(self, rid: str) -> None:
        shutil.rmtree(self.dir(rid), ignore_errors=True)

    # ---------------------------------------------------------------- 内容
    def segments(self, rid: str, live: bool = False) -> list[dict]:
        p = self.dir(rid) / ("live_segments.json" if live else "segments.json")
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []

    def save_segments(self, rid: str, segs: list[dict], live: bool = False) -> None:
        p = self.dir(rid) / ("live_segments.json" if live else "segments.json")
        p.write_text(json.dumps(segs, ensure_ascii=False), encoding="utf-8")

    def dropped(self, rid: str) -> list[dict]:
        p = self.dir(rid) / "transcript.dropped.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []

    def write_live_bilingual(self, rid: str) -> Path | None:
        segments = self.segments(rid, live=True)
        if not any(s.get("zh") for s in segments):
            return None
        lines = [f"# {self.get(rid)['name']} · 实时双语稿", "", "> 实时识别与译文，不是最终整篇精译。", ""]
        for segment in segments:
            seconds = int(segment["start"])
            lines += [f"## {seconds // 60:02d}:{seconds % 60:02d}", "", segment["text"], "", segment.get("zh", ""), ""]
        path = self.dir(rid) / "live-bilingual.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def transcript_paths(self, rid: str) -> dict:
        d = self.dir(rid)
        out = {k: str(d / f"transcript.{k}") for k in ("md", "srt", "txt") if (d / f"transcript.{k}").exists()}
        if (d / "transcript.zh.md").exists():
            out["zh_md"] = str(d / "transcript.zh.md")
        if (d / "live-bilingual.md").exists():
            out["live_md"] = str(d / "live-bilingual.md")
        return out

    # ---------------------------------------------------------------- 翻译
    def translation(self, rid: str) -> dict | None:
        p = self.dir(rid) / "translation.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def save_translation(self, rid: str, data: dict) -> None:
        (self.dir(rid) / "translation.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    def write_bilingual_md(self, rid: str) -> Path | None:
        """双语 Markdown：每段英文 + 中文，段首时间戳。"""
        tr = self.translation(rid)
        meta = self.get(rid)
        if not tr or not meta:
            return None
        hms = lambda t: f"{int(t) // 3600:02d}:{int(t) % 3600 // 60:02d}:{int(t) % 60:02d}"
        lines = [f"# {meta['name']}（双语）", "", f"- 翻译模型: {tr.get('model')}", f"- 术语表: {tr.get('course') or '无'}（{tr.get('glossary_terms', 0)} 条）", "", "---", ""]
        for p in tr["paras"]:
            lines += [f"**[{hms(p['start'])} – {hms(p['end'])}]** {p['en']}", "", f"> {p['zh'] or '（翻译失败）'}", ""]
        out = self.dir(rid) / "transcript.zh.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        return out

    # ---------------------------------------------------------------- 音频
    def import_audio(self, src: Path, name: str, **extra) -> dict:
        meta = self.create(name or src.stem, "import", **extra)
        try:
            with procs.bind(meta["id"]):   # 登记 ffmpeg，导入中可以被 cancel_import 掐掉
                ffmpeg_to_wav(src, self.audio(meta["id"]), normalize=True)
                meta = self.update(meta["id"], duration=wav_duration(self.audio(meta["id"])), original=src.name, status="recorded")
        except Exception:
            self.delete(meta["id"])
            raise
        return meta

    def cancel_import(self, rid: str) -> str:
        """取消导入：杀掉 ffmpeg（导入线程会自己删记录）；已经没有进程的残留记录直接删。
        返回前等记录真的消失，否则调用方刚刷新列表还会看到它（再自己删，兜住导入线程卡住的情况）。"""
        if procs.cancel(rid):
            for _ in range(30):
                if not self.dir(rid).exists():
                    return "cancelled"
                time.sleep(0.1)
            self.delete(rid)
            return "cancelled"
        meta = self.get(rid)
        if meta and meta.get("status") == "importing":
            self.delete(rid)
            return "stale"
        return "none"

    def recover(self) -> list[dict]:
        """服务被强杀会留下"进行中"的记录（进程没了，状态还停在 importing / transcribing）。
        启动时把它们改成可操作的终态，并清掉上传留下的临时文件。"""
        fixed = []
        for meta in self.list():
            if meta.get("status") not in ("importing", "recording", "finalizing", "queued", "transcribing"):
                continue
            rid = meta["id"]
            d = self.dir(rid)
            if (d / "audio.wav").exists() or (d / "audio.m4a").exists():
                fixed.append(self.update(rid, status="recorded",
                                         error="上次被中断（服务被关闭），音频已保留，可重新转录"))
            else:
                fixed.append(self.update(rid, status="error",
                                         error="导入被中断（服务被关闭），没有可用音频，请重新导入"))
        for p in self.root.glob("tmp*"):    # NamedTemporaryFile(dir=LIB_DIR) 留下的上传临时文件
            if p.is_file():
                p.unlink(missing_ok=True)
        return fixed

    def compress_audio(self, rid: str) -> Path:
        """wav → m4a (AAC 64k 单声道)，成功后删 wav。"""
        wav = self.dir(rid) / "audio.wav"
        m4a = self.dir(rid) / "audio.m4a"
        if not wav.exists():
            return m4a
        tmp = m4a.with_suffix(".tmp.m4a")
        subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav),
                        "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-movflags", "+faststart", str(tmp)], check=True)
        tmp.replace(m4a)
        wav.unlink()
        return m4a

    def export_to_vault(self, rid: str, module: str, overwrite: bool = False) -> Path:
        """把 transcript.txt 复制为 <课程>/transcripts/M0N-transcript.txt（默认不覆盖）。"""
        meta = self.get(rid)
        src = self.dir(rid) / "transcript.txt"
        if not src.exists():
            raise FileNotFoundError("还没有转录产物")
        course = next((c for c in load_courses() if c["code"] == meta.get("course")), None)
        if not course or not course["transcripts_dir"]:
            raise ValueError("该录音没有关联课程，或 courses.json 里没有它的 transcripts_dir")
        m = re.fullmatch(r"(M\d{2})(-part\d+|-partial)?", module)
        if not m:
            raise ValueError("模块号格式应为 M04 / M04-part1 / M04-partial")
        dst_dir = Path(course["transcripts_dir"])
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / f"{m.group(1)}-transcript{m.group(2) or ''}.txt"  # vault 约定：M04-transcript-part1.txt
        if dst.exists() and not overwrite:
            raise FileExistsError(f"已存在 {dst}")
        shutil.copyfile(src, dst)
        self.update(rid, exported=str(dst), module=module)
        return dst

    def finalize_recording(self, rid: str) -> dict:
        """录音结束：原地响度归一化，记录时长，状态 → recorded。"""
        wav = self.audio(rid)
        tmp = wav.with_suffix(".norm.wav")
        ffmpeg_to_wav(wav, tmp, normalize=True)
        tmp.replace(wav)
        return self.update(rid, duration=wav_duration(wav), status="recorded")
