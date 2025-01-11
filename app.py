import os
import sys
import time
import hmac
import hashlib
import json
import logging
import traceback
from datetime import datetime, timedelta, time as dt_time

import requests
from flask import Flask, request, render_template_string, redirect, url_for

###############################################################################
#                         LOGGING CONFIG (OPTIONAL)                           #
###############################################################################
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout
)

###############################################################################
#                     GLOBAL CONFIG & BOT STATE VARIABLES                     #
###############################################################################
API_KEY = os.getenv("COINDCX_API_KEY", "<YOUR_COINDCX_API_KEY>")
API_SECRET = os.getenv("COINDCX_API_SECRET", "<YOUR_COINDCX_API_SECRET>")

# For USD-M Perpetual on CoinDCX, e.g. "B-ETH_USDT"
TRADE_SYMBOL = os.getenv("TRADE_SYMBOL", "B-ETH_USDT")

ORDER_SIZE = float(os.getenv("ORDER_SIZE", 1.0))
STOP_LOSS_DISTANCE = float(os.getenv("SL_DISTANCE", 25.0))
TSL_STEP = float(os.getenv("TSL_STEP", 10.0))
MIN_PROFIT_FOR_BREAKEVEN = float(os.getenv("BE_PROFIT", 25.0))
TIMEFRAME = os.getenv("TIMEFRAME", "1m")

LEVERAGE = float(os.getenv("LEVERAGE", 1.0))  # Adjust from control panel

# Session times (in IST), default 08:00 to 05:00 next day (21h)
TRADING_SESSION_START = os.getenv("SESSION_START", "08:00")
TRADING_SESSION_END   = os.getenv("SESSION_END",   "05:00")

BOT_ACTIVE = True
BOT_IN_TRADE = False

current_position_side = None  # "buy"/"sell"
entry_price = None
stop_loss_price = None

pending_order_id = None
pending_order_side = None
pending_trigger_price = None

ERROR_COUNT = 0
MAX_ERROR_RETRIES = 2

app = Flask(__name__)

###############################################################################
#                        COINDCX API ENDPOINTS & URLs                         #
###############################################################################
COINDCX_API_BASE = "https://api.coindcx.com"
COINDCX_CREATE_ORDER = f"{COINDCX_API_BASE}/exchange/v1/orders/create"
COINDCX_CANCEL_ORDER = f"{COINDCX_API_BASE}/exchange/v1/orders/cancel"
COINDCX_GET_ORDERS   = f"{COINDCX_API_BASE}/exchange/v1/orders"
COINDCX_CANDLES      = f"{COINDCX_API_BASE}/market_data/candles"

###############################################################################
#                           HELPER / API FUNCTIONS                            #
###############################################################################
def generate_signature(payload: dict) -> dict:
    json_payload = json.dumps(payload, separators=(',', ':'))
    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        json_payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-AUTH-APIKEY": API_KEY,
        "X-AUTH-SIGNATURE": signature
    }

def safe_request(method, url, **kwargs):
    global ERROR_COUNT
    for attempt in range(MAX_ERROR_RETRIES):
        try:
            resp = requests.request(method, url, timeout=5, **kwargs)
            resp.raise_for_status()
            return resp.json() if resp.text else {}
        except Exception as e:
            logging.warning(f"[safe_request] Attempt {attempt+1} => {e}")
            time.sleep(1)

    ERROR_COUNT += 1
    logging.error("[CRITICAL] Max retries exceeded. Forcing close & exit.")
    force_close_position()
    sys.exit(1)

def force_close_position():
    global BOT_IN_TRADE, current_position_side, entry_price, stop_loss_price
    global pending_order_id, pending_order_side, pending_trigger_price

    logging.error("[FORCE CLOSE] Cancelling pending & closing position if open.")
    if pending_order_id:
        cancel_order(pending_order_id)
        pending_order_id = None
        pending_order_side = None
        pending_trigger_price = None

    if BOT_IN_TRADE and current_position_side in ("buy", "sell"):
        side_to_close = "sell" if current_position_side == "buy" else "buy"
        last_price = get_latest_price()
        if last_price:
            place_order(
                side=side_to_close,
                price=last_price,
                trigger_price=last_price,
                quantity=ORDER_SIZE,
                order_type="market"
            )

    BOT_IN_TRADE = False
    current_position_side = None
    entry_price = None
    stop_loss_price = None

