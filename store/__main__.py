"""python -m store migrate [revision]: bring the database up to date."""

import argparse

from store import migrate


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m store")
    commands = parser.add_subparsers(dest="command", required=True)
    up = commands.add_parser("migrate", help="upgrade the schema")
    up.add_argument("revision", nargs="?", default="head")
    down = commands.add_parser("downgrade", help="downgrade the schema")
    down.add_argument("revision")
    args = parser.parse_args(argv)

    if args.command == "migrate":
        migrate.upgrade(revision=args.revision)
    else:
        migrate.downgrade(revision=args.revision)


if __name__ == "__main__":
    main()
