# ave — AI editing intelligence system

Study a reference video, measure how it is edited, apply that style to your own
footage, and build the result as a DaVinci Resolve timeline. Every decision is
explainable and every version is kept.

**Status: the core loop is closed.** Measure a reference video's editing style,
apply it to your footage, get a Resolve timeline, and adjust it in plain English
— with every version preserved.

```
reference video -> Edit DNA -> style library -> your footage -> FCPXML -> Resolve
                                                     ^                       |
                                                     +---- "make it faster" -+
```

Working: ingest and proxies, the content-hash analysis cache, the durable job
queue, the Resolve capability probe, the nightly research bot, shot detection,
motion estimation, reference analysis into an Edit DNA, the style library, the
silence-driven cut planner, punch-in generation, the operation validation gate,
quality control, the FCPXML writer, style similarity scoring, the in-Resolve
companion script, an FCP7 XML fallback, and natural-language feedback. 166 tests,
none touching the network and none sleeping.

Cuts are transcript-aware: fillers are excised, no cut lands mid-word, and cuts
near a sentence end are pulled onto it. On a real clip, silence alone kept a
fragment of an "um" as its own clip and truncated "dog" mid-word; with the
transcript both are fixed.

Not yet: captions and B-roll.

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
uv run ave doctor                             # what this machine can and cannot do
uv run ave ingest ~/Movies/MyFootage          # index, hash, build 480p proxies
uv run ave reference ref.mp4 --name fast-tech # measure a style from a reference
uv run ave edit talk.mov --style fast-tech --transcribe   # apply it, write a Resolve timeline
uv run ave tweak talk "reduce zooms by 50%"   # adjust, as a new version
uv run ave compare fast-tech slow-doc         # how alike are two styles?
uv run ave install-resolve-script             # tier 2: build from inside Resolve
uv run ave plans talk                         # every version, never overwritten
uv run ave approvals                          # what the planner wasn't sure about
uv run ave edit talk.mov --format both        # FCPXML + FCP7 fallback
uv run pytest -q                              # 227 tests
```

A real run:

```
3 clips  10.0s -> 6.5s  (3.5s removed, 65% kept)

quality report (confidence 1.00)
  clean

wrote ~/Library/Application Support/ave/builds/demo_v001.fcpxml
import it with:  Resolve -> File -> Import -> Timeline
```

Re-running `ingest` on an unchanged folder does no work — that is asserted in the
test suite, and it is what keeps a fanless MacBook Air out of thermal throttle.

### Nothing is silently applied or silently dropped

Every operation passes one gate — `schema → media → bounds → conflict →
confidence → autonomy`, worst decision wins — and comes out `ALLOW`, `DENY` or
`REQUIRE_APPROVAL`, carrying the full reason trail that produced that verdict.
Uncertain operations go to `ave approvals` rather than being applied on a
coin-flip or quietly discarded. Only `ALLOW`-ed operations reach the timeline
file.

The same idea runs through the failure paths. An empty plan does not say "no
clips found"; it measures the audio and tells you *why*:

> The entire source is silent: integrated loudness is -70 LUFS (anything below
> about -60 is digital silence). This footage has no audio to cut on.

And each cut's reason is written into the Resolve timeline as a marker, so it is
readable while scrubbing:

> Kept 2.26s of speech; 1.74s of silence removed before it (style dead-air
> tolerance 0.35s)

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
ave/media/       ffmpeg, content hashing, silence/loudness, ingest
ave/style/       Edit DNA — a style as numbers, each with a confidence
ave/plan/        the EDL schema and the deterministic cut planner
ave/policies/    the one validation gate every operation passes
ave/qc/          quality control on a plan, before anything is written
ave/executors/   FCPXML and FCP7 writers; the in-Resolve plan; capability probe
ave/research/    the nightly bot
```

Several primitives — the queue, the database seam, and the governance pipeline
that became the operation validation gate — are ported from the sibling
`commerce-os` project rather than rewritten.

## Two deviations worth knowing

**auto-editor is not used.** The plan was to shell out to it for the first-pass
cut. `ffmpeg silencedetect` turned out to do that job in about forty lines with
no new dependency, and it is already installed. auto-editor stays interesting for
its *motion*-based cutting, which is a genuinely different signal, but it is not
needed to make cuts from silence.

**The Edit DNA is honest about what it has not measured.** `default_dna()`
returns neutral numbers with zero confidence and a `notes` list saying exactly
that. Nothing in the system has analysed a reference video yet, so nothing
pretends to have.

## What's next (M3, and it needs you)

The highest-severity open risk is FCPXML import fidelity, and it needs ten
minutes at the keyboard rather than more code: build a short timeline in Resolve
containing a cut, a dissolve, a punch-in and a marker, export it as FCPXML 1.10,
and drop it in `tests/golden/`. That pins Resolve's real dialect and turns the
writer's correctness from reasoned to verified.
