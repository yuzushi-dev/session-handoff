import assert from "node:assert/strict";
import test from "node:test";
import { mountView } from "./view.mjs";

function fixture() {
  const elements = new Map();
  for (const id of ["panel", "status", "thread-id", "thread-status", "session-id", "used-tokens", "window-tokens", "used-percent", "source", "checkpoint", "updated-at", "suggestion", "dismiss", "restore"]) {
    elements.set(id, {
      textContent: "", hidden: false, style: {}, dataset: {}, listeners: {},
      addEventListener(name, handler) { this.listeners[name] = handler; },
      focus() { this.focused = true; },
    });
  }
  const document = {
    documentElement: { dataset: {}, style: { setProperty() {} } },
    getElementById: id => elements.get(id),
    listeners: {}, addEventListener(name, handler) { this.listeners[name] = handler; },
  };
  let calls = 0;
  const app = {
    ontoolresult: null, ontoolcancelled: null, onhostcontextchanged: null,
    getHostContext() { return { theme: "dark" }; },
    async connect() { calls++; },
    callServerTool() { throw new Error("UI must not call tools"); },
    sendMessage() { throw new Error("UI must not send messages"); },
  };
  return { app, document, elements, calls: () => calls };
}

const schema = "session-handoff.codex-thread-status/v1";

test("connects once, waits without fetching, and applies host theme", async () => {
  const f = fixture();
  await mountView(f.app, f.document);
  assert.equal(f.calls(), 1);
  assert.match(f.elements.get("status").textContent, /Waiting/);
  assert.equal(f.document.documentElement.dataset.theme, "dark");
  f.app.onhostcontextchanged({ theme: "light" });
  assert.equal(f.document.documentElement.dataset.theme, "light");
});

test("shows IDs with unknown context and ignores non-allowlisted data", async () => {
  const f = fixture();
  await mountView(f.app, f.document);
  f.app.ontoolresult({ structuredContent: {
    schema, thread: { id: "thread-1", sessionId: "session-1" },
    context: { source: "unavailable" }, transcript: "secret transcript",
    api_key: "secret", checkpoint: {},
  } });
  assert.equal(f.elements.get("thread-id").textContent, "thread-1");
  assert.equal(f.elements.get("session-id").textContent, "session-1");
  assert.equal(f.elements.get("used-tokens").textContent, "Unavailable");
  assert.equal(f.elements.get("used-percent").textContent, "Unavailable");
  assert.equal(f.elements.get("source").textContent, "Unavailable");
  assert.ok(![...f.elements.values()].some(element => /secret/.test(element.textContent)));
});

test("displays allowlisted fields as text and marks estimates", async () => {
  const f = fixture();
  await mountView(f.app, f.document);
  const summary = '<img src=x onerror="alert(1)">';
  f.app.ontoolresult({ structuredContent: {
    schema, thread: { id: "thread-2" },
    context: { usedTokens: 1200, windowTokens: 8000, usedPercent: 15, source: "estimate" },
    checkpoint: { summary, updatedAt: "2026-10-02T10:00:00Z" }, suggestion: "Review checkpoint",
  } });
  assert.equal(f.elements.get("checkpoint").textContent, summary);
  assert.equal(f.elements.get("used-tokens").textContent, "1,200");
  assert.equal(f.elements.get("used-percent").textContent, "15%");
  assert.match(f.elements.get("source").textContent, /Estimate/);
  assert.equal(f.elements.get("suggestion").textContent, "Review checkpoint");
});

test("shows only allowlisted thread statuses and clears stale status", async () => {
  const f = fixture();
  await mountView(f.app, f.document);
  assert.equal(f.elements.get("thread-status").textContent, "Unavailable");
  for (const [status, label] of [
    ["active", "Active"], ["idle", "Idle"], ["notLoaded", "Not loaded"], ["systemError", "System error"],
    ["unknown", "Unavailable"], ["constructor", "Unavailable"], [null, "Unavailable"], [undefined, "Unavailable"],
  ]) {
    f.app.ontoolresult({ structuredContent: { schema, thread: { id: "thread-1", status } } });
    assert.equal(f.elements.get("thread-status").textContent, label);
  }
  f.app.ontoolresult({ structuredContent: { schema, thread: { status: "active" } } });
  f.app.ontoolresult({ isError: true });
  assert.equal(f.elements.get("thread-status").textContent, "Unavailable");
});

test("accepts JSON text fallback, rejects malformed and unrelated results", async () => {
  const f = fixture();
  await mountView(f.app, f.document);
  f.app.ontoolresult({ content: [{ type: "text", text: JSON.stringify({ schema, thread: { id: "fallback" } }) }] });
  assert.equal(f.elements.get("thread-id").textContent, "fallback");
  for (const result of [ {}, { structuredContent: { schema: "unrelated", thread: { id: "hidden" } } }, { content: [{ type: "text", text: "invalid json secret" }] } ]) {
    f.app.ontoolresult(result);
    assert.match(f.elements.get("status").textContent, /unavailable/i);
    assert.equal(f.elements.get("thread-id").textContent, "Unavailable");
  }
});

test("rejects invalid numbers and preserves real zero values", async () => {
  const f = fixture();
  await mountView(f.app, f.document);
  f.app.ontoolresult({ structuredContent: {
    schema, context: { usedTokens: 0, windowTokens: -1, usedPercent: Infinity, source: "hook" },
  } });
  assert.equal(f.elements.get("used-tokens").textContent, "0");
  assert.equal(f.elements.get("window-tokens").textContent, "Unavailable");
  assert.equal(f.elements.get("used-percent").textContent, "Unavailable");
});

test("error and cancellation clear previous data without exposing error detail", async () => {
  const f = fixture();
  await mountView(f.app, f.document);
  f.app.ontoolresult({ structuredContent: { schema, thread: { id: "old-thread" } } });
  f.app.ontoolresult({ isError: true, content: [{ type: "text", text: "secret internal error" }] });
  assert.match(f.elements.get("status").textContent, /failed/i);
  assert.equal(f.elements.get("thread-id").textContent, "Unavailable");
  f.app.ontoolcancelled({ reason: "secret" });
  assert.match(f.elements.get("status").textContent, /cancelled/i);
});

test("Escape dismisses locally, restore is keyboard accessible, late results stay dismissed", async () => {
  const f = fixture();
  await mountView(f.app, f.document);
  f.document.listeners.keydown({ key: "Escape" });
  assert.equal(f.elements.get("panel").hidden, true);
  assert.equal(f.elements.get("restore").hidden, false);
  assert.equal(f.elements.get("restore").focused, true);
  f.app.ontoolresult({ structuredContent: { schema, thread: { id: "late" } } });
  assert.equal(f.elements.get("panel").hidden, true);
  f.elements.get("restore").listeners.click();
  assert.equal(f.elements.get("panel").hidden, false);
  assert.equal(f.elements.get("dismiss").focused, true);
  assert.equal(f.elements.get("thread-id").textContent, "late");
});

test("connection failure leaves a useful text fallback", async () => {
  const f = fixture();
  f.app.connect = async () => { throw new Error("secret transport detail"); };
  await mountView(f.app, f.document);
  assert.match(f.elements.get("status").textContent, /plain-text tool result/i);
});
