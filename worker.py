import time
import traceback
import sys

# Import from app.py
from app import (
    BOT_ACTIVE,
    BOT_IN_TRADE,
    pending_order_id,

    check_strategy_conditions,
    check_filled_orders,
    manage_position
)

def main():
    """
    Infinite loop: 
      - If no trade & no pending => check conditions once per minute
      - Otherwise => check every second (pending fill, SL, TSL)
    """
    last_condition_check_time = 0
    condition_check_interval = 60  # 1 minute

    while True:
        try:
            if not BOT_ACTIVE:
                time.sleep(1)
                continue

            now = time.time()

            if not BOT_IN_TRADE and pending_order_id is None:
                # No position, no pending => check strategy once per minute
                if now - last_condition_check_time >= condition_check_interval:
                    check_strategy_conditions()
                    last_condition_check_time = now
            else:
                # We have a pending order or an active trade => check every second
                check_filled_orders()
                manage_position()

            time.sleep(1)

        except Exception as e:
            print("[WORKER ERROR]", e)
            traceback.print_exc()
            # sys.exit(1)  # Let Heroku auto-restart if you want

if __name__ == "__main__":
    main()