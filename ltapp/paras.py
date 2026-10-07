"""段落划分（与前端 web/app.js 的 splitParagraphs 同一套规则），供最终版翻译按段切分、双语导出。"""

from __future__ import annotations

import re

SENT_END = re.compile(r"[.?!。！？…][\"'”’)\]]?$")
GAP_HARD, GAP_SOFT = 3.0, 1.8      # 停顿 ≥3s 无条件分段；≥1.8s 且上一句已结束则分段
PARA_SOFT, PARA_HARD = 75.0, 120.0  # 超过 75s 在下一个句末切；超过 120s 无论如何切
LOOKBACK, MIN_PAUSE = 40.0, 0.6     # 超时无句末时，回溯 40s 找最长停顿（≥0.6s）切


def split_paragraphs(segments: list[dict]) -> list[dict]:
    """segments: [{start, end, text}] → [{start, end, idx: [segment 下标...]}]"""
    paras: list[dict] = []
    cur: dict | None = None
    for i, s in enumerate(segments):
        st, en = s["start"], s["end"]
        prev = segments[cur["idx"][-1]] if cur else None
        gap = st - prev["end"] if prev else 0.0
        sent_end = bool(SENT_END.search(prev["text"])) if prev else True
        brk = (cur is None or gap >= GAP_HARD or (gap >= GAP_SOFT and sent_end)
               or (sent_end and st - cur["start"] >= PARA_SOFT) or st - cur["start"] >= PARA_HARD)
        if not brk and cur and st - cur["start"] >= PARA_SOFT:
            best, best_gap = -1, MIN_PAUSE
            idx = cur["idx"]
            for j in range(1, len(idx)):
                if segments[idx[j]]["start"] < st - LOOKBACK:
                    continue
                g = segments[idx[j]]["start"] - segments[idx[j - 1]]["end"]
                if g >= best_gap:
                    best_gap, best = g, j
            if best > 0:
                tail = idx[best:]
                del idx[best:]
                cur["end"] = segments[idx[-1]]["end"]
                cur = {"start": segments[tail[0]]["start"], "end": segments[tail[-1]]["end"], "idx": tail}
                paras.append(cur)
        if brk:
            cur = {"start": st, "end": en, "idx": []}
            paras.append(cur)
        cur["idx"].append(i)
        cur["end"] = max(cur["end"], en)
    return paras


def para_text(segments: list[dict], para: dict) -> str:
    return " ".join(segments[i]["text"] for i in para["idx"])
