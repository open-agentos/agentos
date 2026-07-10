# Open AgentOS

**AI Agent Command and Control Center**

Open AgentOS turns a GitHub repository into an AI operating system. Issues move through a
status-label state machine. Each transition fires a GitHub Actions workflow that routes work to the
right AI agent. Agents act, post receipts, and hand off — the full lifecycle of an agent's work —
from labelled issue to merged PR — all through standard GitHub primitives.

Bring your own agent: the runner — Claude, Codex, Hermes, your own script — is
a config value, and Open AgentOS is the protocol around it. Every run leaves a
cost-and-outcome record. Every role has its own least-privilege identity.

---

## The loop

```
issue labelled status:todo
        │
        ▼
  orchestrator fires ── routes by label ──▶ builder agent
        │                                        │
        │                                   opens PR, sets status:in-review
        ▼                                        │
   reviewer agent ◀───────────────────────────── ┘
        │
   approves / requests changes
        ▼
   PR merges ──▶ watcher settles the board
```

## Every run is accounted for

Each agent invocation leaves a structured record.

```json
{
  "event": "run",
  "role": "builder",
  "issue": 42,
  "model": "claude-sonnet-4-6",
  "turns": 11,
  "max_turns_hit": false,
  "total_cost_usd": 0.58,
  "clean_exit": "clean",
  "outcome": "merged"
}
```

## Built on least privilege

Each role gets its own GitHub App identity. The reviewer App can't push code — so a reviewer cannot approve its own work. The board App can't touch source. Every action an agent takes is attributable to a named identity, by design rather than by policy.

---

## Quickstart

A first run needs one agent. The full review-and-settle loop comes after.

```bash
# Install, and pull the spec into a repo
uv tool install open-agentos-cli
cd my-agent-repo
agentOS init --from github:open-agentos/agentos@v1.2.4

# Create the one App a first run needs (opens the browser once)
agentOS setup --repo my-org/my-agent-repo --apps builder

# Provision labels, board, and workflows; commit them; confirm
agentOS apply  --repo my-org/my-agent-repo --commit
agentOS verify --repo my-org/my-agent-repo
```

Then label an issue `type:feature` + `status:todo` and watch the PR open. Full walkthrough: **[Getting Started](./docs/getting-started.md)**.

---

## Docs

- [Getting Started](./docs/getting-started.md) — first run in ~30 minutes
- [Agent Roles](./docs/agent-roles.md) — identities, permissions, the runner interface
- [Label Model](./docs/label-model.md) — the state machine behind the labels
- [Intake: the YOLO lane](./docs/intake.md) — unplanned PRs, wild classification, `/approve-intent`
- [Metrics Schema](./docs/metrics-schema.md) — the run-record corpus
- [Specification](./SPEC.md) — the normative reference
- [Uninstall](./docs/uninstall.md) — how to remove agentOS from a repo
- [DIY AgentOS](./docs/diy-agentos.md) — build your own agent loop

## Intake setup requirements (v1.2+)

Three extra steps beyond a standard agentOS install if you want the wild-PR
pipeline to work end to end:

1. `ANTHROPIC_API_KEY` repo secret — the archaeologist needs it to run.
2. Watcher App must have **Pull requests: Read and write** — it writes the
   `Closes #N` link that stub dedupe and merge-settlement depend on.
   `agentOS upgrade` will tell you if it's missing.
3. `agents/archaeologist/AGENT.md` in the repo — installed automatically
   by `agentOS init` from v1.2.2 onward.

> **The archaeologist has no GitHub App by design — this is not a step you
> missed.** It is the only role without an App identity; see
> [docs/intake.md](./docs/intake.md#setup-requirements) and SPEC.md §13
> for why. `agentOS verify` will warn if an App for it is found.


## License

MIT — see [LICENSE](./LICENSE).
