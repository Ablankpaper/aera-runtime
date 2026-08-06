# Hermes 0.20.0 AgentEra upgrade ledger

## Scope and non-promotion boundary

This branch integrates the reviewed Hermes `0.20.0` source as an isolated
`0.20.0-agentera.1` candidate. It does not update the Desktop Runtime Seed,
stable/internal manifests, installed Runtime bytes, signing/notarization, or
customer update channels. Existing `0.18.2-agentera.3` artifacts remain the
rollback source.

## Re-audited inputs

Audit date: 2026-08-06 (Asia/Shanghai), macOS arm64.

| Input | Planning SHA | Live SHA | Decision |
| --- | --- | --- | --- |
| Aera `origin/main` | `d8536e72a919eaa31245ea40dbc1faecf9e82d3d` | `a68171eab1cf427934273192ee6095a4324ce01d` | Use the live Aera SHA because the eight additional commits are the already-approved Line 1 stream-integrity delivery and its deterministic model-test isolation. |
| Hermes reviewed source | `42708f8bb39c9c2fc19146956699699bc3ea2da5` | `42708f8bb39c9c2fc19146956699699bc3ea2da5` | Keep the approved pin. |
| Hermes moving `upstream/main` | n/a | `01a1037d1e6d7b6eb96a786ef282c3aea4818194` | Excluded. It is 185 commits ahead of the reviewed source and requires a separate audit. |

The reviewed upstream and current upstream both declare package version
`0.20.0`; equal version text is not permission to substitute different source
bytes.

The re-audited divergence is `43` Aera-only commits and `4,734` upstream-only
commits. The merge base is
`0f102fa4dc04b7dfdab048169aaaa640d09d7523`.

## Upstream behavior and security review

The 4,734 upstream commits are adopted only through the pinned source. The
areas that can affect Aera-owned contracts are grouped below; each is owned by
the focused conflict suite first and the AgentEra compatibility gate after the
merge.

| Upstream area | Reviewed behavior/security impact | Aera acceptance owner |
| --- | --- | --- |
| Credential and secret scope | Locked credential-pool mutation, exact source classification, profile-scoped secret reads, inherited-key clearing, and bounded OAuth recovery. | `tests/test_tui_gateway_server.py`, compatibility Profile-isolation tests |
| Config and Profile migration | Canonical config loaders, migration registry/floor, syntax-error fail-closed writes, scalar/mapping preservation, and profile-aware `HERMES_HOME`. | `tests/cron/test_file_permissions.py`, compatibility Profile-isolation tests |
| Gateway/session lifecycle | Durable session keys, prompt-persistence failures, resume/reaper/delegate ownership, branch Profile scope, crash journals, and full-disk fail-closed behavior. | `tests/test_tui_gateway_server.py`, `tests/test_tui_gateway_ws.py` |
| Streaming/TUI transport | Batched state writes, model-switch reconciliation, reconnect behavior, Unicode input fixes, and websocket delivery. | `tests/test_tui_gateway_server.py`, `tests/test_tui_gateway_ws.py`, `tests/gateway/test_stream_consumer_fresh_final.py`, `npm run check --workspace @hermes/shared` |
| Skill/MCP boundaries | Active-Profile MCP discovery, deferred tool disclosure, skill catalog behavior, and secret-scope propagation. | AgentEra compatibility Skill/Profile tests |
| Kanban | Read-only empty polls, dispatcher/session isolation, board selection, worker exit status, and Profile-scoped execution. | `tests/hermes_cli/test_kanban_boards.py`, `tests/hermes_cli/test_kanban_core_functionality.py` |
| Dependencies/security floors | Patched dependency floors and lockfile security updates in the pinned tree. | `tests/test_project_metadata.py`, `uv run ruff check .`, candidate build inventory |
| Packaging/release | Upstream `0.20.0` metadata and release tooling must coexist with AgentEra candidate naming and signed-manifest protocol. | `tests/scripts/test_agentera_runtime_smoke.py`, `tests/scripts/test_agentera_runtime_protocol.py`, `tests/scripts/test_agentera_runtime_release_workflow.py` |

