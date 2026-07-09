#!/usr/bin/env python3
"""Run watchnotify (main.py) every 30 minutes until the start of July.

This is a stand-in for the GitHub Actions cron schedule while the repo's
monthly Actions minutes are exhausted. It launches main.py as a subprocess
on a fixed interval and stops automatically on July 1st.

Usage:
    python3 runner.py                 # run with main.py defaults (sends email)
    python3 runner.py -- --noemail    # pass extra args through to main.py
    python3 runner.py --interval 15   # custom interval in minutes

Stop early at any time with Ctrl+C.
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
MAIN_SCRIPT = SCRIPT_DIR / "main.py"


def next_july_first(now: datetime) -> datetime:
    """Return midnight on the first day of the upcoming July."""
    year = now.year if now.month < 7 else now.year + 1
    return datetime(year, 7, 1)


def run_once(extra_args: list[str], timeout_seconds: float) -> int:
    """Run main.py once, streaming its output. Returns the exit code.

    The child is started in its own process group so that, if it hangs (e.g.
    Playwright occasionally stalls during browser teardown), we can kill the
    whole tree of subprocesses and keep the schedule going.
    """
    cmd = [sys.executable, str(MAIN_SCRIPT), *extra_args]
    try:
        proc = subprocess.Popen(cmd, cwd=str(SCRIPT_DIR), start_new_session=True)
    except Exception as e:
        print(f"  ! Run failed to launch: {type(e).__name__}: {e}", flush=True)
        return -1

    try:
        return proc.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        print(f"  ! Run exceeded {timeout_seconds/60:.0f} min - killing it", flush=True)
        _kill_tree(proc)
        return -9
    except KeyboardInterrupt:
        _kill_tree(proc)
        raise


def _kill_tree(proc: subprocess.Popen) -> None:
    """Terminate the child's whole process group (child + browser subprocesses)."""
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            continue


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run main.py on an interval until the start of July."
    )
    parser.add_argument(
        "--interval", type=float, default=30,
        help="minutes between runs (default: 30)",
    )
    parser.add_argument(
        "--timeout", type=float, default=5,
        help="max minutes for a single run before it's killed (default: 5)",
    )
    parser.add_argument(
        "extra", nargs=argparse.REMAINDER,
        help="args to pass through to main.py (prefix with --)",
    )
    args = parser.parse_args()

    # argparse.REMAINDER keeps a leading "--"; drop it if present.
    extra_args = args.extra
    if extra_args and extra_args[0] == "--":
        extra_args = extra_args[1:]

    interval = timedelta(minutes=args.interval)
    timeout_seconds = args.timeout * 60
    stop_at = next_july_first(datetime.now())

    print(f"watchnotify runner - every {args.interval:g} min until {stop_at:%Y-%m-%d %H:%M}")
    print(f"per-run timeout: {args.timeout:g} min")
    if extra_args:
        print(f"passing args to main.py: {' '.join(extra_args)}")
    print(f"press Ctrl+C to stop early\n", flush=True)

    run_count = 0
    try:
        while datetime.now() < stop_at:
            run_count += 1
            started = datetime.now()
            print(f"{'='*60}")
            print(f" RUN #{run_count} - {started:%Y-%m-%d %H:%M:%S}")
            print(f"{'='*60}", flush=True)

            code = run_once(extra_args, timeout_seconds)
            print(f"\n  run #{run_count} exited with code {code}", flush=True)

            # Schedule the next run relative to when this one started, so the
            # cadence stays close to every `interval` regardless of run length.
            next_run = started + interval
            if next_run >= stop_at:
                break

            now = datetime.now()
            sleep_seconds = max(0, (next_run - now).total_seconds())
            print(f"  next run at {next_run:%H:%M:%S} "
                  f"(sleeping {sleep_seconds/60:.1f} min)\n", flush=True)
            time.sleep(sleep_seconds)
    except KeyboardInterrupt:
        print("\nStopped by user.", flush=True)
        return

    print(f"\nReached {stop_at:%Y-%m-%d}. Completed {run_count} run(s). Exiting.",
          flush=True)


if __name__ == "__main__":
    main()
