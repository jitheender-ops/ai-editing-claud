"""
Command line interface.

The CLI is the primary surface and stays so even after the web UI arrives: it is
the testable one, and every capability has to exist here before it gets a button.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

import typer

from ave import config
from ave.database.adapter import get_db
from ave.database.queries import count_media, list_media
from ave.lib import power
from ave.lib.ids import new_run_id
from ave.lib.log import get_run_id, set_run_id
from ave.media.ffmpeg import summarise

app = typer.Typer(add_completion=False, help="AI editing intelligence system.")


@app.callback()
def _main() -> None:
    set_run_id(new_run_id())


@app.command()
def doctor() -> None:
    """Report what this machine can and cannot do, and why."""
    from ave.executors.resolve_api import probe

    config.ensure_dirs()
    ok = True

    typer.echo("environment")
    typer.echo(f"  python           {sys.version.split()[0]}")
    if sys.version_info[:2] != (3, 12):
        typer.echo("    ! expected 3.12 — fusionscript.so will not load on 3.14")
        ok = False

    for binary, required in (("ffmpeg", True), ("ffprobe", True), ("auto-editor", False),
                             ("rclone", False), ("gh", False)):
        found = shutil.which(binary)
        if found:
            typer.echo(f"  {binary:<16} {found}")
        else:
            typer.echo(f"  {binary:<16} MISSING{'' if required else '  (optional)'}")
            if required:
                ok = False

    from ave.media.transcribe import available as transcribe_available

    ready, why = transcribe_available()
    typer.echo(f"  transcription    {'ready' if ready else 'unavailable'}")
    if not ready:
        typer.echo(f"    {why}")

    typer.echo("\nstorage")
    typer.echo(f"  home             {config.AVE_HOME}")
    typer.echo(f"  database         {config.DB_PATH}")
    typer.echo(f"  media indexed    {count_media()}")

    typer.echo("\npower")
    typer.echo(f"  on AC power      {power.on_ac_power()}")
    battery = power.battery_percent()
    typer.echo(f"  battery          {battery}%" if battery is not None else "  battery          n/a")
    typer.echo(f"  workers          {power.WORKERS} (fanless Air: heat, not speed, is the limit)")

    status = probe()
    typer.echo("\nDaVinci Resolve")
    typer.echo(f"  app installed    {status.app_installed}")
    typer.echo(f"  running          {status.running}")
    typer.echo(f"  tier 1 (file)    True   — always available, the primary path")
    typer.echo(f"  tier 2 (in-app)  {status.tier2_available}")
    typer.echo(f"  tier 3 (extern)  {status.tier3_available}")
    typer.echo(f"  -> {status.summary}")
    typer.echo(f"  {status.detail}")

    typer.echo("\nok" if ok else "\nproblems found — see the lines marked !")
    raise typer.Exit(0 if ok else 1)


@app.command()
def ingest(
    path: Path = typer.Argument(..., help="File or folder of media to index."),
    kind: str = typer.Option("source", help="source | reference | asset"),
    proxies: bool = typer.Option(True, help="Build 480p analysis proxies."),
    when_idle: bool = typer.Option(
        False, "--when-idle", help="Refuse to run on battery (thermal policy)."
    ),
) -> None:
    """Index media, hash it, and build analysis proxies. Safe to re-run."""
    from ave.media.ingest import ingest as run_ingest

    if when_idle and not power.on_ac_power():
        typer.echo("on battery — skipping (--when-idle)")
        raise typer.Exit(0)

    get_db()
    result = run_ingest(path, kind=kind, proxies=proxies)

    typer.echo(
        f"scanned {result.scanned}  added {result.added}  unchanged {result.unchanged}  "
        f"proxied {result.proxied}  failed {len(result.failed)}"
    )
    for failed_path, code in result.failed:
        typer.echo(f"  ! {code}  {failed_path}")


@app.command("media")
def media_list(kind: str = typer.Option(None, help="Filter by kind.")) -> None:
    """List indexed media."""
    get_db()
    rows = list_media(kind)
    if not rows:
        typer.echo("nothing indexed yet — try: ave ingest <folder>")
        return
    for row in rows:
        info = row["probe"].get("summary") or summarise(row["probe"])
        fps = info["fps_num"] / info["fps_den"] if info["fps_den"] else 0
        typer.echo(
            f"{row['id']}  {info['duration_s']:>7.1f}s  {info['width']}x{info['height']}"
            f"  {fps:>6.2f}fps  {row['kind']:<9}  {Path(row['path']).name}"
        )


@app.command()
def edit(
    path: Path = typer.Argument(..., help="Media file to edit."),
    project: str = typer.Option(None, help="Project name. Defaults to the file's name."),
    style: str = typer.Option(None, help="Style to apply. Defaults to neutral defaults."),
    noise: float = typer.Option(-30.0, help="Silence threshold in dBFS. Lower = stricter."),
    seed: int = typer.Option(0, help="Seed. The same seed reproduces the plan exactly."),
    autonomy: int = typer.Option(2, help="1 propose | 2 build and flag | 3 autonomous"),
    fmt: str = typer.Option(
        "fcpxml", "--format", help="fcpxml (default) | fcp7 (older, cruder, very well worn) | both"
    ),
) -> None:
    """Plan a cut from silence, validate it, and write a Resolve-importable timeline."""
    from ave.database.queries import (
        get_style, next_plan_version, save_approvals, save_plan, upsert_project,
    )
    from ave.executors.fcp7 import write_fcp7
    from ave.executors.fcpxml import write_fcpxml
    from ave.executors.resolve_plan import write_plan
    from ave.media.ffmpeg import probe, summarise
    from ave.media.hash import content_hash
    from ave.plan.planner import PlanInputs, plan_cut
    from ave.policies.validate import validate_edl
    from ave.qc.validate import run_qc
    from ave.style.models import EditDNA, default_dna

    path = path.expanduser().resolve()
    if not path.exists():
        typer.echo(f"no such file: {path}")
        raise typer.Exit(1)

    config.ensure_dirs()
    get_db()

    dna = default_dna()
    if style:
        row = get_style(style)
        if not row:
            typer.echo(f"unknown style: {style}  (try `ave styles`)")
            raise typer.Exit(1)
        dna = EditDNA.model_validate(row["dna"])

    name = project or path.stem
    project_id = upsert_project(name, footage_dir=str(path.parent), autonomy=autonomy)
    version = next_plan_version(project_id)

    probed = probe(path)
    probed["summary"] = summarise(probed)

    typer.echo(f"planning {path.name} with style '{dna.style_name}' ...")
    edl = plan_cut(
        PlanInputs(
            media_id=content_hash(path),
            path=str(path),
            probe=probed,
            dna=dna,
            project=name,
            version=version,
            seed=seed,
            noise_db=noise,
        )
    )

    validation = validate_edl(edl, dna, autonomy=autonomy)
    if not validation.ok:
        for clip_id, message in validation.clip_errors:
            typer.echo(f"  ERROR  {clip_id}: {message}")
        raise typer.Exit(1)

    qc = run_qc(edl)

    plan_id = save_plan(
        project_id=project_id,
        version=version,
        seed=seed,
        edl=edl.model_dump(mode="json"),
        qc=qc.to_dict(),
        origin="generate",
        run_id=get_run_id(),
    )
    if pending := edl.pending_approval():
        save_approvals(plan_id, [op.model_dump(mode="json") for op in pending])

    summary = edl.summary
    typer.echo(
        f"\n{summary.clip_count} clips  "
        f"{summary.source_duration_s:.1f}s -> {summary.output_duration_s:.1f}s  "
        f"({summary.removed_s:.1f}s removed, {summary.kept_ratio:.0%} kept)"
    )
    typer.echo(f"\nquality report (confidence {qc.confidence:.2f})")
    typer.echo(qc.render())

    if not qc.ok:
        typer.echo("\nnot writing a timeline while there are errors")
        raise typer.Exit(1)

    stem = config.BUILD_DIR / f"{name}_v{version:03d}"
    written = []
    if fmt in ("fcpxml", "both"):
        written.append(write_fcpxml(edl, stem.with_suffix(".fcpxml")))
    if fmt in ("fcp7", "both"):
        written.append(write_fcp7(edl, stem.with_suffix(".xml")))
    if not written:
        typer.echo(f"unknown --format {fmt!r}; expected fcpxml, fcp7 or both")
        raise typer.Exit(1)
    write_plan(edl, config.BUILD_DIR / f"{name}_v{version:03d}.plan.json")

    typer.echo("")
    for item in written:
        typer.echo(f"wrote {item}")
    typer.echo("\nimport it with:  Resolve -> File -> Import -> Timeline")
    typer.echo("or, inside Resolve:  Workspace -> Scripts -> AVE_Build_Timeline")


@app.command("reference")
def reference_add(
    path: Path = typer.Argument(..., help="Reference video to study."),
    name: str = typer.Option(..., "--name", help="Name to save the style under."),
    category: str = typer.Option(None, help="Free-text category, e.g. 'tech'."),
    threshold: float = typer.Option(27.0, help="Shot detection sensitivity. Lower = more cuts."),
) -> None:
    """Measure a reference video's editing style and save it to the style library."""
    from ave.database.queries import upsert_style
    from ave.media.ffmpeg import make_proxy, probe, summarise
    from ave.media.hash import content_hash
    from ave.reference.analyze import analyse_reference

    path = path.expanduser().resolve()
    if not path.exists():
        typer.echo(f"no such file: {path}")
        raise typer.Exit(1)

    config.ensure_dirs()
    get_db()

    digest = content_hash(path)
    probed = probe(path)
    probed["summary"] = summarise(probed)

    typer.echo(f"analysing {path.name} ...")
    proxy, _ = make_proxy(path, digest)

    dna = analyse_reference(
        source_path=path,
        proxy_path=proxy,
        probe=probed,
        style_name=name,
        content_hash=digest,
        threshold=threshold,
    )
    upsert_style(name, dna.model_dump(mode="json"), category=category)

    pacing = dna.pacing
    typer.echo(f"\nstyle '{name}' saved")
    typer.echo("\npacing")
    typer.echo(f"  average shot        {pacing.average_shot_duration_s:.2f}s")
    typer.echo(f"  median shot         {pacing.median_shot_duration_s:.2f}s")
    typer.echo(f"  cuts per minute     {pacing.cuts_per_minute:.1f}")
    typer.echo(f"  dead-air tolerance  {pacing.dead_air_tolerance_s:.2f}s")
    typer.echo("\naudio / colour")
    typer.echo(f"  loudness            {dna.audio.integrated_lufs:.1f} LUFS")
    typer.echo(f"  saturation          {dna.color.saturation_mean:.2f}")
    typer.echo(f"  contrast            {dna.color.contrast:.2f}")
    typer.echo(f"  warmth              {dna.color.temperature_bias:+.3f}")

    typer.echo("\nconfidence")
    for section, value in sorted(dna.confidence.items()):
        bar = "#" * int(value * 20)
        typer.echo(f"  {section:<12} {value:.2f}  {bar}")

    if dna.notes:
        typer.echo("\nwhat this profile does NOT know")
        for note in dna.notes:
            typer.echo(f"  - {note}")

    typer.echo(f"\napply it with:  ave edit <footage> --style {name}")


