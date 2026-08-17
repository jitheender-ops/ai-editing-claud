"""
Ingest: walk a folder, identify media, build proxies, record it.

The contract that matters is idempotence. Re-ingesting the same folder must do
essentially no work — hash, cache hit, move on — because the whole pipeline is
built on the assumption that analysis is never repeated for unchanged media. The
M0 test asserts exactly that.

Proxy building is the expensive part, so it is skipped when the proxy already
exists and can be deferred entirely with `proxies=False` for a fast index pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ave import config
from ave.config import ensure_dirs
from ave.database.queries import upsert_media
from ave.lib.log import log, warn
from ave.media.ffmpeg import MediaError, make_proxy, probe, summarise
from ave.media.hash import content_hash

VIDEO_SUFFIXES = {".mov", ".mp4", ".m4v", ".mkv", ".avi", ".mxf", ".mts", ".m2ts", ".webm"}
AUDIO_SUFFIXES = {".wav", ".aiff", ".aif", ".mp3", ".m4a", ".flac", ".aac", ".mka"}


@dataclass
class IngestResult:
    added: int = 0
    unchanged: int = 0
    proxied: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)  # (path, error code)

    @property
    def scanned(self) -> int:
        return self.added + self.unchanged + len(self.failed)


def find_media(root: Path | str) -> list[Path]:
    """Media files under `root`, excluding anything this tool itself wrote.

    Skipping our own output directories is not cosmetic: proxies are .mp4 files,
    so ingesting a folder that contains them would index the proxies, then build
    proxies *of* the proxies, and inflate the media table on every run. The guard
    lives here so every caller gets it.
    """
    root = Path(root).expanduser().resolve()
    if root.is_file():
        return [root]

    ours = [d.resolve() for d in (config.PROXY_DIR, config.BUILD_DIR)]
    suffixes = VIDEO_SUFFIXES | AUDIO_SUFFIXES

    def is_ours(path: Path) -> bool:
        return any(d == path or d in path.parents for d in ours)

    return sorted(
        p
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix.lower() in suffixes
        and not p.name.startswith(".")
        and not is_ours(p)
    )


def ingest(
    root: Path | str, *, kind: str = "source", proxies: bool = True
) -> IngestResult:
    ensure_dirs()
    result = IngestResult()

    for path in find_media(root):
        try:
            digest = content_hash(path)
            probed = probe(path)
        except MediaError as exc:
            warn("ingest.skip", path=str(path), code=exc.code)
            result.failed.append((str(path), exc.code))
            continue
        except OSError as exc:
            warn("ingest.skip", path=str(path), code="UNREADABLE", reason=str(exc))
            result.failed.append((str(path), "UNREADABLE"))
            continue

        record = {**probed, "summary": summarise(probed)}
        proxy = None

        # Only video needs a visual proxy, and only if one isn't already built.
        if proxies and path.suffix.lower() in VIDEO_SUFFIXES:
            try:
                built, encoded = make_proxy(path, digest)
                proxy = str(built)
                if encoded:
                    result.proxied += 1
            except MediaError as exc:
                warn("ingest.proxy_failed", path=str(path), code=exc.code)

        media_id, created = upsert_media(
            path=str(path), content_hash=digest, kind=kind, probe=record, proxy_path=proxy
        )
        if created:
            result.added += 1
            log("ingest.added", media=media_id, path=path.name)
        else:
            result.unchanged += 1

    log(
        "ingest.done",
        scanned=result.scanned,
        added=result.added,
        unchanged=result.unchanged,
        proxied=result.proxied,
        failed=len(result.failed),
    )
    return result
