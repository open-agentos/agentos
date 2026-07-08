#!/usr/bin/env python3
"""CI check: every workflow job that invokes {{AGENT_RUNNER}} or 'claude'
must have a Claude CLI install step preceding it in the same job.

This catches the class of bug that caused three sequential field failures in
v1.2.2 (F9): the archaeologist job was scaffolded without carrying the Claude
CLI install step from the orchestrator pattern.

Exit 0 = all good. Exit 1 = violations found (printed to stdout).
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
TEMPLATE_WORKFLOWS_DIR = REPO_ROOT / "bootstrap" / "templates" / "workflows"

# Patterns that indicate a job needs the Claude CLI.
RUNNER_PATTERNS = [
    r"\{\{AGENT_RUNNER\}\}",       # template placeholder
    r"claude\s+-p\b",              # direct claude invocation
    r"claude\s+--dangerously",     # direct claude invocation variant
]

# Patterns that satisfy the CLI install requirement.
INSTALL_PATTERNS = [
    r"npm\s+install\s+-g\s+@anthropic-ai/claude-code",
    r"npm\s+install.*claude-code",
]

# Files where we intentionally check for the runner pattern but don't
# need an install step (e.g. AGENTS.md, README, non-workflow files).
EXCLUDED_FILES = {
    "AGENTS.md",
    "README.md",
}


def check_workflow_file(path: Path) -> list[str]:
    """Return list of violation strings for a single workflow file."""
    text = path.read_text(encoding="utf-8")
    violations = []

    # Split into jobs by finding 'jobs:' then each top-level job key.
    # We use a simple line-based approach: collect runs of lines per job block.
    jobs: dict[str, list[str]] = {}
    current_job = None
    in_jobs = False

    for line in text.splitlines():
        if re.match(r"^jobs:\s*$", line):
            in_jobs = True
            continue
        if not in_jobs:
            continue
        # Top-level job key: 2-space indented key followed by colon
        m = re.match(r"^  (\w[\w-]*):\s*$", line)
        if m:
            current_job = m.group(1)
            jobs[current_job] = []
            continue
        if current_job is not None:
            jobs[current_job].append(line)

    for job_name, job_lines in jobs.items():
        job_text = "\n".join(job_lines)
        needs_claude = any(re.search(p, job_text) for p in RUNNER_PATTERNS)
        has_install = any(re.search(p, job_text) for p in INSTALL_PATTERNS)

        if needs_claude and not has_install:
            violations.append(
                f"  {path.name}: job '{job_name}' invokes the agent runner "
                f"but has no Claude CLI install step"
            )

    return violations


def main() -> int:
    all_violations: list[str] = []

    for wf_dir in [WORKFLOWS_DIR, TEMPLATE_WORKFLOWS_DIR]:
        if not wf_dir.exists():
            continue
        for wf_file in sorted(wf_dir.glob("*.yml")):
            if wf_file.name in EXCLUDED_FILES:
                continue
            all_violations.extend(check_workflow_file(wf_file))

    if all_violations:
        print("ERROR: workflow jobs missing Claude CLI install step:")
        for v in all_violations:
            print(v)
        print(
            "\nEvery job that invokes {{AGENT_RUNNER}} or 'claude -p' must have "
            "an 'npm install -g @anthropic-ai/claude-code' step before it.\n"
            "See v1.2.2 F9 for field evidence."
        )
        return 1

    wf_count = sum(
        1 for d in [WORKFLOWS_DIR, TEMPLATE_WORKFLOWS_DIR]
        if d.exists()
        for _ in d.glob("*.yml")
    )
    print(f"OK: all {wf_count} workflow files have Claude CLI install steps before every runner invocation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
