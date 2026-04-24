#!/usr/bin/env bats

SCRIPT="$(cd "$(dirname "$BATS_TEST_FILENAME")/.." && pwd)/scripts/bootstrap-agent-worktree.sh"

setup() {
    TEMP_REPO=$(mktemp -d)
    cd "$TEMP_REPO"
    git init -q -b main
    git config user.name "Test User"
    git config user.email "test@example.com"
    git commit -q --allow-empty -m "initial"
    TEMP_REPO_BASENAME=$(basename "$TEMP_REPO")
    # Use the temp repo's parent dir as worktree base so tests never
    # write under /home/vince/projects/ even when WORKTREE_PARENT leaks
    # from the environment.
    TEMP_WORKTREE_PARENT=$(mktemp -d)
    export WORKTREE_PARENT="$TEMP_WORKTREE_PARENT"
    WORKTREE_BASE="${TEMP_WORKTREE_PARENT}/${TEMP_REPO_BASENAME}"
}

teardown() {
    # Remove any worktree directories created during this test
    if [[ -n "${WORKTREE_BASE:-}" ]]; then
        for d in "${WORKTREE_BASE}"-*; do
            [[ -d "$d" ]] && rm -rf "$d"
        done
    fi
    # Remove temp worktree parent and temp repo
    if [[ -n "${TEMP_WORKTREE_PARENT:-}" && -d "$TEMP_WORKTREE_PARENT" ]]; then
        rm -rf "$TEMP_WORKTREE_PARENT"
    fi
    if [[ -d "${TEMP_REPO:-}" ]]; then
        rm -rf "$TEMP_REPO"
    fi
    unset WORKTREE_PARENT
}

@test "test_help_flag: --help returns 0 and displays Usage" {
    run "$SCRIPT" --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"Usage"* ]]
}

@test "test_no_args: without argument returns 2" {
    run "$SCRIPT"
    [ "$status" -eq 2 ]
}

@test "test_unknown_profile: vlbeau-nonexistent returns 2 with 'unknown profile'" {
    run "$SCRIPT" vlbeau-nonexistent
    [ "$status" -eq 2 ]
    [[ "$output" == *"unknown profile"* ]]
}

@test "test_known_profile_happy_path: vlbeau-qwen36 creates worktree with correct identity" {
    run "$SCRIPT" vlbeau-qwen36
    [ "$status" -eq 0 ]

    expected_path="${WORKTREE_BASE}-qwen36"
    [ -d "$expected_path" ]

    # Verify worktree is tracked
    run git worktree list
    [[ "$output" == *"$expected_path"* ]]

    # Verify identity
    run git -C "$expected_path" config user.name
    [ "$output" = "VLBeauQwen" ]

    run git -C "$expected_path" config user.email
    [ "$output" = "qwen@vlbeau.local" ]
}

@test "test_worktree_already_exists: calling with same profile again returns 1" {
    profile="vlbeau-glm51"

    # Create the worktree first
    run "$SCRIPT" "$profile"
    [ "$status" -eq 0 ]

    # Try creating it again
    run "$SCRIPT" "$profile"
    [ "$status" -eq 1 ]
    [[ "$output" == *"worktree already exists"* ]]
}

@test "test_nonexistent_branch: --branch does-not-exist returns 1" {
    run "$SCRIPT" --branch does-not-exist vlbeau-mistral
    [ "$status" -eq 1 ]
    [[ "$output" == *"does not exist"* ]]
}

@test "test_outside_git_repo: running in /tmp returns 1" {
    run env -C /tmp "$SCRIPT" vlbeau-qwen36
    [ "$status" -eq 1 ]
    [[ "$output" == *"not inside a git repository"* ]]
}

@test "test_dir_exists_not_worktree: directory exists but is not a git worktree, returns 1" {
    expected_path="${WORKTREE_BASE}-heavy"

    # Create a plain directory (not a git worktree)
    mkdir -p "$expected_path"

    run "$SCRIPT" vlbeau-heavy
    [ "$status" -eq 1 ]
    [[ "$output" == *"exists but is not a git worktree"* ]]
}
