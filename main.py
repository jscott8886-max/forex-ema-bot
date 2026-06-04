"""
ForexAI Bot 1 - EMA + RSI + Bollinger Bands + MACD Strategy
Pairs: EUR/USD, GBP/USD, USD/JPY via OANDA API
"""
import os, time, logging, json, math
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, request
from flask_cors import CORS
import requests as req_lib
import pandas as pd
import numpy as np
import oandapyV20
import oandapyV20.endpoints.accounts as accounts
import oandapyV20.endpoints.orders as orders
import oandapyV20.endpoints.trades as trades
import oandapyV20.endpoints.positions as positions
import oandapyV20.endpoints.instruments as instruments
from oandapyV20.contrib.requests import MarketOrderRequest

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

API_KEY     = os.getenv("OANDA_API_KEY", "")
ACCOUNT_ID  = os.getenv("OANDA_ACCOUNT_ID", "")
PAPER_MODE  = os.getenv("PAPER_MODE", "true").lower() == "true"
ENVIRONMENT = "practice" if PAPER_MODE else "live"
PAIRS       = ["EUR_USD", "GBP_USD", "USD_JPY"]
STATE_FILE  = "/tmp/forex_ema_state.json"

# Pip multipliers for P&L calculation
PIP_MULT = {"EUR_USD": 10000, "GBP_USD": 10000, "USD_JPY": 100}

STRATEGY = {
    "ema_fast":         9,
    "ema_slow":         21,
    "ema_trend":        50,
    "rsi_period":       14,
    "rsi_oversold":     35,
    "rsi_overbought":   65,
    "bb_period":        20,
    "bb_std":           2.0,
    "bb_min_bandwidth": 0.05,   # % — forex moves in much smaller ranges than crypto
    "stop_loss_pips":   15,     # pips
    "take_profit_pips": 30,     # pips
    "position_units":   10000,  # mini lot
    "min_score":        4,
    "cooldown_minutes": 15,
    "enabled_regime_filter": True,
}

bot_state = {
    "running":          True,
    "killed":           False,
    "positions":        {},
    "closed_trades":    [],
    "diary":            [],
    "day_pnl":          0.0,
    "total_trades":     0,
    "win_count":        0,
    "account_balance":  0.0,
    "account_equity":   0.0,
    "account_nav":      0.0,
    "signals":          {},
    "market_open":      False,
    "cooldowns":        {},
    "last_signal_data": {},
}

# ── Persistence ────────────────────────────────────────────────────────────────
def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "diary":         bot_state["diary"][-200:],
                "closed_trades": bot_state["closed_trades"][-100:],
                "day_pnl":       bot_state["day_pnl"],
                "total_trades":  bot_state["total_trades"],
                "win_count":     bot_state["win_count"],
            }, f)
    except Exception as e:
        log.error(f"Save state error: {e}")

def diary_entry(symbol, text, entry_type="trade"):
    bot_state["diary"].append({
        "time":   datetime.now().strftime("%H:%M"),
        "symbol": symbol,
        "text":   text,
        "type":   entry_type,
    })
    save_state()

# ── OANDA helpers ──────────────────────────────────────────────────────────────
def get_oanda_client():
    return oandapyV20.API(access_token=API_KEY, environment=ENVIRONMENT)

def is_market_open():
    """Forex market is open Sun 5PM ET to Fri 5PM ET."""
    now = datetime.now(timezone.utc)
    day = now.weekday()  # 0=Mon, 6=Sun
    hour = now.hour
    # Closed Saturday (5) and most of Sunday (6) until 21:00 UTC
    if day == 5: return False
    if day == 6 and hour < 21: return False
    # Closed Friday after 21:00 UTC
    if day == 4 and hour >= 21: return False
    return True

def get_account_data():
    try:
        client = get_oanda_client()
        r = accounts.AccountSummary(ACCOUNT_ID)
        client.request(r)
        acc = r.response["account"]
        bot_state["account_balance"] = float(acc.get("balance", 0))
        bot_state["account_equity"]  = float(acc.get("NAV", 0))
        bot_state["account_nav"]     = float(acc.get("NAV", 0))
    except Exception as e:
        log.error(f"Account fetch error: {e}")

def get_open_trades():
    try:
        client = get_oanda_client()
        r = trades.OpenTrades(ACCOUNT_ID)
        client.request(r)
        return r.response.get("trades", [])
    except Exception as e:
        log.error(f"Open trades error: {e}")
        return []

