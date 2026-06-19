const fs = require("fs");
const {spawnSync} = require("child_process");

const urlsPath = "/config/urls.txt";
const reportsDir = "/reports";
const raw = fs.readFileSync(urlsPath, "utf8");
const urls = raw
  .split(/\r?\n/)
  .flatMap((line) => line.split(","))
  .map((line) => line.trim())
  .filter((line) => line && !line.startsWith("#"));

if (!urls.length) {
  console.error("No URLs found in /config/urls.txt");
  process.exit(1);
}

fs.mkdirSync(reportsDir, {recursive: true});

const preset = (process.env.LIGHTHOUSE_SETTINGS_PRESET || "desktop").toLowerCase();
const stamp = new Date().toISOString().replace(/[:.]/g, "-");

for (const url of urls) {
  const safeName = url
    .replace(/^https?:\/\//, "")
    .replace(/[^a-z0-9]+/gi, "-")
    .replace(/^-|-$/g, "")
    .toLowerCase();
  const outputPath = `${reportsDir}/lighthouse-${safeName}-${stamp}`;
  const args = [
    "lighthouse",
    url,
    "--output=html",
    "--output=json",
    `--output-path=${outputPath}`,
    "--chrome-flags=--headless=new --no-sandbox --disable-dev-shm-usage"
  ];

  if (preset === "desktop") {
    args.push("--preset=desktop");
  }

  console.log(`Running Lighthouse report for ${url}`);
  const result = spawnSync("npx", args, {stdio: "inherit"});
  if (result.status !== 0) {
    process.exit(result.status || 1);
  }
}

console.log("Lighthouse HTML and JSON reports written to /reports");
