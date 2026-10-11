import os
import numpy as np
import pandas as pd
import lightgbm as lgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report
from sklearn.utils.class_weight import compute_sample_weight

# ─── PATH CONFIGURATION ───
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.abspath(os.path.join(current_dir, "../../"))
models_dir = os.path.join(project_root, "models")
data_path = os.path.join(project_root, "historical_macro_matrix.csv") # Replace with your actual historical data dump
model_output_path = os.path.join(models_dir, "macro_regime_v1.txt")

# Ensure models directory exists
os.makedirs(models_dir, exist_ok=True)

# ─── HYPERPARAMETERS ───
FORWARD_SHIFT_BARS = 8       # 8 bars * 2 mins = 16 mins forward target
TREND_THRESHOLD = 0.0006     # 0.10% move required to classify as a trend (Bull/Bear)

def load_and_label_data(filepath):
    """Loads feature matrix and calculates forward-looking target labels."""
    print(f"[SYSTEM] Loading historical matrix from {filepath}...")
    df = pd.read_csv(filepath)
    
    # Assuming standard index or timestamp column exists; sort chronologically
    if 'timestamp' in df.columns:
        df = df.sort_values('timestamp').reset_index(drop=True)

    # 1. Create Forward Target (Nifty 50 Close 16 minutes from now)
    df['future_nifty'] = df['NIFTY_50_close'].shift(-FORWARD_SHIFT_BARS)
    
    # 2. Calculate % Return
    df['future_return'] = (df['future_nifty'] - df['NIFTY_50_close']) / df['NIFTY_50_close']
    
    # 3. Apply Multi-Class Labels
    # LightGBM requires labels starting from 0. 
    # 0 = Bearish, 1 = Chop (Sideways), 2 = Bullish
    conditions = [
        (df['future_return'] < -TREND_THRESHOLD),
        (df['future_return'] > TREND_THRESHOLD)
    ]
    choices = [0, 2]
    df['label'] = np.select(conditions, choices, default=1)
    
    # Drop rows where future target is NaN (the very end of the dataset)
    df = df.dropna(subset=['future_nifty', 'future_return', 'label'])
    
    return df

def train_model():
    if not os.path.exists(data_path):
        print(f"[ERROR] Historical data not found at {data_path}. Please dump your 2-min matrix to CSV first.")
        return

    df = load_and_label_data(data_path)
    
    # Drop target-leaking columns and metadata
    drop_cols = ['future_nifty', 'future_return', 'label', 'timestamp']
    features = [c for c in df.columns if c not in drop_cols]
    
    X = df[features]
    y = df['label'].astype(int)
    
    print(f"[SYSTEM] Training on {len(X)} samples. Features: {len(features)}")
    print(f"[SYSTEM] Class distribution: {np.bincount(y)}")

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.20, shuffle=False)
    
    # Calculate heavy penalty weights for the majority 'Chop' class
    weights = compute_sample_weight(class_weight='balanced', y=y_train)
    
    train_data = lgb.Dataset(X_train, label=y_train, weight=weights)
    test_data = lgb.Dataset(X_test, label=y_test, reference=train_data)
    
    # LightGBM Configuration designed to prevent overfitting on noisy financial data
    params = {
        'objective': 'multiclass',
        'num_class': 3,
        'metric': 'multi_logloss',
        'boosting_type': 'gbdt',
        'learning_rate': 0.05,
        'max_depth': 4,              # Shallow trees prevent fitting to noise
        'num_leaves': 15,
        'feature_fraction': 0.8,     # Use 80% of features per tree (sub-sampling)
        'verbose': -1,
        'seed': 42
    }
    
    print("[SYSTEM] Commencing LightGBM compilation...")
    model = lgb.train(
        params,
        train_data,
        num_boost_round=500,
        valid_sets=[train_data, test_data],
        callbacks=[lgb.early_stopping(stopping_rounds=50)]
    )
    
    # Evaluate
    preds = model.predict(X_test)
    pred_labels = np.argmax(preds, axis=1)
    print("\n[EVALUATION] Out-of-Sample Classification Report:")
    print(classification_report(y_test, pred_labels, target_names=['Bearish', 'Chop', 'Bullish']))
    
    # Save Model Artifact
    model.save_model(model_output_path)
    print(f"\n[SUCCESS] Regime Oracle compiled and serialized to: {model_output_path}")

if __name__ == "__main__":
    train_model()