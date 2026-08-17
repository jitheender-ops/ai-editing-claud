"""
Research watcher tests.

No network: `_get` is replaced with a lookup table. The fallback chain is the
branchiest logic in the repo and it exists because the watched projects version
three different ways, so it needs a check that pins each path.
"""

import urllib.error

import pytest

from ave.research import nightly


def fake_transport(responses, monkeypatch, calls=None):
    """`responses` maps a URL fragment to a payload, or to an exception to raise."""

    def _get(url, headers=None):
        if calls is not None:
            calls.append(url)
        for fragment, value in responses.items():
            if fragment in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(nightly, "_get", _get)
    monkeypatch.setattr(nightly.time, "sleep", lambda _: None)  # no real backoff in tests


def test_prefers_a_published_release(monkeypatch):
    fake_transport({"releases/latest": {"tag_name": "31.5.0", "published_at": "2026-08-17T00:00:00Z"}}, monkeypatch)
    result, err = nightly.latest_version("owner/repo")
    assert err is None
    assert (result["kind"], result["id"], result["at"]) == ("release", "31.5.0", "2026-08-17")


def test_falls_back_to_a_tag_when_there_is_no_release(monkeypatch):
    """OpenTimelineIO's latest releases are pre-releases, so `releases/latest`
    404s even though the project is perfectly healthy."""
    fake_transport({"tags": [{"name": "v0.18.1"}]}, monkeypatch)
    result, err = nightly.latest_version("owner/repo")
    assert err is None
    assert (result["kind"], result["id"]) == ("tag", "v0.18.1")


def test_falls_back_to_push_time_when_there_are_no_tags(monkeypatch):
    """mlx-examples publishes neither releases nor tags."""
    fake_transport({"tags": [], "api.github.com/repos/owner/repo": {"pushed_at": "2026-04-06T18:56:05Z"}}, monkeypatch)
    result, err = nightly.latest_version("owner/repo")
    assert err is None
    assert (result["kind"], result["at"]) == ("push", "2026-04-06")


def test_a_real_fault_is_reported_not_masked(monkeypatch):
    """A 500 on the release endpoint must not silently fall through to tags and
    report a stale-but-plausible version."""
    fake_transport(
        {"releases/latest": urllib.error.HTTPError("u", 500, "Server Error", {}, None)},
        monkeypatch,
    )
    result, err = nightly.latest_version("owner/repo")
    assert result is None
    assert err == "HTTP 500"


def test_transient_5xx_is_retried(monkeypatch):
    """GitHub 504s on large repos often enough that a single attempt would report
    healthy projects as unreachable."""
    calls = []
    state = {"failures": 1}

    def _get(url, headers=None):
        calls.append(url)
        if state["failures"] > 0:
            state["failures"] -= 1
            raise urllib.error.HTTPError(url, 504, "Gateway Timeout", {}, None)
        return {"tag_name": "1.0", "published_at": "2026-01-01T00:00:00Z"}

    monkeypatch.setattr(nightly, "_get", _get)
    monkeypatch.setattr(nightly.time, "sleep", lambda _: None)

    payload, err = nightly._fetch("https://api.github.com/x/releases/latest")
    assert err is None and payload["tag_name"] == "1.0"
    assert len(calls) == 2, "should have retried exactly once"


def test_404_is_not_retried(monkeypatch):
    calls = []
    fake_transport({}, monkeypatch, calls=calls)  # everything 404s
    payload, err = nightly._fetch("https://api.github.com/x")
    assert (payload, err) == (None, "HTTP 404")
    assert len(calls) == 1, "a 404 is an answer, not a transient fault"


def test_digest_names_the_reason_a_check_failed(monkeypatch):
    """'could not be checked' would hide a transient 504 behind the same words as
    a renamed repository, and only one of those needs the user."""
    fake_transport(
        {"releases/latest": urllib.error.HTTPError("u", 503, "Unavailable", {}, None)},
        monkeypatch,
    )
    monkeypatch.setattr(nightly, "WATCHED", [("owner/repo", "why it matters")])

    lines = nightly.watch_upstream({})
    assert "check failed: HTTP 503" in lines[0]


def test_version_change_is_reported_with_why_it_matters(monkeypatch):
    fake_transport({"releases/latest": {"tag_name": "2.0", "published_at": "2026-08-17T00:00:00Z"}}, monkeypatch)
    monkeypatch.setattr(nightly, "WATCHED", [("owner/repo", "first-pass cut engine")])

    state = {"upstream": {"owner/repo": {"kind": "release", "id": "1.0", "at": "2026-01-01"}}}
    lines = nightly.watch_upstream(state)

    assert "1.0 → 2.0" in lines[0]
    assert "first-pass cut engine" in lines[0]
    assert state["upstream"]["owner/repo"]["id"] == "2.0", "state must advance"


def test_unchanged_version_is_silent(monkeypatch):
    fake_transport({"releases/latest": {"tag_name": "1.0", "published_at": "2026-01-01T00:00:00Z"}}, monkeypatch)
    monkeypatch.setattr(nightly, "WATCHED", [("owner/repo", "why")])

    state = {"upstream": {"owner/repo": {"kind": "release", "id": "1.0", "at": "2026-01-01"}}}
    assert nightly.watch_upstream(state) == [], "no news is no line"