Optional upstream providers, plugins, UI features, and skills are not reopened
as Aera work in this train. They are accepted as pinned upstream content only
when they do not break an Aera contract.

## Aera-only commit inventory

`integration-only` means the merge commit introduces no distinct contract
beyond commits already listed in this table. `test-isolation` means the commit
changes deterministic test inputs without changing production behavior.

| # | Commit | Classification | Preserved behavior and owning verification |
| ---: | --- | --- | --- |
| 1 | `546a30eb` | product boundary | AgentEra compatibility gate; `scripts/run_agentera_compatibility.sh -j 4 -q`. |
| 2 | `9c00e1df` | packaging/release | CI requires the compatibility gate; exact-head CI plus the compatibility command. |
| 3 | `b940540a` | security/privacy, packaging/release | Signed manifest validation; `tests/scripts/test_agentera_runtime_protocol.py`. |
| 4 | `1c45a7d7` | packaging/release | Deterministic archives and inventories; `tests/scripts/test_agentera_runtime_archive.py tests/scripts/test_agentera_runtime_inventory.py`. |
| 5 | `2532974a` | packaging/release, platform compatibility | Relocatable seed and isolated smoke; `tests/scripts/test_agentera_runtime_builder.py tests/scripts/test_agentera_runtime_smoke.py`. |
| 6 | `5ff1c900` | packaging/release | Install into copied managed Python; `tests/scripts/test_agentera_runtime_builder.py`. |
| 7 | `0089e4ea` | packaging/release | Generated wheel output is excluded; `tests/scripts/test_agentera_runtime_builder.py`. |
| 8 | `8e9d3f17` | packaging/release | Runtime package directories survive inventory filtering; `tests/scripts/test_agentera_runtime_inventory.py`. |
| 9 | `df632fd4` | packaging/release | Only Runtime state caches are excluded; `tests/scripts/test_agentera_runtime_inventory.py`. |
| 10 | `2593556c` | packaging/release | Bundled Python `venv` module survives; `tests/scripts/test_agentera_runtime_inventory.py`. |
| 11 | `9b48e319` | security/privacy, packaging/release | Runtime smoke uses disposable state; `tests/scripts/test_agentera_runtime_smoke.py`. |
| 12 | `ef605a22` | packaging/release | Installed Runtime metadata is normalized; `tests/scripts/test_agentera_runtime_builder.py`. |
| 13 | `09b05abb` | platform compatibility, packaging/release | Windows entrypoints are deterministic; `tests/scripts/test_agentera_runtime_builder.py`. |
| 14 | `91102a52` | packaging/release | Signed candidate workflow and compatibility gate; `tests/scripts/test_agentera_runtime_release_workflow.py`. |
| 15 | `b861a757` | security/privacy, packaging/release | Release rehearsal is guarded and candidate-only; `tests/scripts/test_agentera_runtime_release_workflow.py`. |
| 16 | `db8e3260` | packaging/release | Runtime workspace dependencies are cached without changing source identity; `tests/scripts/test_agentera_runtime_release_workflow.py`. |
| 17 | `365a20f5` | platform compatibility, packaging/release | Managed Python selection in release jobs; `tests/scripts/test_agentera_runtime_release_workflow.py`. |
| 18 | `dcd67fa4` | packaging/release | Virtualenv Python is excluded from seeds; `tests/scripts/test_agentera_runtime_release_workflow.py`. |
| 19 | `20e9f582` | platform compatibility, packaging/release | Windows command shims resolve from managed Runtime; `tests/scripts/test_agentera_runtime_builder.py`. |
| 20 | `96c99265` | packaging/release | ZIP members have deterministic ordering; `tests/scripts/test_agentera_runtime_archive.py`. |
| 21 | `e46cd0f8` | security/privacy, platform compatibility | Windows smoke homes are isolated; `tests/scripts/test_agentera_runtime_smoke.py`. |
| 22 | `a182c90e` | security/privacy, packaging/release | Release write permission remains least-privilege; `tests/scripts/test_agentera_runtime_release_workflow.py`. |
| 23 | `dbe4c6e8` | packaging/release | AgentEra contributor mapping is retained; candidate release dry-run and conflict review own this metadata. |
| 24 | `8f8bc19f` | integration-only | No unique delta beyond commits 1-23. |
| 25 | `e6a4a6bb` | integration-only | No unique delta beyond commit 1. |
| 26 | `500d7211` | integration-only | No unique delta beyond commits 3-23. |
| 27 | `cb2f750e` | gateway/stream | Fresh-final aging uses monotonic uptime; `tests/gateway/test_stream_consumer_fresh_final.py`. |
| 28 | `c0439e1e` | integration-only | No unique delta beyond commit 27. |
| 29 | `cb690802` | Profile/Memory, security/privacy | Ephemeral context cannot enter self-evolution; `tests/test_ephemeral_context_self_evolution.py`. |
| 30 | `07619631` | product boundary, Profile/Memory | Dashboard reads avoid eager model/credential inventory; `tests/test_tui_gateway_server.py`. |
| 31 | `21853bda` | Profile/Memory, security/privacy | Hermes home repair invariants remain profile-safe; `tests/cron/test_file_permissions.py` and compatibility Profile-isolation tests. |
| 32 | `06c5e8e7` | integration-only | No unique delta beyond commits 30-31. |
| 33 | `dcb0f0bc` | product boundary | A cold chat starts with the selected model; `tests/test_tui_gateway_server.py`. |
| 34 | `6c259922` | Kanban | CLI returns the Kanban command failure status; `tests/hermes_cli/test_kanban_boards.py tests/hermes_cli/test_kanban_core_functionality.py`. |
| 35 | `d8536e72` | integration-only | No unique delta beyond commit 34. |
| 36 | `92fbaeba` | gateway/stream | Assistant turns carry `stream_id`, monotonic `seq`, `final_seq`, and `text_sha256`; `tests/test_tui_gateway_server.py` and `npm run check --workspace @hermes/shared`. |
| 37 | `8d4b0f6b` | gateway/stream | Dashboard websocket delivery has a single writer; `tests/test_tui_gateway_ws.py`. |
| 38 | `ccee98f4` | gateway/stream | Errored streams close without masquerading as complete; `tests/test_tui_gateway_server.py tests/test_tui_gateway_ws.py`. |
| 39 | `ddd3f914` | test-isolation | OpenRouter model tests do not depend on the live catalog; `tests/hermes_cli/test_models.py`. |
| 40 | `e3925deb` | integration-only | No unique delta beyond commit 39. |
| 41 | `64e1aa3d` | integration-only | Combines already inventoried Runtime main and Line 1 changes; no new contract. |
| 42 | `10ebcdbf` | gateway/stream test | Error lifecycle assertions own the contract in commit 38; `tests/test_tui_gateway_server.py`. |
| 43 | `a68171ea` | integration-only | Line 1 merge result; no unique delta beyond commits 36-42. |

