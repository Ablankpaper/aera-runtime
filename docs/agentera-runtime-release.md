# AgentEra Runtime release runbook

This workflow produces signed native Runtime Seeds for AgentEra Studio. It
packages executable program files only. It must never read, copy, migrate, or
delete a user's `HERMES_HOME`, Profile, Memory, sessions, learned Skills,
Curator state, credentials, Gateway/Cron state, logs, or workspaces.

## Release identity

The workflow accepts only four manual inputs:

- `agentera_revision`: a positive packaging revision;
- `channel`: `candidate` or `stable`;
- `candidate_number`: a positive candidate sequence number;
- `publish`: `false` for a signed rehearsal or `true` to create a tag and
  GitHub Release.

The Hermes version is read from `pyproject.toml`. The Runtime version is
`<hermes-version>-agentera.<revision>`. A manual run uses its reviewed
`github.sha`; the guarded pull-request rehearsal uses that pull request's exact
head SHA. Callers cannot supply either source value.

Before this workflow exists on the default branch, a reviewer may apply the
`runtime-dry-run` label to a same-repository pull request to run one candidate
rehearsal. That path fixes the revision and candidate number to `1`, fixes the
channel to `candidate`, checks out the pull request's exact head SHA, and can
never execute the Release creation step. Fork pull requests are rejected by
the job guard. Remove the label after the rehearsal so later commits do not
inherit an operator approval signal.

Candidate tags use
`runtime-v<version>-rc.<candidate-number>`. Stable tags use
`runtime-v<version>`. Tags and releases are immutable and are never replaced.

## Protected signing configuration

Create the GitHub Environment `runtime-production`. Store the Ed25519 private
key only in its protected `AGENTERA_RUNTIME_SIGNING_KEY_PEM_B64` secret. Store
the matching non-secret identifier in the repository variable
`AGENTERA_RUNTIME_SIGNING_KEY_ID`.

The private PEM must remain outside Git and the desktop repository. The
workflow materializes it with mode `0600` only inside the isolated Ubuntu sign
job, removes it in an `always()` cleanup step, and passes no signing secret to
compatibility, native build, or publish jobs. Environment reviewers should be
required before the sign job starts.

Generate a key pair outside the repository with:

```bash
uv run python scripts/generate_agentera_runtime_key.py \
  --key-id agentera-runtime-YYYY-NN \
  --private-out "$HOME/.config/agentera/runtime-signing/agentera-runtime-YYYY-NN.pem" \
  --public-out "$HOME/.config/agentera/runtime-signing/agentera-runtime-YYYY-NN.public.pem"
```

Review the public fingerprint independently before adding that public key to
the desktop trust set.

## Gate and artifact flow

1. The compatibility job runs the existing Hermes system-prompt, Memory,
   Profile, Skills, background review, and Curator invariants plus the Runtime
   Seed smoke suite.
2. `macos-14` must report `arm64`; `windows-2022` must report x64/AMD64. Each
   native runner installs Python `3.11.15` and Node `22`, assembles its own
   Seed, extracts it, and executes the isolated native smoke test.
3. The protected sign job downloads both unsigned results, verifies their
   canonical build metadata, hashes, source commit, Runtime version, and exact
   target set. It signs canonical manifests, verifies them with the derived
   public key, safely extracts both archives without executing foreign
   binaries, signs the channel index, and creates checksums and a license
   bundle.
4. The publish job always rechecks `SHA256SUMS` and the exact asset set. Only
   `publish=true` creates the immutable Git tag and GitHub Release.

`publish=false` is the mandatory first run for every new Runtime revision. It
exercises every build, smoke, signing, verification, and bundle step without
creating external release state.

## Release assets

For version `<version>` and channel `<channel>`, the signed artifact contains
exactly:

```text
agentera-runtime-<version>-darwin-arm64.tar.zst
agentera-runtime-<version>-darwin-arm64.manifest.json
agentera-runtime-<version>-darwin-arm64.manifest.sig
agentera-runtime-<version>-windows-x64.zip
agentera-runtime-<version>-windows-x64.manifest.json
agentera-runtime-<version>-windows-x64.manifest.sig
agentera-runtime-<channel>.index.json
agentera-runtime-<channel>.index.sig
agentera-runtime-<version>-licenses.zip
SHA256SUMS
```

Candidate releases are marked as GitHub prereleases. Stable releases are
marked latest, allowing the reviewed latest-release redirect to expose the
stable signed index. A candidate index never replaces the stable index.

## Reproducibility

Runtime archives normalize paths, modes, timestamps, install metadata, POSIX
shebangs, Windows console entrypoints, and archive ordering. Two clean native
builds from the same commit, target, Python `3.11.15`, lock file, and Runtime
version must produce the same archive SHA-256. The release manifest and its
signature include the release `created_at`, so they are not expected to match
across separate workflow runs.

Before publishing a new builder revision, compare two native dry runs and
investigate every archive hash difference. Never work around a reproducibility
failure by weakening the manifest or checksum checks.

## Key rotation

Key rotation uses an overlap period:

1. Generate a new key outside Git and record its reviewed public fingerprint.
2. Add the new public key to desktop trust while the old public key remains
   trusted, then release that desktop version.
3. Change the protected private-key secret and key-id variable together.
4. Publish and verify a candidate signed by the new key.
5. Promote a stable Runtime only after supported desktop versions trust it.
6. Remove the old public key only after the supported rollback window no
   longer contains artifacts that require it.

Compromise recovery uses a new key id. Never reuse an id for different key
material and never copy a private key into a release asset.

## Failure recovery

- If compatibility, either native smoke, signing, or bundle validation fails,
  fix the cause and rerun with `publish=false`.
- If a dry run fails, it has created no tag or Release. Its temporary Actions
  artifacts may be discarded after investigation.
- If publication fails before a Release exists, confirm that the tag does not
  exist before rerunning. The workflow refuses to replace an existing tag.
- If GitHub creates a partial candidate Release, quarantine it, audit the
  uploaded assets, and remove the incomplete prerelease and tag only through a
  separately reviewed operator action before reusing that candidate number.
- Never delete or overwrite a stable tag. Correct a stable publication with a
  new AgentEra revision and a new signed Release.

After a successful dry run, download the signed Actions artifact and verify
both target manifests with the reviewed public key before requesting separate
authorization for `publish=true`.
