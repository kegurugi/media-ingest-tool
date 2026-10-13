"""Command-line entry point: `mit <command>`."""
from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from . import __version__
from .errors import IngestError
from .interactive import (
    EXIT_ERROR, Prompter, run_daylog, run_history, run_ingest, run_log, run_mhl, run_report, run_verify,
)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mit", description="Offload camera and audio cards to multiple verified destinations."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", default="project.json", help="project config file (default: project.json)")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    p = sub.add_parser("ingest", help="interactive card offload")
    p.add_argument("--source", help="source card path, e.g. /Volumes/A001")
    p.add_argument("--dest", action="append", help="destination volume name or path (repeat for each)")
    p.add_argument("--camera", help="camera ID from project.json")
    p.add_argument("--date", help="shoot date, MMDDYYYY")
    p.add_argument("--media-type", help="VIDEO, AUDIO or STILLS (still confirmed)")
    p.add_argument("--operator", help="operator name or number from project.json")
    p.add_argument("--notes", help="notes for this card (skips the notes prompt)")
    p.add_argument("--copy-all", action="store_true", help="also copy macOS system files (.Spotlight-V100, ._*, ...)")
    p.add_argument(
        "--allow-unverified-drives", action="store_true",
        help="offload even if a destination's physical disk can't be determined (e.g. a network share)",
    )
    p.add_argument(
        "--allow-same-volume", action="store_true",
        help="TESTING ONLY: allow destinations on the same drive as each other or the source",
    )
    p = sub.add_parser("verify", help="re-hash card folders against the hashes in the log")
    p.add_argument("folders", nargs="*", metavar="card_folder", help="card folder(s) on a destination drive")
    p.add_argument("--drive", help="check every current card the drive's log lists")
    p.add_argument("--date", help="with --drive: only that shoot date (MMDDYYYY)")
    p.add_argument("--no-record", action="store_true", help="don't record the results on the drive (strictly read-only)")
    p = sub.add_parser("mhl", help="write ASC MHL manifests for verified cards that don't have one")
    p.add_argument("folders", nargs="*", metavar="card_folder", help="card folder(s) on a destination drive")
    p.add_argument("--drive", help="every current card the drive's log lists")
    p.add_argument("--date", help="with --drive: only that shoot date (MMDDYYYY)")
    p.add_argument("--verify", action="store_true", help="re-hash each card before writing its manifest")
    p = sub.add_parser("report", help="regenerate the card reports and daily card log for a date")
    p.add_argument("date", help="shoot date, MMDDYYYY")
    p.add_argument("--dest", action="append", help="destination volume name or path (default: from project.json)")
    p = sub.add_parser("daylog", help="edit a day's header fields, crew and notes, then rebuild its card log")
    p.add_argument("date", help="shoot date, MMDDYYYY")
    p.add_argument("--dest", action="append", help="destination volume name or path (default: from project.json)")
    p.add_argument("--notes", help="set the day notes (skips the prompts)")
    p = sub.add_parser("log", help="master log tools")
    log_sub = p.add_subparsers(dest="log_command", metavar="<log command>", required=True)
    for name, help_text in (("export", "regenerate the .xlsx from the SQLite log"),
                            ("rebuild", "add missing card records to the SQLite log, then export")):
        lp = log_sub.add_parser(name, help=help_text)
        lp.add_argument("--dest", action="append", help="destination volume name or path (default: from project.json)")
    p = sub.add_parser("history", help="project history across all drive sets (kept beside project.json)")
    hist_sub = p.add_subparsers(dest="history_command", metavar="<history command>", required=True)
    hist_sub.add_parser("show", help="print project totals per drive set")
    hp = hist_sub.add_parser("rebuild", help="add card records from connected drives to the project history")
    hp.add_argument("--dest", action="append", help="destination volume name or path (default: from project.json)")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    runners = {"ingest": run_ingest, "report": run_report, "log": run_log, "history": run_history,
               "daylog": run_daylog, "verify": run_verify, "mhl": run_mhl}
    try:
        return runners[args.command](args, Prompter())
    except IngestError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_ERROR
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled." + (" Nothing was copied." if args.command == "ingest" else ""), file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
