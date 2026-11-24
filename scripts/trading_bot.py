from importlib import import_module

MODULE = import_module("src.trading_bot")

BotConfig = MODULE.BotConfig
CryptoTradingBot = MODULE.CryptoTradingBot
logger = MODULE.logger
