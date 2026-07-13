from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "portal"))
sys.path.insert(0, str(ROOT / "services" / "scan-worker"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{Path(tempfile.mkdtemp(prefix='openaudit-content-')) / 'content.db'}")

import crawler
import content_quality


class FakeResponse:
    url = "https://example.com/about"
    status_code = 200
    headers = {"content-type": "text/html; charset=utf-8"}
    text = """<!doctype html><html lang="en-AU"><head>
      <title>About our accessible website services</title>
      <meta name="description" content="Learn how our team improves accessible websites for public and private organisations across Australia.">
      </head><body><main><h1>Accessible website services</h1><p>Clear content helps every visitor.</p></main></body></html>"""


class ContentQualityTests(unittest.TestCase):
    def test_crawler_extracts_content_metadata(self) -> None:
        with patch("crawler.safe_get", return_value=FakeResponse()):
            page = crawler.fetch_page(object(), FakeResponse.url, 1, "sitemap")

        self.assertEqual(page["language"], "en-AU")
        self.assertEqual(page["h1_count"], 1)
        self.assertGreater(page["word_count"], 4)
        self.assertIn("improves accessible websites", page["meta_description"])

    def test_content_rules_find_missing_and_duplicate_metadata(self) -> None:
        pages = [
            content_page("https://example.com/one", "Shared page title for testing", "Shared description that is deliberately long enough for the duplicate content quality rule to evaluate.", 1, 220, "en-AU"),
            content_page("https://example.com/two", "Shared page title for testing", "Shared description that is deliberately long enough for the duplicate content quality rule to evaluate.", 1, 220, "en-AU"),
            content_page("https://example.com/empty", "", "", 0, 40, ""),
        ]

        issues = content_quality.extract_content_issues(pages)
        issue_ids = {issue["id"] for issue in issues}

        self.assertIn("content-duplicate-title", issue_ids)
        self.assertIn("content-duplicate-meta-description", issue_ids)
        self.assertIn("content-missing-title", issue_ids)
        self.assertIn("content-missing-meta-description", issue_ids)
        self.assertIn("content-missing-h1", issue_ids)
        self.assertIn("content-thin-page", issue_ids)
        self.assertIn("content-missing-language", issue_ids)

    def test_content_only_scan_writes_its_own_report(self) -> None:
        pages = [content_page("https://example.com/", "Example content page title", "", 1, 120, "en-AU")]
        issues = content_quality.extract_content_issues(pages)
        with tempfile.TemporaryDirectory() as directory:
            report = content_quality.write_content_report(Path(directory), "example-com", pages, issues)
            self.assertTrue(report.exists())
            self.assertIn('"website_key": "example-com"', report.read_text(encoding="utf-8"))


def content_page(url: str, title: str, description: str, h1_count: int, word_count: int, language: str) -> dict[str, object]:
    return {
        "url": url,
        "title": title,
        "meta_description": description,
        "h1_count": h1_count,
        "word_count": word_count,
        "language": language,
        "status_code": 200,
        "content_type": "text/html",
    }


if __name__ == "__main__":
    unittest.main()
