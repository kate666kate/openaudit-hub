const fs = require("fs");
const {spawnSync} = require("child_process");
const crypto = require("crypto");

const urlsPath = "/config/urls.txt";
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

const placeholderUrls = urls.filter((url) =>
  /^https?:\/\/example\.(gov\.au|com|org)(\/|$)/i.test(url)
);

if (placeholderUrls.length) {
  console.error(
    "Placeholder URLs detected in /config/urls.txt. Replace them with real pages before running Lighthouse CI."
  );
  process.exit(1);
}

const numberOfRuns = process.env.LHCI_NUMBER_OF_RUNS || "3";
const preset = (process.env.LHCI_SETTINGS_PRESET || "desktop").toLowerCase();
const chromePath = process.env.CHROME_PATH || "/usr/bin/chromium";

process.env.LHCI_BUILD_CONTEXT__CURRENT_HASH = generateHash();
process.env.LHCI_BUILD_CONTEXT__COMMIT_TIME = new Date().toISOString();
process.env.LHCI_BUILD_CONTEXT__CURRENT_BRANCH =
  process.env.LHCI_BUILD_CONTEXT__CURRENT_BRANCH || "main";
process.env.LHCI_BUILD_CONTEXT__COMMIT_MESSAGE =
  process.env.LHCI_BUILD_CONTEXT__COMMIT_MESSAGE || "OpenAudit scheduled Lighthouse run";
process.env.LHCI_BUILD_CONTEXT__AUTHOR =
  process.env.LHCI_BUILD_CONTEXT__AUTHOR || "OpenAudit <openaudit@local>";
process.env.LHCI_BUILD_CONTEXT__AVATAR_URL =
  process.env.LHCI_BUILD_CONTEXT__AVATAR_URL ||
  "https://www.gravatar.com/avatar/00000000000000000000000000000000.jpg?d=identicon";

const collectArgs = [
  "collect",
  `--numberOfRuns=${numberOfRuns}`,
  `--chromePath=${chromePath}`,
  "--settings.chromeFlags=--no-sandbox --disable-dev-shm-usage"
];

if (preset === "desktop") {
  collectArgs.push("--settings.preset=desktop");
}

for (const url of urls) {
  collectArgs.push(`--url=${url}`);
}

const collect = spawnSync("npx", ["lhci", ...collectArgs], {stdio: "inherit"});
if (collect.status !== 0) {
  process.exit(collect.status || 1);
}

const uploadArgs = [
  "upload",
  "--target=lhci",
  `--serverBaseUrl=${process.env.LHCI_SERVER_BASE_URL}`,
  `--token=${process.env.LHCI_BUILD_TOKEN}`
];

if (process.env.LHCI_BASIC_AUTH_USERNAME) {
  uploadArgs.push(`--basicAuth.username=${process.env.LHCI_BASIC_AUTH_USERNAME}`);
  uploadArgs.push(`--basicAuth.password=${process.env.LHCI_BASIC_AUTH_PASSWORD || ""}`);
}

const upload = spawnSync("npx", ["lhci", ...uploadArgs], {stdio: "inherit"});
process.exit(upload.status || 0);

function generateHash() {
  return crypto
    .createHash("sha1")
    .update(`${new Date().toISOString()}-${crypto.randomUUID()}`)
    .digest("hex");
}
