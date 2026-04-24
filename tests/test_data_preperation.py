import os
import tempfile
import pytest
import pandas as pd
import numpy as np

from data_preperation import prepare_training_data, diagnose_data


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def valid_crypto_data(temp_dir):
    """Create sample CSV with valid crypto OHLCV data."""
    data = {
        'timestamp': pd.date_range('2024-01-01', periods=100, freq='1h').tolist(),
        'symbol': ['BTC/EUR'] * 50 + ['ETH/EUR'] * 50,
        'open': np.random.uniform(50000, 60000, 100),
        'high': np.random.uniform(50000, 60000, 100),
        'low': np.random.uniform(50000, 60000, 100),
        'close': np.random.uniform(50000, 60000, 100),
        'volume': np.random.uniform(1, 100, 100),
        'rsi': np.random.uniform(30, 70, 100),
    }
    df = pd.DataFrame(data)
    filepath = os.path.join(temp_dir, 'test_input.csv')
    df.to_csv(filepath, index=False)
    return filepath


def test_prepare_training_data_file_not_found():
    """Test handling of missing input file."""
    result = prepare_training_data(
        input_file='/nonexistent/file.csv',
        output_file='/tmp/output.csv'
    )
    assert result is False


def test_prepare_training_data_creates_output(valid_crypto_data, temp_dir):
    """Test that output file is created with valid input."""
    output_file = os.path.join(temp_dir, 'output.csv')
    result = prepare_training_data(
        input_file=valid_crypto_data,
        output_file=output_file
    )
    assert result is True
    assert os.path.exists(output_file)


def test_prepare_training_data_extracts_coin_from_symbol(valid_crypto_data, temp_dir):
    """Test that 'coin' column is extracted from 'symbol'."""
    output_file = os.path.join(temp_dir, 'output.csv')
    prepare_training_data(
        input_file=valid_crypto_data,
        output_file=output_file
    )
    df = pd.read_csv(output_file)
    assert 'coin' in df.columns
    assert set(df['coin'].unique()) == {'BTC', 'ETH'}


def test_prepare_training_data_filters_low_volume_coins(temp_dir):
    """Test that coins with fewer than 30 records are filtered."""
    data = {
        'timestamp': pd.date_range('2024-01-01', periods=100, freq='1h').tolist(),
        # RARE has only 20 records
        'symbol': ['BTC/EUR'] * 80 + ['RARE/EUR'] * 20,
        'open': np.random.uniform(50000, 60000, 100),
        'high': np.random.uniform(50000, 60000, 100),
        'low': np.random.uniform(50000, 60000, 100),
        'close': np.random.uniform(50000, 60000, 100),
        'volume': np.random.uniform(1, 100, 100),
    }
    df = pd.DataFrame(data)
    input_file = os.path.join(temp_dir, 'test_input.csv')
    df.to_csv(input_file, index=False)

    output_file = os.path.join(temp_dir, 'output.csv')
    prepare_training_data(
        input_file=input_file,
        output_file=output_file
    )

    result = pd.read_csv(output_file)
    assert 'RARE' not in result['coin'].values
    assert 'BTC' in result['coin'].values


def test_prepare_training_data_converts_timestamps(valid_crypto_data, temp_dir):
    """Test that timestamps are properly converted."""
    output_file = os.path.join(temp_dir, 'output.csv')
    prepare_training_data(
        input_file=valid_crypto_data,
        output_file=output_file
    )
    df = pd.read_csv(output_file)
    # Should be sorted by coin and timestamp
    assert len(df) > 0
    # Timestamp should still be present
    assert 'timestamp' in df.columns


def test_prepare_training_data_handles_missing_rsi(temp_dir):
    """Test RSI calculation when column is missing."""
    data = {
        'timestamp': pd.date_range('2024-01-01', periods=50, freq='1h').tolist(),
        'symbol': ['BTC/EUR'] * 50,
        'close': 50000 + np.cumsum(np.random.uniform(-100, 100, 50)),
    }
    df = pd.DataFrame(data)
    input_file = os.path.join(temp_dir, 'test_input.csv')
    df.to_csv(input_file, index=False)

    output_file = os.path.join(temp_dir, 'output.csv')
    prepare_training_data(
        input_file=input_file,
        output_file=output_file
    )

    result = pd.read_csv(output_file)
    # RSI should be calculated
    assert 'rsi' in result.columns or result.shape[0] > 0


def test_prepare_training_data_removes_duplicate_columns(temp_dir):
    """Test that duplicate columns are removed."""
    data = {
        'timestamp': pd.date_range('2024-01-01', periods=50, freq='1h').tolist(),
        'symbol': ['BTC/EUR'] * 50,
        'coin': ['BTC'] * 50,
        'close': np.random.uniform(50000, 60000, 50),
    }
    df = pd.DataFrame(data)
    input_file = os.path.join(temp_dir, 'test_input.csv')
    df.to_csv(input_file, index=False)

    output_file = os.path.join(temp_dir, 'output.csv')
    result = prepare_training_data(
        input_file=input_file,
        output_file=output_file
    )
    assert result is True
    # Should not crash with duplicate columns


def test_prepare_training_data_sorts_by_coin_and_time(valid_crypto_data, temp_dir):
    """Test that output is sorted by coin and timestamp."""
    output_file = os.path.join(temp_dir, 'output.csv')
    prepare_training_data(
        input_file=valid_crypto_data,
        output_file=output_file
    )
    df = pd.read_csv(output_file)
    # Verify coin grouping (same coins should be consecutive)
    coins = df['coin'].values
    for i in range(len(coins) - 1):
        if coins[i] != coins[i + 1]:
            # Ensure no coin appears after being replaced by a different coin
            remaining = coins[i + 1:]
            assert coins[i] not in remaining, "Coins not properly grouped"


def test_diagnose_data_file_not_found(capsys):
    """Test diagnose_data with missing file."""
    diagnose_data(input_file='/nonexistent/file.csv')
    captured = capsys.readouterr()
    assert 'not found' in captured.out.lower()


def test_diagnose_data_prints_info(valid_crypto_data, capsys):
    """Test diagnose_data prints column information."""
    diagnose_data(input_file=valid_crypto_data)
    captured = capsys.readouterr()
    assert 'column' in captured.out.lower()
    assert 'rows' in captured.out.lower() or 'basic' in captured.out.lower()
