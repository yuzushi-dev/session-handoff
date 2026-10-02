import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { webcrypto } from "node:crypto";
import { runInNewContext } from "node:vm";

test("committed HTML connects through the official bridge and never requests more data", async () => {
  const html = await readFile(new URL("../../assets/openai/read-only.html", import.meta.url), "utf8");
  const script = html.match(/<script>([\s\S]*)<\/script>/)?.[1];
  assert.ok(script, "a self-contained script is required");
  const elements = new Map();
  for (const id of ["panel", "status", "thread-id", "thread-status", "session-id", "used-tokens", "window-tokens", "used-percent", "source", "checkpoint", "updated-at", "suggestion", "dismiss", "restore"]) {
    elements.set(id, { textContent: "", hidden: false, addEventListener() {}, focus() {} });
  }
  const document = {
    documentElement: { dataset: {}, style: { setProperty() {} }, getBoundingClientRect: () => ({ height: 400 }) },
    body: {}, getElementById: id => elements.get(id), addEventListener() {},
  };
  const listeners = new Map();
  const messages = [];
  const errors = [];
  let initialized;
  const ready = new Promise(resolve => { initialized = resolve; });
  const receive = data => listeners.get("message")?.({ source: parent, data });
  const parent = {
    postMessage(message) {
      messages.push(message);
      if (message.method === "ui/initialize") {
        queueMicrotask(() => receive({
          jsonrpc: "2.0", id: message.id, result: {
            protocolVersion: "2026-01-26", hostInfo: { name: "Test host", version: "1.0" },
            hostCapabilities: {}, hostContext: { theme: "dark" },
          },
        }));
      }
      if (message.method === "ui/notifications/initialized") initialized();
    },
  };
  const window = {
    parent, innerWidth: 640,
    addEventListener: (name, callback) => listeners.set(name, callback),
    removeEventListener: name => listeners.delete(name),
  };
  // ES2020 hosts do not expose the ES2022 Object.hasOwn helper.
  runInNewContext(`Object.hasOwn = undefined;\n${script}`, {
    window, document, crypto: webcrypto, AbortController, TextEncoder, TextDecoder, URL,
    setTimeout, clearTimeout, queueMicrotask,
    requestAnimationFrame: callback => queueMicrotask(callback),
    ResizeObserver: class { observe() {} disconnect() {} },
    console: { debug() {}, warn() {}, error: (...args) => errors.push(args) },
  }, { contextCodeGeneration: { strings: false, wasm: false } });
  await Promise.race([ready, new Promise((_, reject) => setTimeout(() => reject(new Error("bridge handshake did not complete")), 1000).unref())]);
  await new Promise(resolve => setImmediate(resolve));
  receive({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: {
    content: [], structuredContent: {
      schema: "session-handoff.codex-thread-status/v1", thread: { id: "live-thread", status: "active" },
      context: { source: "unavailable" },
    },
  } });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements.get("thread-id").textContent, "live-thread");
  assert.equal(elements.get("thread-status").textContent, "Active");
  assert.equal(elements.get("used-tokens").textContent, "Unavailable");
  assert.equal(document.documentElement.dataset.theme, "dark");
  assert.equal(errors.length, 0);
  assert.deepEqual(messages.filter(message => "id" in message).map(message => message.method), ["ui/initialize"]);
  assert.ok(messages.every(message => ["ui/initialize", "ui/notifications/initialized", "ui/notifications/size-changed"].includes(message.method)));
});
