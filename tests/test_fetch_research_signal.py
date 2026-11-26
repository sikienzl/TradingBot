"""Tests for scripts/fetch_research_signal.py"""

# Allow importing the script as a module without running main()
import email.utils
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

SCRIPT_PATH = os.path.join(os.path.dirname(
    __file__), "..", "scripts", "fetch_research_signal.py")


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "fetch_research_signal", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestMapFng(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module()

    def _entry(self, value, classification, timestamp="1746144000"):
        return {"value": str(value), "value_classification": classification, "timestamp": timestamp}

    def test_extreme_greed_maps_to_bull(self):
        result = self.mod._map_fng(self._entry(80, "Extreme Greed"))
        self.assertEqual(result["market_regime"], "bull")
        self.assertGreater(result["sentiment_score"], 0)
        self.assertEqual(result["normalized_features"]
                         ["research_regime_bull"], 1.0)
        self.assertEqual(result["normalized_features"]
                         ["research_regime_bear"], 0.0)

    def test_extreme_fear_maps_to_bear(self):
        result = self.mod._map_fng(self._entry(10, "Extreme Fear"))
        self.assertEqual(result["market_regime"], "bear")
        self.assertLess(result["sentiment_score"], 0)
        self.assertEqual(result["normalized_features"]
                         ["research_regime_bear"], 1.0)
        self.assertGreater(result["normalized_features"]
                           ["research_risk_score"], 0)

    def test_neutral_maps_to_sideways(self):
        result = self.mod._map_fng(self._entry(50, "Neutral"))
        self.assertEqual(result["market_regime"], "sideways")
        self.assertAlmostEqual(result["sentiment_score"], 0.0)
        self.assertAlmostEqual(result["confidence"], 0.0)

    def test_greed_maps_to_bull(self):
        result = self.mod._map_fng(self._entry(65, "Greed"))
        self.assertEqual(result["market_regime"], "bull")

    def test_fear_maps_to_bear(self):
        result = self.mod._map_fng(self._entry(30, "Fear"))
        self.assertEqual(result["market_regime"], "bear")

    def test_output_has_required_keys(self):
        result = self.mod._map_fng(self._entry(60, "Greed"))
        required = ["timestamp_utc", "sentiment_score", "confidence", "risk_score",
                    "market_regime", "citations", "normalized_features", "integration"]
        for key in required:
            self.assertIn(key, result)

    def test_normalized_feature_keys(self):
        result = self.mod._map_fng(self._entry(40, "Fear"))
        nf = result["normalized_features"]
        for key in ["research_sentiment_score", "research_confidence", "research_risk_score",
                    "research_regime_bull", "research_regime_bear", "research_regime_sideways"]:
            self.assertIn(key, nf)

    def test_sentiment_score_clamped(self):
        result = self.mod._map_fng(self._entry(0, "Extreme Fear"))
        self.assertGreaterEqual(
            result["normalized_features"]["research_sentiment_score"], -1.0)
        result2 = self.mod._map_fng(self._entry(100, "Extreme Greed"))
        self.assertLessEqual(
            result2["normalized_features"]["research_sentiment_score"], 1.0)

    def test_citation_contains_url(self):
        result = self.mod._map_fng(self._entry(55, "Greed"))
        self.assertTrue(len(result["citations"]) > 0)
        self.assertIn("alternative.me", result["citations"][0])


class TestFetchFng(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module()

    def test_raises_on_empty_data(self):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"data": []}).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with (
            patch("urllib.request.urlopen", return_value=mock_response),
            self.assertRaises(ValueError),
        ):
            self.mod._fetch_fng()

    def test_returns_first_entry(self):
        entry = {"value": "72", "value_classification": "Greed",
                 "timestamp": "1746144000"}
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"data": [entry]}).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            result = self.mod._fetch_fng()
        self.assertEqual(result["value"], "72")


