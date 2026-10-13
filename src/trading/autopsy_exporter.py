import os
import pandas as pd
from datetime import datetime

def export_autopsy_to_excel(autopsy_records: list, base_dir: str = None) -> str:
    """
    Takes in-memory autopsy records, sorts them by security_id and timestamp,
    and exports a formatted Excel file for post-trade analysis.
    """
    if not autopsy_records:
        print("[AUTOPSY] No trade attempts or signals were generated today. Skipping export.")
        return ""

    # Ensure output is placed into src/trading/logs
    if not base_dir:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    logs_dir = os.path.join(base_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)

    df = pd.DataFrame(autopsy_records)

    # 1. Chronological and Token Sort
    df.sort_values(by=["security_id", "timestamp_ist"], ascending=[True, True], inplace=True)

    date_str = datetime.now().strftime("%Y%m%d")
    output_path = os.path.join(logs_dir, f"trade_autopsy_{date_str}.xlsx")

    # 2. Write to Excel with Clean Column Sizing
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Trade_Autopsy")
        worksheet = writer.sheets["Trade_Autopsy"]

        for col in worksheet.columns:
            max_len = max(len(str(cell.value or "")) for cell in col)
            col_letter = col[0].column_letter
            worksheet.column_dimensions[col_letter].width = max(max_len + 3, 12)

    print(f"\n[AUTOPSY SUCCESS] Exported {len(df)} signal traces to: {output_path}")
    return output_path