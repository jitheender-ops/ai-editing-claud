"""
Transcription, with word-level timestamps.

whisper.cpp rather than a Python ML stack, for three reasons that all matter
here:

  * It is a C++ binary with GGML weights — no PyTorch, no CUDA, no 2 GB of
    wheels, and nothing to break when a dependency moves.
  * It runs well on CPU. That makes the same pipeline usable on an ordinary
    desktop, which is the difference between transcription being a Mac-only
    feature and being something that can be offloaded to a machine that can hold
    100% CPU indefinitely — which a fanless Air cannot.
  * Word-level timestamps come out of it directly, and those are the whole point.
    Sentence timings are enough to cut on; only word timings can drive karaoke
    captions or pick the exact frame an emphasis lands on.

`Transcriber` is a Protocol with one implementation. That is not speculative
generality: the transcript is the input to captions, filler-word removal and
B-roll matching, and the model that produces it is the piece most likely to be
swapped as better ones appear.

Audio is extracted to 16 kHz mono WAV first because that is what the model wants;
handing it anything else makes whisper.cpp resample internally and, on some
builds, silently mistime the result.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ave import config
from ave.media.ffmpeg import MediaError, require_ffmpeg

ANALYZER_VERSION = "whisper.cpp-1.9-base.en"

#: Filler words removed when a style asks for it. Deliberately short: an
#: aggressive list starts eating "like" as a verb and "so" as a conjunction.
FILLERS = {"um", "uh", "erm", "uhh", "umm", "hmm", "mmm", "ah", "eh"}


@dataclass
class Word:
    text: str
    start: float
    end: float
    confidence: float = 1.0

    @property
    def normalised(self) -> str:
        """Letters only, lowercased.

        Whisper punctuates fillers unpredictably — it renders a spoken "um" as
        "Um,", "um" or "U.M." depending on context, reading the last as an
        acronym. Stripping only trailing punctuation misses that, so every
        non-letter goes.
        """
        return "".join(c for c in self.text if c.isalpha()).lower()

    @property
    def is_filler(self) -> bool:
        return self.normalised in FILLERS


@dataclass
class Segment:
    text: str
    start: float
    end: float
    words: list[Word] = field(default_factory=list)


@dataclass
class Transcript:
    segments: list[Segment] = field(default_factory=list)
    language: str = "en"
    model: str = ANALYZER_VERSION

    @property
    def words(self) -> list[Word]:
        return [word for segment in self.segments for word in segment.words]

    @property
    def text(self) -> str:
        return " ".join(segment.text.strip() for segment in self.segments).strip()

    def fillers(self) -> list[Word]:
        return [word for word in self.words if word.is_filler]

    def to_dict(self) -> dict:
        return {
            "language": self.language,
            "model": self.model,
            "segments": [
                {
                    "text": s.text, "start": s.start, "end": s.end,
                    "words": [
                        {"text": w.text, "start": w.start, "end": w.end, "confidence": w.confidence}
                        for w in s.words
                    ],
                }
                for s in self.segments
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Transcript":
        return cls(
            language=data.get("language", "en"),
            model=data.get("model", ANALYZER_VERSION),
            segments=[
                Segment(
                    text=s["text"], start=s["start"], end=s["end"],
                    words=[Word(**w) for w in s.get("words", [])],
                )
                for s in data.get("segments", [])
            ],
        )


class Transcriber(Protocol):
    def transcribe(self, path: Path | str) -> Transcript: ...


def model_path(name: str = "ggml-base.en.bin") -> Path:
    return config.AVE_HOME / "models" / name


def available() -> tuple[bool, str]:
    """Whether transcription can run, and why not if it cannot."""
    if not shutil.which("whisper-cli"):
        return False, "whisper-cli not found — install with: brew install whisper-cpp"
    model = model_path()
    if not model.exists():
        return False, f"model missing — run: ave fetch-model  (expected at {model})"
    return True, "ready"


def extract_audio(source: Path | str, out: Path) -> Path:
    """16 kHz mono WAV, which is what the model expects."""
    require_ffmpeg()
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(out)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise MediaError(
            f"could not extract audio from {source}: {result.stderr.strip()[:300]}",
            code="AUDIO_EXTRACT_FAILED",
        )
    return out


class WhisperCppTranscriber:
    """whisper.cpp via its `whisper-cli` binary."""

    def __init__(self, model: Path | None = None, threads: int | None = None) -> None:
        self.model = Path(model) if model else model_path()
        self.threads = threads

    def transcribe(self, path: Path | str) -> Transcript:
        ok, why = available()
        if not ok and not self.model.exists():
            raise MediaError(why, code="TRANSCRIBE_UNAVAILABLE")

        with tempfile.TemporaryDirectory() as tmp:
            wav = extract_audio(path, Path(tmp) / "audio.wav")
            stem = Path(tmp) / "out"
            command = [
                "whisper-cli", "-m", str(self.model), "-f", str(wav),
                # One token per segment plus word splitting is how whisper.cpp is
                # asked for word-level timing; without it the finest granularity
                # is a whole sentence, which cannot drive captions.
                "--max-len", "1", "--split-on-word",
                "--output-json", "--output-file", str(stem),
                "--no-prints",
            ]
            if self.threads:
                command += ["-t", str(self.threads)]

            result = subprocess.run(command, capture_output=True, text=True)
            produced = stem.with_suffix(".json")
            if result.returncode != 0 or not produced.exists():
                raise MediaError(
                    f"whisper-cli failed: {(result.stderr or result.stdout).strip()[:300]}",
                    code="TRANSCRIBE_FAILED",
                )
            return self._parse(json.loads(produced.read_text()))

    @staticmethod
    def _parse(payload: dict) -> Transcript:
        """whisper.cpp emits one entry per token when --max-len 1 is used, so the
        tokens *are* the words; they are regrouped into sentences here."""
        words: list[Word] = []
        for item in payload.get("transcription", []):
            text = (item.get("text") or "").strip()
            if not text:
                continue
            offsets = item.get("offsets") or {}
            # whisper.cpp reports milliseconds.
            words.append(
                Word(text=text, start=offsets.get("from", 0) / 1000.0, end=offsets.get("to", 0) / 1000.0)
            )

        segments: list[Segment] = []
        current: list[Word] = []
        for word in words:
            current.append(word)
            if word.text.endswith((".", "!", "?")) or len(current) >= 30:
                segments.append(
                    Segment(
                        text=" ".join(w.text for w in current),
                        start=current[0].start, end=current[-1].end, words=list(current),
                    )
                )
                current = []
        if current:
            segments.append(
                Segment(
                    text=" ".join(w.text for w in current),
                    start=current[0].start, end=current[-1].end, words=list(current),
                )
            )

        language = (payload.get("result") or {}).get("language", "en")
        return Transcript(segments=segments, language=language)


def default_transcriber() -> Transcriber:
    return WhisperCppTranscriber()
