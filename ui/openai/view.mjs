const SCHEMA = "session-handoff.codex-thread-status/v1";
const UNAVAILABLE = "Unavailable";
const FIELDS = ["thread-id", "thread-status", "session-id", "used-tokens", "window-tokens", "used-percent", "source", "checkpoint", "updated-at", "suggestion"];
const SOURCES = { "app-server": "App server", hook: "Hook", estimate: "Estimate", unavailable: UNAVAILABLE };
const THREAD_STATUSES = { active: "Active", idle: "Idle", notLoaded: "Not loaded", systemError: "System error" };

function text(value, limit = 1200) {
  if (typeof value !== "string" || !value.trim()) return UNAVAILABLE;
  return value.length > limit ? `${value.slice(0, limit)}…` : value;
}

function number(value, percent = false) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return UNAVAILABLE;
  if (!percent && !Number.isInteger(value)) return UNAVAILABLE;
  return `${value.toLocaleString("en-US", { maximumFractionDigits: percent ? 1 : 0 })}${percent ? "%" : ""}`;
}

function statusData(result) {
  if (result?.structuredContent?.schema === SCHEMA) return result.structuredContent;
  // Older hosts may forward only the plain-text tool result.
  for (const item of result?.content ?? []) {
    if (item?.type !== "text" || typeof item.text !== "string" || item.text.length > 32768) continue;
    try {
      const parsed = JSON.parse(item.text);
      if (parsed?.schema === SCHEMA) return parsed;
    } catch { /* An unrelated or plain-text message is not status data. */ }
  }
  return null;
}

export async function mountView(app, document) {
  const element = id => document.getElementById(id);
  const set = (id, value) => { element(id).textContent = value; };
  const clear = () => { for (const id of FIELDS) set(id, UNAVAILABLE); };
  const status = message => { clear(); set("status", message); };
  const contextChanged = context => {
    if (context?.theme === "dark" || context?.theme === "light") {
      document.documentElement.dataset.theme = context.theme;
    }
    for (const edge of ["top", "right", "bottom", "left"]) {
      const inset = context?.safeAreaInsets?.[edge];
      if (typeof inset === "number" && Number.isFinite(inset) && inset >= 0) {
        document.documentElement.style.setProperty(`--safe-${edge}`, `${inset}px`);
      }
    }
  };
  const dismiss = () => {
    element("panel").hidden = true;
    element("restore").hidden = false;
    element("restore").focus();
  };
  element("dismiss").addEventListener("click", dismiss);
  element("restore").addEventListener("click", () => {
    element("panel").hidden = false;
    element("restore").hidden = true;
    element("dismiss").focus();
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && !element("panel").hidden) dismiss();
  });

  status("Waiting for the tool result…");
  // Register before connect: the host may deliver results during initialization.
  app.ontoolresult = result => {
    if (result?.isError) {
      status("Thread status failed. See the plain-text tool result for details.");
      return;
    }
    const data = statusData(result);
    if (!data) {
      status("Thread status unavailable. See the plain-text tool result.");
      return;
    }
    clear();
    set("status", "Read-only status received.");
    set("thread-id", text(data.thread?.id, 256));
    set("thread-status", Object.prototype.hasOwnProperty.call(THREAD_STATUSES, data.thread?.status) ? THREAD_STATUSES[data.thread.status] : UNAVAILABLE);
    set("session-id", text(data.thread?.sessionId, 256));
    set("used-tokens", number(data.context?.usedTokens));
    set("window-tokens", number(data.context?.windowTokens));
    set("used-percent", number(data.context?.usedPercent, true));
    set("source", Object.prototype.hasOwnProperty.call(SOURCES, data.context?.source) ? SOURCES[data.context.source] : UNAVAILABLE);
    set("checkpoint", text(data.checkpoint?.summary));
    set("updated-at", text(data.checkpoint?.updatedAt, 64));
    set("suggestion", text(data.suggestion));
  };
  app.ontoolcancelled = () => status("Thread status cancelled.");
  app.onhostcontextchanged = contextChanged;
  try {
    await app.connect();
    contextChanged(app.getHostContext());
  } catch {
    status("App connection unavailable. See the plain-text tool result.");
  }
}
