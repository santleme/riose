"""RIOSE project command entry point."""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="riose")
    parser.add_argument("product", choices=("mvp3",), help="product workflow")
    args, remainder = parser.parse_known_args(argv)
    if args.product == "mvp3":
        from riose.products.ear_tag.mvp3.cli import main as mvp3_main
        return mvp3_main(remainder)
    parser.error(f"unsupported product: {args.product}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