@app.command()
def tweak(
    project: str = typer.Argument(..., help="Project to adjust."),
    command: str = typer.Argument(..., help='e.g. "make it faster", "reduce zooms by 50%"'),
) -> None:
    """Adjust the latest plan with a natural-language instruction, as a new version."""
    from ave.database.queries import (
        get_plan, get_project, next_plan_version, save_approvals, save_plan,
    )
    from ave.executors.fcpxml import write_fcpxml
    from ave.executors.resolve_plan import write_plan
    from ave.media.ffmpeg import probe, summarise
    from ave.plan.feedback import apply_feedback
    from ave.plan.models import EDL
    from ave.plan.planner import PlanInputs, plan_cut
    from ave.policies.validate import validate_edl
    from ave.qc.validate import run_qc
    from ave.style.models import EditDNA, default_dna

    config.ensure_dirs()
    get_db()

    row = get_project(project)
    if not row:
        typer.echo(f"unknown project: {project}")
        raise typer.Exit(1)

    latest = next_plan_version(row["id"]) - 1
    if latest < 1:
        typer.echo(f"{project} has no plan yet — run `ave edit` first")
        raise typer.Exit(1)

    stored = get_plan(row["id"], latest)
    edl = EDL.model_validate(stored["edl"])
    dna = edl.dna or default_dna()

    result = apply_feedback(command, edl, dna)
    if not result.ok:
        typer.echo(f'not understood: "{command}"')
        typer.echo("\ntry: faster / slower / punchier / reduce zooms by 50% / remove zooms")
        raise typer.Exit(1)

    change = result.changes[0]
    typer.echo(f"v{latest:03d} -> v{latest + 1:03d}")
    typer.echo(f"  {change.description}")

    version = latest + 1
    if change.kind == "replan":
        # Where the cuts fall *is* the pacing, so this one cannot be a patch.
        source = edl.all_clips()[0].source_path if edl.all_clips() else None
        if not source or not Path(source).exists():
            typer.echo("  source media is unavailable, cannot replan")
            raise typer.Exit(1)
        probed = probe(source)
        probed["summary"] = summarise(probed)
        dna = change.dna or dna
        edl = plan_cut(
            PlanInputs(
                media_id=edl.all_clips()[0].source_media_id, path=source, probe=probed,
                dna=dna, project=project, version=version, seed=edl.seed,
            )
        )
        typer.echo("  (cuts recomputed)")
    else:
        edl.version = version
        typer.echo("  (cuts preserved — this was a patch, not a re-plan)")

    validation = validate_edl(edl, dna, autonomy=row["autonomy"])
    if not validation.ok:
        for clip_id, message in validation.clip_errors:
            typer.echo(f"  ERROR  {clip_id}: {message}")
        raise typer.Exit(1)

    qc = run_qc(edl)
    plan_id = save_plan(
        project_id=row["id"], version=version, seed=edl.seed,
        edl=edl.model_dump(mode="json"), qc=qc.to_dict(), origin="feedback",
        origin_detail=command, run_id=get_run_id(), parent_version=latest,
    )
    if pending := edl.pending_approval():
        save_approvals(plan_id, [op.model_dump(mode="json") for op in pending])

    summary = edl.summary
    typer.echo(
        f"\n{summary.clip_count} clips  {summary.output_duration_s:.1f}s  "
        f"({summary.kept_ratio:.0%} kept)"
    )
    typer.echo(qc.render())

    if not qc.ok:
        typer.echo("\nnot writing a timeline while there are errors")
        raise typer.Exit(1)

    out = config.BUILD_DIR / f"{project}_v{version:03d}.fcpxml"
    write_fcpxml(edl, out)
    write_plan(edl, config.BUILD_DIR / f"{project}_v{version:03d}.plan.json")
    typer.echo(f"\nwrote {out}")


