#!/usr/bin/env python3
"""Deterministic metadata-only benchmark; run directly on the UN1250.

This measures ranking overhead with 120 candidate records and 30 watched
favorites, not the initial CPU cost of generating Qwen3 embeddings.
"""
import statistics
import tempfile
import time

from app import Recommender


def main():
    with tempfile.TemporaryDirectory() as data_dir:
        engine = Recommender(data_dir, start_worker=False)
        engine.favorites = [
            {"kind": "movie", "key": str(i), "title": "Favorite " + str(i),
             "genres": ["Science Fiction" if i % 2 else "Thriller"],
             "overview": "A complex story with unexpected twists and friends", "weight": 5 + i}
            for i in range(30)
        ]
        candidates = [
            {"kind": "movie", "tmdb": 1000 + i,
             "title": "Candidate " + str(i),
             "genres": ["Science Fiction" if i % 2 else "Thriller"],
             "overview": "Time travel mystery and a surprising adventure",
             "rating": 7.2 + (i % 3) * .1, "runtime": 100}
            for i in range(120)
        ]
        elapsed = []
        engine.embed_lock.acquire()  # no warm-up Ollama HTTP calls during this benchmark
        try:
            for _ in range(20):
                started = time.perf_counter()
                output = engine.rank(candidates, diversity=65,
                                     mood="mind_bending", query="time travel science fiction mystery")
                elapsed.append((time.perf_counter() - started) * 1000)
                assert len(output["items"]) == 120
        finally:
            engine.embed_lock.release()
        print("Metadata-only ranking, 120 candidates / 30 favorites / 20 iterations")
        print("median: %.1f ms" % statistics.median(elapsed))
        print("p95: %.1f ms" % sorted(elapsed)[18])
        print("Note: this does not measure Ollama first embedding/indexing cost.")


if __name__ == "__main__":
    main()
