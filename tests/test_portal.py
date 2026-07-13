from __future__ import annotations

import importlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PORTAL_DIR = ROOT / "services" / "portal"
sys.path.insert(0, str(PORTAL_DIR))

_temp_dir = Path(tempfile.mkdtemp(prefix="openaudit-portal-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_temp_dir / 'portal.db'}"
os.environ["ALLOW_PRIVATE_TARGETS"] = "false"
app_module = importlib.import_module("app")
database = importlib.import_module("database")
SCAN_WORKER_DIR = ROOT / "services" / "scan-worker"
sys.path.insert(0, str(SCAN_WORKER_DIR))
crawler_module = importlib.import_module("crawler")
content_quality_module = importlib.import_module("content_quality")
visual_evidence_module = importlib.import_module("visual_evidence")
sampling_module = importlib.import_module("sampling")
budgets_module = importlib.import_module("budgets")


class PortalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = app_module.create_app()
        cls.app.config.update(TESTING=True)
        cls.client = cls.app.test_client()

    def test_liveness(self) -> None:
        response = self.client.get("/health/live")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ok")

    def test_readiness(self) -> None:
        response = self.client.get("/health/ready")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["database"])

    def test_scan_type_validation_includes_content(self) -> None:
        self.assertEqual(app_module.validated_scan_type("content"), "content")
        with self.assertRaisesRegex(ValueError, "Invalid scan type"):
            app_module.validated_scan_type("unknown-tool")

    def test_main_operational_pages_render(self) -> None:
        for path in ("/", "/websites", "/scans", "/modules/issues", "/modules/activity-plans", "/modules/content-optimization", "/modules/duplicate-content", "/modules/keyword-suggestions", "/modules/performance-budgets", "/modules/robots-indexing", "/modules/structured-data", "/modules/security-headers", "/modules/sitemaps", "/modules/ecommerce-readiness", "/modules/conversion-tracking"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_navigation_search_is_interactive(self) -> None:
        response = self.client.get("/")
        html = response.get_data(as_text=True)
        self.assertIn('id="si-menu-search" type="search"', html)
        self.assertIn('placeholder="Search tools and pages"', html)
        self.assertIn("si-page-search-submit", html)
        self.assertIn('<script src="/static/app.js" defer></script>', html)

    def test_navigation_has_accessible_responsive_drawer(self) -> None:
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('aria-controls="si-navigation-drawer"', html)
        self.assertIn('aria-expanded="false"', html)
        self.assertIn('id="si-navigation-drawer"', html)

    def test_deep_crawl_extracts_technical_signals_and_actionable_issues(self) -> None:
        html = """<html><head>
        <link rel="canonical" href="https://other.example/page">
        <meta name="robots" content="noindex">
        <meta property="og:title" content="Example">
        <script type="application/ld+json">{broken json</script>
        </head><body><h1>Page</h1></body></html>"""
        response = mock.Mock(
            url="https://example.com/page",
            headers={"content-type": "text/html", "x-content-type-options": "nosniff"},
        )
        soup = crawler_module.BeautifulSoup(html, "html.parser")
        data = crawler_module.extract_technical_data(soup, response)
        self.assertFalse(data["indexable"])
        self.assertTrue(data["canonical_cross_domain"])
        self.assertEqual(data["invalid_json_ld"], 1)

        issues = content_quality_module.extract_technical_issues({
            "url": response.url, "source": "sitemap", "technical_data": data,
        })
        issue_ids = {issue["id"] for issue in issues}
        self.assertIn("technical-sitemap-noindex", issue_ids)
        self.assertIn("technical-invalid-json-ld", issue_ids)
        self.assertIn("security-missing-csp", issue_ids)

    def test_sitemaps_summary_uses_sitemap_report_without_lighthouse_json(self) -> None:
        report = {
            "kind": "openaudit-sitemap-crawl",
            "site": "https://truvisionled.com.au/",
            "site_key": "truvisionled-com-au",
            "generated_at": "2026-07-13T00:00:00+00:00",
            "sitemap_entry": "https://truvisionled.com.au/sitemap.xml",
            "sitemaps": ["https://truvisionled.com.au/sitemap.xml"],
            "urls": [
                {
                    "loc": "https://truvisionled.com.au/products/lightbar",
                    "lastmod": "2026-07-01",
                    "changefreq": "daily",
                    "priority": "",
                }
            ],
            "url_count": 1,
            "sitemap_count": 1,
            "error_count": 0,
            "errors": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "sitemap-truvisionled-com-au-2026-07-13T00-00-00Z.json"
            report_path.write_text(app_module.json.dumps(report), encoding="utf-8")
            with mock.patch.object(app_module, "REPORTS_DIR", Path(directory)):
                summary = app_module.load_lighthouse_report_summary(
                    "https://truvisionled.com.au/",
                    include_keywords=False,
                    crawl_pages=[
                        {
                            "url": "https://truvisionled.com.au/",
                            "source": "homepage",
                            "content_type": "text/html",
                        }
                    ],
                )
        sitemaps = summary["crawler_summary"]["sitemaps"]
        self.assertEqual(sitemaps["status"], "Report available")
        self.assertEqual(sitemaps["url_count"], "1")
        self.assertEqual(sitemaps["source_label"], "XML sitemap report")
        self.assertEqual(sitemaps["sitemap_files"][0]["kind"], "Index")
        self.assertEqual(sitemaps["sample_pages"][0]["loc"], "https://truvisionled.com.au/products/lightbar")

    def test_broken_links_summary_uses_crawl_inventory_without_linkchecker_report(self) -> None:
        summary = app_module.build_link_integrity_summary(
            [],
            [
                {
                    "url": "https://example.com/missing",
                    "status_code": 404,
                    "source": "sitemap",
                },
                {
                    "url": "https://example.com/ok",
                    "status_code": 200,
                    "source": "internal-link",
                },
            ],
        )
        self.assertEqual(summary["status"], "Needs fixes")
        self.assertEqual(summary["source_label"], "Crawl inventory")
        self.assertEqual(summary["error_count"], "1")
        self.assertEqual(summary["inventory_findings"][0]["status"], "404")
        self.assertIn("301 redirect", summary["inventory_findings"][0]["action"])
        self.assertGreaterEqual(len(summary["inventory_findings"][0]["fix_steps"]), 3)

    def test_broken_links_page_has_clickable_inventory_actions(self) -> None:
        website = database.ensure_website("https://clicklinks.example.com", "Clickable links")
        database.replace_crawl_pages(website["key"], [
            {
                "url": "https://clicklinks.example.com/missing",
                "title": "Missing page",
                "status_code": 404,
                "depth": 1,
                "source": "sitemap",
                "content_type": "text/html",
            }
        ])
        response = self.client.get(f"/modules/broken-links?site={website['key']}")
        html = response.get_data(as_text=True)
        self.assertIn('href="https://clicklinks.example.com/missing"', html)
        self.assertIn(">Inspect</a>", html)
        self.assertIn("data-copy-text=\"https://clicklinks.example.com/missing\"", html)
        self.assertIn("Copy fix steps", html)
        self.assertIn("How to fix this broken URL", html)

    def test_dashboard_shows_operations_center(self) -> None:
        response = self.client.get("/")
        html = response.get_data(as_text=True)
        self.assertIn("Operations center", html)
        self.assertIn("Website registry", html)
        self.assertIn("Start or monitor scans", html)

    def test_websites_page_shows_registry_guidance(self) -> None:
        response = self.client.get("/websites")
        html = response.get_data(as_text=True)
        self.assertIn("Website registry", html)
        self.assertIn("Run first scan", html)
        self.assertIn("Excluded paths", html)

    def test_scans_page_shows_scan_control_center(self) -> None:
        response = self.client.get("/scans")
        html = response.get_data(as_text=True)
        self.assertIn("Scan control center", html)
        self.assertIn("When to run full audit", html)
        self.assertIn("What happens next", html)

    def test_page_search_api_is_scoped_to_selected_website(self) -> None:
        website = database.ensure_website("https://search.example.com", "Search test")
        database.replace_crawl_pages(website["key"], [{
            "url": "https://search.example.com/services",
            "title": "Services",
            "status_code": 200,
            "depth": 1,
            "source": "internal-link",
            "content_type": "text/html",
        }])

        response = self.client.get(f"/api/crawl-pages?site={website['key']}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()[0]["title"], "Services")

    def test_ecommerce_readiness_maps_shopify_pages_to_operations(self) -> None:
        website = database.ensure_website("https://coffee.example.com", "Coffee shop")
        database.replace_crawl_pages(website["key"], [
            {
                "url": "https://coffee.example.com/products/espresso-blend",
                "title": "Espresso Blend",
                "meta_description": "",
                "word_count": 90,
                "h1_count": 1,
                "status_code": 200,
                "depth": 1,
                "source": "sitemap",
                "content_type": "text/html",
                "technical_data": {"structured_data_types": [], "open_graph": {}},
            },
            {
                "url": "https://coffee.example.com/collections/coffee-beans",
                "title": "Coffee Beans",
                "meta_description": "Shop coffee beans.",
                "word_count": 220,
                "h1_count": 1,
                "status_code": 200,
                "depth": 1,
                "source": "sitemap",
                "content_type": "text/html",
                "technical_data": {"structured_data_types": ["CollectionPage"], "open_graph": {"title": "Coffee Beans"}},
            },
            {
                "url": "https://coffee.example.com/pages/coffee-subscription",
                "title": "Coffee Subscription",
                "meta_description": "Subscribe for recurring coffee delivery.",
                "word_count": 300,
                "h1_count": 1,
                "status_code": 200,
                "depth": 1,
                "source": "internal-link",
                "content_type": "text/html",
            },
        ])

        summary = app_module.build_ecommerce_summary(database.list_crawl_pages(website["key"]), [])
        self.assertEqual(summary["product_pages"], "1")
        self.assertEqual(summary["collection_pages"], "1")
        self.assertEqual(summary["subscription_pages"], "1")
        self.assertTrue(any("Product schema" in item["finding"] for item in summary["opportunities"]))

        html = self.client.get(f"/modules/ecommerce-readiness?site={website['key']}").get_data(as_text=True)
        self.assertIn("Shopify Plus eCommerce operations", html)
        self.assertIn("Product and CRO action queue", html)
        self.assertIn("Espresso Blend", html)

        tracking_html = self.client.get(f"/modules/conversion-tracking?site={website['key']}").get_data(as_text=True)
        self.assertIn("GA4 / GTM event map", tracking_html)
        self.assertIn("add_to_cart", tracking_html)
        self.assertIn("subscribe", tracking_html)

    def test_page_inspector_shows_only_selected_website_evidence(self) -> None:
        website = database.ensure_website("https://inspector.example.com", "Inspector test")
        page_url = "https://inspector.example.com/contact"
        database.replace_crawl_pages(website["key"], [{
            "url": page_url,
            "title": "Contact us",
            "status_code": 200,
            "depth": 1,
            "source": "sitemap",
            "content_type": "text/html",
        }])
        database.reconcile_issues(website["key"], [{
            "id": "image-alt",
            "title": "Images must have alternate text",
            "source": "pa11y",
            "category": "Accessibility",
            "points": 3,
            "affected_examples": [{
                "page_url": page_url,
                "selector": "main img.hero",
                "snippet": '<img class="hero">',
                "explanation": "Add meaningful alternative text.",
                "screenshot_path": "evidence-sample.png",
                "highlight": '{"x":10,"y":20,"width":200,"height":80}',
            }],
        }], "pa11y.json", scanned_sources={"pa11y"})

        response = self.client.get(f"/pages/inspect?site={website['key']}&url={page_url}")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Contact us", html)
        self.assertIn("Images must have alternate text", html)
        self.assertIn("main img.hero", html)

        other = database.ensure_website("https://other-inspector.example.com", "Other inspector")
        cross_site = self.client.get(f"/pages/inspect?site={other['key']}&url={page_url}")
        self.assertEqual(cross_site.status_code, 404)

    def test_content_quality_module_uses_managed_content_findings(self) -> None:
        website = database.ensure_website("https://content.example.com", "Content test")
        page_url = "https://content.example.com/about"
        database.replace_crawl_pages(website["key"], [{
            "url": page_url,
            "title": "About",
            "meta_description": "",
            "language": "en-AU",
            "word_count": 75,
            "h1_count": 1,
            "status_code": 200,
            "depth": 1,
            "source": "sitemap",
            "content_type": "text/html",
        }])
        database.reconcile_issues(website["key"], [{
            "id": "content-thin-page",
            "title": "Page may have thin content",
            "source": "content",
            "category": "Content Quality",
            "difficulty": "Medium",
            "responsibility": "Content / SEO",
            "points": 1.5,
            "affected_examples": [{"page_url": page_url, "selector": "main", "snippet": "75 words", "explanation": "Expand useful content."}],
        }], "content.json", scanned_sources={"content"})

        response = self.client.get(f"/modules/content-quality?site={website['key']}")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Content quality score", html)
        self.assertIn("Page may have thin content", html)
        self.assertIn(page_url, html)

    def test_issue_detail_shows_context_and_affected_pages(self) -> None:
        website = database.ensure_website("https://issue-detail.example.com", "Issue detail test")
        page_url = "https://issue-detail.example.com/services"
        database.replace_crawl_pages(website["key"], [{
            "url": page_url,
            "title": "Services",
            "status_code": 200,
            "depth": 1,
            "source": "internal-link",
            "content_type": "text/html",
        }])
        database.reconcile_issues(website["key"], [{
            "id": "image-alt",
            "title": "Images must have alternate text",
            "source": "pa11y",
            "category": "Accessibility",
            "recommendation": "Add descriptive alt text to meaningful images.",
            "difficulty": "Medium",
            "responsibility": "Content / Development",
            "points": 3,
            "occurrences": 2,
            "pages": 1,
            "fix_guidance": {
                "owner": "Content / Development",
                "priority": "Medium",
                "steps": [
                    "Open the image component or CMS field that produced the missing alt text.",
                    "Write alt text that explains the purpose of the image.",
                ],
                "handoff_note": "Content can draft the wording and development can apply it in templates if needed.",
                "success_signal": "The next scan no longer reports missing alt text.",
                "what_to_change": ["Add meaningful alt text to each affected image."],
                "why_it_matters": "Screen readers need alt text to describe meaningful images.",
                "where_to_change": [{"place": "Image field or template", "detail": "Update the HTML output or media metadata."}],
                "validation": ["The next scan no longer reports the missing alt issue."],
                "acceptance_criteria": ["Each meaningful image includes accurate alt text."],
                "code_hint": "Update the image output in the CMS or template.",
            },
            "affected_examples": [{
                "page_url": page_url,
                "selector": "main img.hero",
                "snippet": '<img class="hero">',
                "explanation": "Add meaningful alternative text.",
                "screenshot_path": "evidence-sample.png",
                "highlight": '{"x":10,"y":20,"width":200,"height":80}',
            }],
        }], "pa11y.json", scanned_sources={"pa11y"})

        response = self.client.get(f"/issues/image-alt?site={website['key']}")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Likely owner", html)
        self.assertIn("Pages most likely affected right now", html)
        self.assertIn(page_url, html)
        self.assertIn("How to close this issue", html)
        self.assertIn('/reports/evidence-sample.png', html)
        saved_issue = database.list_issues(website["key"])[0]
        self.assertEqual(saved_issue["evidence"][0]["screenshot_path"], "evidence-sample.png")

    def test_worker_attaches_visual_evidence_by_selector(self) -> None:
        issues = [{"affected_examples": [
            {"selector": "main img.hero", "page_url": "https://visual.example.com"},
            {"selector": "button.buy", "page_url": "https://visual.example.com"},
        ]}]
        completed = mock.Mock(
            returncode=0,
            stdout='[{"selector":"main img.hero","screenshot":"/reports/evidence-one.png","rect":{"x":12,"y":30,"width":240,"height":90}}]',
            stderr="",
        )
        with mock.patch.object(visual_evidence_module.subprocess, "run", return_value=completed):
            visual_evidence_module.attach_visual_evidence(Path("/reports"), "visual-example", "https://visual.example.com", "pa11y-1", issues)
        first, second = issues[0]["affected_examples"]
        self.assertEqual(first["screenshot_path"], "evidence-one.png")
        self.assertIn('"width":240', first["highlight"])
        self.assertNotIn("screenshot_path", second)

    def test_lighthouse_sampling_covers_distinct_site_sections(self) -> None:
        pages = [
            {"url": "https://example.com/", "depth": 0, "source": "homepage"},
            {"url": "https://example.com/products/light-one", "depth": 2, "source": "internal-link"},
            {"url": "https://example.com/products/light-two", "depth": 2, "source": "internal-link"},
            {"url": "https://example.com/blog/install-guide", "depth": 2, "source": "sitemap"},
            {"url": "https://example.com/contact", "depth": 1, "source": "internal-link"},
        ]
        selected = sampling_module.select_lighthouse_candidates(pages, 4)
        self.assertEqual(len(selected), 4)
        self.assertEqual(
            {page["sample_group"] for page in selected},
            {"/", "/products/:page", "/blog/:page", "/contact"},
        )

    def test_lighthouse_sampling_round_robins_when_capacity_remains(self) -> None:
        pages = [
            {"url": "https://example.com/", "depth": 0, "source": "homepage"},
            {"url": "https://example.com/products/a", "depth": 2},
            {"url": "https://example.com/products/b", "depth": 2},
            {"url": "https://example.com/products/c", "depth": 2},
        ]
        selected = sampling_module.select_lighthouse_candidates(pages, 3)
        self.assertEqual([page["url"] for page in selected], [
            "https://example.com/", "https://example.com/products/a", "https://example.com/products/b",
        ])

    def test_lighthouse_budgets_create_measured_violations(self) -> None:
        data = {
            "categories": {
                "performance": {"score": 0.62}, "accessibility": {"score": 0.91}, "seo": {"score": 0.74},
            },
            "audits": {
                "largest-contentful-paint": {"numericValue": 4200},
                "cumulative-layout-shift": {"numericValue": 0.04},
            },
        }
        website = {
            "budget_performance": 70, "budget_accessibility": 80, "budget_seo": 80,
            "budget_lcp_ms": 2500, "budget_cls": 0.1,
        }
        issues = budgets_module.evaluate_lighthouse_budgets(data, "https://example.com/products/a", website)
        self.assertEqual({issue["id"] for issue in issues}, {
            "budget-performance", "budget-seo", "budget-largest-contentful-paint",
        })
        self.assertTrue(all(issue["source"] == "budget" for issue in issues))
        self.assertIn("Current LCP: 4200ms", issues[2]["affected_examples"][0]["explanation"])
        guidance = app_module.build_managed_issue_fix_guidance({
            **issues[2], "audit_id": issues[2]["id"], "owner": "Development",
        })
        self.assertIn("LCP element", guidance["what_to_change"][0])
        self.assertIn("configured millisecond limit", guidance["success_signal"])

    def test_website_quality_budgets_are_saved_and_bounded(self) -> None:
        website = database.create_website({
            "base_url": "https://budget-settings.example.com", "name": "Budget settings",
            "budget_performance": 110, "budget_accessibility": -2, "budget_seo": 85,
            "budget_lcp_ms": 300, "budget_cls": 8,
        })
        self.assertEqual(website["budget_performance"], 100)
        self.assertEqual(website["budget_accessibility"], 0)
        self.assertEqual(website["budget_seo"], 85)
        self.assertEqual(website["budget_lcp_ms"], 500)
        self.assertEqual(website["budget_cls"], 2.0)

    def test_keyword_brand_name_is_derived_from_site_url(self) -> None:
        self.assertEqual(app_module.seo_brand_name("https://www.truvisionled.com.au"), "Truvisionled")
        self.assertEqual(app_module.seo_brand_name("https://piranhaoffroad.com.au"), "Piranhaoffroad")

    def test_keyword_cannibalization_groups_duplicate_phrases(self) -> None:
        rows = app_module.build_keyword_cannibalization([
            {"suggested_keyword": "led light bars", "page": "https://example.com/a"},
            {"suggested_keyword": "led light bars", "page": "https://example.com/b"},
            {"suggested_keyword": "marker lights", "page": "https://example.com/c"},
        ])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["keyword"], "led light bars")
        self.assertEqual(rows[0]["count"], 2)

    def test_search_console_actions_prioritize_real_queries(self) -> None:
        actions = app_module.build_search_console_actions(
            [
                {
                    "query": "led work lights",
                    "page": "https://example.com/collections/work-lights",
                    "clicks": "12",
                    "impressions": "640",
                    "ctr": "1.9%",
                    "position": "11.4",
                }
            ],
            {
                "https://example.com/collections/work-lights": {
                    "title": "",
                    "meta": "",
                    "h1": "Work Lights",
                    "word_count": "95",
                }
            },
            [],
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["query"], "led work lights")
        self.assertEqual(actions[0]["focus"], "Title tag")
        self.assertIn("real search visibility", actions[0]["why_now"])

    def test_search_console_csv_upload_is_scoped_and_persisted(self) -> None:
        website = database.ensure_website("https://gsc.example.com", "GSC test")
        csv_text = (
            "Query,Page,Clicks,Impressions,CTR,Position\n"
            "work lights,https://gsc.example.com/work-lights,12,640,1.9%,11.4\n"
            "wrong site,https://other.example.com/page,4,90,4.4%,7.2\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(app_module, "SEARCH_CONSOLE_DIRS", [Path(directory)]):
                response = self.client.post(
                    "/api/search-console/import",
                    data={
                        "site": website["key"],
                        "search_console_csv": (io.BytesIO(csv_text.encode("utf-8")), "search-console.csv"),
                    },
                    content_type="multipart/form-data",
                )
                self.assertEqual(response.status_code, 302)
                self.assertIn("gsc=imported", response.headers["Location"])
                self.assertIn("rows=1", response.headers["Location"])
                self.assertIn("skipped=1", response.headers["Location"])
                stored = Path(directory) / f"gsc-{website['key']}.csv"
                self.assertTrue(stored.exists())
                loaded = app_module.load_search_console_rows(website["base_url"])
                self.assertEqual(len(loaded), 1)
                self.assertEqual(loaded[0]["query"], "work lights")

    def test_search_console_pages_csv_upload_infers_page_topic(self) -> None:
        website = database.ensure_website("https://truvisionled.com.au", "Tru Vision")
        csv_text = (
            "Top pages,Clicks,Impressions,CTR,Position\n"
            "https://truvisionled.com.au/products/12-inch-slimline-lightbar-10-30v,4,90,4.4%,7.2\n"
            "https://other.example.com/page,2,30,6.6%,8.1\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(app_module, "SEARCH_CONSOLE_DIRS", [Path(directory)]):
                response = self.client.post(
                    "/api/search-console/import",
                    data={
                        "site": website["key"],
                        "search_console_csv": (io.BytesIO(csv_text.encode("utf-8")), "Pages.csv"),
                    },
                    content_type="multipart/form-data",
                )
                self.assertEqual(response.status_code, 302)
                self.assertIn("gsc=imported", response.headers["Location"])
                self.assertIn("rows=1", response.headers["Location"])
                loaded = app_module.load_search_console_rows(website["base_url"])
                self.assertEqual(len(loaded), 1)
                self.assertEqual(loaded[0]["query"], "12 inch slimline lightbar 10 30v")
                self.assertEqual(loaded[0]["source_type"], "page")

    def test_search_console_csv_upload_requires_query_or_page_column(self) -> None:
        website = database.ensure_website("https://gsc-invalid.example.com", "Invalid GSC")
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(app_module, "SEARCH_CONSOLE_DIRS", [Path(directory)]):
                response = self.client.post(
                    "/api/search-console/import",
                    data={
                        "site": website["key"],
                        "search_console_csv": (io.BytesIO(b"Country,Clicks\nAustralia,2\n"), "bad.csv"),
                    },
                    content_type="multipart/form-data",
                )
                self.assertEqual(response.status_code, 302)
                self.assertIn("gsc=error", response.headers["Location"])
                self.assertFalse((Path(directory) / f"gsc-{website['key']}.csv").exists())

    def test_keyword_action_workflow_persists_and_enriches_recommendation(self) -> None:
        website = database.ensure_website("https://workflow.example.com", "Workflow test")
        page_url = "https://workflow.example.com/products/work-lights"
        action = database.upsert_keyword_action(
            website["key"],
            page_url,
            "LED work lights",
            "Improve existing page",
            "in_progress",
            "SEO team",
            "Rewrite the title and intro.",
        )
        self.assertEqual(action["status"], "in_progress")
        recommendation = [{"page": page_url, "keyword": "LED work lights"}]
        summary = app_module.apply_keyword_action_states(website["key"], recommendation)
        self.assertEqual(recommendation[0]["workflow"]["owner"], "SEO team")
        self.assertEqual(recommendation[0]["workflow"]["status"], "in_progress")
        self.assertEqual(summary["in_progress"], 1)

    def test_keyword_action_api_rejects_another_website_page(self) -> None:
        website = database.ensure_website("https://workflow-scope.example.com", "Workflow scope")
        response = self.client.post(
            "/api/keyword-actions",
            json={
                "site": website["key"],
                "page_url": "https://other.example.com/page",
                "keyword": "work lights",
                "decision": "Improve existing page",
                "status": "accepted",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("selected website", response.get_json()["error"])

    def test_keyword_action_api_marks_task_completed(self) -> None:
        website = database.ensure_website("https://workflow-api.example.com", "Workflow API")
        response = self.client.post(
            "/api/keyword-actions",
            json={
                "site": website["key"],
                "page_url": "https://workflow-api.example.com/work-lights",
                "keyword": "LED work lights",
                "decision": "Improve existing page",
                "status": "completed",
                "owner": "Content team",
                "note": "Published and ready for validation.",
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["status"], "completed")
        self.assertTrue(payload["completed_at"])

    def test_activity_plan_center_combines_keywords_and_issues(self) -> None:
        center = app_module.build_activity_plan_center(
            [{
                "keyword": "LED work lights",
                "page": "https://example.com/work-lights",
                "decision": "Improve existing page",
                "decision_label": "Improve existing page",
                "priority": "High",
                "points": "2.4",
                "workflow": {"status": "in_progress", "owner": "SEO team", "note": "Updating title"},
            }],
            [{
                "id": "issue-1",
                "issue_key": "image-alt",
                "title": "Images need alt text",
                "source": "pa11y",
                "category": "Accessibility",
                "status": "resolved",
                "owner": "Content team",
                "priority": "medium",
                "points": 3,
            }],
            "all",
        )
        self.assertEqual(center["total"], 2)
        self.assertEqual(center["in_progress"], 1)
        self.assertEqual(center["completed"], 1)
        self.assertEqual({row["kind"] for row in center["rows"]}, {"keyword", "issue"})
        self.assertEqual(center["owner_workload"], [{"owner": "SEO team", "count": 1}])
        self.assertEqual({item["key"] for item in center["work_types"]}, {"keyword", "issue"})

    def test_keyword_activity_form_returns_to_activity_center(self) -> None:
        website = database.ensure_website("https://activity-return.example.com", "Activity return")
        response = self.client.post(
            "/api/keyword-actions",
            data={
                "site": website["key"],
                "page_url": "https://activity-return.example.com/work-lights",
                "keyword": "LED work lights",
                "decision": "Improve existing page",
                "status": "accepted",
                "owner": "SEO team",
                "return_to": f"/modules/activity-plans?site={website['key']}&status=todo",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/modules/activity-plans", response.headers["Location"])

    def test_activity_plan_api_returns_selected_site_work(self) -> None:
        website = database.ensure_website("https://activity-api.example.com", "Activity API")
        database.upsert_keyword_action(
            website["key"],
            "https://activity-api.example.com/work-lights",
            "LED work lights",
            "Improve existing page",
            "accepted",
            "SEO team",
        )
        response = self.client.get(f"/api/activity-plans?site={website['key']}&status=all")
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.get_json()["total"], 1)

    def test_content_optimization_identifies_actionable_page_issues(self) -> None:
        pages = [
            {
                "url": "https://example.com/a",
                "title": "Work Lights | Example",
                "meta_description": "A repeated description for work lights and related vehicle products.",
                "word_count": 80,
                "h1_count": 0,
                "status_code": 200,
                "content_type": "text/html",
            },
            {
                "url": "https://example.com/b",
                "title": "Work Lights | Example",
                "meta_description": "A repeated description for work lights and related vehicle products.",
                "word_count": 320,
                "h1_count": 1,
                "status_code": 200,
                "content_type": "text/html",
            },
        ]
        summary = app_module.build_content_optimization_summary(pages)
        self.assertEqual(summary["pages"], 2)
        self.assertEqual(summary["thin_pages"], 1)
        self.assertEqual(summary["needs_attention"], 2)
        first_labels = {issue["label"] for issue in summary["rows"][0]["issues"]}
        self.assertIn("Missing H1", first_labels)
        self.assertIn("Thin content", first_labels)
        self.assertGreater(summary["rows"][0]["points"], 0)
        self.assertIn("Missing H1", summary["rows"][0]["action_note"])

    def test_duplicate_content_groups_exact_titles_and_meta(self) -> None:
        pages = [
            {
                "url": f"https://example.com/{slug}",
                "title": "Shared product title",
                "meta_description": "This exact description is reused across multiple product pages.",
                "word_count": 250,
                "h1_count": 1,
                "status_code": 200,
                "content_type": "text/html",
            }
            for slug in ("one", "two")
        ]
        summary = app_module.build_duplicate_content_summary(pages)
        self.assertEqual(summary["affected_pages"], 2)
        self.assertEqual(summary["duplicate_title_groups"], 1)
        self.assertEqual(summary["duplicate_meta_groups"], 1)
        self.assertEqual(len(summary["groups"]), 2)
        self.assertEqual(summary["groups"][0]["primary_page"]["url"], "https://example.com/one")
        self.assertEqual(len(summary["groups"][0]["duplicate_pages"]), 1)
        self.assertEqual(summary["groups"][0]["similarity"], "100% exact match")

    def test_crawler_content_fingerprints_are_stable_and_private(self) -> None:
        text = " ".join(["reliable automotive LED work light installation guidance"] * 12)
        exact_hash, simhash = crawler_module.content_fingerprints(text)
        repeated_hash, repeated_simhash = crawler_module.content_fingerprints(text)
        self.assertEqual(exact_hash, repeated_hash)
        self.assertEqual(simhash, repeated_simhash)
        self.assertEqual(len(exact_hash), 64)
        self.assertEqual(len(simhash), 16)
        self.assertNotIn("automotive", exact_hash)

    def test_keyword_page_fetch_is_shared_between_extractors(self) -> None:
        class Response:
            text = "<html><head><title>LED lights</title></head><body><h1>Work lights</h1><p>Useful product guidance for drivers and installers.</p></body></html>"
            headers = {"content-type": "text/html"}

            @staticmethod
            def raise_for_status() -> None:
                return None

        app_module._PAGE_HTML_CACHE.clear()
        with mock.patch.object(app_module.requests, "get", return_value=Response()) as get:
            snapshot = app_module.fetch_page_seo_snapshot("https://cache.example.com/page")
            content = app_module.fetch_keyword_source_text("https://cache.example.com/page")
        self.assertEqual(snapshot["title"], "LED lights")
        self.assertIn("Work lights", content)
        self.assertEqual(get.call_count, 1)

    def test_duplicate_content_detects_exact_body_fingerprints(self) -> None:
        pages = [
            {
                "url": f"https://example.com/{slug}",
                "title": f"Unique title {slug}",
                "meta_description": f"A unique description for the {slug} page with enough detail to avoid grouping.",
                "word_count": 240,
                "h1_count": 1,
                "content_hash": "a" * 64,
                "content_simhash": "1a2b3c4d5e6f7788",
                "status_code": 200,
                "content_type": "text/html",
            }
            for slug in ("one", "two")
        ]
        summary = app_module.build_duplicate_content_summary(pages)
        self.assertEqual(summary["exact_body_groups"], 1)
        self.assertEqual(summary["near_body_groups"], 0)
        self.assertEqual(summary["body_similarity_status"], "Live")

    def test_duplicate_content_detects_near_body_fingerprints(self) -> None:
        pages = [
            {
                "url": f"https://example.com/{slug}",
                "title": f"Different title {slug}",
                "meta_description": f"A distinct description for {slug} with enough wording to remain unique.",
                "word_count": 260,
                "h1_count": 1,
                "content_hash": hash_value,
                "content_simhash": simhash,
                "status_code": 200,
                "content_type": "text/html",
            }
            for slug, hash_value, simhash in (
                ("one", "b" * 64, "0000000000000000"),
                ("two", "c" * 64, "0000000000000003"),
            )
        ]
        summary = app_module.build_duplicate_content_summary(pages)
        self.assertEqual(summary["exact_body_groups"], 0)
        self.assertEqual(summary["near_body_groups"], 1)
        self.assertEqual(app_module.simhash_distance("0", "3"), 2)
        self.assertIn("97%", summary["groups"][0]["similarity"])

    def test_content_action_is_persistent_and_appears_in_activity_plan(self) -> None:
        website = database.ensure_website("https://content-plan.example.com", "Content plan")
        action = database.upsert_content_action(
            website["key"], "duplicate-content", "Resolve duplicate product pages",
            "https://content-plan.example.com/products/main",
            ["https://content-plan.example.com/products/main", "https://content-plan.example.com/products/copy"],
            "accepted", "Web team", "Merge overlapping copy.", 7.5,
        )
        saved = database.list_content_actions(website["key"])
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["action_key"], action["action_key"])
        center = app_module.build_activity_plan_center([], [], "all", [], saved)
        self.assertEqual(center["rows"][0]["kind"], "content")
        self.assertEqual(center["rows"][0]["owner"], "Web team")

        optimization = database.upsert_content_action(
            website["key"], "content-optimization", "Improve product content",
            "https://content-plan.example.com/products/main",
            ["https://content-plan.example.com/products/main"], "in_progress", "Content team", "Add title and H1.", 5,
        )
        center = app_module.build_activity_plan_center([], [], "all", [], [optimization])
        self.assertIn("Content optimization", center["rows"][0]["subtitle"])

    def test_content_action_rejects_pages_from_another_website(self) -> None:
        website = database.ensure_website("https://safe-content.example.com", "Safe content")
        response = self.client.post("/api/content-actions", json={
            "site": website["key"], "action_type": "content-optimization",
            "title": "Mixed site task", "primary_url": "https://safe-content.example.com/page",
            "affected_urls": "https://safe-content.example.com/page|https://other.example.org/page",
            "status": "accepted", "points": 5,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("Every affected page", response.get_json()["error"])

    def test_activity_plan_csv_export_escapes_spreadsheet_formulas(self) -> None:
        website = database.ensure_website("https://export-plan.example.com", "Export plan")
        database.upsert_content_action(
            website["key"], "content-optimization", "=HYPERLINK(\"bad\")",
            "https://export-plan.example.com/page", ["https://export-plan.example.com/page"],
            "accepted", "@owner", "+unsafe note", 5,
        )
        response = self.client.get(f"/api/activity-plans/export.csv?site={website['key']}")
        self.assertEqual(response.status_code, 200)
        exported = response.get_data(as_text=True)
        self.assertIn("'=HYPERLINK", exported)
        self.assertIn("'@owner", exported)
        self.assertIn("'+unsafe note", exported)

    def test_content_modules_use_selected_site_crawl_inventory(self) -> None:
        website = database.ensure_website("https://content-modules.example.com", "Content modules")
        database.replace_crawl_pages(website["key"], [
            {
                "url": "https://content-modules.example.com/page-a",
                "title": "Repeated content title",
                "meta_description": "The same sufficiently long description appears on both test pages.",
                "word_count": 90,
                "h1_count": 0,
                "status_code": 200,
                "content_type": "text/html",
            },
            {
                "url": "https://content-modules.example.com/page-b",
                "title": "Repeated content title",
                "meta_description": "The same sufficiently long description appears on both test pages.",
                "word_count": 240,
                "h1_count": 1,
                "status_code": 200,
                "content_type": "text/html",
            },
        ])
        content_response = self.client.get(f"/modules/content-optimization?site={website['key']}")
        duplicate_response = self.client.get(f"/modules/duplicate-content?site={website['key']}")
        self.assertEqual(content_response.status_code, 200)
        self.assertEqual(duplicate_response.status_code, 200)
        self.assertIn("Pages to improve", content_response.get_data(as_text=True))
        self.assertIn("Duplicate groups", duplicate_response.get_data(as_text=True))

    def test_page_edit_queue_builds_current_vs_suggested_tasks(self) -> None:
        queue = app_module.build_page_edit_queue([
            {
                "page": "https://example.com/work-lights",
                "keyword": "LED work lights",
                "priority": "Highest impact",
                "points": "2.6",
                "focus": "Title tag",
                "why_now": "This page has demand and missing core SEO fields.",
                "why_it_matters": "This page already appears in search, so improving the page match can lift clicks.",
                "decision": "Improve existing page",
                "matched_query": "led work lights",
                "current_title": "",
                "current_h1": "Work Lights",
                "current_meta": "",
                "title": "LED work lights | Example",
                "h1": "LED work lights",
                "meta": "Shop LED work lights for trucks and commercial vehicles.",
                "intro": "Add a short intro that explains use cases and fitment.",
                "related_issues": [
                    {
                        "title": "Document doesn't have a meta description",
                        "href": "/issues/meta-description?site=example-com",
                        "points": "1.7",
                        "match": "This page",
                    }
                ],
            }
        ])
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["keyword"], "LED work lights")
        self.assertEqual(queue[0]["tasks"][0]["label"], "Title tag")
        self.assertEqual(queue[0]["tasks"][0]["current"], "Missing")
        self.assertEqual(queue[0]["related_issues"][0]["match"], "This page")
        self.assertEqual(queue[0]["decision"], "Improve existing page")

    def test_keyword_strategy_decision_prefers_improve_existing_page(self) -> None:
        decision = app_module.keyword_strategy_decision(
            "led work lights",
            "https://example.com/products/work-lights",
            {"title": "", "meta": "", "h1": "Work Lights", "word_count": "110"},
            {"query": "led work lights", "impressions": "320", "position": "11.2"},
        )
        self.assertEqual(decision, "Improve existing page")

    def test_keyword_strategy_decision_can_create_supporting_content(self) -> None:
        decision = app_module.keyword_strategy_decision(
            "how to wire led work lights",
            "https://example.com/support/work-lights",
            {"title": "", "meta": "", "h1": "", "word_count": "80"},
            {"query": "", "impressions": "0", "position": ""},
        )
        self.assertEqual(decision, "Create supporting content")

    def test_keyword_decision_summary_can_name_supporting_content_type(self) -> None:
        label = app_module.keyword_decision_summary(
            "how to wire led work lights",
            "https://example.com/support/work-lights",
            {"title": "", "meta": "", "h1": "", "word_count": "80"},
            {"query": "", "impressions": "0", "position": ""},
            [],
        )
        self.assertEqual(label, "Create how-to guide")

    def test_keyword_decision_confidence_marks_crawl_only_evidence_medium(self) -> None:
        confidence = app_module.keyword_decision_confidence(
            "led work lights",
            "https://example.com/work-lights",
            {"title": "LED work lights", "meta": "desc", "h1": "LED work lights", "word_count": "220"},
            {},
            [],
        )
        self.assertEqual(confidence, "Medium confidence")

    def test_keyword_supporting_content_brief_is_actionable(self) -> None:
        brief = app_module.keyword_supporting_content_brief(
            "how to wire led work lights",
            "https://example.com/support/work-lights",
            {},
        )
        self.assertEqual(brief["type"], "Create how-to guide")
        self.assertIn("How to use", brief["title"])
        self.assertGreaterEqual(len(brief["sections"]), 3)
        self.assertIn("Link back", brief["internal_link"])

    def test_keyword_confidence_class_maps_labels(self) -> None:
        self.assertEqual(app_module.keyword_confidence_class("High confidence"), "high")
        self.assertEqual(app_module.keyword_confidence_class("Medium confidence"), "medium")
        self.assertEqual(app_module.keyword_confidence_class("Needs real search data"), "needs-data")

    def test_keyword_edit_brief_explains_title_edits(self) -> None:
        brief = app_module.keyword_edit_brief(
            "led work lights",
            "https://example.com/products/work-lights",
            "Title tag",
            "Improve existing page",
        )
        self.assertEqual(brief["title"], "Rewrite the title first")
        self.assertIn("LED work lights", brief["do"])
        self.assertIn("unique", brief["validation"])

    def test_keyword_meta_description_is_complete_and_copy_ready(self) -> None:
        meta = app_module.seo_meta_example(
            "12 inch slimline LED light bar",
            "product",
            "Truvisionled",
        )
        self.assertNotIn("...", meta)
        self.assertTrue(meta.endswith("."))
        self.assertLessEqual(len(meta), 155)
        self.assertIn("Truvisionled", meta)

        queue = app_module.build_page_edit_queue([
            {
                "page": "https://example.com/products/lightbar",
                "keyword": "12 inch slimline LED light bar",
                "current_meta": "",
                "meta": meta,
                "title": "12 inch slimline LED light bar | Truvisionled",
                "h1": "12 inch slimline LED light bar",
                "intro": "Add one useful paragraph.",
            }
        ])
        meta_task = next(task for task in queue[0]["tasks"] if task["label"] == "Meta description")
        self.assertEqual(meta_task["suggested"], meta)
        self.assertTrue(meta_task["important"])
        self.assertIn("characters", meta_task["metric"])
        self.assertIn("CMS meta description", meta_task["guidance"])

    def test_keyword_candidate_rejects_specs_and_brand_fragments(self) -> None:
        page = "https://truvisionled.com.au/products/12-inch-slimline-lightbar-10-30v"
        self.assertFalse(
            app_module.keyword_candidate_is_useful(
                "30v",
                "12 inch slimline LED light bar",
                page,
                {},
            )
        )
        self.assertFalse(
            app_module.keyword_candidate_is_useful(
                "tru vision led",
                "tru vision LED",
                page,
                {},
            )
        )

    def test_page_specific_keywords_keep_product_differences(self) -> None:
        slimline = app_module.page_specific_keyword_phrase(
            "lightbar",
            "https://example.com/products/12-inch-slimline-lightbar-10-30v",
            {"h1": "12 Inch Slimline Lightbar 10-30V 60W 3400Lm"},
        )
        combo = app_module.page_specific_keyword_phrase(
            "lightbar",
            "https://example.com/products/12-inch-combo-beams-lightbar-10-30v",
            {"h1": "12 Inch Combo Beams Lightbar 10-30V 50W 2950Lm"},
        )
        self.assertEqual(slimline, "12 inch slimline LED light bar")
        self.assertEqual(combo, "12 inch combo beam LED light bar")
        self.assertNotEqual(slimline, combo)

    def test_crawl_only_keyword_confidence_is_not_high(self) -> None:
        confidence = app_module.keyword_decision_confidence(
            "12 inch slimline LED light bar",
            "https://example.com/products/slimline-lightbar",
            {"title": "Slimline Lightbar", "meta": "Description", "h1": "Slimline Lightbar"},
            {},
            [{"title": "Heading order"}, {"title": "Image alt"}],
        )
        self.assertEqual(confidence, "Medium confidence")

    def test_keyword_issue_signals_are_scoped_to_the_page(self) -> None:
        issues = [
            {
                "id": "heading-order",
                "title": "Heading order",
                "affected_examples": [{"page_url": "https://example.com/products/a"}],
            },
            {
                "id": "meta-description",
                "title": "Meta description",
                "affected_examples": [{"page_url": "https://example.com/products/b"}],
            },
        ]
        scoped = app_module.keyword_relevant_issues("https://example.com/products/b", issues)
        self.assertEqual(len(scoped), 1)
        self.assertEqual(scoped[0]["id"], "meta-description")

    def test_related_issue_refs_prioritize_same_page_and_focus(self) -> None:
        issues = [
            {
                "id": "meta-description",
                "title": "Document doesn't have a meta description",
                "category": "SEO",
                "points": 1.7,
                "affected_examples": [
                    {"page_url": "https://example.com/work-lights"}
                ],
            },
            {
                "id": "heading-order",
                "title": "Heading elements are not in sequentially-descending order",
                "category": "SEO",
                "points": 1.2,
                "affected_examples": [
                    {"page_url": "https://example.com/other-page"}
                ],
            },
        ]
        refs = app_module.build_related_issue_refs(
            "https://example.com/work-lights",
            "Meta description",
            issues,
        )
        self.assertEqual(refs[0]["title"], "Document doesn't have a meta description")
        self.assertEqual(refs[0]["match"], "This page")

    def test_related_issue_refs_exclude_unrelated_best_practice_items(self) -> None:
        issues = [
            {
                "id": "deprecations",
                "title": "Uses deprecated APIs",
                "category": "Best Practices",
                "points": 12.5,
                "affected_examples": [{"page_url": "https://example.com/work-lights"}],
            },
            {
                "id": "meta-description",
                "title": "Document doesn't have a meta description",
                "category": "SEO",
                "points": 1.7,
                "affected_examples": [{"page_url": "https://example.com/work-lights"}],
            },
        ]
        refs = app_module.build_related_issue_refs(
            "https://example.com/work-lights",
            "Meta description",
            issues,
        )
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["id"], "meta-description")

    def test_build_keyword_source_urls_prefers_crawl_and_deduplicates(self) -> None:
        urls = app_module.build_keyword_source_urls(
            "https://example.com/",
            [
                "https://example.com/products/a",
                "https://example.com/products/b",
            ],
            [
                "https://example.com/products/b",
                "https://example.com/about",
            ],
        )
        self.assertEqual(
            urls,
            [
                "https://example.com/",
                "https://example.com/products/a",
                "https://example.com/products/b",
                "https://example.com/about",
            ],
        )


if __name__ == "__main__":
    unittest.main()
