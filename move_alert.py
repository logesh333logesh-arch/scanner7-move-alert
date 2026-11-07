#!/usr/bin/env python3
"""
Scanner 7 - MOVE START ALERT (11 instruments)

Professional "volatility expansion breakout" logic on 5-min candles:
  1. Breakout   : candle CLOSES beyond the last 1-hour high/low (first close outside only)
  2. Expansion  : candle range >= 1.5 x ATR(14) and it closes near its extreme (strong close)
  3. Confirmers (need at least 1):
       - Squeeze  : the hour before was compressed (calm range vs 100-bar average)
       - Volume   : volume >= 2x 20-bar average (futures / crypto only - index volume is 0 on Yahoo)
       - Trend    : breakout direction agrees with EMA50
Then it tracks the move: alerts once when the move is EXTENDED (don't chase) and once when it FAILS.

Run:  python move_alert.py          (normal)
      python move_alert.py --test   (sends a connection test message)
Env : TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (without them, prints instead of sending)
"""
import os
import sys
import json
import datetime as dt

import numpy as np
import pandas as pd
import requests

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")
STATE_FILE = "state.json"
IST = "Asia/Kolkata"

# ---------------- tunables ----------------
INTERVAL_MIN = 5
CHANNEL_BARS = 12        # 12 x 5min = 1 hour breakout channel
MIN_SESSION_BARS = 6     # indices: no alerts in first 30 min (channel needs today's bars)
ATR_N = 14
BASE_N = 100
EXPANSION_X = 1.5        # breakout candle range >= 1.5 x ATR
STRONG_CLOSE = 0.60      # close in top/bottom 40% of the candle
SQUEEZE_RATIO = 0.85     # last-hour avg range <= 0.85 x 100-bar avg range
VOLUME_X = 2.0
EXTENDED_X = 3.0         # move extended when price is 3 ATR beyond breakout level
LATE_X = 1.5             # already 1.5 ATR away when alert is sent = late
LOOKBACK_BARS = 3        # re-check last 3 bars (GitHub cron can be late)
MAX_STALE_MIN = 20       # skip instrument if latest candle is older (market closed)
ACTIVE_MAX_HOURS = 12
BREAKOUT_BUFFER_X = 0.25  # close must clear the channel by >= 0.25 ATR (kills marginal breakouts)
FAIL_BUFFER_X = 0.5       # move 'failed' only if price goes 0.5 ATR back THROUGH the level (retests are normal)
MIN_EXTRAS_VOL = 2        # futures/crypto need 2 confirmers (MEDIUM alerts removed)

# name, yahoo symbol, kind  (index = no volume + session based; fut/crypto use volume)
# NOTE: crude/natgas/gold/silver use international futures as a direction proxy for MCX.
INSTRUMENTS = [
    ("NIFTY", "^NSEI", "index"),
    ("BANKNIFTY", "^NSEBANK", "index"),
    ("SENSEX", "^BSESN", "index"),
    ("FINNIFTY", "NIFTY_FIN_SERVICE.NS", "index"),
    ("CRUDE OIL", "CL=F", "fut"),
    ("NATURAL GAS", "NG=F", "fut"),
    ("GOLD", "GC=F", "fut"),
    ("SILVER", "SI=F", "fut"),
    ("BITCOIN", "BTC-USD", "crypto"),
    ("ETHEREUM", "ETH-USD", "crypto"),
    ("SOLANA", "SOL-USD", "crypto"),
]


# ---------------- telegram / state ----------------
def send(text):
    if not TOKEN or not CHAT:
        print("----- (not sent: no Telegram secrets) -----")
        print(text)
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data={"chat_id": CHAT, "text": text},
            timeout=20,
        )
        if r.status_code != 200:
            print("Telegram error:", r.status_code, r.text[:200])
    except Exception as e:  # noqa
        print("Telegram exception:", e)


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, ensure_ascii=False)


# ---------------- data ----------------
def fetch(sym):
    import yfinance as yf  # lazy import so logic can be tested offline

    df = yf.download(
        sym, period="5d", interval=f"{INTERVAL_MIN}m",
        progress=False, auto_adjust=False, threads=False,
    )
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["High", "Low", "Close"]).copy()
    idx = df.index
    df.index = idx.tz_localize("UTC") if idx.tz is None else idx
    df.index = df.index.tz_convert(IST)
    # drop the still-forming candle
    now = pd.Timestamp.now(tz=IST)
    step = pd.Timedelta(minutes=INTERVAL_MIN)
    df = df[df.index + step <= now + pd.Timedelta(seconds=5)]
    return df if len(df) else None


