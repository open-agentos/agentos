#!/usr/bin/env python3
"""CI check: user-facing install references must match the version in pyproject.toml.

Checks a fixed set of files that contain 'agentOS init --from github:open-agentos/agentos@vX.Y.Z'
install references. Every such reference must match the current version.

Files intentionally excluded (these contain fictional plugin version examples,
historical changelog notes, upgrade logic comments, or third-party library refs):
  - CHANGELOG.md       — historical version entries; stale references are correct
  - SPEC.md            — fictional plugin example (@v1.2.0 in a plugin URL)
  - agentOS.yaml       — plugin example in a comment block
  - bootstrap/agentOS.yaml — same
  - docs/plugins.md    — fictional third-party plugin version examples
  - bootstrap/upgrade.py — version normalisation comments
  - bootstrap/cli.py   — historical 'before v1.2.0' note

Exit 0 = all good. Exit 1 = stale references found (printed to stdout).
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

# Files that carry user-facing install instructions referencing a specific version.
# These must stay current.
CHECKED_FILES = [
    "README.md",
    "docs/getting-started.md",
]

VERSION_RE = re.compile(r"github:open-agentos/agentos@(v[\d.]+)")


def get_current_version() -> str:
    """Read the canonical version from pyproject.toml."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([\d.]+)"', pyproject, re.MULTILINE)
    if not m:
        raise RuntimeError("Could not parse version from pyproject.toml")
    return m.group(1)


def main() -> int:
    current = get_current_version()
    expected_tag = f"v{current}"
    violations: list[str] = []

    for rel_path in CHECKED_FILES:
        path = REPO_ROOT / rel_path
        if not path.exists():
            print(f"WARNING: {rel_path} not found — skipping")
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in VERSION_RE.finditer(line):
                found = m.group(1)
                if found != expected_tag:
                    violations.append(
                        f"  {rel_path}:{lineno}: found {found}, expected {expected_tag}"
                    )

    if violations:
        print(f"ERROR: stale install references (current version is {expected_tag}):")
        for v in violations:
            print(v)
        print(
            "\nUpdate these references to match the version in pyproject.toml.\n"
            "Run: grep -rn 'open-agentos/agentos@v' README.md docs/getting-started.md"
        )
        return 1

    print(f"OK: all install references in checked files point to {expected_tag}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
