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
                                                      "summary": "A mystery", "Guid": [{"id": "tmdb://222"}]}]}}
        with patch.object(ai, "get_plex", side_effect=plex):
            favorites = ai.plex_preferences()
        movie = next(x for x in favorites if x["kind"] == "movie")
        show = next(x for x in favorites if x["kind"] == "show")
        self.assertEqual(movie["weight"], 8)
        self.assertGreater(show["weight"], 8)
        self.assertEqual(movie["genres"], ["Thriller"])
        self.assertEqual(movie["external_ids"], {"tmdb": "222"})

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

    def test_feedback_persists_and_hide_excludes_a_title(self):
        with tempfile.TemporaryDirectory() as temp:
            engine = ai.Recommender(temp, start_worker=False)
            engine.favorites = [{"title": "Space", "genres": ["Sci-Fi"], "weight": 15}]
            engine.feedback("movie", 100, "more")
            self.assertEqual(ai.Recommender(temp, start_worker=False)._feedback()[("movie", "100")], "more")
            engine.feedback("movie", 100, "hide")
            candidates = [
                {"kind": "movie", "tmdb": 100, "title": "Hidden Movie", "genres": ["Sci-Fi"], "rating": 8},
                {"kind": "movie", "tmdb": 101, "title": "Other Movie", "genres": ["Sci-Fi"], "rating": 7.6,
                 "source_rows": ["discovery"]},
            ]
            engine.embed_lock.acquire()
            try:
                out = engine.rank(candidates, diversity=85)
            finally:
                engine.embed_lock.release()
            self.assertEqual([x["tmdb"] for x in out["items"]], [101])
            self.assertEqual([x["tmdb"] for x in out["hidden_gems"]], [101])
            self.assertIn("Similar to", out["items"][0]["ai_reason"])
            engine.feedback("movie", 100, "clear")
            self.assertNotIn(("movie", "100"), engine._feedback())

    def test_jellyfin_play_count_without_completed_flag_is_not_a_finish(self):
        response = {"Items": [
            {"Id": "1", "Type": "Movie", "Name": "Started Only",
             "UserData": {"PlayCount": 4, "Played": False}},
            {"Id": "2", "Type": "Movie", "Name": "Actually Finished",
             "UserData": {"PlayCount": 2, "Played": True}},
        ]}
        with patch.object(ai, "get_json", return_value=response):
            results = ai.jellyfin_preferences()
        self.assertEqual([x["title"] for x in results], ["Actually Finished"])

    def test_natural_language_ranks_relevant_title_without_history(self):
        with tempfile.TemporaryDirectory() as temp:
            engine = ai.Recommender(temp, start_worker=False)
            options = [
                {"kind": "movie", "tmdb": 1, "title": "Summer Garden",
                 "overview": "A documentary about flowers", "genres": ["Documentary"]},
                {"kind": "movie", "tmdb": 2, "title": "Time Loops",
                 "overview": "A psychological mystery about science fiction time travel",
                 "genres": ["Science Fiction", "Mystery"]},
            ]
            engine.embed_lock.acquire()
            try:
                result = engine.rank(options, query="psychological science fiction mystery", mood="mind_bending")
            finally:
                engine.embed_lock.release()
            self.assertTrue(result["personalized"])
            self.assertEqual(result["items"][0]["tmdb"], 2)
            self.assertEqual(result["items"][0]["ai_reason"], "Matches your search")

    def test_mood_short_movie_favors_known_short_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            engine = ai.Recommender(temp, start_worker=False)
            options = [
                {"kind": "movie", "tmdb": 1, "title": "Epic", "runtime": 190},
                {"kind": "movie", "tmdb": 2, "title": "Short", "runtime": 90},
                {"kind": "show", "tvdb": 3, "title": "Series"},
            ]
            engine.embed_lock.acquire()
            try:
                result = engine.rank(options, mood="short")
            finally:
                engine.embed_lock.release()
            self.assertEqual(result["items"][0]["tmdb"], 2)

    def test_franchise_diversity_does_not_group_unrelated_titles(self):
        self.assertEqual(ai.collection_key({"title": "John Wick: Chapter 2"}), "sequel:john wick")
        self.assertEqual(ai.collection_key({"title": "A Different Movie"}), "")
        self.assertEqual(ai.collection_key({"collection_id": 1234, "title": "Movie 5"}), "collection:1234")
        with tempfile.TemporaryDirectory() as temp:
            engine = ai.Recommender(temp, start_worker=False)
            options = [
                {"kind": "movie", "tmdb": 1, "title": "Galaxy Quest: Chapter 2",
                 "collection_id": 1234, "genres": ["Adventure"]},
                {"kind": "movie", "tmdb": 2, "title": "Galaxy Quest: Chapter 3",
                 "collection_id": 1234, "genres": ["Adventure"]},
                {"kind": "movie", "tmdb": 3, "title": "Another Adventure", "genres": ["Adventure"]},
            ]
            engine.embed_lock.acquire()
            try:
                output = engine.rank(options, mood="surprise", diversity=100)
            finally:
                engine.embed_lock.release()
            self.assertIn(3, [x["tmdb"] for x in output["items"][:2]])

    def test_quality_counters_use_aggregates_and_validate_input(self):
        with tempfile.TemporaryDirectory() as temp:
            engine = ai.Recommender(temp, start_worker=False)
            engine.metric("served", "recommended", 20)
            engine.metric("open", "recommended", 2)
            engine.metric("request", "recommended", 1)
            report = ai.Recommender(temp, start_worker=False).stats()
            self.assertEqual(report["by_source"]["recommended"]["served"], 20)
            self.assertEqual(report["by_source"]["recommended"]["open_per_100_served"], 10.0)
            self.assertEqual(report["by_source"]["recommended"]["request"], 1)
            with self.assertRaises(ValueError):
                engine.metric("served", "recommended", 1000)
            with self.assertRaises(ValueError):
                engine.metric("served", "unknown", 1)

    def test_query_mood_and_event_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            engine = ai.Recommender(temp, start_worker=False)
            with self.assertRaises(ValueError):
                engine.rank([], query="x" * 181)
            with self.assertRaises(ValueError):
                engine.rank([], mood="secret")
            with self.assertRaises(ValueError):
                engine.rank([{}] * 121)

    def test_exact_provider_ids_exclude_watched_without_guessing_by_title(self):
        with tempfile.TemporaryDirectory() as temp:
            engine = ai.Recommender(temp, start_worker=False)
            engine.favorites = [
                {"kind": "movie", "title": "Watched Film", "genres": ["Mystery"],
                 "external_ids": {"tmdb": "4321"}, "weight": 15}
            ]
            options = [
                {"kind": "movie", "tmdb": 4321, "title": "Watched Film"},
                {"kind": "movie", "tmdb": 4322, "title": "Watched Film"},
            ]
            engine.embed_lock.acquire()
            try:
                result = engine.rank(options)
            finally:
                engine.embed_lock.release()
            self.assertEqual([item["tmdb"] for item in result["items"]], [4322])

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