def sync_positions():
    try:
        open_trades = get_open_trades()
        live_ids = set()
        for t in open_trades:
            inst = t["instrument"]
            live_ids.add(inst)
            if inst not in bot_state["positions"]:
                bot_state["positions"][inst] = {
                    "trade_id":   t["id"],
                    "entry":      float(t["price"]),
                    "units":      float(t["currentUnits"]),
                    "open_time":  t.get("openTime", "")[:16].replace("T", " "),
                    "symbol":     inst,
                    "unrealized_pnl": float(t.get("unrealizedPL", 0)),
                }
            else:
                bot_state["positions"][inst]["unrealized_pnl"] = float(t.get("unrealizedPL", 0))
        for inst in list(bot_state["positions"].keys()):
            if inst not in live_ids:
                del bot_state["positions"][inst]
    except Exception as e:
        log.error(f"Position sync error: {e}")

def get_candles(pair, count=100, granularity="M5"):
    """Fetch OANDA candles with explicit time window."""
    try:
        client = get_oanda_client()
        end   = datetime.now(timezone.utc)
        start = end - timedelta(hours=12)
        params = {
            "count": count,
            "granularity": granularity,
            "price": "M",
            "from": start.isoformat(),
            "to":   end.isoformat(),
        }
        r = instruments.InstrumentsCandles(pair, params=params)
        client.request(r)
        candles = r.response.get("candles", [])
        if not candles:
            return None
        data = []
        for c in candles:
            if c.get("complete", False):
                mid = c["mid"]
                data.append({
                    "time":   c["time"],
                    "open":   float(mid["o"]),
                    "high":   float(mid["h"]),
                    "low":    float(mid["l"]),
                    "close":  float(mid["c"]),
                    "volume": int(c.get("volume", 0)),
                })
        if not data:
            return None
        df = pd.DataFrame(data)
        df["time"] = pd.to_datetime(df["time"])
        df.set_index("time", inplace=True)
        return df
    except Exception as e:
        log.error(f"Candles error {pair}: {e}")
        return None

def place_order(pair, units, sl_price, tp_price):
    try:
        client = get_oanda_client()
        data = {
            "order": {
                "type":        "MARKET",
                "instrument":  pair,
                "units":       str(int(units)),
                "timeInForce": "FOK",
                "stopLossOnFill":   {"price": f"{sl_price:.5f}"},
                "takeProfitOnFill": {"price": f"{tp_price:.5f}"},
            }
        }
        r = orders.Orders(ACCOUNT_ID, data=data)
        client.request(r)
        return r.response
    except Exception as e:
        log.error(f"Order error {pair}: {e}")
        return None

def close_trade(trade_id):
    try:
        client = get_oanda_client()
        r = trades.TradeClose(ACCOUNT_ID, tradeID=trade_id)
        client.request(r)
        return r.response
    except Exception as e:
        log.error(f"Close trade error {trade_id}: {e}")
        return None

# ── Indicators ─────────────────────────────────────────────────────────────────
def compute_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def compute_rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def compute_bollinger(series, period=20, std=2.0):
    mid   = series.rolling(period).mean()
    sigma = series.rolling(period).std()
    upper = mid + std * sigma
    lower = mid - std * sigma
    bw    = ((upper - lower) / mid * 100).iloc[-1]
    return upper, mid, lower, bw

def compute_macd(series, fast=12, slow=26, signal=9):
    ema_fast  = series.ewm(span=fast, adjust=False).mean()
    ema_slow  = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    sig_line  = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, sig_line, macd_line - sig_line

def is_in_cooldown(pair):
    cooldown_until = bot_state["cooldowns"].get(pair)
    if cooldown_until and datetime.now() < cooldown_until:
        remaining = (cooldown_until - datetime.now()).seconds // 60
        log.info(f"{pair} in cooldown for {remaining} more minutes")
        return True
    return False

def set_cooldown(pair):
    bot_state["cooldowns"][pair] = datetime.now() + timedelta(minutes=STRATEGY["cooldown_minutes"])

def is_signal_stale(pair, sig_data):
    last = bot_state["last_signal_data"].get(pair)
    if last is None:
        bot_state["last_signal_data"][pair] = sig_data
        return False
    if (sig_data.get("rsi") == last.get("rsi") and
        sig_data.get("macd_hist") == last.get("macd_hist")):
        log.warning(f"{pair} — stale signal detected, skipping")
        return True
    bot_state["last_signal_data"][pair] = sig_data
    return False

