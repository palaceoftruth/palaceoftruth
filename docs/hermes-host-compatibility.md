# Real-Hermes compatibility gate (SAR-1404)

The provider unit suite replaces `agent.memory_provider` with a small test stub.
It remains useful for Palace behavior, but cannot prove compatibility with a real
Hermes `MemoryProvider`, discovery loader, or `MemoryManager`.

`check_hermes_host_compatibility.py` loads those classes from an explicitly
supplied **trusted real checkout**, without starting an agent or making model/API
calls. Supported baseline: `NousResearch/hermes-agent` commit
`e83b1d51f13a08b424636f0b519db86a35aa7bd7`.

## Run locally

Use Linux with `libseccomp.so.2` (standard on the Ubuntu CI runner), Python 3.12,
and an isolated virtualenv. Minimum dependencies are the selected host's
`pyyaml`, `python-dotenv`, and `httpx` declarations. The workflow extracts these
from its `pyproject.toml`; it does not install the host or provider into a live
Hermes environment. It saves the selected requirements and installed versions.

```sh
/path/to/isolated/venv/bin/python scripts/check_hermes_host_compatibility.py \
  --hermes-root /path/to/trusted/hermes-checkout \
  --output /tmp/hermes-contract.json
```

The current repository's provider is the default. `--plugin-root DIR` instead
checks a trusted extracted plugin artifact. It is copied to scratch, never
installed in a live profile. `--timeout` is bounded to 1–180 seconds (default 45).
The output JSON records the resolved host SHA and individual gates; exit status
is 0 for the supported contract and 1 for incompatibility/setup failure.

Optional constructor/signature smoke (requires the host's additional context
compressor dependencies, deliberately omitted from the minimal CI lane):

```sh
/path/to/isolated/venv/bin/python scripts/check_hermes_host_compatibility.py \
  --hermes-root /path/to/trusted/hermes-checkout \
  --context-helper-smoke --output /tmp/hermes-context-contract.json
```

This imports and instantiates the real `ContextCompressor` with an explicit
context length. It **does not** summarize, prune, generate, or call a model.
Missing optional dependencies fail the requested gate, rather than being silently
reported as a pass.

## What is actually exercised

- Real host discovery of the copied provider, real base-class identity, and
  uncredentialed availability (must be false).
- Explicit initialization, registration, schema normalization, tool-name routing,
  static system prompt, and local invalid-argument/unknown-tool handling.
- Real manager fan-out to turn-start, prefetch, queued prefetch, sync with optional
  messages/author metadata, memory-write metadata adaptation, session resume,
  serialized end/switch boundary, bounded drain, and shutdown.
- Wrappers observe **real provider methods** without replacing their behavior.
  Exceptions swallowed by host fan-out still fail this gate. Tests inject a
  failing real-provider method to verify that regression detection.
- Legacy pre-compress invocation and strict checkpoint rejection through the real
  manager, not through the provider stub.

No-auth hooks can be no-ops. This is host API/lifecycle compatibility evidence,
**not** proof of network retrieval, durable capture, credentialed identity,
checkpoint durability, or a successful production conversation.

### Checkpoint limitation and future acceptance gates

Palace currently inherits checkpoint API v1. The real manager rejects
`require_checkpoint=True`, correctly. JSON therefore reports `checkpoint` as
`supported_limitation`, with `strict_checkpoint_supported: false`. This is an
accepted baseline limitation, not a CI failure or a claim of v2 support.

`--require-gate checkpoint` deliberately returns failure today. **Do not enable
it in CI until SAR-1406 implements and tests the durable v2 contract.** Merely
advertising v2 also fails closed, requiring acceptance fixtures rather than
allowing a successful no-op to masquerade as durable checkpointing.

The gate table and repeatable `--require-gate NAME` option are the extension
points. For SAR-1406 add credential-free transport fixtures that verify normalized
evidence handoff, a durable receipt before success, failure propagation preserving
the transcript, and cross-session ordering; then require that gate in CI.

## Isolation and scope

The child starts with `-I -S`: no inherited Python path, user site, `.pth`, or
`sitecustomize` startup. Its environment is an explicit allowlist with fresh
`HOME`, `HERMES_HOME`, and cwd. It contains no inherited secrets/configuration.
Project plugin discovery is disabled. Only the copied named memory provider is
loaded; no broad plugin discovery/entry-point autoload is run.

Before host imports, Linux seccomp denies socket creation/connect/send and exec
syscalls, inherited by background threads. Native libc socket self-checks cover
IPv4, IPv6, and Unix sockets. A Python audit hook additionally denies DNS,
subprocess execution, and external `.env`/auth/config reads, recording attempted
operations so swallowed failures cannot become green results. Failure to install
the kernel filter is a failure, never a weaker fallback. Scratch state is removed
even after a child timeout.

This is **not a general filesystem sandbox for malicious code**. Supply only
trusted local checkouts; CI executes unreviewed changes exclusively on disposable
GitHub-hosted runners with a read-only token and no checkout credential persistence.
Do not run arbitrary downloaded host/provider code on a credentialed workstation.

## CI

`.github/workflows/hermes-host-compatibility.yml` runs two bounded jobs in a matrix:

- supported immutable host SHA;
- upstream `main` canary, recording its resolved SHA before dependency installation.

It runs on relevant PR paths, weekly on Monday, and manually. Both use disposable
GitHub-hosted runners, a fresh dependency virtualenv, at most 15 minutes per matrix
job, and at most 180 seconds for the harness command. Reports, host SHA, and
installed dependency versions are uploaded for 14 days, even on failure.

A separate default-branch schedule/manual failure job has only `issues: write`.
It checks out no code, consumes no untrusted artifacts, and creates or updates a
single bot-owned issue using a stable marker. Serialized alerts prevent duplicate
creation. It cannot upgrade a host, publish an artifact, deploy, or merge a PR.
PR failures never run this privileged alert job.

The main build workflow now classifies `third_party_plugins/hermes/**` changes as
backend-fast, so plugin-only PRs cannot skip the existing provider unit suite.
The new workflow policy tests also execute the actual classifier shell against a
fixture Git repository.

## Regression tests and release evidence

```sh
HERMES_COMPAT_TEST_ROOT=/path/to/trusted/hermes-checkout \
  python -m pytest backend/tests/test_hermes_host_compatibility.py \
    backend/tests/test_hermes_host_workflow.py \
    backend/tests/test_build_push_workflow.py -q
```

Real-host subprocess cases skip unless the checkout variable is explicitly set;
the standalone CI harness always requires a real checkout. Run the existing
provider unit suite in a clean environment too: inherited `PALACEOFTRUTH_*` scope
configuration can contaminate its fixtures. Do not "fix" provider code to satisfy
an environment-contaminated test.

After an authorized push, require the **Hermes host compatibility** workflow's
`Real host (supported)` and `Real host (upstream-main)` jobs and the ordinary
**Build and Push Images** PR validation. Local checks cannot verify hosted-runner
permissions, checkout availability, artifact upload, or issue alerts. A default-
branch manual run validates the canary after merge; never trigger the separate
build workflow manually just to run this canary (that workflow can publish).
