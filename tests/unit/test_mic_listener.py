"""Unit tests for the privacy-first Windows MicListener."""

from __future__ import annotations

import io
import time

from src.aura.core.mic_listener import MicListener, parse_recognizer_line


class _FakeProc:
    def __init__(self, lines: list[str]) -> None:
        self.stdin = io.StringIO()
        self.stdout = iter(lines)
        self.stderr = io.StringIO("")
        self.terminated = False
        self.killed = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


def test_parse_recognizer_line_filters_noise_and_low_confidence():
    assert parse_recognizer_line("READY") == ("ready", "")
    assert parse_recognizer_line("HEARD|0.20|cough") == ("ignore", "")
    assert parse_recognizer_line("HEARD|0.82|open   my gmail") == ("transcript", "open my gmail")
    assert parse_recognizer_line("") == ("ignore", "")


def test_mic_listener_strict_off_pause_resume_and_stop():
    transcripts: list[str] = []
    statuses: list[str] = []
    fake = _FakeProc(["READY\n", "HEARD|0.91|hi jarvis what are you doing\n"])

    ml = MicListener(
        on_transcript=transcripts.append,
        on_status=statuses.append,
        process_factory=lambda: fake,  # type: ignore[arg-type,return-value]
    )
    assert ml.is_listening is False
    assert ml.start() is True
    for _ in range(30):
        if transcripts and "listening" in statuses:
            break
        time.sleep(0.02)

    assert "listening" in statuses
    assert transcripts == ["hi jarvis what are you doing"]

    ml.pause()
    assert ml.is_paused is True
    assert "PAUSE" in fake.stdin.getvalue()
    ml.resume()
    assert ml.is_paused is False
    assert "RESUME" in fake.stdin.getvalue()

    ml.stop()
    assert ml.is_listening is False
    assert fake.terminated is True
    assert "off" in statuses


def test_transcribe_whisper_audio_filters_no_speech_and_low_logprob():
    from types import SimpleNamespace
    from src.aura.core.mic_listener import transcribe_whisper_audio

    class _FakeModel:
        def transcribe(self, audio, **kwargs):
            segs = [
                SimpleNamespace(text=" Thank you.", no_speech_prob=0.85, avg_logprob=-0.4),
                SimpleNamespace(text=" Hey Jarvis open my gmail", no_speech_prob=0.05, avg_logprob=-0.25),
            ]
            return iter(segs), None

    out = transcribe_whisper_audio(_FakeModel(), [0.0] * 1600)
    assert out == "Hey Jarvis open my gmail"


def test_extract_wake_command_and_listener_filtering():
    from src.aura.core.mic_listener import extract_wake_command

    assert extract_wake_command("Hey Jarvis, open calculator") == "open calculator"
    assert extract_wake_command("Hey, Jarvis, open calculator") == "open calculator"
    assert extract_wake_command("Hey Jarvis") == "Hey Jarvis"
    assert extract_wake_command("Hey, Jarvis.") == "Hey Jarvis"
    assert extract_wake_command("Jarvis") == "Hey Jarvis"
    assert extract_wake_command("hi jarvis what are you doing") == "hi jarvis what are you doing"

    assert extract_wake_command("Pass the salt please", require_wake_word=False) == "Pass the salt please"
    assert extract_wake_command("Pass the salt please", require_wake_word=True) is None
    assert extract_wake_command("Jarvis what time is it", require_wake_word=True) == "what time is it"
    assert extract_wake_command("Please, Jarvis, check my calendar", require_wake_word=True) == "check my calendar"

    heard: list[str] = []
    fake = _FakeProc([
        "READY\n",
        "HEARD|0.90|random background chatter\n",
        "HEARD|0.92|Hey Jarvis, open notepad\n",
    ])
    ml = MicListener(
        on_transcript=heard.append,
        process_factory=lambda: fake,  # type: ignore[arg-type,return-value]
        require_wake_word=True,
    )
    assert ml.require_wake_word is True
    assert ml.start() is True
    for _ in range(30):
        if heard:
            break
        time.sleep(0.02)
    ml.stop()
    assert heard == ["open notepad"]


def test_indian_english_normalization_and_stop_command():
    from src.aura.core.mic_listener import (
        extract_wake_command,
        is_stop_command,
        normalize_indian_english_transcript,
        transcribe_whisper_audio,
    )

    assert normalize_indian_english_transcript("open you tube") == "open YouTube"
    assert normalize_indian_english_transcript("hey service check my calender") == "Hey Jarvis check my calendar"
    assert normalize_indian_english_transcript("open g mail and v s code") == "open Gmail and VS Code"
    assert normalize_indian_english_transcript("hi jarvis what are you doing") == "hi jarvis what are you doing"

    assert extract_wake_command("hey service open you tube", require_wake_word=True) == "open YouTube"

    assert is_stop_command("stop") is True
    assert is_stop_command("Hey Jarvis, stop talking!") is True
    assert is_stop_command("pause") is True
    assert is_stop_command("quiet please") is True
    assert is_stop_command("stop the server in server.py") is False
    assert is_stop_command("open YouTube") is False

    class _StrictSigModel:
        def transcribe(self, audio, language="en", beam_size=1, condition_on_previous_text=False, vad_filter=False):
            from types import SimpleNamespace
            return [SimpleNamespace(text="open you tube", no_speech_prob=0.05, avg_logprob=-0.2)], None

    assert transcribe_whisper_audio(_StrictSigModel(), [0.1, -0.1]) == "open YouTube"


