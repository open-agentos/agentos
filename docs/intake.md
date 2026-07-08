# Intake: the YOLO lane

*Ships in agentOS v1.2. This doc reflects v1.2.2.*

Sometimes you don't want to write an issue. You have an idea, or a CLI agent
open, or ten spare minutes — and you just want to push code. agentOS is
intent-first everywhere else: issue → plan → approval → build → review. But
real development includes the other mode, and pretending it doesn't happen
just means it happens invisibly.

Intake is the lane for that mode. You push a branch and open a PR. That's
the entire interface — no labels to remember, no issue to write, no command
to run. The system manufactures everything the planned lane gets up front —
the issue, the intent document, the review scope, the audit trail — after
the fact, and your unplanned work rejoins the same review loop everything
else goes through.

The pitch in one line: **YOLO in, receipts out.**

## The 60-second version

```
git checkout -b whatever
# ...hack, or let your CLI agent hack...
git push origin whatever
# open a PR. done. walk away.
```

What happens next, without you:

1. **Classification.** The classifier sees a PR with no linked issue from a
   human (not a bot, not an agent) and marks it *wild*. The rule is purely
   syntactic — no heuristics, no vibes — so you can always predict what the
   system will do.
2. **A stub issue appears**, labeled `source:wild` + `status:intake`, and
   your PR body gains `Closes #N`. This restores the invariant that every
   merged PR traces to an issue — the issue is just written after the code
   instead of before it.
3. **Janitors run** — deterministic tools (linters, semgrep, secret
   scanners) from your `agentOS.yaml`. In the default *report* mode they
   only publish findings; nothing touches your branch.
4. **The archaeologist reconstructs your intent.** An agent reads the diff
   and writes the issue that would have existed: what this change appears
   to do, its scope, its confidence, and questions for you if it isn't
   sure. Facts (files touched, LOC, dependency changes) are computed by
   tooling, never by the model — the model only writes the interpretation,
   clearly labeled as inferred.
5. **A human types `/approve-intent`** on the stub after checking the
   reconstruction against the diff. This asserts "yes, that's what I was
   doing" — not that the code is correct. Correctness is the next step's
   job.
6. **Your PR joins the normal loop** at `status:in-review`: the same
   reviewer agent, the same checks, the same merge gates as planned work.
   Merge closes the stub; the watcher settles it like any other issue.

## The state machine

```
unplanned PR opened
      │  classifier: stub issue, source:wild
      ▼
status:intake ────── touches protected paths ────► status:blocked
      │              (workflows, agentOS.yaml)        (human only)
      │  janitors run; archaeologist writes
      │  the reconstruction
      ▼
status:intake-review        ◄── any new push resets to status:intake
      │  /approve-intent          and invalidates prior approval
      ▼
status:in-review ──► the existing loop, unchanged ──► merged, settled
```

`source:wild` never comes off. Forever after, anyone auditing the repo can
see which work arrived planned and which arrived wild — and the analytics
can price the difference.

## Commands

On the stub issue:

- `/approve-intent` — the reconstruction matches your actual intent;
  proceed to review. Requires the configured approver permission
  (default: admin), verified live. The approval must postdate the latest
  push — keep hacking after approval and the approval self-invalidates.
- `/request-changes <notes>` — send the reconstruction back.

## Security model, honestly

Wild work can never be as trustworthy as planned work — the planned lane's
security property is intent-before-code, and intake structurally lacks it.
So intake doesn't pretend parity; it builds a visibly second-class trust
tier:

- **Tripwires run before any agent.** A wild diff touching
  `.github/workflows/**`, `agentOS.yaml`, or other protected paths goes
  straight to `status:blocked`. No reconstruction, no autofix, human eyes
  mandatory.
- **The archaeologist holds no GitHub token and has no App identity — by
  design, not omission.** It's a pure function: diff in, JSON out. The
  workflow validates the payload against a schema and performs every write
  on its behalf. Do not create a GitHub App for it; that silently defeats
  the containment. See [Setup requirements](#setup-requirements) below.
- **Facts before interpretation, always.** The intake-review comment leads
  with tool-computed facts and a direct link to the diff. The model's
  prose is captioned as inferred, with a confidence rating.
- **Stricter downstream defaults.** `source:wild` work gets human-only
  merge and tighter review-cycle limits, regardless of repo defaults.
- **Forks are hardened.** No autofix ever, GitHub's own fork protections
  (no secrets, maintainer approval for first-timers), and fork authors
  can't trigger archaeologist re-runs by commenting.

## Configuration

Repository variables (override without editing workflows):

    INTAKE_ENABLED            true (default)
    INTAKE_EXCLUDE_ACTORS     dependabot[bot],renovate[bot]
    INTAKE_SETTLE_SECONDS     300 — quiet period before the archaeologist runs
    INTAKE_MAX_DIFF_LINES     6000 — above this: facts-only, a human reads the diff
    INTAKE_APPROVE_INTENT_SELF true — may the author approve their own intent
    INTAKE_TRIPWIRE_PATHS     .github/workflows/**,... (protected paths)

The janitor sequence, per-path ratchets, and report-to-work settings live in
`agentOS.yaml` under `intake:` — see the annotated spec file. Every janitor
ships in **report** mode; graduate individual tools to autofix once trusted
(autofix push lands in v1.3 — see limitations).

⚠ A cautionary tale about `INTAKE_EXCLUDE_ACTORS`: it excludes *authors*,
including human ones. If intake silently ignores your PRs, check whether
your own username is in that list.

## Setup requirements

Three things the intake workflow needs beyond a standard agentOS install:

1. `agents/archaeologist/AGENT.md` in the repo (installed by
   `agentOS init` from v1.2.2).
2. `ANTHROPIC_API_KEY` (or your runner's equivalent) as a repo secret.
3. The **watcher App** needs `Pull requests: Read and write` — it writes
   the `Closes #N` link that stub dedupe and merge-settlement depend on.
   `agentOS upgrade` will tell you if it's missing.

**No GitHub App for the archaeologist.** That's not a step you missed.
The archaeologist has no App identity by design — see the security model
above. Every other role in `agentOS.yaml` gets an App; the archaeologist
is the deliberate exception. `agentOS verify` will warn loudly if an App
matching the archaeologist's slug exists, because it silently defeats the
security guarantee in SPEC.md §13.

## Current limitations (v1.2.2)

Honesty section. These are specified but not yet shipped:

- **Autofix doesn't push yet.** `mode: autofix` currently behaves as
  report. The full autofix pipeline — janitor App commits, test oracle,
  convergence revert — is the v1.3 headline.
- **Findings don't become issues yet.** Report findings land in the job
  step summary. Automatic filing as follow-on cleanup issues, with
  dismissal tracking, is v1.3.
- **No JSONL receipts for intake runs yet** (v1.3). The stub issue and its
  comments are the audit trail in the meantime.

## Why bother?

Because the alternative isn't "developers stop YOLOing" — it's YOLO work
merging with no review scope, no intent record, and no accounting. Intake
doesn't lower the bar for unplanned work; it raises the floor under it.
The same pipeline launders an agent's overnight spree, a teammate's messy
Friday PR, and a drive-by open-source contribution — labels don't care who
pushed. And because everything lands in the same label model and the same
settlement records as planned work, the question every engineering lead
asks — *what does YOLO mode actually cost us?* — stops being a debate and
becomes a dashboard.
