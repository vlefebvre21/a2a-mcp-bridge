# ADR-003 — Per-profile git worktrees and distinct git identities for Hermes agents

- **Status:** Accepted (reviewed 2026-04-23 by VLBeauClaudeOpus — LGTM + 3 nits inlined + 3 open questions resolved)
- **Date:** 2026-04-23
- **Context window:** post v0.4.4, parallel to v0.5-bridge-primitives branch
- **Authors:** VLBeauQwen36 (drafter), VLBeauClaudeOpus (reviewer), vlefebvre21

## 1. Context

Multiple Hermes profiles (`vlbeau-main`/Opus, `vlbeau-qwen36`, `vlbeau-glm51`,
…) routinely operate on the **same git working directory**
(`/home/vince/projects/a2a-mcp-bridge`) in overlapping time windows.
Each profile runs in its own agent session, with its own in-RAM state, but
the underlying filesystem — including `.git/HEAD`, `.git/refs`, staged
changes, untracked files — is shared.

Two independent failure modes have been observed in production during the
v0.5 chantier (2026-04-22 → 2026-04-23):

1. **Branch-pointer TOCTOU.** A `git checkout`, `git branch -f`, or
   `git reset` executed by one agent silently mutates the HEAD/branch
   pointers visible to every other agent. Symptoms: "my branch moved
   without a commit I recognize", "HEAD is detached but I didn't detach
   it", "my tests were green and now they fail". Detection requires
   `git reflog`, which **does not record the mutating agent** — only a
   textual description of the operation.

2. **WIP absorption race.** Agent A creates untracked files or unstaged
   edits; agent B wakes up mid-stream, sees the WIP in the shared tree,
   polishes it, and commits before A. A's subsequent `sed`/`patch` on
   the same content becomes a no-op, and `git add -A` reveals only
   cosmetic differences. Time lost: 10 min per incident on average, plus
   duplicate A2A exchanges to reconstruct "who did what when".

Compounding both issues, the shared working tree has a single
`user.name` / `user.email` in `.git/config`:

```
user.name=the user
user.email=usermail@gmail.com
```

All commits made by any agent (or by the user in person) land under the
same authorship. `git log --format='%an'` cannot distinguish agents, and
`git reflog` cannot identify the author of a pointer mutation. Attribution
today requires a cross-reference across three independent channels:
commit message content, A2A message timestamps, and each agent's declared
scope. This cost has been paid multiple times already (see A2A archive
2026-04-22 evening and 2026-04-23 morning).

## 2. Problem statement

Two coupled problems to solve:

- **P1 — Isolation.** Prevent silent mutations of one agent's branch
  state by another agent operating on the same working directory. Each
  agent should be able to `git checkout`, `git branch -f`, or rebase
  without any risk of side-effects on peers.

- **P2 — Attribution.** Make `git log --format='%an %h %s'`
  self-sufficient for answering "which agent made this commit?" without
  requiring A2A cross-reference. Additionally, make "the user committed
  this himself" visually distinct from "an agent committed this".

Both problems already have workarounds (A2A diagnostic pings for P1;
scope+timestamp+commit-message triangulation for P2), but the workarounds
scale poorly: every new agent added to the roster multiplies the number
of cross-references needed per incident.

## 3. Options considered

### Option 1 — Status quo + A2A diagnostic protocol

