import requests
import pandas as pd
import numpy as np
import time
from datetime import datetime
import os

# ==== CONFIG ====
BOT_TOKEN = "7828854549:AAGo8Dx9RlIs13a6dZ9I73-2u6dDvkx7LvY"  # Replace with your actual token
CHAT_ID = -1002558399674               # Group chat ID
INTERVAL_5M = "5m"
INTERVAL_15M = "15m"
LOOKBACK_CANDLES = 50
CHECK_EVERY = 300  # seconds (5 min)
LOG_FILE = "trade_signals.csv"
last_alert_time = {}  # Track alerts to avoid duplicates

# ==== TELEGRAM SEND FUNCTION ====
def send_telegram_message(msg):
    print("[DEBUG] Sending message to Telegram...")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload)
        if r.status_code != 200:
            print("[ERROR] Telegram send error:", r.text)
    except Exception as e:
        print("[ERROR] Telegram send exception:", e)

# ==== GET COINS ====
def get_top_15_symbols():
    print("[DEBUG] Fetching top 15 USDT pairs by volume...")
    url = "https://api.binance.com/api/v3/ticker/24hr"
    data = requests.get(url).json()
    df = pd.DataFrame(data)
    df['quoteVolume'] = df['quoteVolume'].astype(float)
    df = df[df['symbol'].str.endswith('USDT')]
    df = df.sort_values('quoteVolume', ascending=False).head(15)
    symbols = df['symbol'].tolist()
    print(f"[INFO] Top 15 symbols: {symbols}")
    return symbols

# ==== GET CANDLE DATA ====
def get_klines(symbol, interval, limit):
    print(f"[DEBUG] Fetching {interval} klines for {symbol}...")
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    data = requests.get(url).json()
    df = pd.DataFrame(data, columns=[
        'time','o','h','l','c','v','ct','qv','n','tbbav','tbqv','ignore'
    ])
    df['c'] = df['c'].astype(float)
    df['h'] = df['h'].astype(float)
    df['l'] = df['l'].astype(float)
    df['v'] = df['v'].astype(float)
    df['time'] = pd.to_datetime(df['time'], unit='ms')
    return df

# ==== TECHNICAL INDICATORS ====
def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def bollinger_bands(series, period=20, std_mult=2):
    sma = series.rolling(period).mean()
    std = series.rolling(period).std()
    upper = sma + (std_mult * std)
    lower = sma - (std_mult * std)
    return upper, lower

def vwap(df):
    cum_vol = df['v'].cumsum()
    cum_pv = (df['c'] * df['v']).cumsum()
    return (cum_pv / cum_vol).iloc[-1]

def calculate_atr(df, period=14):
    hl = df['h'] - df['l']
    hc = np.abs(df['h'] - df['c'].shift())
    lc = np.abs(df['l'] - df['c'].shift())
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(period).mean().iloc[-1]

def trend_filter_15m(df):
    ema30 = ema(df['c'], 30)
    ema90 = ema(df['c'], 90)
    if ema30.iloc[-1] > ema90.iloc[-1]:
        return "up"
    elif ema30.iloc[-1] < ema90.iloc[-1]:
        return "down"
    else:
        return "sideways"

# ==== CHECK SIGNAL ====
def check_signal(symbol):
    print(f"[DEBUG] Checking signal for {symbol}...")
    df5m = get_klines(symbol, INTERVAL_5M, LOOKBACK_CANDLES)
    df15m = get_klines(symbol, INTERVAL_15M, LOOKBACK_CANDLES)
    close = df5m['c']
    latest_time = df5m['time'].iloc[-1]
    ema_fast = ema(close, 9)
    ema_slow = ema(close, 30)
    rsi_val = rsi(close, 14)
    bb_upper, bb_lower = bollinger_bands(close, 20, 2)
    vwap_val = vwap(df5m)
    atr_val = calculate_atr(df5m)
    trend15 = trend_filter_15m(df15m)
    ema_fast_prev, ema_slow_prev = ema_fast.iloc[-2], ema_slow.iloc[-2]
    ema_fast_last, ema_slow_last = ema_fast.iloc[-1], ema_slow.iloc[-1]
    rsi_last = rsi_val.iloc[-1]
    close_last = close.iloc[-1]
    bb_upper_last, bb_lower_last = bb_upper.iloc[-1], bb_lower.iloc[-1]
    confidence = 0
    direction = None
    # BUY Setup
    if ema_fast_prev < ema_slow_prev and ema_fast_last > ema_slow_last and rsi_last > 50:
        confidence += 1
        if close_last < bb_lower_last:
            confidence += 1
        if close_last < vwap_val:
            confidence += 1
        if trend15 == "up":
            confidence += 1
        direction = "BUY"
    # SELL Setup
    elif ema_fast_prev > ema_slow_prev and ema_fast_last < ema_slow_last and rsi_last < 50:
        confidence += 1
        if close_last > bb_upper_last:
            confidence += 1
        if close_last > vwap_val:
            confidence += 1
        if trend15 == "down":
            confidence += 1
        direction = "SELL"
    if direction and confidence >= 3:
        if last_alert_time.get(symbol) == latest_time:
            print(f"[INFO] Duplicate alert skipped for {symbol}")
            return None
        last_alert_time[symbol] = latest_time
        if direction == "BUY":
            sl = round(close_last - atr_val, 4)
            tp = round(close_last + 2 * atr_val, 4)
        else:
            sl = round(close_last + atr_val, 4)
            tp = round(close_last - 2 * atr_val, 4)
        conf_level = "High" if confidence == 4 else "Medium"
        log_trade(symbol, direction, close_last, sl, tp, conf_level, confidence, trend15, latest_time)
        msg = (f"*{direction} SIGNAL* — `{symbol}` @ {close_last}\n"
               f"SL: `{sl}` | TP: `{tp}`\n"
               f"Confidence: *{conf_level}* ({confidence}/4)\n"
               f"Trend: *{trend15}* (15m)\n"
               f"Timeframe: 5m | {latest_time.strftime('%Y-%m-%d %H:%M')}")
        return msg
    print(f"[INFO] No valid signal for {symbol}")
    return None

# ==== LOGGING ====
def log_trade(symbol, direction, price, sl, tp, conf_level, confidence, trend, timestamp):
    entry = {
        "time": timestamp,
        "symbol": symbol,
        "direction": direction,
        "price": price,
        "stop_loss": sl,
        "take_profit": tp,
        "confidence": conf_level,
        "confidence_score": confidence,
        "trend": trend
    }
    print(entry)     # DEBUG: Makes sure entry is a dict
    df = pd.DataFrame([entry])
    file_exists = os.path.exists(LOG_FILE)
    df.to_csv(LOG_FILE, mode='a', header=not file_exists, index=False)
    print(f"[DEBUG] Logged trade for {symbol}")

# ==== MAIN LOOP ====
if __name__ == "__main__":
    print("🚀 Scalping Alert Bot Started...")
    while True:
        try:
            symbols = get_top_15_symbols()
            found_signal = False
            for sym in symbols:
                signal = check_signal(sym)
                if signal:
                    send_telegram_message(signal)
                    print(f"[ALERT] {signal}")
                    found_signal = True
            if not found_signal:
                print("[INFO] No entries found in this check. Sending alert...")
                send_telegram_message("⚠ No entry signals found in the last 5 min check.")
        except Exception as e:
            print("[ERROR] Main loop exception:", e)
        print(f"[DEBUG] Waiting {CHECK_EVERY} seconds until next scan...\n")
        time.sleep(CHECK_EVERY)
