const pa11y = require("pa11y");

const url = process.argv[2];
if (!url) {
  console.error("A URL is required.");
  process.exit(2);
}

pa11y(url, {
  standard: process.env.PA11Y_STANDARD || "WCAG2AA",
  timeout: Number(process.env.PA11Y_TIMEOUT_MS || 60000),
  wait: Number(process.env.PA11Y_WAIT_MS || 500),
  chromeLaunchConfig: {
    executablePath: process.env.CHROME_PATH || "/usr/bin/chromium",
    args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
  }
})
  .then((result) => process.stdout.write(JSON.stringify(result)))
  .catch((error) => {
    console.error(error && error.stack ? error.stack : String(error));
    process.exit(1);
  });
