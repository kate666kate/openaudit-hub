const puppeteer = require("puppeteer");

const [url, outputPrefix, encodedSelectors] = process.argv.slice(2);
if (!url || !outputPrefix || !encodedSelectors) {
  process.stderr.write("URL, output prefix, and selectors are required.\n");
  process.exit(2);
}

async function main() {
  const selectors = JSON.parse(Buffer.from(encodedSelectors, "base64url").toString("utf8")).slice(0, 5);
  const browser = await puppeteer.launch({
    executablePath: process.env.CHROME_PATH || "/usr/bin/chromium",
    headless: true,
    args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
  });
  const results = [];
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1280, height: 800, deviceScaleFactor: 1 });
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 30000 });
    await new Promise((resolve) => setTimeout(resolve, 500));

    for (let index = 0; index < selectors.length; index += 1) {
      const selector = String(selectors[index] || "").trim();
      if (!selector) continue;
      const visual = await page.evaluate((target) => {
        let element;
        try {
          element = document.querySelector(target);
        } catch (_) {
          return null;
        }
        if (!element) return null;
        document.querySelectorAll("[data-openaudit-highlight]").forEach((node) => {
          node.style.outline = node.dataset.openauditOutline || "";
          node.style.boxShadow = node.dataset.openauditShadow || "";
          node.removeAttribute("data-openaudit-highlight");
          node.removeAttribute("data-openaudit-outline");
          node.removeAttribute("data-openaudit-shadow");
        });
        element.dataset.openauditHighlight = "true";
        element.dataset.openauditOutline = element.style.outline || "";
        element.dataset.openauditShadow = element.style.boxShadow || "";
        element.style.outline = "5px solid #e11d48";
        element.style.outlineOffset = "4px";
        element.style.boxShadow = "0 0 0 10px rgba(225,29,72,.22)";
        element.scrollIntoView({ block: "center", inline: "center" });
        const rect = element.getBoundingClientRect();
        return { x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height) };
      }, selector);
      if (!visual) continue;
      await new Promise((resolve) => setTimeout(resolve, 150));
      const screenshot = `${outputPrefix}-${index + 1}.png`;
      await page.screenshot({ path: screenshot, fullPage: false });
      results.push({ selector, screenshot, rect: visual });
    }
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify(results));
}

main().catch((error) => {
  process.stderr.write(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
