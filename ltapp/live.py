"""实时转录 v2：滑动窗口 + 逐词提交（whisper_streaming 的 LocalAgreement-2 思路）。

每 STEP 秒对"最近一段音频（从上次裁剪点到现在）"用 large-v3-turbo 重跑一次带词级时间戳的转录；
连续两轮结果一致的词提交为定稿（不再改），末尾还在变的词作为 pending 灰字显示。
模型每轮都看到跨块的上下文，所以没有"块边界切句"的问题；结束录音后整文件再用 large-v3 重跑出最终版。
"""

from __future__ import annotations

import queue
import re
import threading
import time

import numpy as np

from .config import SR
from .engine import ModelHolder
import transcribe  # 复用最终版的幻觉判定（结尾语 / 重复 / 术语复读）

STEP = 0.8          # 两轮推理之间至少新增的音频秒数
USE_VAD = False     # 每轮对整段缓冲跑 VAD 会随缓冲增长改变切块，让开头的词也不稳定；关掉后靠噪声过滤兜底
NO_SPEECH = 0.6     # 段的 no_speech_prob 超过即视为静音幻觉
MAX_BUF = 20.0      # 缓冲超过这个长度就裁剪（裁在句末 / 停顿处）
KEEP = 8.0          # 裁剪时至少保留最近这么多秒，保证下一轮有上下文
CTX_CHARS = 200     # 作为 initial_prompt 的已提交文本长度


def _norm(w: str) -> str:
    return re.sub(r"[^a-z0-9'\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+", "", w.lower())


