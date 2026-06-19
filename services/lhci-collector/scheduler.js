const {spawnSync} = require("child_process");

const intervalMinutes = Number(process.env.LHCI_SCHEDULE_INTERVAL_MINUTES || "1440");
const runOnStart = (process.env.LHCI_SCHEDULE_RUN_ON_START || "true").toLowerCase() === "true";
const intervalMs = Math.max(intervalMinutes, 1) * 60 * 1000;

function runOnce() {
  const startedAt = new Date().toISOString();
  console.log(`[scheduler] Starting Lighthouse run at ${startedAt}`);
  const result = spawnSync("node", ["/app/run-lhci.js"], {stdio: "inherit"});
  const endedAt = new Date().toISOString();
  console.log(`[scheduler] Lighthouse run finished at ${endedAt} with exit code ${result.status ?? 1}`);
}

if (runOnStart) {
  runOnce();
}

setInterval(runOnce, intervalMs);
console.log(`[scheduler] Scheduled Lighthouse every ${intervalMinutes} minute(s)`);
