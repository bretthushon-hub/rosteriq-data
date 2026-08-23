#!/bin/bash
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

pip install espn_api "nfl_data_py" "pandas>=2.0" pyarrow
