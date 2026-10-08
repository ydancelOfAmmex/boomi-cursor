# Boomi CI/CD

GitHub Actions validates component XML on every pull request. Once a day, and when you run the workflow with action `pull`, it downloads the current components from the Boomi account into `active-development/` and commits them. A push to `main` then deploys the changed release processes to the **test** environment. Production deploy is a manual run with action `deploy`. A commit created by the pull (`[boomi-pull]`) does not deploy.

The pull lists every current component on the account default branch. Connection components stay on the platform and are not written into git. Later runs download a component only when its platform version changed. `ci/pull-index.json` records the version last saved.

Deploy replaces the existing deployment of each release process in the target environment. Subprocesses are not deployed on their own. They are pushed, and a parent in `ci/deploy-targets.txt` is redeployed when that parent's XML references the changed component id.

## GitHub setup

Repository secrets (Settings → Secrets and variables → Actions):

| Secret | Value |
| --- | --- |
| `BOOMI_API_URL` | Platform base URL, no `/api` suffix |
| `BOOMI_USERNAME` | API user |
| `BOOMI_API_TOKEN` | API token |
| `BOOMI_ACCOUNT_ID` | Account id |

Create two GitHub Environments, `boomi-test` and `boomi-production`. On each, set `BOOMI_ENVIRONMENT_ID` to that Boomi environment's id (Manage → Atom Management; the id is in the URL). Add required reviewers on `boomi-production` so a production run waits for approval.

The repository is not a git repo yet. Initialize it, push to GitHub, and the workflow in `.github/workflows/boomi.yml` runs from there.

## Local check

```bash
python ci/validate_components.py
python ci/pull_account.py --list-only
python ci/plan_release.py --files "active-development/process/[FWK] (Sub) Handle Integration Error.xml"
bash ci/deploy.sh --files path/to/component.xml --dry-run
```