class LiveTranscriber:
    def __init__(self, holder: ModelHolder, lang: str = "en", hotwords: str | None = None,
                 mt=None, glossary: dict | None = None, target: str = "zh"):
        self.holder, self.lang, self.hotwords = holder, lang, hotwords or None
        self.words: list[dict] = []      # 已提交：{"s": 起, "e": 止, "w": 词}（绝对秒）
        self.pending = ""                # 未提交的灰字
        # 实时翻译：每当一句定稿（句末标点），在后台线程逐句翻译；sents[k] = {"ws","we","en","zh"}
        self.mt, self.glossary, self.target = mt, glossary or {}, target
        self.sents: list[dict] = []
        self.mt_error = ""
        self._sent_start = 0             # 当前未成句的第一个词下标
        self._mt_q: queue.Queue = queue.Queue()
        self.busy = False
        self.last_latency = 0.0          # 最近一次提交的词相对录音进度的延迟（秒），供监控
        self.iterations = 0
        self.log: list[dict] = []        # 每轮：缓冲长度 / 推理耗时 / 假设词数 / 提交词数
        self._q: queue.Queue = queue.Queue()
        self._buf = np.zeros(0, dtype=np.float32)
        self._buf_t0 = 0.0               # _buf[0] 的绝对时间
        self._fed = 0.0                  # 已喂入的音频总秒数
        self._ran_at = 0.0               # 上一轮推理时的 _fed
        self._prev_hyp: list[tuple] = []  # 上一轮未提交的假设 [(s, e, w)]
        self._stop = threading.Event()
        self._asr_lease = getattr(holder, "model_id", None) if hasattr(holder, "store") else None
        self._mt_lease = "mt-live" if mt is not None and getattr(mt, "store", None) is not None else None
        if self._asr_lease:
            holder.store.acquire(self._asr_lease)
        if self._mt_lease:
            try:
                mt.store.acquire(self._mt_lease)
            except Exception:
                if self._asr_lease:
                    holder.store.release(self._asr_lease)
                raise
        self._thread = threading.Thread(target=self._audio_worker, daemon=True)
        self._thread.start()
        self._mt_thread = threading.Thread(target=self._translation_worker, daemon=True) if mt is not None else None
        if self._mt_thread is not None:
            self._mt_thread.start()

    # ---------------------------------------------------------------- 外部接口
    def feed(self, chunk: np.ndarray) -> None:
        self._q.put(chunk)

    def stop(self) -> list[dict]:
        """停止：把剩余音频跑最后一轮并全部提交，返回按句切分的 segments（供预览）。"""
        self._stop.set()
        self._thread.join(timeout=120)
        if self._mt_thread is not None:
            # 最后一轮提交的句子在这里才入队，所以翻译线程不能靠 _stop 退出：用哨兵 None 结束它（FIFO 保证前面的句子先译完），
            # 最多等 10s（CPU 上每句 ~0.4s；翻译服务挂了就不等，剩下的句子留空）
            self._close_sentence()
            self._mt_q.put(None)
            self._mt_thread.join(timeout=10)
        return self.segments()

    def segments(self) -> list[dict]:
        """已提交的词按句末标点合成句子段；开了实时翻译的话，把在该段内起始的句子的译文带上（zh），供结束后的预览继续显示中文。"""
        zh_at = {s["ws"]: s["zh"] for s in self.sents if s.get("zh")}
        out, cur, i0 = [], [], 0
        for i, w in enumerate(self.words):
            cur.append(w)
            if re.search(r"[.?!。！？]$", w["w"]) or i == len(self.words) - 1:
                seg = {"start": cur[0]["s"], "end": cur[-1]["e"], "text": " ".join(x["w"] for x in cur)}
                zh = "".join(zh_at[k] for k in range(i0, i + 1) if k in zh_at)
                if zh:
                    seg["zh"] = zh
                out.append(seg)
                cur, i0 = [], i + 1
        return out

    # ---------------------------------------------------------------- 内部
    def _audio_worker(self):
        try:
            self._loop()
        finally:
            if self._asr_lease:
                self.holder.store.release(self._asr_lease)

    def _translation_worker(self):
        try:
            self._mt_loop()
        finally:
            if self._mt_lease:
                self.mt.store.release(self._mt_lease)

    def _drain(self):
        parts = []
        while True:
            try:
                parts.append(self._q.get_nowait())
            except queue.Empty:
                break
        if parts:
            self._buf = np.concatenate([self._buf, *parts])
            self._fed += sum(len(p) for p in parts) / SR

    def _model(self):
        if self.holder.live_model is not None:
            return self.holder.live_model
        return self.holder.model.model if self.holder.ready else None

    def _hypothesis(self) -> list[tuple]:
        """对当前缓冲跑一轮，返回 [(绝对起, 绝对止, 词)]。"""
        self._trim()  # 推理变慢时，先去掉已定稿音频 / 长静音，避免整段积压进入模型
        model = self._model()
        if model is None or len(self._buf) < SR // 2:
            return []
        audio = self._buf
        peak = float(np.abs(audio).max())
        if 0 < peak < 0.3:
            audio = audio * (0.5 / peak)
        # 全窗都没有语音时不调用 Whisper，避免静音幻觉变成待定词，反过来阻止缓冲裁剪。
        # 不用 VAD 重切识别窗口，保留 LocalAgreement 所需的稳定上下文。
        from faster_whisper.vad import get_speech_timestamps
        if not get_speech_timestamps(audio, threshold=0.35, min_silence_duration_ms=1000, speech_pad_ms=400):
            return []
        # 上下文只能用"缓冲起点之前"的已提交文字：缓冲内音频对应的词若也进 prompt，模型会当作已转录而跳过它们
        ctx = " ".join(w["w"] for w in self.words if w["e"] <= self._buf_t0 + 0.05)[-CTX_CHARS:]
        if not re.search(r"[.?!,]", ctx):   # 没有上下文，或上下文已陷入无标点模式（会自我强化）→ 垫一句带标点的种子句
            ctx = (transcribe.SEED_PROMPT.get(self.lang, "") + " " + ctx).strip()
        t_infer = time.time()
        segs, info = model.transcribe(
            audio, language=None if self.lang == "auto" else self.lang, beam_size=1, word_timestamps=True, vad_filter=USE_VAD,
            condition_on_previous_text=False, hotwords=self.hotwords, initial_prompt=ctx or None,
        )
        if self.lang == "auto":
            self.lang = info.language
        hyp = []
        for s in segs:
            if s.no_speech_prob > NO_SPEECH or self._is_noise(s.text):   # 关了 VAD，静音幻觉靠这两道兜
                continue
            for w in s.words or []:
                t = w.word.strip()
                if t:
                    hyp.append((self._buf_t0 + w.start, self._buf_t0 + w.end, t))
        self.log.append({"buf": round(len(self._buf) / SR, 1), "infer": round(time.time() - t_infer, 2), "hyp": len(hyp)})
        return hyp

    def _iterate(self, final: bool = False) -> None:
        self.busy = True
        try:
            hyp = self._hypothesis()
        except Exception as e:  # noqa: BLE001
            print(f"[live] 推理失败: {e}")
            self.busy = False
            return
        self.busy = False
        self.iterations += 1
        # 缓冲开头到"最后已提交词"的部分已定稿：新假设里这部分先剔掉，只对齐未提交的尾部。
        # 先按时间宽松剔除（留 1s 余量，词级时间戳每轮有抖动），再按文本去重（新假设开头 ≤5 词与已提交末尾重叠）
        if self.words:
            committed_end = self.words[-1]["e"]
            hyp = [h for h in hyp if h[0] > committed_end - 1.5]   # 粗过滤：明显早于已提交末尾的词
            # 以文本为锚：在新假设开头 12 个词里找"已提交末尾 k 个词"（k=5..1），找到就从其后接续；
            # 找不到（模型换了说法）才退回时间判定，且放宽到 -0.3s（turbo 的词级时间戳跨轮抖动较大）
            tail = [_norm(w["w"]) for w in self.words[-5:]]
            norms = [_norm(h[2]) for h in hyp]
            anchor = None
            for k in range(min(5, len(tail)), 0, -1):
                pat = tail[-k:]
                for j in range(0, min(len(norms) - k + 1, 12)):
                    if norms[j:j + k] == pat and abs(hyp[j + k - 1][1] - committed_end) <= 1.5:
                        anchor = j + k
                        break
                if anchor is not None:
                    break
            hyp = hyp[anchor:] if anchor is not None else [h for h in hyp if h[0] > committed_end - 0.3]
        # LocalAgreement-2：与上一轮假设的公共前缀提交
        n = 0
        if final:
            n = len(hyp)
        else:
            for a, b in zip(self._prev_hyp, hyp):
                if _norm(a[2]) == _norm(b[2]):
                    n += 1
                else:
                    break
        if n:
            for s, e, w in hyp[:n]:
                if w == "i" or w.startswith("i'"):
                    w = "I" + w[1:]
                self.words.append({"s": round(s, 2), "e": round(e, 2), "w": w})
                if self.mt is not None and re.search(r"[.?!。！？]$", w) and not w.endswith(("...", "…")):   # 省略号不算句末
                    self._close_sentence()
            self.last_latency = round(self._fed - hyp[n - 1][1], 2)
        self._prev_hyp = hyp[n:]
        self.pending = " ".join(w for _, _, w in hyp[n:])
        if self.log:
            self.log[-1]["commit"] = n
        self._trim()

    # ---------------------------------------------------------------- 实时翻译
    def _close_sentence(self) -> None:
        if self.lang == "zh":
            return
        ws, we = self._sent_start, len(self.words)
        if we - ws < 1:
            return
        en = " ".join(w["w"] for w in self.words[ws:we])
        self.sents.append({"ws": ws, "we": we, "en": en, "zh": None})
        self._sent_start = we
        self._mt_q.put(len(self.sents) - 1)

    def _mt_loop(self) -> None:
        from . import mt as mtmod
        while True:
            k = self._mt_q.get()
            if k is None:            # stop() 发的哨兵
                return
            s = self.sents[k]
            bg = " ".join(x["en"] for x in self.sents[max(0, k - 2):k])
            try:
                if self.mt.mode is None and not self.mt.ensure("live"):
                    self.mt_error = self.mt.error
                    s["zh"] = ""
                    continue
                s["zh"] = self.mt.translate(s["en"], mtmod.glossary_hits(s["en"], self.glossary, 8), bg, self.target,
                                            max_tokens=max(64, len(s["en"].split()) * 4), timeout=30)
            except Exception as e:  # noqa: BLE001
                self.mt_error = str(e)
                s["zh"] = ""

    def _trim(self) -> None:
        """缓冲过长时裁剪。只裁在"句末"或"词后有停顿"的已提交词处（切在词中间会让下一轮丢词），
        切点再向前留 0.25s 余量；至少保留最近 KEEP 秒。实在没有合适的点就等下一轮。"""
        dur = len(self._buf) / SR
        if dur <= MAX_BUF:
            return
        # 只检查最后已定稿词之后的音频；裁掉领先的静音，不压缩中间时间轴，
        # 不跳过还在等待定稿的语音。VAD 仅用于找静音边界，Whisper 仍整窗识别。
        from faster_whisper.vad import get_speech_timestamps
        start = max(0, int(((self.words[-1]["e"] if self.words else self._buf_t0) - self._buf_t0) * SR))
        tail = self._buf[start:]
        if len(tail) >= SR:
            # 与识别时相同地放大低音量语音，避免把轻声讲话误当成可裁掉的静音。
            peak = float(np.abs(tail).max())
            if 0 < peak < 0.3:
                tail = tail * (0.5 / peak)
            speech = get_speech_timestamps(tail, threshold=0.35, min_silence_duration_ms=1000, speech_pad_ms=400)
            cut = self._buf_t0 + (start + speech[0]["start"]) / SR if speech else self._buf_t0 + dur - KEEP
            if self._prev_hyp:
                cut = min(cut, self._prev_hyp[0][0] - 0.25)
            if cut > self._buf_t0 + 1:
                self._cut_buffer(cut)
                dur = len(self._buf) / SR
        if dur <= MAX_BUF or len(self.words) < 2:
            return
        limit = self._buf_t0 + dur - KEEP
        cands = [i for i, w in enumerate(self.words[:-1]) if self._buf_t0 < w["e"] <= limit]
        if not cands:
            return
        pick = None
        for i in reversed(cands):                       # 优先最近的句末
            if re.search(r"[.?!]$", self.words[i]["w"]):
                pick = i
                break
        if pick is None:
            for i in reversed(cands):                   # 其次词后有 ≥0.5s 停顿
                if self.words[i + 1]["s"] - self.words[i]["e"] >= 0.5:
                    pick = i
                    break
        if pick is None:
            if dur < MAX_BUF * 1.8:                     # 还能撑，等句末出现
                return
            pick = cands[-1]                            # 撑不住了：切最近的词，靠余量 + 锚定去重补救
        cut = max(self._buf_t0, self.words[pick]["e"] - 0.25)
        self._cut_buffer(cut)

    def _cut_buffer(self, cut: float) -> None:
        k = int((cut - self._buf_t0) * SR)
        cut = self._buf_t0 + k / SR
        self._prev_hyp = [h for h in self._prev_hyp if h[0] >= cut]
        self._buf = self._buf[k:]
        self._buf_t0 = cut

    def _is_noise(self, text: str) -> bool:
        t = text.lower()
        if any(ph in t for ph in transcribe.YT_PHRASES) or any(ph in t for ph in transcribe.SEED_PHRASES):
            return True
        tk = transcribe._tokens(text)
        if (len(tk) >= 3 and len(set(tk)) <= 1) or (len(tk) >= 4 and len(set(tk)) <= 2) or (4 <= len(tk) <= 16 and transcribe._ngram_repeat(tk)):
            return True
        if self.hotwords:
            phrases = [h.strip().lower() for h in self.hotwords.split(",") if h.strip()]
            hits = sum(1 for ph in phrases if ph in t)
            if hits >= 3 and len(tk) <= 2 * hits + 2:
                return True
        return False

    def _loop(self):
        while not self._stop.is_set():
            self._drain()
            if self._fed - self._ran_at >= STEP:
                self._ran_at = self._fed
                self._iterate()
            else:
                self._stop.wait(0.1)
        self._drain()
        self._iterate(final=True)
