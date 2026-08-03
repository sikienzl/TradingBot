"""
Data Storage

This module handles data storage and retrieval for the trading bot.
"""

import logging
import sqlite3

import pandas as pd


class DataStorage:
    """Class for storing and retrieving market data"""
    
    def __init__(self, db_path: str = "data/trading_data.db"):
        """
        Initialize the data storage.
        
        Args:
            db_path: Path to the SQLite database
        """
        self.db_path = db_path
        self.logger = logging.getLogger(__name__)
        self.init_database()
        self.logger.info("Data storage initialized")
    
    def init_database(self):
        """Initialize the database schema."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Create tables if they don't exist
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ohlcv_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS indicators (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                sma_20 REAL,
                sma_50 REAL,
                rsi REAL,
                upper_band REAL,
                middle_band REAL,
                lower_band REAL,
                macd REAL,
                macd_signal REAL,
                macd_hist REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def save_ohlcv_data(self, symbol: str, timeframe: str, data: pd.DataFrame):
        """
        Save OHLCV data to database.
        
        Args:
            symbol: Trading pair symbol
            timeframe: Timeframe
            data: DataFrame with OHLCV data
        """
        self.logger.info(f"Saving OHLCV data for {symbol} ({timeframe})")
        
        conn = sqlite3.connect(self.db_path)
        data.to_sql('ohlcv_data', conn, if_exists='append', index=False)
        conn.close()
    
    def get_ohlcv_data(self, symbol: str, timeframe: str, limit: int = 1000) -> pd.DataFrame:
        """
        Retrieve OHLCV data from database.
        
        Args:
            symbol: Trading pair symbol
            timeframe: Timeframe
            limit: Number of records to retrieve
            
        Returns:
            DataFrame with OHLCV data
        """
        self.logger.info(f"Retrieving OHLCV data for {symbol} ({timeframe})")
        
        conn = sqlite3.connect(self.db_path)
        query = """
            SELECT * FROM ohlcv_data 
            WHERE symbol = ? AND timeframe = ? 
            ORDER BY timestamp DESC 
            LIMIT ?
        """
        df = pd.read_sql_query(query, conn, params=[symbol, timeframe, limit])
        conn.close()
        
        return df

# Example usage
if __name__ == "__main__":
    storage = DataStorage()
    print("Data storage initialized")