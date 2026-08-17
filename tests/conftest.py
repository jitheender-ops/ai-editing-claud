import shutil
import subprocess

import pytest

from ave import config
from ave.database.adapter import reset_db_for_tests


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """Every test gets its own database and proxy directory, so nothing touches
    the real ~/Library/Application Support/ave."""
    monkeypatch.setattr(config, "AVE_HOME", tmp_path)
    monkeypatch.setattr(config, "PROXY_DIR", tmp_path / "proxies")
    monkeypatch.setattr(config, "BUILD_DIR", tmp_path / "builds")
    config.ensure_dirs()
    db = reset_db_for_tests(tmp_path / "test.db")
    yield db
    db.close()


@pytest.fixture
def sample_video(tmp_path):
    """A real 2-second video, generated rather than committed."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    out = tmp_path / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=640x360:rate=30:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    return out
