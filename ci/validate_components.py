#!/usr/bin/env python3
"""Validate Boomi component XML checked in under active-development/."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "active-development"
NS = "http://api.platform.boomi.com/"
COMPONENT_TAG = f"{{{NS}}}Component"
SKIP_DIRS = {".sync-state", ".shasum-shim", "inventories", "feedback"}


def component_files() -> list[Path]:
    files: list[Path] = []
    if not DEV.is_dir():
        return files
    for path in DEV.rglob("*.xml"):
        if any(part in SKIP_DIRS for part in path.relative_to(DEV).parts):
            continue
        files.append(path)
    return sorted(files)


def validate(path: Path) -> list[str]:
    errors: list[str] = []
    rel = path.relative_to(ROOT).as_posix()
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return [f"{rel}: XML parse error: {exc}"]

    if root.tag != COMPONENT_TAG:
        errors.append(f"{rel}: root element must be bns:Component")
        return errors

    component_id = root.attrib.get("componentId", "")
    component_type = root.attrib.get("type", "")
    name = root.attrib.get("name", "")

    if not component_type:
        errors.append(f"{rel}: missing type attribute")
    if not name:
        errors.append(f"{rel}: missing name attribute")
    if len(component_id) != 36 or component_id.count("-") != 4:
        errors.append(f"{rel}: componentId must be the platform GUID")
    if root.attrib.get("deleted", "false") == "true":
        errors.append(f"{rel}: deleted component should not be deployed from git")
    return errors


def main() -> int:
    files = component_files()
    if not files:
        print("No component XML found under active-development/")
        return 1

    failures: list[str] = []
    for path in files:
        failures.extend(validate(path))

    if failures:
        print(f"Component validation failed ({len(failures)} issue(s)):")
        for item in failures:
            print(f"  {item}")
        return 1

    print(f"Validated {len(files)} component XML file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
