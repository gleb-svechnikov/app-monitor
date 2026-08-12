#!/usr/bin/env python3
"""Lightweight app-usage & resource monitor for the CrowPi2.

Polls the process list for GUI-ish apps the user has open and logs open/close
times, plus periodic CPU/memory/temperature samples, to a local SQLite db.

Usage:
    python3 monitor.py run                  # start the polling daemon
    python3 monitor.py report                # today's app usage
    python3 monitor.py report --since 7       # last 7 days
    python3 monitor.py report --resources      # resource stats instead
"""

import argparse
import getpass
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import psutil

POLL_SECONDS = 5
DB_PATH = Path.home() / ".local" / "share" / "app-monitor" / "history.db"

# Known background/system processes on this CrowPi2 (Wayfire desktop) that
# are never "apps the user opened". Matched as a substring against the
# process name.
DENYLIST_PREFIXES = (
    "dbus-daemon", "gnome-keyring-d", "gtk-nop", "gvfsd", "gvfs-",
    "menu-cached", "pipewire", "polkit-mate-aut", "wayfire", "wf-panel-pi",
    "wfrespawn", "wireplumber", "xdg-", "xsel", "xsettingsd", "Xwayland",
    "systemd", "(sd-pam)", "ssh-agent", "bash", "sh", "python3", "ps",
    "fusermount", "dconf-service", "sleep", "timeout", "sudo", "date",
    "echo", "cat", "grep", "awk", "sed", "find", "git", "node", "claude",
    "pkill", "kill", "nohup", "env", "which", "uname", "ls", "printf",
    "sort", "cron", "at",
)


def is_tracked_app(name: str, cmdline: list[str], terminal: str | None) -> bool:
    # Anything run from a shell (a command typed in lxterminal, or this
    # daemon's own tooling) inherits a controlling tty. Real GUI apps and
    # background daemons alike are launched detached, with no tty -- so this
    # is what actually separates "an app the user opened" from "a command
    # someone ran", which a name-based list alone can't do.
    if terminal is not None:
        return False
    if any(name.startswith(prefix) for prefix in DENYLIST_PREFIXES):
        return False
    if name == "pcmanfm" and "--desktop" in cmdline:
        return False
    return True


