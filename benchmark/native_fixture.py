"""Build a minimal native Codex paginated home for migration tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SESSION_ID = "10000000-0000-4000-8000-000000000002"


def build_paginated_codex_home(
    home: str | Path,
    session_id: str = SESSION_ID,
) -> Path:
    root = Path(home)
    rollout = root / "sessions" / "2026" / "08" / "25" / f"rollout-benchmark-{session_id}.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-25T10:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "session_id": session_id,
                    "timestamp": "2026-08-25T10:00:00Z",
                    "cwd": "/work/benchmark",
                    "cli_version": "0.149.1",
                    "history_mode": "paginated",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    database = root / "thread_history_1.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE thread_items (
            thread_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            rollout_ordinal INTEGER NOT NULL,
            created_at_ms INTEGER,
            item_json TEXT NOT NULL,
            item_type TEXT NOT NULL,
            updated_at_ordinal INTEGER
        );
        """
    )
    items = [
        (
            "user-1",
            "userMessage",
            {
                "type": "userMessage",
                "id": "user-1",
                "content": [
                    {"type": "text", "text": "benchmark user request"},
                    {
                        "type": "image",
                        "url": "https://example.test/portable-remote-image-sentinel.png",
                        "detail": "low",
                    },
                    {
                        "type": "localImage",
                        "path": "portable-local-image-sentinel.png",
                    },
                    {
                        "type": "audio",
                        "url": "https://example.test/portable-audio-sentinel.wav",
                    },
                    {
                        "type": "localAudio",
                        "path": "portable-local-audio-sentinel.wav",
                    },
                    {
                        "type": "skill",
                        "name": "portable-skill-sentinel",
                        "path": "/skills/portable-skill-sentinel",
                    },
                    {
                        "type": "mention",
                        "name": "portable-mention-sentinel",
                        "path": "src/portable-mention-sentinel.py",
                    },
                ],
            },
        ),
        (
            "agent-1",
            "agentMessage",
            {"type": "agentMessage", "id": "agent-1", "text": "benchmark assistant response"},
        ),
        (
            "command-1",
            "commandExecution",
            {
                "type": "commandExecution",
                "id": "command-1",
                "command": "pytest -q",
                "cwd": "/work/benchmark",
                "aggregatedOutput": "2 passed",
                "exitCode": 0,
                "status": "completed",
            },
        ),
        (
            "reasoning-1",
            "reasoning",
            {"type": "reasoning", "id": "reasoning-1", "summary": ["private"]},
        ),
        (
            "search-1",
            "webSearch",
            {
                "type": "webSearch",
                "id": "search-1",
                "query": "context rot",
                "action": {"type": "search"},
                "results": [{"title": "portable-web-result-sentinel"}],
            },
        ),
        (
            "patch-1",
            "fileChange",
            {
                "type": "fileChange",
                "id": "patch-1",
                "changes": [
                    {
                        "path": "src/cache.py",
                        "kind": "update",
                        "diff": "portable-file-change-sentinel",
                    }
                ],
                "status": "completed",
            },
        ),
        (
            "mcp-1",
            "mcpToolCall",
            {
                "type": "mcpToolCall",
                "id": "mcp-1",
                "server": "docs",
                "tool": "lookup",
                "status": "completed",
                "arguments": {"query": "portable-mcp-query-sentinel"},
                "mcpAppResourceUri": "portable-mcp-resource-sentinel",
                "result": {
                    "content": [
                        {"type": "text", "text": "portable-mcp-result-sentinel"}
                    ]
                },
                "error": None,
            },
        ),
        (
            "collab-1",
            "collabAgentToolCall",
            {
                "type": "collabAgentToolCall",
                "id": "collab-1",
                "tool": "spawnAgent",
                "status": "completed",
                "senderThreadId": "parent-thread",
                "receiverThreadIds": ["child-thread"],
                "prompt": "portable-collab-prompt-sentinel",
                "agentsStates": {
                    "child-thread": {
                        "status": "completed",
                        "message": "portable-collab-result-sentinel",
                    }
                },
            },
        ),
        (
            "activity-1",
            "subAgentActivity",
            {
                "type": "subAgentActivity",
                "id": "activity-1",
                "kind": "completed",
                "agentThreadId": "child-thread",
                "agentPath": "portable-agent-path-sentinel",
            },
        ),
        (
            "collab-v2-1",
            "collabToolCall",
            {
                "type": "collabToolCall",
                "id": "collab-v2-1",
                "tool": "send_input",
                "status": "completed",
                "senderThreadId": "parent-thread",
                "receiverThreadId": "child-thread",
                "prompt": "portable-collab-v2-prompt-sentinel",
                "agentStatus": {
                    "status": "completed",
                    "message": "portable-collab-v2-result-sentinel",
                },
            },
        ),
        (
            "image-view-1",
            "imageView",
            {
                "type": "imageView",
                "id": "image-view-1",
                "path": "portable-image-view-sentinel.png",
            },
        ),
        (
            "image-generation-1",
            "imageGeneration",
            {
                "type": "imageGeneration",
                "id": "image-generation-1",
                "status": "completed",
                "revisedPrompt": "portable-image-prompt-sentinel",
                "result": "portable-image-result-sentinel",
                "transparentBackground": False,
                "savedPath": "portable-image-output-sentinel.png",
            },
        ),
        (
            "compaction-1",
            "contextCompaction",
            {"type": "contextCompaction", "id": "compaction-1"},
        ),
        (
            "hook-1",
            "hookPrompt",
            {
                "type": "hookPrompt",
                "id": "hook-1",
                "fragments": [
                    {
                        "hookRunId": "hook-run-1",
                        "text": "portable-hook-prompt-sentinel",
                    }
                ],
            },
        ),
        (
            "plan-1",
            "plan",
            {"type": "plan", "id": "plan-1", "text": "portable-plan-sentinel"},
        ),
        (
            "dynamic-1",
            "dynamicToolCall",
            {
                "type": "dynamicToolCall",
                "id": "dynamic-1",
                "namespace": "fixture",
                "tool": "lookup",
                "arguments": {"query": "portable-dynamic-query-sentinel"},
                "status": "completed",
                "contentItems": [
                    {
                        "type": "inputText",
                        "text": "portable-dynamic-result-sentinel",
                    }
                ],
                "success": True,
                "durationMs": 4,
            },
        ),
        (
            "sleep-1",
            "sleep",
            {"type": "sleep", "id": "sleep-1", "durationMs": 25},
        ),
        (
            "review-enter-1",
            "enteredReviewMode",
            {
                "type": "enteredReviewMode",
                "id": "review-enter-1",
                "review": "portable-review-target-sentinel",
            },
        ),
        (
            "review-exit-1",
            "exitedReviewMode",
            {
                "type": "exitedReviewMode",
                "id": "review-exit-1",
                "review": "portable-review-result-sentinel",
            },
        ),
    ]
    for ordinal, (item_id, item_type, item) in enumerate(items):
        connection.execute(
            "INSERT INTO thread_items VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                "turn-1",
                item_id,
                ordinal,
                1_756_112_400_000 + ordinal,
                json.dumps(item),
                item_type,
                ordinal,
            ),
        )
    connection.commit()
    connection.close()
    return rollout
