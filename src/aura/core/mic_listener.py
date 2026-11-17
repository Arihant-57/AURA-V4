"""
Privacy-first Windows Microphone Listener for AURA V4 Dynamic Island.

Primary engine (when `sounddevice` and `faster-whisper` are installed):
- Captures 16 kHz mono float32 microphone audio ONLY while explicitly enabled.
- Uses adaptive RMS Voice Activity Detection (VAD) with pre-roll + silence
  endpointing so natural multi-word speech in any English accent is captured
  without clipping first syllables.
- Transcribes locally offline via `faster-whisper` (`base.en`, `int8`, CPU).

Fallback engine:
- Built-in Windows `.NET System.Speech.Recognition` (`DictationGrammar`) running
  in a hidden background PowerShell process (`CREATE_NO_WINDOW = 0x08000000`).

Strict privacy guarantees:
- Disabled by default; zero microphone streams or subprocesses exist while Off.
- `pause()` / `resume()` discards audio frames while AURA is thinking or speaking.
- `stop()` closes the audio stream or kills the recognizer process immediately.
"""

from __future__ import annotations

import base64
from collections import deque
import os
import queue
import re
import subprocess
import sys
import threading
from typing import Any, Callable


CREATE_NO_WINDOW = 0x08000000

_PS_DICTATION_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$rec = $null
try {
    $installed = [System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers()
    foreach ($lang in @('en-IN', 'en-GB')) {
        $match = $installed | Where-Object { $_.Culture.Name -eq $lang } | Select-Object -First 1
        if ($null -ne $match) {
            $rec = New-Object System.Speech.Recognition.SpeechRecognitionEngine($match.Culture)
            break
        }
    }
} catch {}
if ($null -eq $rec) {
    $rec = New-Object System.Speech.Recognition.SpeechRecognitionEngine
}
$rec.BabbleTimeout = [TimeSpan]::FromSeconds(0)
$rec.InitialSilenceTimeout = [TimeSpan]::FromSeconds(0)
$rec.EndSilenceTimeout = [TimeSpan]::FromMilliseconds(850)
$rec.EndSilenceTimeoutAmbiguous = [TimeSpan]::FromMilliseconds(1100)

$dictation = New-Object System.Speech.Recognition.DictationGrammar
$rec.LoadGrammar($dictation)
$rec.SetInputToDefaultAudioDevice()

$script:paused = $false
$rec.add_SpeechRecognized({
    param($s, $e)
    if ($script:paused) { return }
    if ($null -eq $e -or $null -eq $e.Result) { return }
    $txt = ($e.Result.Text -replace "[\r\n]+", " ").Trim()
    $conf = [math]::Round($e.Result.Confidence, 3)
    if ($txt.Length -gt 0) {
        [Console]::Out.WriteLine(("HEARD|" + $conf + "|" + $txt))
        [Console]::Out.Flush()
    }
})

$rec.add_SpeechDetected({
    if ($script:paused) { return }
    [Console]::Out.WriteLine("SPEECH_START")
    [Console]::Out.Flush()
})

$rec.RecognizeAsync([System.Speech.Recognition.RecognizeMode]::Multiple)
[Console]::Out.WriteLine("READY")
[Console]::Out.Flush()

try {
    while ($true) {
        $line = [Console]::In.ReadLine()
        if ($null -eq $line) { break }
        $cmd = $line.Trim().ToUpperInvariant()
        if ($cmd -eq "QUIT") { break }
        elseif ($cmd -eq "PAUSE") { $script:paused = $true }
        elseif ($cmd -eq "RESUME") { $script:paused = $false }
    }
} finally {
    try { $rec.RecognizeAsyncCancel() } catch {}
    try { $rec.Dispose() } catch {}
}
"""

_WHISPER_MODEL_LOCK = threading.Lock()
_CACHED_WHISPER_MODEL: Any = None

_WAKE_ONLY_RE = re.compile(
    r"^(?:(?:hey|hi|hello|yo|ok|okay)[\s,]+)?(?P<wake>jarvis|aura|ultron|megatron)[\s,!?.:;]*$",
    re.IGNORECASE,
)
_WAKE_PREFIX_RE = re.compile(
    r"^(?:(?:hey|hi|hello|yo|ok|okay)[\s,]+)?(?P<wake>jarvis|aura|ultron|megatron)[\s,:;.-]+(?P<rest>.+)$",
    re.IGNORECASE,
)
_MID_WAKE_RE = re.compile(
    r"\b(?P<wake>jarvis|aura|ultron|megatron)\b(?:[\s,:;.-]+(?P<rest>.+))?$",
    re.IGNORECASE,
)
_CONVERSATIONAL_FOLLOWUPS = frozenset({
    "what are you doing",
    "how are you",
    "who are you",
    "what's up",
    "whats up",
    "good morning",
    "good afternoon",
    "good evening",
})


_WHISPER_INITIAL_PROMPT = (
    "Hey Jarvis, Aura, Ultron, Megatron, Arihant. Open Chrome, open Gmail, open Google Calendar, "
    "open Downloads, open Desktop, open Documents, open VS Code, open smart helmet, "
    "use Gemini, use FreeLLMAPI, summarize my day, read my emails, stop, pause, cancel."
)

_DEFAULT_HOTWORDS = (
    "Aura, Jarvis, Ultron, Arihant, Chrome, Gmail, Calendar, FreeLLMAPI, Gemini, "
    "Downloads, Desktop, Documents, smart-helmet, YouTube"
)

_INDIAN_EN_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:you\s+tube|u\s*tube|youtube\.com)\b", re.IGNORECASE), "YouTube"),
    (re.compile(r"\bg\s+mail\b", re.IGNORECASE), "Gmail"),
    (re.compile(r"\bcalender\b", re.IGNORECASE), "calendar"),
    (re.compile(r"\bv\s*s\s+code\b", re.IGNORECASE), "VS Code"),
    (re.compile(r"\bbrowzer\b", re.IGNORECASE), "browser"),
    (re.compile(r"\b(?:krome|crom)\b", re.IGNORECASE), "Chrome"),
    (re.compile(r"\b(?:googal|googel)\b", re.IGNORECASE), "Google"),
    (re.compile(r"\b(?:jemini|geminy|gemni|jimini)\b", re.IGNORECASE), "Gemini"),
    (re.compile(r"\bfree\s+l(?:lm|m|l)\s*a\s*p\s*i\b|\bfree\s+llapi\b", re.IGNORECASE), "FreeLLMAPI"),
    (re.compile(r"\bdown\s+loads\b", re.IGNORECASE), "Downloads"),
    (re.compile(r"\bdesk\s+top\b", re.IGNORECASE), "Desktop"),
    (re.compile(r"\bwhats\s+app\b", re.IGNORECASE), "WhatsApp"),
    (re.compile(r"\b(?:arihanth|arihant\s+sir|are\s+you\s+hunt)\b", re.IGNORECASE), "Arihant"),
    (re.compile(r"\b(hey|hi|hello|ok|okay|open|ask|stop)\s+ora\b", re.IGNORECASE), r"\1 Aura"),
    (re.compile(r"\b(?:altron|altrons|ultran|oltron)\b", re.IGNORECASE), "Ultron"),
    (re.compile(r"\bmega\s+tron\b", re.IGNORECASE), "Megatron"),
    (re.compile(r"\bhey\s+(?:service|jarves|jarvish|charvis|chavis|javis|jar\s+vis)\b", re.IGNORECASE), "Hey Jarvis"),
    (re.compile(r"\b(?:jarves|jarvish|charvis|chavis|javis|jar\s+vis)\b", re.IGNORECASE), "Jarvis"),
)


def _build_whisper_prompt_and_hotwords() -> tuple[str, str]:
    """Build domain & user-name biased Whisper prompt/hotwords for Indian-English clarity."""
    extra_terms: list[str] = []
    seen: set[str] = set()

    def _add_term(raw: str) -> None:
        clean = " ".join(str(raw or "").split()).strip(" .,:;")
        if 2 <= len(clean) <= 40:
            key = clean.lower()
            if key not in seen:
                seen.add(key)
                extra_terms.append(clean)

    try:
        from pathlib import Path
        from .personal_memory import PersonalMemoryStore

        store = PersonalMemoryStore()
        for fact in store.list_facts()[:20]:
            for tok in re.findall(r"\b[A-Z][a-zA-Z0-9_-]{2,24}\b", fact.text):
                _add_term(tok)
        desktop = Path.home() / "Desktop"
        if desktop.is_dir():
            for entry in list(desktop.iterdir())[:12]:
                if not entry.name.startswith("."):
                    _add_term(entry.stem if entry.is_file() else entry.name)
    except Exception:
        pass

    if extra_terms:
        extra_str = ", ".join(extra_terms[:10])
        return f"{_WHISPER_INITIAL_PROMPT} Names & folders: {extra_str}.", f"{_DEFAULT_HOTWORDS}, {extra_str}"
    return _WHISPER_INITIAL_PROMPT, _DEFAULT_HOTWORDS

_STOP_CMD_RE = re.compile(
    r"^(?:(?:hey|ok|okay|hi|please|wait|hold\s+on)[,\s]+)*(?:(?:jarvis|aura|ultron|megatron)[,\s]+)?(?:stop|pause|cancel|abort|quiet|hush|shut\s+up|be\s+quiet|stop\s+talking|stop\s+speaking|stop\s+reading|stop\s+it|never\s+mind|nevermind)(?:[,\s]+(?:please|now|jarvis|aura|ultron|megatron))?[.!?]*$",
    re.IGNORECASE,
)
_BARGE_IN_STOP_RE = re.compile(
    r"\b(?:stop(?:\s+talking|\s+speaking|\s+reading|\s+it|\s+now)?|quiet|hush|shut\s+up|be\s+quiet|cancel(?:\s+that)?|abort|never\s*mind|pause)\b",
    re.IGNORECASE,
)
_CODE_STOP_GUARD_RE = re.compile(r"\.[a-z]{1,4}\b|\b(?:server|process|docker|service|script|loop)\b", re.IGNORECASE)


def normalize_indian_english_transcript(text: str) -> str:
    """Fix common Indian-English STT homophones and split app names without altering normal prose."""
    clean = " ".join((text or "").split()).strip()
    if not clean:
        return ""
    for pat, repl in _INDIAN_EN_REPLACEMENTS:
        clean = pat.sub(repl, clean)
    return " ".join(clean.split()).strip()


def is_stop_command(text: str, during_playback: bool = False) -> bool:
    """Return True when spoken text is an explicit barge-in stop/pause/quiet command."""
    clean = normalize_indian_english_transcript(text)
    if not clean:
        return False
    if _STOP_CMD_RE.match(clean):
        return True
    # During active playback/thinking, speaker bleed can prepend words before "stop"
    if during_playback and not _CODE_STOP_GUARD_RE.search(clean):
        return bool(_BARGE_IN_STOP_RE.search(clean))
    return False


def extract_wake_command(text: str, require_wake_word: bool = False) -> str | None:
    """Normalize a spoken utterance around optional wake words ('Jarvis', 'Aura', 'Megatron')."""
    clean = normalize_indian_english_transcript(text)
    if not clean:
        return None

    m_only = _WAKE_ONLY_RE.match(clean)
    if m_only:
        return f"Hey {m_only.group('wake').capitalize()}"

    m = _WAKE_PREFIX_RE.match(clean)
    if m:
        rest = (m.group("rest") or "").strip(" ,:;.-")
        if rest.rstrip("?!.").lower() in _CONVERSATIONAL_FOLLOWUPS:
            return clean
        return rest

    if not require_wake_word:
        return clean

    m_mid = _MID_WAKE_RE.search(clean)
    if not m_mid:
        return None
    wake = m_mid.group("wake")
    rest = (m_mid.group("rest") or "").strip(" ,:;.-")
    return rest if rest else f"Hey {wake.capitalize()}"


def _has_whisper_mic_deps() -> bool:
    try:
        import faster_whisper  # noqa: F401
        import numpy  # noqa: F401
        import sounddevice  # noqa: F401
        return True
    except Exception:
        return False


def _resolve_whisper_model_name(requested: str = "base.en") -> str:
    env_mdl = os.environ.get("AURA_WHISPER_MODEL", "").strip()
    if env_mdl:
        return env_mdl
    if requested == "base.en":
        from pathlib import Path
        hub_dir = Path.home() / ".cache" / "huggingface" / "hub"
        for preferred in ("small.en", "small"):
            if (hub_dir / f"models--Systran--faster-whisper-{preferred}").is_dir():
                return preferred
    return requested


def _get_whisper_model(model_size: str = "base.en") -> Any:
    global _CACHED_WHISPER_MODEL
    chosen = _resolve_whisper_model_name(model_size)
    with _WHISPER_MODEL_LOCK:
        if _CACHED_WHISPER_MODEL is not None:
            return _CACHED_WHISPER_MODEL
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        from faster_whisper import WhisperModel

        try:
            _CACHED_WHISPER_MODEL = WhisperModel(
                chosen,
                device="cpu",
                compute_type="int8",
                local_files_only=True,
            )
        except Exception:
            _CACHED_WHISPER_MODEL = WhisperModel(
                chosen,
                device="cpu",
                compute_type="int8",
                local_files_only=False,
            )
        return _CACHED_WHISPER_MODEL


def transcribe_whisper_audio(model: Any, audio: Any) -> str:
    """Transcribe a 16 kHz mono float32 audio array, filtering low-confidence noise."""
    normalized_audio = audio
    try:
        peak = float(abs(audio).max()) if getattr(audio, "size", 0) else 0.0
        if 0.004 < peak < 0.25:
            normalized_audio = audio * min(6.0, 0.35 / peak)
    except Exception:
        normalized_audio = audio

    prompt_txt, hotwords_txt = _build_whisper_prompt_and_hotwords()
    beam = max(1, int(os.environ.get("AURA_WHISPER_BEAM_SIZE", "1")))
    try:
        segments, _ = model.transcribe(
            normalized_audio,
            language="en",
            beam_size=beam,
            best_of=beam,
            temperature=0.0,
            initial_prompt=prompt_txt,
            hotwords=hotwords_txt,
            condition_on_previous_text=False,
            vad_filter=False,
        )
    except TypeError:
        try:
            segments, _ = model.transcribe(
                normalized_audio,
                language="en",
                beam_size=beam,
                best_of=beam,
                temperature=0.0,
                initial_prompt=prompt_txt,
                condition_on_previous_text=False,
                vad_filter=False,
            )
        except TypeError:
            segments, _ = model.transcribe(
                normalized_audio,
                language="en",
                beam_size=1,
                condition_on_previous_text=False,
                vad_filter=False,
            )
    kept: list[str] = []
    for seg in segments:
        no_speech = getattr(seg, "no_speech_prob", 0.0) or 0.0
        avg_logprob = getattr(seg, "avg_logprob", 0.0) or 0.0
        txt = " ".join((getattr(seg, "text", "") or "").split()).strip()
        if not txt or no_speech > 0.78 or avg_logprob < -1.55:
            continue
        kept.append(txt)
    joined = normalize_indian_english_transcript(" ".join(kept))
    if len(joined) < 2:
        return ""
    return joined


def _default_process_factory() -> subprocess.Popen[str]:
    encoded = base64.b64encode(_PS_DICTATION_SCRIPT.encode("utf-16-le")).decode("ascii")
    from .mcp_hub import win32_subprocess_kwargs

    kwargs = win32_subprocess_kwargs(hide_window=True)
    return subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **kwargs,
    )


def parse_recognizer_line(line: str, min_confidence: float = 0.35) -> tuple[str, str]:
    """Parse one stdout line from the fallback recognizer subprocess."""
    clean = (line or "").strip()
    if not clean:
        return ("ignore", "")
    if clean == "READY":
        return ("ready", "")
    if clean == "SPEECH_START":
        return ("speech_onset", "")
    if clean.startswith("HEARD|"):
        parts = clean.split("|", 2)
        if len(parts) != 3:
            return ("ignore", "")
        try:
            conf = float(parts[1])
        except ValueError:
            conf = 0.0
        text = " ".join(parts[2].split()).strip()
        if conf < min_confidence or len(text) < 2:
            return ("ignore", "")
        return ("transcript", text)
    return ("log", clean)


class MicListener:
    """Explicitly toggled offline speech listener with hard shutdown cleanup."""

    def __init__(
        self,
        on_transcript: Callable[[str], None],
        on_status: Callable[[str], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        min_confidence: float = 0.35,
        process_factory: Callable[[], subprocess.Popen[str]] | None = None,
        whisper_model_size: str = "base.en",
        require_wake_word: bool | None = None,
        end_silence_blocks: int = 16,
        max_utterance_blocks: int = 300,
        on_speech_onset: Callable[[], None] | None = None,
        voice_engine: Any | None = None,
        playback_level_fn: Callable[[], float] | None = None,
        barge_in_rms_floor: float = 0.032,
        barge_in_onset_blocks: int = 2,
    ) -> None:
        self._on_transcript = on_transcript
        self._on_status = on_status
        self._on_error = on_error
        self._min_confidence = min_confidence
        self._custom_factory = process_factory is not None
        self._process_factory = process_factory or _default_process_factory
        self._whisper_model_size = whisper_model_size
        self._end_silence_blocks = max(5, int(end_silence_blocks))
        self._max_utterance_blocks = max(60, int(max_utterance_blocks))
        if require_wake_word is None:
            self._require_wake_word = os.environ.get("AURA_WAKE_WORD_ONLY", "0") == "1"
        else:
            self._require_wake_word = bool(require_wake_word)
        self._on_speech_onset = on_speech_onset
        self._voice_engine = voice_engine
        self._playback_level_fn = playback_level_fn
        self._barge_in_rms_floor = max(0.005, float(barge_in_rms_floor))
        self._barge_in_onset_blocks = max(1, int(barge_in_onset_blocks))

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._proc: subprocess.Popen[str] | None = None
        self._worker_thread: threading.Thread | None = None
        self._audio_queue: queue.Queue[Any] | None = None
        self._listening = False
        self._paused = False
        self._barge_in = False
        self._onset_triggered_count = 0
        self.reset_vad_state()

    def attach_voice_engine(self, voice_engine: Any | None) -> None:
        """Attach or replace the VoiceEngine stopped immediately on speech onset."""
        self._voice_engine = voice_engine

    def reset_vad_state(self) -> None:
        """Reset streaming VAD, echo tracker, and speech-onset accumulators."""
        self._pre_roll: deque[Any] = deque(maxlen=8)
        self._voiced_chunks: list[Any] = []
        self._speech_blocks = 0
        self._quiet_blocks = 0
        self._ambient_rms = 0.004
        self._echo_rms = 0.004
        self._calib_samples = 0
        self._barge_onset_streak = 0
        self._onset_fired = False
        self._utterance_during_barge_in = False

    def set_barge_in(self, active: bool) -> None:
        """Enable fast speech-onset and stop-word detection while AURA is thinking or speaking."""
        new_val = bool(active)
        if new_val == self._barge_in:
            return
        self._barge_in = new_val
        self._barge_onset_streak = 0
        if new_val:
            # Entering playback: reset per-playback onset gate so new user speech fires immediately.
            self._onset_fired = False
            if not self._voiced_chunks:
                self._echo_rms = max(self._ambient_rms, 0.006)
        else:
            # Leaving playback after barge-in or completion: allow subsequent playback to arm cleanly.
            self._onset_fired = False

    @property
    def require_wake_word(self) -> bool:
        return self._require_wake_word

    @require_wake_word.setter
    def require_wake_word(self, value: bool) -> None:
        self._require_wake_word = bool(value)

    def _emit_transcript(self, raw_text: str) -> str | None:
        during_playback = bool(self._barge_in or self._utterance_during_barge_in)
        self._utterance_during_barge_in = False
        if is_stop_command(raw_text, during_playback=during_playback):
            self._on_transcript("stop")
            return "stop"
        cmd = extract_wake_command(raw_text, require_wake_word=self._require_wake_word)
        if cmd:
            self._on_transcript(cmd)
            return cmd
        return None

    def _current_playback_level(self) -> float:
        level = 0.0
        fn = self._playback_level_fn
        if fn is not None:
            try:
                level = max(level, float(fn()))
            except Exception:
                pass
        ve = self._voice_engine
        if ve is not None:
            lvl_fn = getattr(ve, "current_playback_level", None)
            if callable(lvl_fn):
                try:
                    level = max(level, float(lvl_fn()))
                except Exception:
                    pass
            elif getattr(ve, "is_actively_playing", False) or getattr(ve, "is_speaking", False):
                level = max(level, 0.25)
        return max(0.0, min(1.0, level))

    def _trigger_speech_onset(self) -> bool:
        """Immediately halt TTS playback on verified user speech onset."""
        if self._paused or self._stop_event.is_set() or self._onset_fired:
            return False
        self._onset_fired = True
        self._utterance_during_barge_in = True
        self._onset_triggered_count += 1

        ve = self._voice_engine
        if ve is not None:
            stop_fn = getattr(ve, "stop", None)
            if callable(stop_fn):
                try:
                    stop_fn()
                except Exception:
                    pass

        cb = self._on_speech_onset
        if cb is not None:
            try:
                cb()
            except Exception:
                pass
        return True

    @property
    def is_listening(self) -> bool:
        return self._listening and not self._stop_event.is_set()

    @property
    def is_paused(self) -> bool:
        return self._paused

    def start(self) -> bool:
        """Start listening via local faster-whisper + sounddevice, or fallback subprocess."""
        with self._lock:
            if self._listening:
                return True
            self._stop_event.clear()
            self._paused = False

            if not self._custom_factory and _has_whisper_mic_deps():
                self._listening = True
                self._audio_queue = queue.Queue(maxsize=320)
                if self._on_status:
                    self._on_status("loading")
                self._worker_thread = threading.Thread(
                    target=self._whisper_loop,
                    name="aura-whisper-mic",
                    daemon=True,
                )
                self._worker_thread.start()
                return True

            if sys.platform != "win32" and not self._custom_factory:
                if self._on_error:
                    self._on_error("Microphone dictation requires sounddevice + faster-whisper on this platform.")
                return False

            try:
                proc = self._process_factory()
            except Exception as exc:
                if self._on_error:
                    self._on_error(f"Could not start microphone listener: {exc}")
                return False

            self._proc = proc
            self._listening = True
            self._worker_thread = threading.Thread(
                target=self._read_loop,
                args=(proc,),
                name="aura-mic-reader",
                daemon=True,
            )
            self._worker_thread.start()
            return True

    def pause(self) -> None:
        """Temporarily discard audio frames (e.g., while AURA speaks)."""
        with self._lock:
            self._paused = True
            self.reset_vad_state()
            if self._audio_queue is not None:
                while not self._audio_queue.empty():
                    try:
                        self._audio_queue.get_nowait()
                    except queue.Empty:
                        break
            self._send_cmd("PAUSE")

    def resume(self) -> None:
        """Resume voice capture when AURA is idle again."""
        with self._lock:
            self._paused = False
            if self._audio_queue is not None:
                while not self._audio_queue.empty():
                    try:
                        self._audio_queue.get_nowait()
                    except queue.Empty:
                        break
            if self._listening:
                self._send_cmd("RESUME")

    def stop(self) -> None:
        """Stop listening immediately and close any audio stream or subprocess."""
        with self._lock:
            self._listening = False
            self._paused = False
            self._stop_event.set()
            proc = self._proc
            self._proc = None
            aq = self._audio_queue
            self._audio_queue = None

        if aq is not None:
            try:
                aq.put_nowait(None)
            except Exception:
                pass

        if proc is not None:
            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.write("QUIT\n")
                    proc.stdin.flush()
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        if self._on_status:
            self._on_status("off")

    def _send_cmd(self, cmd: str) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        try:
            if not proc.stdin.closed:
                proc.stdin.write(cmd + "\n")
                proc.stdin.flush()
        except Exception:
            pass

    def process_audio_chunk(self, chunk: Any, model: Any = None, np_mod: Any = None) -> str | None:
        """Process one 100ms float32 audio chunk with echo-guarded onset barge-in and VAD."""
        if self._paused:
            self._pre_roll.clear()
            self._voiced_chunks.clear()
            self._speech_blocks = 0
            self._quiet_blocks = 0
            self._barge_onset_streak = 0
            return None

        if np_mod is None:
            import numpy as np_mod

        rms = float(np_mod.sqrt(np_mod.mean(chunk * chunk)))
        if self._calib_samples < 4 and not self._voiced_chunks and not self._barge_in and rms < 0.025:
            self._ambient_rms = (self._ambient_rms * self._calib_samples + rms) / (self._calib_samples + 1)
            self._calib_samples += 1
            self._pre_roll.append(chunk)
            return None

        normal_threshold = max(0.008, min(0.055, self._ambient_rms * 2.2))
        playback_level = self._current_playback_level() if self._barge_in else 0.0
        barge_threshold = min(
            0.25,
            max(
                self._barge_in_rms_floor,
                self._ambient_rms * 3.5,
                self._echo_rms * 1.8,
                playback_level * 0.16,
            ),
        )

        if not self._voiced_chunks:
            active_threshold = barge_threshold if self._barge_in else normal_threshold
            if rms >= active_threshold:
                keep_pre = list(self._pre_roll)[-3:] if self._barge_in else list(self._pre_roll)
                self._voiced_chunks.extend(keep_pre)
                self._voiced_chunks.append(chunk)
                self._speech_blocks = 1
                self._quiet_blocks = 0
                if self._barge_in:
                    self._barge_onset_streak = 1
                    self._utterance_during_barge_in = True
                    if self._barge_onset_streak >= self._barge_in_onset_blocks:
                        self._trigger_speech_onset()
                else:
                    self._barge_onset_streak = 0
            else:
                if self._barge_in:
                    self._echo_rms = 0.85 * self._echo_rms + 0.15 * rms
                else:
                    self._ambient_rms = 0.92 * self._ambient_rms + 0.08 * rms
                    self._echo_rms = 0.90 * self._echo_rms + 0.10 * self._ambient_rms
                self._barge_onset_streak = 0
                self._pre_roll.append(chunk)
            return None

        self._voiced_chunks.append(chunk)
        if self._barge_in and not self._onset_fired:
            if rms >= barge_threshold:
                self._barge_onset_streak += 1
                self._speech_blocks += 1
                self._quiet_blocks = 0
                if self._barge_onset_streak >= self._barge_in_onset_blocks:
                    self._trigger_speech_onset()
            else:
                self._barge_onset_streak = 0
                if self._speech_blocks < self._barge_in_onset_blocks:
                    # Drop isolated single-block speaker pops/transients before capturing an utterance.
                    self._echo_rms = 0.85 * self._echo_rms + 0.15 * rms
                    self._pre_roll.append(chunk)
                    self._voiced_chunks.clear()
                    self._speech_blocks = 0
                    self._quiet_blocks = 0
                    self._utterance_during_barge_in = False
                    return None
                self._quiet_blocks += 1
        else:
            if rms >= normal_threshold * 0.72:
                self._speech_blocks += 1
                self._quiet_blocks = 0
            else:
                self._quiet_blocks += 1

        # Fast sliding stop-word check every ~1.4s while AURA speaks (or if utterance started during playback)
        if (
            (self._barge_in or self._utterance_during_barge_in)
            and model is not None
            and len(self._voiced_chunks) >= 14
            and (len(self._voiced_chunks) % 14 == 0)
        ):
            probe_np = np_mod.concatenate(self._voiced_chunks[-22:]).astype(np_mod.float32)
            probe_txt = transcribe_whisper_audio(model, probe_np)
            if probe_txt and is_stop_command(probe_txt, during_playback=True):
                if not self._paused and not self._stop_event.is_set():
                    self._on_transcript("stop")
                self._pre_roll.clear()
                self._voiced_chunks.clear()
                self._speech_blocks = 0
                self._quiet_blocks = 0
                self._barge_onset_streak = 0
                self._utterance_during_barge_in = False
                return "stop"

        # Allow natural mid-sentence pauses (~1.6s) and up to ~30s continuous dictation
        if self._quiet_blocks >= self._end_silence_blocks or len(self._voiced_chunks) >= self._max_utterance_blocks:
            emitted: str | None = None
            if self._speech_blocks >= 2 and model is not None:
                audio_np = np_mod.concatenate(self._voiced_chunks).astype(np_mod.float32)
                text = transcribe_whisper_audio(model, audio_np)
                if text and not self._paused and not self._stop_event.is_set():
                    emitted = self._emit_transcript(text)
            else:
                self._utterance_during_barge_in = False
            self._pre_roll.clear()
            self._voiced_chunks.clear()
            self._speech_blocks = 0
            self._quiet_blocks = 0
            self._barge_onset_streak = 0
            return emitted
        return None

    def _whisper_loop(self) -> None:
        import numpy as np
        import sounddevice as sd

        sample_rate = 16000
        block_samples = 1600  # 100 ms chunks
        aq = self._audio_queue
        if aq is None:
            return

        def _callback(indata: Any, _frames: int, _time_info: Any, _status: Any) -> None:
            if self._stop_event.is_set() or self._paused:
                return
            try:
                aq.put_nowait(indata[:, 0].copy())
            except queue.Full:
                pass

        stream = None
        try:
            model = _get_whisper_model(self._whisper_model_size)
            if self._stop_event.is_set():
                return
            stream = sd.InputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="float32",
                blocksize=block_samples,
                callback=_callback,
            )
            stream.start()
            if self._on_status:
                self._on_status("listening")

            self.reset_vad_state()

            while not self._stop_event.is_set() and self._listening:
                try:
                    chunk = aq.get(timeout=0.2)
                except queue.Empty:
                    continue
                if chunk is None or self._stop_event.is_set():
                    break
                self.process_audio_chunk(chunk, model=model, np_mod=np)
        except Exception as exc:
            if not self._stop_event.is_set() and self._on_error:
                self._on_error(f"Microphone listener error: {exc}")
        finally:
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
            with self._lock:
                self._listening = False

    def _read_loop(self, proc: subprocess.Popen[str]) -> None:
        try:
            stdout = proc.stdout
            if stdout is not None:
                for raw_line in stdout:
                    if self._stop_event.is_set() or not self._listening:
                        break
                    kind, payload = parse_recognizer_line(raw_line, min_confidence=self._min_confidence)
                    if kind == "ready" and self._on_status:
                        self._on_status("listening")
                    elif kind == "speech_onset" and not self._paused:
                        if self._barge_in:
                            self._trigger_speech_onset()
                    elif kind == "transcript" and not self._paused:
                        if self._barge_in:
                            self._utterance_during_barge_in = True
                        self._emit_transcript(payload)
            rc = proc.poll()
            if rc and not self._stop_event.is_set() and self._listening:
                err_txt = ""
                try:
                    if proc.stderr is not None:
                        err_txt = proc.stderr.read().strip()
                except Exception:
                    pass
                if self._on_error:
                    self._on_error(err_txt or f"Microphone listener exited with code {rc}.")
        except Exception as exc:
            if not self._stop_event.is_set() and self._on_error:
                self._on_error(f"Microphone listener error: {exc}")
        finally:
            with self._lock:
                if self._proc is proc:
                    self._proc = None
                    self._listening = False
