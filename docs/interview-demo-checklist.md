# OpenAudit Hub Interview Demo Checklist

Use this checklist when presenting OpenAudit Hub from a laptop.

## Before the interview

- Clone the repository and run `docker compose up -d --build` once before the interview.
- Open `http://localhost:9090` and confirm the dashboard loads.
- Keep one scanned public website ready, preferably one where you understand the issues.
- Keep a static GitHub Pages demo or screenshots ready as a fallback.
- Do not show `.env`, raw Search Console CSV exports, customer IDs, analytics IDs, or private reports.

## Five-minute demo route

1. Open **Dashboard** and explain the idea: one open-source hub for website governance.
2. Open **Websites** and show how multiple sites can be managed from the registry.
3. Open **Scans** and explain the scan types: Full, Accessibility, Content, and Lighthouse.
4. Open **SEO Advanced > Keyword suggestions** and show the first recommended content edit.
5. Open **Accessibility > Issues** and explain one issue using evidence, owner, and remediation steps.
6. Open **Quality Assurance > Broken links** and show how broken URLs become an actionable fix list.
7. Use the page search box to inspect a crawled page and show page-scoped findings.

## Short positioning line

OpenAudit Hub is an open-source website governance dashboard inspired by Siteimprove. It combines Lighthouse, Pa11y, crawl inventory, broken-link checks, content quality checks, keyword analysis, and issue workflow into one Docker-based local tool.

## How to connect it to ecommerce work

- Shopify content and products: use the crawl inventory and content checks to find weak product pages, missing metadata, broken links, and thin content.
- CRO and analytics: use GA4/GTM to measure whether fixes improve add-to-cart, checkout, form submissions, phone calls, or quote requests.
- Campaigns: use Search Console CSV and keyword recommendations to improve landing pages for real queries.
- Team workflow: assign issues to content, SEO, development, or marketing owners and track them through resolution.

## If something fails live

- If Docker is slow, show the static demo or screenshots first.
- If a scan is still running, explain that scans are queued in the background and show previous results.
- If there is no Search Console data, explain that YAKE extracts current page topics locally, while Search Console CSV adds real search demand.
- If asked about production readiness, mention future work: authentication, async scaling, Search Console OAuth, CMS connectors, and richer history dashboards.
