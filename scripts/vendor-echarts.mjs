import { copyFile, mkdir, stat } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const source = resolve(root, "node_modules/echarts/dist/echarts.esm.min.js");
const destination = resolve(root, "site/assets/vendor/echarts.esm.min.js");
const licenseSource = resolve(root, "node_modules/echarts/LICENSE");
const noticeSource = resolve(root, "node_modules/echarts/NOTICE");

const sourceInfo = await stat(source).catch(() => null);
if (!sourceInfo?.isFile()) {
  throw new Error("ECharts 6.1.0 is not installed. Run npm install first.");
}

await mkdir(dirname(destination), { recursive: true });
await copyFile(source, destination);
await copyFile(licenseSource, resolve(dirname(destination), "LICENSE.echarts.txt"));
await copyFile(noticeSource, resolve(dirname(destination), "NOTICE.echarts.txt"));
console.log(`Vendored ECharts: ${destination}`);
