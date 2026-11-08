import unittest
from unittest.mock import MagicMock, patch, mock_open
import json
from src.trading_bot import CryptoTradingBot

class TestTradingBotScorecard(unittest.TestCase):
    def setUp(self):
        self.bot = CryptoTradingBot.__new__(CryptoTradingBot)
        # Mock the config object
        self.bot.config = MagicMock()

    def test_check_scorecard_verdict_all_ok(self):
        """Test that all OK returns True."""
        self.bot.config.scorecard_verdict_paths = ["path1.json", "path2.json"]
        
        # Mock open to return a valid JSON for both paths
        mock_data = json.dumps({"verdict": "GO", "reasons": ["All good"]})
        with patch("builtins.open", mock_open(read_data=mock_data)):
            ok, reason = self.bot._check_scorecard_verdict()
            self.assertTrue(ok)
            self.assertEqual(reason, "")

    def test_check_scorecard_verdict_one_no_go(self):
        """Test that one NO-GO returns False and aggregated reasons."""
        self.bot.config.scorecard_verdict_paths = ["path1.json", "path2.json"]
        
        # Mock open to return NO-GO for path1 and GO for path2
        def side_effect(path, *args, **kwargs):
            if "path1.json" in path:
                return mock_open(read_data=json.dumps({"verdict": "NO_GO", "reasons": ["Reason 1"]}))()
            else:
                return mock_open(read_data=json.dumps({"verdict": "GO", "reasons": ["Reason 2"]}))()

        with patch("builtins.open", side_effect=side_effect):
            ok, reason = self.bot._check_scorecard_verdict()
            self.assertFalse(ok)
            self.assertIn("Scorecard NO-GO (path1.json): Reason 1", reason)

    def test_check_scorecard_verdict_file_not_found(self):
        """Test that missing files are ignored."""
        self.bot.config.scorecard_verdict_paths = ["missing.json"]
        
        with patch("builtins.open", side_effect=FileNotFoundError):
            ok, reason = self.bot._check_scorecard_verdict()
            self.assertTrue(ok)
            self.assertEqual(reason, "")

if __name__ == "__main__":
    unittest.main()
