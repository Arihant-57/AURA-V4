"""
Free Zero-Cost Voice Engine for AURA V4 (Jarvis, Ultron & Megatron DSP).

Zero paid subscriptions (no ElevenLabs key required):
- Online primary: Microsoft Neural Voices via free `edge-tts`
  - `jarvis`: refined British assistant (`en-GB-RyanNeural`)
  - `ultron`: calm, resonant synthetic baritone (`en-US-AndrewNeural`, pitch -16Hz, rate -7%)
    piped through a pure-NumPy Ultron metallic-waveguide DSP chain
  - `megatron`: deep base (`en-US-ChristopherNeural`, pitch -22Hz, rate -10%)
    piped through a pure-NumPy Cybertronian Megatron DSP chain.
- Offline fallback: Built-in Windows SAPI (`System.Speech`) writing native PCM WAV,
  with the same DSP chains applied for `ultron` and `megatron`.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from typing import Literal
import wave

import numpy as np

VoicePersona = Literal["off", "jarvis", "ultron", "megatron"]

_DEFAULT_EDGE_VOICES: dict[str, tuple[str, str, str]] = {
    # persona -> (voice_name, pitch, rate)
    "jarvis": ("en-GB-RyanNeural", "-4Hz", "+4%"),
    "ultron": ("en-US-AndrewNeural", "-16Hz", "-7%"),
    "megatron": ("en-US-ChristopherNeural", "-22Hz", "-10%"),
}

_ACTIVE_PERSONAS = ("jarvis", "ultron", "megatron")
_ALL_PERSONAS = ("off", "jarvis", "ultron", "megatron")

_MIXER_LOCK = threading.Lock()


def _clean_speech_text(text: str, max_chars: int = 550) -> str:
    """Strip markdown formatting and code blocks so TTS reads crisp natural phrasing."""
    cleaned = re.sub(r"```[\s\S]*?```", " code block omitted. ", text)
    cleaned = re.sub(r"`([^`]+)`", r"\1", cleaned)
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)
    cleaned = re.sub(r"[*_#>-]+", " ", cleaned)
    cleaned = " ".join(cleaned.split()).strip()
    if len(cleaned) > max_chars:
        cut = cleaned[:max_chars]
        last_dot = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        cleaned = cut[: last_dot + 1] if last_dot > 120 else (cut.rstrip() + "...")
    return cleaned


def _to_mono_normalized(samples: np.ndarray) -> np.ndarray | None:
    """Mix stereo PCM down to mono float32 normalized to peak=1.0, or None for silence."""
    arr = np.asarray(samples, dtype=np.float32)
    if arr.ndim == 2:
        arr = arr.mean(axis=1)
    if arr.size == 0:
        return None
    peak = float(np.max(np.abs(arr)))
    if peak < 1e-6:
        return None
    return arr / peak


def _to_int16_pcm(signal: np.ndarray, ceiling: float = 0.92) -> np.ndarray:
    """Normalize a float32 processed buffer back into safe non-clipping int16 PCM."""
    out_peak = float(np.max(np.abs(signal)))
    if out_peak > 1e-6:
        signal = (signal / out_peak) * ceiling
    return (np.clip(signal, -1.0, 1.0) * 32767.0).astype(np.int16)


def apply_megatron_dsp(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """
    Transform clean mono/stereo int16 PCM samples into a Cybertronian Megatron voice:
    comb metallic resonance + 46 Hz ring modulation + sub-harmonic warmth + tanh grit.
    """
    x = _to_mono_normalized(samples)
    if x is None:
        return np.zeros(np.asarray(samples).shape[0] if np.asarray(samples).ndim > 0 else 0, dtype=np.int16)

    # 1. Warm speech shaping (gentle high cut around 4.2 kHz via pure-NumPy FIR)
    if sample_rate > 9000 and x.size >= 9:
        fc = min(0.45, 4200.0 / float(sample_rate))
        n = np.arange(-4, 5, dtype=np.float32)
        h = (np.sinc(2.0 * fc * n) * np.hanning(9)).astype(np.float32)
        h_sum = float(np.sum(h))
        if h_sum > 1e-6:
            h /= h_sum
            x = np.convolve(x, h, mode="same").astype(np.float32)

    # 2. Cybertronian metallic double comb / flange resonance (7.5 ms + 16 ms)
    d1 = max(1, int(sample_rate * 0.0075))
    d2 = max(2, int(sample_rate * 0.016))
    comb = np.copy(x)
    if x.size > d1:
        comb[d1:] += 0.34 * x[:-d1]
    if x.size > d2:
        comb[d2:] -= 0.22 * x[:-d2]

    # 3. 46 Hz Decepticon ring-modulation blend (keep 62% dry body for sharp intelligibility)
    t = np.arange(comb.size, dtype=np.float32) / float(max(8000, sample_rate))
    carrier = np.sin(2.0 * np.pi * 46.0 * t).astype(np.float32)
    ring_layer = comb * carrier
    voiced = 0.62 * comb + 0.38 * ring_layer

    # 4. Subtle octave sub-harmonic growl + soft tanh saturation
    sub_harmonic = np.sign(x) * (x * x)
    mixed = 0.85 * voiced + 0.28 * sub_harmonic
    driven = np.tanh(mixed * 1.45)
    return _to_int16_pcm(driven)


def apply_ultron_dsp(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """
    Transform clean mono/stereo int16 PCM samples into an Ultron sentient-AI voice:
    crisp titanium waveguide comb (4.2 ms + 11.5 ms) + subtle 18 Hz harmonic shimmer.
    """
    x = _to_mono_normalized(samples)
    if x is None:
        return np.zeros(np.asarray(samples).shape[0] if np.asarray(samples).ndim > 0 else 0, dtype=np.int16)

    # 1. Tight metallic waveguide taps for cold chrome AI resonance without muddying consonants
    d1 = max(1, int(sample_rate * 0.0042))
    d2 = max(2, int(sample_rate * 0.0115))
    comb = np.copy(x)
    if x.size > d1:
        comb[d1:] += 0.26 * x[:-d1]
    if x.size > d2:
        comb[d2:] -= 0.16 * x[:-d2]

    # 2. Subtle 18 Hz synthetic shimmer + controlled odd-harmonic presence
    t = np.arange(comb.size, dtype=np.float32) / float(max(8000, sample_rate))
    shimmer = np.sin(2.0 * np.pi * 18.0 * t).astype(np.float32)
    blended = 0.84 * comb + 0.16 * (comb * shimmer)
    driven = 0.72 * blended + 0.28 * np.tanh(blended * 1.35)
    return _to_int16_pcm(driven)


def _write_mono_wav(path: Path, pcm_int16: np.ndarray, sample_rate: int) -> Path:
    mono = np.asarray(pcm_int16, dtype=np.int16)
    if mono.ndim == 2:
        mono = mono.mean(axis=1).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(mono.tobytes())
    return path


def _read_wav_pcm(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())
    if sw != 2:
        raise ValueError(f"Unsupported WAV sample width: {sw}")
    arr = np.frombuffer(frames, dtype=np.int16)
    if nch > 1:
        arr = arr.reshape(-1, nch).mean(axis=1).astype(np.int16)
    return arr, sr


def _synthesize_edge_to_pcm(text: str, persona: str) -> tuple[np.ndarray, int]:
    import edge_tts
    import pygame

    voice, pitch, rate = _DEFAULT_EDGE_VOICES.get(persona, _DEFAULT_EDGE_VOICES["jarvis"])
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    mp3_path = Path(tmp.name)
    tmp.close()

    async def _gen() -> None:
        comm = edge_tts.Communicate(text, voice, pitch=pitch, rate=rate)
        await comm.save(str(mp3_path))

    try:
        try:
            asyncio.run(_gen())
        except RuntimeError:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_gen())
            finally:
                loop.close()

        with _MIXER_LOCK:
            sr = 24000
            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=sr, size=-16, channels=1)
            else:
                sr = int(pygame.mixer.get_init()[0])
            snd = pygame.mixer.Sound(str(mp3_path))
            arr = pygame.sndarray.array(snd).copy()
            if arr.ndim == 2:
                arr = arr.mean(axis=1).astype(np.int16)
            return arr, sr
    finally:
        try:
            mp3_path.unlink(missing_ok=True)
        except OSError:
            pass


def _synthesize_sapi_wav(text: str, out_wav: Path, rate: int = 0) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Windows SAPI TTS is supported only on Windows.")
    escaped = text.replace("'", "''")
    ps = f"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.Rate = {int(max(-8, min(8, rate)))}
$s.SetOutputToWaveFile('{str(out_wav)}')
$s.Speak('{escaped}')
$s.Dispose()
"""
    enc = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
    from .mcp_hub import win32_subprocess_kwargs

    extra = win32_subprocess_kwargs(hide_window=True)
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
        **extra,
    )
    if proc.returncode != 0 or not out_wav.is_file() or out_wav.stat().st_size <= 44:
        raise RuntimeError(proc.stderr.strip() or "Windows SAPI synthesis failed.")


