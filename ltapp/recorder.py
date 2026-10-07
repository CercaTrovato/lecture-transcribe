"""麦克风录音：开始 / 暂停 / 继续 / 结束。暂停期间丢帧，因此文件时间轴 = 有效录音时长，时间戳连续。"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
import soxr

from .config import SR


def list_input_devices() -> list[dict]:
    """可用输入设备。同一物理麦在多个 host API 下会重复出现，优先列 MME，其次 WASAPI。"""
    apis = {i: a["name"] for i, a in enumerate(sd.query_hostapis())}
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] <= 0:
            continue
        api = apis.get(d["hostapi"], "")
        out.append({"index": i, "name": d["name"], "api": api, "default": i == sd.default.device[0]})
    # MME 由 Windows 自动重采样到 16k，最省事；WASAPI 共享模式要走设备原生采样率 + 软件重采样
    out.sort(key=lambda x: (not x["default"], 0 if "MME" in x["api"] else 1))
    return out


class Recorder:
    def __init__(self):
        self.state = "idle"          # idle | recording | paused
        self.rid: str | None = None
        self.frames = 0
        self.peak = 0.0
        self._q: queue.Queue = queue.Queue()
        self._stream = None
        self._writer: threading.Thread | None = None
        self._file = None
        self._on_audio = None
        self._lock = threading.Lock()
        self.started_at = 0.0

    # ---------------------------------------------------------------- 属性
    @property
    def elapsed(self) -> float:
        return self.frames / SR

    def status(self) -> dict:
        return {"state": self.state, "rid": self.rid, "elapsed": round(self.elapsed, 1), "peak": round(self.peak, 3)}

    # ---------------------------------------------------------------- 控制
    def start(self, rid: str, wav_path: Path, device: int | None = None, on_audio=None) -> None:
        with self._lock:
            if self.state != "idle":
                raise RuntimeError("已在录音")
            self.rid, self.frames, self.peak = rid, 0, 0.0
            self._on_audio = on_audio
            self._q = queue.Queue()
            self._file = sf.SoundFile(str(wav_path), "w", samplerate=SR, channels=1, subtype="PCM_16")
            try:
                self._stream, self._in_rate = self._open_stream(device)
            except Exception:
                self._file.close()
                self.rid = None
                raise
            self._resampler = soxr.ResampleStream(self._in_rate, SR, 1, dtype="float32") if self._in_rate != SR else None
            self._writer = threading.Thread(target=self._write_loop, daemon=True)
            self.state = "recording"
            self.started_at = time.time()
            self._writer.start()
            self._stream.start()

    def pause(self) -> None:
        if self.state == "recording":
            self.state = "paused"

    def resume(self) -> None:
        if self.state == "paused":
            self.state = "recording"

    def stop(self) -> float:
        """停止并落盘，返回有效时长（秒）。"""
        with self._lock:
            if self.state == "idle":
                raise RuntimeError("未在录音")
            self.state = "stopping"
            self._stream.stop()
            self._stream.close()
            self._q.put(None)
            self._writer.join(timeout=10)
            self._file.close()
            dur = self.elapsed
            self.state = "idle"
            self.rid = None
            return dur

    # ---------------------------------------------------------------- 内部
    def _open_stream(self, device):
        """先试 16k；不行（WASAPI 共享模式）就用设备默认采样率，回调里软件重采样。"""
        last = None
        rates = [SR]
        try:
            native = int(sd.query_devices(device, "input")["default_samplerate"])
            if native != SR:
                rates.append(native)
        except Exception:  # noqa: BLE001
            pass
        rates += [r for r in (48000, 44100) if r not in rates]
        for rate in rates:
            try:
                st = sd.InputStream(samplerate=rate, channels=1, dtype="float32", device=device,
                                    blocksize=rate // 10, callback=self._callback)
                return st, rate
            except Exception as e:  # noqa: BLE001
                last = e
        raise RuntimeError(f"打不开麦克风（试过 {rates}）: {last}")

    def _callback(self, indata, n, t, status):
        if self.state != "recording":
            return
        chunk = indata[:, 0].copy()
        if self._resampler is not None:
            chunk = self._resampler.resample_chunk(chunk)
            if not len(chunk):
                return
        self._q.put(chunk)
        self.frames += len(chunk)
        self.peak = max(float(np.abs(chunk).max()), self.peak * 0.8)  # 衰减，让电平条有动态

    def _write_loop(self):
        while True:
            chunk = self._q.get()
            if chunk is None:
                break
            self._file.write(chunk)
            if self._on_audio:
                try:
                    self._on_audio(chunk)
                except Exception:
                    pass
