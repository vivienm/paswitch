"""Command-line interface for paswitch."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated

import typer

from . import notification
from .audio import (
    BackendError,
    Direction,
    NothingToSwitchError,
    PactlBackend,
    Sink,
    SinkNotFoundError,
    SwitchRefusedError,
    cycle_default,
    switch_to,
)
from .exclude import Exclude

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="paswitch",
    help="A tiny CLI audio output switcher for PipeWire and PulseAudio.",
    no_args_is_help=False,
)


class LogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


def setup_logging(log_level: LogLevel) -> None:
    logging.basicConfig(
        level=log_level.upper(),
        format="%(levelname)s %(name)s: %(message)s",
    )


@dataclass(frozen=True)
class Settings:
    """Global options shared with subcommands through `ctx.obj`."""

    notify: bool = True
    exclude: Exclude = field(default_factory=Exclude)


def _backend() -> PactlBackend:
    """Factory used by the CLI; monkeypatched in tests."""
    return PactlBackend()


def _announce(target: Sink, *, notify: bool) -> None:
    """Print and notify the newly selected sink."""
    message = f"Audio output: {target.label}"
    typer.echo(message)
    if notify:
        notification.send(message)


# Width of the marker + index prefix, so the wrapped name aligns under the
# description: `"* " (2) + "%4d" (4) + "  " (2)`.
_SINK_INDENT = " " * 8


def _format_sink(sink: Sink, *, default: bool, excluded: bool) -> str:
    """Render one list entry as two lines, styled for terminals."""
    states = []
    if excluded:
        states.append("excluded")
    if not sink.is_available:
        states.append("unavailable")
    # Yellow marks a sink skipped when cycling; the default never is, so it
    # keeps its bold styling and only the tag conveys an unavailable port.
    colour = typer.colors.YELLOW if states and not default else None
    marker = typer.style("*" if default else " ", bold=default, fg=colour)
    index = typer.style(f"{sink.index:>4}", bold=default, fg=colour)
    description = typer.style(sink.label, bold=default, fg=colour)
    tag = typer.style(f"  ({', '.join(states)})", fg=colour) if states else ""
    name = typer.style(f"[{sink.name}]", dim=True)
    return f"{marker} {index}  {description}{tag}\n{_SINK_INDENT}{name}"


def _fail(message: str, *, code: int, notify: bool = False) -> typer.Exit:
    """Report an error on stderr, optionally notify, and exit.

    `notify` is reserved for genuine failures (backend unreachable, switch
    refused): a critical, non-transient popup that survives in the notification
    log. Benign dead-ends — nothing to switch, sink not found (exit 1) — stay
    silent: they are usually a no-op or a typo behind a keybinding, not a fault
    worth interrupting the user for.

    Returns the `typer.Exit` to raise so callers can `raise _fail(...)`
    and keep their exception chaining explicit.
    """
    typer.echo(message, err=True)
    if notify:
        # Non-transient so the error survives in the notification log, and
        # replace=False so it is not silently overwritten by the next routine
        # switch notification (which reuses the shared replace_id).
        notification.send(
            message,
            urgency="critical",
            expire_ms=5000,
            replace=False,
            transient=False,
        )
    return typer.Exit(code=code)


def _do_cycle(ctx: typer.Context, direction: Direction) -> None:
    settings: Settings = ctx.obj
    notify = settings.notify
    try:
        target = cycle_default(
            _backend(), direction=direction, exclude=settings.exclude
        )
    except NothingToSwitchError as exc:
        raise _fail(f"Nothing to switch: {exc}", code=1) from exc
    except SwitchRefusedError as exc:
        raise _fail(f"Switch refused: {exc}", code=3, notify=notify) from exc
    except BackendError as exc:
        raise _fail(f"Audio backend error: {exc}", code=2, notify=notify) from exc
    _announce(target, notify=notify)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    log_level: Annotated[
        LogLevel,
        typer.Option(
            envvar="PASWITCH_LOG_LEVEL",
            help="Set the verbosity level for log messages.",
            case_sensitive=False,
        ),
    ] = LogLevel.INFO,
    notify: Annotated[
        bool,
        typer.Option(
            "--notify/--no-notify",
            envvar="PASWITCH_NOTIFY",
            help="Send a desktop notification (use --no-notify to disable).",
        ),
    ] = True,
    exclude: Annotated[
        Exclude | None,
        typer.Option(
            "--exclude",
            "-x",
            envvar="PASWITCH_EXCLUDE",
            metavar="GLOB",
            parser=Exclude.parse,
            show_default=False,
            help="Colon-separated list of glob patterns to exclude sinks when cycling.",
        ),
    ] = None,
) -> None:
    """List the audio sinks, or run an explicit subcommand."""
    setup_logging(log_level)
    # The callback runs first (invoke_without_command=True) and always sets this
    # before any subcommand reads it back as `ctx.obj`. `exclude` is parsed
    # (split on ':' and lowercased) by Exclude.parse via the Typer parser, for
    # both the command line and the env var; it is None when neither is given,
    # so fall back to an empty Exclude.
    ctx.obj = Settings(notify=notify, exclude=exclude or Exclude())
    if ctx.invoked_subcommand is None:
        _do_list(ctx)


@app.command(name="next")
def next_sink(ctx: typer.Context) -> None:
    """Switch to the next audio sink."""
    _do_cycle(ctx, Direction.NEXT)


@app.command(name="prev")
def prev_sink(ctx: typer.Context) -> None:
    """Switch to the previous audio sink."""
    _do_cycle(ctx, Direction.PREV)


def _do_list(ctx: typer.Context) -> None:
    settings: Settings = ctx.obj
    try:
        backend = _backend()
        sinks = sorted(backend.list_sinks())
        default = backend.default_sink_name()
    except BackendError as exc:
        raise _fail(
            f"Audio backend error: {exc}", code=2, notify=settings.notify
        ) from exc
    for sink in sinks:
        is_default = sink.name == default
        excluded = not is_default and settings.exclude.match(sink)
        typer.echo(_format_sink(sink, default=is_default, excluded=excluded))


@app.command(name="list")
def list_sinks(ctx: typer.Context) -> None:
    """List available audio sinks."""
    _do_list(ctx)


@app.command(name="set")
def set_sink(
    ctx: typer.Context,
    name: Annotated[
        str,
        typer.Argument(help="Sink to switch to (exact name or unique substring)."),
    ],
) -> None:
    """Switch to the specified audio sink."""
    settings: Settings = ctx.obj
    notify = settings.notify
    try:
        target = switch_to(_backend(), name)
    except SinkNotFoundError as exc:
        raise _fail(f"Sink not found: {exc}", code=1) from exc
    except SwitchRefusedError as exc:
        raise _fail(f"Switch refused: {exc}", code=3, notify=notify) from exc
    except BackendError as exc:
        raise _fail(f"Audio backend error: {exc}", code=2, notify=notify) from exc
    _announce(target, notify=notify)


if __name__ == "__main__":
    app()
