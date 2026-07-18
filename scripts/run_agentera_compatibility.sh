#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

TEST_PATHS=(
  tests/agent/test_system_prompt_restore.py
  tests/tools/test_memory_tool.py
  tests/run_agent/test_background_review.py
  tests/run_agent/test_background_review_cache_parity.py
  tests/run_agent/test_background_review_toolset_restriction.py
  tests/test_background_review_session_isolation.py
  tests/test_profile_isolation_runtime.py
  tests/agent/test_file_safety_cross_profile.py
  tests/tools/test_skill_manager_tool.py
  tests/agent/test_curator.py
  tests/agent/test_curator_backup.py
)

if [[ "${AGENTERA_RUNTIME_RELEASE_BUILD:-0}" == "1" ]]; then
  TEST_PATHS+=(tests/scripts/test_agentera_runtime_smoke.py)
fi

exec "$REPO_ROOT/scripts/run_tests.sh" "${TEST_PATHS[@]}" "$@"
