"""把已有的 .srt 转成 Notta 兼容 .txt（不必重跑模型）。用法：uv run srt2txt.py a.srt [b.srt ...]"""

import sys
from pathlib import Path

from transcribe import write_txt


def parse_srt(path: Path):
    segs = []
    for blk in path.read_text(encoding="utf-8").strip().split("\n\n"):
        lines = blk.split("\n")
        if len(lines) < 3:
            continue
        a, b = lines[1].split(" --> ")
        to_s = lambda t: int(t[:2]) * 3600 + int(t[3:5]) * 60 + float(t[6:].replace(",", "."))
        segs.append({"start": to_s(a), "end": to_s(b), "text": " ".join(lines[2:]).strip()})
    return segs


for p in map(Path, sys.argv[1:]):
    out = p.with_suffix(".txt")
    write_txt(parse_srt(p), out)
    print(f"{out}")