def synthesize_wav(
    text: str,
    persona: VoicePersona = "jarvis",
    output_path: Path | None = None,
    prefer_offline: bool = False,
) -> Path:
    """Synthesize spoken text into a WAV file (Jarvis, Ultron, or Megatron DSP, 100% free)."""
    clean = _clean_speech_text(text)
    if not clean:
        raise ValueError("Speech text cannot be empty.")

    dest = output_path
    if dest is None:
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        dest = Path(tmp.name)
        tmp.close()

    mode = persona if persona in _ACTIVE_PERSONAS else "jarvis"
    pcm: np.ndarray | None = None
    sr = 24000

    if not prefer_offline:
        try:
            pcm, sr = _synthesize_edge_to_pcm(clean, mode)
        except Exception:
            pcm = None

    if pcm is None:
        sapi_rate = -2 if mode == "megatron" else (-1 if mode == "ultron" else 1)
        _synthesize_sapi_wav(clean, dest, rate=sapi_rate)
        pcm, sr = _read_wav_pcm(dest)

    if mode == "megatron":
        pcm = apply_megatron_dsp(pcm, sr)
    elif mode == "ultron":
        pcm = apply_ultron_dsp(pcm, sr)

    return _write_mono_wav(dest, pcm, sr)


def _compute_wav_rms_envelope(path: Path, block_ms: int = 100) -> list[float]:
    """Return normalized [0.0, 1.0] per-block RMS energy envelope for echo-guard tracking."""
    try:
        pcm, sr = _read_wav_pcm(path)
    except Exception:
        return []
    if pcm.size == 0 or sr <= 0:
        return []
    block_samples = max(1, int(sr * max(20, block_ms) / 1000.0))
    floats = pcm.astype(np.float32) / 32768.0
    env: list[float] = []
    for start in range(0, floats.size, block_samples):
        seg = floats[start : start + block_samples]
        if seg.size:
            env.append(float(np.sqrt(np.mean(seg * seg))))
    return env