def test_barge_in_stop_during_playback_and_listener_bypass():
    from src.aura.core.mic_listener import is_stop_command

    assert is_stop_command("you have windows open Jarvis stop", during_playback=True) is True
    assert is_stop_command("ok stop talking now please", during_playback=True) is True
    assert is_stop_command("Jarvis pause", during_playback=True) is True
    assert is_stop_command("stop the server in server.py", during_playback=True) is False
    assert is_stop_command("you have windows open Jarvis", during_playback=True) is False

    heard: list[str] = []
    fake = _FakeProc([
        "READY\n",
        "HEARD|0.90|some random chatter\n",
        "HEARD|0.92|stop\n",
    ])
    ml = MicListener(
        on_transcript=heard.append,
        process_factory=lambda: fake,  # type: ignore[arg-type,return-value]
        require_wake_word=True,
    )
    ml.set_barge_in(True)
    assert ml.start() is True
    for _ in range(30):
        if heard:
            break
        time.sleep(0.02)
    ml._emit_transcript("speaker said hello Jarvis stop")
    ml.set_barge_in(False)
    ml._emit_transcript("speaker said stop without wake during idle")
    ml.stop()
    assert heard == ["stop", "stop"]


def test_ultron_wake_stop_normalization_and_continuous_pause_defaults() -> None:
    from src.aura.core.mic_listener import (
        extract_wake_command,
        is_stop_command,
        normalize_indian_english_transcript,
    )

    listener = MicListener(lambda _txt: None)
    assert listener._end_silence_blocks == 16
    assert listener._max_utterance_blocks == 300

    assert normalize_indian_english_transcript("Hey altron open chrome") == "Hey Ultron open chrome"
    assert (
        extract_wake_command(
            "Hey Ultron create a project plan for Horizontal Pod Autoscaler",
            require_wake_word=True,
        )
        == "create a project plan for Horizontal Pod Autoscaler"
    )
    assert is_stop_command("Ultron stop talking", during_playback=True) is True


def test_instant_speech_onset_barge_in_echo_guard_and_full_utterance_capture():
    from types import SimpleNamespace
    import numpy as np

    onsets: list[str] = []
    transcripts: list[str] = []
    ve_stops: list[bool] = []

    class _FakeVoiceEngine:
        def __init__(self) -> None:
            self.is_speaking = True
            self.is_actively_playing = True

        def current_playback_level(self) -> float:
            return 0.25 if self.is_actively_playing else 0.0

        def stop(self) -> None:
            ve_stops.append(True)
            self.is_speaking = False
            self.is_actively_playing = False

    fake_ve = _FakeVoiceEngine()

    class _FakeWhisperModel:
        def transcribe(self, audio, **kwargs):
            return [SimpleNamespace(text="check smart helmet readme instead", no_speech_prob=0.05, avg_logprob=-0.2)], None

    def _on_onset():
        onsets.append("onset")
        # Simulate UI immediately clearing barge-in flag once TTS is silenced
        ml.set_barge_in(False)

    ml = MicListener(
        on_transcript=transcripts.append,
        on_speech_onset=_on_onset,
        voice_engine=fake_ve,
        require_wake_word=False,
        end_silence_blocks=5,
        barge_in_rms_floor=0.032,
        barge_in_onset_blocks=2,
    )
    ml.set_barge_in(True)

    quiet_chunk = np.zeros(1600, dtype=np.float32)
    echo_chunk = np.full(1600, 0.025, dtype=np.float32)  # below max(0.032, 0.25*0.16=0.04)
    speech_chunk = np.full(1600, 0.12, dtype=np.float32)  # clearly above echo threshold

    # 1) Speaker echo bleed does not trigger onset or capture
    assert ml.process_audio_chunk(echo_chunk, model=_FakeWhisperModel(), np_mod=np) is None
    assert onsets == []
    assert ve_stops == []

    # 2) Isolated 100ms loud click/pop followed by silence is discarded cleanly
    assert ml.process_audio_chunk(speech_chunk, model=_FakeWhisperModel(), np_mod=np) is None
    assert onsets == []
    assert ml.process_audio_chunk(quiet_chunk, model=_FakeWhisperModel(), np_mod=np) is None
    assert onsets == []
    assert ml._voiced_chunks == []

    # 3) 200ms sustained speech onset stops TTS immediately and captures the FULL query
    ml.process_audio_chunk(speech_chunk, model=_FakeWhisperModel(), np_mod=np)
    assert onsets == []  # 1 block not yet 2 blocks
    ml.process_audio_chunk(speech_chunk, model=_FakeWhisperModel(), np_mod=np)
    assert onsets == ["onset"]
    assert ve_stops == [True]
    assert fake_ve.is_speaking is False
    # Further speech + silence endpoints and emits full query without dropping onset audio
    ml.process_audio_chunk(speech_chunk, model=_FakeWhisperModel(), np_mod=np)
    for _ in range(5):
        out = ml.process_audio_chunk(quiet_chunk, model=_FakeWhisperModel(), np_mod=np)
    assert out == "check smart helmet readme instead"
    assert transcripts == ["check smart helmet readme instead"]


def test_fallback_speech_start_line_triggers_onset_without_dropping_followup():
    assert parse_recognizer_line("SPEECH_START") == ("speech_onset", "")

    onsets: list[str] = []
    transcripts: list[str] = []
    fake = _FakeProc([
        "READY\n",
        "SPEECH_START\n",
        "HEARD|0.91|what is on my desktop\n",
    ])
    ml = MicListener(
        on_transcript=transcripts.append,
        on_speech_onset=lambda: onsets.append("onset"),
        process_factory=lambda: fake,  # type: ignore[arg-type,return-value]
        require_wake_word=False,
    )
    ml.set_barge_in(True)
    assert ml.start() is True
    for _ in range(30):
        if onsets and transcripts:
            break
        time.sleep(0.02)
    ml.stop()
    assert onsets == ["onset"]
    assert transcripts == ["what is on my desktop"]