Accept the shared working tree. Encode the diagnostic pattern
(reflog inspection → A2A ping → owner confirms → owner executes the fix)
into the `a2a-inbox-triage` skill. This is already deployed as of
2026-04-23 (Pitfall #8 + branch-pointer safety pattern).

**Pros**

- Zero infrastructure change. Already live.
- Works for any operation, not just git.

**Cons**

- O(n²) A2A traffic as agent count grows — every ambiguous pointer move
  triggers cross-checks against every active peer.
- Attribution remains a triangulation exercise, not a lookup.
- Does not prevent the WIP absorption race — only helps debug it
  post-mortem.
- `git reflog` loss: after 90 days of default reflog expiry, forensic
  attribution becomes impossible.

### Option 2 — Per-profile git worktree + per-worktree git identity (**recommended**)

For each active Hermes profile, create a dedicated `git worktree` at a
path parallel to the main checkout, and set profile-local
`user.name` / `user.email`:

```bash
git worktree add /home/vince/projects/a2a-mcp-bridge-qwen36 feat/<branch>
cd /home/vince/projects/a2a-mcp-bridge-qwen36
git config user.name  "VLBeauQwen"
git config user.email "qwen@vlbeau.local"
```

Proposed identity mapping:

| Profile            | `user.name`    | `user.email`            |
|--------------------|----------------|-------------------------|
| `vlbeau-main`      | `VLBeauOpus`   | `opus@vlbeau.local`     |
| `vlbeau-qwen36`    | `VLBeauQwen`   | `qwen@vlbeau.local`     |
| `vlbeau-glm51`     | `VLBeauGLM51`  | `glm51@vlbeau.local`    |
| `vlbeau-deepseek`  | `VLBeauDeepSeek` | `deepseek@vlbeau.local` |
| main checkout (the user) | `the user` | `usermail@gmail.com` |

Each worktree has its own `HEAD`, `index`, and working copy. Branch
pointers in `.git/refs/heads/` are shared across worktrees, but a
worktree's *checkout* of a branch is private: `git checkout other-branch`
in worktree A does not affect the HEAD of worktree B.

**Pros**

- Solves P1: branch-pointer TOCTOU is reduced to the rare case where two
  agents explicitly `git branch -f` the same ref. Everyday
  `checkout`/`rebase`/`reset` operations are isolated.
- Solves P2: `git log --format='%an %h %s'` directly identifies the
  agent. Commits by the user himself remain under his real identity, so
  "agent vs human" attribution is also free.
- No new tooling: `git worktree` is a standard git feature since 2.5.
- WIP absorption race is largely eliminated — each agent sees its own
  working copy, so B cannot see A's untracked files.
- Forensic attribution survives reflog expiry: `git log` is permanent.

**Cons**

- Requires an onboarding step per agent (create worktree, set identity).
  Mitigated by a one-time setup script.
- Disk usage: each worktree is a full checkout. Bridge repo is small
  (< 10 MB), acceptable. For larger repos, worktrees share `.git/objects`
  so only the working tree is duplicated.
- Commits authored under `opus@vlbeau.local` must **not** be pushed to a
  repository with CLA verification against real emails. Limited to
  VLBeau-internal repos for now.
- Branches that cross worktrees (e.g. agent A creates `feat/x`, agent B
  wants to continue on it) require an explicit `git worktree move` or a
  fresh checkout in B's worktree.

### Option 3 — Single shared worktree + commit-trailer based attribution

Keep the shared tree. Require every agent to append an
`Agent-Id: vlbeau-<profile>` trailer to every commit message. Provide a
`prepare-commit-msg` git hook that reads `$HERMES_PROFILE` and injects
the trailer automatically.

**Pros**

- Partially solves P2 (attribution readable via `git log`).
- No filesystem duplication.

**Cons**

- Does **not** solve P1 at all. Branch-pointer TOCTOU remains.
- Hook installation is per-clone and silently skippable. A single agent
  forgetting to install it breaks the invariant.
- Trailers are cosmetic — `%an`-based tooling (GitHub UI, `git shortlog`,
  blame heatmaps) still shows a single author.
- WIP absorption race is unaffected.

## 4. Decision

**Adopt Option 2: per-profile git worktrees with distinct git identities.**

Rationale:

- Option 2 is the only option that addresses both P1 (isolation) and
  P2 (attribution) at a structural level, rather than via protocol
  overhead.
- The one-time onboarding cost is bounded (one script, < 10 lines per
  profile) and amortizes across every future chantier.
- It composes cleanly with ADR-001 (multi-session per profile) and
  ADR-002 (wake-up intent): each *session* within a profile still shares
  the same worktree and identity, which is correct — the isolation
  boundary is the *profile*, not the *session*.
- The CLA caveat is acceptable because VLBeau internal tooling is not
  upstreamed under these synthetic identities. If a patch needs to go
  upstream, the user re-commits it from the main checkout under his own
  identity, which is already the status quo.

## 5. Consequences

### 5.1 Setup

Add a setup script `scripts/bootstrap-agent-worktree.sh` that takes a
profile name and:

1. Computes the worktree path:
   `/home/vince/projects/<repo>-<profile>`.
2. Creates the worktree on the profile's canonical starting branch
   (default: `main`).
3. Sets `user.name` and `user.email` per the mapping in §3 Option 2.
4. Emits a one-line confirmation with the path and identity.

### 5.2 Agent memory

Each agent's memory should record its **own** worktree path once, so
that subsequent `cd /path` commands are correct without asking the user.
Proposed memory entry:

> `a2a-mcp-bridge worktree: /home/vince/projects/a2a-mcp-bridge-<profile>,
> identity VLBeau<Profile> <<profile>@vlbeau.local>. NEVER operate on the
> main checkout (/home/vince/projects/a2a-mcp-bridge) — that's the user's.`

### 5.3 Shared-tree residual risk

Two residual cases remain even after Option 2:

- **Explicit `git branch -f` on a ref another worktree has checked out.**
  Git will refuse by default (`fatal: 'feat/x' is already checked out at
  '/path/to/other-worktree'`), which is the correct behaviour. Agents
  must not use `--force` to override this.

- **Two agents creating independent branches with the same name.** Both
  worktrees see a single `refs/heads/feat/x`; the second creation fails.
  Resolution: agents prefix branch names with their profile when
  chantier scope is not coordinated (e.g. `qwen/feat/logging-ext`).

### 5.4 Skill updates

- `a2a-inbox-triage` Pitfall #8 (branch-pointer safety): add a note that
  once ADR-003 is deployed, `%an` becomes the primary attribution key
  and the triangulation pattern becomes the fallback for cross-worktree
  mutations only.
- `github-pr-workflow` and `github-code-review`: ensure commit-signing
  and PR-authorship instructions reference the per-profile identity,
  not `usermail@gmail.com`.

### 5.5 CI / remote implications

- GitHub and other remotes will display commits under
  `VLBeau<Profile>` with a fake `@vlbeau.local` email. For private
  repos this is cosmetic; for public repos, squash-merge via the user's
  main checkout re-attributes the final commit.
- **Caveat on `@vlbeau.local`** — `.local` is reserved by RFC 6762
  (mDNS) for link-local name resolution. The address is intentionally
  non-routable and will never deliver mail, which is the desired
  property. Some tooling (commitlint, husky `commit-msg` hooks, overly
  strict CI email validators) may flag `@vlbeau.local` as malformed.
  Mitigation: document the convention in contribution guides; if a
  downstream consumer rejects it, fall back to `<profile>@vlbeau.example`
  per RFC 2606 or a user-owned domain the operator controls.
- No GPG signing required for the synthetic identities; if signing is
  later enforced, each profile gets its own GPG key.

### 5.6 Migration

- Existing feature branches (e.g. `feat/v0.5-bridge-primitives`) stay in
  the shared checkout until they merge. New branches created post-ADR-003
  acceptance are born in the appropriate worktree.
- No rewrite of historical commits. Past attribution remains
  triangulation-based; ADR-003 improves the future, not the past.

### 5.7 Shared `.git/hooks` residual risk

Per-profile worktrees share the same `.git/hooks/` directory (hooks are
repo-level, not worktree-level). Consequences:

- Any hook installed by one profile (pre-commit, commit-msg, pre-push)
  runs for **every** profile committing in any worktree. A hook that
  assumes a specific identity, shells out to a profile-specific script,
  or rewrites commit messages can break other agents' commits silently.
- Hook installation (via `pre-commit install`, `husky install`, or
  manual scripts) must be coordinated via A2A before being enabled:
  ping the active agents, agree on the hook set, install once from the
  main checkout, document in `CONTRIBUTING.md`.
- If per-profile hook behaviour is ever needed, `core.hooksPath` can be
  set per-worktree via `git config --worktree core.hooksPath` — deferred
  until a concrete need arises.

## 6. Resolved questions

All three open questions from the draft have been resolved during the
2026-04-23 review with VLBeauClaudeOpus:

1. **Main checkout location** — stays at
   `/home/vince/projects/a2a-mcp-bridge`. Moving it would break existing
   scripts, cron jobs, and tmux panes for no real benefit. Per-profile
   worktrees go alongside as `/home/vince/projects/a2a-mcp-bridge-<profile>`.

2. **`bootstrap-agent-worktree.sh` location** — committed to this repo
   under `scripts/`. Keeps the script versioned with the policy it
   implements; a short note at the top states it applies to this repo
   only. Hermes-level generalisation is deferred until a second repo
   adopts the same pattern.

3. **`vlbeau-main` profile handling** — the profile keeps its name;
   its synthetic identity is `VLBeauOpus <opus@vlbeau.local>`. The user
   commits as himself from a shell **outside** Hermes (the main checkout
   retains his real `user.name`/`user.email`). This matches current
   practice and avoids renaming a widely-referenced profile.

## 7. Related ADRs and skills

- **ADR-001** — Multi-session concurrency. Defines the session model
  within a profile. ADR-003 narrows the isolation boundary to the
  profile level.
- **ADR-002** — Wake-up intent coupling. Orthogonal: ADR-003 is about
  where the agent operates on disk, ADR-002 is about what the agent is
  supposed to do when woken.
- `a2a-inbox-triage` — Pitfalls #8 and WIP absorption document the
  failure modes that motivated this ADR.