No Aera-only behavior is retired in this upgrade. Integration-only merge
commits and deterministic test-isolation commits do not require separate
production behavior owners.

## Re-audited conflict set

The pinned merge now has 12 conflicts rather than the nine from planning:

```text
agent/credential_pool.py
hermes_cli/config.py
pyproject.toml
scripts/release.py
tests/gateway/test_stream_consumer_fresh_final.py
tests/hermes_cli/test_kanban_boards.py
tests/hermes_cli/test_kanban_core_functionality.py
tests/hermes_cli/test_models.py
tests/test_tui_gateway_server.py
tests/test_tui_gateway_ws.py
tui_gateway/server.py
tui_gateway/ws.py
```

The three added conflicts (`tests/hermes_cli/test_models.py`,
`tests/test_tui_gateway_ws.py`, and `tui_gateway/ws.py`) are direct owners of
the post-plan model-test isolation and Line 1 stream/single-writer contracts.
They do not expand the approved product design.

## Verification ledger

A verification key is repository/worktree + exact SHA + command/gate +
input/config/environment. A conclusive key is not rerun unchanged.

| Key | SHA/input | Gate | Environment | State | Result |
| --- | --- | --- | --- | --- | --- |
| `L5-AUDIT-001` | Aera `a68171eab1cf427934273192ee6095a4324ce01d`; pinned upstream `42708f8bb39c9c2fc19146956699699bc3ea2da5`; moving upstream `01a1037d1e6d7b6eb96a786ef282c3aea4818194` | fetch refs, version identities, `rev-list --left-right --count`, downstream inventory | read-only Git, macOS arm64, 2026-08-06 | passed | Pin retained; divergence `43 4734`; moving upstream excluded. |
| `L5-MERGE-TREE-001` | Aera `a68171ea` + pinned upstream `42708f8b` | `git merge-tree --write-tree --messages origin/main 42708f8b...` | clean isolated worktree, no merge in progress | passed | Expected reviewed set of 12 content conflicts. |
| `L5-BASELINE-001` | Aera `a68171ea` before merge | Task 1 seven-file focused baseline | local shared development venv, canonical `scripts/run_tests.sh`, isolated test homes | passed | 7 files, 642 tests passed, 0 failed in 21.4s. |
| `L5-MERGE-FOCUSED-RED-001` | merge `80d795f7206ad7b0d0a2a2f219b7003ab1d69a8e` | Task 3 six-file conflict-owner focused suite | local shared development venv, canonical `scripts/run_tests.sh`, isolated test homes | failed | 606 passed, 5 failed. Failures were limited to direct test doubles using the pre-0.20 `run_conversation` signature and the pre-queue private WebSocket send helper. |
| `L5-WS-GREEN-001` | merge `80d795f7` + direct-test diff `b872a00fd90d072029eefa51b8f110ec51d2d297522fd61e5c8c9cb7d60a35e4` | `scripts/run_tests.sh tests/test_tui_gateway_ws.py -q` | local shared development venv, canonical test runner, new compatibility input | passed | 9 passed. The cross-batch test now exercises Aera's loop-owned single-writer queue and proves whole-batch ordering. |
| `L5-SERVER-GREEN-001` | merge `80d795f7` + direct-test diff `b872a00fd90d072029eefa51b8f110ec51d2d297522fd61e5c8c9cb7d60a35e4` | `scripts/run_tests.sh tests/test_tui_gateway_server.py -q` | local shared development venv, canonical test runner, new compatibility input | passed | 527 passed. Test agents accept 0.20's persisted-user-message argument; failure completion preserves partial Unicode text and digest; a later hook cannot replace the first legal terminal frame. |
| `L5-CHILD-MIRROR-GREEN-001` | merge `80d795f7` + direct-test diff `b872a00fd90d072029eefa51b8f110ec51d2d297522fd61e5c8c9cb7d60a35e4` | `scripts/run_tests.sh tests/tui_gateway/test_subagent_child_mirror.py -q` | local shared development venv, canonical test runner, new compatibility input | passed | 9 passed. Mirrored child turns retain the Aera stream envelope and final digest under the upstream session lifecycle. |
| `L5-FOCUSED-GREEN-001` | `65d519ce443a7563f7c827e01c817f50517093c6` | Task 3 six-file conflict-owner focused suite | local shared development venv, canonical test runner, isolated test homes | passed | 6 files, 611 passed, 0 failed in 29.0s. |
| `L5-COMPAT-GREEN-001` | `65d519ce443a7563f7c827e01c817f50517093c6` | `scripts/run_agentera_compatibility.sh -j 4 -q` | local shared development venv, canonical compatibility runner, isolated test homes | passed | 11 files, 161 passed, 0 failed in 7.8s. |
| `L5-RUFF-GREEN-001` | `65d519ce443a7563f7c827e01c817f50517093c6` | `uv run ruff check .` | uv environment synchronized from the pinned lockfile | passed | All checks passed; Ruff reported one pre-existing malformed `# noqa` warning without a rule failure. |
| `L5-SHARED-DEPS-RED-001` | `65d519ce443a7563f7c827e01c817f50517093c6` | `npm run check --workspace @hermes/shared` | incomplete worktree `node_modules`; `vitest` absent although present in `package-lock.json` | failed | TypeScript could not resolve `vitest` from `src/skill-scaffold.test.ts`; no source or lockfile defect was indicated. |
| `L5-SHARED-GREEN-001` | `65d519ce443a7563f7c827e01c817f50517093c6` | `npm ci`, then `npm run check --workspace @hermes/shared` and `git diff --check` | Node `v24.13.0`, npm `11.6.2`, dependencies installed exactly from the committed lockfile | passed | Typecheck and ESLint passed; install and checks left tracked files unchanged. |

