#!/usr/bin/env bash
# Configure the dedicated memory reviewer without changing Hermes' main model.
set -euo pipefail
exec python3 "$(cd -- "$(dirname -- "$0")" && pwd)/configure.py" "$@"
