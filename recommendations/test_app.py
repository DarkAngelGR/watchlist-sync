"""Offline tests for watch-history grouping, ranking and API authorization."""
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("orama_recommender", Path(__file__).with_name("app.py"))
ai = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ai)


class RecommenderTests(unittest.TestCase):
    def test_plex_groups_completed_episodes_without_counting_each_as_rewatch(self):
        history = [
            {"type": "movie", "ratingKey": "42", "title": "Film"},
            {"type": "movie", "ratingKey": "42", "title": "Film"},
            {"type": "episode", "ratingKey": "10", "grandparentRatingKey": "5", "grandparentTitle": "Show"},
            {"type": "episode", "ratingKey": "11", "grandparentRatingKey": "5", "grandparentTitle": "Show"},
            {"type": "episode", "ratingKey": "10", "grandparentRatingKey": "5", "grandparentTitle": "Show"},
        ]
        def plex(_path, params=None):
            if params is not None:
                self.assertEqual(params["accountID"], ai.PLEX_ACCOUNT_ID)
                return {"MediaContainer": {"Metadata": history}}
            return {"MediaContainer": {"Metadata": [{"title": "Film", "Genre": [{"tag": "Thriller"}],
                                                      "summary": "A mystery"}]}}
        with patch.object(ai, "get_plex", side_effect=plex):
            favorites = ai.plex_preferences()
        movie = next(x for x in favorites if x["kind"] == "movie")
        show = next(x for x in favorites if x["kind"] == "show")
        self.assertEqual(movie["weight"], 8)
        self.assertGreater(show["weight"], 8)
        self.assertEqual(movie["genres"], ["Thriller"])

    def test_metadata_ranking_favors_similar_genres_and_keeps_existing_items(self):
        with tempfile.TemporaryDirectory() as temp:
            engine = ai.Recommender(temp, start_worker=False)
            engine.favorites = [{"title": "The Space Journey", "overview": "science fiction outer space",
                                 "genres": ["Science Fiction"], "weight": 18}]
            candidates = [
                {"kind": "movie", "tmdb": 1, "title": "Underwater Documentary", "genres": ["Documentary"]},
                {"kind": "movie", "tmdb": 2, "title": "Space Odyssey", "genres": ["Science Fiction"]},
            ]
            engine.embed_lock.acquire()  # no background model calls in offline tests
            try:
                output = engine.rank(candidates)
            finally:
                engine.embed_lock.release()
            self.assertTrue(output["personalized"])
            self.assertEqual([i["tmdb"] for i in output["items"]], [2, 1])
            self.assertEqual(len(engine.rank([])["items"]), 0)

    def test_repeated_plex_page_does_not_count_as_rewatch(self):
        history = [
            {"type": "movie", "ratingKey": str(i), "historyKey": "/history/" + str(i),
             "title": "Film " + str(i)} for i in range(200)
        ]
        calls = []
        def plex(_path, params=None):
            if params is not None:
                calls.append(params["X-Plex-Container-Start"])
                return {"MediaContainer": {"Metadata": history}}
            return {"MediaContainer": {"Metadata": []}}
        with patch.object(ai, "get_plex", side_effect=plex):
            favorites = ai.plex_preferences()
        self.assertEqual(calls, [0, 200])
        self.assertEqual(len(favorites), 50)
        self.assertTrue(all(f["weight"] == 3 for f in favorites))

    def test_empty_history_preserves_original_order(self):
        with tempfile.TemporaryDirectory() as temp:
            engine = ai.Recommender(temp, start_worker=False)
            orig = [{"title": "First"}, {"title": "Second"}]
            self.assertEqual(engine.rank(orig), {"personalized": False, "items": orig})

    def test_only_exact_bearer_secret_can_rank(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(ai, "API_KEY", "x" * 32):
            engine = ai.Recommender(temp, start_worker=False)
            srv = ThreadingHTTPServer(("127.0.0.1", 0), ai.make_handler(engine))
            thread = threading.Thread(target=srv.serve_forever, daemon=True)
            thread.start()
            try:
                url = "http://127.0.0.1:%s/v1/rank" % srv.server_port
                for header in ("", "Bearer bad"):
                    req = Request(url, b'{"items":[]}', {"Authorization": header}, method="POST")
                    with self.assertRaises(HTTPError) as err:
                        urlopen(req, timeout=3)
                    self.assertEqual(err.exception.code, 401)
                req = Request(url, b'{"items":[]}', {"Authorization": "Bearer " + ai.API_KEY},
                              method="POST")
                with urlopen(req, timeout=3) as response:
                    self.assertFalse(json.load(response)["personalized"])
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
