import unittest
import os
from types import SimpleNamespace
from unittest.mock import patch

from ltapp.engine import ModelHolder
from ltapp.metal import word_segments


class AdapterTests(unittest.TestCase):
    def test_word_timestamps_are_not_replaced_by_uniform_guesses(self):
        parts = [SimpleNamespace(t0=10, t1=27, text="Hello", probability=.9),
                 SimpleNamespace(t0=31, t1=105, text="world.", probability=.8)]
        segments = word_segments(parts, offset=8)
        self.assertEqual(segments[0].text, "Hello world.")
        self.assertEqual(segments[0].words[0].start, 8.1)
        self.assertEqual(segments[0].words[0].end, 8.27)
        self.assertEqual(segments[0].words[1].start, 8.31)
        self.assertAlmostEqual(segments[0].end, 9.05)

    def test_pause_splits_segment_and_preserves_cjk_text(self):
        parts = [SimpleNamespace(t0=0,t1=40,text="今天",probability=.9),
                 SimpleNamespace(t0=40,t1=80,text="讨论。",probability=.9),
                 SimpleNamespace(t0=500,t1=600,text="Next.",probability=.9)]
        segments = word_segments(parts)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0].text, "今天讨论。")
        self.assertEqual(segments[1].start, 5)

    def test_turbo_loader_shares_live_and_final_instance_and_loads_local_only(self):
        from pathlib import Path
        store = SimpleNamespace(choose_asr=lambda: "asr-turbo", path=lambda _: Path("local-model"),
                                spec=lambda _: {"backend":"ctranslate2"})
        native = SimpleNamespace(model=object())
        holder = ModelHolder(store)
        with patch("transcribe.load_model", return_value=native) as load:
            holder.ensure_live("asr-turbo")
            holder.ensure_main("asr-turbo")
        self.assertEqual(load.call_count, 1)
        self.assertIs(holder.live_model, holder.model.model)

    def test_cpu_mode_works_even_if_cuda_support_is_unavailable(self):
        from pathlib import Path
        store = SimpleNamespace(choose_asr=lambda: "asr-turbo", path=lambda _: Path("local-model"),
                                spec=lambda _: {"backend":"ctranslate2"})
        holder = ModelHolder(store)
        with patch.dict(os.environ, {"LT_DEVICE":"cpu"}), \
             patch("ctranslate2.get_cuda_device_count", side_effect=RuntimeError("not compiled with CUDA")), \
             patch("transcribe.load_model", return_value=SimpleNamespace(model=object())):
            self.assertTrue(holder.ensure_main("asr-turbo"))
        self.assertEqual(holder.device, "cpu")
