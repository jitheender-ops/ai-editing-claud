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
from ave.lib.log import set_run_id
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
