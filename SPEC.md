# GitHub AgentOS Specification

**Version:** 1.2-draft
**Status:** Draft
**Org:** https://github.com/open-agentos

---

## 1. Purpose and Scope

This document is the normative specification for the GitHub AgentOS system. It defines:

1. The label model and state machine that drives agent routing
2. The agent role model and permission contract
3. The runtime interface that any agent runner must satisfy
4. The GitHub Actions workflow contract (triggers, outputs, receipts)
5. The Projects v2 board field contract
6. The JSONL run-record metrics schema
7. The plugin interface for extending the core system
8. The intake pipeline for unplanned ("wild") pull requests (§12)

This spec does NOT define:
- The content of agent system prompts (that is the operator's responsibility)
- Which LLM provider or model to use (configurable per-deployment)
- Project-specific workflows (those belong in plugins)

Implementations that conform to this spec are interoperable: any AgentOS-compliant agent
runner can be dropped into any AgentOS-provisioned repository.

---

## 2. Terminology

MUST / MUST NOT / SHOULD / SHOULD NOT / MAY follow RFC 2119.

  Agentos repo     The agentOS repository. Contains the agentOS.yaml file,
                bootstrap CLI, templates, and this document.

  Target repo   The GitHub repository being provisioned. The user's project.

  Agent         An automated process that reads GitHub context, performs work, and
                updates GitHub state (labels, PRs, issue comments, board fields).

  Role          A named agent identity with a defined permission scope and a set of
                status labels that trigger it. Core roles: builder, reviewer, watcher, board.

  Runner        The CLI command that executes an agent for a given issue. The spec defines
                the runtime interface (env vars, exit codes). The runner is user-supplied.

  Operator      The person who imports and deploys the spec to a target repo.

  Plugin        An opt-in extension to the core spec. Plugins add labels, board fields,
                workflows, and agent config without modifying the core spec file.

  Wild work     Code pushed to the target repo (or a fork) with no closing-linked
                issue, by an actor that is not an agentOS App. See §12.

  Stub issue    A system-created issue that represents a wild PR inside the label
                state machine. System-owned during intake. See §12.4.

  Reconstruction  The intent document written into the stub issue body: a computed
                facts block plus an LLM-authored interpretation. See §12.6.

  Facts block   The portion of a reconstruction computed by deterministic tooling.
                The archaeologist can reference it, never author it.

  Janitor       A deterministic tool run against a wild branch during status:intake.
                Operates in autofix or report mode. See §12.5.

  Tripwire      A deterministic path- or content-based rule that routes a wild PR
                to status:blocked before any agent runs. See §12.6.2.

  Settle window A quiet period with no author pushes that must elapse before
                autofix janitors and the archaeologist run.

  Finding       A single item produced by a report-mode janitor, carrying a
                stable fingerprint.

---

## 3. Label Model

### 3.1 Axes

Labels are organised into axes. Each axis has a string prefix (e.g. "status:") and a set
of values. The full label name is "{axis_prefix}{value}" (e.g. "status:todo").

The bootstrap provisions all labels defined in agentOS.yaml idempotently (upsert by name).

Core axes:

  status    Lifecycle state of an issue. The PRIMARY ROUTING TRIGGER. Changing a
            status label is the mechanism by which agents and humans hand off work.

  agent     Current ownership. Set by agents to declare they hold an issue.
            Read by humans and dashboards. Does NOT trigger workflows.

  type      Issue classification. Used for board filtering and routing heuristics.

  review    Reviewer verdict sub-flags. Provides fine-grained signal alongside
            status:changes-requested.

  source    Issue origin provenance. Set at creation time. Enables filtering by
            whether an issue was created by a human or an agent.

  follow-on Async handoff signals. Labels in this axis declare that a follow-on
            action is needed. Their routing behaviour is configurable (see 3.4).

### 3.2 Status State Machine

The status axis defines a finite state machine. Transitions are driven by label events
in GitHub Actions. The following diagram shows the core transition graph:

```
                      [human creates issue]
                              |
              .---------------+---------------.
              |                               |
              v                               v
       status:plan                      (no planning)
       (optional; triggers planner)          |
              |                              |
              v                              |
      status:plan-review                     |
      (awaiting /approve-plan)               |
              |                              |
    admin /approve-plan                      |
    (permission verified live)               |
              |                              |
              +------------------------------+
              |
              v
         status:todo            --> builder agent (gated by approval check)
              |
    .---------+---------.
    |                   |
    v                   v
status:in-review   status:blocked    (human resolves)
    |
.---------+---------.
|                   |
v                   v
status:approved  status:changes-requested --> builder agent (retry)
    |
    v
status:done              (set automatically on PR close / issue close)
```

Conforming implementations MUST support at minimum: todo, in-review, changes-requested,
approved, blocked, done. The planning states `status:plan` and `status:plan-review` are
required when the planner is enabled (default). `governance.planning: off` is the only
mode in which these states are inactive.

### 3.3 Routing Table

This table defines which agent role is triggered by each status label:

  status:plan              -> planner (dispatched on label event; concurrency-guarded)
  status:plan-review       -> no agent; awaiting /approve-plan from authorised approver
  status:todo              -> builder (only after approval gate passes; see §3.6)
  status:in-review         -> reviewer
  status:changes-requested -> builder
  status:approved          -> docs (if enabled; else no-op)
  status:blocked           -> no agent; human intervention required
  status:done              -> no agent; terminal state
  status:in-progress       -> no agent; informational only

### 3.4 Follow-on Label Routing

Labels in the follow-on axis MAY be configured to trigger a specific agent role. The
routing behaviour is defined in agentOS.yaml under each follow-on label's `routes_to`
field. If `routes_to` is null the label is informational only.

Example: `follow-on:docs-needed` with `routes_to: docs` causes the orchestrator to
dispatch the docs agent when this label is applied, regardless of the current status.

### 3.5 Label Idempotency

The bootstrap MUST:
- Create a label if it does not exist (POST /repos/{owner}/{repo}/labels)
- Update a label's colour if it exists but the colour differs (PATCH)
- Skip a label if it exists and the colour matches (no-op)
- NEVER delete labels not in the spec (labels may be user-created)

### 3.6 Planning Stage and Dispatch-time Approval

#### 3.6.1 The two planning states

`status:plan` and `status:plan-review` are first-class, visible status states.

  status:plan
    Entry point for the planning stage. Applying this label dispatches the planner.
    The planner fires ONLY on this label — it does not fire on `status:todo`.

  status:plan-review
    Set by the planner when it has written a plan into the issue body. No agent is
    dispatched. The issue waits for a human approval command.

Conforming implementations MUST support both states when the planner is enabled.

#### 3.6.2 The marker contract

The plan lives in the issue body between two HTML comment markers:

    <!-- AGENTOS:PLAN:BEGIN -->
    (plan content, CE-style template)
    <!-- AGENTOS:PLAN:END -->

Rules:
  - The planner MUST replace the content between the markers on each run (never append).
  - Content above the BEGIN marker is the human's original intent and MUST be preserved.
  - The builder reads the plan block as its authoritative implementation contract.
  - When no markers are present and planning is `optional` or `off`, the builder uses
    the full issue body.

#### 3.6.3 Dispatch-time approval semantics

Approval is NOT a label. It is a live permission check performed by the orchestrator
at the moment of builder dispatch.

A build MUST NOT be dispatched unless ALL of the following conditions hold:

  1. The issue body contains a plan block between the markers.
     (Requirement waived when `governance.planning` is `optional` or `off`.)

  2. An `/approve-plan` comment exists from a user whose GitHub collaborator
     permission level (checked live via the GitHub API at dispatch time) is in
     `governance.approvers`. The check MUST be performed at dispatch, not cached.

  3. That approval comment's `created_at` MUST be later than the timestamp of the
     most recent plan receipt comment on the issue. A stale approval that predates
     the current plan revision DOES NOT authorise a build.

Applying `status:todo` without a valid approval comment MUST result in no build
being dispatched. The orchestrator silently skips the builder dispatch.

#### 3.6.4 Slash commands

The orchestrator listens for `issue_comment` events with these commands:

  /approve-plan
    Triggers the approval check. If the commenter has an approver-level permission
    and all build conditions hold, the orchestrator transitions the issue to
    `status:todo` and dispatches the builder. If the commenter lacks permission,
    the orchestrator posts a polite refusal and takes no further action.

  /request-changes <notes>
    Sent by an approver to request plan revisions. The orchestrator transitions
    the issue back to `status:plan`, which re-triggers the planner. The planner
    incorporates the notes into the revised plan (they appear as comments on the
    issue). The orchestrator MUST verify the commenter has approver-level permission
    before honouring this command.

#### 3.6.5 governance config block

The `governance:` block in agentOS.yaml configures the planning and approval gate:

```yaml
governance:
  planning: required          # required | optional | off
  approvers: [admin]          # GitHub collaborator permission levels
  approve_command: "/approve-plan"
  changes_command: "/request-changes"
  plan_begin_marker: "<!-- AGENTOS:PLAN:BEGIN -->"
  plan_end_marker:   "<!-- AGENTOS:PLAN:END -->"
```

  planning: required (default)
    The planner MUST run and an admin MUST approve before the builder runs.

  planning: optional
    Issues MAY go straight to `status:todo`. A plan block is not required.
    An admin approval is still required.

  planning: off
    Legacy mode. The builder fires on `status:todo` unconditionally. No approval
    check is performed. `status:plan` and `status:plan-review` have no effect.

#### 3.6.6 Idempotency and concurrency

The orchestrator MUST use a `concurrency:` group keyed on the issue number for the
planner job. This prevents two planner runs from racing on the same issue. A second
`status:plan` event cancels the in-flight planner run. This replaces the need for a
"planner is busy" state label.

---

## 4. Agent Role Model

### 4.1 Core Roles

Four roles are defined in core. All four MUST be provisioned for a conforming deployment.

  builder
    Implements features and fixes. Opens pull requests. The only role with write access
    to repository contents. Also handles retry after changes-requested.
    Permissions: contents:write, issues:write, pull_requests:write, metadata:read,
                 workflows:write

  reviewer
    Reviews pull requests. Approves or requests changes. MUST NOT have write access to
    repository contents (this is an intentional security constraint — a reviewer that can
    push code can circumvent its own review).
    Permissions: issues:write, pull_requests:write, metadata:read, checks:write

  watcher
    Minimal-footprint role. Used for: settlement (updating board after PR close), issue
    creation (creating follow-on issues), and scheduled monitoring tasks. Plugins that add
    scheduled behaviours (e.g. a daily question generator) run as watcher.
    Permissions: issues:write, metadata:read

  board
    Projects v2 mutations only. Used exclusively to update board fields. No repository
    write access. MUST use organization_projects:write (not repository_projects).
    Permissions: organization_projects:write, metadata:read

### 4.2 Optional Roles

Two additional roles are defined in core but disabled by default:

  docs
    Updates documentation and changelog after approved PRs. Reuses the builder GitHub App
    (same credentials, same permissions). Enabled by uncommenting in agentOS.yaml.
    Triggers on: status:approved, follow-on:docs-needed (if configured)

  planner
    Turns a thin human-written issue into a concrete, file-level plan written into the
    issue body between AGENTOS:PLAN:BEGIN/END markers. Core role, enabled by default.
    Triggers on: status:plan
    Permissions needed: issues:write only (currently reuses builder App as a pragmatic
    shortcut; splitting to a dedicated App with issues:write only is a recommended TODO).
    See §3.6 for the full planning stage and approval gate specification.

### 4.2.1 Intake Roles (v1.2)

Two additional roles ship with the intake pipeline (§12), enabled when
`intake.enabled: true` (the default):

  janitor
    Deterministic cleanup tooling run against wild branches during status:intake.
    Autofix janitors push tool-generated, semantics-preserving commits under the
    janitor App identity; report janitors emit check runs and findings. Dedicated
    GitHub App with a deliberately narrow permission set.
    Permissions: contents:write, checks:write, pull_requests:write (comments).
    NO issues:write, NO workflows:write, no review or approval capability.
    See §12.5 and §12.9.2.

  archaeologist
    Reconstructs intent from unplanned work. A pure function: diff and context
    in, one schema-validated JSON payload out. Holds NO GitHub token and has NO
    App identity — the orchestrator performs every side effect on its behalf.
    The runner MUST NOT grant it shell execution, network access, or any tool
    with side effects.
    Triggers on: status:intake (dispatched by the orchestrator after janitors
    settle and tripwires pass).
    See §12.6.

### 4.3 GitHub App Identity

Each role that has `create_app: true` in agentOS.yaml gets its own GitHub App. Each
App is installed on the target repo and mints short-lived installation tokens at runtime.

Roles with `reuse_app: {other_role}` share the GitHub App of the named role. This is
acceptable for roles with identical permission requirements (docs and planner both need
the same capabilities as builder).

Token minting: the bootstrap provides github_token.py (scripts/github_token.py in the
target repo after `agentOS apply`). At runtime: JWT signed with the app's private key ->
POST /app/installations/{id}/access_tokens -> short-lived token (1 hour TTL).

Credential storage:
  Local development:  .env file (GITHUB_APP_ID_{ROLE}, GITHUB_APP_PRIVATE_KEY_{ROLE})
  GitHub Actions:     repository secrets (same names, set by `agentOS setup`)

### 4.4 Branch Naming Convention

Agent branches MUST follow the pattern:

  agent/{role}/{issue_number}-{slug}

Examples:
  agent/builder/42-add-user-auth
  agent/reviewer/42-add-user-auth

The run-receipt workflow parses the issue number from this pattern. Branches that do not
match this pattern will not receive run receipts.

---

## 5. Runtime Interface

The spec is runtime-agnostic. Any CLI tool that satisfies this interface can be used as
the agent runner.

### 5.1 Environment Variables

The orchestrator workflow injects these env vars before invoking the runner command:

  REQUIRED:
    AGENT_ROLE           The role being executed (builder / reviewer / watcher / etc.)
    ISSUE_NUMBER         The GitHub issue number (integer as string)
    GITHUB_TOKEN         Short-lived GitHub App installation token for this role
    GITHUB_REPOSITORY    owner/repo (standard GHA variable)
    GITHUB_RUN_ID        The Actions run ID (standard GHA variable)

  OPTIONAL (set if configured):
    LLM_PROVIDER         Provider identifier (e.g. "anthropic", "openai", "cloudflare")
    LLM_MODEL            Model identifier (e.g. "claude-sonnet-4-6")
    LLM_API_KEY          API key for the provider
    AGENT_MAX_TURNS      Maximum turns budget for this run (integer)
    OPS_REPO             owner/repo of the ops/metrics repository (if separate)
    OPS_REPO_TOKEN       PAT with read/write access to the ops repo

### 5.2 Exit Codes

  0    Clean exit. Agent completed its task successfully.
  1    Crashed / error. The orchestrator will post a failure comment to the issue.
  2    Max turns reached. Treated as a soft failure; agent should set status:blocked
       before exiting if it cannot make progress.

### 5.3 Runner Command Configuration

The runner command is specified in config.yaml:

```yaml
agent:
  runner: "hermes run"       # or: claude, codex, my-custom-runner
```

The orchestrator workflow calls: `{runner} $AGENT_ROLE` or equivalent, after injecting
all env vars into the shell environment.

Implementations MAY use a more complex invocation (e.g. passing flags). The runner command
is a shell string passed to bash -c.

### 5.4 Agent Scaffold

After `agentOS apply`, the target repo contains:

  AGENTS.md                     Operating manual for all agents. Defines roles,
                                 execution protocol, state machine, guardrail rules.
  agents/
    _shared/
      context-management.md     How to manage context window across long runs
      escalation.md             When and how to escalate to human
      loop.md                   Loop guardrail rules (no infinite loops)
      telemetry.md              How to emit run records
    {role}/
      AGENT.md                  Role-specific instructions (fill in by operator)

AGENT.md files MUST contain at minimum:
  # Role: {role name}
  ## Purpose
  ## Constraints
  ## Output Format
  ## Handoff Protocol

---

## 6. Workflow Contract

### 6.1 agent-orchestrator.yml

  Triggers:
    issues:   [opened, labeled, closed]
    pull_request: [labeled]

  On issue labeled with a status:* label:
    1. Read the label name
    2. Look up the routing table (label -> role)
    3. If a role is found and is enabled: dispatch the runner
    4. Set agent:{role} label on the issue
    5. Call run-receipt.yml as a reusable workflow when complete

  On issue closed (without status:done):
    1. Set status:done label

  On type:feature issue opened:
    1. Add issue to the Projects v2 board (if board is enabled)

  On follow-on:* label applied:
    1. Look up configured routes_to for that follow-on label
    2. If routes_to is set: dispatch the named role

### 6.2 agent-settlement.yml

  Triggers:
    pull_request: [closed]

  On PR closed (merged or unmerged):
    1. Determine linked issue number (from PR body or branch name)
    2. Mint a board token (watcher role)
    3. Run projector.py: update the Outcome field on the board item
    4. Set status:done on the linked issue (if merged)

### 6.3 detect-run-failure.yml

  Triggers:
    workflow_run: workflows: ["Agent Orchestrator"], types: [completed]
    condition: conclusion == 'failure'

  On failure:
    1. Fetch failed job details via GitHub API
    2. Parse issue number from failed job name or branch name
    3. Post a structured escalation comment to the issue
    4. If issue cannot be determined: fall back to issue #1

### 6.4 run-receipt.yml (reusable)

  Triggers:
    workflow_call: inputs: job_status (string, required)

  Condition: branch name matches agent/{role}/{number}-{slug}

  Actions:
    1. Parse issue number and role from branch name
    2. Compute duration from workflow start time
    3. Post a machine-parseable receipt comment to the issue

  Receipt comment format:
  ```
  <!-- agentOS:run-receipt -->
  **Run Receipt** | Role: {role} | Status: {status} | Duration: {duration}s
  Run ID: {run_id} | Branch: {branch}
  <!-- /agentOS:run-receipt -->
  ```

---

## 7. Projects v2 Board Contract

### 7.1 Field Definitions

The board has 10 fields divided into three flow categories:

  METADATA (set by operator or orchestrator before/during run):
    Role          single_select   Maps agent:* ownership label to a board value
    Status        single_select   Mirrors status:* label (denormalised for board UI)
    Max turns     number          Per-issue turn budget. Default: 40.

  PARAMETER (configures agent behaviour for this issue):
    Model         single_select   Which LLM to use. Default options (see 7.2).

  TELEMETRY (written by runner after each run, read by dashboards):
    Outcome       single_select   reduce: latest_settlement
    Clean exit    single_select   reduce: last_run
    Cost to date  number          reduce: sum_runs
    Turns         number          reduce: sum_runs
    Attempts      number          reduce: count_runs

### 7.2 Default Model Options

The Model field ships with these five options (values are display names; operators map
them to actual model IDs in config.yaml):

  claude-haiku          Anthropic Claude Haiku — fast, cheap, best for routine tasks
  claude-sonnet         Anthropic Claude Sonnet — balanced capability and cost
  gpt-4o-mini           OpenAI GPT-4o Mini — fast general-purpose option
  gemini-flash          Google Gemini Flash — fast multimodal option
  kimi-k2               Moonshot Kimi K2 — strong coding performance

Operators extend or replace this list in their agentOS.yaml or via a plugin.

### 7.3 Reduce Semantics

  latest_settlement     The value from the most recent settlement event wins.
                        Used for Outcome: a reverted PR should update the Outcome,
                        not keep the original "Merged" value.

  last_run              The value from the most recent run event wins.
                        Used for Clean exit: reflects the last run's exit status.

  sum_runs              Values from all run events for this issue are summed.
                        Used for Cost to date, Turns, Attempts.

  count_runs            Count of run events. Used for Attempts.

### 7.4 Schema Fingerprinting

The bootstrap computes a SHA-256 fingerprint of the field definition block in agentOS.yaml
and stores it in field-bindings.json alongside the live GraphQL node IDs:

```json
{
  "schema_fingerprint": "sha256:...",
  "board_id": "PVT_...",
  "fields": { ... }
}
```

On re-run, if the fingerprint matches, the board provisioning step is skipped. If the
fingerprint differs, the fields are re-synced. New options are added; existing options
are never deleted (GitHub Projects v2 does not support option deletion via API).

---

## 8. Metrics Schema

### 8.1 Run Record (v6)

Every agent invocation MUST produce a run record appended to the JSONL corpus
(ops-metrics/{YYYY-MM}.jsonl). Fields:

  TOP LEVEL:
    schema_version    int      Always 6
    event             str      "run" | "settlement"
    run_key           str      "{repo}|{role}|{kind}|{issue}|{run_id}|{attempt}"

  IDENTITY block:
    repo              str      "owner/repo"
    role              str      Agent role name
    kind              str      "issue" | "pr"
    number            int      Issue or PR number
    agent_identity    str      GitHub App slug
    run_id            str      GitHub Actions run ID
    attempt           int      1-indexed attempt count
    github_actions_run_url str
    model_provider    str
    model_name        str

  LIFECYCLE block:
    started_at        str      ISO 8601
    ended_at          str      ISO 8601
    duration_seconds  float

  EXECUTION block:
    turns             int
    tool_calls        int
    max_turns_hit     bool
    compaction        object   Context compaction events (see schema JSON)

  COST block:
    input_tokens      int
    output_tokens     int
    total_tokens      int
    input_cost_usd    float
    output_cost_usd   float
    total_cost_usd    float
    modeled_cost_usd  float    Cost if computed from model_rates.yml
    per_turn          array    [{input: int, output: int}]

  FRICTION block:
    tool_errors       int
    retries           int
    repeats           int
    max_turns_proximity float  Turns / max_turns ratio
    tool_error_breakdown array [{tool: str, count: int}]

  CONTEXT block:
    diff_lines_added  int
    diff_lines_removed int
    files_changed_count int
    issue_labels      array[str]
    model_version     str
    context_inflation_ratio float

  CLEAN_EXIT block:
    status            str      "clean" | "crashed" | "max_turns" | "infra_failure"
    detail            str
    error             object | null   {error_type, tool, code}

  LINKAGE block:
    pr_number         int | null
    issue_number      int
    previous_run_id   str | null

  outcome             str      "provisional" | "merged" | "closed_unmerged" |
                               "ci_failed" | "reverted" | "abandoned"

### 8.2 Settlement Record (v6)

  schema_version    int      6
  event             str      "settlement"
  run_key           str      "settlement|{repo}|{pr_number}"
  settled_at        str      ISO 8601
  outcome           str      See outcome values above
  ci_result         str | null
  reviewer_verdict  str | null
  reverted_at       str | null
  reverted_by       str | null
  pr_number         int

### 8.3 Cost Accounting

Model rates are stored in scripts/model_rates.yml:

```yaml
# NOTICE: Rates are approximate and may be stale. Verify with your provider.
# Last updated: 2026-06-26
anthropic:
  claude-haiku-4-5-20251001:
    input_rate_usd_per_m: 0.80
    output_rate_usd_per_m: 4.00
    context_window: 250000
```

The `modeled_cost_usd` field is computed using this table. If the provider+model is not
in the table, `modeled_cost_usd` is null and `total_cost_usd` relies on API-reported costs.

---

## 9. Plugin Interface

### 9.1 Plugin Manifest

A plugin is a directory containing a plugin.yaml manifest. It may also contain:
  labels.yml       Additional labels to provision
  workflows/       Additional GHA workflow files to copy to .github/workflows/
  agents/          Additional or override AGENT.md content per role
  scripts/         Additional scripts to copy to the target repo's scripts/

plugin.yaml structure:
```yaml
pluginVersion: "1.0"
name: "my-plugin"
description: "What this plugin does"
specVersionRequired: ">=1.0"   # semver range

labels:
  - axis: phase
    values:
      - name: "1"
        color: "c2e0c6"

board_fields:
  - name: Sprint
    type: text
    flow: metadata

follow_on_routes:
  docs-needed:
    routes_to: docs

workflows:
  - source: workflows/my-workflow.yml
    target: .github/workflows/my-workflow.yml
    enabled_by_default: true
```

### 9.2 Plugin Loading

Plugins are listed in agentOS.yaml:
```yaml
plugins:
  - name: three-questions
    source: github:open-agentos/agentos//plugins/three-questions@v1.2.0
```

Sources supported in v1.0:
  github:{owner}/{repo}//{path}@{ref}   Remote GitHub path (downloaded at apply time)
  local:{path}                           Local filesystem path (relative to agentOS.yaml)

The bootstrap applies plugins in order after core provisioning. Plugins MUST NOT modify
core-provisioned resources (they may only add). If a plugin attempts to modify a core
label's colour, the bootstrap MUST warn and skip that change.

### 9.3 Reference Plugin: three-questions

The three-questions plugin ships in this repo at plugins/three-questions/ and demonstrates
the full plugin interface. It adds:
  - phase:* labels (project milestone tracking)
  - follow-on:dreaming-needed label
  - follow-on:docs-needed label (routed to docs agent)
  - Watcher scheduled workflow (daily intelligence brief generation)
  - Watcher AGENT.md template with source configuration

---

## 10. Bootstrap CLI Contract

### 10.1 Commands

  agentOS init [--from {source}]
    Generates agentOS.yaml in the current directory. If --from is provided, downloads
    and uses that spec as the starting point. Otherwise generates a blank spec with
    prompts for operator choices.

  agentOS setup --repo {owner/repo}
    Interactive GitHub App registration wizard. For each role with create_app: true,
    opens the GitHub App manifest flow in a browser, receives the OAuth callback on
    localhost:4000, and writes credentials to .env and to GHA repo secrets.
    Requires: gh CLI authenticated, GITHUB_TOKEN in environment.

  agentOS apply --repo {owner/repo} [--labels-only] [--board-only] [--workflows-only] [--force]
    Provisions the target repo from agentOS.yaml. Runs all steps unless a --only flag
    limits scope. Idempotent. Tracks progress in .agentOS-state.json. Safe to re-run.

  agentOS verify --repo {owner/repo}
    Checks that the target repo matches agentOS.yaml. Reports pass/fail per component.
    Exit 0 if all pass. Exit 1 if any fail.

### 10.2 State File

  .agentOS-state.json tracks bootstrap progress:
  ```json
  {
    "spec_fingerprint": "sha256:...",
    "repo": "owner/repo",
    "steps": {
      "labels": {"status": "complete", "at": "2026-06-26T10:00:00Z"},
      "board": {"status": "complete", "at": "2026-06-26T10:01:00Z"},
      "workflows": {"status": "failed", "at": "2026-06-26T10:02:00Z", "error": "..."},
      "apps": {"status": "pending"}
    }
  }
  ```
  On re-run, steps with status "complete" whose input fingerprint matches are skipped.
  Steps with status "failed" or "pending" are retried.

---

## 11. Conformance

A deployment is AgentOS-conformant if:
  - All required labels (status:*, agent:*, type:*, review:*, source:*) are present
  - The Projects v2 board has all 10 required fields with correct types
  - The four core GitHub Apps are installed with the specified permission scopes
  - The orchestrator workflow fires on issue label events AND issue_comment events
    and routes per the routing table (§3.3)
  - The orchestrator enforces the dispatch-time approval gate (§3.6.3) before
    dispatching the builder on status:todo
  - The settlement workflow fires on PR close and updates the Outcome field
  - Each agent invocation produces a valid v6 run record
  - The planner role emits run records with identity.role = "planner"

Plugins may extend a conformant deployment without breaking conformance.

When intake is enabled (`intake.enabled: true`, the default), a deployment is
intake-conformant if additionally:
  - The intake labels (source:wild, status:intake, status:intake-review, and the
    three intake follow-on labels) are provisioned
  - The classifier applies the syntactic classification rule (§12.2) with no
    heuristic additions
  - The recursion guard (§12.2.4) is implemented and tested: agent-identity
    pushes never reset intake state
  - `pull_request_target` is never combined with a checkout of contributed code
    anywhere in the intake pipeline (§12.2.6)
  - The trusted-author gate (§12.2.7) is evaluated before classification and
    fails closed: untrusted-author PRs cause no stub, no janitors, no
    archaeologist run, and no LLM spend
  - The archaeologist runs tokenless with orchestrator-mediated writes (§12.6)
  - Janitor and archaeologist invocations produce valid run records with
    identity.role = "janitor" / "archaeologist" (§12.10)

---

## 12. Intake — Unplanned ("Wild") Pull Requests

### 12.1 Purpose and non-goals

agentOS v1.2 is intent-first: an issue exists, a plan is approved, an agent
produces code, receipts accumulate. Real development includes a second mode: a
developer (or their local coding agent) produces code first, in their own
environment, with no issue and no plan. Without intake that work is invisible
to agentOS — it bypasses the review loop, leaves no receipts, and erodes the
guarantee that every merged change traces to a documented intent.

**The primary goal of intake is to support developers who push code from their
own coding environment — with no ceremony at the moment of pushing — while
keeping that work compatible with a mixed team of humans and agents operating
the planned lane.** The developer's entire interface is `git push` followed by
opening a pull request. Everything else is the system's job.

Non-goals:

- **Trust parity with the planned lane.** The planned lane's security property
  is intent-before-code. Intake structurally lacks it. Wild work is a visibly
  second-class trust tier by design; `source:wild` never washes off.
- **Trailing mode** (post-hoc intake for direct-to-main pushes in repositories
  without branch protection) is deferred. See §12.12.
- **Stacked-PR decomposition** of oversized wild diffs ("the Splitter") is
  deferred. See §12.12.
- Reforming developer behaviour. Intake metabolises unplanned work; it does
  not discourage it.

### 12.2 Classification

#### 12.2.1 Trigger events

The classifier is a GitHub Actions workflow firing on `pull_request` events:
`opened`, `reopened`, `ready_for_review`, `synchronize`, and `edited` (base
branch retarget). The classifier authenticates as the **watcher App** for all
writes (issue creation, labels, comments). No new App identity is required for
classification.

#### 12.2.2 The classification rule

A PR is classified **wild** if and only if all of the following hold:

1. The PR author is not an agentOS App identity for this repository.
2. The PR author is not listed in `intake.exclude_actors`.
3. The PR has no closing-linked issue (GraphQL `closingIssuesReferences`,
   which covers closing keywords and sidebar-linked issues).

The rule is deliberately syntactic. Implementations MUST NOT add heuristic
classification (commit-message quality, diff shape, or model-based guessing).
Predictability is a feature: a developer can always know, before pushing, how
the system will treat their branch.

Edge cases (normative resolutions):

- **Bot flood.** Dependabot, Renovate, and similar bots author PRs with no
  linked issues and non-agent actors. `intake.exclude_actors` MUST default to
  a maintained list including `dependabot[bot]` and `renovate[bot]`. Excluded
  actors' PRs are ignored by intake entirely.
- **Human PR with a linked issue.** This is planned work done manually;
  archaeology would be redundant and janitor pushes to a deliberately-prepared
  branch may be unwelcome. Linked PRs are not wild. Default behaviour is
  `intake.linked_prs: ignore`. Operators MAY set
  `intake.linked_prs: janitors-report-only` to run the report tier (no pushes,
  no archaeology) on all human PRs. Autofix on linked PRs is intentionally not
  offered in v1.2.
- **PR retargeted to a different base branch.** Classification and all check
  results are base-relative. On `edited` events that change the base ref, the
  classifier MUST re-run classification and, if the PR remains wild, reset the
  stub issue to `status:intake`.

#### 12.2.3 Draft PRs

On a wild **draft** PR, only report-mode janitors run (as check runs). Autofix
janitors, the archaeologist, and the stub issue MUST all wait for
`ready_for_review`. Early findings while the developer is still flailing are a
gift; pushed commits and intent guesses against a moving target are noise and
cost.

#### 12.2.4 The recursion guard

A janitor push emits a `synchronize` event, which re-fires the classifier,
which resets intake state, which re-runs janitors, which push again. GitHub's
built-in Actions loop protection applies only to `GITHUB_TOKEN`, **not** to
App-token pushes, so this loop is live by default.

The classifier MUST resolve the pushing actor on every `synchronize` event and
MUST ignore pushes whose actor is any agentOS App identity for the repository.
Only author (human) pushes reset intake state. This check is normative and
load-bearing; implementations MUST test it.

#### 12.2.5 Forks

Fork PRs that satisfy §12.2.2 enter the intake lane in **hardened mode**:

- Autofix janitors are disabled regardless of configuration (App tokens cannot
  push to fork branches; the spec states the policy rather than relying on the
  platform accident).
- Report janitors run under GitHub's default fork protections: `pull_request`
  event, read-only token, no secrets, first-contribution runs require
  maintainer approval.
- Author comments do not trigger archaeologist re-runs (§12.6.6).

Intake thereby doubles as a first-time-contributor pipeline: a drive-by OSS
contribution receives the same facts block, reconstruction, and review path as
an internal wild PR, at a stricter trust setting.

#### 12.2.6 pull_request_target prohibition

Implementations MUST NOT use `pull_request_target` with a checkout of the
contributed code anywhere in the intake pipeline.

#### 12.2.7 Trusted-author gate (operator cost policy)

The archaeologist (§12.6) is LLM-backed and runs on every wild PR. On a public
repository this means an unbounded population of contributors can each cause
model spend simply by opening a PR. Implementations MUST provide a trusted-author
gate that bounds this exposure.

The gate is evaluated in the classifier, **before** the §12.2.2 syntactic rule,
and is distinct from it. It is not a classification heuristic and MUST NOT change
what "wild" means (§12.2.2's predictability guarantee is preserved): it decides
only whether the intake pipeline runs at all for a given author.

An author is **trusted** if either:

1. Their login appears in `intake.allow_actors`, or
2. Their repository collaborator permission is at or above
   `intake.min_permission`, using the total order
   `none < read < triage < write < maintain < admin`.

When the author is **not** trusted, the classifier MUST leave the PR entirely
untouched: no stub issue, no janitors, no archaeologist invocation, and
therefore no LLM spend. Such a PR remains an ordinary GitHub pull request that a
maintainer triages by hand; a maintainer admits it to the agent lane explicitly
(for example by applying `status:todo`, or by adding the author to
`intake.allow_actors`).

Defaults and failure behaviour (normative):

- `intake.min_permission` defaults to `write`. `intake.allow_actors` defaults to
  empty.
- The gate MUST **fail closed**. If `min_permission` is unset or not a member of
  the permission order, or if the author's permission cannot be resolved (for
  example a non-collaborator returns 404, or the permission API errors), the
  author MUST be treated as untrusted. A misconfiguration can therefore only
  suppress spend, never enable it.
- Operators MAY relax the gate toward the pre-1.2.4 "trust everyone" behaviour by
  setting `min_permission: read` (or lower), accepting the corresponding spend
  exposure.

Interaction with forks (§12.2.5): the gate is orthogonal to hardened mode. A fork
PR from a trusted author still enters intake in hardened mode; a fork PR from an
untrusted author is left untouched by the gate before hardened mode is ever
considered.

### 12.3 Label model additions

#### 12.3.1 Source axis

One new value on the existing source axis. Set once at stub-issue creation.
Never changed, never removed — including after merge.

    Label          Hex Colour   Description
    -------------  -----------  ------------------------------------------------
    source:wild    d4c5f9       Issue was created by the intake classifier from
                                an unplanned pull request.

#### 12.3.2 Status axis

Two new values. Single-select semantics apply as for all status labels: the
orchestrator removes any existing `status:*` label before applying a new one.

    Label                  Hex Colour   Description
    ---------------------  -----------  ---------------------------------------
    status:intake          c5b8f0       Wild PR classified. Janitors running or
                                        settling; archaeologist queued.
    status:intake-review   e4d069       Reconstruction written; awaiting
                                        /approve-intent.

`routes_to` additions:

    labels:
      status:
        intake:
          routes_to: archaeologist   # dispatched by orchestrator after janitors
                                     # settle and tripwires pass
        intake-review:
          routes_to: null            # awaiting human /approve-intent

Wild work joins the existing machine at `status:in-review`. The transitions
`in-review → approved | changes-requested → merged | closed` are unchanged.
Hard failures anywhere in intake reuse `status:blocked` with its existing
semantics. The `plan / plan-review` naming precedent is intentionally
mirrored: `intake / intake-review` is the same two-stage shape run in the
opposite temporal direction.

#### 12.3.3 State machine graft

    unplanned PR (ready for review)
            |
            |  classifier: create stub issue, closing-link it,
            |  apply source:wild + status:intake
            v
      status:intake ──────────── tripwire or hard finding ────► status:blocked
            |                                                      (human)
            |  janitors settle; archaeologist runs;
            |  reconstruction written to stub body
            v
      status:intake-review
            |
            |  /approve-intent (must postdate latest push, §12.7.4)
            v
      status:in-review ──► existing loop, unchanged
            |
            v
      status:merged / status:closed ──► watcher settlement (§12.8, §12.10)

Any author push while in `status:intake-review` or later (pre-merge) returns
the stub to `status:intake`. Agent-identity pushes do not (§12.2.4).

A force-push during intake-review is handled identically to any author push —
reset to `status:intake`, re-run the pipeline. Janitors are idempotent and
convergent (§12.5.5), so re-running is safe. The `/approve-intent` staleness
rule (§12.7.4) independently invalidates any approval that predates the push.

#### 12.3.4 Follow-on axis

Three new values, consumed by the watcher at settlement exactly as existing
follow-on labels are (§12.8):

    follow-on:needs-cleanup           Report-tier lint/format/dead-code findings.
    follow-on:needs-tests             Characterisation-test work for touched code.
    follow-on:needs-security-review   Semgrep / dependency-audit findings.

#### 12.3.5 Placement of state

All intake workflow state lives on the **stub issue**, never on the PR. The PR
carries exactly one piece of intake data: the closing-keyword link in its
body. This preserves the invariant that the orchestrator mutates labels only
on issues, and it means watcher settlement works unmodified (merge closes the
stub via the closing link; the `pull_request.closed` event fires as today).

### 12.4 The stub issue

#### 12.4.1 Creation

On classifying a ready-for-review wild PR, the classifier MUST:

1. Create an issue titled from the PR title (fallback: branch name), body
   containing the intent markers (empty) and a system notice that the issue is
   intake-managed.
2. Apply `source:wild` and `status:intake`. No `type:*` label is applied at
   creation; the archaeologist proposes one (§12.6.4).
3. Edit the PR body to prepend `Closes #<stub>` (preserving existing content).
4. Post a comment on the PR linking the stub and describing what happens next
   (including the `git pull --rebase` guidance of §12.5.3).

Stub creation MUST be idempotent: if a closing-linked stub already exists
(reopened PR, classifier re-run), the classifier reuses it.

#### 12.4.2 Ownership and lifecycle

The stub issue is **system-owned from creation until settlement**. Humans
interact with it through commands (`/approve-intent`, `/request-changes`,
`/dismiss-findings`) and ordinary comments, not by editing its labels or body.

- **Stub closed manually mid-intake:** while the linked PR is open, the
  classifier MUST reopen a manually-closed stub and post a comment explaining
  system ownership and pointing at the PR-close path for cancelling the work.
  Closing the **PR** is the supported cancellation: the watcher settles the
  stub with `outcome=cancelled`.
- **PR reopened after cancellation:** the classifier finds the existing
  closing-linked stub, reopens it, and resets it to `status:intake`. A fresh
  settlement record is written at the eventual terminal state. Metrics
  consumers MUST tolerate multiple settlement records per issue with the
  latest being authoritative.

#### 12.4.3 Body structure

The stub body after reconstruction contains, in order: the system notice, the
facts block, the interpretation between the existing
`<!-- AGENTOS:PLAN:BEGIN -->` / `<!-- AGENTOS:PLAN:END -->` markers, and the
janitor summary. Reusing the PLAN markers is deliberate: every issue body has
one canonical location for its intent document, and `source:*` disambiguates
whether intent preceded code (planned) or was inferred from it (wild).

### 12.5 The janitor layer

#### 12.5.1 The tier criterion

At intake time there is generally no behavioural oracle: wild branches
typically carry no tests, no reviewed intent, no baseline. Therefore the
boundary between what a janitor may fix and what it may only report cannot be
confidence-based. It is capability-based.

A janitor MAY run in **autofix** mode only if its transformation is:

1. **Deterministic** — tool-generated; same input produces same output; no
   LLM anywhere in the path.
2. **Semantics-preserving by construction** — AST-equivalent or purely
   lexical transforms. "Probably fine" transforms do not qualify; where a
   tool classifies its own fixes (ruff, ESLint), only the safe class is
   permitted.
3. **Idempotent** — a second run over its own output is a no-op.

Everything else is **report** mode. The normative one-line trust story:
*if an agent pushed it, its harmlessness was provable from syntax; if judgment
was required, it went through review.*

#### 12.5.2 The three tiers

**Autofix (pushes commits):** code formatting, import sorting,
whitespace/EOL/EditorConfig conformance, license headers, codegen
regeneration, lockfile-to-manifest synchronisation.

**Report (check runs + facts block):** static-analysis findings (semgrep),
secret detection, dependency CVE and license audit, type errors, lint rules
without safe fixes, dead-code detection, missing-test detection, TODO/FIXME
inventory, oversized-binary detection.

**Not janitors (excluded from intake):** refactoring, docstring or
documentation generation, test writing, error-handling improvements, debug
statement removal — any transformation requiring an LLM or judgment. These
are builder work after intent approval, or follow-on issues at settlement
(§12.8). Running agentic "cleanup" during intake would burn tokens on
possibly-rejected intent and contaminate the diff the human is about to
approve.

Security findings are report-tier **categorically**: a janitor MUST NOT apply
autofixes offered by a security tool. A patched-over vulnerability the human
never saw is worse than a loud finding.

#### 12.5.3 Execution model

Janitors run serially, in the order declared in configuration, inside a
GitHub Actions job triggered from `status:intake`, after tripwires (§12.6.2)
and after the settle window elapses:

- `intake.settle_seconds` (default 300): autofix janitors and the
  archaeologist MUST NOT start until no author push has occurred for this
  window. Report-mode checks MAY run immediately on every push.

A janitor MAY declare `block_on: critical` or `block_on: any`: if findings
at or above that severity exist after the run, the stub is routed to
`status:blocked` before the archaeologist dispatches. The `secrets` janitor
(§12.5.7) always blocks regardless of this field.

A `paths:` ratchet overrides the global janitor mode for files matching a
glob path. Example: `"legacy/**": {janitors_mode: report}` keeps legacy
code report-only even when the global mode is `autofix`.

The race between janitor pushes and the developer's continued local work is
the single most likely cause of a developer disabling the feature. Three
layers address it: (a) the settle window keeps janitors off actively-moving
branches; (b) any author push during or after janitor runs resets to
`status:intake`; janitors re-run and re-converge — idempotency makes this
safe; (c) documentation MUST prominently instruct `git pull --rebase` as the
wild-lane norm, and the classifier's first PR comment (§12.4.1) includes this
line. Operators who still find pushes too intrusive can set every janitor to
`mode: report`.

#### 12.5.4 Commit discipline

Each autofix janitor that changes anything pushes **its own commit** under the
**janitor App identity** with a trailer:

    AgentOS-Janitor: <name>
    AgentOS-Run-Id: <run id>

Janitors MUST NOT amend, squash, or force-push, and MUST NOT modify commits
authored by anyone else. Attribution is structural: every janitor-written line
is signed by an App that, by permission table (§12.9.2), cannot review,
approve, or push to protected branches.

#### 12.5.5 Convergence

After the full janitor sequence completes, the sequence MUST run a second
time and produce an empty diff. If it does not converge by
`intake.convergence_passes` (default 2):

1. All janitor commits from this intake cycle are reverted (one revert
   commit, janitor-attributed).
2. Every autofix janitor is demoted to report mode for this PR.
3. An issue is filed against the operator's janitor configuration
   (`type:bug`, `source:agent-created`) — dueling formatters are a config
   defect, not a per-PR event.

#### 12.5.6 The test oracle

If the repository has a test suite (`intake.test_oracle: auto`):

- Run it against the author's head commit before any autofix.
- Run it again after janitors converge.
- **pass → pass:** proceed.
- **pass → fail:** revert all janitor commits, demote the janitor set to
  report mode for this PR, and file a config issue — by the tier criterion,
  any autofix that changes test results was mis-tiered. Self-demotion turns
  tier mistakes into filed bugs instead of shipped breakage.
- **fail baseline:** there is no oracle; proceed on the syntactic-safety
  criterion alone and record `oracle: none` in the facts block.

Empty or merge-commit-only diffs: skip janitors, write a facts-only
reconstruction with `confidence: low`, proceed to `status:intake-review`.
The human gate still applies.

#### 12.5.7 Secrets

Secret detection findings receive the strictest report handling: the PR is
routed to `status:blocked`, and the report MUST demand **rotation**, not
removal — a secret pushed to any remote is already burned; deleting it from
the branch tip does nothing about history. The janitor MUST NOT attempt
history rewriting.

### 12.6 The archaeologist

#### 12.6.1 Role definition

The archaeologist reconstructs intent from unplanned work. It is the first
agentOS role whose entire input is untrusted by design, and its contract is
correspondingly stricter than any existing role: it is a **pure function** —
diff and context in, one JSON payload out. It holds no GitHub token. The
orchestrator performs every side effect on its behalf.

The rationale is a gap App scoping cannot close: GitHub permissions are not
row-level. An `issues:write` token can edit any issue in the repository. A
hijacked archaeologist holding such a token could rewrite approved plans on
unrelated issues. Mediated writes reduce its fully-hijacked blast radius to a
misleading string rendered inside a template, adjacent to deterministic
facts, read by a human.

#### 12.6.2 Tripwires precede everything

Before janitors or archaeologist run, the orchestrator evaluates
`intake.tripwire_paths` against the diff's file list (deterministically —
no model involved). Defaults:

    .github/workflows/**
    .github/actions/**
    agentOS.yaml
    .agentOS/**

Any match routes the stub to `status:blocked` immediately: no reconstruction,
no autofix, human eyes mandatory. This rule also severs a downstream hazard:
the builder holds `workflows:write`, so a wild `changes-requested` cycle
(§12.9) must never begin on a diff that touches workflow definitions.

#### 12.6.3 Inputs

- The diff, PR title, branch name, and commit messages — delimited and
  declared untrusted data in the runner prompt contract.
- Read-only repository context (surrounding code).
- The orchestrator-computed facts block and janitor results, provided as
  ground truth the archaeologist MAY reference and MUST NOT restate with
  alterations.

The archaeologist runner MUST NOT be granted shell execution, network access,
or any tool with side effects. URLs appearing in the diff are data, never
fetch targets.

#### 12.6.4 Output contract

One JSON payload, schema-validated by the orchestrator before any GitHub
write occurs:

    {
      "interpretation": "string — what this change appears to be trying to do",
      "confidence": "high | medium | low",
      "proposed_type": "feature | bug | chore | docs",
      "scope": ["string — files/behaviours this change legitimately covers"],
      "proposed_title": "string — replaces branch-name garbage on the stub",
      "questions": ["string — asked of the author when confidence < high"]
    }

Schema violations are treated as a failed run (existing runner exit-code
semantics); the orchestrator retries once, then routes to `status:blocked`.

The `scope` array becomes the reviewer's reference for
`review:scope-violation` — the first time that flag has defined meaning for
unplanned work. Note the defence-in-depth assumption: a deceived
archaeologist could write a generous scope, which is why the reviewer's
`review:security-concern` check is scope-independent and why the facts block
lists touched surfaces regardless of anything the model says.

#### 12.6.5 What the orchestrator does with the payload

Assembles facts + interpretation into the stub body between the intent
markers (interpretation explicitly captioned as machine-inferred), applies
`type:<proposed_type>`, sets the stub title, transitions to
`status:intake-review`, posts the intake-review comment (§12.7), and writes
the standard JSONL run record with `role: "archaeologist"`.

#### 12.6.6 Re-runs, corrections, and cost caps

- Any author push (post-settle-window) re-dispatches the archaeologist with
  the updated diff. Concurrency group keyed on the stub issue number, as for
  the planner.
- For same-repo authors, a reply to the archaeologist's questions
  re-dispatches with the reply included as high-weight context — a one-line
  answer from the person who wrote the code beats any amount of diff
  archaeology.
- For fork authors, replies MUST NOT trigger re-runs (spam and injection
  vector); a maintainer comment MAY.

Cost caps, all configurable:

    intake.max_diff_lines   (default 6000)  Above this, skip interpretation
                                            entirely: facts-only reconstruction,
                                            confidence pinned to low.
    intake.max_recon_runs   (default 5)     Per stub. Beyond it, facts-only
                                            mode; a human reads the diff.

Binary-only or generated-only diffs: facts-only reconstruction,
`confidence: low`, questions to the author auto-populated ("describe what
this changes").

### 12.7 The intake-review comment and /approve-intent

#### 12.7.1 The comment is the security boundary

The intake-review comment is what a human reads before approving. Its
information ordering is normative, not stylistic:

1. **Facts** (computed): files and surfaces touched, LOC delta, dependency
   delta, oracle status, janitor findings summary with check-run links.
2. **Commit provenance:** author commits and janitor commits, listed
   separately.
3. **Direct link to the PR's Files changed view.**
4. **Interpretation**, visibly captioned: *"Inferred by the archaeologist —
   verify against the diff."* Confidence shown. Questions to the author, if
   any.
5. **Actions:** `/approve-intent`, `/request-changes <notes>`,
   `/dismiss-findings <category> --reason <text>`.

Facts before interpretation, always. The design intent is that a
rubber-stamping human is rubber-stamping computed facts, not model prose.

#### 12.7.2 What /approve-intent asserts

The approver asserts that (a) the interpretation matches the actual intent of
the work, and (b) they have seen the facts block. It does **not** assert code
correctness — that remains the review stage's job. The command is available
to the same authoriser set as `/approve-plan` (permission verified live at
dispatch, per the existing pattern) and SHOULD be implemented as an alias
into the same handler.

#### 12.7.3 Author-approver identity

The PR author MAY be the approver only if they hold the authoriser role;
operators on mixed teams SHOULD require a second person for wild work via
`intake.approve_intent_self: false` (default `true` for solo operators, set
explicitly by `agentOS init` according to repo collaborator count).

#### 12.7.4 Staleness

An `/approve-intent` is valid only if its timestamp postdates the latest push
to the PR branch — author or janitor. This reuses the existing
approval-postdates-plan ordering check verbatim. Combined with §12.3.3's
reset-on-author-push rule, a developer who keeps YOLOing after approval
automatically invalidates that approval.

If `/approve-intent` and a `git push` land within seconds of each other, the
ordering check is evaluated by the orchestrator at dispatch time against
GitHub's recorded timestamps, exactly as `/approve-plan` is today. Ties or
out-of-order delivery resolve to "approval invalid; re-approve after the
branch settles," and the orchestrator comments to say so.

### 12.8 Report-to-work

#### 12.8.1 Filing is the default; nothing decays silently

Report findings are filed as work automatically; the alternative to fixing is
**dismissing**, and dismissal is a first-class recorded act.

During intake, the orchestrator maps report categories onto the follow-on
axis on the stub (§12.3.4). At settlement — merge, not approval — the watcher
does exactly what it does today: one fresh issue per follow-on label, linking
back to the stub, carrying the full findings report, entering the **planned
lane** (`source:agent-created`, plan gate per governance, builder, reviewer,
human merge). The wild taint does not propagate: delegated cleanup passes
through every gate the original PR skipped.

Filing at settlement rather than approval is deliberate: findings against an
unmerged PR are hypothetical; findings against merged code are debt. A wild
PR closed without merge takes its findings to the grave, recorded as
`outcome=cancelled`, and no orphaned cleanup issues are created.

#### 12.8.2 Deduplication

Findings carry stable fingerprints (semgrep's native fingerprints; a
tool+rule+path+normalised-location hash otherwise). Before spawning, the
watcher checks open cleanup issues: repeat findings append a linking comment
to the existing issue rather than filing a duplicate. Granularity is one
issue per category per settlement ("41 lint findings from PR #212"), not one
per finding. If an open cleanup issue exists for the same category and
path-scope, the watcher appends to it instead of filing anew — three YOLO PRs
into the same messy module must not triple the backlog.

#### 12.8.3 Dismissal

Posted on the cleanup issue (or the stub before settlement) by an authoriser.
The watcher records actor, timestamp, category, and reason in the settlement
record. Undismissed, unfixed findings remain open issues on the board —
visible, ageing, and countable. The failure mode this exists to prevent is
findings rotting in CI logs nobody opens.

#### 12.8.4 Per-category configuration

    intake:
      reports:
        lint:     {file: auto, start: auto}
        tests:    {file: auto, start: auto}     # characterisation tests
        security: {file: auto, start: manual}
        deps:     {file: auto, start: manual}

`start: auto` applies `status:todo` on the spawned issue (which still passes
the planned lane's own approval gates); `start: manual` files it visibly and
leaves launch timing to a human. Security defaults to manual launch: filing
is safe; auto-dispatching a builder against a security finding is where a
human should choose the moment.

### 12.9 Provenance-weighted governance

`source:*` labels already adjust orchestrator behaviour
(`auto_start_agent_issues`); intake extends the same pattern. For issues
carrying `source:wild`, downstream defaults are stricter and MUST be
overridable only explicitly:

    governance:
      wild:
        auto_merge: false            # regardless of repo default
        final_approval: human        # a human merges, always
        max_review_cycles: 2         # tighter than the planned default of 3
        changes_requested_routes_to: builder   # builder | author

`changes_requested_routes_to: builder` means an agent iterates on the human's
wild code in response to review comments — for the developer who chose YOLO
mode, delegated iteration is usually the revealed preference. The `author`
setting instead posts the review to the PR and waits for a human push. Note
that §12.6.2's tripwire has already excluded workflow-touching diffs from
ever reaching a builder cycle.

**Admin force-merge bypassing gates:** the platform permits an admin to merge
a wild PR before `/approve-intent` or review completes; the spec cannot
prevent it. The resolution is observability instead of prevention — the
watcher detects unsatisfied gates at settlement and records
`gates_bypassed: true` with the merging actor in the settlement record. The
dashboard surfaces the count.

#### 12.9.1 Threat model

Three archaeologist failure modes and their containment:

- **Deception** (prompt injection via diff content): an attacker embeds
  instructions in the diff, commit messages, or file content, attempting to
  cause the archaeologist to produce a misleading reconstruction that a
  human approves. Mitigated by: (a) the computed facts block is independent
  of anything the model says — file list, LOC delta, dependency changes are
  enumerated before the model runs; (b) the facts-first comment ordering is
  normative (§12.7.1); (c) tripwires exclude the highest-risk diffs from the
  pipeline entirely (§12.6.2); (d) `review:security-concern` is
  scope-independent — the reviewer checks for security issues regardless of
  the scope the archaeologist declared.

- **Hallucination** (fabricated intent in the audit trail): the archaeologist
  invents a plausible-sounding interpretation that doesn't match the diff.
  Mitigated by: the author-confirmation loop (`questions` field, reply
  re-dispatch for same-repo authors), the `confidence` field surfaced
  prominently, and `source:wild` permanently marking the reconstruction as
  machine-inferred rather than human-stated intent.

- **Hijack** (side effects under agent credentials): a compromised
  archaeologist attempts to perform side effects beyond writing its JSON
  payload. Mitigated by the pure-function contract: no token, no shell
  execution, no network access. Every write is mediated by the orchestrator
  after schema-validating the payload. The fully-hijacked blast radius is a
  misleading string rendered inside a template, adjacent to deterministic
  facts, read by a human before any code runs.

#### 12.9.2 App permission delta

One new App: **janitor**.

    Permission       Level    Reason
    ---------------  -------  -------------------------------------------
    contents         write    Push autofix commits to wild PR branches
    checks           write    Report findings as check runs
    pull_requests    write    Comment on PRs (findings summaries)
    metadata         read     Required by all Apps

The janitor App holds no `issues`, `workflows`, or `actions` permissions.
It cannot review, approve, label, or touch protected branches.

The **archaeologist has no App identity** — it holds no token and performs
no writes. `contents:read` for context is its ceiling; mediated writes are
the only path to any GitHub state change.

#### 12.9.3 Workflow execution posture

Running repo-configured janitor commands against wild code is code execution.
For same-repo authors this is the standing trust already extended to
collaborators. For forks, GitHub's `pull_request` protections apply (§12.2.5).
In all cases:

- Janitor jobs MUST NOT receive repository secrets beyond the janitor App key.
- Intake workflows MUST NOT use `pull_request_target` with a checkout of
  contributed code (§12.2.6).
- The recursion guard (§12.2.4) is restated here as a security property:
  unguarded, it is also a cost-amplification attack vector.

#### 12.9.4 Residual risks (accepted, documented)

- A deceived archaeologist writes a plausible-but-wrong interpretation; a
  hurried human approves it. Mitigated by facts-first ordering and staleness
  rules; not eliminated. This is the residual risk of *any* human-approval
  system.
- Autofix janitors run tool binaries from the repo's own toolchain
  configuration; a malicious lockfile could theoretically weaponise a
  formatter. Partially mitigated by pinned tool versions in the intake
  workflow definition; full supply-chain hardening is out of scope for v1.2.

#### 12.9.5 Explicitly rejected alternatives

Post-hoc plan fabrication (writing reconstruction as if it were a plan,
without `source:wild`) — rejected because provenance laundering breaks the
audit trail's core guarantee. LLM-based classification — rejected for
unpredictability (§12.2.2). Archaeologist with direct GitHub writes —
rejected (§12.6.1). Janitor autofix for security findings — rejected
(§12.5.2, §12.5.7).

### 12.10 Receipts and metrics

#### 12.10.1 Run records

Janitor batches and archaeologist runs write standard JSONL run records.

`role: "janitor"` records carry one event per janitor tool per intake cycle:

    "intake": {
      "tools": [
        {"name": "format",  "mode": "autofix", "commit_sha": "abc123", "findings": 0},
        {"name": "semgrep", "mode": "report",  "commit_sha": null,     "findings": 2}
      ],
      "findings_by_category": {"lint": 41, "security": 2},
      "convergence_passes": 1,
      "oracle": "pass"    // pass | fail | none (no baseline suite)
    }

`total_cost_usd: 0.0` is written explicitly for every janitor record —
deterministic tools are free, and the corpus should say so rather than
omit them.

`role: "archaeologist"` records carry:

    "intake": {
      "confidence": "medium",
      "proposed_type": "feature",
      "diff_lines": 412,
      "facts_only": false    // true when max_diff_lines/max_recon_runs hit
    }

#### 12.10.2 Settlement record additions

Wild settlement records carry the following additional fields:

    "source": "wild",
    "intake": {
      "intake_cycles":       2,          // count of status:intake transitions
      "janitor_commits":     3,
      "findings_filed":      {"lint": 41, "security": 2},
      "findings_dismissed":  [{"category": "lint", "actor": "...",
                               "reason": "...", "ts": "..."}],
      "recon_confidence":    "medium",
      "gates_bypassed":      false,      // true + merging actor if admin force-merged
      "oracle":              "pass"      // pass | none | reverted
    }

Metrics consumers MUST tolerate multiple settlement records per issue (a
wild PR cancelled and later reopened, then merged — §12.4.2); the latest
is authoritative.

#### 12.10.3 Derived metrics

Named here so dashboards converge on shared definitions:

- **Wild share** — wild merges / all merges.
- **Wild-debt ratio** — cleanup issues spawned per wild merge.
- **Cleanup half-life** — median time from filing to close of spawned issues.
- **Dismissal rate** — dismissed / (dismissed + fixed), per category.
- **Intake overhead** — median archaeologist cost + wall-clock per wild merge.

Together these answer the question every engineering lead will ask: *what
does YOLO mode actually cost us?* The planned and wild lanes are comparable
from the same corpus with zero new instrumentation.

#### 12.10.4 Edge case — label collision on provisioning

A target repo already carries a label named `status:intake` from prior
tooling. The existing upsert contract (§3.5) applies unchanged: colour and
description are updated to spec values; `agentOS apply` reports the update.

### 12.11 Configuration reference

The full `intake:` block with defaults:

    intake:
      enabled: true
      exclude_actors: ["dependabot[bot]", "renovate[bot]"]
      linked_prs: ignore                 # ignore | janitors-report-only
      settle_seconds: 300
      convergence_passes: 2
      test_oracle: auto                  # auto | off
      max_diff_lines: 6000
      max_recon_runs: 5
      model: null                        # archaeologist model; null = orchestrator default
      approve_intent_self: true          # set by init from collaborator count
      tripwire_paths:
        - ".github/workflows/**"
        - ".github/actions/**"
        - "agentOS.yaml"
        - ".agentOS/**"
      janitors:
        - name: format
          run: "<tool command>"
          mode: report                   # autofix | report | off
          # block_on: critical           # optional: route to blocked on critical findings
        - name: semgrep
          run: "semgrep --config auto"
          mode: report
          block_on: critical
        - name: secrets
          run: "<secret-scanner>"
          mode: report
          block_on: any                  # secrets always block; not configurable downward
      paths:                             # per-path mode ratchet (optional)
        # "legacy/**":    {janitors_mode: report}
        # "generated/**": {janitors_mode: off}
      forks:
        autofix: false                   # normative; not configurable upward
      reports:
        lint:     {file: auto, start: auto}
        tests:    {file: auto, start: auto}
        security: {file: auto, start: manual}
        deps:     {file: auto, start: manual}

    governance:
      wild:
        auto_merge: false
        final_approval: human
        max_review_cycles: 2
        changes_requested_routes_to: builder

`agentOS init` MUST ship this block with `enabled: true` and every janitor in
`mode: report`. Report-only is behaviourally invisible (no pushes, no diff
mutations) while still exercising classification, stub creation, archaeology,
and the human gate — the correct first-contact posture. Operators graduate
janitors to autofix per-tool.

### 12.12 Deferred features

    Trailing mode (direct-to-main intake)   Requires solving attribution and
                                            settlement for already-merged
                                            commits; revisit with field data
                                            from v1.2.

    The Splitter (stacked-PR decomposition) High value for oversized wild
                                            diffs; blocked on reliable
                                            semantic diff chunking.
                                            max_diff_lines + facts-only mode
                                            is the v1.2 answer.

    Autofix on linked (planned-manual) PRs  Deliberately withheld; janitor
                                            pushes to deliberately-prepared
                                            branches are unwelcome until
                                            requested.

### 12.13 Open questions

1. **Source axis openness.** Intake added `source:wild` to core rather than
   as a plugin axis value, because the planned/wild distinction is
   first-class routing logic, not an operator concern. The next integrator
   will want `source:alert` (webhook-originated) or `source:import`. Should
   the `source` axis be opened to plugin value-additions? Deferred pending
   a concrete plugin deployment that needs it.

2. **Risk-acknowledged approval.** Should facts that cross a threshold (e.g.
   a new dependency, a secrets-adjacent path) require
   `/approve-intent --ack-risk` rather than plain approval, making the
   reviewer explicitly confirm they read the flags? Deferred pending
   real intake-review comment usage data; the facts block already surfaces
   the signals.

3. **Suggestion-mode autofix.** GitHub review suggestions instead of janitor
   commits would eliminate the push-race (§12.5.3) entirely at the cost of
   one click per fix. Worth prototyping if the settle-window mitigations
   prove insufficient in practice.

---

## Appendix A: Colour Reference

These are the canonical label colours used by the core spec. Operators MAY change colours;
the routing logic is based on label names, not colours.

  status:plan              c5def5   Light blue (planner entry)
  status:plan-review       e4e669   Yellow (awaiting approval)
  status:intake            c5b8f0   Light purple (wild PR classified; janitors/archaeologist)
  status:intake-review     e4d069   Yellow (awaiting /approve-intent)
  status:todo              ededed   Light gray
  status:in-progress       0075ca   Blue
  status:in-review         fbca04   Yellow
  status:changes-requested d93f0b   Red-orange
  status:approved          0e8a16   Green
  status:blocked           b60205   Dark red
  status:planning          bfd4f2   Light blue (legacy; kept for back-compat)
  status:done              0e8a16   Green

  agent:builder            1d76db   Blue
  agent:reviewer           cc317c   Pink
  agent:docs               5319e7   Purple
  agent:watcher            0075ca   Blue
  agent:planner            f9d0c4   Salmon
  agent:janitor            c2e0c6   Light green
  agent:archaeologist      d4c5f9   Light purple

  type:feature             84b6eb   Light blue
  type:bug                 ee0701   Red
  type:chore               fef2c0   Cream
  type:question            d876e3   Lavender

  review:scope-violation   b60205   Dark red

  source:agent-created     0e8a16   Green
  source:human-created     bfd4f2   Light blue
  source:wild              d4c5f9   Light purple (intake classifier; see §12)

  follow-on:needs-cleanup           fef2c0   Cream (intake report findings)
  follow-on:needs-tests             fef2c0   Cream (characterisation tests)
  follow-on:needs-security-review   b60205   Dark red (security findings)

---

## Appendix B: Runtime Interface Env Var Reference

  AGENT_ROLE              string    Required. Role name.
  ISSUE_NUMBER            string    Required. Issue number (integer as string).
  GITHUB_TOKEN            string    Required. App installation token.
  GITHUB_REPOSITORY       string    Required. "owner/repo" (set by GHA).
  GITHUB_RUN_ID           string    Required. Actions run ID (set by GHA).
  LLM_PROVIDER            string    Optional. Provider slug.
  LLM_MODEL               string    Optional. Model identifier.
  LLM_API_KEY             string    Optional. API key.
  AGENT_MAX_TURNS         string    Optional. Integer string.
  OPS_REPO                string    Optional. "owner/ops-repo".
  OPS_REPO_TOKEN          string    Optional. PAT for ops repo.

---

## Appendix C: Plan Body Template (CE-style)

The planner MUST use this template between the AGENTOS:PLAN:BEGIN/END markers.
The builder reads the content between these markers as its authoritative contract.

```markdown
<!-- AGENTOS:PLAN:BEGIN -->
### Plan

**Problem / intent**
<one-paragraph restatement of what the issue is asking for>

**Context & constraints**
<relevant existing behaviour, files, invariants, hard constraints>

**Approach**
<the chosen design, and briefly why over alternatives>

**Task breakdown**
- [ ] <file-level, ordered, independently checkable step>
- [ ] <...>

**Acceptance criteria**
- <observable condition 1>

**Test plan**
<how each acceptance criterion is verified — commands, smoke steps>

**Risks & open questions**
- <risk or decision that needs a human>

**Out of scope**
- <explicitly excluded>
<!-- AGENTOS:PLAN:END -->
```

---

## Appendix D: Approval Gate Design Notes

### Why live permission check, not a stored label

An earlier design stored approval in a `review:plan-approved` label and defended it
with a guard workflow that reverted the label if applied by a non-approver. That
approach requires: a dedicated guard workflow, a GITHUB_TOKEN loop-prevention caveat
(to avoid the guard triggering itself), and a label that can still be spoofed by a
repo admin with direct label access.

The dispatch-time check (§3.6.3) is simpler and stronger:
- No label to defend, so no guard workflow needed.
- No loop-prevention caveat.
- The permission check runs against live GitHub data at the moment of dispatch.
- Manually labelling an issue to bypass the gate simply results in no build — the
  orchestrator's check fails silently and the builder is not dispatched.

This appendix documents the reasoning so it is not lost if a future requirement
(e.g. an approval signal consumed by a system that cannot re-derive it live) ever
justifies a stored approval token. In that case, see the label+guard pattern
described in the plan brief's Appendix E.