# ── Signal generation ──────────────────────────────────────────────────────────
def generate_signal(pair):
    try:
        df = get_candles(pair, count=100, granularity="M5")
        if df is None or len(df) < 50:
            return "HOLD", {}

        close = df["close"]
        price = float(close.iloc[-1])
        pip   = 1 / PIP_MULT[pair]

        ema_fast  = compute_ema(close, STRATEGY["ema_fast"])
        ema_slow  = compute_ema(close, STRATEGY["ema_slow"])
        ema_trend = compute_ema(close, STRATEGY["ema_trend"])

        trend_bull = ema_fast.iloc[-1] > ema_slow.iloc[-1]
        trend_prev = ema_fast.iloc[-2] > ema_slow.iloc[-2]
        cross_up   = trend_bull and not trend_prev
        cross_down = not trend_bull and trend_prev
        above_50   = price > ema_trend.iloc[-1]

        rsi      = compute_rsi(close, STRATEGY["rsi_period"])
        rsi_val  = float(rsi.iloc[-1])

        bb_up, bb_mid, bb_lo, bb_bw = compute_bollinger(close, STRATEGY["bb_period"], STRATEGY["bb_std"])
        bb_buy  = price < float(bb_lo.iloc[-1]) and bb_bw >= STRATEGY["bb_min_bandwidth"]
        bb_sell = price > float(bb_up.iloc[-1])

        _, _, histogram = compute_macd(close)
        macd_hist = float(histogram.iloc[-1])

        buy_score = sell_score = 0
        if trend_bull: buy_score  += 2
        if cross_up:   buy_score  += 1
        if not trend_bull: sell_score += 2
        if cross_down: sell_score += 1
        if rsi_val < STRATEGY["rsi_oversold"]:   buy_score  += 2
        if rsi_val > STRATEGY["rsi_overbought"]: sell_score += 2
        if bb_buy:  buy_score  += 1
        if bb_sell: sell_score += 1
        if macd_hist > 0: buy_score  += 1
        if macd_hist < 0: sell_score += 1

        sig_data = {
            "price":     round(price, 5),
            "rsi":       round(rsi_val, 1),
            "bb_bw":     round(bb_bw, 4),
            "macd_hist": round(macd_hist, 6),
            "ema_trend": "BULL" if trend_bull else "BEAR",
            "above_50":  bool(above_50),
            "buy_score": buy_score,
            "sell_score":sell_score,
            "signal":    "HOLD",
        }

        log.info(f"{pair} | price={price:.5f} RSI={rsi_val:.1f} MACD={macd_hist:.6f} BB_BW={bb_bw:.4f} BUY={buy_score} SELL={sell_score}")

        if buy_score >= STRATEGY["min_score"] and buy_score > sell_score and above_50:
            return "BUY", {**sig_data, "signal": "BUY"}
        elif sell_score >= STRATEGY["min_score"] and sell_score > buy_score:
            return "SELL", {**sig_data, "signal": "SELL"}
        return "HOLD", sig_data

    except Exception as e:
        log.error(f"Signal error {pair}: {e}")
        return "HOLD", {"price": 0, "signal": "HOLD"}

# ── Trading Loop ───────────────────────────────────────────────────────────────
def trading_loop():
    if not API_KEY or not ACCOUNT_ID:
        log.warning("No OANDA credentials — bot idle")
        return

    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
        log.info("Cleared stale state")

    get_account_data()
    sync_positions()

    log.info(f"ForexAI EMA Bot started | Paper={PAPER_MODE}")
    diary_entry("SYSTEM",
        f"ForexAI EMA Bot started | SL={STRATEGY['stop_loss_pips']}pips | "
        f"TP={STRATEGY['take_profit_pips']}pips | "
        f"Min score={STRATEGY['min_score']} | Cooldown={STRATEGY['cooldown_minutes']}min", "system")

    while True:
        try:
            if bot_state["killed"]:
                time.sleep(5)
                continue

            market_open = is_market_open()
            bot_state["market_open"] = market_open

            if not market_open:
                log.info("Forex market closed — waiting")
                time.sleep(300)
                continue

            get_account_data()
            sync_positions()
            now = datetime.now()

            for pair in PAIRS:
                signal, sig_data = generate_signal(pair)
                bot_state["signals"][pair] = sig_data

                in_position = pair in bot_state["positions"]

                if in_position:
                    pos = bot_state["positions"][pair]
                    # OANDA handles SL/TP automatically — just check for manual exit signals
                    pnl = pos.get("unrealized_pnl", 0)
                    price = sig_data.get("price", pos["entry"])
                    pips  = (price - pos["entry"]) * PIP_MULT[pair]

                    # Manual exit on strong opposing signal
                    if signal == "SELL" and pips > 5:
                        result = close_trade(pos["trade_id"])
                        if result:
                            win = pnl > 0
                            bot_state["closed_trades"].append({
                                "symbol": pair, "entry": pos["entry"], "exit": price,
                                "units": pos["units"], "pnl": round(pnl, 2),
                                "pips": round(pips, 1), "win": win,
                                "time": pos["open_time"],
                                "close_time": now.strftime("%H:%M"),
                                "signal": "Manual exit — SELL signal"
                            })
                            bot_state["day_pnl"]       = round(bot_state["day_pnl"] + pnl, 2)
                            bot_state["total_trades"] += 1
                            if win: bot_state["win_count"] += 1
                            del bot_state["positions"][pair]
                            diary_entry(pair,
                                f"{'WIN' if win else 'LOSS'} | {pos['entry']:.5f} → {price:.5f} | "
                                f"P&L ${pnl:.2f} | {pips:+.1f} pips | Manual exit",
                                "win" if win else "loss")
                            save_state()

                elif signal == "BUY" and not bot_state["killed"]:
                    if is_in_cooldown(pair):
                        continue
                    if is_signal_stale(pair, sig_data):
                        continue

                    price = sig_data.get("price", 0)
                    if price <= 0:
                        continue

                    pip  = 1 / PIP_MULT[pair]
                    sl   = round(price - STRATEGY["stop_loss_pips"] * pip, 5)
                    tp   = round(price + STRATEGY["take_profit_pips"] * pip, 5)
                    units = STRATEGY["position_units"]

                    result = place_order(pair, units, sl, tp)
                    if result:
                        bot_state["positions"][pair] = {
                            "trade_id":  result.get("orderFillTransaction", {}).get("tradeOpened", {}).get("tradeID", ""),
                            "entry":     price,
                            "units":     units,
                            "open_time": now.strftime("%H:%M"),
                            "symbol":    pair,
                            "unrealized_pnl": 0,
                            "sl": sl, "tp": tp,
                        }
                        diary_entry(pair,
                            f"BUY | {price:.5f} | {units:,} units | "
                            f"SL={sl:.5f} TP={tp:.5f} | "
                            f"Score {sig_data.get('buy_score','?')} | RSI {sig_data.get('rsi','?')}",
                            "trade")
                        save_state()

            time.sleep(60)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Loop error: {e}")
            time.sleep(30)

