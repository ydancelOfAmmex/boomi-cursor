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


def clean(value: str) -> str:
    value = value.strip().strip('"').strip("'").strip()
    if "'" in value or "\n" in value or "\r" in value:
        raise SystemExit("A Boomi credential contains a quote or newline and cannot be written safely.")
    return value


def normalize_api_url(value: str) -> str:
    value = clean(value).rstrip("/")
    marker = "/api/rest"
    if marker in value:
        value = value.split(marker, 1)[0].rstrip("/")
    if value and "://" not in value:
        value = "https://" + value
    return value


def quote(value: str) -> str:
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

    values = {name: clean(os.environ[name]) for name in required if os.environ.get(name)}
    if "BOOMI_API_URL" in values:
        values["BOOMI_API_URL"] = normalize_api_url(values["BOOMI_API_URL"])
        host = values["BOOMI_API_URL"].split("://", 1)[-1].split("/")[0]
        print(f"Boomi API host: {host}")
    lines = [f"{name}={quote(values[name])}" for name in required if name in values]
    lines.append("BOOMI_VERIFY_SSL=true")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {target} ({len(lines) - 1} credentials, values omitted).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
