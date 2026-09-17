import json
import os

TARGET_FILE = "/home/ubuntu/project_pipeline/target_ticks.json"
SYMBOLS_FILE = "/home/ubuntu/project_pipeline/symbols.json"

def update():
    if not os.path.exists(TARGET_FILE):
        print("Screener file not found. Keeping yesterday's symbols.")
        return

    with open(TARGET_FILE, 'r') as f:
        target_data = json.load(f)

    # Slice the first 12 items from the screener list
    top_12 = target_data[:12]

    # Extract ONLY the token strings from those 12 items
    token_list = [item["token"] for item in top_12]

    # Format it exactly how the WebSocket expects {"tokens": [...]}
    output_data = {"tokens": token_list}

    # Overwrite the symbols.json file
    with open(SYMBOLS_FILE, 'w') as f:
        json.dump(output_data, f, indent=4)

    print(f"Successfully loaded {len(token_list)} tokens into symbols.json")

if __name__ == "__main__":
    update()
