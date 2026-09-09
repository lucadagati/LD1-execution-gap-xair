#!/usr/bin/env bash
# Deprecated: use sync_paper_outputs.sh (data/ + figures, no paper artifact bundle).
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/sync_paper_outputs.sh" "$@"
