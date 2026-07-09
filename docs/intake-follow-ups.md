# Intake Follow-Ups — Everything Not in the v1.2.2 Tag

**Purpose:** the complete, triaged remainder. Nothing in this doc shipped in
v1.2.2; if it matters, it is named here with a target. Supersedes the open
portions of `v1.2.2-fix-spec-and-plan.md` (F-numbers preserved for
traceability).

---

## 1. v1.2.3 — Fast-Follow Patch (code fixes, each validated on hello-world
before tagging)

**1.1 F1 remainder — stub discovery that survives link failure.**
The App-permission fix cured the symptom; the mechanism is still fragile.
Still to do: (a) machine marker `<!-- agentOS:intake-pr:<N> -->` written at
stub creation, searched before any create; (b) closing-link write becomes
MUST-succeed — remove the graceful-skip path, fail the run loudly with the
missing-permission diagnostic; (c) orphan adoption: marker-stub with no
`status:*` label gets `status:intake` applied on the next classify run.
Note: **stub reuse has never executed successfully in the field** — every
pre-fix retrigger minted a new stub, and no re-push happened after the fix.
Its first validation is a v1.2.3 test (§4, T3).

**1.2 F3 — draft tripwires + base-ref janitor config (security).**
Classify must evaluate tripwires for drafts; janitors job must read
`agentOS.yaml` from the base ref, never the PR head; remove janitor App
secrets from the executor env until autofix ships. The draft hole
(attacker-editable janitor commands with the janitor key in env) is real
and currently open.

**1.3 F5 — staleness by push time.** Replace `committedDate` with head-ref
`pushedDate` (fallback: last synchronize timeline event). Rebased or
backdated commits currently defeat the approval-staleness check.

**1.4 F7 — wild→planned transition.** When classify finds a non-stub
closing link on a PR with an existing marker-stub, close the stub with an
explanatory comment. Today it orphans the stub open at `status:intake`.

