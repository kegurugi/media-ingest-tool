"""Command-line entry point: `mit <command>`.

Commands are stubs until their build stage lands.
"""
from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from . import __version__
from .errors import IngestError
from .interactive import EXIT_ERROR, Prompter, run_history, run_ingest, run_log, run_report

_STAGE = {"verify": 7, "daylog": 6}


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
        "--allow-same-volume", action="store_true",
        help="TESTING ONLY: allow destinations on the same drive as each other or the source",
    )
    p = sub.add_parser("verify", help="re-hash a card folder against the log")
    p.add_argument("card_folder")
    p = sub.add_parser("report", help="regenerate the card reports for a date (from the card records)")
    p.add_argument("date", help="shoot date, MMDDYYYY")
    p.add_argument("--dest", action="append", help="destination volume name or path (default: from project.json)")
    p = sub.add_parser("daylog", help="edit a day's header fields, crew and notes")
    p.add_argument("date", help="shoot date, MMDDYYYY")
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
    runners = {"ingest": run_ingest, "report": run_report, "log": run_log, "history": run_history}
    if args.command in runners:
        try:
            return runners[args.command](args, Prompter())
        except IngestError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return EXIT_ERROR
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled. Nothing was copied.", file=sys.stderr)
            return EXIT_ERROR
    print(f"mit {args.command}: not implemented yet (build stage {_STAGE[args.command]})", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
