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

_temp_dir = Path(tempfile.mkdtemp(prefix="openaudit-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{_temp_dir / 'test.db'}"
os.environ["ALLOW_PRIVATE_TARGETS"] = "false"
database = importlib.import_module("database")


class DatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        database.init_database()

    def test_database_readiness(self) -> None:
        self.assertTrue(database.database_is_ready())

    def test_normalizes_public_url(self) -> None:
        self.assertEqual(database.normalized_url("https://example.com/about"), "https://example.com/about/")

    def test_rejects_embedded_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "credentials"):
            database.normalized_url("https://admin:secret@example.com")

    def test_rejects_private_targets(self) -> None:
        for url in ("http://localhost", "http://127.0.0.1", "http://169.254.169.254"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                database.normalized_url(url)

    def test_issue_lifecycle_is_isolated_by_website(self) -> None:
        first = database.create_website({"name": "First", "base_url": "https://first.example.com"})
        second = database.create_website({"name": "Second", "base_url": "https://second.example.com"})
        finding = [{"id": "image-alt", "title": "Images need alt text", "source": "pa11y", "points": 3}]

        database.reconcile_issues(first["key"], finding, "first.json", scanned_sources={"pa11y"})

        self.assertEqual(len(database.list_issues(first["key"])), 1)
        self.assertEqual(database.list_issues(second["key"]), [])


if __name__ == "__main__":
    unittest.main()