**1.5 F8 — classification observability.** Tolerant `INTAKE_ENABLED` parse;
one-line verdict (event, actor, wild?, reason, stub#) written to the step
summary on every classify run; `agentOS verify` prints effective `INTAKE_*`
values and warns on non-`[bot]` entries in exclude_actors. Two field
incidents this cycle (exclude_actors self-veto, four days; enabled-flag
suspicion) were invisible outside CI logs.

**1.6 F4.1/F4.3 — findings become visible.** Janitors apply `follow-on:*`
labels from findings categories; findings JSON attached to the intake-review
comment in a collapsed details block. (Full report-to-work stays v1.3.)

**1.7 F9.4 — bootstrap smoke test.** CI: `agentOS init` into scratch, grep
every job invoking `{{AGENT_RUNNER}}`/`claude`, assert CLI-install step +
`ANTHROPIC_API_KEY` present in the same job. Catches the class, not the
instance (three instances found by sequential field failure this cycle).

**1.8 F10.2 — verify warns on Apps for no-App roles.** Detect an installed
App matching a `create_app: false` role's slug; warn that SPEC §13 no
longer holds.

**1.9 F11.3/F11.4 — token-write audit + regression test.** Grep every
`status:*` label write for bare `GITHUB_TOKEN` where a *different* workflow
must react to the event; add the two-run regression test (approve →
distinct orchestrator run with reviewer executing).

**1.10 Spec text amendments** (land with their code): watcher permission
table; marker discovery normative, closingIssuesReferences demoted to
settlement mechanism; closing-link MUST-succeed; base-ref config invariant;
push-time staleness.

## 2. Decision Items (need a call, then a target)

**2.1 `review:security-concern` is unprovisioned.** The §13 threat model
leans on it ("scope-independent reviewer security check") but neither the
label nor reviewer behavior emitting it exists. Options: provision label +
teach the reviewer (v1.3), or amend §13 to say the current backstop is the
human facts-block read. Do not leave the spec claiming a control that
does not exist.

**2.2 `type:docs` vs schema enum.** Resolved in v1.2.2 (label added) —
listed for the record.

**2.3 `/request-changes` on wild stubs — untested and suspect.** The
orchestrator's handler is plan-scoped (routes toward replanning); its
behavior on a `source:wild` stub at `status:intake-review` is unknown and
plausibly wrong. Test it; then either wire wild-aware behavior (return to
`status:intake` + archaeologist re-run with the notes as context) or remove
it from the intake comment until it works.

**2.4 governance.wild — verify, don't trust.** The reviewer honored
`final_approval: human` and cited "max 2 review cycles" in its receipt —
better than feared. Still unverified: actual max_review_cycles enforcement
on a changes-requested loop, `auto_merge: false` against a repo with
auto-merge on, and the `changes_requested_routes_to: author` option
(unimplemented). Verify the first two (v1.2.3 test), build the third (v1.3).

## 3. v1.3 — Intake Completion (unchanged from the plan, consolidated)

1. Autofix finished: janitor token mint, push-with-lease, test oracle with
   revert+demote, real convergence revert, re-armed executor secrets.
2. Run records: JSONL receipts for archaeologist + janitor events; wild
   work must not be less observable than planned work.
3. Report-to-work complete: `/dismiss-findings` handler with recorded
   actor/reason; watcher spawns category issues at settlement with
   fingerprint dedup; `intake.reports.*` semantics enforced.
4. Settlement/metrics: projector + run_record learn `source:wild`;
   settlement fields (intake_cycles, janitor_commits, findings_filed,
   findings_dismissed, recon_confidence, gates_bypassed, oracle); derived
   metrics (wild share, wild-debt ratio, cleanup half-life, dismissal rate,
   intake overhead).
5. Governance wired fully, incl. `changes_requested_routes_to: author`.
6. Check-run publication of janitor findings.
7. F11.5 structural option: direct `workflow_dispatch`/`repository_dispatch`
   of the reviewer from approve-intent, removing the labeled-event
   side-channel dependency entirely.
8. Carried deferrals: trailing mode, intake-lite for linked human PRs,
   the Splitter.

## 4. Outstanding Test Matrix (hello-world)

Never run or never passed cleanly — execute against v1.2.3 candidates:

    T1   Planned-lane control, done RIGHT: a PR whose body says
         "Fixes #<real open issue>" → no stub, no intake labels.
         (The first attempt used an issue body + nonexistent #100.)
    T3   Reset-on-push + STUB REUSE: push to a wild branch at
         intake-review → same stub returns to status:intake (no new stub
         minted), fresh reconstruction, prior approval invalidated.
         First-ever field test of the reuse path.
    T4b  Refusal paths: /approve-intent from non-admin → permission
         refusal; at wrong status → "nothing to approve".
    T6   Findings: wild branch with a deliberate semgrep hit → findings in
         step summary (v1.2.2) / follow-on label + details block (v1.2.3).
    T-rt Red-team: draft wild PR editing agentOS.yaml → after F3, tripwire
         blocks and janitor config comes from base.
    T-c  Cancellation settlement: close a wild PR unmerged → stub settles
         outcome=cancelled.
    T-f  Fork PR (from a second account): hardened mode — report-only,
         no secrets, archaeologist gated on maintainer approval.

## 5. Hello-World Cleanup (housekeeping, do anytime)

- Close orphan stubs #14 and #15 (no closing links, dead) with a comment
  referencing this doc; same for #9/#10 if still open.
- Close #16 (blocked stub for PR #13) and PR #13 itself — the branch
  contains a workflow-edit commit and has served its purpose. #11 likewise
  if still open.
- Close test issue #12.
- Reset `INTAKE_SETTLE_SECONDS` from 15 to a realistic value (or remove the
  variable to restore the 300 default) once testing quiets down.
- Optional: delete the uninstalled archaeologist GitHub App to remove the
  temptation entirely.
