from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PORTAL_DIR = ROOT / "services" / "portal"
sys.path.insert(0, str(PORTAL_DIR))

_temp_dir = Path(tempfile.mkdtemp(prefix="openaudit-portal-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_temp_dir / 'portal.db'}"
os.environ["ALLOW_PRIVATE_TARGETS"] = "false"
app_module = importlib.import_module("app")
database = importlib.import_module("database")


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

    def test_main_operational_pages_render(self) -> None:
        for path in ("/", "/websites", "/scans", "/modules/issues"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_navigation_search_is_interactive(self) -> None:
        response = self.client.get("/")
        html = response.get_data(as_text=True)
        self.assertIn('id="si-menu-search" type="search"', html)
        self.assertIn('placeholder="Search tools and pages"', html)
        self.assertIn('<script src="/static/app.js" defer></script>', html)

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


if __name__ == "__main__":
    unittest.main()
