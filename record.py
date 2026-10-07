"""
课堂录音：从默认麦克风录到 E 盘，按 Enter 停止；可选录完直接转录。

用法：
    uv run record.py                      # 录到 recordings/<时间戳>.wav，Enter 停止
    uv run record.py --name BDA_week3     # 自定义文件名
    uv run record.py --transcribe         # 停止后自动调用 transcribe.py
    uv run record.py --list               # 列出麦克风设备
    uv run record.py --device 9           # 指定设备编号
"""

from __future__ import annotations

import argparse
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf

from ltapp.config import DATA_DIR
REC_DIR = DATA_DIR / "recordings"
SR = 16000  # Whisper 输入采样率，直接录 16k 单声道省空间：1 小时 ≈ 115 MB


def normalize_inplace(wav: Path) -> None:
    """80 Hz 高通 + EBU R128 响度归一化到 -16 LUFS，让小声录音听回去也够响。"""
    import imageio_ffmpeg

    tmp = wav.with_suffix(".norm.wav")
    subprocess.run(
        [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav),
         "-af", "highpass=f=80,loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", str(tmp)],
        check=True,
    )
    tmp.replace(wav)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", help="文件名（不含扩展名），默认用时间戳")
    p.add_argument("--device", type=int, help="输入设备编号（--list 查看），默认系统默认麦克风")
    p.add_argument("--list", action="store_true", help="列出设备后退出")
    p.add_argument("--transcribe", action="store_true", help="录完自动转录")
    p.add_argument("--hotwords", help="传给 transcribe.py 的术语提示")
    p.add_argument("--no-norm", action="store_true", help="录完不做响度归一化（保留原始电平）")
    args = p.parse_args()

    if args.list:
        print(sd.query_devices())
        return

    REC_DIR.mkdir(parents=True, exist_ok=True)
    name = args.name or datetime.now().strftime("%Y-%m-%d_%H-%M")
    out = REC_DIR / f"{name}.wav"
    if out.exists():
        sys.exit(f"已存在：{out}")

    dev_info = sd.query_devices(args.device, "input")
    print(f"麦克风：{dev_info['name']}")
    print(f"文件：  {out}")
    print("录音中… 按 Enter 停止\n")

    q: queue.Queue[np.ndarray] = queue.Queue()
    stop = threading.Event()

    def cb(indata, frames, t, status):
        if status:
            print(f"\n[警告] {status}", file=sys.stderr)
        q.put(indata.copy())

    threading.Thread(target=lambda: (input(), stop.set()), daemon=True).start()

    t0 = time.time()
    peak_win, last_draw = 0.0, t0
    with sf.SoundFile(out, "w", samplerate=SR, channels=1, subtype="PCM_16") as f, \
         sd.InputStream(samplerate=SR, channels=1, dtype="float32", device=args.device, callback=cb):
        while not stop.is_set():
            try:
                chunk = q.get(timeout=0.2)
            except queue.Empty:
                continue
            f.write(chunk)
            peak_win = max(peak_win, float(np.abs(chunk).max()))
            now = time.time()
            if now - last_draw >= 0.5:
                elapsed = int(now - t0)
                bar = "#" * int(min(peak_win, 1.0) * 30)
                warn = "  ← 声音太小，靠近点/调高输入音量" if peak_win < 0.02 else ""
                print(f"\r{elapsed // 3600:02d}:{elapsed % 3600 // 60:02d}:{elapsed % 60:02d}  |{bar:<30}|{warn:<22}", end="", flush=True)
                peak_win, last_draw = 0.0, now
        # 把队列里剩余的写完
        while not q.empty():
            f.write(q.get())

    dur = time.time() - t0
    print(f"\n\n已保存：{out}  ({dur / 60:.1f} 分钟, {out.stat().st_size / 1e6:.0f} MB)")

    if not args.no_norm:
        print("响度归一化...", end=" ", flush=True)
        normalize_inplace(out)
        print("完成")

    if args.transcribe:
        cmd = [sys.executable, str(Path(__file__).with_name("transcribe.py")), str(out)]
        if args.hotwords:
            cmd += ["--hotwords", args.hotwords]
        subprocess.run(cmd, check=False)


if __name__ == "__main__":
    main()