class TestFetchNews(unittest.TestCase):
    def setUp(self):
        self.mod = _load_module()

    def test_news_items_are_deduplicated_and_keep_publication_and_fetch_times(self):
        published = email.utils.format_datetime(datetime.now(UTC) - timedelta(minutes=3))
        xml = (
            "<rss><channel>"
            f"<item><title>ETF approved</title><link>https://news.test/a</link><pubDate>{published}</pubDate></item>"
            "<item><title>Undated bullish headline</title><link>https://news.test/undated</link></item>"
            "</channel></rss>"
        ).encode()

        def response_for(_request, timeout):
            response = MagicMock()
            response.read.return_value = xml
            response.__enter__ = lambda _self: response
            response.__exit__ = MagicMock(return_value=False)
            return response

        with patch("urllib.request.urlopen", side_effect=response_for):
            result = self.mod._fetch_news_signal(["https://feed.test/one", "https://feed.test/two"], 24, 10)

        self.assertEqual(len(result["news_items"]), 1)
        item = result["news_items"][0]
        self.assertEqual(item["link"], "https://news.test/a")
        self.assertIn("published_at", item)
        self.assertIn("fetched_at", item)
        self.assertIn("content_sha256", item)


class TestNewsItemStoreWiring(unittest.TestCase):
    """The fetcher must persist every scored item as an immutable record."""

    def setUp(self):
        self.mod = _load_module()

    def _run_fetch(self, tmp_path, argv):
        published = email.utils.format_datetime(datetime.now(UTC) - timedelta(minutes=3))
        xml = (
            "<rss><channel>"
            f"<item><title>ETF approved</title><link>https://news.test/a</link>"
            f"<pubDate>{published}</pubDate></item>"
            "</channel></rss>"
        ).encode()

        def response_for(_request, timeout):
            response = MagicMock()
            response.read.return_value = xml
            response.__enter__ = lambda _self: response
            response.__exit__ = MagicMock(return_value=False)
            return response

        argv = ["--providers", "news", "--output", str(tmp_path / "signal.json")] + argv
        with (
            patch("urllib.request.urlopen", side_effect=response_for),
            patch.object(sys, "argv", ["fetch_research_signal.py", *argv]),
            patch("builtins.print"),
        ):
            self.mod.main()
        return json.loads((tmp_path / "signal.json").read_text(encoding="utf-8"))

    def test_store_receives_one_record_per_scored_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            store = tmp_path / "news.jsonl"
            signal = self._run_fetch(tmp_path, ["--news-store", str(store)])

            lines = [line for line in store.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])
            self.assertEqual(record["link"], "https://news.test/a")
            self.assertTrue(record["published_at"])
            self.assertTrue(record["fetched_at"])
            self.assertTrue(record["stored_at"])
            self.assertEqual(signal["integration"]["news_store_appended"], 1)
            self.assertEqual(signal["integration"]["news_store_path"], str(store))

    def test_rerunning_the_fetch_does_not_duplicate_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            store = tmp_path / "news.jsonl"
            self._run_fetch(tmp_path, ["--news-store", str(store)])
            signal = self._run_fetch(tmp_path, ["--news-store", str(store)])

            lines = [line for line in store.read_text(encoding="utf-8").splitlines() if line]
            self.assertEqual(len(lines), 1)
            self.assertEqual(signal["integration"]["news_store_appended"], 0)
            self.assertEqual(signal["integration"]["news_store_duplicates"], 1)

    def test_unwritable_store_does_not_fail_the_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            blocker = tmp_path / "blocked"
            blocker.write_text("not a directory", encoding="utf-8")
            signal = self._run_fetch(
                tmp_path, ["--news-store", str(blocker / "news.jsonl")]
            )

            self.assertTrue(signal["news_items"])
            self.assertIn("news_store_error", signal["integration"])

    def test_store_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            signal = self._run_fetch(tmp_path, ["--news-store", "none"])

            self.assertNotIn("news_store_path", signal["integration"])
            self.assertEqual(list(tmp_path.iterdir()), [tmp_path / "signal.json"])

    def test_resolve_store_path_disables_on_placeholder_values(self):
        for value in ("", "none", "NONE", "off", "false"):
            self.assertEqual(self.mod._resolve_store_path(value), "")
        self.assertTrue(self.mod._resolve_store_path("data/news.jsonl").endswith(
            os.path.join("data", "news.jsonl")))
        self.assertEqual(self.mod._resolve_store_path("/tmp/news.jsonl"), "/tmp/news.jsonl")


if __name__ == "__main__":
    unittest.main()