# ── JSON helper ────────────────────────────────────────────────────────────────
def clean_nan(obj):
    if obj is None: return None
    if isinstance(obj, datetime): return obj.isoformat()
    if hasattr(obj, '__module__') and type(obj).__module__ == 'numpy':
        try: obj = obj.item()
        except: return 0
    if isinstance(obj, bool): return obj
    if isinstance(obj, float):
        return 0.0 if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, int): return obj
    if isinstance(obj, str): return obj
    if isinstance(obj, dict): return {str(k): clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)): return [clean_nan(v) for v in obj]
    try: return str(obj)
    except: return None

# ── Flask API ──────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

@app.after_request
def add_no_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"]        = "no-cache"
    response.headers["Expires"]       = "0"
    return response

@app.route("/status")
def status():
    get_account_data()
    sync_positions()
    wins  = bot_state["win_count"]
    total = bot_state["total_trades"]
    payload = {
        "running":          bot_state["running"],
        "killed":           bot_state["killed"],
        "paper_mode":       PAPER_MODE,
        "market_open":      bot_state["market_open"],
        "positions":        bot_state["positions"],
        "closed_trades":    bot_state["closed_trades"][-50:],
        "diary":            bot_state["diary"][-100:],
        "day_pnl":          bot_state["day_pnl"],
        "total_trades":     total,
        "win_rate":         round(wins/total*100) if total > 0 else 0,
        "strategy":         STRATEGY,
        "signals":          bot_state["signals"],
        "account_balance":  bot_state["account_balance"],
        "account_equity":   bot_state["account_equity"],
        "account_nav":      bot_state["account_nav"],
        "active_cooldowns": {k: v.isoformat() for k, v in bot_state.get("cooldowns", {}).items()
                             if isinstance(v, datetime) and v > datetime.now()},
        "version":          "ForexEMA-1.0",
    }
    return jsonify(clean_nan(payload))

@app.route("/killswitch", methods=["POST"])
def killswitch():
    data = request.json or {}
    bot_state["killed"] = data.get("kill", True)
    status_str = "KILLED" if bot_state["killed"] else "RESUMED"
    diary_entry("SYSTEM", f"Kill switch {status_str}", "system")
    return jsonify({"killed": bot_state["killed"], "status": status_str})

@app.route("/settings", methods=["POST"])
def update_settings():
    data    = request.json or {}
    allowed = ["stop_loss_pips","take_profit_pips","position_units",
               "min_score","cooldown_minutes","rsi_oversold","rsi_overbought"]
    for k in allowed:
        if k in data:
            STRATEGY[k] = data[k]
    diary_entry("SYSTEM", "Settings updated", "system")
    return jsonify({"ok": True, "strategy": STRATEGY})

@app.route("/diary")
def get_diary():
    return jsonify({"diary": bot_state["diary"]})

@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat(),
                    "version": "ForexEMA-1.0", "market_open": is_market_open()})

@app.route("/")
def index():
    try:
        return open("/app/index.html").read()
    except Exception:
        return open("index.html").read()

if __name__ == "__main__":
    import threading
    t = threading.Thread(target=trading_loop, daemon=True)
    t.start()
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
