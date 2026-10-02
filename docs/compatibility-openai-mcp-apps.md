# OpenAI Codex and MCP Apps compatibility

## Verified surface

The integration was implemented against Codex CLI 0.160.0 and the matching published `openai-codex==0.160.0` SDK. Synthetic fixtures cover direct MCP `_meta.threadId` and `_meta.sessionId` fields, legacy calls without metadata, and the common Codex hook payload fields. Thread and session identifiers stay separate and are read afresh on each tool call.

`codex_thread_status` is read-only and accepts no arguments. It can return status only for the `threadId` attached by the host to that call. It does not use an older call, a hook's `session_id`, or a frontend-supplied identifier as a substitute. Without a bound thread or a supported local app-server, thread status remains unavailable.

The MCP App resource uses the allowlisted URI `ui://session-handoff/openai/read-only.html` and MIME type `text/html;profile=mcp-app`. The published package includes a self-contained bundle of the official MCP Apps bridge and upstream license notices. The view displays only allowlisted fields from the current tool result and makes no additional tool calls.

## Limits

- Codex hook payloads provide fields used by the current hooks, including `session_id`, `transcript_path`, `cwd`, `hook_event_name`, and `model`; they do not provide token counters. The existing compact advisor remains opt-in and keeps its labelled transcript-size estimate as fallback.
- The app-server adapter is optional and read-only. It uses the official SDK only when its version exactly matches 0.160.0 and the existing local Unix control socket is present. It calls `thread/read` with `includeTurns=false`, blocks inbound server requests, and closes its proxy after each call. The daemon/runtime version behind the socket is not checked; 0.160.0 is the verified pairing, and other versions are best-effort with failures reported as unavailable.
- The public SDK's thread-scoped read does not provide token usage or context-window values. `thread/tokenUsage/updated` is global; this integration does not consume it or attribute it to a thread. Token fields therefore stay null and their source is `unavailable`.
- No thread listing, compaction, resume, fork, transcript transfer, or other mutating app-server operation is exposed.
- The installed CLI schema and SDK were checked, and the adapter was tested with a simulated transport. The current Codex CLI's live MCP App renderer and access to a real host thread were not smoke-tested. The bundled App bridge uses the ES2022 `Object.hasOwn` built-in, so a rendering host must support it; the text tool response remains usable without an App renderer.

## Native migration decision

The 0.160.0 SDK exposes `thread/start` and typed request/response models for `thread/inject_items`; the wire method accepts raw Responses API items. The migration engine's portable text, image, function-call, and function-result payloads can be serialized in that shape. This is not enough to replace the current supervised rollout writer: injected items do not carry the source timestamps, session metadata, or provenance envelope, and their empty acknowledgement cannot prove exactly-once delivery. A timeout can leave a started thread with an uncertain prefix, and this integration has no verified rollback path. Runtime persistence, order after resume, and recovery after a partial injection were not verified in an isolated real app-server. The current converter therefore remains the migration path.

## Local checks

From the source checkout, install build tools with `npm ci --ignore-scripts`, then run `npm run build:openai-ui`, `npm run test:openai-ui`, and the Python tests. Building the committed resource requires Node 20 or newer; the published/runtime package is self-contained and does not include the UI build sources or require Node modules. The optional SDK is listed in `requirements-codex-app-server.txt` and is not a core dependency.

The source references are [Codex hooks](https://developers.openai.com/codex/hooks/), [Codex app-server](https://developers.openai.com/codex/app-server/), [MCP Apps](https://modelcontextprotocol.io/docs/extensions/apps), and the [published Python SDK](https://pypi.org/project/openai-codex/).
