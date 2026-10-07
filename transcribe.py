"""
本地课程录音转录：faster-whisper (large-v3) + ffmpeg 响度归一化 + VAD 自动分段。

用法：
    uv run transcribe.py <音频或视频文件>... [--model large-v3] [--out 输出目录]

输出（与输入同名）：
    <name>.srt   带时间戳字幕
    <name>.txt   Notta 兼容布局（时间戳一行 + 文本一行），可直接改名为 M0N-transcript.txt 交给 CityU vault 的转录融合流程
    <name>.md    Markdown 段落视图，每段前带时间戳，给人读
"""

from __future__ import annotations

import argparse
import json
import os
import re
import site
import subprocess
import sys
import time
import wave
from pathlib import Path


# ---------------------------------------------------------------------------
# Windows 上 ctranslate2 需要能找到 pip 装的 cuDNN / cuBLAS DLL
# ---------------------------------------------------------------------------
_DLL_HANDLES = []
def _register_nvidia_dlls() -> None:
    if sys.platform != "win32":
        return
    extra = os.environ.get("LT_EXTRA_SITE")
    for sp in site.getsitepackages() + ([extra] if extra else []):
        nv = Path(sp) / "nvidia"
        if not nv.is_dir():
            continue
        for d in nv.glob("*/bin"):
            _DLL_HANDLES.append(os.add_dll_directory(str(d)))
            os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")


_register_nvidia_dlls()

# 模型缓存放 E 盘（C 盘没空间）；用户环境变量 HF_HOME 已设同值，这里兜底
from ltapp.config import DATA_DIR
os.environ.setdefault("HF_HOME", str(DATA_DIR / "cache" / "huggingface"))
os.environ["HF_HUB_OFFLINE"] = "1"  # 模型只能通过明确的模型管理操作下载

import imageio_ffmpeg  # noqa: E402
from faster_whisper import BatchedInferencePipeline, WhisperModel  # noqa: E402
from tqdm import tqdm  # noqa: E402

from ltapp import procs  # noqa: E402  ffmpeg 统一走可取消的 runner（导入可中途取消）


# ---------------------------------------------------------------------------
# 音频预处理
# ---------------------------------------------------------------------------
LOUDNORM = "I=-16:TP=-1.5:LRA=11"
HIGHPASS = "highpass=f=80"   # 80 Hz 高通去空调 / 桌子低频嗡声


