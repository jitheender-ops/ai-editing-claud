# ave — AI editing intelligence system

Study a reference video, measure how it is edited, apply that style to your own
footage, and build the result as a DaVinci Resolve timeline. Every decision is
explainable and every version is kept.

**Status: M0 complete.** Ingest, proxies, the analysis cache, the job queue, the
Resolve capability probe and the nightly research bot all work. No editing yet —
that is M1.

## The three decisions that shape everything

**1. The build artifact is a timeline file, not Resolve API calls.**
Resolve here is 21.0.2 *free*, where external scripting is Studio-only. More
decisively, the scripting API has **no way to add a transition at all** — the
word "transition" does not appear once in Blackmagic's 101 KB scripting README —
and `SetProperty` exposes only static transforms, so animated zoom ramps are out
of reach too. Writing FCPXML expresses strictly more than the API can, works on
the free edition today, and makes the pipeline a pure function you can unit-test
without launching Resolve. auto-editor (5k stars) reached the same conclusion
independently: its `--export resolve` writes `.fcpxml`.

**2. Captions are rendered by ffmpeg + libass, not authored in Fusion.**
ASS subtitles natively do per-word karaoke timing, colour and scale animation.
That is ~100 lines and pixel-identical every run, against a fragile Fusion
integration that is hard to verify.

**3. The LLM never sees a frame and never emits a timestamp.**
It receives transcript text and a structured media index, and returns selections
and labels only. Every frame number, cut point and zoom value is computed by
deterministic Python. That is what makes runs reproducible and lets every edit
answer "why is this cut here?" with a rule id and its inputs.

## Running it

```bash
uv run ave doctor                      # what this machine can and cannot do
uv run ave ingest ~/Movies/MyFootage   # index, hash, build 480p proxies
uv run ave media                       # what is indexed
uv run pytest -q                       # 38 tests, no network, no sleeping
```

Re-running `ingest` on an unchanged folder does no work — that is asserted in the
test suite, and it is what keeps a fanless MacBook Air out of thermal throttle.

## Keeping the Mac cool

The M4 Air has no fan, so heat is the real constraint rather than speed. Analysis
runs on 480p proxies at a few sampled frames per second, proxies are encoded on
the hardware encoder (`h264_videotoolbox`), nothing is ever analysed twice, jobs
are chunked and resumable, and `--when-idle` refuses to start heavy work on
battery.

## The nightly research bot

`.github/workflows/research.yml` runs on GitHub's runners — free and unlimited on
a public repo — and commits its findings back, so the Mac collects them with
`git pull`. It watches the upstream tools this project depends on and indexes
freely-licensed audio.

Only the *manifest* is committed; asset bytes go to Google One via rclone when a
token is configured. That split is deliberate: `research/assets.json` stays the
source of truth, so if the storage lapses the library costs a re-download rather
than being lost.

Two things worth knowing before wiring the sync:

- Use an **rclone OAuth refresh token for your own account**, not a GCP service
  account. A service account cannot spend Google One quota — the uploader owns
  the file, and a service account only has its own 15 GB.
- GitHub disables scheduled workflows after 60 days of repository inactivity. The
  digest commit is the heartbeat that prevents that.

## Layout

```
ave/lib/         ids, seeded rng, structured logs, power policy
ave/database/    narrow adapter (the Postgres seam), schema, all SQL
ave/jobs/        durable queue: backoff, dead-letter, resumable
ave/media/       ffmpeg, content hashing, ingest
ave/executors/   Resolve capability probe; FCPXML writer lands in M1
ave/research/    the nightly bot
```

Several primitives — the queue, the database seam, the governance idea that
becomes the op validation gate in M1 — are ported from the sibling `commerce-os`
project rather than rewritten.

## What's next (M1)

auto-editor first pass → word-level transcript → cut planner honouring the
style's dead-air tolerance → Edit Decision List → validation gate → FCPXML you
import into Resolve.
