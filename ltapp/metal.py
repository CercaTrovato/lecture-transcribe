"""whisper.cpp adapter exposing the same segment/word contract as CTranslate2.

The native model stays resident. Packaging supplies a Metal-enabled pywhispercpp
wheel on macOS; native hardware acceptance is required before release.
"""
from __future__ import annotations

import math
import os
import platform
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import soundfile as sf


def word_segments(parts, offset=0.0):
    """Group actual word-timestamp segments into sentences for shared filtering."""
    out, words, probabilities = [], [], []
    def flush():
        if not words:
            return
        text = " ".join(w.word.strip() for w in words)
        text = re.sub(r"(?<=[\u4e00-\u9fff]) (?=[\u4e00-\u9fff，。！？])", "", text)
        out.append(SimpleNamespace(start=words[0].start, end=words[-1].end, text=text, words=list(words),
                                   avg_logprob=sum(probabilities) / max(1, len(probabilities)), no_speech_prob=0.0))
        words.clear(); probabilities.clear()
    for part in parts:
        start, end = offset + part.t0 / 100, offset + part.t1 / 100
        if words and (start - words[-1].end > 1.8 or end - words[0].start > 20):
            flush()
        text = part.text.strip()
        if not text:
            continue
        words.append(SimpleNamespace(start=start, end=end, word=" " + text))
        probability = getattr(part, "probability", 1.0)
        probabilities.append(math.log(max(1e-6, probability)) if math.isfinite(probability) else -0.5)
        if re.search(r"[.!?。！？]$", text):
            flush()
    flush()
    return out


class MetalModel:
    max_length = 448
    def __init__(self, path: Path):
        from pywhispercpp.model import Model
        if not path.is_file():
            raise FileNotFoundError("模型文件不存在；推理过程不会自动下载模型")
        self.device = "metal" if os.environ.get("LT_DEVICE") == "metal" or (os.environ.get("LT_DEVICE", "auto") == "auto" and platform.machine() == "arm64") else "cpu"
        self.native = Model(str(path), params_sampling_strategy=1, context_params={"use_gpu": self.device == "metal"},
                            print_realtime=False, print_progress=False, print_timestamps=False)
        self.model = self
        # Conservative byte-token upper bound avoids exceeding the common prompt budget.
        self.hf_tokenizer = SimpleNamespace(encode=lambda text: SimpleNamespace(ids=list(text.encode("utf-8"))))
        self.lock = threading.RLock()

    def detect_language(self, audio):
        with self.lock:
            (language, probability), probabilities = self.native.auto_detect_language(audio)
            return language, float(probability), list(probabilities.items())

    def transcribe(self, audio, language=None, hotwords=None, initial_prompt=None, clip_timestamps=None,
                   word_timestamps=True, **kwargs):
        if isinstance(audio, (str, Path)):
            audio, rate = sf.read(str(audio), dtype="float32")
            if rate != 16000:
                raise ValueError("推理适配器只接受已归一化的 16 kHz 音频")
        audio = np.asarray(audio, dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        offset = 0.0
        if clip_timestamps:
            bounds = [float(value) for value in str(clip_timestamps).split(",")]
            offset = bounds[0]
            audio = audio[int(bounds[0]*16000):int(bounds[1]*16000)]
        if language is None:
            language, _, _ = self.detect_language(audio[:30 * 16000])
        spans = [{"start": 0, "end": len(audio)}]
        if kwargs.get("vad_filter"):
            from faster_whisper.vad import get_speech_timestamps
            spans = get_speech_timestamps(audio, **kwargs.get("vad_parameters", {}))
        chunks = [(start, min(span["end"], start + 30 * 16000)) for span in spans
                  for start in range(span["start"], span["end"], 30 * 16000)]
        def decode():
            for start, end in chunks:
                with self.lock:
                    prompt = " ".join(value for value in (initial_prompt, hotwords) if value) or None
                    parts = self.native.transcribe(audio[start:end], language=language, initial_prompt=prompt, no_context=True,
                                                   token_timestamps=True, max_len=1, extract_probability=True,
                                                   beam_search={"beam_size": kwargs.get("beam_size", 1), "patience": 1.0})
                    segments = word_segments(parts, offset + start / 16000)
                yield from segments
        return decode(), SimpleNamespace(language=language, dropped=[])