## Conflict decisions

| Conflict | Resolution decision | Focused behavior owner |
| --- | --- | --- |
| `agent/credential_pool.py` | Adopt upstream's read-only config loader and detailed rationale; it is behavior-equivalent to Aera's performance fix. | `tests/test_tui_gateway_server.py`, compatibility Profile tests |
| `hermes_cli/config.py` | Adopt upstream config changes but retain Aera's deliberate removal of process-lifetime home-setup memoization, so every load still repairs permissions and required directories. | `tests/cron/test_file_permissions.py`, compatibility Profile tests |
| `pyproject.toml` | Use upstream `0.20.0` dev/security pins and retain AgentEra's `zstandard==0.25.0` archive dependency; regenerate `uv.lock` with `uv lock`. | `tests/test_project_metadata.py`, Runtime archive/smoke tests |
| `scripts/release.py` | Adopt upstream's frozen legacy map plus one-file-per-email registry; migrate the AgentEra mapping to `contributors/emails/1468650289@qq.com`. | release dry-run and candidate contributor audit |
| `tests/gateway/test_stream_consumer_fresh_final.py` | Retain Aera's monotonic-aging and fallback behavior cases that upstream pruned. | this focused file |
| `tests/hermes_cli/test_kanban_boards.py` | Retain unknown/empty board rejection, invalid-slug exit status, and archive cases. | this focused file |
| `tests/hermes_cli/test_kanban_core_functionality.py` | Retain Aera's run/event and bulk-completion behavior cases. | this focused file |
| `tests/hermes_cli/test_models.py` | Retain deterministic catalog fixtures while adopting upstream model behavior tests. | this focused file |
| `tests/test_tui_gateway_server.py` | Keep Aera model-inventory scheduling, custom endpoint transport, stream-integrity, and error-lifecycle cases; also keep upstream battery and service-tier cases. | this focused file |
| `tests/test_tui_gateway_ws.py` | Keep Aera queue/single-writer/order/teardown cases and upstream cross-batch contract. | this focused file |
| `tui_gateway/server.py` | Adopt upstream's split handler modules and session lifecycle. Port Aera custom endpoint `base_url` into `methods_session.py`. Preserve sequenced stream envelopes for normal, error, notification, auto-continue, Kanban, and mirrored child turns while retaining upstream crash markers, billing errors, and resume replay. | server + websocket focused files; `@hermes/shared` check |
| `tui_gateway/ws.py` | Keep Aera's loop-owned queue as the sole socket writer, integrate upstream live-transport unregister, and join the writer during disconnect. The queue supersedes the weaker lock-only implementation while preserving whole-batch ordering. | websocket focused file |

The merge input remains exactly Aera `a68171ea` plus reviewed upstream
`42708f8b`; no content from moving `upstream/main` is included.
