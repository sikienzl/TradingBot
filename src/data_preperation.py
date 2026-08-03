# data_preparation.py
import pandas as pd
import numpy as np
from pathlib import Path
import os


def prepare_training_data(input_file="full_crypto_data.csv", output_file="training_data.csv"):
    try:
        print("🔍 Überprüfe Eingabedatei...")
        if not os.path.exists(input_file):
            raise FileNotFoundError(f"Datei {input_file} nicht gefunden")

        # Daten laden mit spezieller Behandlung für leere Werte
        print("📊 Lade Rohdaten...")
        df = pd.read_csv(input_file)

        # 1. Spaltenanalyse und Bereinigung
        print("🔧 Verarbeite Daten...")

        # Coin-Spalte aus 'symbol' extrahieren (z.B. "0G/EUR" -> "0G")
        if 'coin' not in df.columns:
            df['coin'] = df['symbol'].str.split('/').str[0]
        else:
            # Falls 'coin' bereits existiert, duplizierte Spalte entfernen
            if len(df.columns[df.columns == 'coin']) > 1:
                df = df.loc[:, ~df.columns.duplicated()]

        # Zeitstempel konvertieren (bereits im richtigen Format)
        df['timestamp'] = pd.to_datetime(df['timestamp'])

        # 2. Behandlung von leeren Werten
        print("🧹 Bereinige leere Werte...")

        # Numerische Spalten identifizieren
        numeric_cols = ['open', 'high', 'low', 'close', 'volume',
                        'rsi', 'macd', 'macd_signal', 'macd_hist',
                        'sma_50', 'sma_200', 'ema_20', 'ema_50', 'ema_200',
                        'atr_14', 'stoch_k', 'stoch_d', 'cci_20', 'obv',
                        'bb_upper', 'bb_middle', 'bb_lower']

        # Leere Werte durch NaN ersetzen
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # 3. Qualitätskontrolle
        print("✅ Datenqualität prüfen...")

        # Mindestanzahl gültiger Werte pro Coin
        min_valid = 30
        value_counts = df.groupby('coin').size()

        # Nur Coins mit genug Daten behalten
        valid_coins = value_counts[value_counts >= min_valid].index
        df = df[df['coin'].isin(valid_coins)]

        # 4. Technische Indikatoren berechnen (falls fehlen)
        print("📈 Berechne fehlende Indikatoren...")

        if 'rsi' not in df.columns or df['rsi'].isna().all():
            # Einfache RSI-Berechnung (vereinfacht)
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            df['rsi'] = 100 - (100 / (1 + rs))

        # 5. Daten für Training vorbereiten
        print("🛠️ Finalisiere Daten...")

        # Nur relevante Spalten behalten
        final_columns = ['timestamp', 'coin', 'close', 'rsi', 'macd', 'macd_signal', 'macd_hist',
                         'sma_50', 'sma_200', 'ema_20', 'ema_50', 'ema_200',
                         'atr_14', 'stoch_k', 'stoch_d', 'cci_20', 'obv',
                         'bb_upper', 'bb_middle', 'bb_lower']
        final_columns = [col for col in final_columns if col in df.columns]

        # Doppelte Spalten entfernen (falls noch vorhanden)
        df = df.loc[:, ~df.columns.duplicated()]

        # Nach Coin und Zeit sortieren
        df = df.sort_values(['coin', 'timestamp'])

        # 6. Daten speichern
        df.to_csv(output_file, index=False)
        print(f"✅ Trainingsdaten vorbereitet: {len(df)} Datensätze")
        print(
            f"   - Zeitrahmen: {df['timestamp'].min()} bis {df['timestamp'].max()}")
        print(f"   - Enthaltene Coins: {df['coin'].nunique()}")
        return True

    except Exception as e:
        print(f"❌ Fehler bei Datenvorbereitung: {str(e)}")
        return False


def diagnose_data(input_file="full_crypto_data.csv"):
    """Diagnostic function for analyzing the data structure"""
    if not os.path.exists(input_file):
        print(f"❌ File {input_file} not found")
        return

    print(f"📄 Analyzing {input_file}...")
    df = pd.read_csv(input_file, nrows=100)  # Analyze first 100 rows

    print("\n📋 Basic information:")
    print(f"- Rows: {len(df)}")
    print(f"- Columns: {len(df.columns)}")
    print(f"- Timeframe: {df['timestamp'].min()} to {df['timestamp'].max()}")

    print("\n🔍 Column overview:")
    for col in df.columns:
        dtype = df[col].dtype
        sample = df[col].dropna(
        ).iloc[0] if not df[col].dropna().empty else "N/A"
        na_count = df[col].isna().sum()
        print(f"- {col}: {dtype} (example: {sample}, NaN: {na_count})")

    print("\n💡 Recommendations:")
    if 'symbol' in df.columns:
        print("✅ 'symbol' column found - can be used for 'coin'")
    else:
        print("⚠️ No currency column (symbol/pair) found")

    numeric_cols = ['open', 'high', 'low', 'close', 'volume', 'rsi', 'macd']
    for col in numeric_cols:
        if col in df.columns:
            na_percent = df[col].isna().mean() * 100
            if na_percent > 30:
                print(f"⚠️ {col} has {na_percent:.1f}% NaN values")


if __name__ == "__main__":
    # First run diagnostics
    diagnose_data()

    # Then prepare the data
    success = prepare_training_data()
    if success:
        print("✅ Data preparation completed.")
    else:
        print("❌ Data preparation failed.")
