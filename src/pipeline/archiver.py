"""Extracts ticks older than 2 days to CSV and purges them from PostgreSQL."""
import asyncio
import os
import datetime
import asyncpg
from dotenv import load_dotenv

load_dotenv()

async def run_archival():
    # Ensure the local CSV storage folder exists
    archive_dir = os.path.join(os.path.dirname(__file__), "../../historical_csv_data")
    os.makedirs(archive_dir, exist_ok=True)

    print("Connecting to PostgreSQL...")
    try:
        conn = await asyncpg.connect(
            user=os.getenv("DB_USER", "postgres"),
            password=os.getenv("DB_PASS", "postgres"),
            database=os.getenv("DB_NAME", "postgres"),
            host=os.getenv("DB_HOST", "localhost")
        )
    except Exception as e:
        print(f"Database connection failed: {e}")
        return

    # Define the cutoff: Anything older than 2 days
    print("Calculating records older than 48 hours...")
    
    # Check how many records are about to be archived
    count_query = "SELECT COUNT(*) FROM market_ticks WHERE timestamp < NOW() - INTERVAL '2 days'"
    old_record_count = await conn.fetchval(count_query)

    if old_record_count == 0:
        print("No records older than 2 days found. Database is already clean!")
        await conn.close()
        return

    print(f"Found {old_record_count} old ticks. Initiating CSV export...")

    # Generate a timestamped filename for the CSV
    today_str = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
    csv_filepath = os.path.join(archive_dir, f"ticks_archive_{today_str}.csv")

    # 1. Export to CSV using Postgres's lightning-fast COPY command
    export_query = "SELECT * FROM market_ticks WHERE timestamp < NOW() - INTERVAL '2 days' ORDER BY timestamp ASC"
    await conn.copy_from_query(export_query, output=csv_filepath, format='csv', header=True)
    
    print(f"Successfully saved to: {csv_filepath}")

    # 2. Delete the exported records from the hot database to free up RAM/Storage
    print("Deleting archived records from PostgreSQL...")
    delete_query = "DELETE FROM market_ticks WHERE timestamp < NOW() - INTERVAL '2 days'"
    await conn.execute(delete_query)

    print("Archival complete. Database is optimized.")
    await conn.close()

if __name__ == "__main__":
    asyncio.run(run_archival())
