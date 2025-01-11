import time
import traceback
import sys

from app import (
    BOT_ACTIVE,
    BOT_IN_TRADE,
    pending_order_id,

    check_strategy_conditions,
    check_filled_orders,
    manage_position,
    session_just_ended,
    force_close_position
)

def main():
    """
    - If session just ended, force close positions and pending orders
    - If not in trade & no pending => check conditions once per minute
    - If pending or in trade => check every second
    """
    last_condition_check = 0
    check_interval = 60  # 1 minute

    while True:
        try:
            if not BOT_ACTIVE:
                # Bot inactive => do nothing except see if session ended
                # but if the session ended we still want to close any open position
                if session_just_ended():
                    force_close_position()
                time.sleep(1)
                continue

            # Check if the session ended just now => force close
            if session_just_ended():
                force_close_position()

            now = time.time()

            # If no trade & no pending => check conditions once/minute
            if not BOT_IN_TRADE and pending_order_id is None:
                if now - last_condition_check >= check_interval:
                    check_strategy_conditions()
                    last_condition_check = now
            else:
                # We have a pending order or an active trade => second-by-second checks
                check_filled_orders()
                manage_position()

            time.sleep(1)

        except Exception as e:
            print("[WORKER ERROR]", e)
            traceback.print_exc()
            # sys.exit(1)  # Let Heroku auto-restart if you want

if __name__=="__main__":
    main()