def normalize_audio(src: Path, dst: Path, sr: int = 16000) -> None:
    """任意音视频 → 16 kHz 单声道 wav，两遍线性响度归一化。
    第一遍测整段的综合响度（EBU R128 带门控，纯噪声段不计入），第二遍用**固定增益**处理——
    小声讲课照样被抬起来，但暂停 / 课间只有风扇声的段落不会像单遍动态模式那样被单独放大
    （那会抹掉停顿、诱发幻觉）。峰值超限时 ffmpeg 自动降低增益。"""
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    base = [ffmpeg, "-y", "-hide_banner", "-nostdin", "-i", str(src), "-vn", "-ac", "1"]
    measure = procs.run(
        base + ["-af", f"{HIGHPASS},loudnorm={LOUDNORM}:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    err = measure.stderr
    m = None
    try:
        m = json.loads(err[err.rfind("{"):err.rfind("}") + 1])
    except Exception:  # noqa: BLE001
        m = None
    if m and m.get("input_i") not in (None, "-inf") and float(m["input_i"]) > -70:
        # ffmpeg 的 linear 模式有两个前提，不满足会静默回退到动态模式：
        #  ① 目标 LRA ≥ 源 LRA（课堂录音有停顿，LRA 常 >11）→ 目标 LRA 取 max(11, 源 LRA+1)
        #  ② 加增益后真峰值 ≤ TP → 若会超，则把目标响度下调到刚好不超
        in_i, in_tp, in_lra = float(m["input_i"]), float(m["input_tp"]), float(m["input_lra"])
        target_i, tp = -16.0, -1.5
        if in_tp + (target_i - in_i) > tp - 0.2:
            target_i = round(tp - 0.2 - in_tp + in_i, 1)   # 留 0.2 dB 余量，否则四舍五入后超 0.01 dB 也会回退到动态
        lra = min(50, max(11, int(in_lra) + 2))
        af = (f"{HIGHPASS},loudnorm=I={target_i}:TP={tp}:LRA={lra}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
              f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    else:
        af = HIGHPASS   # 全程静音 / 测量失败：只做高通，不放大
    procs.run(base + ["-loglevel", "error", "-af", af, "-ar", str(sr), "-c:a", "pcm_s16le", str(dst)], check=True)


def preprocess(src: Path, dst: Path, normalize: bool = True) -> float:
    """转成 16 kHz 单声道 wav；normalize=True 时做高通 + 两遍线性响度归一化。返回时长（秒）。"""
    if normalize:
        normalize_audio(src, dst)
    else:
        procs.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(src),
                   "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)], check=True)
    with wave.open(str(dst), "rb") as w:
        return w.getnframes() / w.getframerate()


# ---------------------------------------------------------------------------
# 输出格式
# ---------------------------------------------------------------------------
def fmt_srt(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_hms(t: float) -> str:
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def write_srt(segments, path: Path) -> None:
    with path.open("w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            f.write(f"{i}\n{fmt_srt(seg['start'])} --> {fmt_srt(seg['end'])}\n{seg['text']}\n\n")


def merge_paragraphs(segments, gap: float = 1.8, max_chars: int = 700):
    """按停顿 / 长度合并成段落。停顿 > gap 秒或段落超长时切段。"""
    paras, cur, cur_start, prev_end = [], [], 0.0, None
    for seg in segments:
        if cur and (seg["start"] - prev_end > gap or sum(len(t) for t in cur) > max_chars):
            paras.append((cur_start, " ".join(cur)))
            cur = []
        if not cur:
            cur_start = seg["start"]
        cur.append(seg["text"])
        prev_end = seg["end"]
    if cur:
        paras.append((cur_start, " ".join(cur)))
    return paras


def fmt_notta(t: float) -> str:
    """Notta 导出的时间戳写法：<1h 用 MM:SS，否则 HH:MM:SS。"""
    t = int(t)
    if t < 3600:
        return f"{t // 60:02d}:{t % 60:02d}"
    return fmt_hms(t)


def write_txt(segments, path: Path) -> None:
    """Notta 兼容布局：时间戳单独一行，下一行文本，空行分隔。
    CityU vault 的 transcript_check.py / transcript-merge skill 按这个格式解析，直接可用。"""
    with path.open("w", encoding="utf-8") as f:
        for seg in segments:
            f.write(f"{fmt_notta(seg['start'])} \n{seg['text']} \n\n")


def write_md(paras, path: Path, title: str, meta: dict) -> None:
    lines = [f"# {title}", ""]
    lines += [f"- {k}: {v}" for k, v in meta.items()]
    lines += ["", "---", ""]
    for start, text in paras:
        lines.append(f"**[{fmt_hms(start)}]** {text}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
# 种子 prompt：一句带标点的"前文"，只给实时链路用（上下文为空或陷入无标点模式时垫入）。
# 最终版不用：实测让批处理丢词。它只是解码语境，不进转录；被模型"背"出来时 postfilter 按 SEED_PHRASES 删掉。
SEED_PROMPT = {"en": "Okay, let's continue with today's lecture.", "zh": "好，我们继续今天的课。"}
SEED_PHRASES = tuple(v.lower() for v in SEED_PROMPT.values()) + ("let's continue with today's lecture",)

COMMON_KW = dict(
    beam_size=5,
    vad_filter=True,
    vad_parameters={"min_silence_duration_ms": 500},
    condition_on_previous_text=False,  # 防止长音频里一句幻觉污染后续
)

# 批处理偶发"整块截断"：一块 ~30s 音频只吐出几个词就打 EOT。超过这个时长且词速低于阈值的段视为可疑，用顺序模式重转。
SUSPECT_MIN_SEC = 12.0
SUSPECT_WORDS_PER_SEC = 1.0


def load_model(model_name: str, device: str = "cpu", compute_type: str = "int8") -> BatchedInferencePipeline:
    """加载模型（首次约 5s，之后常驻显存 ~3GB）。GUI 里只调一次，复用返回值。"""
    return BatchedInferencePipeline(model=WhisperModel(model_name, device=device, compute_type=compute_type, local_files_only=True))


class Progress:
    """统一进度：frac 0–1、阶段名、服务端估算的剩余秒数。cb(frac, phase, eta) 可为 None。"""

    def __init__(self, cb):
        self.cb = cb

    def __call__(self, frac: float, phase: str, eta: float | None = None):
        if self.cb:
            self.cb(min(max(frac, 0.0), 1.0), phase, None if eta is None else max(0.0, eta))


# 各阶段在总进度里的占比（按实测耗时：主转录 ~85%，可疑段校验 ~12%，过滤 + 写出 ~3%）
PH_MAIN, PH_REDO, PH_FILTER = 0.85, 0.12, 0.03

# ETA 校准：记录上一次实测的速度（x 实时）做先验，与本次测量按"先验相当于 N 秒音频"的权重混合，
# 避免开头几秒的启动慢导致估算翻倍。文件在 library/_calibration.json。
CALIB_PATH = Path(os.environ.get("LT_CALIB", str(Path(__file__).resolve().parent / "library" / "_calibration.json")))
CALIB_DEFAULT = {"main": 22.0, "redo": 6.0, "tail": 0.6}   # tail = (校验 + 过滤耗时) / 主转录耗时
PRIOR_MAIN_SEC, PRIOR_REDO_SEC = 600.0, 60.0
DETECT_SEC, TAIL_SEC = 0.15, 5.0     # 每处语种检测约 0.15s；写出 + 收尾约 5s


def load_calib() -> dict:
    try:
        d = json.loads(CALIB_PATH.read_text(encoding="utf-8"))
        return {**CALIB_DEFAULT, **{k: float(v) for k, v in d.items()}}
    except Exception:  # noqa: BLE001
        return dict(CALIB_DEFAULT)


def save_calib(**measured) -> None:
    cal = load_calib()
    for k, v in measured.items():
        if v and v > 0:
            cal[k] = round(0.6 * cal[k] + 0.4 * v, 2)
    try:
        CALIB_PATH.parent.mkdir(parents=True, exist_ok=True)
        CALIB_PATH.write_text(json.dumps(cal), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _seg_dict(seg) -> dict:
    """faster-whisper 段 → dict。有词级时间戳时段起止取首末词（段级时间戳会跨过静音），并保留词表供高亮 / 后续整理。"""
    d = {"start": seg.start, "end": seg.end, "text": seg.text.strip(), "lp": float(seg.avg_logprob), "nsp": float(seg.no_speech_prob)}
    words = [w for w in (seg.words or []) if w.word.strip()]
    if words:
        d["start"], d["end"] = float(words[0].start), float(words[-1].end)
        d["words"] = [[round(float(w.start), 2), round(float(w.end), 2), w.word.strip()] for w in words]
    return d


WORD_GAP_SPLIT = 2.0   # 段内两个词之间的空隙 ≥ 此秒数就拆段（Whisper 常把停顿包在一段里，段级时间戳看不到）


def split_on_word_gaps(segments: list[dict], gap: float = WORD_GAP_SPLIT) -> list[dict]:
    out = []
    for s in segments:
        words = s.get("words")
        if not words or len(words) < 2:
            out.append(s)
            continue
        groups, cur = [], [words[0]]
        for w in words[1:]:
            if w[0] - cur[-1][1] >= gap:
                groups.append(cur)
                cur = []
            cur.append(w)
        groups.append(cur)
        if len(groups) == 1:
            out.append(s)
            continue
        for g in groups:
            out.append({**s, "start": g[0][0], "end": g[-1][1], "text": " ".join(w[2] for w in g), "words": g})
    return out


def speech_units(text: str) -> int:
    """CJK has no word spaces: count characters plus Latin words for density checks."""
    return len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]|[A-Za-z0-9]+(?:['-][A-Za-z0-9]+)*", text))


def _is_suspect(s: dict) -> bool:
    d = s["end"] - s["start"]
    return d >= SUSPECT_MIN_SEC and speech_units(s["text"]) / d < SUSPECT_WORDS_PER_SEC


HOTWORDS_MAX_TOKENS = 64   # 实测（AC6761 M03 整节课）：10 条/48 token 术语错 3 处、0 条错 8 处；50 条/223 token 反而变慢且词数下降


def fit_hotwords(model, hotwords: str | None, log=print, limit: int = HOTWORDS_MAX_TOKENS) -> str | None:
    """术语提示按真实 tokenizer 在短语边界截断到 limit 个 token（faster-whisper 自身上限 223，
    但长 prompt 会拖慢解码并诱发幻觉，所以默认只用前 ~12 条）。"""
    if not hotwords or not hotwords.strip():
        return None
    limit = min(limit, model.model.max_length // 2 - 1)
    tk = model.model.hf_tokenizer
    phrases = [h.strip() for h in hotwords.split(",") if h.strip()]
    kept = []
    for ph in phrases:
        cand = ", ".join(kept + [ph])
        if len(tk.encode(" " + cand).ids) > limit:
            break
        kept.append(ph)
    if len(kept) < len(phrases):
        log(f"    术语提示 {len(phrases)} 条超过 {limit} token 上限，只用前 {len(kept)} 条（被丢：{', '.join(phrases[len(kept):len(kept) + 3])}…）")
    return ", ".join(kept) or None


def run_transcribe(model, wav: Path, duration: float, batch: int, lang: str, hotwords, progress=None, log=print,
                   checkpoint=lambda: None, clock=time.monotonic):
    """progress(frac, phase, eta_sec)；log(str) 接收状态文本。"""
    prog = Progress(progress)
    cal = load_calib()
    hotwords = fit_hotwords(model, hotwords, log)
    t0 = clock()
    seg_iter, info = model.transcribe(
        str(wav),
        language=None if lang == "auto" else lang,
        batch_size=batch,
        without_timestamps=False,  # 带时间戳解码，避免批处理模式在块中途提前终止丢掉整块内容
        word_timestamps=True,      # 段的起止改取词的起止：Whisper 的段级时间戳会跨过静音，把停顿吞掉
        hotwords=hotwords,
        **COMMON_KW,   # 不用 initial_prompt：实测种子句让批处理丢 2% 词、术语变差（见 output/compare_AC6761_M03_seed.md）
    )
    lang = info.language if lang == "auto" else lang
    log(f"    识别语言: {lang}")
    segments = []
    susp_sec, susp_n, lowconf_n = 0.0, 0, 0
    t_first = None  # 批处理先对整个文件跑 VAD（2h 约 15s）才吐第一句，速率从第一句到达起算才准
    prog(0.0, "转录", duration / cal["main"] + TAIL_SEC)
    for seg in seg_iter:
        checkpoint()
        if t_first is None:
            t_first = clock()
        text = seg.text.strip()
        if text:
            s_ = _seg_dict(seg)
            segments.append(s_)
            if _is_suspect(s_):
                susp_sec += s_["end"] - s_["start"]
                susp_n += 1
            if s_["lp"] <= LP_SUSPECT:
                lowconf_n += 1
        done = min(seg.end, duration)
        el = clock() - t_first
        # 先验 + 实测混合的速度；并把"按目前可疑段比例预计的重转 / 过滤时间"提前算进 ETA
        speed = (done + PRIOR_MAIN_SEC) / (el + PRIOR_MAIN_SEC / cal["main"])
        scale = duration / max(done, 1.0)
        main_left = (duration - done) / speed
        tail_sampled = susp_n * scale * DETECT_SEC + 0.5 * susp_sec * scale / cal["redo"] + lowconf_n * scale * DETECT_SEC
        tail_prior = cal["tail"] * duration / speed
        w = min(1.0, done / max(duration, 1) * 2)  # 过半后完全信本文件的采样，之前混合上次的占比
        eta = main_left + (1 - w) * tail_prior + w * tail_sampled + TAIL_SEC
        prog(PH_MAIN * done / max(duration, 1), "转录", eta)
    main_el = clock() - (t_first or t0)
    segments = split_on_word_gaps(segments)

    # 兜底：可疑截断段（长而词少）。先语种检测：不是目标语言的（旁边同学闲聊）直接丢，不浪费时间重转
    suspects = [
        i for i, s in enumerate(segments)
        if _is_suspect(s)
    ]
    dropped: list[dict] = []
    if suspects:
        from faster_whisper.audio import decode_audio
        audio = decode_audio(str(wav), sampling_rate=16000)
        todo, skipped = [], 0
        susp_total = sum(segments[i]["end"] - segments[i]["start"] for i in suspects)
        for n, i in enumerate(suspects):
            checkpoint()
            s = segments[i]
            prog(PH_MAIN, f"语种检测 {n + 1}/{len(suspects)}", (len(suspects) - n) * DETECT_SEC + 0.7 * susp_total / cal["redo"] + TAIL_SEC)
            clip = audio[int(s["start"] * 16000):int(s["end"] * 16000)]
            try:
                detected, prob, _ = model.model.detect_language(clip)
            except Exception:  # noqa: BLE001
                detected, prob = lang, 1.0
            if detected != lang and prob >= LANG_GATE_PROB:
                dropped.append({**s, "reason": f"非{lang}语音({detected} {prob:.2f})"})
                skipped += 1
            else:
                todo.append(i)
        log(f"    可疑截断段 {len(suspects)} 处：{skipped} 处为非{lang}语音直接忽略，{len(todo)} 处重转")
        total_clip = sum(segments[i]["end"] - segments[i]["start"] for i in todo) or 1.0
        done_clip, t1 = 0.0, clock()
        redo_map: dict[int, list] = {}
        for n, i in enumerate(todo):
            checkpoint()
            s = segments[i]
            el = clock() - t1
            speed = (done_clip + PRIOR_REDO_SEC) / (el + PRIOR_REDO_SEC / cal["redo"])
            prog(PH_MAIN + PH_REDO * done_clip / total_clip, f"校验可疑段 {n + 1}/{len(todo)}", (total_clip - done_clip) / speed + TAIL_SEC)
            redo, _ = model.model.transcribe(
                str(wav),
                language=lang,
                clip_timestamps=f"{s['start']},{s['end']}",
                hotwords=hotwords,
                word_timestamps=True,
                **{**COMMON_KW, "beam_size": 3},  # 重转的段本就稀疏，beam 3 足够，比 5 快约 1/3
            )
            redo = split_on_word_gaps([_seg_dict(r) for r in redo if r.text.strip()])
            new_words = sum(speech_units(r["text"]) for r in redo)
            if new_words > speech_units(s["text"]):
                log(f"    [{fmt_hms(s['start'])}-{fmt_hms(s['end'])}] {len(s['text'].split())} 词 → {new_words} 词")
                redo_map[i] = redo
            done_clip += s["end"] - s["start"]
        redo_el = clock() - t1
        if done_clip >= 120 and redo_el > 10 and duration >= 1800:
            save_calib(redo=done_clip / redo_el)
        drop_idx = {i for i in suspects if i not in todo}
        fixed = []
        for i, s in enumerate(segments):
            if i in drop_idx:
                continue
            fixed.extend(redo_map.get(i, [s]))
        segments = fixed

    if duration >= 1800 and main_el > 5:  # 只用正常长度的课校准，短片段/静音片段速度没有代表性
        save_calib(main=duration / main_el)
    n_check = sum(1 for s_ in segments if s_["lp"] <= LP_SUSPECT) + count_sparse(segments)
    prog(PH_MAIN + PH_REDO, "过滤幻觉", n_check * DETECT_SEC + 3)
    t_tail0 = clock()
    segments, dropped2 = postfilter(segments, wav, model, lang, hotwords, log, progress, checkpoint, clock)
    dropped = sorted(dropped + dropped2, key=lambda d: d["start"])
    if duration >= 1800 and main_el > 5:
        save_calib(tail=(clock() - (t_first or t0) - main_el) / main_el)
    for s in segments + dropped:
        s.pop("lp", None)
        s.pop("nsp", None)
    info.dropped = dropped  # 附在 info 上，供 transcribe_file 写出
    return segments, info


# ---------------------------------------------------------------------------
# 后过滤：课堂录音里小测 / 自习 / 课间的幻觉
#   ① 术语提示词被原样复读   ② YouTube 式结尾语（"Thank you for watching"）
#   ③ 同一短句连续重复 ≥3 次   ④ 低置信窗做语种检测，不是目标语言（旁边同学的中文闲聊被"翻译"）就整窗丢弃
# ---------------------------------------------------------------------------
YT_PHRASES = ("thank you for watching", "thanks for watching", "please subscribe", "like, share", "subtitles by", "see you in the next video")
LP_SUSPECT = -0.40          # 批处理模式 avg_logprob 按 ~30s 窗共享；闲聊窗普遍 ≤ -0.43
LANG_GATE_PROB = 0.6        # 语种检测：非目标语言概率 ≥ 此值才丢（逐段判定）
LANG_GATE_RUN_PROB = 0.75   # 太短不能单独检测的段，沿用整窗判定，但要更高的概率
SPARSE_WIN, SPARSE_WORDS = 90.0, 40   # 稀疏区域：90s 窗内 < 40 词（正常讲课 120+ 词/分钟）
CJK = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


def _tokens(t: str) -> tuple:
    """小写、去标点、去复数 s，用于比较是否同一句 / 同一词。"""
    return tuple(w[:-1] if len(w) > 3 and w.endswith("s") else w for w in re.findall(r"[a-z0-9\-]+|[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", t.lower()))


def count_sparse(segments: list[dict]) -> int:
    """稀疏区域里会被逐段语种检测的段数（用于估算过滤阶段耗时）。"""
    import bisect
    starts = [s["start"] for s in segments]
    prefix = [0]
    for s in segments:
        prefix.append(prefix[-1] + speech_units(s["text"]))
    n = 0
    for k, s in enumerate(segments):
        a = bisect.bisect_left(starts, s["start"] - SPARSE_WIN / 2)
        b = bisect.bisect_right(starts, s["start"] + SPARSE_WIN / 2)
        if prefix[b] - prefix[a] < SPARSE_WORDS and s["end"] - s["start"] >= 1.5:
            n += 1
    return n


def _ngram_repeat(tk: tuple) -> bool:
    """段内某个 2–4 词短语重复 ≥2 次且覆盖 ≥60% 的词。"""
    for n in (4, 3, 2):
        if len(tk) < 2 * n:
            continue
        grams = [tk[i:i + n] for i in range(len(tk) - n + 1)]
        for g in set(grams):
            c = grams.count(g)
            if c >= 2 and c * n >= 0.6 * len(tk):
                return True
    return False


def postfilter(segments: list[dict], wav: Path, model, lang: str, hotwords, log=print,
               progress=None, checkpoint=lambda: None, clock=time.monotonic) -> tuple[list[dict], list[dict]]:
    """返回 (保留的段, 被丢弃的段[带 reason])。"""
    if not segments:
        return segments, []
    drop: dict[int, str] = {}

    # ① 提示词复读：模型在静音处把 prompt 原样吐出来——特征是术语**按列表顺序连续**出现（≥3 个），
    #    或整段几乎全由术语词表里的词组成（≥6 词且 ≥80%）。单纯命中几个术语不算（正常讲课一句话也会含多个术语）。
    if hotwords:
        phrases = [h.strip().lower() for h in hotwords.split(",") if h.strip()]
        norm = lambda t: re.sub(r"[^a-z0-9\u4e00-\u9fff ]+", " ", t.lower())
        norm_phr = [norm(ph).split() for ph in phrases if norm(ph).strip()]
        hot_vocab = set(w for ph in norm_phr for w in ph)
        for i, s in enumerate(segments):
            words = norm(s["text"]).split()
            leak = False
            for k in range(len(norm_phr) - 2):
                seq = norm_phr[k] + norm_phr[k + 1] + norm_phr[k + 2]
                if any(words[j:j + len(seq)] == seq for j in range(len(words) - len(seq) + 1)):
                    leak = True
                    break
            if leak or (len(words) >= 6 and sum(w in hot_vocab for w in words) / len(words) >= 0.8):
                drop[i] = "提示词复读"

    # ② YouTube 式幻觉 / 种子 prompt 被复读
    for i, s in enumerate(segments):
        t = s["text"].lower()
        if any(ph in t for ph in YT_PHRASES):
            drop[i] = "结尾语幻觉"
        elif any(ph in t for ph in SEED_PHRASES):
            drop[i] = "种子提示复读"

    # ③ 重复：同一短句连续 ≥3 段，或一段之内同一个词反复（"T-accounts, T-accounts, T-accounts"）
    i = 0
    while i < len(segments):
        j = i
        key = _tokens(segments[i]["text"])
        while j + 1 < len(segments) and _tokens(segments[j + 1]["text"]) == key:
            j += 1
        if j - i + 1 >= 3 and len(key) <= 10:
            for k in range(i, j + 1):
                drop[k] = "重复幻觉"
        i = j + 1
    for i, s in enumerate(segments):
        tk = _tokens(s["text"])
        if len(tk) >= 4 and len(set(tk)) <= 2:
            drop[i] = "重复幻觉"
        elif 4 <= len(tk) <= 16 and _ngram_repeat(tk):
            drop[i] = "重复幻觉"

    # ⑤ 低置信窗里孤立的极短句（"Thank you." / "Eighty-eight." / 外语碎片），前后 ≥8s 无语音
    for i, s in enumerate(segments):
        if i in drop or s["lp"] > LP_SUSPECT or len(_tokens(s["text"])) > 3:
            continue
        prev_end = segments[i - 1]["end"] if i > 0 else -1e9
        next_start = segments[i + 1]["start"] if i + 1 < len(segments) else 1e9
        if s["start"] - prev_end >= 8 and next_start - s["end"] >= 8:
            drop[i] = "孤立短句"

    # ④ 低置信窗 → 语种检测（逐段）；⑥ 稀疏区域（小测 / 自习：90s 窗内 < 40 词，正常讲课 120+ 词/分钟）→ 全部逐段检测
    runs, i = [], 0
    while i < len(segments):
        if segments[i]["lp"] <= LP_SUSPECT and i not in drop:
            j = i
            while j + 1 < len(segments) and segments[j + 1]["lp"] <= LP_SUSPECT and abs(segments[j + 1]["lp"] - segments[i]["lp"]) < 1e-6:
                j += 1
            runs.append((i, j))
            i = j + 1
        else:
            i += 1
    starts = [s["start"] for s in segments]
    wc = [speech_units(s["text"]) for s in segments]
    prefix = [0]
    for w in wc:
        prefix.append(prefix[-1] + w)
    import bisect

    def words_around(k: int) -> int:
        a = bisect.bisect_left(starts, segments[k]["start"] - SPARSE_WIN / 2)
        b = bisect.bisect_right(starts, segments[k]["start"] + SPARSE_WIN / 2)
        return prefix[b] - prefix[a]

    sparse = [k for k in range(len(segments)) if k not in drop and words_around(k) < SPARSE_WORDS]
    need_audio = bool(runs) or bool(sparse)
    audio = None
    if need_audio:
        from faster_whisper.audio import decode_audio
        audio = decode_audio(str(wav), sampling_rate=16000)

    # Count the actual distinct segment/window checks, including whole-window checks.
    candidates = {k for i, j in runs if segments[j]["end"] - segments[i]["start"] >= 2.0
                  for k in range(i, j + 1) if k not in drop and segments[k]["end"] - segments[k]["start"] >= 1.5}
    candidates.update(k for k in sparse if segments[k]["end"] - segments[k]["start"] >= 1.5)
    total = len(candidates) + sum(segments[j]["end"] - segments[i]["start"] >= 2.0 for i, j in runs)
    done, started = 0, clock()
    prog = Progress(progress)
    prog(PH_MAIN + PH_REDO, f"过滤校验 0/{total}", None)
    log(f"    过滤校验：{total} 次语种检测")

    def detect(a: float, b: float):
        nonlocal done
        checkpoint()
        try:
            d, p, _ = model.model.detect_language(audio[int(a * 16000):int(b * 16000)])
            return d, p
        except Exception:  # noqa: BLE001
            return lang, 1.0
        finally:
            done += 1
            elapsed = clock() - started
            eta = elapsed / done * max(0, total - done) + 3
            prog(PH_MAIN + PH_REDO + PH_FILTER * 0.65 * done / max(total, 1), f"过滤校验 {done}/{total}", eta)

    checked: set[int] = set()

    def gate_segment(k: int, tag: str) -> None:
        if k in drop or k in checked:
            return
        s = segments[k]
        if s["end"] - s["start"] < 1.5:
            return
        checked.add(k)
        d, p = detect(max(0.0, s["start"] - 0.2), s["end"] + 0.2)
        if d != lang and p >= LANG_GATE_PROB:
            drop[k] = f"非{lang}语音({d} {p:.2f}{tag})"

    for i, j in runs:
        a, b = segments[i]["start"], segments[j]["end"]
        if b - a < 2.0:
            continue
        run_lang, run_prob = detect(a, b)
        for k in range(i, j + 1):
            s = segments[k]
            if s["end"] - s["start"] >= 1.5:
                gate_segment(k, "")
            elif run_lang != lang and run_prob >= LANG_GATE_RUN_PROB:
                drop.setdefault(k, f"非{lang}语音({run_lang} {run_prob:.2f} 整窗)")
    for k in sparse:
        gate_segment(k, " 稀疏区")

    # ⑦ 目标语言是英语却输出了中日韩字符 → 旁边的中文闲聊
    if lang == "en":
        for k, s in enumerate(segments):
            if k not in drop and CJK.search(s["text"]):
                drop[k] = "非en语音(含中文字符)"

    if drop:
        by_reason = {}
        for k, r in drop.items():
            by_reason.setdefault(r.split("(")[0], []).append(k)
        for r, ks in by_reason.items():
            spans = f"{fmt_hms(segments[ks[0]]['start'])}…{fmt_hms(segments[ks[-1]]['end'])}"
            log(f"    过滤 {len(ks)} 段（{r}，{spans}）")
    keep = [s for i, s in enumerate(segments) if i not in drop]
    gone = [{**segments[i], "reason": r} for i, r in sorted(drop.items())]
    return keep, gone


def transcribe_file(
    src: Path,
    model,
    *,
    out_dir: Path | None = None,
    lang: str = "en",
    batch: int = 8,
    hotwords: str | None = None,
    normalize: bool = True,
    keep_wav: bool = False,
    model_name: str = "large-v3",
    stem: str | None = None,
    title: str | None = None,
    progress=None,
    log=print,
    checkpoint=lambda: None,
    clock=time.monotonic,
) -> dict:
    """转录一个文件，写出 .srt/.txt/.md（+ <stem>.dropped.json 被过滤的噪声段）。
    返回 {"srt","txt","md","segments","paras","dropped","duration","elapsed"}。
    GUI 调用：传 progress(frac, phase, eta_sec) 更新进度条、log(str) 追加日志，不要依赖 stdout。"""
    src = Path(src)
    stem = stem or src.stem  # 输出文件名（不含扩展名），默认与输入同名
    out_dir = Path(out_dir) if out_dir else src.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    wav = out_dir / f"{stem}.16k.wav"

    t0 = clock()
    log(f"=== {src.name}")
    log("[1/3] 预处理（响度归一化）...")
    checkpoint()
    duration = preprocess(src, wav, normalize=normalize)
    log(f"    时长 {fmt_hms(duration)}  ({clock() - t0:.0f}s)")

    while True:
        log(f"[2/3] 转录（batch={batch}）...")
        try:
            checkpoint()
            segments, info = run_transcribe(model, wav, duration, batch, lang, hotwords, progress, log, checkpoint, clock)
            break
        except RuntimeError as e:
            if "out of memory" not in str(e).lower() or batch <= 1:
                raise
            batch //= 2
            log(f"    显存不足，降到 batch={batch} 重试")

    log("[3/3] 写出...")
    checkpoint()
    Progress(progress)(PH_MAIN + PH_REDO + PH_FILTER * 0.7, "写出", 1)
    paras = merge_paragraphs(segments)
    paths = {k: out_dir / f"{stem}.{k}" for k in ("srt", "txt", "md")}
    dropped = getattr(info, "dropped", [])
    (out_dir / f"{stem}.dropped.json").write_text(json.dumps(dropped, ensure_ascii=False, indent=1), encoding="utf-8")
    write_srt(segments, paths["srt"])
    write_txt(segments, paths["txt"])
    write_md(paras, paths["md"], title=title or stem,
             meta={"source": src.name, "duration": fmt_hms(duration), "model": model_name, "language": info.language})
    if not keep_wav:
        wav.unlink(missing_ok=True)

    elapsed = clock() - t0
    Progress(progress)(1.0, "完成", 0)
    log(f"完成：{len(segments)} 句 / {len(paras)} 段，过滤噪声 {len(dropped)} 段，用时 {elapsed:.0f}s（{duration / max(elapsed, 1):.1f}x 实时）")
    log(f"输出：{paths['md']}")
    return {**paths, "segments": segments, "paras": paras, "dropped": dropped, "duration": duration, "elapsed": elapsed, "language": info.language}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("inputs", nargs="+", help="音频 / 视频文件")
    p.add_argument("--model", default="asr-turbo", choices=["asr-turbo", "asr-large"], help="已主动安装或引用的转录模型")
    p.add_argument("--lang", default="auto", help="auto 自动检测；zh 中文；en 英语")
    p.add_argument("--out", help="输出目录（默认与输入同目录）")
    p.add_argument("--batch", type=int, default=8, help="批大小（8GB 显存用 8；显存不足会自动减半重试）")
    p.add_argument("--hotwords", help="课程术语提示，如 'Bayesian, MCMC, Gibbs sampling'，提高专有名词识别")
    p.add_argument("--no-norm", action="store_true", help="跳过响度归一化")
    p.add_argument("--keep-wav", action="store_true", help="保留预处理后的 wav")
    p.add_argument("--cpu", action="store_true", help="强制用 CPU（int8）")
    args = p.parse_args()

    files = [Path(x) for x in args.inputs]
    missing = [f for f in files if not f.exists()]
    if missing:
        sys.exit(f"找不到文件：{missing}")

    if args.cpu:
        os.environ["LT_DEVICE"] = "cpu"
    from ltapp.engine import ModelHolder
    holder = ModelHolder()
    print(f"加载已安装模型 {args.model}...", end=" ", flush=True)
    t0 = time.time()
    if not holder.ensure_main(args.model):
        sys.exit(holder.error)
    model = holder.model
    print(f"{time.time() - t0:.0f}s")

    for f in files:
        bar = tqdm(total=100, bar_format="{desc} {percentage:3.0f}%|{bar}| [{elapsed}{postfix}]")

        def progress(frac, phase, eta, bar=bar):
            bar.set_description_str(phase)
            bar.set_postfix_str("" if eta is None else f"<{fmt_hms(eta)}")
            bar.update(max(0, int(frac * 100) - bar.n))

        def log(msg, bar=bar):
            bar.write(msg)

        try:
            transcribe_file(
                f, model, out_dir=args.out, lang=args.lang, batch=args.batch, hotwords=args.hotwords,
                normalize=not args.no_norm, keep_wav=args.keep_wav, model_name=args.model,
                progress=progress, log=log,
            )
        finally:
            bar.close()


if __name__ == "__main__":
    main()
