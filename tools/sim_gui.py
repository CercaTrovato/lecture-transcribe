"""开发用：用"模拟麦克风"跑真正的 GUI —— 点开始录音后按真实时间回放一个 wav，整条链路（实时转录 / 实时翻译 /
结束收尾 / 整文件转录 / 最终版翻译 / 前端渲染）与真实录音完全一致，只是不用对着麦克风说话。

用法：先 stop_gui.bat 停掉正式服务（两个进程同时占 GPU 会互相拖慢），再
    uv run tools/sim_gui.py recordings/test.wav [--port 8765] [--loop]
然后在浏览器里正常操作。--loop 让 wav 播完后从头再来（模拟长课）。"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ltapp.config import HOST, SR  # noqa: E402


class SimRecorder:
    """与 ltapp.recorder.Recorder 同接口；start() 起一个线程按真实时间把 wav 喂给 on_audio 并写入文件。"""

    def __init__(self, wav: Path, loop: bool = False):
        x, sr = sf.read(str(wav), dtype="float32")
        if x.ndim > 1:
            x = x.mean(1)
        assert sr == SR, f"需要 {SR}Hz 单声道 wav"
        self.src, self.loop = x, loop
        self.state = "idle"
        self.rid = None
        self.frames = 0
        self.peak = 0.0
        self.started_at = 0.0
        self._stop = threading.Event()
        self._thread = None
        self._file = None
        self._on_audio = None

    @property
    def elapsed(self) -> float:
        return self.frames / SR

    def status(self) -> dict:
        return {"state": self.state, "rid": self.rid, "elapsed": round(self.elapsed, 1), "peak": round(self.peak, 3)}

    def start(self, rid, wav_path, device=None, on_audio=None):
        if self.state != "idle":
            raise RuntimeError("已在录音")
        self.rid, self.frames, self.peak, self._on_audio = rid, 0, 0.0, on_audio
        self._file = sf.SoundFile(str(wav_path), "w", samplerate=SR, channels=1, subtype="PCM_16")
        self._stop.clear()
        self.state = "recording"
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._play, daemon=True)
        self._thread.start()

    def pause(self):
        if self.state == "recording":
            self.state = "paused"

    def resume(self):
        if self.state == "paused":
            self.state = "recording"

    def stop(self) -> float:
        if self.state == "idle":
            raise RuntimeError("未在录音")
        self._stop.set()
        self._thread.join(timeout=5)
        self._file.close()
        dur = self.elapsed
        self.state, self.rid = "idle", None
        return dur

    def _play(self):
        block = SR // 10
        pos, t0, sent = 0, time.time(), 0
        while not self._stop.is_set():
            if pos >= len(self.src):
                if not self.loop:
                    time.sleep(0.1)     # 播完了：像麦克风静音一样等用户点结束
                    continue
                pos = 0
            # 按真实时间节奏：已发送的块数不能超过墙钟时间
            if sent * 0.1 > time.time() - t0:
                time.sleep(0.02)
                continue
            sent += 1
            if self.state != "recording":   # 暂停：丢帧（与真实录音一致）
                pos += block
                continue
            chunk = self.src[pos:pos + block]
            pos += block
            self._file.write(chunk)
            self.frames += len(chunk)
            self.peak = max(float(np.abs(chunk).max()), self.peak * 0.8)
            if self._on_audio:
                self._on_audio(chunk)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--loop", action="store_true")
    a = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    import ltapp.server as srv
    srv.recorder = SimRecorder(Path(a.wav), loop=a.loop)
    srv.jobs.is_recording = lambda: srv.recorder.state != "idle"
    print(f"[sim] 模拟麦克风：{a.wav}（{len(srv.recorder.src) / SR:.0f}s{'，循环' if a.loop else ''}） → http://{HOST}:{a.port}")
    uvicorn.run(srv.app, host=HOST, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
