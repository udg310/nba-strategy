#!/usr/bin/env bash
# Select the release as the deployment user. Also usable against an offline remote.
set -euo pipefail

fail() { echo "$*" >&2; exit 1; }
[[ $# -eq 3 ]] || fail "Usage: checkout-release.sh REPOSITORY DIRECTORY BRANCH_OR_FULL_SHA"
remote=$1
directory=$2
requested=$3
if [[ $requested =~ ^[0-9a-fA-F]{40}$ ]]; then
  ref=$(printf '%s' "$requested" | tr 'A-F' 'a-f')
else
  git check-ref-format "refs/heads/$requested" || fail "Invalid release branch: $requested"
  ref="refs/heads/$requested"
fi

mkdir -p -- "$directory"
cd -- "$directory"
if [[ ! -e .git ]]; then
  [[ -z $(find . -mindepth 1 -maxdepth 1 -print -quit) ]] ||
    fail "Deployment directory is nonempty and has no .git: $directory"
  git init --quiet
  git remote add origin "$remote"
fi

tracked=$(git status --porcelain --untracked-files=no)
# Generated environments and new runner logs are expected. Edits to any
# tracked file, including archived runner logs, still appear in $tracked.
untracked=$(git ls-files --others --exclude-standard -- \
  . ':(top,exclude).venv' ':(top,exclude)data/live_trade_final' ':(top,exclude)data/dry_run')
[[ -z $tracked && -z $untracked ]] ||
  fail "Deployment has local changes. Preserve them before updating:
$tracked
$untracked"

git fetch --no-tags --no-recurse-submodules -- "$remote" "$ref" ||
  fail "Could not fetch requested release: $requested"
target=$(git rev-parse --verify 'FETCH_HEAD^{commit}')
if [[ $ref != refs/heads/* && $target != "$ref" ]]; then
  fail "Requested SHA does not identify the fetched commit: $requested"
fi
current=$(git rev-parse --verify --quiet HEAD || true)
printf 'Current commit: %s\nTarget commit:  %s (%s)\n' "${current:-<new install>}" "$target" "$requested"
if [[ -n $current ]] && ! git merge-base --is-ancestor "$current" "$target"; then
  fail "Requested release is not a fast-forward from $current to $target"
fi

# Preserve even ignored runtime data if a later release starts tracking it.
git checkout --no-overwrite-ignore --detach "$target"
