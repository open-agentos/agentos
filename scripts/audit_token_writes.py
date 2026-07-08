#!/usr/bin/env python3
"""F11.3 — Static audit: status:* label writes must use a minted App token
when a different workflow depends on the resulting issues.labeled event.

Background: GitHub does NOT fire new workflow runs for API writes authenticated
with the default GITHUB_TOKEN (loop-prevention, platform-level). This means
any job that writes a status:* label and relies on agent-orchestrator.yml's
`on: issues: types: [labeled]` trigger MUST mint an App token first.

The v1.2.2 F11 fix patched /approve-intent. This audit checks for any other
instances of the pattern.

Methodology:
  For each job in each workflow file, scan for:
    1. addLabels / addLabel / addLabels calls with a status: value
    2. AND the job uses `github-token: ${{ secrets.GITHUB_TOKEN }}` (bare token)
  Flag if both are true AND the workflow is NOT agent-orchestrator.yml itself
  (the orchestrator doesn't need to re-trigger itself).

Exit 0 = clean. Exit 1 = violations found.
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
TEMPLATE_WORKFLOWS_DIR = REPO_ROOT / "bootstrap" / "templates" / "workflows"

# Pattern: a status: label value being written
STATUS_LABEL_RE = re.compile(r"['\"]status:[^'\"]+['\"]")

# Pattern: bare GITHUB_TOKEN used as github-token (not a watcher/app token)
BARE_TOKEN_RE = re.compile(r"github-token:\s*\$\{\{?\s*secrets\.GITHUB_TOKEN\s*\}?\}")


# Pattern: addLabels call (JS) — indicates a label write
ADD_LABELS_RE = re.compile(r"\.addLabels\(|addLabels\s*\(")

# Workflow that is the trigger receiver — writing labels here is fine,
# it is the orchestrator itself.
SKIP_WORKFLOWS = {"agent-orchestrator.yml"}


def check_workflow(path: Path) -> list[str]:
    violations: list[str] = []
    if path.name in SKIP_WORKFLOWS:
        return violations

    text = path.read_text(encoding="utf-8")

    # Split into jobs (same simple approach as check_runner_prereqs.py)
    jobs: dict[str, list[str]] = {}
    current_job = None
    in_jobs = False

    for line in text.splitlines():
        if re.match(r"^jobs:\s*$", line):
            in_jobs = True
            continue
        if not in_jobs:
            continue
        m = re.match(r"^  (\w[\w-]*):\s*$", line)
        if m:
            current_job = m.group(1)
            jobs[current_job] = []
            continue
        if current_job is not None:
            jobs[current_job].append(line)

    for job_name, job_lines in jobs.items():
        job_text = "\n".join(job_lines)

        writes_status_label = bool(ADD_LABELS_RE.search(job_text)) and bool(
            STATUS_LABEL_RE.search(job_text)
        )
        uses_bare_token = bool(BARE_TOKEN_RE.search(job_text))

        if writes_status_label and uses_bare_token:
            violations.append(
                f"  {path.name}: job '{job_name}' writes a status:* label "
                f"with bare GITHUB_TOKEN — issues.labeled event will NOT fire "
                f"for other workflows. Use a minted App token instead. "
                f"(See v1.2.2 F11 for field evidence.)"
            )

    return violations


def main() -> int:
    all_violations: list[str] = []

    for wf_dir in [WORKFLOWS_DIR, TEMPLATE_WORKFLOWS_DIR]:
        if not wf_dir.exists():
            continue
        for wf_file in sorted(wf_dir.glob("*.yml")):
            all_violations.extend(check_workflow(wf_file))

    if all_violations:
        print("ERROR: status:* label writes using bare GITHUB_TOKEN:")
        for v in all_violations:
            print(v)
        return 1

    print("OK: no status:* label writes with bare GITHUB_TOKEN found in non-orchestrator jobs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
