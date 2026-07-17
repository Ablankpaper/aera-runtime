# AgentEra compatibility gate

AgentEra may add account, ownership, versioning, policy, audit, and publication around Hermes, but it must not replace or weaken Hermes's native agent behavior. Any change that breaks this gate is release-blocking even when AgentEra-owned product tests pass.

## Run locally

From the Runtime repository root:

```bash
scripts/run_agentera_compatibility.sh -j 4 -q
```

The wrapper delegates to `scripts/run_tests.sh`, the canonical Hermes test runner. It intentionally contains no separate pytest setup and does not replace the full Runtime test suite.

## Protected behavior

| Invariant | Behavior tests |
| --- | --- |
| The system prompt and active-session tool contract remain stable | `tests/agent/test_system_prompt_restore.py` |
| Native Memory operations continue to use the Hermes profile | `tests/tools/test_memory_tool.py` |
| Background review remains local, isolated, cache-consistent, and restricted to its intended toolset | `tests/run_agent/test_background_review.py`, `tests/run_agent/test_background_review_cache_parity.py`, `tests/run_agent/test_background_review_toolset_restriction.py`, `tests/test_background_review_session_isolation.py` |
| Each installation keeps a physically isolated writable profile and cannot cross profile file boundaries | `tests/test_profile_isolation_runtime.py`, `tests/agent/test_file_safety_cross_profile.py` |
| Hermes can still create and manage learned skills | `tests/tools/test_skill_manager_tool.py` |
| Curator state and backups keep their native behavior | `tests/agent/test_curator.py`, `tests/agent/test_curator_backup.py` |

The selected files are existing Hermes behavior tests. AgentEra should extend this list when it introduces a new integration boundary; it must not rewrite the tests to merely assert AgentEra implementation details.

## Change boundary

This gate is additive infrastructure. Establishing it must not modify Hermes production Python code, Memory formats, background review scheduling, skill learning, Curator behavior, or profile routing. Product-specific integration belongs outside those native mechanisms and must pass this gate before release.
