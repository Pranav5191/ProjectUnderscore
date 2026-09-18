# test_scoring.py
import sqlite3
import pandas as pd
from src.scoring.volatility import NormalizedATRScorer

# 1. Read from the dummy DB
conn = sqlite3.connect('test_history.db')
df = pd.read_sql("SELECT * FROM daily_ohlcv WHERE token = 'DUMMY1' ORDER BY timestamp ASC", conn)

# 2. Initialize the Scorer
atr_scorer = NormalizedATRScorer(period=14)

# 3. Execute the Math
score = atr_scorer.calculate(df)
print(f"Normalized Volatility Score: {score:.2f}")