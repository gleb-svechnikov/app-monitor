# App Monitor

Lightweight background monitor for the CrowPi2: tracks which GUI apps get
opened and for how long, plus periodic CPU/memory/temperature samples.
Logs to a local SQLite db (`~/.local/share/app-monitor/history.db`). No
dependencies beyond `psutil` (already installed system-wide on this Pi) and
the Python standard library.

## Run it manually

```bash
python3 monitor.py run        # foreground daemon, Ctrl-C to stop
python3 monitor.py report                 # today's app usage
python3 monitor.py report --since 7        # last 7 days
python3 monitor.py report --resources       # CPU/mem/temp stats instead
```

## Run it automatically at login

```bash
mkdir -p ~/.config/systemd/user
cp app-monitor.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now app-monitor.service
systemctl --user status app-monitor.service
```

To also have it start at boot even before you log in on the console:

```bash
loginctl enable-linger pi
```

## Notes

- Detection is process-based, not window-based — this compositor (Wayfire)
  doesn't expose window info to `wmctrl`/`xdotool`. A process only counts as
  a tracked "app" if it (a) has no controlling terminal — this is what
  separates a real GUI app (or a command run from the desktop) from a
  command someone typed into a terminal, which inherits that terminal's tty
  — and (b) isn't in the `DENYLIST_PREFIXES` list of known background
  daemons / CLI utilities in `monitor.py`.
- This is a heuristic, not exact: an unusual background daemon with no tty
  and an unfamiliar name could show up in a report. If that happens, add its
  name to `DENYLIST_PREFIXES`.
