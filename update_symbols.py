import json
import os

TARGET_FILE = "/home/ubuntu/project_pipeline/target_ticks.json"
SYMBOLS_FILE = "/home/ubuntu/project_pipeline/symbols.json"

# 5 Permanent Core Tokens: NIFTY 50, NIFTY BANK, NIFTY IT, RELIANCE, HDFCBANK
HARDCODED_TOKENS = [
    "99926000",  # NIFTY 50
    "99926009",  # NIFTY BANK
    "99926002",  # NIFTY IT
    "2885",      # RELIANCE-EQ
    "1333"       # HDFCBANK-EQ
]

SCREENER_LIMIT = 25

def update():
    # Start with the 5 permanent tokens
    token_list = list(HARDCODED_TOKENS)

    if not os.path.exists(TARGET_FILE):
        print("Screener file not found. Writing only the 5 hardcoded symbols.")
    else:
        with open(TARGET_FILE, 'r') as f:
            target_data = json.load(f)

        # Take 25 unique tokens from target_ticks.json
        added_from_screener = 0
        for item in target_data:
            if added_from_screener >= SCREENER_LIMIT:
                break
            tok = str(item["token"])
            if tok not in token_list:
                token_list.append(tok)
                added_from_screener += 1

    # Format exactly how the WebSocket expects: {"tokens": [...]}
    output_data = {"tokens": token_list}

    with open(SYMBOLS_FILE, 'w') as f:
        json.dump(output_data, f, indent=4)

    print(f"Successfully loaded {len(token_list)} tokens into symbols.json "
          f"({len(HARDCODED_TOKENS)} hardcoded + {len(token_list) - len(HARDCODED_TOKENS)} screened)")

if __name__ == "__main__":
    update()
