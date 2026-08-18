# Decisions

The load-bearing choices, and the evidence behind them. Written down because
each one was verified against this machine rather than assumed, and because the
reasoning is what a future change needs to argue with.

## 1. The build artifact is a timeline file, not Resolve API calls

**Evidence.** Resolve here is 21.0.2 *free* — `CFBundleIdentifier` is
`com.blackmagic-design.DaVinciResolveLite`, where external scripting is a Studio
feature. More decisively, the word "transition" appears **zero times** in
Blackmagic's 101 KB scripting README: there is no API to add a dissolve on any
edition. `SetProperty` exposes `Pan`, `Tilt`, `ZoomX`, `ZoomY`, `Crop*` and
`DynamicZoomEase` — all static, no keyframe setter — so animated zoom ramps are
equally out of reach. `ImportTimelineFromFile` accepts AAF/EDL/XML/FCPXML/DRT/OTIO.

**Therefore.** A file expresses strictly more than the API can, works on the free
edition today, and makes the pipeline a pure function testable without launching
Resolve. auto-editor (5k stars) reached the same conclusion independently: its
`--export resolve` writes `.fcpxml`.

**Cost.** An extra manual step — File → Import → Timeline. Tier 2 removes it for
those who want one click.

## 2. FCPXML 1.10, with FCP7 XML as a fallback

Resolve 21's own export constants stop at `EXPORT_FCPXML_1_10`, so that is the
dialect Resolve itself writes and therefore reads most reliably.

The fallback exists because import fidelity is the one risk that **cannot be
tested from this machine** — Resolve is not scriptable on the free edition, so no
test here can confirm an import. A structurally different second format is the
hedge, and it is the same one auto-editor ships.

## 3. The LLM never sees a frame and never emits a timestamp

It receives transcript text and a structured index, and returns selections and
labels only. Every frame number, cut point and zoom value is deterministic
Python. This is what makes runs reproducible, keeps cost at cents, and lets an
edit answer "why is this cut here?" with a rule id and its inputs.

commerce-os reached the same conclusion for goal decomposition: *"a model that
produces a malformed plan breaks everything downstream."*

## 4. Heat, not speed, is the constraint

The MacBook Air M4 is fanless and throttles under sustained decode within
minutes. So: analysis runs on 480p proxies at a few sampled frames per second
(~100× less decode), proxies use the hardware encoder, nothing is analysed twice,
jobs are chunked and resumable, two workers rather than three, and `--when-idle`
refuses to start on battery.

## 5. Three-way decisions, never two

Every operation comes out `ALLOW`, `DENY` or `REQUIRE_APPROVAL`. The third value
is the point: an operation the planner is unsure about is neither applied on a
coin-flip nor silently discarded. Taken from commerce-os's governance pipeline,
along with its ordering rule — one entry point, fixed order, worst decision wins,
*specifically so that a call cannot satisfy one check and skip another*.

This produced a useful emergent behaviour rather than a special case: operations
inherit the confidence of the measurement behind them, so a poorly-measured style
generates punch-ins below the approval floor and they queue for a human. Nothing
was written to make that happen.

## 6. Confidence and `notes` on every measurement

Some editing characteristics are not recoverable from a rendered video at any
effort — font family, SFX identity, caption animation. A profile that guesses at
those is worse than one that admits the gap, because the planner would apply the
guess with full conviction. So every section carries a confidence and `notes`
says in plain English what was not measured.

The same rule governs similarity scoring: two *unmeasured* sections hold
identical defaults, so scoring them would report a meaningless perfect match.
They are reported as "not comparable" and excluded.

## 7. Integer frames everywhere

FCPXML writes time as exact rationals (`1001/30000s`) because 29.97 has no
decimal representation. Seconds are a float and floats drift: a few hundred cuts
in and clips land a frame apart, which QC then reports as an "accidental gap"
that is really a rounding bug. Every time in the EDL is an integer frame count;
conversion happens once, at write.

## What was rejected

**auto-editor as a dependency.** The plan was to shell out to it for the
first-pass cut. `ffmpeg silencedetect` does that in ~40 lines with no new
dependency and is already installed. auto-editor remains interesting for
*motion*-based cutting, a genuinely different signal.

**Celery, Redis, Docker, SQLAlchemy, LangChain, moviepy, librosa.** A SQLite
table plus a process pool is the right size for one user on one laptop, and
survives restarts because SQLite does.

**A mulberry32 RNG class.** commerce-os needs one because JS has no seedable RNG.
`random.Random` already is one; the idea (a seed stored on the plan) transferred,
the code did not.

**`lightning-whisper-mlx`** (955 stars) — no licence file, so not legally usable.
**`montage-ai`** — closest prior art, but PolyForm Noncommercial.

## Still open

- **FCPXML import fidelity.** Needs ten minutes in Resolve: build a timeline with
  a cut, a dissolve, a punch-in and a marker, export as FCPXML 1.10, commit it to
  `tests/golden/`. That turns the writer's correctness from reasoned to verified.
- **Transcription.** Captions, transcript-driven cuts and B-roll all wait on it.
- **Patch and replan do not compose.** A replan recomputes punch-ins, so an
  earlier zoom patch is not carried forward. Every version is preserved so
  nothing is lost, but composing them means replaying patch history onto each new
  plan.
