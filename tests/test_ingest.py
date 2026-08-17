"""
Ingest tests.

The load-bearing assertion is idempotence: re-ingesting the same folder must do
no work. Every later stage assumes analysis is never repeated for unchanged
media, so if this breaks, the thermal budget on a fanless machine breaks with it.
"""

from ave.database.queries import count_media, get_media_by_hash, list_media
from ave.media.ffmpeg import probe, summarise
from ave.media.hash import content_hash
from ave.media.ingest import find_media, ingest


def test_ingest_indexes_and_probes(sample_video):
    result = ingest(sample_video.parent, proxies=False)

    assert (result.added, result.unchanged, result.failed) == (1, 0, [])
    assert count_media() == 1

    info = list_media()[0]["probe"]["summary"]
    assert 1.8 < info["duration_s"] < 2.2
    assert (info["width"], info["height"]) == (640, 360)
    assert info["has_audio"] is True


def test_reingest_does_zero_work(sample_video):
    first = ingest(sample_video.parent, proxies=True)
    assert first.added == 1
    assert first.proxied == 1, "first pass must actually build the proxy"

    second = ingest(sample_video.parent, proxies=True)
    assert second.added == 0, "unchanged media must not be re-indexed"
    assert second.unchanged == 1
    assert second.proxied == 0, "unchanged media must not be re-encoded"
    assert count_media() == 1, "and must not create a duplicate row"


def test_moved_file_relinks_instead_of_duplicating(sample_video, tmp_path):
    ingest(sample_video.parent, proxies=False)
    original_id = list_media()[0]["id"]

    moved_dir = tmp_path / "moved"
    moved_dir.mkdir()
    moved = moved_dir / "renamed.mp4"
    sample_video.rename(moved)

    result = ingest(moved_dir, proxies=False)

    assert result.added == 0 and result.unchanged == 1
    assert count_media() == 1
    row = list_media()[0]
    assert row["id"] == original_id, "same content is the same media, wherever it lives"
    assert row["path"] == str(moved), "and its path is refreshed"


def test_hash_distinguishes_different_content(sample_video, tmp_path):
    other = tmp_path / "other.bin"
    other.write_bytes(b"x" * 5000)
    assert content_hash(sample_video) != content_hash(other)


def test_hash_is_stable_across_calls(sample_video):
    assert content_hash(sample_video) == content_hash(sample_video)


def test_hash_covers_small_files_whole(tmp_path):
    """Below the two-chunk threshold the whole file is hashed, so a one-byte
    change in the middle is still caught."""
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"A" * 1000 + b"x" + b"A" * 1000)
    b.write_bytes(b"A" * 1000 + b"y" + b"A" * 1000)
    assert content_hash(a) != content_hash(b)


def test_unreadable_media_is_reported_not_raised(tmp_path):
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"this is not a video")

    result = ingest(tmp_path, proxies=False)

    assert result.added == 0
    assert [code for _, code in result.failed] == ["PROBE_FAILED"]
    assert count_media() == 0, "a file we cannot probe must not enter the index"


def test_ingest_never_indexes_its_own_proxies(sample_video):
    """Proxies are .mp4 files. If the scan walked into the proxy directory it
    would index them, then build proxies of proxies, growing the table forever."""
    ingest(sample_video.parent, proxies=True)
    assert count_media() == 1

    ingest(sample_video.parent, proxies=True)
    assert count_media() == 1, "the second pass must not have found its own output"


def test_find_media_ignores_non_media_and_dotfiles(tmp_path):
    (tmp_path / "notes.txt").write_text("hello")
    (tmp_path / ".hidden.mp4").write_bytes(b"")
    (tmp_path / "clip.mov").write_bytes(b"")

    assert [p.name for p in find_media(tmp_path)] == ["clip.mov"]


def test_summarise_keeps_fps_rational(sample_video):
    """29.97 is 30000/1001. Rounding it here is how frame drift reaches a
    timeline three layers later, so the pair is preserved."""
    info = summarise(probe(sample_video))
    assert info["fps_den"] >= 1
    assert info["fps_num"] > 0


def test_media_lookup_by_hash(sample_video):
    ingest(sample_video.parent, proxies=False)
    digest = content_hash(sample_video)

    assert get_media_by_hash(digest, "source") is not None
    assert get_media_by_hash(digest, "reference") is None, "kind is part of the identity"
