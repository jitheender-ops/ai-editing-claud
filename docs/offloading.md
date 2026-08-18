# Offloading work to a second machine

Reference hardware for this note: an i5-10400F (6 cores / 12 threads, AVX2),
16 GB DDR4, GT 730 4 GB, liquid cooled, 1 TB storage.

## What that machine is and is not good for

**The GT 730 is not usable for this work.** It is Kepler, compute capability 3.5.
Modern CUDA and PyTorch dropped that generation years ago, and at roughly 700
GFLOPS it would lose to the CPU beside it even if the toolchains still supported
it. Nothing here should be pointed at it.

**The CPU plus the liquid cooling is the real asset**, and specifically because
of what the Mac cannot do. A fanless MacBook Air throttles within minutes of
sustained load — that constraint shapes this entire codebase. A liquid-cooled
desktop holds 100% on 12 threads indefinitely. So the split is not "fast machine,
slow machine"; it is **interactive work on the laptop, long unattended batches on
the desktop.**

whisper.cpp is the natural fit: it is a CPU C++ binary with AVX2 paths and no
CUDA requirement, so an i5-10400F runs it well.

## How the transfer works

No shared database, no runner, no network service. **The analysis cache is keyed
by content hash**, not by path or machine, so a transcript computed anywhere is
valid everywhere for the same bytes.

On the desktop:

```bash
ave transcribe big-interview.mov --threads 12
ave analysis-export big-interview.mov -o interview.analysis.json
```

Copy that JSON back (it is small — text, not media), then on the laptop:

```bash
ave analysis-import interview.analysis.json --media big-interview.mov
```

The laptop now has the transcript in its cache and will never recompute it.
`--media` is only needed the first time, before that file has been indexed
locally.

Import **verifies the content hash** before attaching anything. Silently
attaching a transcript to the wrong footage would be close to impossible to
notice later, so a mismatch is refused rather than warned about.

## Setting the desktop up

Same three pieces as the laptop, all cross-platform:

```bash
# Python 3.12, ffmpeg, whisper.cpp
uv python install 3.12
ave fetch-model --name base.en     # 124 MB, once
```

Model sizes, if the desktop is doing the heavy lifting: `base.en` (124 MB) is
enough for cutting and captions; `small.en` (466 MB) is noticeably better on
accented or noisy speech and still comfortable on 16 GB.

## What not to offload

**Not the interactive loop.** `ave edit`, `ave tweak` and `ave compare` are
sub-second and want to be where you are.

**Not proxies, if the Mac is to hand.** `h264_videotoolbox` is hardware-encoded
and costs the Air almost nothing — a desktop doing it in software on CPU is
slower *and* hotter.

**Not the research bot.** It already runs on GitHub's machines for free, so
moving it to hardware you pay to power would be a step backwards. The one reason
to reconsider is if it ever needs to analyse video rather than metadata, since a
self-hosted runner would lift both the time limit and the storage ceiling.
