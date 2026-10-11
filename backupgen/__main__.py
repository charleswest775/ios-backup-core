"""Command line: python -m backupgen <command> …"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from backupgen.persona import list_personas, load_persona


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backupgen", description="Generate synthetic iPhone backups from persona packs.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list personas and their snapshots")

    gen = sub.add_parser("generate", help="write backup snapshots")
    gen.add_argument("persona")
    gen.add_argument("--snapshot", action="append", help="snapshot id (repeatable; default: all)")
    gen.add_argument("--out", type=Path, required=True, help="folder; each snapshot/variant gets its own subfolder")
    gen.add_argument("--password", help="encrypt with this password")
    gen.add_argument("--iterations", choices=("fast", "real"), default="fast",
                     help="PBKDF2 rounds: 'real' matches iOS (10M, slow to open), 'fast' for tests")
    gen.add_argument("--seed", type=int)

    report = sub.add_parser("report", help="print a persona pack's review document (Markdown)")
    report.add_argument("persona")

    harvest = sub.add_parser("harvest", help="record table definitions from a real backup or SQLite file")
    source = harvest.add_mutually_exclusive_group(required=True)
    source.add_argument("--backup", type=Path, help="backup folder (harvests every known database + container)")
    source.add_argument("--sqlite", type=Path, help="a single database file")
    harvest.add_argument("--database", action="append",
                         help="database name, e.g. sms.db: required with --sqlite; with --backup, harvest only these (repeatable)")
    harvest.add_argument("--password")
    harvest.add_argument("--ios", required=True)
    harvest.add_argument("--build", required=True)
    harvest.add_argument("--source", required=True, help="where it came from, e.g. 'iOS Simulator 26.5'")

    args = parser.parse_args(argv)

    if args.command == "list":
        for persona_id in list_personas():
            persona = load_persona(persona_id)
            snaps = ", ".join(f"{s.id} (iOS {s.ios}, {s.backup_date.date()})" for s in persona.snapshots.values())
            print(f"{persona_id}: {persona.name}, {persona.real_start.year}–{persona.real_end.year}. Snapshots: {snaps}")
        return 0

    if args.command == "generate":
        from backupgen.generate import generate

        persona = load_persona(args.persona)
        for snapshot_id in args.snapshot or list(persona.snapshots):
            variant = "encrypted" if args.password else "plain"
            out = args.out / f"{persona.id}-{snapshot_id}-{variant}"
            backup = generate(persona.id, snapshot_id, out, password=args.password,
                              iterations=args.iterations, seed=args.seed, persona=persona)
            print(backup)
        return 0

    if args.command == "report":
        from backupgen.report import render

        sys.stdout.write(render(load_persona(args.persona)))
        return 0

    if args.command == "harvest":
        from backupgen import harvest as h

        if args.backup:
            paths = h.harvest_backup(args.backup, args.password, args.ios, args.build, args.source,
                                     only=set(args.database) if args.database else None)
        else:
            if not args.database or len(args.database) != 1:
                parser.error("--sqlite needs exactly one --database")
            paths = [h.write_schema(args.ios, args.build, args.source, args.database[0], h.harvest_sqlite(args.sqlite))]
        for path in paths:
            print(path)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
