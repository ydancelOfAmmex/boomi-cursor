#!/usr/bin/env python3
"""Pull current component XML from the Boomi account into active-development/.

Lists every current component on the account default branch, then downloads
each one whose platform version is not already stored locally. Connection
components (connector-settings) are skipped: a pulled password is a platform
token, and committing it would store credential material in git.

Writes ci/pull-index.json so the next run downloads only components whose
version changed. Removes a previously indexed file only when the listing
completed and that component is gone from the account.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "active-development"
INDEX_PATH = ROOT / "ci" / "pull-index.json"
INVENTORIES = DEV / "inventories"
NS = "http://api.platform.boomi.com/"
COMPONENT_TAG = f"{{{NS}}}Component"
SKIP_DIRS = {".sync-state", ".shasum-shim", "inventories", "feedback"}
# Pulled password fields are write-only platform tokens, not the real secret.
EXCLUDED_TYPES = {"connector-settings"}
ROOT_ID = re.compile(r"<bns:Component\b[^>]*\bcomponentId=\"([^\"]+)\"")


def xml_attr_text(name: str) -> str:
    """Match the pull script, which reads the raw XML attribute text."""
    return (
        name.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def safe_filename(name: str) -> str:
    text = xml_attr_text(name)
    for char in '<>:"/\\|?*':
        text = text.replace(char, "_")
    text = text.strip(". ")
    return text or "unnamed_component"


def load_index() -> dict:
    if not INDEX_PATH.is_file():
        return {"components": {}}
    data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    data.setdefault("components", {})
    return data


def save_index(index: dict) -> None:
    index["updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    INDEX_PATH.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")


def local_components() -> dict[str, Path]:
    found: dict[str, Path] = {}
    if not DEV.is_dir():
        return found
    for path in DEV.rglob("*.xml"):
        if any(part in SKIP_DIRS for part in path.relative_to(DEV).parts):
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            match = ROOT_ID.search(path.read_text(encoding="utf-8", errors="replace"))
            component_id = match.group(1) if match else ""
        else:
            if root.tag != COMPONENT_TAG:
                continue
            component_id = root.attrib.get("componentId", "")
        if component_id:
            found[component_id] = path
    return found


def search_components() -> tuple[list[dict], bool]:
    before = set(INVENTORIES.glob("component_search_*.json")) if INVENTORIES.is_dir() else set()
    result = subprocess.run(
        ["bash", "boomi-integration/scripts/boomi-component-search.sh", "--name", "%"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode != 0:
        raise SystemExit(result.returncode)

    after = set(INVENTORIES.glob("component_search_*.json"))
    created = after - before
    if not created:
        raise SystemExit("Component search did not write an inventory file.")
    inventory = max(created, key=lambda path: path.stat().st_mtime)
    payload = json.loads(inventory.read_text(encoding="utf-8"))
    records = payload.get("records") or []
    truncated = bool(payload.get("metadata", {}).get("truncated"))
    warned = "WARN:" in result.stdout or "WARN:" in result.stderr
    complete = not truncated and not warned and len(records) > 0
    return records, complete


def destination(record: dict, by_id: dict[str, Path], taken: dict[str, str]) -> str:
    component_id = record["componentId"]
    existing = by_id.get(component_id)
    if existing is not None:
        rel = existing.relative_to(ROOT).as_posix()
        taken[rel] = component_id
        return rel

    stem = safe_filename(record.get("name") or "unnamed_component")
    type_dir = (record.get("type") or "unknown").lower()
    rel = f"active-development/{type_dir}/{stem}.xml"
    owner = taken.get(rel)
    if owner not in (None, component_id):
        rel = f"active-development/{type_dir}/{stem}__{component_id}.xml"
    taken[rel] = component_id
    return rel


def pull_one(component_id: str, rel: str) -> None:
    subprocess.run(
        [
            "bash",
            "boomi-integration/scripts/boomi-component-pull.sh",
            "--component-id",
            component_id,
            "--target-path",
            rel,
        ],
        cwd=ROOT,
        check=True,
    )


def sync_state_path(rel: str) -> Path:
    suffix = rel.removeprefix("active-development/")
    if suffix.endswith(".xml"):
        suffix = suffix[:-4]
    return DEV / ".sync-state" / (suffix.replace("/", "__") + ".json")


def remove_local(rel: str, component_id: str) -> None:
    path = ROOT / rel
    if path.is_file():
        path.unlink()
        print(f"Removed {rel}")
    state = sync_state_path(rel)
    if state.is_file():
        state.unlink()
    legacy = DEV / ".sync-state" / (Path(rel).stem + ".json")
    if legacy.is_file():
        try:
            stored = json.loads(legacy.read_text(encoding="utf-8")).get("component_id")
        except json.JSONDecodeError:
            stored = None
        if stored == component_id:
            legacy.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-only", action="store_true", help="List the account and exit without downloading")
    args = parser.parse_args()

    records, complete = search_components()
    skipped = [row for row in records if row.get("type") in EXCLUDED_TYPES]
    selected = [row for row in records if row.get("componentId") and row.get("type") not in EXCLUDED_TYPES]
    print(
        f"Account listing: {len(records)} component(s), "
        f"{len(selected)} to sync, {len(skipped)} connection(s) left on the platform."
    )
    if args.list_only:
        return 0

    index = load_index()
    components = index["components"]
    by_id = local_components()
    taken = {path.relative_to(ROOT).as_posix(): component_id for component_id, path in by_id.items()}
    remote_ids: set[str] = set()
    failures: list[str] = []
    downloaded = 0
    unchanged = 0

    for record in selected:
        component_id = record["componentId"]
        remote_ids.add(component_id)
        rel = destination(record, by_id, taken)
        version = record.get("version")
        prior = components.get(component_id) or {}
        if prior.get("version") == version and (ROOT / rel).is_file():
            unchanged += 1
            components[component_id] = {
                "version": version,
                "type": record.get("type"),
                "name": record.get("name"),
                "path": rel,
            }
            continue
        name = record.get("name") or component_id
        print(f"Pulling {name} ({record.get('type')}) version {version}")
        try:
            pull_one(component_id, rel)
        except subprocess.CalledProcessError:
            failures.append(f"{name} ({component_id})")
            continue
        downloaded += 1
        components[component_id] = {
            "version": version,
            "type": record.get("type"),
            "name": record.get("name"),
            "path": rel,
        }

    removed = 0
    if complete and not failures and selected:
        for component_id, meta in list(components.items()):
            if component_id in remote_ids:
                continue
            remove_local(meta.get("path", ""), component_id)
            del components[component_id]
            removed += 1
    elif not complete:
        print("Listing was partial. Existing files were left in place.")

    save_index(index)
    print(f"Pulled {downloaded}, unchanged {unchanged}, removed {removed}, failed {len(failures)}.")
    if failures:
        print("Failed components:")
        for item in failures:
            print(f"  {item}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
