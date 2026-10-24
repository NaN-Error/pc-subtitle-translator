"""Run with python -m pc_subtitle_translator; --demo never starts capture."""

from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description="Translate Windows PC audio into movable subtitles.")
    parser.add_argument("--demo", action="store_true", help="Open an appearance preview without audio capture or API requests.")
    args = parser.parse_args()
    from .ui import run_app
    return run_app(demo=args.demo)


if __name__ == "__main__":
    raise SystemExit(main())