def cancel_order(order_id: str):
    payload = {"id": order_id}
    headers = generate_signature(payload)
    return safe_request("POST", COINDCX_CANCEL_ORDER, headers=headers, json=payload)

def place_order(side: str,
                price: float,
                trigger_price: float,
                quantity: float,
                order_type="stop_limit"):
    timestamp = int(round(time.time() * 1000))
    payload = {
        "side": side.lower(),
        "order_type": order_type,
        "market": TRADE_SYMBOL,  # e.g., "B-ETH_USDT"
        "price_per_unit": str(price),
        "trigger_price": str(trigger_price),
        "total_quantity": str(quantity),
        "timestamp": timestamp,
        # If CoinDCX futures require a "leverage" param (check official docs):
        "leverage": str(LEVERAGE)
    }
    headers = generate_signature(payload)
    return safe_request("POST", COINDCX_CREATE_ORDER, headers=headers, json=payload)

def get_candles(symbol: str, interval: str, limit=20):
    end_time = int(time.time() * 1000)
    start_time = end_time - (limit * 60 * 1000)
    params = {
        "pair": symbol,
        "interval": interval,
        "limit": limit,
        "startTime": start_time,
        "endTime": end_time
    }
    try:
        resp = requests.get(COINDCX_CANDLES, params=params, timeout=5)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logging.error(f"[get_candles] Error: {e}")
        return None

def get_latest_price():
    candles = get_candles(TRADE_SYMBOL, TIMEFRAME, limit=1)
    if candles and len(candles) > 0:
        return float(candles[-1][4])
    return None

###############################################################################
#              TIME CONVERSION (IST -> UTC) FOR SESSION MANAGEMENT           #
###############################################################################
def ist_time_to_utc_time(ist_str):
    """
    Convert an "HH:MM" string in IST to a Python time object in UTC.
    For example, "08:00" IST -> 02:30 UTC (on the same day).
    """
    # Parse the IST string
    ist_dt = datetime.strptime(ist_str, "%H:%M")
    # The date is arbitrary for time-of-day usage; we just need the offset
    # IST = UTC + 5:30 => So to convert IST -> UTC, subtract 5:30
    # We'll do this on a random date reference, e.g., 1970-01-01
    dummy_date = datetime(1970, 1, 1, ist_dt.hour, ist_dt.minute)
    # Subtract 5.5 hours
    utc_date = dummy_date - timedelta(hours=5, minutes=30)
    return dt_time(utc_date.hour, utc_date.minute)

def is_in_trading_session():
    """
    The user sets TRADING_SESSION_START, TRADING_SESSION_END in IST.
    We:
      1) Convert them to UTC times
      2) Compare with the current UTC time
      3) If crossing midnight, handle wrap logic
    """
    # Current UTC time of day
    utc_now = datetime.utcnow()
    current_utc_time = dt_time(utc_now.hour, utc_now.minute)

    start_utc_time = ist_time_to_utc_time(TRADING_SESSION_START)  # time obj in UTC
    end_utc_time   = ist_time_to_utc_time(TRADING_SESSION_END)

    # If end < start => session crosses midnight in IST
    # Example: 08:00 IST -> 02:30 UTC, 05:00 IST -> 23:30 UTC (previous day).
    # So if session crosses midnight, we consider "if current_time >= start_utc_time or current_time < end_utc_time"
    # Otherwise "start <= current < end"
    if end_utc_time < start_utc_time:
        # crosses midnight
        if current_utc_time >= start_utc_time or current_utc_time < end_utc_time:
            return True
        else:
            return False
    else:
        # normal
        return (start_utc_time <= current_utc_time < end_utc_time)

###############################################################################
#                          INDICATORS (VWAP, ATR, ST)                         #
###############################################################################
def compute_vwap(candles):
    total_tpv = 0.0
    total_vol = 0.0
    for c in candles:
        high, low, close, vol = float(c[2]), float(c[3]), float(c[4]), float(c[5])
        typ_price = (high + low + close) / 3.0
        total_tpv += (typ_price * vol)
        total_vol += vol
    if total_vol == 0:
        return None
    return total_tpv / total_vol

