"""Check that the NAS-side integration is opt-in and keeps trusted metadata."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

import requests

spec = importlib.util.spec_from_file_location("watchlist_sync_app", Path(__file__).resolve().parents[1] / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class HookTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.syncer = app.Syncer(self.dir.name)
        self.base = {"sections": [
            {"id": "trending", "title": "Trending", "items": [
                {"kind": "movie", "tmdb": 1, "title": "One"},
                {"kind": "movie", "tmdb": 2, "title": "Two"}]},
            {"id": "recommended", "title": "Recommended for you", "items": [
                {"kind": "show", "tvdb": 3, "title": "Three"}]},
        ]}

    def tearDown(self):
        self.dir.cleanup()

    def test_opt_in_and_failure_fallback(self):
        with patch.dict(os.environ, {"RECOMMENDER_URL": "", "RECOMMENDER_API_KEY": ""}):
            self.assertIs(self.syncer._rank_discover(self.base, "test-profile"), self.base)
        with patch.dict(os.environ, {"RECOMMENDER_URL": "http://localhost:8788",
                                     "RECOMMENDER_API_KEY": "x" * 32,
                                     "RECOMMENDER_PROFILE_UUID": "test-profile"}), \
             patch.object(app.requests, "post", side_effect=requests.Timeout):
            self.assertIs(self.syncer._rank_discover(self.base, "test-profile"), self.base)

    def test_different_profile_never_receives_personalized_row(self):
        with patch.dict(os.environ, {"RECOMMENDER_URL": "http://localhost:8788",
                                     "RECOMMENDER_API_KEY": "x" * 32,
                                     "RECOMMENDER_PROFILE_UUID": "test-profile"}), \
             patch.object(app.requests, "post") as post:
            result = self.syncer._rank_discover(self.base, "someone-else")
            self.assertIs(result, self.base)
            post.assert_not_called()

    def test_rank_order_uses_upstream_candidate_records(self):
        malicious = {"kind": "movie", "tmdb": 2, "title": "Fake injected title",
                     "status": "have", "other": "unsafe"}
        mock = Mock()
        mock.json.return_value = {"personalized": True, "items": [
            malicious,
            {"kind": "movie", "tmdb": 9999, "title": "Unknown"},
            {"kind": "show", "tvdb": 3, "title": "Three"},
            {"kind": "movie", "tmdb": 1, "title": "One"},
        ]}
        with patch.dict(os.environ, {"RECOMMENDER_URL": "http://localhost:8788",
                                     "RECOMMENDER_API_KEY": "x" * 32,
                                     "RECOMMENDER_PROFILE_UUID": "test-profile"}), \
             patch.object(app.requests, "post", return_value=mock) as post:
            result = self.syncer._rank_discover(self.base, "test-profile", 75)
        row = result["sections"][0]
        self.assertEqual(row["id"], "recommended")
        self.assertEqual([x["title"] for x in row["items"]], ["Two", "Three", "One"])
        self.assertEqual([s["id"] for s in result["sections"]], ["recommended", "trending"])
        self.assertEqual(self.base["sections"][0]["title"], "Trending")
        sent = post.call_args.kwargs
        self.assertIn("Authorization", sent["headers"])
        self.assertEqual(len(sent["json"]["items"]), 3)


if __name__ == "__main__":
    unittest.main()