# ---------------- logic ----------------
def prepare(df):
    c, h, l = df["Close"], df["High"], df["Low"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    df["tr"] = tr
    df["atr"] = tr.rolling(ATR_N).mean().shift(1)
    df["base"] = tr.rolling(BASE_N).mean().shift(1)
    df["recent"] = tr.rolling(CHANNEL_BARS).mean().shift(1)
    df["ema50"] = c.ewm(span=50, adjust=False).mean()
    df["vavg"] = df["Volume"].rolling(20).mean().shift(1)
    df["day"] = [d for d in df.index.date]
    df["nsess"] = df.groupby("day").cumcount()
    return df


def channel(df, pos, session):
    start = pos - CHANNEL_BARS
    if session:
        k = int(df["nsess"].iloc[pos])
        if k < MIN_SESSION_BARS:
            return None
        start = pos - min(CHANNEL_BARS, k)
    if start < 0:
        return None
    w = df.iloc[start:pos]
    return float(w["High"].max()), float(w["Low"].min())


def find_events(name, kind, df):
    session = kind == "index"
    use_vol = kind != "index"
    out = []
    n = len(df)
    for pos in range(max(n - LOOKBACK_BARS, BASE_N + 1), n):
        row = df.iloc[pos]
        atr = row["atr"]
        if pd.isna(atr) or atr <= 0:
            continue
        ch = channel(df, pos, session)
        if ch is None:
            continue
        hi, lo = ch
        close = float(row["Close"])
        if close > hi:
            direction = "UP"
        elif close < lo:
            direction = "DOWN"
        else:
            continue
        clear = (close - hi) if direction == "UP" else (lo - close)
        if clear < BREAKOUT_BUFFER_X * float(atr):
            continue
        prev_close = float(df["Close"].iloc[pos - 1])
        if not (lo <= prev_close <= hi):      # only the FIRST close outside the channel
            continue
        rng = float(row["High"] - row["Low"])
        if rng <= 0 or rng < EXPANSION_X * atr:
            continue
        pos_in_bar = (close - float(row["Low"])) / rng
        if direction == "UP" and pos_in_bar < STRONG_CLOSE:
            continue
        if direction == "DOWN" and (1 - pos_in_bar) < STRONG_CLOSE:
            continue

        squeeze = bool(row["base"] > 0 and row["recent"] / row["base"] <= SQUEEZE_RATIO)
        volx = None
        vol_ok = False
        if use_vol and row["vavg"] and row["vavg"] > 0:
            volx = float(row["Volume"] / row["vavg"])
            vol_ok = volx >= VOLUME_X
        trend_ok = (close > row["ema50"]) if direction == "UP" else (close < row["ema50"])
        extras = int(squeeze) + int(vol_ok) + int(bool(trend_ok))
        if extras < (MIN_EXTRAS_VOL if use_vol else 1):
            continue
        out.append({
            "name": name, "kind": kind, "dir": direction,
            "bar": df.index[pos].isoformat(),
            "level": hi if direction == "UP" else lo,
            "close": close, "atr": float(atr), "exp": rng / float(atr),
            "bar_high": float(row["High"]), "bar_low": float(row["Low"]),
            "squeeze": squeeze, "volx": volx, "vol_ok": vol_ok, "trend_ok": bool(trend_ok),
            "extras": extras,
        })
    return out


# ---------------- messages ----------------
def strength_label(ev):
    if ev["extras"] >= 3:
        return "VERY STRONG 🔥🔥🔥"
    if ev["extras"] == 2:
        return "STRONG 🔥🔥"
    return "MEDIUM 🔥"


def msg_start(ev, price):
    up = ev["dir"] == "UP"
    lines = [f"🚀 MOVE START — {ev['name']}"]
    lines.append("⬆️ மேல போக ஆரம்பிச்சிருக்கு (UP)" if up else "⬇️ கீழ இறங்க ஆரம்பிச்சிருக்கு (DOWN)")
    if ev["kind"] == "index":
        lines.append("🟢 CALL side மட்டும் பாருங்க" if up else "🔴 PUT side மட்டும் பாருங்க")
    lines.append("━━━━━━━━━━")
    lines.append(f"💰 Price: {price:,.2f}")
    lines.append(f"📍 Breakout level: {ev['level']:,.2f}")
    lines.append(f"💪 Strength: {strength_label(ev)}")
    lines.append(f"✅ Candle size: {ev['exp']:.1f}× ATR")
    if ev["squeeze"]:
        lines.append("✅ Squeeze-க்கு அப்புறம் breakout")
    if ev["vol_ok"]:
        lines.append(f"✅ Volume: {ev['volx']:.1f}× average")
    if ev["trend_ok"]:
        lines.append("✅ Trend (EMA50) ஒத்துப்போகுது")
    stop = ev["bar_low"] if up else ev["bar_high"]
    lines.append(f"🛑 Stop idea: {stop:,.2f}")
    dist = ((price - ev["level"]) if up else (ev["level"] - price)) / ev["atr"]
    if dist > LATE_X:
        lines.append(f"⏳ லேட்: ஏற்கனவே {dist:.1f}× ATR ஓடிடுச்சு — துரத்தாதீங்க")
    else:
        lines.append("⚡ இப்போ தான் ஆரம்பம்")
    lines.append(f"🕒 {pd.Timestamp(ev['bar']).strftime('%d %b %H:%M')} IST")
    return "\n".join(lines)


def msg_extended(name, act, price):
    mult = abs(price - act["level"]) / act["atr"]
    return (f"🔥 MOVE EXTENDED — {name}\n"
            f"{mult:.1f}× ATR ஓடிடுச்சு (price {price:,.2f}).\n"
            "❌ புதுசா entry போடாதீங்க — துரத்துனா தப்பான இடம்.\n"
            "✅ ஏற்கனவே trade இருந்தா profit lock / trail பண்ணுங்க.")


def msg_failed(name, act, price):
    return (f"⚠️ MOVE FAILED / முடிஞ்சது — {name}\n"
            f"Price ({price:,.2f}) breakout level ({act['level']:,.2f}) க்கு திரும்பி வந்துடுச்சு.\n"
            "Sideways ஆகலாம் — புது entry வேண்டாம், அடுத்த alert-க்கு காத்திருங்க.")


# ---------------- main ----------------
def process(name, kind, df, st, now_utc):
    s = st.setdefault(name, {})
    price = float(df["Close"].iloc[-1])
    last_day = str(df["day"].iloc[-1])
    act = s.get("active")

    # --- manage the current move ---
    if act:
        started = dt.datetime.fromisoformat(act["started"])
        too_old = (now_utc - started).total_seconds() > ACTIVE_MAX_HOURS * 3600
        new_day = kind == "index" and act.get("day") != last_day
        if too_old or new_day:
            act = None
            s["active"] = None
    if act:
        up = act["dir"] == "UP"
        buf = FAIL_BUFFER_X * act["atr"]
        failed = price < act["level"] - buf if up else price > act["level"] + buf
        if failed:
            send(msg_failed(name, act, price))
            s["active"] = None
            act = None
        else:
            beyond = (price - act["level"]) if up else (act["level"] - price)
            if beyond >= EXTENDED_X * act["atr"] and not act.get("ext_sent"):
                send(msg_extended(name, act, price))
                act["ext_sent"] = True

    # --- look for a fresh move start ---
    seen = s.setdefault("seen", [])
    for ev in find_events(name, kind, df):
        if ev["bar"] in seen:
            continue
        seen.append(ev["bar"])
        if act and act["dir"] == ev["dir"]:
            continue  # same move continuing - alert only once per move
        send(msg_start(ev, price))
        act = {"dir": ev["dir"], "level": ev["level"], "atr": ev["atr"],
               "started": now_utc.isoformat(), "day": last_day, "ext_sent": False}
        s["active"] = act
    s["seen"] = seen[-20:]


def main():
    if "--test" in sys.argv:
        send("✅ Scanner 7 (Move Start Alert) connected.\n"
             "11 instruments: NIFTY, BANKNIFTY, SENSEX, FINNIFTY, CRUDE, NATGAS, GOLD, SILVER, BTC, ETH, SOL")
        return
    st = load_state()
    now_utc = dt.datetime.now(dt.timezone.utc)
    for name, sym, kind in INSTRUMENTS:
        try:
            df = fetch(sym)
            if df is None or len(df) < BASE_N + 20:
                print(f"{name}: not enough data")
                continue
            last_end = df.index[-1] + pd.Timedelta(minutes=INTERVAL_MIN)
            age_min = (pd.Timestamp.now(tz=IST) - last_end).total_seconds() / 60
            if age_min > MAX_STALE_MIN:
                print(f"{name}: market closed / stale ({age_min:.0f} min old)")
                continue
            df = prepare(df)
            process(name, kind, df, st, now_utc)
            print(f"{name}: ok, price {df['Close'].iloc[-1]:.2f}")
        except Exception as e:  # one bad symbol must never stop the others
            print(f"{name}: ERROR {e}")
    save_state(st)


if __name__ == "__main__":
    main()