def connect_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS app_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            pid INTEGER NOT NULL,
            start_ts REAL NOT NULL,
            end_ts REAL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS resource_samples (
            ts REAL PRIMARY KEY,
            cpu_pct REAL NOT NULL,
            mem_pct REAL NOT NULL,
            temp_c REAL
        )"""
    )
    conn.commit()
    return conn


def close_stale_sessions(conn: sqlite3.Connection) -> None:
    """Discard sessions left open by a previous run (crash/reboot): we can't
    know their real end time, so close them at their own start time."""
    conn.execute(
        "UPDATE app_sessions SET end_ts = start_ts WHERE end_ts IS NULL"
    )
    conn.commit()


def read_temp() -> float | None:
    try:
        zones = psutil.sensors_temperatures().get("cpu_thermal")
        return zones[0].current if zones else None
    except (AttributeError, OSError):
        return None


def poll_processes(username: str) -> dict[str, int]:
    """Return {app_name: pid} for currently tracked apps (one pid per name)."""
    apps: dict[str, int] = {}
    for proc in psutil.process_iter(["pid", "name", "username", "cmdline", "terminal"]):
        info = proc.info
        if info.get("username") != username:
            continue
        name = info.get("name") or ""
        cmdline = info.get("cmdline") or []
        if is_tracked_app(name, cmdline, info.get("terminal")):
            apps[name] = info["pid"]
    return apps


def run_daemon() -> None:
    username = getpass.getuser()
    conn = connect_db()
    close_stale_sessions(conn)

    open_sessions: dict[str, int] = {}  # name -> app_sessions.id
    try:
        while True:
            now = time.time()
            current = poll_processes(username)

            for name, pid in current.items():
                if name not in open_sessions:
                    cur = conn.execute(
                        "INSERT INTO app_sessions (name, pid, start_ts, end_ts) "
                        "VALUES (?, ?, ?, NULL)",
                        (name, pid, now),
                    )
                    open_sessions[name] = cur.lastrowid

            for name in list(open_sessions):
                if name not in current:
                    conn.execute(
                        "UPDATE app_sessions SET end_ts = ? WHERE id = ?",
                        (now, open_sessions[name]),
                    )
                    del open_sessions[name]

            conn.execute(
                "INSERT OR IGNORE INTO resource_samples (ts, cpu_pct, mem_pct, temp_c) "
                "VALUES (?, ?, ?, ?)",
                (now, psutil.cpu_percent(), psutil.virtual_memory().percent, read_temp()),
            )
            conn.commit()
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()


class Ansi:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    GREEN = "\033[32m"


def use_color() -> bool:
    return sys.stdout.isatty()


def style(text: str, *codes: str) -> str:
    if not use_color():
        return text
    return "".join(codes) + text + Ansi.RESET


def color_for_pct(value: float) -> str:
    if value >= 85:
        return Ansi.RED
    if value >= 60:
        return Ansi.YELLOW
    return Ansi.GREEN


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def format_last_seen(ts: float) -> str:
    dt = datetime.fromtimestamp(ts)
    days_ago = (datetime.now().date() - dt.date()).days
    if days_ago <= 0:
        day_str = "today"
    elif days_ago == 1:
        day_str = "yesterday"
    else:
        day_str = f"{days_ago} days ago"
    return f"{day_str} {dt.strftime('%H:%M')}"


def since_timestamp(days: int | None) -> float:
    if days is None:
        start_of_day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return start_of_day.timestamp()
    return (datetime.now() - timedelta(days=days)).timestamp()


def report_apps(conn: sqlite3.Connection, since_ts: float) -> None:
    now = time.time()
    rows = conn.execute(
        """SELECT name,
                  SUM(COALESCE(end_ts, ?) - start_ts) AS total,
                  COUNT(*) AS sessions,
                  MAX(COALESCE(end_ts, ?)) AS last_seen
           FROM app_sessions
           WHERE start_ts >= ?
           GROUP BY name
           ORDER BY total DESC""",
        (now, now, since_ts),
    ).fetchall()

    if not rows:
        print("No app activity recorded in this window.")
        return

    header = f"{'DURATION':<10} {'LAST SEEN':<18} {'APP':<26} {'SESSIONS'}"
    print(style(header, Ansi.BOLD))
    print(style("-" * len(header), Ansi.DIM))
    for name, total, sessions, last_seen in rows:
        last_seen_str = format_last_seen(last_seen)
        print(f"{format_duration(total):<10} {last_seen_str:<18} {name:<26} {sessions}")


def report_resources(conn: sqlite3.Connection, since_ts: float) -> None:
    row = conn.execute(
        """SELECT AVG(cpu_pct), MAX(cpu_pct), AVG(mem_pct), MAX(mem_pct),
                  AVG(temp_c), MAX(temp_c), COUNT(*)
           FROM resource_samples WHERE ts >= ?""",
        (since_ts,),
    ).fetchone()

    avg_cpu, max_cpu, avg_mem, max_mem, avg_temp, max_temp, count = row
    if not count:
        print("No resource samples recorded in this window.")
        return

    print(style(f"Samples: {count}", Ansi.DIM))

    def line(label: str, avg: float, mx: float, unit: str) -> None:
        avg_str = style(f"{avg:.1f}{unit}", color_for_pct(avg))
        max_str = style(f"{mx:.1f}{unit}", color_for_pct(mx))
        print(f"{style(f'{label:<5}', Ansi.BOLD)} avg {avg_str}  max {max_str}")

    line("CPU:", avg_cpu, max_cpu, "%")
    line("Mem:", avg_mem, max_mem, "%")
    if avg_temp is not None:
        line("Temp:", avg_temp, max_temp, "C")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="start the polling daemon (foreground)")

    report_parser = sub.add_parser("report", help="print a usage/resource report")
    report_parser.add_argument(
        "--since", type=int, metavar="DAYS",
        help="report over the last N days instead of just today",
    )
    report_parser.add_argument(
        "--resources", action="store_true",
        help="show CPU/mem/temp stats instead of app usage",
    )

    args = parser.parse_args()

    if args.command == "run":
        run_daemon()
    elif args.command == "report":
        conn = connect_db()
        since_ts = since_timestamp(args.since)
        if args.resources:
            report_resources(conn, since_ts)
        else:
            report_apps(conn, since_ts)
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
