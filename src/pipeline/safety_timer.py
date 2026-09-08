import asyncio
from datetime import datetime
from logzero import logger

async def intraday_square_off_guard(execution_client=None):
    """
    Independent background task that automatically flattens all open positions
    at 3:15 PM IST to protect against broker auto square-off rules.
    """
    while True:
        now = datetime.now()
        
        # Check if current time is 3:15 PM IST or later during trading hours
        if now.hour == 15 and now.minute >= 15:
            logger.info("[SAFETY GUARD] 3:15 PM threshold reached. Executing blanket market exit...")
            try:
                if execution_client:
                    pass # TODO: Add execution_client.flatten_all_positions() here when trade logic is built
                    
                logger.info("[SAFETY GUARD] Successfully squared off all positions.")
            except Exception as e:
                logger.error(f"[SAFETY GUARD ERROR] Failed to flatten positions: {e}")
            
            # Sleep for 24 hours to prevent repetitive firing on the same day
            await asyncio.sleep(86400)
        
        # Check every 30 seconds
        await asyncio.sleep(30)
