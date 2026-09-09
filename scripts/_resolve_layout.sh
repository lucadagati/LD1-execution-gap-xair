#!/usr/bin/env bash
# Monorepo layout: adaptix/{scripts,XAIR_Runtime,ResearchTrack/...}.
# Sets: REPO_ROOT, SCRIPTS, XAIR_ROOT (ADAPTIX_ROOT alias)
_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -d "$_here/../XAIR_Runtime" ]; then
  REPO_ROOT="$(cd "$_here/.." && pwd)"
  SCRIPTS="$_here"
  XAIR_ROOT="$REPO_ROOT/XAIR_Runtime"
elif [ -f "$_here/../pyproject.toml" ] && [ -d "$_here/../xair" ]; then
  REPO_ROOT="$(cd "$_here/.." && pwd)"
  SCRIPTS="$_here"
  XAIR_ROOT="$REPO_ROOT"
else
  echo "ERROR: expected adaptix monorepo with XAIR_Runtime/ next to scripts/" >&2
  exit 1
fi

ADAPTIX_ROOT="$REPO_ROOT"
export REPO_ROOT SCRIPTS XAIR_ROOT ADAPTIX_ROOT
unset _here
