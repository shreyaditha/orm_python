"""Command-line interface: ``pyorm makemigrations`` / ``pyorm migrate``.

Usage::

    pyorm makemigrations --models examples.blog_demo --db sqlite:///blog.db
    pyorm migrate --db sqlite:///blog.db
    pyorm migrate --rollback --db sqlite:///blog.db
"""

from __future__ import annotations

import argparse
import importlib
import sys
from typing import Sequence

from pyorm.connection import configure
from pyorm.migrations import MigrationRunner
from pyorm.models import registry


def _import_models(module_path: str) -> None:
    """Import a module so its Model subclasses register themselves."""
    importlib.import_module(module_path)
    registry.finalize()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pyorm", description="PyORM migration CLI")
    parser.add_argument(
        "--db",
        default="sqlite:///pyorm.db",
        help="Database URL (default sqlite:///pyorm.db)",
    )
    parser.add_argument(
        "--migrations-dir",
        default="migrations",
        help="Directory for generated migration files",
    )
    parser.add_argument(
        "--models",
        default=None,
        help="Dotted module path that declares models (required for makemigrations)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("makemigrations", help="Diff models against last state and write a migration")
    migrate = sub.add_parser("migrate", help="Apply pending migrations")
    migrate.add_argument(
        "--rollback",
        action="store_true",
        help="Revert the most recently applied migration",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    engine = configure(args.db)
    runner = MigrationRunner(args.migrations_dir, engine)

    if args.command == "makemigrations":
        if not args.models:
            parser.error("--models is required for makemigrations (e.g. examples.blog_demo)")
        _import_models(args.models)
        path = runner.makemigrations()
        if path is None:
            print("No model changes detected.")
        else:
            print(f"Wrote {path}")
        return 0

    if args.command == "migrate":
        if args.models:
            _import_models(args.models)
        if args.rollback:
            name = runner.rollback()
            if name is None:
                print("No applied migrations to roll back.")
            else:
                print(f"Rolled back {name}")
            return 0
        ran = runner.migrate()
        if not ran:
            print("No pending migrations.")
        else:
            for name in ran:
                print(f"Applied {name}")
        return 0

    parser.error(f"Unknown command {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
