"""翻译引擎：管理一个 llama-server 子进程（一次只跑一个模型），提供术语表 + 背景的翻译调用。

实时用 Hy-MT2-1.8B-Q4 跑 CPU（每句中位 ~0.4s）；最终版用 Hy-MT2-7B-Q4 跑 GPU（需先卸载 Whisper，2 小时课 ~5 分钟）。
选型依据见 docs/mt-bakeoff-2026-09-18.md。
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
import urllib.request
import os
import shutil
import sys
from pathlib import Path

from .config import COURSES_PATH

from .config import DATA_DIR
LLAMA_SERVER = Path(os.environ.get("LT_LLAMA_SERVER", shutil.which("llama-server") or
                    str(DATA_DIR / "runtime" / ("llama-server.exe" if sys.platform == "win32" else "llama-server"))))
GGUF_DIR = DATA_DIR / "models"
MT_PORT = int(os.environ.get("LT_MT_PORT", "8790"))
# 实时翻译跑 CPU（-ngl 0）：Windows WDDM 下另一个进程只要持有活跃 CUDA 上下文，CTranslate2 的实时转录就慢 14 倍
# （跨进程 GPU 时间片切换；实测 0.36s/轮 → 5s/轮）。1.8B Q4 在 8 线程 CPU 上每句中位 0.42s，够用。
# 最终版翻译跑 GPU：那时 Whisper 已卸载，实测无干扰。
MT_MODELS = {
    "live": {"gguf": GGUF_DIR / "Hy-MT2-1.8B-Q4_K_M.gguf", "ctx": 2048, "repo": "tencent/Hy-MT2-1.8B-GGUF", "ngl": 0, "threads": 8},
    "final": {"gguf": GGUF_DIR / "Hy-MT2-7B-Q4_K_M.gguf", "ctx": 4096, "repo": "tencent/Hy-MT2-7B-GGUF", "ngl": 99, "threads": 8},
}
TARGET = {"zh": "Chinese", "en": "English"}


# ---------------------------------------------------------------- 术语表
def _norm_term(s: str) -> str:
    """整词匹配用：小写、去标点、每个词去掉复数 s（ASR 常漏/多 s：account payable ↔ accounts payable）。"""
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in re.findall(r"[a-z0-9]+", s.lower()))


def parse_glossary_md(path: Path) -> dict:
    """解析知识库术语表（| 中文 | English | … | 行）→ {English: 中文}。"""
    gl = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|") or line.startswith("|---") or ("中文" in line and "English" in line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        zh = re.sub(r"[⭐⚠️]|\*\*|[（(].*?[）)]", "", cells[0]).strip()
        en = re.sub(r"\*\*|\[\[.*?\]\]", "", cells[1]).strip()
        en = re.sub(r"\s*\(.*?\)\s*$", "", en)
        if zh and en and re.search(r"[A-Za-z]", en) and re.search(r"[一-鿿]", zh) and len(en) < 60:
            gl[en] = zh
    return gl


_gl_cache: dict[str, tuple[float, dict]] = {}


def load_glossary(course: str | None) -> dict:
    """课程术语表：courses.json 的 glossary 字段优先，否则解析 <dir>/_meta/术语表.md；缓存按文件时间失效。"""
    if not course:
        return {}
    try:
        courses = json.loads(COURSES_PATH.read_text(encoding="utf-8"))
        items = courses["courses"] if isinstance(courses, dict) else courses
        c = next((x for x in items if x.get("code") == course), None)
    except Exception:  # noqa: BLE001
        c = None
    if not c:
        return {}
    if isinstance(c.get("glossary"), dict) and c["glossary"]:
        return dict(c["glossary"])
    md = Path(c.get("dir", "")) / "_meta" / "术语表.md"
    if not md.exists():
        return {}
    mtime = md.stat().st_mtime
    hit = _gl_cache.get(course)
    if hit and hit[0] == mtime:
        return hit[1]
    gl = parse_glossary_md(md)
    _gl_cache[course] = (mtime, gl)
    return gl


def glossary_hits(text: str, gl: dict, limit: int) -> list[tuple[str, str]]:
    """文本里命中的术语，整词匹配（大小写 / 单复数不敏感），跳过 ≤3 字母的非缩写词，被更长术语包含的短术语去掉，长词优先。"""
    nt = " " + _norm_term(text) + " "
    hits = []
    for en, zh in gl.items():
        if len(en) <= 3 and not en.isupper():
            continue
        ne = _norm_term(en)
        if ne and f" {ne} " in nt:
            hits.append((en, zh))
    hits.sort(key=lambda x: -len(x[0]))
    out = []
    for en, zh in hits:
        if not any(_norm_term(en) in _norm_term(longer) and en != longer for longer, _ in out):
            out.append((en, zh))
    return out[:limit]


def build_prompt(source: str, terms: list[tuple[str, str]], background: str, target: str = "zh") -> str:
    """Hy-MT2 官方模板：术语对照 + 背景信息 + 只输出译文。"""
    parts = []
    if terms:
        parts.append("Reference the following translations:\n" + "\n".join(f"{en} translates to {zh}" for en, zh in terms))
    if background:
        parts.append(f"[Background Information]\n{background}")
    parts.append(f"Translate the following text into {TARGET.get(target, target)}. Note that you must ONLY output the translated result "
                 f"without any additional explanation:\n\n{source}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- 引擎
class MTEngine:
    def __init__(self, store=None):
        self.store = store
        self.mode: str | None = None      # live | final | None
        self.proc: subprocess.Popen | None = None
        self.error = ""
        self._lock = threading.Lock()
        self.busy = 0

    def available(self, mode: str) -> bool:
        path = self.store.path("mt-live" if mode == "live" else "mt-final") if self.store else MT_MODELS[mode]["gguf"]
        return LLAMA_SERVER.exists() and path is not None and path.exists()

    def missing(self, mode: str) -> str:
        if not LLAMA_SERVER.exists():
            return f"缺少 llama-server：{LLAMA_SERVER}"
        path = self.store.path("mt-live" if mode == "live" else "mt-final") if self.store else MT_MODELS[mode]["gguf"]
        if path is None or not path.exists():
            return f"尚未安装 {MT_MODELS[mode]['gguf'].name}，请在模型管理中主动安装。"
        return ""

    def ensure(self, mode: str) -> bool:
        """切到指定模型（已是则直接返回）。加载 1.8B 约 1s，7B 约 3s。"""
        with self._lock:
            if self.mode == mode and self.proc and self.proc.poll() is None:
                return True
            self._stop_locked()
            if not self.available(mode):
                self.error = self.missing(mode)
                return False
            m = dict(MT_MODELS[mode])
            if self.store:
                m["gguf"] = self.store.path("mt-live" if mode == "live" else "mt-final")
            if os.environ.get("LT_DEVICE") == "cpu":
                m["ngl"] = 0
            try:
                self.proc = subprocess.Popen(
                    [str(LLAMA_SERVER), "-m", str(m["gguf"]), "-ngl", str(m["ngl"]), "-c", str(m["ctx"]),
                     *(["-fa", "on"] if m["ngl"] else []),
                     "--port", str(MT_PORT), "--host", "127.0.0.1", "-t", str(m["threads"]), "--log-disable"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception as e:  # noqa: BLE001
                self.error = f"启动 llama-server 失败: {e}"
                return False
            for _ in range(180):
                time.sleep(0.5)
                if self.proc.poll() is not None:
                    self.error = f"llama-server 退出 code={self.proc.returncode}（显存不足？）"
                    self.proc = None
                    return False
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{MT_PORT}/health", timeout=1) as r:
                        if json.loads(r.read()).get("status") == "ok":
                            self.mode = mode
                            self.error = ""
                            print(f"[mt] {mode} 模型就绪：{m['gguf'].name}")
                            return True
                except Exception:  # noqa: BLE001
                    pass
            self.error = "llama-server 90s 未就绪"
            self._stop_locked()
            return False

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(10)
            except Exception:  # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            self.proc = None
            print("[mt] 翻译模型已卸载")
        self.mode = None

    def translate(self, source: str, terms: list[tuple[str, str]], background: str = "", target: str = "zh",
                  max_tokens: int = 1024, timeout: int = 300) -> str:
        body = {"messages": [{"role": "user", "content": build_prompt(source, terms, background, target)}],
                "max_tokens": max_tokens, "stream": False,
                "temperature": 0.7, "top_p": 0.6, "top_k": 20, "repeat_penalty": 1.05}   # 官方推荐采样
        req = urllib.request.Request(f"http://127.0.0.1:{MT_PORT}/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        self.busy += 1
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
        finally:
            self.busy -= 1
        text = d["choices"][0]["message"]["content"].strip()
        # 小模型偶发把指令模板复述出来：出现模板关键句就视为失败，调用方可重试或留空
        if re.search(r"translates to|Translate the following|Reference the following|将以下文本翻译|请参考以下翻译", text):
            raise ValueError("模型复述了指令模板")
        return text
