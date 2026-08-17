"""
The nightly research job.

Runs on a GitHub Actions runner, not on the laptop — free on a public repo, and
it works with the lid shut. It commits what it finds back to the repo, so the Mac
picks the results up with `git pull` whenever it is next on.

Three jobs, in order of usefulness:

  watch    upstream tools this project depends on. auto-editor is a public-domain
           CLI we shell out to, so a breaking change matters; Resolve's scripting
           README changing matters even more.
  assets   index freely-licensed audio via Openverse. Only the *manifest* is
           committed — id, url, licence, tags. Bytes go to Google One via rclone
           when a token is configured, and the manifest stays the source of truth
           so the library survives that storage lapsing.
  digest   a dated markdown summary. It is also the heartbeat: GitHub disables
           scheduled workflows after 60 days without repository activity, and
           this commit is what keeps the schedule alive.

Standard library only, on purpose — this must run on a bare runner with no
install step, and every network call is allowed to fail without taking the job
down with it.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

RESEARCH_DIR = Path(__file__).resolve().parent.parent.parent / "research"
STATE_PATH = RESEARCH_DIR / "state.json"
ASSETS_PATH = RESEARCH_DIR / "assets.json"

#: Upstream we depend on, and why a change here matters to us.
WATCHED = [
    ("WyattBlue/auto-editor", "first-pass cut engine + FCPXML dialect reference"),
    ("AcademySoftwareFoundation/OpenTimelineIO", "timeline interchange sanity check"),
    ("Breakthrough/PySceneDetect", "shot boundary detection"),
    ("ml-explore/mlx-examples", "mlx-whisper transcription"),
    ("WheheoHu/pybmd", "Resolve API wrapper reference"),
]

#: Audio we can legally use. Openverse needs no API key, which is why it is first.
ASSET_QUERIES = ["whoosh", "impact", "riser", "transition", "ambience", "click"]

USER_AGENT = "ave-research/0.1 (+https://github.com/)"
TIMEOUT = 20


def _get(url: str, headers: dict[str, str] | None = None) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode())


def _gh_headers() -> dict[str, str]:
    # The runner's token lifts the rate limit from 60/hr to 5000/hr.
    token = os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _fetch(url: str, headers: dict[str, str] | None = None, retries: int = 1) -> tuple[Any, str | None]:
    """Returns (payload, error). Never raises.

    Retries once on 5xx: GitHub returns 504 on `/commits` for large repos often
    enough that a single attempt reports healthy projects as unreachable.
    """
    for attempt in range(retries + 1):
        try:
            return _get(url, headers), None
        except urllib.error.HTTPError as exc:
            if 500 <= exc.code < 600 and attempt < retries:
                time.sleep(2)
                continue
            return None, f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < retries:
                time.sleep(2)
                continue
            return None, f"unreachable ({exc})"
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, "invalid JSON"
    return None, "gave up"


def latest_version(repo: str) -> tuple[dict[str, Any] | None, str | None]:
    """Newest release, then newest tag, then last push time.

    Three steps because these projects version differently: auto-editor cuts
    releases, OpenTimelineIO's latest are pre-releases (so `releases/latest`
    404s), and mlx-examples does not tag at all. Each step is one cheap call —
    `/commits` was the obvious fallback but times out on large repos.
    """
    base = f"https://api.github.com/repos/{repo}"

    release, err = _fetch(f"{base}/releases/latest", _gh_headers())
    if isinstance(release, dict) and release.get("tag_name"):
        return {
            "kind": "release",
            "id": release["tag_name"],
            "at": (release.get("published_at") or "")[:10],
        }, None
    # A 404 means "no published release", which is normal. Anything else is a
    # real fault and must not be masked by falling through.
    if err and err != "HTTP 404":
        return None, err

    tags, err = _fetch(f"{base}/tags?per_page=1", _gh_headers())
    if isinstance(tags, list) and tags:
        return {"kind": "tag", "id": tags[0]["name"], "at": ""}, None
    if err:
        return None, err

    info, err = _fetch(base, _gh_headers())
    if isinstance(info, dict) and info.get("pushed_at"):
        return {"kind": "push", "id": info["pushed_at"], "at": info["pushed_at"][:10]}, None
    return None, err or "no version signal available"


def watch_upstream(state: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    previous = state.setdefault("upstream", {})

    for repo, why in WATCHED:
        current, err = latest_version(repo)
        if current is None:
            # Say *why*. "could not be checked" hides a transient 504 behind the
            # same words as a renamed repository, and only one of those needs you.
            lines.append(f"- `{repo}` — check failed: {err}")
            continue

        was = previous.get(repo, {}).get("id")
        if was is None:
            lines.append(f"- `{repo}` now at **{current['id']}** ({current['kind']}) — first check")
        elif was != current["id"]:
            stamp = f", {current['at']}" if current["at"] else ""
            lines.append(
                f"- `{repo}` **{was} → {current['id']}** ({current['kind']}{stamp})"
                f"  \n  matters because: {why}"
            )
        previous[repo] = current

    return lines


def harvest_assets(assets: dict[str, Any]) -> list[str]:
    """Append newly-seen freely-licensed audio to the manifest. Metadata only."""
    known = assets.setdefault("items", {})
    added = 0

    for query in ASSET_QUERIES:
        params = urllib.parse.urlencode(
            {"q": query, "license_type": "commercial,modification", "page_size": 20}
        )
        try:
            payload = _get(f"https://api.openverse.org/v1/audio/?{params}")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
            continue

        for item in payload.get("results", []):
            key = item.get("id")
            if not key or key in known:
                continue
            known[key] = {
                "title": item.get("title"),
                "url": item.get("url"),
                "licence": item.get("license"),
                "licence_url": item.get("license_url"),
                "creator": item.get("creator"),
                "duration_ms": item.get("duration"),
                "query": query,
                "source": "openverse",
            }
            added += 1

    return [f"- {added} new freely-licensed audio assets indexed ({len(known)} total)"] if added \
        else [f"- no new assets ({len(known)} in the manifest)"]


def main() -> int:
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    assets = json.loads(ASSETS_PATH.read_text()) if ASSETS_PATH.exists() else {}

    upstream_lines = watch_upstream(state)
    asset_lines = harvest_assets(assets)

    today = time.strftime("%Y-%m-%d")
    body = [f"# Research digest — {today}", "", "## Upstream", ""]
    body += upstream_lines or ["- nothing changed"]
    body += ["", "## Assets", ""] + asset_lines
    body += [
        "",
        "---",
        "",
        "_Generated by `ave/research/nightly.py` on a GitHub Actions runner._",
        "_This file is also the heartbeat: without a commit every 60 days GitHub_",
        "_disables the schedule._",
    ]

    (RESEARCH_DIR / f"{today}.md").write_text("\n".join(body) + "\n")
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    ASSETS_PATH.write_text(json.dumps(assets, indent=2, sort_keys=True) + "\n")

    print("\n".join(upstream_lines + asset_lines) or "nothing to report")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
