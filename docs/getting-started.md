# Getting Started

By the end of this quick setup guide, you will have a GitHub repository provisioned with the Open AgentOS label model, project board, GitHub Actions workflows, agent scaffolding, and required runner scripts — and you will have watched your first automated agent run complete successfully. It should take less than 15 minutes for someone comfortable with GitHub features and command line tools.

If at any time you wish to stop and reverse out of the installation process, you can uninstall it by referring to the [uninstall guide](./uninstall.md).

You can also build the system yourself. For an explanation of how to do this, see [DIY AgentOS](./diy-agentos.md).

---

## Fast path

With the [prerequisites](#before-you-start) in place, these commands take you from an empty repo to a first PR. Each is explained in [The walkthrough](#the-walkthrough).

```bash
# 1. Install the CLI
uv tool install open-agentos-cli

# 2. Pull the framework into a repo
cd my-agent-repo
agentOS init --from github:open-agentos/agentos@v1.2.3

# 3. Export GITHUB_TOKEN (required for the apply step)
export GITHUB_TOKEN=$(gh auth token)

# 4. Create the one App a first run needs (opens the browser once)
agentOS setup --repo my-org/my-agent-repo --role builder

# 5. Copy the utility workflows scripts from your clone of the agentos repository
# (Required: Workflows run python scripts under scripts/ for token minting and board syncs)
cp -r path/to/cloned/agentos/scripts ./scripts

# 6. Provision labels, board, and workflows
agentOS apply --repo my-org/my-agent-repo

# 7. Add your credentials as GitHub Actions secrets (see below — apply does NOT do this for you)
#    From .env: BUILDER_APP_ID, BUILDER_PRIVATE_KEY, and your LLM key (ANTHROPIC_API_KEY or LLM_API_KEY)

# 8. Copy prompt template to activate the builder role
cp agents/builder/AGENT.md.template agents/builder/AGENT.md

# 9. Commit and push the generated files manually
git add .github/ workflows/ agents/ AGENTS.md agentOS.yaml config.yaml.example scripts/
git commit -m "chore: provision agentOS core scaffolding, scripts, and agent prompts"
git push origin main

# 10. Confirm everything is wired up
agentOS verify --repo my-org/my-agent-repo
```

Then create an issue and label it:

```bash
gh issue create --repo my-org/my-agent-repo \
  --title "Add hello-world endpoint" \
  --body "Create a GET /hello endpoint that returns {\"message\": \"hello world\"}" \
  --label "type:feature"

gh issue edit <issue-number> --repo my-org/my-agent-repo --add-label "status:todo"
```

An Actions run appears within a few seconds. A minute or two later there's a PR on branch `agent/issue-<N>-<slug>` and the issue is at `status:in-review`.

---

## Before you start

Four things need to be in place:

**GitHub CLI, authenticated.** Check with `gh auth status`. For org work, grant the org and project scopes:

```bash
gh auth refresh -s admin:org,project
```

**An active GITHUB_TOKEN in your shell.** External CLI tools like `agentOS` do not automatically inherit authentication from the `gh` tool. You must export it explicitly:

```bash
export GITHUB_TOKEN=$(gh auth token)
```

**Python 3.11+.** Check with `python3 --version`.

**uv.** The CLI installs into an isolated environment with [uv](https://docs.astral.sh/uv/); `pip install open-agentos-cli` in a virtualenv also works.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**An LLM provider key.** Export one now; you'll add it as a repo secret in [step 4](#4-add-your-secrets) below.

```bash
export ANTHROPIC_API_KEY=sk-ant-...      # or OPENAI_API_KEY, or OPENROUTER_API_KEY
```

`LLM_API_KEY` also works as a provider-agnostic alternative — the runner accepts it directly and falls back to `ANTHROPIC_API_KEY` if it's unset.

You'll also need a repo to provision — an existing one where you have admin access, or a new one. If you're pointing agentOS at an existing repo, it's worth trying the flow on a low-stakes or sample repo first to get a feel for it before running it against something you can't easily revert:

```bash
gh repo create my-org/my-agent-repo --private --clone
cd my-agent-repo
```

---

## The walkthrough

The same steps, with what each one does.

### 1. Install and initialise

```bash
uv tool install open-agentos-cli
agentOS init --from github:open-agentos/agentos@v1.2.3
```

`init` fetches the canonical `agentOS.yaml` from the agentos repo, writes it locally, and creates a `.agentOS/` scaffold whose `keys/` directory is git-ignored. Pin to a tag rather than `@main` so a later change can't alter your label model without you asking for it.

The defaults are enough for a first run. Tuning `agentOS.yaml` is covered under [Customise the framework](#customise-the-framework).

### 2. Create the builder App

agentOS gives each role its own GitHub App identity instead of a shared token. That's the security model: the reviewer App can't push code, the board App can't touch source, and every agent action is attributable to a named identity. Creating an App requires a browser confirmation, so this step isn't fully scriptable — but a first run needs only the builder.

Roughly eight minutes, one trip through the browser.

```bash
agentOS setup --repo my-org/my-agent-repo --role builder
```
> [!NOTE]
> Ensure you pass `--role builder` (not `--apps builder`). The CLI parses role filters via the `--role` parameter.

The CLI opens GitHub's App-creation page with the fields pre-filled. For each App:

1. Review the permissions shown (builder: contents, pull requests, issues, checks).
2. Click **Create GitHub App**.
3. Click **Generate a private key** — this downloads a `.pem`. (Skipping it means regenerating the key by hand later.)
4. Return to the terminal and press **Enter**.

The CLI reads the `.pem` from your Downloads folder, moves it into `.agentOS/keys/`, and records the App ID in `.env`. The key stays on your machine — you'll copy it to GitHub as a repo secret in [step 4](#4-add-your-secrets) below.

### 3. Provision

Before provisioning workflows, copy the required helper scripts from the root of the core `agentos` repository to your local target directory under a `scripts/` folder. The provisioned GitHub workflows rely on these python utility scripts to sign App tokens, update Project boards, and handle run telemetry.

```bash
# Copy scripts/ folder from the agentos source code tree
cp -r path/to/cloned/agentos/scripts ./scripts
```

Once the scripts are in place, run `apply`:

```bash
agentOS apply --repo my-org/my-agent-repo
```

> [!IMPORTANT]
> The `apply` command requires the `GITHUB_TOKEN` environment variable to be exported in your terminal (see [Before you start](#before-you-start)). The `--commit` flag is not supported; you should review your git status and commit/push the files manually.

`apply` runs four steps and is safe to re-run — it skips anything already in place:

- **Labels** — upserts the label model. Creates what's missing, fixes changed colours, never deletes labels it didn't create.
- **Board** — creates the Projects v2 board and writes its ID into `agentOS.yaml`.
- **Workflows** — writes the orchestrator and builder workflows to `.github/workflows/`. These reference your secrets by name but `apply` does not upload them — that's a manual step, next.
- **Scaffold** — drops in editable prompt template files for the builder role (e.g., `agents/builder/AGENT.md.template`).

> [!NOTE]
> To activate the builder agent, you must copy or rename its template file to `AGENT.md` (e.g., `cp agents/builder/AGENT.md.template agents/builder/AGENT.md`). The template files are written with a `.template` extension so that subsequent updates or runs of `agentOS apply` do not overwrite your customized system prompts in `AGENT.md`.

Review the diff and commit the new files manually:

```bash
# Copy/rename the template prompt file to activate the builder role
cp agents/builder/AGENT.md.template agents/builder/AGENT.md

git add .github/ workflows/ agents/ AGENTS.md agentOS.yaml config.yaml.example scripts/
git commit -m "chore: provision agentOS core scaffolding"
git push origin main
```

### 4. Add your secrets

`agentOS setup` wrote your builder App's credentials to a local `.env` file — but that file never leaves your machine, and `apply` doesn't upload it. GitHub Actions can only read secrets added through the repo itself, so copy each value across by hand:

1. Open `.env` and find `BUILDER_APP_ID` and `BUILDER_PRIVATE_KEY`.
2. On GitHub, go to your repo's **Settings → Secrets and variables → Actions**, click **New repository secret**, and add each one (same name, same value).
3. Add one more: your LLM key, as `ANTHROPIC_API_KEY` (or `LLM_API_KEY`).

Full steps if you need them: [Creating secrets for a repository](https://docs.github.com/en/actions/security-guides/using-secrets-in-github-actions#creating-secrets-for-a-repository).

Skipping this step is the most common reason a first run fails silently — the workflow dispatches but the runner has no credentials. Do this before moving on.

Repeat it for every additional role App you create later (`agentOS setup --role reviewer`, etc.) — each writes its own `{ROLE}_APP_ID` / `{ROLE}_PRIVATE_KEY` pair to `.env` that needs the same manual copy.

### 5. Verify

```bash
agentOS verify --repo my-org/my-agent-repo
```

A passing run:

```
Verify report:
  ✓ labels:present                    all 31 present
  ✓ labels:colors                     all colors match
  ✓ workflow:agent-orchestrator.yml   present
  ✓ workflow:agent-settlement.yml     present
  ✓ workflow:detect-run-failure.yml   present
  ✓ workflow:run-receipt.yml          present
  ✓ board:board_id                    PVT_kwHOB4example123
  ✓ board:fields                      9/9 fields bound
  ✓ board:live                        board resolves in GitHub
  ✓ no-app-role:archaeologist         no App found for no-App role 'archaeologist' (correct)

  PASS — 10/10 checks passed
```

A `✗` line names the specific check that failed (e.g. `workflow:agent-orchestrator.yml — MISSING`) — the check name tells you what to fix. If everything else passes but the run itself fails, the most common cause is the secrets step above.

### 6. Run it

The orchestrator fires when an issue has both a `type:*` label and `status:todo`.

```bash
gh issue create --repo my-org/my-agent-repo \
  --title "Add hello-world endpoint" \
  --body "Create a GET /hello endpoint that returns {\"message\": \"hello world\"}" \
  --label "type:feature"

gh issue edit <issue-number> --repo my-org/my-agent-repo --add-label "status:todo"
```

Watch it:

```bash
gh run watch --repo my-org/my-agent-repo
```

The orchestrator reads the issue, moves it to `status:in-progress`, and dispatches the builder. The builder branches, runs the configured agent, commits, opens a PR, and sets `status:in-review`.

You end with a PR on `agent/issue-<N>-<slug>`, and a receipt posted as a comment on the issue:

```
### Agent run record (provisional)

- **✅ run exited cleanly** — clean-exit: `clean`
- role: `builder` · kind: `issue` · #42
- turns: **6** · tool calls: **14**
- friction — errors: 0 · retries: 0 · repeats: 0
- tokens — in: 18420 · out: 2103 · total: **20523**
- cost — $0.087210 (18420 in · 2103 out)
- duration: 73s · [View run](https://github.com/my-org/my-agent-repo/actions/runs/123456789)

_Outcome is `provisional` until the PR is settled (merge / CI / review)._
```

That receipt is the first entry in the run record — the per-run accounting of cost, turns, outcome, exit status, and token counts.

---

## Going further

The steps above used one App. The rest of the system builds on the same pattern.

### Add the review-and-settle loop

The full loop runs multiple roles, each with its own GitHub App identity or mediated execution layer. The core roles defined in the specification include:

* **`builder`** — Implements features and fixes; opens pull requests. The only role with write access to repository contents.
* **`reviewer`** — Reviews pull requests and issues; cannot push commits.
* **`watcher`** — Handles run settlement, issue creation, telemetry, and monitoring.
* **`board`** — Updates Projects v2 board fields (no code access).
* **`janitor`** — Runs deterministic cleanup tooling on wild branches during intake.
* **`archaeologist`** — Reconstructs intent from unplanned ("wild") pull requests. Has no App identity (mediated writes via watcher).
* **`planner`** — Expands issues into file-level implementation plans (reuses `builder` credentials).
* **`docs`** — Updates documentation and changelogs after approved PRs (reuses `builder` credentials).

Provision the other App-backed roles (`reviewer`, `watcher`, `board`, and `janitor`) the way you did `builder`:

```bash
agentOS setup --repo my-org/my-agent-repo --role reviewer --role watcher --role board --role janitor
agentOS apply --repo my-org/my-agent-repo
```

> [!NOTE]
> Ensure you copy or rename the respective prompt templates (`AGENT.md.template`) to `AGENT.md` for any new active roles (like `reviewer` or `watcher`) that you wish to customize and enable.

Each new role adds its own `{ROLE}_APP_ID` / `{ROLE}_PRIVATE_KEY` pair to `.env` — copy them to GitHub Actions secrets the same way as in [Add your secrets](#4-add-your-secrets).

`status:in-review` then triggers a review, merges settle on their own, and every run lands in the corpus. [Agent Roles](./agent-roles.md) covers what each identity can and can't do.

### Customise the framework

Edit `agentOS.yaml` to change behaviour:

- **`runtime.runner`** — the agent executable CI invokes (`claude`, `codex`, `hermes`, or your own).
- **`labels`** — the axes that drive routing; see [Label Model](./label-model.md).
- **`board`** — the fields on the Projects v2 board.

Re-run `agentOS apply` after a change. It only touches what differs.

### Add a plugin

Plugins add domain-specific labels, workflows, and scheduled agents without modifying core. See [Plugin Authoring](./plugins.md).

### Read the run data

Every run appends to a structured corpus — cost, turns, outcome, exit status. Once you have a handful, [Metrics Schema](./metrics-schema.md) covers querying what your agents cost and whether their work shipped.

---

## Next

- [Agent Roles](./agent-roles.md) — what each App can do, and the runner interface
- [Label Model](./label-model.md) — the state machine behind the labels
- [Specification](../SPEC.md) — the normative reference
- [Uninstall](./uninstall.md) — how to remove agentOS from a repo
