import sys
from importlib import import_module
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MODULE = import_module("src.trading_bot")

BotConfig = MODULE.BotConfig
CryptoTradingBot = MODULE.CryptoTradingBot
logger = MODULE.logger
