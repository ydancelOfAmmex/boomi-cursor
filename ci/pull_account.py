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
import base64
import json
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
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
HTTP_CODE = re.compile(r"HTTP ([0-9]{3})\b")
ERROR_LINE = re.compile(r"^ERROR:.*$", re.MULTILINE)
SEARCH_TIMEOUT = 300
PULL_TIMEOUT = 180


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
    # A raw tab, CR, or other C0 control in the name lands in the sync-state
    # JSON. jq rejects that string (U+0000–U+001F must be escaped).
    text = re.sub(r"[\x00-\x1f\x7f]", "_", text)
    for char in '<>:"/\\|?*':
        text = text.replace(char, "_")
    text = text.strip(". ")
    return text or "unnamed_component"


def load_index() -> dict:
    if not INDEX_PATH.is_file():
        return {"components": {}}
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"{INDEX_PATH.relative_to(ROOT).as_posix()} is not valid JSON ({exc}). "
            "The pull stopped so a damaged index cannot delete local files."
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("components"), dict):
        raise SystemExit(
            f"{INDEX_PATH.relative_to(ROOT).as_posix()} has no components object. "
            "The pull stopped so a damaged index cannot delete local files."
        )
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
            text = path.read_text(encoding="utf-8", errors="replace")
            root = ET.fromstring(text)
        except OSError as exc:
            raise SystemExit(f"Could not read {path.relative_to(ROOT).as_posix()}: {exc}") from exc
        except ET.ParseError:
            match = ROOT_ID.search(text)
            component_id = match.group(1) if match else ""
        else:
            if root.tag != COMPONENT_TAG:
                continue
            component_id = root.attrib.get("componentId", "")
        if component_id:
            found[component_id] = path
    return found


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def api_host(base: str) -> str:
    if not base:
        return "(missing)"
    return base.split("://", 1)[-1].split("/")[0] or "(missing)"


def redact(text: str, secrets: list[str]) -> str:
    redacted = text
    for secret in secrets:
        if secret and len(secret) >= 6:
            redacted = redacted.replace(secret, "***")
    return redacted


def http_help(code: str, *, after_connect: bool) -> str:
    if code == "000" and after_connect:
        return (
            "HTTP 000 means Boomi sent no response. The connectivity check had already "
            "reached the API, so this request likely timed out. Re-run the pull."
        )
    if code == "000":
        return (
            "HTTP 000 means Boomi sent no response. Set the BOOMI_API_URL secret to "
            "https://api.boomi.com with no path, quotes, or spaces."
        )
    if code == "401":
        return "Boomi rejected the API credentials (HTTP 401). Check BOOMI_USERNAME and BOOMI_API_TOKEN."
    if code == "403":
        return "The API user is not allowed to read this component (HTTP 403)."
    if code == "404":
        return "Boomi could not find the account or component (HTTP 404). Check BOOMI_ACCOUNT_ID."
    if code in {"429", "503"}:
        return f"Boomi asked the pull to back off (HTTP {code}). Re-run the pull; finished components are skipped."
    if code.startswith("5"):
        return f"Boomi returned a server error (HTTP {code}). Re-run the pull."
    if code:
        return f"Boomi returned HTTP {code}."
    return ""


def explain_output(output: str, *, after_connect: bool) -> str:
    match = HTTP_CODE.search(output)
    code = match.group(1) if match else ""
    parts = [http_help(code, after_connect=after_connect)]
    errors = [line.strip() for line in ERROR_LINE.findall(output)]
    if errors:
        parts.append(errors[-1][:500])
    elif output.strip():
        parts.append(output.strip().splitlines()[-1][:500])
    return "\n".join(part for part in parts if part)