@app.command("install-resolve-script")
def install_resolve_script() -> None:
    """Install the companion script into Resolve's Scripts menu (tier 2).

    Works on the free edition: a script in this folder runs inside Resolve's own
    Python, which is not "external scripting" and so is not Studio-gated.
    """
    import shutil

    source = config.REPO_ROOT / "resolve_scripts" / "AVE_Build_Timeline.py"
    if not source.exists():
        typer.echo(f"missing {source}")
        raise typer.Exit(1)

    target_dir = config.RESOLVE_USER_SCRIPTS
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    shutil.copy2(source, target)

    typer.echo(f"installed {target}")
    typer.echo("\nin Resolve:  Workspace -> Scripts -> AVE_Build_Timeline")
    typer.echo("it builds the newest plan as a NEW timeline; existing ones are untouched.")


@app.command("compare")
def compare_styles(
    left: str = typer.Argument(..., help="First style name."),
    right: str = typer.Argument(..., help="Second style name."),
    detail: bool = typer.Option(False, "--detail", help="Show per-field numbers."),
) -> None:
    """Score how alike two styles are, section by section."""
    from ave.database.queries import get_style
    from ave.style.compare import compare
    from ave.style.models import EditDNA

    get_db()
    rows = {}
    for name in (left, right):
        row = get_style(name)
        if not row:
            typer.echo(f"unknown style: {name}  (try `ave styles`)")
            raise typer.Exit(1)
        rows[name] = EditDNA.model_validate(row["dna"])

    report = compare(rows[left], rows[right])
    typer.echo(f"{left}  vs  {right}\n")
    typer.echo(report.render())

    if detail:
        typer.echo("")
        for section in report.sections:
            if section.detail:
                typer.echo(f"  {section.name}")
                for line in section.detail:
                    typer.echo(f"    {line}")

    if report.overall is None:
        typer.echo(
            "\nNeither style has enough measured to compare. Sections that were "
            "never measured hold the same defaults, so scoring them would report "
            "a similarity that means nothing."
        )


