from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "trading_bot.py"
SPEC = spec_from_file_location("trading_bot_root", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise ImportError(f"Unable to load trading_bot module from {MODULE_PATH}")

MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

BotConfig = MODULE.BotConfig
CryptoTradingBot = MODULE.CryptoTradingBot
logger = MODULE.logger
