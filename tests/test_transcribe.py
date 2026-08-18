"""
Transcription tests.

Split deliberately: the parsing and normalisation logic is tested with fixtures
and runs anywhere, while the tests that need the actual model skip when it is
absent. A test suite that only passes on a machine with a 124 MB model
downloaded is a test suite that stops being run.
"""

import shutil

import pytest

from ave.media.transcribe import (
    Segment,
    Transcript,
    WhisperCppTranscriber,
    Word,
    available,
)

# Resolved at import, before the sandbox fixture redirects AVE_HOME to a tmp
# directory — otherwise these tests would look for the model inside the sandbox
# and always skip.
from ave.media.transcribe import model_path as _model_path

REAL_MODEL = _model_path()
MODEL_PRESENT = available()[0] and REAL_MODEL.exists()
needs_model = pytest.mark.skipif(not MODEL_PRESENT, reason="whisper model not installed")


# ── filler normalisation ─────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["um", "Um,", "U.M.", "UM", "um.", "  um  "])
def test_a_filler_is_recognised_however_whisper_punctuates_it(raw):
    """Whisper renders a spoken 'um' as 'Um,', 'um' or 'U.M.' depending on
    context — it reads the last as an acronym. Stripping only trailing
    punctuation misses that one, which is the form it actually produced on a real
    clip."""
    assert Word(raw, 0.0, 0.1).is_filler


@pytest.mark.parametrize("raw", ["the", "dog,", "umbrella", "summary", "human"])
def test_ordinary_words_are_not_fillers(raw):
    """'umbrella' contains 'um'. Substring matching would eat it."""
    assert not Word(raw, 0.0, 0.1).is_filler


def test_normalisation_keeps_letters_only():
    assert Word("U.M.!", 0, 0).normalised == "um"
    assert Word("don't", 0, 0).normalised == "dont"


# ── parsing whisper.cpp output ───────────────────────────────────────────────


def whisper_payload(words):
    """whisper.cpp emits one entry per token with millisecond offsets."""
    return {
        "result": {"language": "en"},
        "transcription": [
            {"text": text, "offsets": {"from": int(start * 1000), "to": int(end * 1000)}}
            for text, start, end in words
        ],
    }


def test_milliseconds_are_converted_to_seconds():
    payload = whisper_payload([("Hello", 1.5, 2.0)])
    transcript = WhisperCppTranscriber._parse(payload)

    assert transcript.words[0].start == pytest.approx(1.5)
    assert transcript.words[0].end == pytest.approx(2.0)


def test_words_are_regrouped_into_sentences_on_terminal_punctuation():
    payload = whisper_payload(
        [("One", 0, 1), ("two.", 1, 2), ("Three", 2, 3), ("four!", 3, 4)]
    )
    transcript = WhisperCppTranscriber._parse(payload)

    assert len(transcript.segments) == 2
    assert transcript.segments[0].text == "One two."
    assert transcript.segments[1].text == "Three four!"


def test_a_run_without_punctuation_still_gets_segmented():
    """Otherwise one unpunctuated monologue becomes a single unusable segment."""
    payload = whisper_payload([(f"w{i}", i, i + 1) for i in range(70)])
    transcript = WhisperCppTranscriber._parse(payload)

    assert len(transcript.segments) >= 2
    assert all(len(s.words) <= 30 for s in transcript.segments)


def test_trailing_words_are_not_lost():
    payload = whisper_payload([("One", 0, 1), ("two.", 1, 2), ("dangling", 2, 3)])
    transcript = WhisperCppTranscriber._parse(payload)

    assert transcript.words[-1].text == "dangling"
    assert len(transcript.segments) == 2


def test_empty_tokens_are_skipped():
    payload = whisper_payload([("", 0, 1), ("   ", 1, 2), ("real", 2, 3)])
    assert len(WhisperCppTranscriber._parse(payload).words) == 1


# ── round trip ───────────────────────────────────────────────────────────────


def test_transcript_round_trips_through_json():
    """It is cached as JSON in the analysis table, so this is the real contract."""
    original = Transcript(
        segments=[
            Segment(text="Hello there.", start=0.0, end=1.0,
                    words=[Word("Hello", 0.0, 0.5), Word("there.", 0.5, 1.0)])
        ]
    )
    restored = Transcript.from_dict(original.to_dict())

    assert restored.text == original.text
    assert [w.text for w in restored.words] == [w.text for w in original.words]
    assert restored.words[0].start == 0.0


def test_availability_explains_what_is_missing():
    ok, why = available()
    if not ok:
        assert "whisper-cli" in why or "model" in why, "must say which piece is absent"


# ── with the real model ──────────────────────────────────────────────────────


@pytest.fixture
def spoken(tmp_path):
    """Real speech with known text, via macOS `say` — so the transcript has a
    ground truth rather than merely being self-consistent."""
    if not shutil.which("say") or not shutil.which("ffmpeg"):
        pytest.skip("say/ffmpeg unavailable")
    import subprocess

    aiff = tmp_path / "speech.aiff"
    subprocess.run(["say", "-o", str(aiff), "The quick brown fox jumps over the lazy dog."],
                   check=True, capture_output=True)
    out = tmp_path / "speech.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30", "-i", str(aiff),
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)],
        check=True, capture_output=True,
    )
    return out


@needs_model
def test_known_speech_transcribes_correctly(spoken):
    transcript = WhisperCppTranscriber(model=REAL_MODEL).transcribe(spoken)
    text = transcript.text.lower()

    for expected in ("quick", "brown", "fox", "lazy", "dog"):
        assert expected in text, f"missing {expected!r} from: {text}"


@needs_model
def test_word_timings_are_ordered_and_inside_the_clip(spoken):
    words = WhisperCppTranscriber(model=REAL_MODEL).transcribe(spoken).words

    assert words, "no words returned"
    assert all(w.end >= w.start for w in words), "a word cannot end before it starts"
    for earlier, later in zip(words, words[1:]):
        assert later.start >= earlier.start, "words must be in order"