@app.command("fetch-model")
def fetch_model(
    name: str = typer.Option("base.en", help="tiny.en | base.en | small.en | medium.en"),
) -> None:
    """Download a whisper.cpp model. Run once per machine."""
    import urllib.request

    filename = f"ggml-{name}.bin"
    target = config.AVE_HOME / "models" / filename
    if target.exists():
        typer.echo(f"already have {target} ({target.stat().st_size / 1e6:.0f} MB)")
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{filename}"
    typer.echo(f"downloading {filename} ...")
    try:
        urllib.request.urlretrieve(url, target)
    except Exception as exc:  # noqa: BLE001
        target.unlink(missing_ok=True)
        typer.echo(f"download failed: {exc}")
        raise typer.Exit(1)
    typer.echo(f"saved {target} ({target.stat().st_size / 1e6:.0f} MB)")


@app.command()
def transcribe(
    path: Path = typer.Argument(..., help="Media to transcribe."),
    threads: int = typer.Option(None, help="CPU threads. Set this high on a desktop."),
    words: bool = typer.Option(False, "--words", help="Print word-level timings."),
    force: bool = typer.Option(False, "--force", help="Ignore the cache."),
) -> None:
    """Transcribe with word-level timestamps. Cached by content hash."""
    from ave.database.queries import get_analysis, save_analysis, upsert_media
    from ave.media.ffmpeg import probe, summarise
    from ave.media.hash import content_hash
    from ave.media.transcribe import ANALYZER_VERSION, Transcript, WhisperCppTranscriber, available

    path = path.expanduser().resolve()
    if not path.exists():
        typer.echo(f"no such file: {path}")
        raise typer.Exit(1)

    ok, why = available()
    if not ok:
        typer.echo(why)
        raise typer.Exit(1)

    config.ensure_dirs()
    get_db()

    digest = content_hash(path)
    probed = probe(path)
    probed["summary"] = summarise(probed)
    media_id, _ = upsert_media(
        path=str(path), content_hash=digest, kind="source", probe=probed, proxy_path=None
    )

    cached = None if force else get_analysis(media_id, "transcript", ANALYZER_VERSION)
    if cached:
        transcript = Transcript.from_dict(cached["data"])
        typer.echo("(cached)")
    else:
        typer.echo(f"transcribing {path.name} ...")
        started = time.monotonic()
        transcript = WhisperCppTranscriber(threads=threads).transcribe(path)
        elapsed = time.monotonic() - started
        save_analysis(
            media_id=media_id, kind="transcript", analyzer_version=ANALYZER_VERSION,
            data=transcript.to_dict(), duration_ms=int(elapsed * 1000),
        )
        duration = probed["summary"]["duration_s"] or 1
        typer.echo(f"took {elapsed:.1f}s for {duration:.1f}s of audio ({duration / elapsed:.1f}x realtime)")

    typer.echo("")
    if words:
        for word in transcript.words:
            mark = "  <- filler" if word.is_filler else ""
            typer.echo(f"  {word.start:7.2f}-{word.end:6.2f}  {word.text}{mark}")
    else:
        for segment in transcript.segments:
            typer.echo(f"  [{segment.start:6.2f}] {segment.text}")

    fillers = transcript.fillers()
    typer.echo(f"\n{len(transcript.words)} words, {len(transcript.segments)} segments, "
               f"{len(fillers)} fillers")


