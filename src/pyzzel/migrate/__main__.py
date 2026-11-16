"""Command line interface for migration generation and application."""

import argparse
import importlib
import os
from pathlib import Path
import sys

from pyzzel import connect, disconnect
from pyzzel.errors import MigrationError, PyzzelError
from . import apply, create, generate, status, check, pull


def _parser():
    parser = argparse.ArgumentParser(prog="python -m pyzzel.migrate")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("generate", "create", "apply", "status", "check", "pull"):
        command = commands.add_parser(name)
        command.add_argument("--dir", default="migrations")
        command.add_argument("--models", action="append", default=[])
        command.add_argument("--history-table", default="pyzzel_migrations")
        if name in ("generate", "create"):
            command.add_argument("name")
        if name in ("apply", "status", "check", "pull"):
            command.add_argument("--url", default=None)
        if name == "check":
            command.add_argument("--ignore", action="append", default=[])
        if name == "pull":
            command.add_argument("--ignore", action="append", default=[])
            command.add_argument("--write", action="store_true")
        if name == "apply":
            command.add_argument("--allow-destructive", action="store_true")
            command.add_argument("--dry-run", action="store_true")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    try:
        if args.command == "check" and not args.models:
            print("check requires at least one --models module.", file=sys.stderr)
            return 1
        for module in args.models:
            try:
                importlib.import_module(module)
            except ImportError as error:
                print(f"could not import {module}: {error}", file=sys.stderr)
                return 1
        if args.command == "generate":
            path = generate(args.name, args.dir)
            print(path if path is not None else "No changes.")
            return 0
        if args.command == "create":
            print(create(args.name, args.dir))
            return 0
        url = args.url or os.environ.get("PYZZEL_DATABASE_URL")
        if not url:
            print("Database URL required: pass --url or set PYZZEL_DATABASE_URL.", file=sys.stderr)
            return 1
        try:
            connect(url)
            if args.command == "check":
                report = check(args.dir, ignore=args.ignore, history_table=args.history_table)
                sections = [
                    ("Pending model changes (run generate):", report.pending_model_changes),
                    ("Migration files:", report.migration_problems),
                    ("Drift (snapshot vs database):", report.drift),
                    ("Warnings:", report.warnings),
                ]
                shown = False
                for title, items in sections:
                    if items:
                        print(title)
                        for item in items: print(f"  - {item}")
                        shown = True
                if not shown:
                    print("OK: models, migrations and database agree.")
                return 0 if report.ok else 1
            if args.command == "pull":
                result = pull(args.dir, write=args.write, ignore=args.ignore,
                              history_table=args.history_table)
                if result.changes:
                    print("Differences between the snapshot and the database:")
                    for item in result.changes: print(f"  - {item}")
                else:
                    print("Snapshot already matches the database.")
                if result.warnings:
                    print("Warnings:")
                    for item in result.warnings: print(f"  - {item}")
                print("snapshot.json updated" if args.write else "run again with --write to replace the snapshot")
                return 0
            if args.command == "apply":
                names = apply(
                    args.dir, allow_destructive=args.allow_destructive,
                    dry_run=args.dry_run, history_table=args.history_table,
                )
                if names:
                    verb = "would apply" if args.dry_run else "applied"
                    for name in names:
                        print(f"{verb} {name}")
                else:
                    print("Nothing to apply.")
            else:
                for item in status(args.dir, history_table=args.history_table):
                    print(f"{item.state:<8} {item.name}")
            return 0
        finally:
            disconnect()
    except (MigrationError, PyzzelError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
