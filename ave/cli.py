"""
Command line interface.

The CLI is the primary surface and stays so even after the web UI arrives: it is
the testable one, and every capability has to exist here before it gets a button.
"""

from __future__ import annotations

import shutil
import sys
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
) -> None:
    """Plan a cut from silence, validate it, and write a Resolve-importable timeline."""
    from ave.database.queries import (
        get_style, next_plan_version, save_approvals, save_plan, upsert_project,
    )
    from ave.executors.fcpxml import write_fcpxml
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

    out = config.BUILD_DIR / f"{name}_v{version:03d}.fcpxml"
    write_fcpxml(edl, out)
    typer.echo(f"\nwrote {out}")
    typer.echo("import it with:  Resolve -> File -> Import -> Timeline")


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