class BoomiScriptError(Exception):
    """A Boomi shell script failed. The message is safe to print."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


def run_boomi(args: list[str], timeout: int, secrets: list[str]) -> str:
    command = Path(args[1]).name if len(args) > 1 else args[0]
    try:
        result = subprocess.run(
            args,
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise SystemExit(
            "bash was not found, so the Boomi scripts could not run. "
            "GitHub Actions provides bash. On Windows, install Git for Windows and reopen the terminal."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise BoomiScriptError(f"{command} timed out after {timeout} seconds.") from exc
    output = redact((result.stdout or "") + (result.stderr or ""), secrets)
    sys.stdout.write(result.stdout or "")
    sys.stderr.write(redact(result.stderr or "", secrets))
    if result.returncode != 0:
        detail = explain_output(output, after_connect=True)
        raise BoomiScriptError(detail or f"{command} failed with exit code {result.returncode}.")
    return output


def check_api() -> list[str]:
    """Confirm the API answers, and return secret values that later output must hide."""
    env_path = ROOT / ".env"
    if not env_path.is_file():
        raise SystemExit(
            ".env is missing from the project root. "
            "GitHub Actions writes it from the repository secrets. Locally, put the Boomi API values in .env."
        )
    env = read_env(env_path)
    required = ("BOOMI_API_URL", "BOOMI_ACCOUNT_ID", "BOOMI_USERNAME", "BOOMI_API_TOKEN")
    missing = [name for name in required if not env.get(name)]
    base = env.get("BOOMI_API_URL", "").rstrip("/")
    host = api_host(base)
    print(f"Connecting to Boomi API host {host}")
    if missing or not base.startswith("https://"):
        detail = f"Missing {', '.join(missing)}. " if missing else ""
        raise SystemExit(
            f"{detail}BOOMI_API_URL must be https://api.boomi.com with no path. This run is using host {host}."
        )
    account = env["BOOMI_ACCOUNT_ID"]
    user = env["BOOMI_USERNAME"]
    token = env["BOOMI_API_TOKEN"]
    url = f"{base}/api/rest/v1/{account}/ComponentMetadata/query"
    body = json.dumps(
        {
            "QueryFilter": {
                "expression": {
                    "operator": "and",
                    "nestedExpression": [
                        {"operator": "EQUALS", "property": "currentVersion", "argument": ["true"]},
                        {"operator": "EQUALS", "property": "deleted", "argument": ["false"]},
                        {"operator": "EQUALS", "property": "type", "argument": ["process"]},
                    ],
                }
            }
        }
    ).encode()
    request = urllib.request.Request(url, data=body, method="POST")
    credential = base64.b64encode(f"BOOMI_TOKEN.{user}:{token}".encode()).decode()
    request.add_header("Authorization", "Basic " + credential)
    request.add_header("Accept", "application/json")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode())
    except TimeoutError as exc:
        raise SystemExit(f"Timed out after 30 seconds connecting to {host}.") from exc
    except urllib.error.HTTPError as exc:
        detail = redact(exc.read().decode(errors="replace")[:300], [token, user])
        message = http_help(str(exc.code), after_connect=False)
        raise SystemExit(f"{message} Host {host}. {detail}".strip()) from exc
    except ssl.SSLError as exc:
        raise SystemExit(f"TLS handshake with {host} failed. {exc}") from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            raise SystemExit(f"Timed out after 30 seconds connecting to {host}.") from exc
        if isinstance(reason, ssl.SSLError):
            raise SystemExit(f"TLS handshake with {host} failed. {reason}") from exc
        raise SystemExit(
            f"Could not connect to Boomi API host {host}. {reason}. "
            "Set the BOOMI_API_URL secret to https://api.boomi.com with no path, quotes, or spaces."
        ) from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Boomi answered from {host}, but the response was not JSON. {exc}") from exc
    print(f"Connected. The account has {payload.get('numberOfResults', 'unknown')} current process component(s).")
    return [token, user]


def search_components(secrets: list[str]) -> tuple[list[dict], bool]:
    before = set(INVENTORIES.glob("component_search_*.json")) if INVENTORIES.is_dir() else set()
    try:
        output = run_boomi(
            ["bash", "boomi-integration/scripts/boomi-component-search.sh", "--name", "%"],
            SEARCH_TIMEOUT,
            secrets,
        )
    except BoomiScriptError as exc:
        raise SystemExit(f"Could not list components.\n{exc}") from exc

    after = set(INVENTORIES.glob("component_search_*.json"))
    created = after - before
    if not created:
        raise SystemExit("Component search finished without writing an inventory file.")
    inventory = max(created, key=lambda path: path.stat().st_mtime)
    try:
        payload = json.loads(inventory.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Component search wrote an unreadable inventory file: {exc}") from exc
    except OSError as exc:
        raise SystemExit(f"Could not read the component search inventory: {exc}") from exc
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise SystemExit("Component search inventory has no records list.")
    truncated = bool(payload.get("metadata", {}).get("truncated")) if isinstance(payload.get("metadata"), dict) else False
    warned = "WARN:" in output
    complete = not truncated and not warned and len(records) > 0
    if not complete:
        reason = "the folder walk was truncated" if truncated else "the query warned that a page was missing"
        if not records:
            reason = "the query returned no components"
        print(f"Listing is partial ({reason}). Existing files will be left in place.")
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


def pull_one(component_id: str, rel: str, secrets: list[str]) -> None:
    run_boomi(
        [
            "bash",
            "boomi-integration/scripts/boomi-component-pull.sh",
            "--component-id",
            component_id,
            "--target-path",
            rel,
        ],
        PULL_TIMEOUT,
        secrets,
    )


def sync_state_path(rel: str) -> Path:
    suffix = rel.removeprefix("active-development/")
    if suffix.endswith(".xml"):
        suffix = suffix[:-4]
    return DEV / ".sync-state" / (suffix.replace("/", "__") + ".json")


def remove_local(rel: str, component_id: str) -> bool:
    try:
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
    except OSError as exc:
        print(f"Could not remove {rel}: {exc}", file=sys.stderr)
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-only", action="store_true", help="List the account and exit without downloading")
    args = parser.parse_args()

    secrets = check_api()
    records, complete = search_components(secrets)
    rows = [row for row in records if isinstance(row, dict)]
    if len(rows) != len(records):
        complete = False
        print(
            f"Skipped {len(records) - len(rows)} listing record(s) that were not component objects. "
            "Existing files will be left in place.",
            file=sys.stderr,
        )
    skipped = [row for row in rows if row.get("type") in EXCLUDED_TYPES]
    selected = [row for row in rows if row.get("componentId") and row.get("type") not in EXCLUDED_TYPES]
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
            pull_one(component_id, rel, secrets)
        except BoomiScriptError as exc:
            failures.append(f"{name} ({component_id})\n    {exc}")
            print(f"Failed {name}: {exc}", file=sys.stderr)
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
            if remove_local(meta.get("path", ""), component_id):
                del components[component_id]
                removed += 1
    elif not complete:
        print("Listing was partial. Existing files were left in place.")
    elif failures:
        print("Some downloads failed. Existing files for other components were left in place.")

    try:
        save_index(index)
    except OSError as exc:
        print(f"Could not write {INDEX_PATH.relative_to(ROOT).as_posix()}: {exc}", file=sys.stderr)
        return 1
    print(f"Pulled {downloaded}, unchanged {unchanged}, removed {removed}, failed {len(failures)}.")
    if failures:
        print(
            "Failed components are not recorded as current, so the next pull retries them:",
            file=sys.stderr,
        )
        for item in failures:
            print(f"  {item}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
