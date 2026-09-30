# Codex recovery and Sando composition pilot

This bounded Linux pilot used Codex CLI `0.159.2`, `gpt-5.6-luna`, and reasoning `low` with the repository's synthetic `compound-rot` fixture. It did not use private sessions, desktop clients, IDE surfaces, or the app-server API as evidence for CLI behavior.

## Recovery result

The semantic-handoff and native-compact arms used the same client, model, effort, facts, stale traps, and recovery probe. Each recovered all six critical facts, the immediate next action, both focused tests, and rejected the obsolete cap of 5 and broad `Exception` handling.

The native thread contains two real `compacted` events because bounded PTY startup retries submitted the compact command twice before the successful probe. The result demonstrates recovery after native compaction. It is not a one-compaction performance comparison and does not establish general superiority over native compaction.

The structured receipt is [native-compact-receipt.json](../.orca/compat-pilot/native-compact-receipt.json). The exact synthetic inputs are [recovery-fixture-compound-rot-short.md](../.orca/compat-pilot/recovery-fixture-compound-rot-short.md) and [recovery-oracle-compound-rot.md](../.orca/compat-pilot/recovery-oracle-compound-rot.md). The generated semantic state is [generated-recovery-handoff.md](../.orca/compat-pilot/generated-recovery-handoff.md).

Codex `0.159.2` was rejected by the existing version-pinned app-server benchmark. That lane remains `not-run` and is not used to make a CLI claim.

## Sando composition result

Installed Sando `0.7.0` captured the existing long synthetic fixture as a private `160256`-byte artifact. Its SHA-256 is `e08687ab31d86d0002e951b6ab91cf8b93aa28d5ac1f9d51e07fbe7729cdc219`; the local handle is `sando:sha256:e08687ab31d86d00`. Official artifact recovery returned schema `sando-artifact-recovery/v1`, the matching digest, and `sourceBytes: 160256`.

The current final-C package (`0.7.4-jev.3`, content hash `05268602f4f5228b684d726470025562ce9187cb873176e6310dcaf79d518d9f`, bundle hash `9cd6cefc7574c0fd4e43bdaa5d28752f541355f3002aea2529817aa0ec2715b0`) created and read a central handoff containing that handle and the exact recovery command. The submitted and read document hashes both equal `b05b49efaa4d40581208784701fbaacf02f43367f4187940a81c843a4b9bf480`.

The handoff document is [sando-handoff.md](../.orca/compat-pilot/sando-handoff.md), and the local artifact content is [e08687ab…txt](../.orca/compat-pilot/sando-workspace/.sando/sando/artifacts/e08687ab31d86d0002e951b6ab91cf8b93aa28d5ac1f9d51e07fbe7729cdc219.txt). A well-formed absent handle exited `2` with `artifact handle is unavailable`; no content was invented.

The artifact remains local to this workspace. The pilot adds no shared or cross-machine artifact store.

A later native Codex gate installed final-C and Sando together, exercised both manual and automatic checkpoint reinjection, and composed Sando artifact recovery with a validated handoff in one real session. See [Codex native compaction compatibility](compatibility-codex-compaction.md). The linked evidence files are local laboratory artifacts excluded from Git and the npm package.
