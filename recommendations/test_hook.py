"""Check that the NAS-side integration is opt-in and keeps trusted metadata."""
import base64
import importlib.util
import json
import threading
import os
from pathlib import Path
import tempfile
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
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

    def test_plex_user_token_must_match_claimed_uuid(self):
        with patch.dict(os.environ, {"RECOMMENDER_AUTH_PROVIDER": "plex"}):
            response = Mock()
            response.json.return_value = {"uuid": "real-uuid"}
            with patch.object(app.requests, "get", return_value=response) as get:
                self.assertTrue(self.syncer._ai_verified("real-uuid", "signed-user-token"))
                self.assertFalse(self.syncer._ai_verified("another-uuid", "signed-user-token"))
                self.assertEqual(get.call_args.kwargs["headers"]["X-Plex-Token"], "signed-user-token")
                self.assertNotIn("signed-user-token", repr(self.syncer._ai_verification_cache))
        with patch.dict(os.environ, {"RECOMMENDER_AUTH_PROVIDER": ""}):
            self.assertFalse(self.syncer._ai_verified("real-uuid", "signed-user-token"))

    def test_jellyfin_token_identity_is_enforced(self):
        with patch.dict(os.environ, {"RECOMMENDER_AUTH_PROVIDER": "jellyfin",
                                     "RECOMMENDER_JELLYFIN_URL": "http://127.0.0.1:8096"}):
            response = Mock()
            response.json.return_value = {"Id": "jellyfin-user"}
            with patch.object(app.requests, "get", return_value=response):
                self.assertTrue(self.syncer._ai_verified("jellyfin-user", "user-access-token"))
                self.assertFalse(self.syncer._ai_verified("another-user", "user-access-token"))

    def test_tv_api_denies_claimed_profile_without_verified_token(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(self.syncer, "super-secret"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%s" % server.server_port
        basic = "Basic " + base64.b64encode(b"user:super-secret").decode()
        try:
            with patch.object(self.syncer, "_ai_verified", return_value=False), \
                 patch.object(self.syncer, "tv_discover", return_value={"sections": []}) as discover:
                req = Request(base + "/api/tv/discover?viewer=other-user",
                              headers={"Authorization": basic})
                with urlopen(req, timeout=3) as resp:
                    self.assertEqual(json.load(resp), {"sections": []})
                self.assertEqual(discover.call_args.args[0], "")
                unauthorized = Request(base + "/api/tv/ai/stats?viewer=other-user",
                                       headers={"Authorization": basic})
                with self.assertRaises(HTTPError) as err:
                    urlopen(unauthorized, timeout=3)
                self.assertEqual(err.exception.code, 403)
                feedback = Request(base + "/api/tv/feedback",
                                   headers={"Authorization": basic, "Content-Type": "application/json"},
                                   data=b'{"viewer":"other-user","kind":"movie","id":1,"action":"more"}')
                with self.assertRaises(HTTPError) as err:
                    urlopen(feedback, timeout=3)
                self.assertEqual(err.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()

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
