"""Build, export or explicitly publish the preset. No command creates a repository."""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib

from packbuilder import build, release
from packbuilder.files import BuildError, build_lock, read_json


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the Endfield Game Pack")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ["build", "export", "publish"]:
        command = commands.add_parser(name)
        command.add_argument("--root", type=pathlib.Path, default=pathlib.Path.cwd(), help="Preset folder containing config/endfield.json")
        if name == "build":
            command.add_argument("--no-network", action="store_true", help="Use saved sources and bundled images only")
            command.add_argument("--date", type=datetime.date.fromisoformat, help=argparse.SUPPRESS)
            command.add_argument("--publish", action="store_true", help="Publish the checked archive to GitHub and then update index.json")
        if name == "export":
            command.add_argument("--out", type=pathlib.Path, required=True, help="Local registry directory")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "build":
            result = build.build(root, no_network=args.no_network, today=args.date, publish=args.publish)
        elif args.command == "export":
            with build_lock(root):
                result = release.export(root / "packs/endfield", args.out.resolve())
        else:
            (root / ".cache").mkdir(exist_ok=True)
            with build_lock(root):
                result = release.publish(root, read_json(root / "config/endfield.json"))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except BuildError as error:
        parser.exit(1, str(error) + "\n")