@app.command("analysis-export")
def analysis_export(
    path: Path = typer.Argument(..., help="Media whose analysis should be exported."),
    out: Path = typer.Option(None, "-o", help="Output JSON. Defaults to <name>.analysis.json"),
) -> None:
    """Export cached analysis for one file, for transfer to another machine.

    The cache is keyed by content hash, not by path or machine, so a transcript
    computed on a desktop is valid on the laptop for the same file. That is what
    makes offloading work without any shared database.
    """
    import json

    from ave.database.adapter import get_db as _db
    from ave.media.hash import content_hash

    path = path.expanduser().resolve()
    if not path.exists():
        typer.echo(f"no such file: {path}")
        raise typer.Exit(1)

    get_db()
    digest = content_hash(path)
    rows = _db().all(
        """SELECT a.kind, a.analyzer_version, a.data, a.duration_ms
           FROM analysis a JOIN media m ON m.id = a.media_id
           WHERE m.content_hash = ?""",
        digest,
    )
    if not rows:
        typer.echo(f"no cached analysis for {path.name} — run `ave transcribe` first")
        raise typer.Exit(1)

    payload = {
        "schema": "ave-analysis/1",
        "content_hash": digest,
        "source_name": path.name,
        "entries": [
            {"kind": r["kind"], "analyzer_version": r["analyzer_version"],
             "data": json.loads(r["data"]), "duration_ms": r["duration_ms"]}
            for r in rows
        ],
    }
    target = out or Path(f"{path.stem}.analysis.json")
    target.write_text(json.dumps(payload, indent=2))
    typer.echo(f"wrote {target}  ({len(rows)} analyses for content {digest[:12]})")


