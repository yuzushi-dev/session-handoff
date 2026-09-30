# Client compatibility receipts

`scripts/verify_client_compat.py` creates a local receipt for one concrete
session-handoff build and host. It validates the contract fixtures without
starting a model. Client presence, authentication, and verified flows remain
separate facts: a missing executable or an omitted live probe is `not-run`,
never `passed`.

## Published matrix

The native results in `docs/compatibility-results.md` describe the frozen
`0.7.4-jev.3` final-C build. Release `0.7.4-jev.4` bumps metadata and release
documentation with the same runtime modules; those receipts retain their
original identity and are not relabeled as this package's certification.

This matrix changes only when a receipt for the exact package content,
platform, and client versions contains matching live evidence. The historical
README result is context, not fresh certification.

| Combination | Platform | Status for this package | Evidence or reason |
|---|---|---|---|
| Codex 0.153.4 | Linux | `not-run` | Historical comparison target; no bound live receipt |
| Codex 0.159.2 | Linux | `not-run` | Candidate; final packaged build has not completed native acceptance |
| Claude Code 2.1.263 to/from Codex 0.159.2 | Linux | `not-run` | Baseline pair; no bound two-way migration receipt |
| Claude Code 2.1.285 to/from Codex 0.159.2 | Linux | `not-run` | Installed-current pair; no bound two-way migration receipt |
| Codex 0.159.2 with the candidate Sando plugin | Linux | `not-run` | Composition requires its own exact-build live case |
| The same combinations | macOS | `not-run` | A Linux simulation cannot certify macOS |

Offline fixture results prove the report and adapter contracts only. They do
not change a row in this table to `passed`. A failed native case remains failed
in its receipt; a later omitted probe cannot overwrite it with `not-run`.

Run the offline check from a checkout:

```sh
python3 scripts/verify_client_compat.py \
  --output /tmp/session-handoff-compatibility.json
```

Use `--package-root` for an extracted npm package. The root must contain
`package.json`; this also proves that the packaged script can run without the
checkout's test or benchmark modules. The default output is
`$XDG_STATE_HOME/session-handoff/compatibility-report.json` (or
`~/.local/state/session-handoff/compatibility-report.json`).

The runner creates a synthetic repository and isolated HOME and XDG trees next
to the output. Subprocesses receive only `PATH`, locale, and TLS certificate
locations from the caller. API keys and other credentials are not inherited.
Receipt strings pass through the shared secret redactor before they are
written. `--version` is the only client command used by the
offline check; authentication remains `not-run`.

## Receipt contract

The receipt schema is `session-handoff.compatibility-report/v1`:

- `build` records the package version, checkout or installed-package origin,
  Git SHA and tree when available, dirty-worktree state, and SHA-256 of the
  effective packaged content. The content hash is authoritative when a dirty
  checkout shares a Git SHA with a different candidate.
- `environment` records Python, operating system, release, and architecture.
- `lab` records the isolated paths and the names of inherited variables.
- `clients` records executable presence, real path, version, installation type,
  and authentication status independently.
- `cases` records capability, proof level (`deterministic`, `simulated`, or
  `live`), exact clients and versions, status (`passed`, `failed`, or
  `not-run`), reason, evidence paths, and evidence SHA-256 values.

Deterministic fixture validation does not certify a native client format.
Consumers such as `doctor` may report a combination as verified only when a
matching `live` case binds the same build hash, platform, client version, and
capability. Unknown combinations are unverified; they are not inferred to be
incompatible.

The content digest covers `package.json` and each existing regular file chosen
by the package's `files` list. Paths are sorted. For every file the digest
receives the 8-byte big-endian length of its UTF-8 POSIX relative path, the path,
the 8-byte big-endian content length, and the content.

## Importing native evidence

Live probes are separate and explicit. Their evidence file has schema
`session-handoff.live-evidence/v1`. Its top-level `build` contains the exact
`package_version` and `content_sha256` from the offline receipt, and its
`environment` contains that receipt's `system`, `release`, and `machine`.
Evidence for a different build or platform is rejected. The document also
contains a `cases` array. Each case has
`id`, `capability`, `clients`, `client_versions`, `status`, `reason`, and
`evidence_files`. Evidence paths must be relative to the evidence JSON and must
resolve to existing files within that directory. Executed cases require at
least one evidence file.

The full native certification uses these case IDs: `native-installation`,
`claude-handoff`, `codex-handoff`, `claude-to-codex-migration`,
`codex-to-claude-migration`, `manual-compaction`, and
`automatic-compaction`. Missing cases remain `not-run`; directional cases do
not replace one another. Authentication changes a client's separate
`authenticated` status only through an explicit, single-client
`authentication` case such as `codex-authentication`. A successful `--version`
probe or another live capability never implies authentication.

```json
{
  "schema": "session-handoff.live-evidence/v1",
  "build": {
    "package_version": "0.7.4-jev.3",
    "content_sha256": "<64 lowercase hex characters>"
  },
  "environment": {
    "system": "Linux",
    "release": "<kernel release>",
    "machine": "x86_64"
  },
  "cases": [
    {
      "id": "codex-native-resume",
      "capability": "resume",
      "clients": ["codex"],
      "client_versions": {"codex": "0.159.2"},
      "status": "passed",
      "reason": "the client resumed the synthetic session",
      "evidence_files": ["codex-native-resume.txt"]
    }
  ]
}
```

Import it with:

```sh
python3 scripts/verify_client_compat.py \
  --live-evidence /path/to/lab/live-evidence.json \
  --output /tmp/session-handoff-compatibility.json
```

Invalid, missing, absolute, or escaping evidence paths produce a failed import
case and a nonzero exit. Evidence contents are hashed into the receipt. Native
captures must come from sessions constructed for the test; do not sanitize and
reuse personal conversations.

The contract tests use pytest:

```sh
python3 -m pytest tests/compat
```
