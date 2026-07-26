import ccxt
import talib
import numpy as np
import pandas as pd
from datetime import datetime
import time
import sqlite3
from pathlib import Path


class CryptoDataFetcher:
    """Klasse zum Abrufen und Verarbeiten von Kryptowährungsdaten"""

    def __init__(self, exchange='kraken', quote='EUR', days=1000, timeframe='1d'):
        self.exchange = exchange
        self.quote = quote
        self.days = days
        self.timeframe = timeframe
        self.data = {}
        self.error_log = []
        self.db_conn = None

    def initialize_exchange(self):
        """Initialisiert die Exchange-Verbindung"""
        try:
            exchange_class = getattr(ccxt, self.exchange)
            exchange = exchange_class({
                'enableRateLimit': True,
                'options': {'adjustForTimeDifference': True}
            })
            exchange.load_markets()
            return exchange
        except Exception as e:
            print(f"❌ Exchange Initialisierung fehlgeschlagen: {str(e)}")
            return None

    def get_available_pairs(self, exchange):
        """Holt alle verfügbaren Trading-Paare mit der gewünschten Quote-Währung"""
        pairs = []
        for symbol in exchange.symbols:
            if symbol.endswith(f'/{self.quote}') and exchange.markets[symbol]['active']:
                pairs.append(symbol)
        return sorted(pairs)

    def fetch_ohlcv_with_retry(self, exchange, symbol, max_retries=3):
        """Versucht mehrfach, OHLCV-Daten abzurufen"""
        for attempt in range(max_retries):
            try:
                ohlcv = exchange.fetch_ohlcv(
                    symbol, self.timeframe, limit=self.days)
                return ohlcv
            except Exception as e:
                wait_time = (attempt + 1) * 5
                print(
                    f"Versuch {attempt + 1}/{max_retries} für {symbol} fehlgeschlagen. Warte {wait_time}s...", end="\r")
                time.sleep(wait_time)
        self.error_log.append((symbol, str(e)))
        return None

    def calculate_indicators(self, df):
        """Berechnet technische Indikatoren für den DataFrame"""
        try:
            df['rsi'] = talib.RSI(df['close'], timeperiod=14)
            df['macd'], df['macd_signal'], df['macd_hist'] = talib.MACD(
                df['close'])
            df['sma_50'] = talib.SMA(df['close'], timeperiod=50)
            df['sma_200'] = talib.SMA(df['close'], timeperiod=200)
            df['ema_20'] = talib.EMA(df['close'], timeperiod=20)
            df['ema_50'] = talib.EMA(df['close'], timeperiod=50)
            df['ema_200'] = talib.EMA(df['close'], timeperiod=200)
            df['atr_14'] = talib.ATR(
                df['high'], df['low'], df['close'], timeperiod=14)
            slowk, slowd = talib.STOCH(df['high'], df['low'], df['close'], fastk_period=14,
                                       slowk_period=3, slowk_matype=0, slowd_period=3, slowd_matype=0)
            df['stoch_k'] = slowk
            df['stoch_d'] = slowd
            df['cci_20'] = talib.CCI(
                df['high'], df['low'], df['close'], timeperiod=20)
            df['obv'] = talib.OBV(df['close'], df['volume'])
            df['bb_upper'], df['bb_middle'], df['bb_lower'] = talib.BBANDS(
                df['close'])
            return True
        except Exception as e:
            print(f"Indikator-Berechnung fehlgeschlagen: {str(e)}")
            return False

    def process_symbol(self, exchange, symbol):
        """Verarbeitet ein einzelnes Währungspaar"""
        print(f"Verarbeite {symbol}...", end="\r")

        ohlcv = self.fetch_ohlcv_with_retry(exchange, symbol)
        if ohlcv is None:
            return False

        df = pd.DataFrame(
            ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df['symbol'] = symbol
        df['base'] = symbol.split('/')[0]
        df['quote'] = symbol.split('/')[1]

        if self.calculate_indicators(df):
            self.data[symbol] = df
            return True
        return False

    def fetch_all_data(self):
        """Hauptmethode zum Abrufen aller Daten"""
        exchange = self.initialize_exchange()
        if not exchange:
            return False

        pairs = self.get_available_pairs(exchange)
        print(f"Gefundene Währungspaare: {len(pairs)}")

        for i, pair in enumerate(pairs, 1):
            success = self.process_symbol(exchange, pair)
            if success:
                print(f"Erfolgreich: {i}/{len(pairs)} - {pair}", end="\r")
            time.sleep(0.1)  # Respektiere API-Limits

        print(
            f"\n\nVerarbeitung abgeschlossen. Erfolgreich: {len(self.data)}/{len(pairs)}")
        if self.error_log:
            print(f"Fehler bei {len(self.error_log)} Währungen:")
            for symbol, error in self.error_log[:5]:
                print(f"- {symbol}: {error}")
        return True

    def save_to_csv(self, filename="crypto_data.csv"):
        """Speichert alle Daten in einer CSV-Datei"""
        try:
            all_dfs = []
            for df in self.data.values():
                all_dfs.append(df)

            if not all_dfs:
                print("Keine Daten zum Speichern verfügbar")
                return False

            combined = pd.concat(all_dfs)
            combined.to_csv(filename, index=False)
            print(f"Daten erfolgreich in {filename} gespeichert")
            return True
        except Exception as e:
            print(f"Fehler beim Speichern: {str(e)}")
            return False

    def save_to_sqlite(self, filename="crypto_data.db"):
        """Speichert Daten in einer SQLite-Datenbank"""
        try:
            conn = sqlite3.connect(filename)
            combined = pd.concat([df for df in self.data.values()])
            combined.to_sql('crypto_data', conn,
                            if_exists='replace', index=False)
            conn.close()
            print(f"Daten erfolgreich in {filename} gespeichert")
            return True
        except Exception as e:
            print(f"Fehler beim Speichern in SQLite: {str(e)}")
            return False

    def get_sample_data(self, symbol=None):
        """Gibt Beispieldaten für eine Währung zurück"""
        if not symbol:
            symbol = next(iter(self.data)) if self.data else None
            if not symbol:
                return None

        if symbol in self.data:
            return self.data[symbol].tail(5)
        return None


if __name__ == "__main__":
    print("🚀 Krypto-Datenanalyse-Tool")
    print("="*50)

    # Initialisiere den Fetcher
    # 1h-Candles: 720 Kerzen = 30 Tage; passt zur Bot-Inference (OHLCV_TIMEFRAME=1h)
    fetcher = CryptoDataFetcher(
        exchange='kraken', quote='EUR', days=720, timeframe='1h')

    # Daten abrufen
    print("📊 Starte Datenabruf...")
    success = fetcher.fetch_all_data()

    if success and fetcher.data:
        # Daten speichern
        print("\n💾 Speichere Daten...")
        fetcher.save_to_csv("full_crypto_data.csv")
        fetcher.save_to_sqlite("crypto_data.db")

        # Beispielausgabe
        sample = fetcher.get_sample_data()
        if sample is not None:
            print("\n📈 Beispieldaten (letzte 5 Tage):")
            print(sample.to_string(index=False))

        print("\n✅ Prozess erfolgreich abgeschlossen!")
        print(f"Verarbeitete Währungen: {len(fetcher.data)}")
        print(
            f"Gespeicherte Datensätze: {sum(len(df) for df in fetcher.data.values())}")
    else:
        print("❌ Keine Daten konnten abgerufen werden")