@app.command("analysis-import")
def analysis_import(
    payload_path: Path = typer.Argument(..., help="A file written by analysis-export."),
    media: Path = typer.Option(None, help="The matching media file, if not yet indexed."),
) -> None:
    """Import analysis computed on another machine."""
    import json

    from ave.database.queries import get_media_by_hash, save_analysis, upsert_media
    from ave.media.ffmpeg import probe, summarise
    from ave.media.hash import content_hash

    get_db()
    payload = json.loads(payload_path.read_text())
    if payload.get("schema") != "ave-analysis/1":
        typer.echo(f"unrecognised schema: {payload.get('schema')}")
        raise typer.Exit(1)

    digest = payload["content_hash"]
    row = get_media_by_hash(digest, "source")
    if not row:
        if not media or not media.exists():
            typer.echo(
                f"{payload['source_name']} is not indexed here. Pass --media <path> "
                f"pointing at the same file, or run `ave ingest` on it first."
            )
            raise typer.Exit(1)
        local = content_hash(media)
        if local != digest:
            # Refusing here is the point: silently attaching a transcript to the
            # wrong footage would be near-impossible to notice later.
            typer.echo(f"content mismatch — {media.name} is not the file this was computed from")
            raise typer.Exit(1)
        probed = probe(media)
        probed["summary"] = summarise(probed)
        media_id, _ = upsert_media(
            path=str(media.resolve()), content_hash=digest, kind="source",
            probe=probed, proxy_path=None,
        )
    else:
        media_id = row["id"]

    for entry in payload["entries"]:
        save_analysis(
            media_id=media_id, kind=entry["kind"],
            analyzer_version=entry["analyzer_version"], data=entry["data"],
            duration_ms=entry.get("duration_ms"),
        )
    typer.echo(f"imported {len(payload['entries'])} analyses for {payload['source_name']}")


@app.command()
def plans(project: str = typer.Argument(..., help="Project name.")) -> None:
    """List every version of a project's plan. Nothing is ever overwritten."""
    from ave.database.queries import get_project, list_plans

    get_db()
    row = get_project(project)
    if not row:
        typer.echo(f"unknown project: {project}")
        raise typer.Exit(1)
    for plan in list_plans(row["id"]):
        detail = f"  {plan['origin_detail']}" if plan["origin_detail"] else ""
        typer.echo(
            f"v{plan['version']:03d}  {plan['created_at']}  {plan['origin']}{detail}"
        )


@app.command()
def styles() -> None:
    """List saved styles."""
    from ave.database.queries import list_styles

    get_db()
    rows = list_styles()
    if not rows:
        typer.echo("no styles saved — edits will use neutral defaults")
        return
    for row in rows:
        pacing = row["dna"].get("pacing", {})
        typer.echo(
            f"{row['name']:<24} v{row['version']}  "
            f"dead-air {pacing.get('dead_air_tolerance_s', '?')}s  "
            f"{row['category'] or '-'}"
        )


@app.command()
def approvals(
    approve: str = typer.Option(None, help="Approval id to approve."),
    reject: str = typer.Option(None, help="Approval id to reject."),
) -> None:
    """Operations the planner was not confident enough to apply unreviewed."""
    from ave.database.queries import list_approvals, set_approval_state

    get_db()
    if approve or reject:
        target, state = (approve, "APPROVED") if approve else (reject, "REJECTED")
        if set_approval_state(target, state):
            typer.echo(f"{target} -> {state}")
        else:
            typer.echo(f"{target} is not pending")
            raise typer.Exit(1)
        return

    pending = list_approvals()
    if not pending:
        typer.echo("nothing awaiting approval")
        return
    for row in pending:
        typer.echo(f"{row['id']}  op {row['op_id']}")
        for reason in row["reasons"]:
            typer.echo(f"    [{reason['rule_id']}] {reason['message']}")


@app.command()
def jobs(dead: bool = typer.Option(False, "--dead", help="Show dead-lettered jobs only.")) -> None:
    """Inspect the job queue."""
    from ave.jobs.queue import list_dead_letters, list_jobs

    get_db()
    rows = list_dead_letters() if dead else list_jobs()
    if not rows:
        typer.echo("no dead-lettered jobs" if dead else "queue empty")
        return
    for row in rows:
        typer.echo(
            f"{row['id']}  {row['status']:<8} {row['kind']:<20} attempts={row['attempts']}"
            f"{'  ' + row['last_error'][:60] if row['last_error'] else ''}"
        )


if __name__ == "__main__":
    app()
