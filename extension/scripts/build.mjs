import { cp, mkdir, rm } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const dist = join(root, "dist");
const src = join(root, "src");

await rm(join(dist, "manifest.json"), { force: true });
await mkdir(dist, { recursive: true });

for (const file of ["manifest.json", "popup.html", "options.html", "styles.css"]) {
  await cp(join(src, file), join(dist, file));
}

await cp(join(src, "assets"), join(dist, "assets"), { recursive: true });