def compute_atr(candles, period=7):
    trs = []
    for i in range(1, len(candles)):
        high = float(candles[i][2])
        low = float(candles[i][3])
        prev_close = float(candles[i-1][4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    atrs = []
    for i in range(len(trs)):
        if i < period:
            atrs.append(None)
        else:
            chunk = trs[i-period+1 : i+1]
            atrs.append(sum(chunk)/period)
    return atrs

def compute_supertrend(candles, period=7, multiplier=2):
    if len(candles) < period+1:
        return ["red"] * len(candles)
    atr_vals = compute_atr(candles, period)
    st_colors = []
    for i in range(len(candles)):
        if i == 0 or atr_vals[i-1] is None:
            st_colors.append("red")
            continue
        high = float(candles[i][2])
        low = float(candles[i][3])
        close = float(candles[i][4])
        hl2 = (high + low) / 2.0
        cur_atr = atr_vals[i-1]

        ub = hl2 + multiplier * cur_atr
        lb = hl2 - multiplier * cur_atr
        if close > ub:
            st_colors.append("green")
        elif close < lb:
            st_colors.append("red")
        else:
            st_colors.append(st_colors[-1])
    return st_colors

###############################################################################
#                           BOT STRATEGY & LOGIC                              #
###############################################################################
def check_strategy_conditions():
    global pending_order_id, pending_order_side, pending_trigger_price

    if not BOT_ACTIVE:
        return

    if not is_in_trading_session():
        # Cancel any pending if outside session
        if pending_order_id:
            logging.info("[check_strategy_conditions] Outside session -> cancel pending.")
            cancel_order(pending_order_id)
            pending_order_id = None
            pending_order_side = None
            pending_trigger_price = None
        return

    candles = get_candles(TRADE_SYMBOL, TIMEFRAME, limit=20)
    if not candles or len(candles) < 4:
        logging.warning("[check_strategy_conditions] Not enough candles or fetch error.")
        return

    # Simple VWAP from last 5 candles
    vwap_val = compute_vwap(candles[-5:])
    st_colors = compute_supertrend(candles, period=7, multiplier=2)
    if not vwap_val or not st_colors:
        logging.warning("[check_strategy_conditions] Missing VWAP or ST data.")
        return

    c2 = candles[-2]
    c3 = candles[-3]
    close2 = float(c2[4])
    close3 = float(c3[4])
    st_color2 = st_colors[-2]

    # Example conditions
    buy_cond  = (close3 > vwap_val and close2 > vwap_val and st_color2 == "green")
    sell_cond = (close3 < vwap_val and close2 < vwap_val and st_color2 == "red")

    if buy_cond:
        last_high = float(c2[2])
        resp = place_order("buy", last_high, last_high, ORDER_SIZE)
        if resp and "id" in resp:
            pending_order_id = resp["id"]
            pending_order_side = "buy"
            pending_trigger_price = last_high
            logging.info(f"[BUY SETUP] Placed stop-limit near {last_high}")

    elif sell_cond:
        last_low = float(c2[3])
        resp = place_order("sell", last_low, last_low, ORDER_SIZE)
        if resp and "id" in resp:
            pending_order_id = resp["id"]
            pending_order_side = "sell"
            pending_trigger_price = last_low
            logging.info(f"[SELL SETUP] Placed stop-limit near {last_low}")

def check_filled_orders():
    global BOT_IN_TRADE, current_position_side, entry_price, stop_loss_price
    global pending_order_id, pending_order_side

    if not pending_order_id:
        return

    headers = generate_signature({})
    resp = safe_request("POST", COINDCX_GET_ORDERS, headers=headers, json={})
    if not resp:
        return

    for od in resp:
        if od["id"] == pending_order_id:
            status = od["status"].lower()
            logging.info(f"[check_filled_orders] Order {od['id']} => {status}")
            if status == "filled":
                BOT_IN_TRADE = True
                current_position_side = od["side"].lower()
                entry_price = float(od["price_per_unit"])
                if current_position_side == "buy":
                    stop_loss_price = entry_price - STOP_LOSS_DISTANCE
                else:
                    stop_loss_price = entry_price + STOP_LOSS_DISTANCE

                pending_order_id = None
                pending_order_side = None
                logging.info(f"[ORDER FILLED] side={current_position_side}, entry={entry_price}")

            elif status in ["cancelled", "rejected"]:
                logging.info("[ORDER CANCEL/REJECT] Freed to place new orders.")
                pending_order_id = None
                pending_order_side = None

def manage_position():
    global BOT_IN_TRADE, current_position_side, entry_price, stop_loss_price
    global pending_order_id

    # Always check if pending just got filled
    check_filled_orders()

    if not BOT_IN_TRADE:
        return

    latest_price = get_latest_price()
    if not latest_price:
        return

    if current_position_side == "buy":
        profit = latest_price - entry_price
    else:
        profit = entry_price - latest_price

    # Move SL to break-even
    if profit >= MIN_PROFIT_FOR_BREAKEVEN:
        be_sl = entry_price
        if current_position_side == "buy" and stop_loss_price < be_sl:
            stop_loss_price = be_sl
            logging.info("[manage_position] Moved SL to break-even.")
        elif current_position_side == "sell" and stop_loss_price > be_sl:
            stop_loss_price = be_sl
            logging.info("[manage_position] Moved SL to break-even.")

    # Stepwise TSL
    if profit > MIN_PROFIT_FOR_BREAKEVEN:
        steps = int((profit - MIN_PROFIT_FOR_BREAKEVEN) // TSL_STEP)
        if current_position_side == "buy":
            new_sl = entry_price + steps * TSL_STEP
            if stop_loss_price < new_sl:
                stop_loss_price = new_sl
                logging.info(f"[TSL] Moved SL up to {stop_loss_price}")
        else:
            new_sl = entry_price - steps * TSL_STEP
            if stop_loss_price > new_sl:
                stop_loss_price = new_sl
                logging.info(f"[TSL] Moved SL down to {stop_loss_price}")

    # Check if SL is hit
    if current_position_side == "buy" and latest_price <= stop_loss_price:
        logging.info("[STOP LOSS HIT] Exiting BUY.")
        place_order("sell", latest_price, latest_price, ORDER_SIZE, order_type="market")
        if pending_order_id:
            cancel_order(pending_order_id)
            pending_order_id = None
        BOT_IN_TRADE = False
        current_position_side = None
        entry_price = None
        stop_loss_price = None

    elif current_position_side == "sell" and latest_price >= stop_loss_price:
        logging.info("[STOP LOSS HIT] Exiting SELL.")
        place_order("buy", latest_price, latest_price, ORDER_SIZE, order_type="market")
        if pending_order_id:
            cancel_order(pending_order_id)
            pending_order_id = None
        BOT_IN_TRADE = False
        current_position_side = None
        entry_price = None
        stop_loss_price = None

###############################################################################
#             FLASK ROUTES: CONTROL PANEL (HTML) & JSON-BASED CONFIG          #
###############################################################################
CONTROL_PANEL_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>CoinDCX Futures Bot Control Panel</title>
</head>
<body>
    <h1>Futures Bot Control Panel (Symbol: {{ trade_symbol }})</h1>
    <form method="POST" action="/control_panel">
        <p>
            <label>Bot Status:</label>
            <input type="radio" name="bot_active" value="true" {% if bot_active %}checked{% endif %}>Start
            <input type="radio" name="bot_active" value="false" {% if not bot_active %}checked{% endif %}>Stop
        </p>
        <p>
            <label>Order Size:</label>
            <input type="number" step="0.001" name="order_size" value="{{ order_size }}">
        </p>
        <p>
            <label>Stop Loss Distance ($):</label>
            <input type="number" step="0.01" name="stop_loss_distance" value="{{ stop_loss_distance }}">
        </p>
        <p>
            <label>TSL Step ($):</label>
            <input type="number" step="0.01" name="tsl_step" value="{{ tsl_step }}">
        </p>
        <p>
            <label>Leverage (X):</label>
            <input type="number" step="0.1" name="leverage" value="{{ leverage }}">
        </p>
        <p>
            <label>Session Start (IST):</label>
            <input type="text" name="session_start" value="{{ session_start }}">
        </p>
        <p>
            <label>Session End (IST):</label>
            <input type="text" name="session_end" value="{{ session_end }}">
        </p>
        <p>
            <label>Timeframe:</label>
            <input type="text" name="timeframe" value="{{ timeframe }}">
        </p>
        <p>
            <button type="submit">Update Settings</button>
        </p>
    </form>
    <hr>
    <h3>Current Bot State</h3>
    <ul>
        <li>BOT_ACTIVE: {{ bot_active }}</li>
        <li>BOT_IN_TRADE: {{ bot_in_trade }}</li>
        <li>Position Side: {{ current_side }}</li>
        <li>Entry Price: {{ entry_price }}</li>
        <li>Stop Loss Price: {{ stop_loss_price }}</li>
        <li>Pending Order ID: {{ pending_oid }}</li>
        <li>Pending Order Side: {{ pending_side }}</li>
        <li>Leverage: {{ leverage }}x</li>
    </ul>
</body>
</html>
"""

@app.route("/")
def index():
    return "CoinDCX Futures Bot is running. Go to /control_panel for settings."

@app.route("/control_panel", methods=["GET"])
def get_control_panel():
    return render_template_string(
        CONTROL_PANEL_HTML,
        trade_symbol=TRADE_SYMBOL,
        bot_active=BOT_ACTIVE,
        bot_in_trade=BOT_IN_TRADE,
        current_side=current_position_side,
        entry_price=entry_price,
        stop_loss_price=stop_loss_price,
        pending_oid=pending_order_id,
        pending_side=pending_order_side,
        order_size=ORDER_SIZE,
        stop_loss_distance=STOP_LOSS_DISTANCE,
        tsl_step=TSL_STEP,
        leverage=LEVERAGE,
        session_start=TRADING_SESSION_START,
        session_end=TRADING_SESSION_END,
        timeframe=TIMEFRAME
    )

@app.route("/control_panel", methods=["POST"])
def post_control_panel():
    global BOT_ACTIVE, ORDER_SIZE, STOP_LOSS_DISTANCE, TSL_STEP, LEVERAGE
    global TRADING_SESSION_START, TRADING_SESSION_END, TIMEFRAME

    form = request.form
    BOT_ACTIVE = (form.get("bot_active", "false") == "true")
    ORDER_SIZE = float(form.get("order_size", "1.0"))
    STOP_LOSS_DISTANCE = float(form.get("stop_loss_distance", "25.0"))
    TSL_STEP = float(form.get("tsl_step", "10.0"))
    LEVERAGE = float(form.get("leverage", "1.0"))
    TRADING_SESSION_START = form.get("session_start", "08:00")
    TRADING_SESSION_END   = form.get("session_end",   "05:00")
    TIMEFRAME = form.get("timeframe", "1m")

    logging.info("[Control Panel] Updated config from HTML form.")
    return redirect(url_for("get_control_panel"))

@app.route("/control", methods=["POST"])
def control_json():
    """
    JSON-based config if you prefer:
    {
      "bot_active": true,
      "order_size": 2.0,
      "stop_loss_distance": 30,
      "tsl_step": 10,
      "leverage": 5.0,
      "session_start": "08:00",
      "session_end": "05:00",
      "timeframe": "1m"
    }
    """
    global BOT_ACTIVE, ORDER_SIZE, STOP_LOSS_DISTANCE, TSL_STEP, LEVERAGE
    global TRADING_SESSION_START, TRADING_SESSION_END, TIMEFRAME

    data = request.json or {}
    if "bot_active" in data:
        BOT_ACTIVE = bool(data["bot_active"])
    if "order_size" in data:
        ORDER_SIZE = float(data["order_size"])
    if "stop_loss_distance" in data:
        STOP_LOSS_DISTANCE = float(data["stop_loss_distance"])
    if "tsl_step" in data:
        TSL_STEP = float(data["tsl_step"])
    if "leverage" in data:
        LEVERAGE = float(data["leverage"])
    if "session_start" in data:
        TRADING_SESSION_START = data["session_start"]
    if "session_end" in data:
        TRADING_SESSION_END = data["session_end"]
    if "timeframe" in data:
        TIMEFRAME = data["timeframe"]

    logging.info("[Control JSON] Updated config via JSON.")
    return {"message": "Config updated"}, 200