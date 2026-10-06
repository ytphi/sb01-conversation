#!/usr/bin/env python3
"""
monitor_export.py  -  save an sb01 monitoring report where YOU choose

  python3 scripts/monitor_export.py --audience team1      # Team 1 operational report
  python3 scripts/monitor_export.py --audience security   # security-study report
  python3 scripts/monitor_export.py --audience transcript # what was said, by whom, when
  python3 scripts/monitor_export.py --list                # sessions still in temporary storage
  python3 scripts/monitor_export.py --status              # live status of a running session

Options:
  --session ID   pick a session (default: most recent)
  --output PATH  write here instead of opening the Save As dialog
  --discard      delete the session's temporary data after a successful save

Normally sb01_conversation.py asks "Save monitoring report? [y/N]" at shutdown,
so this command is for a session that crashed, a session still running, or a
second report from a session not yet deleted. Sessions live only in temporary
storage (see teleop/monitor.py); nothing is written to the project folder
unless you pick it. Nothing is uploaded or sent anywhere.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from teleop.monitor import RUNTIME_BASE, discard_session, list_sessions          # noqa: E402
from teleop.monitor_reports import (AUDIENCES, _inside_repo, choose_destination,      # noqa: E402
                                    confirm_security, confirm_transcript, export)


def main():
    ap = argparse.ArgumentParser(description="Save an sb01 monitoring report where you choose.")
    ap.add_argument("--audience", choices=sorted(AUDIENCES))
    ap.add_argument("--session")
    ap.add_argument("--output")
    ap.add_argument("--discard", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if not RUNTIME_BASE:
        sys.exit("Monitoring storage is disabled (no safe temporary folder).")
    sessions = list_sessions()
    if args.list:
        if not sessions:
            print(f"No monitoring sessions in temporary storage ({RUNTIME_BASE}).")
        labels = {"clean": "ended normally, unsaved", "signal": "stopped by a signal, unsaved",
                  "interrupted": "interrupted, unsaved", "exit": "exited, unsaved"}
        for s in sessions:
            state = "running" if s["alive"] else labels.get(s["exit_type"], "crashed, unsaved")
            print(f"  {s['session']}  {state}")
        return
    if args.session:
        sessions = [s for s in sessions if s["session"] == args.session]
    if not sessions:
        sys.exit("No monitoring session found in temporary storage"
                 + (f" with id {args.session}." if args.session else ".")
                 + " Sessions are deleted after the shutdown prompt.")
    s = sessions[-1]

    if args.status:
        path = os.path.join(s["path"], "status.json")
        print(open(path, encoding="utf-8").read() if os.path.isfile(path) else "No status written yet.")
        return
    if not args.audience:
        ap.error("--audience team1, security or transcript is required")
    if args.audience == "security" and not confirm_security():
        sys.exit("Security report not saved.")
    if args.audience == "transcript" and not confirm_transcript():
        sys.exit("Transcript not saved.")

    if args.output:
        dest = os.path.abspath(os.path.expanduser(args.output))
        if os.path.isdir(dest):
            dest = os.path.join(dest, f"sb01_{args.audience}_{s['session']}.zip")
        if not dest.lower().endswith(".zip"):
            dest += ".zip"
        if _inside_repo(dest):
            sys.exit("Refusing to write inside the project folder with --output; "
                     "use the Save As dialog if you really want it there.")
        if os.path.exists(dest):
            sys.exit(f"{dest} already exists; choose another name.")
    else:
        dest = choose_destination(args.audience, s["session"])
        if not dest:
            sys.exit("Not saved.")

    if not export(s["events"], s["session"], args.audience, dest):
        sys.exit(2)
    if args.discard and not s["alive"]:
        discard_session(s["path"])
        print("Temporary monitoring data deleted.")


if __name__ == "__main__":
    main()
