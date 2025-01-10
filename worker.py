import time
import traceback
import sys

# Import the global variables and functions from app.py
from app import (
    BOT_ACTIVE,
    BOT_IN_TRADE,
    pending_order_id,

    # Bot logic methods
    check_strategy_conditions,
    check_filled_orders,
    manage_position
)

def main():
    """
    Infinite loop orchestrating minute-check vs second-check logic.
    """
    last_condition_check_time = 0
    condition_check_interval = 60  # 1 minute

    while True:
        try:
            # If the bot is inactive, just sleep 1 second
            if not BOT_ACTIVE:
                time.sleep(1)
                continue

            now = time.time()

            # If no trade and no pending, check conditions once per minute
            if not BOT_IN_TRADE and pending_order_id is None:
                if now - last_condition_check_time >= condition_check_interval:
                    check_strategy_conditions()
                    last_condition_check_time = now
            else:
                # If we have a pending order or an active trade,
                # run second-by-second checks
                check_filled_orders()
                manage_position()

            # Sleep 1 second every loop iteration
            time.sleep(1)

        except Exception as e:
            print("[WORKER ERROR]", e)
            traceback.print_exc()
            # Optionally exit so Heroku restarts the worker:
            # sys.exit(1)

if __name__ == "__main__":
    main()