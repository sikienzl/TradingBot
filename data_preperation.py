# data_preparation.py
import pandas as pd
import numpy as np
from pathlib import Path
import os


def prepare_training_data(input_file="full_crypto_data.csv", output_file="training_data.csv"):
    try:
        print("🔍 Checking input file...")
        if not os.path.exists(input_file):
            raise FileNotFoundError(f"File {input_file} not found")

        # Load data with special handling for empty values
        print("📊 Loading raw data...")
        df = pd.read_csv(input_file)

        # 1. Column analysis and cleanup
        print("🔧 Processing data...")

        # Extract coin column from 'symbol' (e.g. "0G/EUR" -> "0G")
        if 'coin' not in df.columns:
            df['coin'] = df['symbol'].str.split('/').str[0]
        else:
            # Remove duplicated column if 'coin' already exists
            if len(df.columns[df.columns == 'coin']) > 1:
                df = df.loc[:, ~df.columns.duplicated()]

        # Convert timestamps (already in the correct format)
        df['timestamp'] = pd.to_datetime(df['timestamp'])

        # 2. Handle missing values
        print("🧹 Cleaning missing values...")

        # Identify numeric columns
        numeric_cols = ['open', 'high', 'low', 'close', 'volume',
                        'rsi', 'macd', 'macd_signal', 'macd_hist',
                        'sma_50', 'sma_200', 'ema_20', 'ema_50', 'ema_200',
                        'atr_14', 'stoch_k', 'stoch_d', 'cci_20', 'obv',
                        'bb_upper', 'bb_middle', 'bb_lower']

        # Replace empty values with NaN
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # 3. Quality control
        print("✅ Checking data quality...")

        # Minimum number of valid values per coin
        min_valid = 30
        value_counts = df.groupby('coin').size()

        # Keep only coins with enough data
        valid_coins = value_counts[value_counts >= min_valid].index
        df = df[df['coin'].isin(valid_coins)]

        # 4. Calculate technical indicators (if missing)
        print("📈 Calculating missing indicators...")

        if 'rsi' not in df.columns or df['rsi'].isna().all():
            # Einfache RSI-Berechnung (vereinfacht)
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            df['rsi'] = 100 - (100 / (1 + rs))

        # 5. Prepare data for training
        print("🛠️ Finalizing data...")

        # Keep only relevant columns
        final_columns = ['timestamp', 'coin', 'close', 'rsi', 'macd', 'macd_signal', 'macd_hist',
                         'sma_50', 'sma_200', 'ema_20', 'ema_50', 'ema_200',
                         'atr_14', 'stoch_k', 'stoch_d', 'cci_20', 'obv',
                         'bb_upper', 'bb_middle', 'bb_lower']
        final_columns = [col for col in final_columns if col in df.columns]

        # Remove duplicate columns (if any remaining)
        df = df.loc[:, ~df.columns.duplicated()]

        # Sort by coin and time
        df = df.sort_values(['coin', 'timestamp'])

        # 6. Save data
        df.to_csv(output_file, index=False)
        print(f"✅ Training data prepared: {len(df)} records")
        print(
            f"   - Time range: {df['timestamp'].min()} to {df['timestamp'].max()}")
        print(f"   - Coins included: {df['coin'].nunique()}")
        return True

    except Exception as e:
        print(f"❌ Error during data preparation: {str(e)}")
        return False


def diagnose_data(input_file="full_crypto_data.csv"):
    """Diagnostic function for analysing the data structure"""
    if not os.path.exists(input_file):
        print(f"❌ File {input_file} not found")
        return

    print(f"📄 Analysing {input_file}...")
    df = pd.read_csv(input_file, nrows=100)  # Analyse first 100 rows

    print("\n📋 Basic information:")
    print(f"- Rows: {len(df)}")
    print(f"- Columns: {len(df.columns)}")
    print(f"- Time range: {df['timestamp'].min()} to {df['timestamp'].max()}")

    print("\n🔍 Column overview:")
    for col in df.columns:
        dtype = df[col].dtype
        sample = df[col].dropna(
        ).iloc[0] if not df[col].dropna().empty else "N/A"
        na_count = df[col].isna().sum()
        print(f"- {col}: {dtype} (Example: {sample}, NaN: {na_count})")

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
    # Run diagnosis first
    diagnose_data()

    # Then prepare data
    success = prepare_training_data()
    if success:
        print("✅ Data preparation complete.")
    else:
        print("❌ Data preparation failed.")
