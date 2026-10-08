#!/usr/bin/env python3
"""Write a workspace .env from the process environment. Values are not printed."""

from __future__ import annotations

import os
import sys
from pathlib import Path

REQUIRED = (
    "BOOMI_API_URL",
    "BOOMI_USERNAME",
    "BOOMI_API_TOKEN",
    "BOOMI_ACCOUNT_ID",
    "BOOMI_ENVIRONMENT_ID",
)


def quote(value: str) -> str:
    if "'" in value or "\n" in value or "\r" in value:
        raise SystemExit("A Boomi credential contains a quote or newline and cannot be written safely.")
    return "'" + value + "'"


def main() -> int:
    required = REQUIRED
    if "--api-only" in sys.argv:
        required = tuple(name for name in REQUIRED if name != "BOOMI_ENVIRONMENT_ID")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        print("Missing required environment variables: " + ", ".join(missing), file=sys.stderr)
        return 1

    target = Path(".env")
    if target.exists() and "--force" not in sys.argv:
        print("Refusing to overwrite an existing .env. Pass --force to replace it.", file=sys.stderr)
        return 1

    lines = [f"{name}={quote(os.environ[name])}" for name in required if os.environ.get(name)]
    lines.append("BOOMI_VERIFY_SSL=true")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {target} ({len(lines) - 1} credentials, values omitted).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
