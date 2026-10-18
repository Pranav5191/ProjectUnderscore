import json
import os

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
TARGET_FILE = os.path.join(PROJECT_ROOT, "target_ticks.json")
SYMBOLS_FILE = os.path.join(PROJECT_ROOT, "symbols.json")

SCREENER_LIMIT = 25
FALLBACK_TOKENS = ["2885", "1333", "3045"]  # Reliance, HDFC Bank, SBI safety fallback

def update():
    token_list = []

    if not os.path.exists(TARGET_FILE):
        print("[WARN] target_ticks.json not found. Using fallback equity tokens.")
        token_list = list(FALLBACK_TOKENS)
    else:
        with open(TARGET_FILE, "r", encoding="utf-8") as f:
            target_data = json.load(f)

        for item in target_data:
            if len(token_list) >= SCREENER_LIMIT:
                break
            tok = str(item.get("token", "")).strip()
            if tok and tok not in token_list:
                token_list.append(tok)

    output_data = {"tokens": token_list}
    with open(SYMBOLS_FILE, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=4)

    print(f"[SYMBOLS LOCKED] Loaded {len(token_list)} active equity tokens into symbols.json")

if __name__ == "__main__":
    update()
