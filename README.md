# paswitch

A tiny CLI audio output switcher for PipeWire and PulseAudio.

## Install

```sh
uv tool install https://github.com/vivienm/paswitch
```

From a checkout:

```sh
uv sync
uv run paswitch --help
```

Needs `pactl` on `PATH` (shipped with `pipewire-pulse` or PulseAudio).
Desktop notifications go through the freedesktop D-Bus service; without a
notification daemon (or a session bus) they are silently skipped.

## Usage

```sh
paswitch            # list sinks (default action)
paswitch next       # cycle to the next sink — handy on a keybinding
paswitch prev       # cycle to the previous sink
paswitch set NAME   # switch to a sink (exact name or unique substring)
```

Global options go before the subcommand:

```sh
paswitch -x '*hdmi*:*digital*' next    # skip sinks matching these globs
paswitch --no-notify next              # no desktop notification
paswitch --log-level debug list        # verbose logging
```

### Excluding sinks

`-x GLOBS` skips sinks matching a colon-separated list of case-insensitive
globs (matched against the sink name and description) when cycling. The same
format is read from the `PASWITCH_EXCLUDE` env var; `--exclude` overrides it.

Sinks whose active port is unavailable (e.g. an unplugged HDMI output) are
skipped automatically and tagged `(unavailable)`.

### Notifications

On by default (`--no-notify` or `PASWITCH_NOTIFY=0` to disable). Repeated taps
replace the previous popup in place instead of stacking.

### Exit codes

- `0` — success
- `1` — nothing to switch to, or sink not found
- `2` — audio backend error (`pactl` missing or failed)
- `3` — the server silently refused the switch and kept the old default
