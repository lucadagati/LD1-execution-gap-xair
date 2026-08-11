#!/usr/bin/env bash
# Resolve repo layout for standalone XAIR repo, Adaptix monorepo, or paper artifact bundle.
# Sets: REPO_ROOT, SCRIPTS, XAIR_ROOT
_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -f "$_here/../pyproject.toml" ] && [ -d "$_here/../xair" ]; then
  # Standalone: repo root contains xair/, experiments/, scripts/
  REPO_ROOT="$(cd "$_here/.." && pwd)"
  SCRIPTS="$_here"
  XAIR_ROOT="$REPO_ROOT"
elif [ -d "$_here/../XAIR_Runtime" ]; then
  # Adaptix monorepo: adaptix/scripts + adaptix/XAIR_Runtime
  REPO_ROOT="$(cd "$_here/.." && pwd)"
  SCRIPTS="$_here"
  XAIR_ROOT="$REPO_ROOT/XAIR_Runtime"
elif [ -d "$_here/../xair_runtime" ]; then
  # Paper artifact bundle: artifact/code/scripts + artifact/code/xair_runtime
  REPO_ROOT="$(cd "$_here/.." && pwd)"
  SCRIPTS="$_here"
  XAIR_ROOT="$REPO_ROOT/xair_runtime"
else
  echo "ERROR: cannot locate XAIR runtime next to scripts/" >&2
  exit 1
fi

# Legacy alias used by older shell scripts
ADAPTIX_ROOT="$REPO_ROOT"
export REPO_ROOT SCRIPTS XAIR_ROOT ADAPTIX_ROOT
unset _here
