import { build } from "esbuild";
import { mkdir, readFile, writeFile } from "node:fs/promises";

const { outputFiles } = await build({
  entryPoints: [new URL("./main.mjs", import.meta.url).pathname],
  bundle: true,
  write: false,
  format: "iife",
  platform: "browser",
  target: "es2022",
  minify: true,
  legalComments: "inline",
});
const template = await readFile(new URL("./template.html", import.meta.url), "utf8");
const script = outputFiles[0].text
  .replace(/^[\t ]+$/gm, "")
  .replace(/<\/script/gi, "<\\/script");
const html = template.replace("<script data-bundle></script>", () => `<script>${script}</script>`);
const directory = new URL("../../assets/openai/", import.meta.url);
await mkdir(directory, { recursive: true });
await writeFile(new URL("read-only.html", directory), html);
const licenses = [];
for (const name of ["@modelcontextprotocol/ext-apps", "@modelcontextprotocol/client", "@modelcontextprotocol/core", "zod"]) {
  const license = await readFile(new URL(`../../node_modules/${name}/LICENSE`, import.meta.url), "utf8");
  licenses.push(`${name}\n${license}`);
}
await writeFile(new URL("THIRD_PARTY_LICENSES.txt", directory), `Upstream code bundled and minified for the browser; license notices follow.\n\n${licenses.join("\n\n")}`);
