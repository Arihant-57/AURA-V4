"""Unit tests for the free Jarvis & Megatron DSP voice engine."""

from __future__ import annotations

import os
from pathlib import Path
import wave

import numpy as np
import pytest

from src.aura.core.voice import (
    VoiceEngine,
    _clean_speech_text,
    apply_megatron_dsp,
    synthesize_wav,
)


def test_clean_speech_text_strips_code_and_markdown():
    raw = "## Summary\nSee [docs](https://example.com) and run `pytest`:\n```python\nprint(1)\n```\nDone!"
    cleaned = _clean_speech_text(raw)
    assert "```" not in cleaned
    assert "https://example.com" not in cleaned
    assert "docs" in cleaned
    assert "Done!" in cleaned


def test_apply_megatron_dsp_transforms_and_normalizes():
    sr = 24000
    t = np.linspace(0, 0.25, int(sr * 0.25), endpoint=False)
    tone = (np.sin(2.0 * np.pi * 140.0 * t) * 18000).astype(np.int16)
    stereo = np.column_stack([tone, tone])

    out = apply_megatron_dsp(stereo, sr)
    assert out.dtype == np.int16
    assert out.ndim == 1
    assert out.size == tone.size
    assert int(np.max(np.abs(out))) <= 32767
    assert int(np.max(np.abs(out))) > 5000

    # Silence produces zeros safely without NaN
    silent = apply_megatron_dsp(np.zeros(400, dtype=np.int16), sr)
    assert silent.size == 400
    assert int(np.max(np.abs(silent))) == 0


def test_voice_config_persistence_and_offline_wav_synthesis(tmp_path: Path):
    ve = VoiceEngine(storage_dir=tmp_path)
    assert ve.get_config().persona == "off"

    updated = ve.update_config(persona="megatron", auto_speak=True, prefer_offline=True)
    assert updated.persona == "megatron"
    assert updated.auto_speak is True

    reloaded = VoiceEngine(storage_dir=tmp_path)
    assert reloaded.get_config().persona == "megatron"
    assert reloaded.get_config().prefer_offline is True

    if os.name != "nt":
        pytest.skip("Windows SAPI WAV synthesis test")

    out_wav = tmp_path / "megatron_test.wav"
    produced = synthesize_wav("All hail Megatron.", persona="megatron", output_path=out_wav, prefer_offline=True)
    assert produced == out_wav
    assert out_wav.stat().st_size > 1000
    with wave.open(str(out_wav), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getnframes() > 0


def test_voice_engine_stop_interrupts_playback(tmp_path: Path):
    from unittest.mock import patch

    ve = VoiceEngine(storage_dir=tmp_path)
    ve.update_config(persona="jarvis", prefer_offline=True)
    wav_path = tmp_path / "dummy.wav"
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 16000)  # 1.0s

    before = ve._stop_seq
    with patch("src.aura.core.voice.synthesize_wav", return_value=wav_path):
        ve.speak_async("Testing interruption", persona="jarvis")
        ve.stop()
        assert ve._stop_seq >= before + 2
        assert ve.is_speaking is False


def test_apply_ultron_dsp_and_config_synthesis(tmp_path: Path) -> None:
    import numpy as np
    from unittest.mock import patch
    from src.aura.core.voice import VoiceEngine, apply_ultron_dsp, synthesize_wav

    sr = 24000
    t = np.linspace(0, 0.25, int(sr * 0.25), endpoint=False)
    samples = (np.sin(2 * np.pi * 160 * t) * 16000).astype(np.int16)
    processed = apply_ultron_dsp(samples, sr)
    assert processed.shape == samples.shape
    assert processed.dtype == np.int16
    assert not np.array_equal(processed, samples)

    engine = VoiceEngine(storage_dir=tmp_path)
    cfg = engine.update_config(persona="ultron", auto_speak=True, prefer_offline=True)
    assert cfg.persona == "ultron"
    assert engine.get_config().persona == "ultron"

    def _fake_sapi(_text: str, out_wav: Path, rate: int = 0) -> None:
        import wave

        out_wav.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out_wav), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(22050)
            wf.writeframes(samples.tobytes())

    out_path = tmp_path / "ultron.wav"
    with patch("src.aura.core.voice._synthesize_sapi_wav", side_effect=_fake_sapi):
        res = synthesize_wav("All strings removed.", persona="ultron", output_path=out_path, prefer_offline=True)
    assert res == out_path
    assert out_path.exists() and out_path.stat().st_size > 44


def test_wav_rms_envelope_and_playback_level_tracking(tmp_path: Path):
    import threading
    import time
    from src.aura.core.voice import _compute_wav_rms_envelope

    wav_path = tmp_path / "tone.wav"
    sr = 16000
    t = np.linspace(0, 0.4, int(sr * 0.4), endpoint=False)
    loud = (np.sin(2.0 * np.pi * 220.0 * t) * 16000).astype(np.int16)
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(loud.tobytes())

    env = _compute_wav_rms_envelope(wav_path, block_ms=100)
    assert len(env) == 4
    assert all(0.25 < v < 0.5 for v in env)
    assert _compute_wav_rms_envelope(tmp_path / "missing.wav") == []

    ve = VoiceEngine(storage_dir=tmp_path)
    stop_evt = threading.Event()
    with ve._state_lock:
        ve._speaking = True
        ve._actively_playing = True
        ve._playback_started_at = time.monotonic() - 0.12
        ve._playback_envelope = env
        ve._playback_stop_event = stop_evt

    assert ve.is_actively_playing is True
    assert ve.current_playback_level() > 0.25
    ve.stop()
    assert stop_evt.is_set() is True
    assert ve.is_actively_playing is False
    assert ve.current_playback_level() == 0.0
