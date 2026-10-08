#!/usr/bin/env bash
# Push changed Boomi components, then package and deploy release processes.
# Usage: bash ci/deploy.sh --base <rev> --head <rev> [--dry-run]
#    or: bash ci/deploy.sh --files <xml>... [--dry-run]
#
# Requires .env in the repo root (ci/write_env.py writes it in GitHub Actions).
# Deploy uses BOOMI_ENVIRONMENT_ID and replaces the existing deployment of each
# release process in that environment.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DRY_RUN=false
BASE=""
HEAD="HEAD"
FILES=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base) BASE="$2"; shift 2 ;;
    --head) HEAD="$2"; shift 2 ;;
    --files)
      shift
      while [[ $# -gt 0 && "$1" != --* ]]; do
        FILES+=("$1")
        shift
      done
      ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help)
      sed -n '2,8p' "$0"
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

PLAN="$(mktemp)"
trap 'rm -f "$PLAN"' EXIT

if [[ ${#FILES[@]} -gt 0 ]]; then
  python ci/plan_release.py --files "${FILES[@]}" > "$PLAN"
elif [[ -n "$BASE" ]]; then
  python ci/plan_release.py --base "$BASE" --head "$HEAD" > "$PLAN"
else
  echo "Pass --base <rev> or --files <xml>..." >&2
  exit 1
fi

echo "Release plan:"
cat "$PLAN"

push_count="$(jq -r '.push | length' "$PLAN")"
deploy_count="$(jq -r '.deploy | length' "$PLAN")"

if [[ "$push_count" == "0" ]]; then
  echo "No component XML changes to release."
  exit 0
fi

if $DRY_RUN; then
  echo "Dry run: ${push_count} component(s) would be pushed, ${deploy_count} process(es) deployed."
  exit 0
fi

if [[ ! -f .env ]]; then
  echo "ERROR: .env is missing. In GitHub Actions, run ci/write_env.py first." >&2
  exit 1
fi

notes="${BOOMI_DEPLOY_NOTES:-CI deploy $(date -u +%Y-%m-%dT%H:%M:%SZ)}"

while IFS= read -r file; do
  [[ -z "$file" ]] && continue
  echo "Pushing ${file}"
  bash boomi-integration/scripts/boomi-component-push.sh "$file"
done < <(jq -r '.push[]' "$PLAN")

while IFS= read -r file; do
  [[ -z "$file" ]] && continue
  echo "Deploying ${file}"
  bash boomi-integration/scripts/boomi-deploy.sh "$file" --deployment-notes "$notes"
done < <(jq -r '.deploy[]' "$PLAN")

echo "Release complete: pushed ${push_count}, deployed ${deploy_count}."
