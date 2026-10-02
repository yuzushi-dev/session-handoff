import { App } from "@modelcontextprotocol/ext-apps/app-with-deps";
import { mountView } from "./view.mjs";

const app = new App({ name: "Session handoff · Codex thread status", version: "1.0.0" }, {});
void mountView(app, document);
