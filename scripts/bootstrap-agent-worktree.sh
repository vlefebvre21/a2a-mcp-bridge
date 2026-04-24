#!/usr/bin/env bash
#
# bootstrap-agent-worktree.sh — Create a per-profile git worktree with a
# distinct git identity for a Hermes agent.
#
# Implements ADR-003 (per-profile git worktrees and distinct git identities).
# Scope: this repository only. For other repos, add a similar script per
# repo until Hermes-level generalisation (ADR-003 §6 Q2).
#
# Usage: bootstrap-agent-worktree.sh [-b|--branch <name>] <profile>
# Exit codes: 0 OK / 1 runtime error / 2 usage error.
#
set -euo pipefail

# ── Profile identity table ──
declare -A PROFILE_NAMES=(
    [vlbeau-main]="VLBeauOpus"
    [vlbeau-qwen36]="VLBeauQwen"
    [vlbeau-glm51]="VLBeauGLM51"
    [vlbeau-deepseek]="VLBeauDeepSeek"
    [vlbeau-opus]="VLBeauOpusAlias"
    [vlbeau-gemini]="VLBeauGemini"
    [vlbeau-heavy]="VLBeauHeavy"
    [vlbeau-mistral]="VLBeauMistral"
    [vlbeau-magent]="VLBeauMagent"
)

declare -A PROFILE_EMAILS=(
    [vlbeau-main]="opus@vlbeau.local"
    [vlbeau-qwen36]="qwen@vlbeau.local"
    [vlbeau-glm51]="glm51@vlbeau.local"
    [vlbeau-deepseek]="deepseek@vlbeau.local"
    [vlbeau-opus]="opus-alt@vlbeau.local"
    [vlbeau-gemini]="gemini@vlbeau.local"
    [vlbeau-heavy]="heavy@vlbeau.local"
    [vlbeau-mistral]="mistral@vlbeau.local"
    [vlbeau-magent]="magent@vlbeau.local"
)

# ── Usage ──
usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS] <profile>

Create a git worktree for a Hermes agent profile.

Arguments:
  profile              One of: ${!PROFILE_NAMES[*]}

Options:
  -b, --branch <name>  Use <name> as the starting branch (default: main)
  -h, --help           Show this help message and exit
EOF
}

# ── Argument parsing ──
profile=""
branch="main"

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        -b|--branch)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --branch requires an argument" >&2
                exit 2
            fi
            branch="$2"
            shift 2
            ;;
        -*)
            echo "ERROR: unknown option '$1'" >&2
            exit 2
            ;;
        *)
            profile="$1"
            shift
            ;;
    esac
done

if [[ -z "$profile" ]]; then
    usage >&2
    exit 2
fi

# ── Validate profile ──
if [[ -z "${PROFILE_NAMES[$profile]+x}" ]]; then
    echo "ERROR: unknown profile '$profile'" >&2
    exit 2
fi

user_name="${PROFILE_NAMES[$profile]}"
user_email="${PROFILE_EMAILS[$profile]}"

# ── Check we're inside a git repo ──
repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
    echo "ERROR: not inside a git repository" >&2
    exit 1
}

repo_basename="$(basename "$repo_root")"
repo_parent="$(dirname "$repo_root")"

# ── Compute worktree path ──
# Worktree lives as a sibling of the main checkout, with the profile short
# name appended. Example: /home/vince/projects/a2a-mcp-bridge-qwen36.
# Override via WORKTREE_PARENT env var (mainly for tests).
profile_short="${profile#vlbeau-}"
worktree_parent="${WORKTREE_PARENT:-$repo_parent}"
worktree_path="${worktree_parent}/${repo_basename}-${profile_short}"

# ── Check if path already exists ──
if [[ -e "$worktree_path" ]]; then
    if git worktree list --porcelain 2>/dev/null | grep -q "^worktree ${worktree_path}$"; then
        echo "ERROR: worktree already exists at ${worktree_path}, use 'git worktree remove' first" >&2
    else
        echo "ERROR: ${worktree_path} exists but is not a git worktree" >&2
    fi
    exit 1
fi

# ── Check starting branch exists (local or remote) ──
if ! git rev-parse --verify "refs/heads/${branch}" >/dev/null 2>&1 && \
   ! git rev-parse --verify "refs/remotes/origin/${branch}" >/dev/null 2>&1; then
    echo "ERROR: branch '${branch}' does not exist locally or on origin" >&2
    exit 1
fi

# ── Create the worktree ──
# Try checking out the branch directly first; if it's already checked out
# elsewhere (e.g. in the source repo), create a -b tracking branch instead.
if ! git worktree add "$worktree_path" "$branch" 2>/dev/null; then
    wt_branch="worktrees/${profile}"
    git worktree add -b "$wt_branch" "$worktree_path" "$branch"
fi

# ── Configure identity in the worktree ──
git -C "$worktree_path" config user.name "$user_name"
git -C "$worktree_path" config user.email "$user_email"

echo "INFO: worktree created at ${worktree_path} (identity: ${user_name} <${user_email}>)"
