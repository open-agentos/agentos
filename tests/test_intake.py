"""
tests/test_intake.py — Guards for the v1.2 intake feature (SPEC.md §12).

Covers:
  - intake: config block presence and normative defaults in agentOS.yaml
  - governance.wild block defaults
  - new labels (source:wild, status:intake, status:intake-review, follow-on
    additions, agent axis additions) with spec-mandated colours and routing
  - janitor / archaeologist role definitions and their permission boundaries
  - agentOS.yaml validates against schema/agentOS.schema.json (this is the
    regression guard for the governance-block schema gap found during 1.2.0)
  - the intake workflow template's security invariants:
      * recursion guard present (§12.2.4 — normative, MUST be tested)
      * never uses pull_request_target (§12.2.6)
      * archaeologist step receives no GitHub token
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from bootstrap.labels import labels_from_spec

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "agentOS.yaml"
SCHEMA_PATH = REPO_ROOT / "schema" / "agentOS.schema.json"
INTAKE_WORKFLOW = REPO_ROOT / "templates" / "workflows" / "agent-intake.yml"


@pytest.fixture(scope="module")
def spec() -> dict:
    with open(SPEC_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def label_map(spec) -> dict:
    return {lbl.name: lbl for lbl in labels_from_spec(spec)}


# ---------------------------------------------------------------------------
# Config block
# ---------------------------------------------------------------------------


def test_intake_block_present_with_normative_defaults(spec):
    intake = spec["intake"]
    assert intake["enabled"] is True
    # Bot-flood guard MUST default to a list including these two (§12.2.2).
    assert "dependabot[bot]" in intake["exclude_actors"]
    assert "renovate[bot]" in intake["exclude_actors"]
    assert intake["linked_prs"] == "ignore"
    assert intake["settle_seconds"] == 300
    assert intake["convergence_passes"] == 2
    assert intake["test_oracle"] == "auto"
    assert intake["max_diff_lines"] == 6000
    assert intake["max_recon_runs"] == 5
    assert intake["model"] is None
    assert intake["approve_intent_self"] is True


def test_intake_tripwire_defaults(spec):
    """§12.6.2 default tripwire paths must all be present."""
    paths = spec["intake"]["tripwire_paths"]
    for required in (".github/workflows/**", ".github/actions/**",
                    "agentOS.yaml", ".agentOS/**"):
        assert required in paths, f"Missing default tripwire path: {required}"


def test_init_posture_all_janitors_report_mode(spec):
    """§12.11: init MUST ship every janitor in mode: report."""
    janitors = spec["intake"]["janitors"]
    assert janitors, "intake.janitors must not be empty"
    for j in janitors:
        assert j["mode"] == "report", (
            f"Janitor '{j['name']}' ships in mode '{j['mode']}'; the "
            "first-contact posture is report-only for every janitor."
        )


def test_intake_reports_defaults(spec):
    """§12.8.4: security and deps default to manual launch."""
    reports = spec["intake"]["reports"]
    assert reports["lint"] == {"file": "auto", "start": "auto"}
    assert reports["tests"] == {"file": "auto", "start": "auto"}
    assert reports["security"]["start"] == "manual"
    assert reports["deps"]["start"] == "manual"


def test_governance_wild_defaults(spec):
    """§12.9: wild governance is stricter than the planned lane."""
    wild = spec["governance"]["wild"]
    assert wild["auto_merge"] is False
    assert wild["final_approval"] == "human"
    assert wild["max_review_cycles"] == 2
    assert wild["changes_requested_routes_to"] in ("builder", "author")


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def test_intake_status_labels(label_map):
    intake = label_map["status:intake"]
    assert intake.color == "c5b8f0"          # spec-mandated (§12.3.2)
    assert intake.routes_to == "archaeologist"

    review = label_map["status:intake-review"]
    assert review.color == "e4d069"          # spec-mandated (§12.3.2)
    assert review.routes_to is None          # awaiting human /approve-intent


def test_source_wild_label(label_map):
    wild = label_map["source:wild"]
    assert wild.color == "d4c5f9"            # spec-mandated (§12.3.1)


def test_intake_follow_on_labels(label_map):
    for name in ("follow-on:needs-cleanup", "follow-on:needs-tests",
                 "follow-on:needs-security-review"):
        assert name in label_map, f"Missing intake follow-on label: {name}"
        # Consumed by the watcher at settlement, not orchestrator-routed.
        assert label_map[name].routes_to is None


def test_intake_agent_axis_labels(label_map):
    assert "agent:janitor" in label_map
    assert "agent:archaeologist" in label_map


# ---------------------------------------------------------------------------
# Roles and permission boundaries
# ---------------------------------------------------------------------------


def _agent(spec, agent_id):
    for a in spec["agents"]:
        if a["id"] == agent_id:
            return a
    raise AssertionError(f"Agent '{agent_id}' not defined in agentOS.yaml")


def test_janitor_role_permission_table(spec):
    """§12.9.2: contents/checks/pull_requests write; NO issues, NO workflows."""
    janitor = _agent(spec, "janitor")
    assert janitor["create_app"] is True
    perms = janitor["permissions"]
    assert perms.get("contents") == "write"
    assert perms.get("checks") == "write"
    assert perms.get("pull_requests") == "write"
    assert "issues" not in perms, "Janitor MUST NOT hold issues:write (§12.9.2)"
    assert "workflows" not in perms, "Janitor MUST NOT hold workflows:write (§12.9.2)"


def test_archaeologist_has_no_app_identity(spec):
    """§12.6.1 / §12.9.2: the archaeologist has no App identity at all."""
    arch = _agent(spec, "archaeologist")
    assert arch.get("create_app") is False
    assert "reuse_app" not in arch, (
        "Archaeologist must not reuse another App's credentials — it holds no token."
    )
    assert not arch.get("permissions"), (
        "Archaeologist must declare no permissions — all writes are mediated."
    )


def test_status_intake_routes_to_archaeologist_in_spec(spec):
    for axis in spec["labels"]:
        if axis["axis"] == "status":
            values = {v["name"]: v for v in axis["values"]}
            assert values["intake"]["routes_to"] == "archaeologist"
            assert values["intake-review"]["routes_to"] is None
            return
    raise AssertionError("status axis missing from spec")


# ---------------------------------------------------------------------------
# Schema validation (regression guard for the governance schema gap)
# ---------------------------------------------------------------------------


def test_agentos_yaml_validates_against_schema(spec):
    """The root agentOS.yaml must validate against its own JSON schema.

    This is the guard that would have caught the v1.1 drift where the
    governance: block existed in agentOS.yaml but not in the schema
    (additionalProperties: false makes that a hard failure).
    """
    jsonschema = pytest.importorskip("jsonschema")
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        schema = json.load(f)
    jsonschema.validate(instance=spec, schema=schema)


# ---------------------------------------------------------------------------
# Workflow template security invariants
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def intake_workflow_text() -> str:
    return INTAKE_WORKFLOW.read_text(encoding="utf-8")


def test_intake_workflow_parses_and_has_expected_jobs(intake_workflow_text):
    doc = yaml.safe_load(intake_workflow_text)
    jobs = set(doc["jobs"].keys())
    assert {"classify", "janitors", "archaeologist", "approve-intent"} <= jobs
    # Trigger events per §12.2.1 (yaml parses bare `on:` as boolean True).
    triggers = doc.get("on") or doc.get(True)
    pr_types = set(triggers["pull_request"]["types"])
    assert {"opened", "reopened", "ready_for_review", "synchronize", "edited"} <= pr_types


def test_recursion_guard_present(intake_workflow_text):
    """§12.2.4 is normative and load-bearing: the classifier must resolve the
    pushing actor on synchronize and ignore agentOS App identities."""
    assert "RECURSION GUARD" in intake_workflow_text
    assert "isAgentIdentity(pushActor)" in intake_workflow_text
    assert "synchronize" in intake_workflow_text


def test_no_pull_request_target(intake_workflow_text):
    """§12.2.6: pull_request_target with contributed-code checkout is forbidden
    anywhere in the intake pipeline. We forbid the trigger outright."""
    assert "pull_request_target" not in intake_workflow_text.replace(
        "This workflow NEVER uses pull_request_target", "")


def test_archaeologist_step_receives_no_github_token(intake_workflow_text):
    """§12.6.1: the archaeologist runner step must not be handed a token."""
    doc = yaml.safe_load(intake_workflow_text)
    steps = doc["jobs"]["archaeologist"]["steps"]
    runner_steps = [s for s in steps if s.get("id") == "recon"]
    assert runner_steps, "archaeologist job must contain the 'recon' runner step"
    env = runner_steps[0].get("env", {})
    assert "GITHUB_TOKEN" not in env, (
        "The archaeologist is a pure function and holds no GitHub token (§12.6.1)."
    )


def test_intake_workflow_mirrors_synced():
    bundled = REPO_ROOT / "bootstrap" / "templates" / "workflows" / "agent-intake.yml"
    assert bundled.exists(), "bootstrap/templates missing agent-intake.yml"
    assert bundled.read_text(encoding="utf-8") == INTAKE_WORKFLOW.read_text(encoding="utf-8")


def test_archaeologist_agent_template_exists():
    for base in (REPO_ROOT / "templates", REPO_ROOT / "bootstrap" / "templates"):
        path = base / "agents" / "archaeologist" / "AGENT.md.template"
        assert path.exists(), f"Missing {path}"
        text = path.read_text(encoding="utf-8")
        assert "pure function" in text.lower()
        assert "NO GitHub token" in text or "no GitHub token" in text
