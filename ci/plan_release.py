#!/usr/bin/env python3
"""Choose which changed Boomi components to push and which processes to deploy.

Prints a JSON plan. Push order is dependency order: profiles, scripts, maps,
operations, connections, subprocesses, then release processes. A release process
is deployed when its own XML changed, or when a changed component id appears in
that process XML (so a subprocess or map change redeploys the parent).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "active-development"
TARGETS = ROOT / "ci" / "deploy-targets.txt"
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
    return files


def load_component(path: Path) -> dict:
    root = ET.parse(path).getroot()
    if root.tag != COMPONENT_TAG:
        raise ValueError(f"{path}: root element must be bns:Component")
    rel = path.relative_to(ROOT).as_posix()
    return {
        "path": rel,
        "id": root.attrib.get("componentId", ""),
        "type": root.attrib.get("type", ""),
        "name": root.attrib.get("name", path.stem),
        "text": path.read_text(encoding="utf-8"),
    }


def push_rank(component: dict) -> tuple[int, str]:
    typ = component["type"]
    name = component["name"]
    path = component["path"]
    if typ.startswith("profile.") or "/profile." in f"/{path}":
        rank = 10
    elif typ.startswith("script.") or "/script." in f"/{path}":
        rank = 20
    elif typ == "transform.map" or "/transform.map/" in f"/{path}":
        rank = 30
    elif typ == "connector-action" or "/connector-action/" in f"/{path}":
        rank = 40
    elif typ == "connector-settings" or "/connector-settings/" in f"/{path}":
        rank = 50
    elif typ == "process" and "(Sub)" in name:
        rank = 60
    elif typ == "process":
        rank = 70
    else:
        rank = 55
    return (rank, path)


def load_targets() -> list[str]:
    if not TARGETS.is_file():
        return []
    lines = []
    for raw in TARGETS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip().replace("\\", "/")
        if line:
            lines.append(line)
    return lines


def git_changed(base: str, head: str) -> list[str]:
    result = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=ACMRT",
            base,
            head,
            "--",
            "active-development",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    changed = []
    for line in result.stdout.splitlines():
        rel = line.strip().replace("\\", "/")
        if rel.endswith(".xml") and rel.startswith("active-development/"):
            changed.append(rel)
    return changed


def build_plan(changed: list[str]) -> dict:
    by_path = {item["path"]: item for item in (load_component(path) for path in component_files())}
    unknown = [path for path in changed if path not in by_path]
    if unknown:
        raise SystemExit("Changed path is not a component XML file:\n  " + "\n  ".join(unknown))

    selected = [by_path[path] for path in changed]
    selected.sort(key=push_rank)

    changed_ids = {item["id"] for item in selected if item["id"]}
    targets = load_targets()
    if targets:
        release_paths = targets
        missing = [path for path in release_paths if path not in by_path]
        if missing:
            raise SystemExit(
                "ci/deploy-targets.txt lists files that are not component XML:\n  " + "\n  ".join(missing)
            )
    else:
        release_paths = [
            item["path"]
            for item in by_path.values()
            if item["type"] == "process" and "(Sched)" in item["name"]
        ]

    deploy: list[str] = []
    reasons: dict[str, list[str]] = {}
    for path in release_paths:
        component = by_path[path]
        why: list[str] = []
        if path in changed:
            why.append("process XML changed")
        for other in selected:
            if other["path"] == path or not other["id"]:
                continue
            if other["id"] in component["text"]:
                why.append(f"references {other['name']} ({other['id']})")
        if why:
            deploy.append(path)
            reasons[path] = why

    subprocesses = [
        item["path"] for item in selected if item["type"] == "process" and "(Sub)" in item["name"]
    ]
    return {
        "push": [item["path"] for item in selected],
        "deploy": deploy,
        "deploy_reasons": reasons,
        "skipped_subprocesses": subprocesses,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--base", help="Git revision at the start of the range")
    source.add_argument("--files", nargs="+", help="Component XML paths to include")
    parser.add_argument("--head", default="HEAD", help="Git revision at the end of the range")
    args = parser.parse_args()

    if args.files:
        changed = [path.replace("\\", "/") for path in args.files]
    else:
        changed = git_changed(args.base, args.head)

    plan = build_plan(changed)
    json.dump(plan, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