@dataclass
class VoiceConfig:
    persona: VoicePersona = "off"
    auto_speak: bool = False
    prefer_offline: bool = False


class VoiceEngine:
    """Persists free voice settings and manages non-blocking speaker playback."""

    def __init__(self, storage_dir: Path | None = None) -> None:
        if storage_dir is None:
            from ..config.settings import get_default_storage_dir
            storage_dir = get_default_storage_dir()
        self.storage_dir = Path(storage_dir)
        self.config_path = self.storage_dir / "voice_config.json"
        self._play_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._speaking = False
        self._actively_playing = False
        self._stop_seq = 0
        self._playback_started_at = 0.0
        self._playback_envelope: list[float] = []
        self._playback_block_s = 0.1
        self._playback_stop_event: threading.Event | None = None
        self._config = self._load()

    @property
    def is_speaking(self) -> bool:
        return self._speaking

    @property
    def is_actively_playing(self) -> bool:
        return self._actively_playing and self._speaking

    def current_playback_level(self) -> float:
        """Return normalized [0.0, 1.0] speaker playback RMS at the current instant (with 100ms reverb tail)."""
        with self._state_lock:
            if not self._actively_playing or not self._speaking:
                return 0.0
            if not self._playback_envelope:
                return 0.22
            elapsed = max(0.0, time.monotonic() - self._playback_started_at)
            idx = int(elapsed / self._playback_block_s)
            if idx >= len(self._playback_envelope):
                return 0.0
            lo = max(0, idx - 1)
            return float(max(self._playback_envelope[lo : idx + 1]))

    def _load(self) -> VoiceConfig:
        if not self.config_path.is_file():
            return VoiceConfig()
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            persona = raw.get("persona", "off")
            if persona not in _ALL_PERSONAS:
                persona = "off"
            return VoiceConfig(
                persona=persona,
                auto_speak=bool(raw.get("auto_speak", False)),
                prefer_offline=bool(raw.get("prefer_offline", False)),
            )
        except Exception:
            return VoiceConfig()

    def get_config(self) -> VoiceConfig:
        return VoiceConfig(
            persona=self._config.persona,
            auto_speak=self._config.auto_speak,
            prefer_offline=self._config.prefer_offline,
        )

    def update_config(
        self,
        persona: VoicePersona | None = None,
        auto_speak: bool | None = None,
        prefer_offline: bool | None = None,
    ) -> VoiceConfig:
        if persona is not None and persona in _ALL_PERSONAS:
            self._config.persona = persona
        if auto_speak is not None:
            self._config.auto_speak = bool(auto_speak)
        if prefer_offline is not None:
            self._config.prefer_offline = bool(prefer_offline)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(
                {
                    "persona": self._config.persona,
                    "auto_speak": self._config.auto_speak,
                    "prefer_offline": self._config.prefer_offline,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return self.get_config()

    def stop(self) -> None:
        with self._state_lock:
            self._stop_seq += 1
            self._speaking = False
            self._actively_playing = False
            self._playback_envelope = []
            evt = self._playback_stop_event
        if evt is not None:
            evt.set()
        if sys.platform == "win32":
            try:
                import winsound
                winsound.PlaySound(None, getattr(winsound, "SND_PURGE", 0))
            except Exception:
                pass

    def speak_async(self, text: str, persona: VoicePersona | None = None) -> None:
        target = persona or self._config.persona
        if target == "off":
            return
        clean = _clean_speech_text(text)
        if not clean:
            return
        stop_evt = threading.Event()
        with self._state_lock:
            prev_evt = self._playback_stop_event
            self._stop_seq += 1
            seq = self._stop_seq
            self._speaking = True
            self._actively_playing = False
            self._playback_envelope = []
            self._playback_stop_event = stop_evt
        if prev_evt is not None:
            prev_evt.set()
        if sys.platform == "win32":
            try:
                import winsound
                winsound.PlaySound(None, getattr(winsound, "SND_PURGE", 0))
            except Exception:
                pass

        def _worker() -> None:
            wav_path: Path | None = None
            if self._stop_seq != seq or stop_evt.is_set():
                return
            self._speaking = True
            try:
                wav_path = synthesize_wav(
                    clean,
                    persona=target,
                    prefer_offline=self._config.prefer_offline,
                )
                if self._stop_seq != seq or stop_evt.is_set():
                    return
                envelope = _compute_wav_rms_envelope(wav_path, block_ms=100)
                with self._play_lock:
                    if self._stop_seq != seq or stop_evt.is_set():
                        return
                    if sys.platform == "win32":
                        import winsound
                        duration_s = 3.0
                        try:
                            with wave.open(str(wav_path), "rb") as wf:
                                duration_s = wf.getnframes() / float(max(1, wf.getframerate()))
                        except Exception:
                            pass
                        with self._state_lock:
                            if self._stop_seq != seq or stop_evt.is_set():
                                return
                            self._actively_playing = True
                            self._playback_started_at = time.monotonic()
                            self._playback_envelope = envelope
                        winsound.PlaySound(str(wav_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
                        deadline = time.monotonic() + duration_s + 0.15
                        while self._stop_seq == seq and not stop_evt.is_set():
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                break
                            if stop_evt.wait(timeout=min(0.02, remaining)):
                                break
                        with self._state_lock:
                            self._actively_playing = False
                            self._playback_envelope = []
                        winsound.PlaySound(None, getattr(winsound, "SND_PURGE", 0))
            except Exception:
                pass
            finally:
                with self._state_lock:
                    if self._stop_seq == seq:
                        self._speaking = False
                        self._actively_playing = False
                        self._playback_envelope = []
                if wav_path is not None:
                    try:
                        wav_path.unlink(missing_ok=True)
                    except OSError:
                        pass

        threading.Thread(target=_worker, name="aura-voice-tts", daemon=True).start()
