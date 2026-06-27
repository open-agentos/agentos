#!/usr/bin/env python3
"""
scripts/github_token.py — Runtime GitHub App installation token minter.

Called by GHA workflows to mint a short-lived installation token for a role.

Usage:
    python3 scripts/github_token.py <role>

Role is one of: builder, reviewer, watcher, board, docs, planner

Credential env vars (ROLE = uppercased role name, e.g. BUILDER):
    {ROLE}_APP_ID          GitHub App ID
    {ROLE}_PRIVATE_KEY     Raw PEM content (literal \\n sequences honoured)

Repo env vars (first match wins):
    TARGET_REPO            owner/repo of the target repository
    GITHUB_REPOSITORY      fallback (set automatically by GHA)

Prints the installation token to stdout with no trailing newline.
Exits non-zero on any error.

Requires: PyJWT>=2.8, cryptography>=41.0, requests>=2.31
"""

from __future__ import annotations

import os
import sys
import time

import jwt
import requests
from cryptography.hazmat.primitives.serialization import load_pem_private_key

GITHUB_API = "https://api.github.com"


def _load_private_key(role: str):
    role_u = role.upper()
    content = os.environ.get(f"{role_u}_PRIVATE_KEY")
    if not content:
        raise KeyError(f"{role_u}_PRIVATE_KEY is not set")
    # GHA secrets store literal \n — restore real newlines.
    if "\\n" in content and "\n" not in content.strip("\n"):
        content = content.replace("\\n", "\n")
    return load_pem_private_key(content.encode("utf-8"), password=None)


def _sign_jwt(app_id: str, private_key) -> str:
    now = int(time.time())
    return jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": app_id},
        private_key,
        algorithm="RS256",
    )


def _auth_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get_installation_id(jwt_token: str, repo: str) -> str:
    if not repo or "/" not in repo:
        raise RuntimeError(
            "Cannot discover installation ID: set TARGET_REPO or GITHUB_REPOSITORY (owner/repo)"
        )
    owner, repo_name = repo.split("/", 1)
    resp = requests.get(
        f"{GITHUB_API}/repos/{owner}/{repo_name}/installation",
        headers=_auth_headers(jwt_token),
        timeout=20,
    )
    resp.raise_for_status()
    return str(resp.json()["id"])


def mint_token(role: str) -> str:
    role_u = role.upper()
    app_id = os.environ.get(f"{role_u}_APP_ID")
    if not app_id:
        raise KeyError(f"{role_u}_APP_ID is not set")

    private_key = _load_private_key(role)
    jwt_token = _sign_jwt(app_id, private_key)

    install_id = os.environ.get(f"{role_u}_INSTALLATION_ID")
    if not install_id:
        repo = os.environ.get("TARGET_REPO") or os.environ.get("GITHUB_REPOSITORY", "")
        install_id = _get_installation_id(jwt_token, repo)

    resp = requests.post(
        f"{GITHUB_API}/app/installations/{install_id}/access_tokens",
        headers=_auth_headers(jwt_token),
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()["token"]


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/github_token.py <role>", file=sys.stderr)
        sys.exit(1)

    role_arg = sys.argv[1].lower()
    try:
        token = mint_token(role_arg)
        print(token, end="")
    except KeyError as exc:
        print(f"Missing env var: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
