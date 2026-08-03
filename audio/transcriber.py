"""
Turns the per-user WAV files py-cord's recording sink produces into a single
chronological, speaker-labeled transcript.
"""
import os
from dataclasses import dataclass
from faster_whisper import WhisperModel
from config import WHISPER_MODEL_SIZE

_model: WhisperModel | None = None


def get_model() -> WhisperModel:
    """Lazily load the model once and reuse it (loading is slow, ~seconds to a minute)."""
    global _model
    if _model is None:
        # int8 compute type keeps this usable on a free-tier CPU-only box
        _model = WhisperModel(WHISPER_MODEL_SIZE, device="cpu", compute_type="int8")
    return _model


@dataclass
class TranscriptLine:
    start: float  # seconds from session start
    speaker_label: str
    text: str


def transcribe_speaker_file(wav_path: str, speaker_label: str) -> list[TranscriptLine]:
    model = get_model()
    segments, _info = model.transcribe(wav_path, vad_filter=True)
    lines = []
    for seg in segments:
        text = seg.text.strip()
        if text:
            lines.append(TranscriptLine(start=seg.start, speaker_label=speaker_label, text=text))
    return lines


def build_full_transcript(per_speaker_wavs: dict[str, str]) -> str:
    """
    per_speaker_wavs: {speaker_label: wav_file_path}
    Returns one chronological transcript string, e.g.:
        [00:03:12] Aria (Sarah): I search the chest for traps.
    """
    all_lines: list[TranscriptLine] = []
    for speaker_label, wav_path in per_speaker_wavs.items():
        if os.path.exists(wav_path) and os.path.getsize(wav_path) > 0:
            all_lines.extend(transcribe_speaker_file(wav_path, speaker_label))

    all_lines.sort(key=lambda l: l.start)

    out = []
    for line in all_lines:
        minutes, seconds = divmod(int(line.start), 60)
        hours, minutes = divmod(minutes, 60)
        timestamp = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        out.append(f"[{timestamp}] {line.speaker_label}: {line.text}")

    return "\n".join(out)
