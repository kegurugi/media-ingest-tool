"""Command-line entry point: `mit <command>`.

Commands are stubs until their build stage lands.
"""
from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from . import __version__

_STAGE = {"ingest": 2, "verify": 7, "report": 4, "daylog": 6, "log export": 5}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mit", description="Offload camera and audio cards to multiple verified destinations."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", default="project.json", help="project config file (default: project.json)")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("ingest", help="interactive card offload")
    p = sub.add_parser("verify", help="re-hash a card folder against the log")
    p.add_argument("card_folder")
    p = sub.add_parser("report", help="regenerate card reports and the daily card log for a date")
    p.add_argument("date", help="shoot date, MMDDYYYY")
    p = sub.add_parser("daylog", help="edit a day's header fields, crew and notes")
    p.add_argument("date", help="shoot date, MMDDYYYY")
    p = sub.add_parser("log", help="master log tools")
    log_sub = p.add_subparsers(dest="log_command", metavar="<log command>", required=True)
    log_sub.add_parser("export", help="regenerate the .xlsx from the SQLite log")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    command = "log export" if args.command == "log" else args.command
    print(f"mit {command}: not implemented yet (build stage {_STAGE[command]})", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
