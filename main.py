# ForexAI EMA Bot - v1.1 (fixed candle fetch)
import os, time, logging, math
from datetime import datetime, timezone
from flask import Flask, jsonify, request
from flask_cors import CORS
import threading
 
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)
 
app = Flask(__name__)
CORS(app)
 
OANDA_API_KEY    = os.environ.get("OANDA_API_KEY", "")
OANDA_ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID", "")
PAPER_MODE       = os.environ.get("PAPER_MODE", "true").lower() == "true"
OANDA_ENV        = "practice" if PAPER_MODE else "live"
 
SYMBOLS = ["EUR_USD", "GBP_USD", "USD_JPY"]
 
STRATEGY = {
    "ema_fast": 9, "ema_slow": 21, "ema_trend": 50,
    "rsi_period": 14, "rsi_oversold": 35, "rsi_overbought": 65,
    "bb_period": 20, "bb_std": 2.0, "bb_min_bandwidth": 0.05,
    "min_score": 4, "stop_loss_pips": 15, "take_profit_pips": 30,
    "position_units": 10000, "cooldown_minutes": 15,
    "enabled_regime_filter": True
}
 
bot_state = {
    "running": True, "killed": False, "positions": {},
    "closed_trades": [], "diary": [], "day_pnl": 0.0,
    "total_trades": 0, "win_count": 0, "signals": {s: {} for s in SYMBOLS},
    "account_balance": 0.0, "account_equity": 0.0, "account_nav": 0.0,
    "active_cooldowns": {}, "market_open": False, "version": "ForexEMA-1.2"
}
 
def get_oanda_client():
    import oandapyV20
    return oandapyV20.API(access_token=OANDA_API_KEY, environment=OANDA_ENV)
 
def get_candles(symbol, granularity="M5", count=100):
    """Fetch candles using only count (no from/to conflict)"""
    try:
        import oandapyV20.endpoints.instruments as instruments
        client = get_oanda_client()
        params = {"granularity": granularity, "count": count, "price": "M"}
        r = instruments.InstrumentsCandles(instrument=symbol, params=params)
        client.request(r)
        candles = r.response.get("candles", [])
        result = []
        for c in candles:
            if c.get("complete", False):
                m = c["mid"]
                result.append({
                    "time": c["time"],
                    "open": float(m["o"]),
                    "high": float(m["h"]),
                    "low":  float(m["l"]),
                    "close": float(m["c"]),
                    "volume": int(c.get("volume", 0))
                })
        return result
    except Exception as e:
        log.error(f"Candles error {symbol}: {e}")
        return []
 
def calc_ema(prices, period):
    if len(prices) < period:
        return []
    k = 2 / (period + 1)
    ema = [sum(prices[:period]) / period]
    for p in prices[period:]:
        ema.append(p * k + ema[-1] * (1 - k))
    return ema
 
def calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - (100 / (1 + rs))
 
def calc_bb(closes, period=20, std_dev=2.0):
    if len(closes) < period:
        return None, None, None
    window = closes[-period:]
    mid = sum(window) / period
    variance = sum((x - mid) ** 2 for x in window) / period
    std = math.sqrt(variance)
    return mid - std_dev * std, mid, mid + std_dev * std
 
def calc_macd(closes):
    if len(closes) < 26:
        return 0.0
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    if not ema12 or not ema26:
        return 0.0
    min_len = min(len(ema12), len(ema26))
    macd_line = [ema12[-(min_len-i)] - ema26[-(min_len-i)] for i in range(min_len)]
    signal = calc_ema(macd_line, 9)
    if not signal:
        return 0.0
    return macd_line[-1] - signal[-1]
 
def pip_value(symbol):
    return 0.0001 if "JPY" not in symbol else 0.01
 
def is_market_open():
    now = datetime.now(timezone.utc)
    wd = now.weekday()
    h = now.hour + now.minute / 60
    if wd == 4 and h >= 21:
        return False
    if wd == 5:
        return False
    if wd == 6 and h < 21:
        return False
    return True
 
def get_account_info():
    try:
        import oandapyV20.endpoints.accounts as accounts
        client = get_oanda_client()
        r = accounts.AccountSummary(OANDA_ACCOUNT_ID)
        client.request(r)
        acct = r.response["account"]
        bot_state["account_balance"] = float(acct.get("balance", 0))
        bot_state["account_nav"]     = float(acct.get("NAV", 0))
        bot_state["account_equity"]  = float(acct.get("NAV", 0))
    except Exception as e:
        log.error(f"Account info error: {e}")
 
def sync_positions():
    try:
        import oandapyV20.endpoints.trades as trades
        client = get_oanda_client()
        r = trades.OpenTrades(OANDA_ACCOUNT_ID)
        client.request(r)
        open_trades = r.response.get("trades", [])
        synced = {}
        for t in open_trades:
            sym = t["instrument"]
            synced[sym] = {
                "symbol": sym,
                "entry": float(t["price"]),
                "units": int(t["currentUnits"]),
                "trade_id": t["id"],
                "open_time": t.get("openTime", datetime.now(timezone.utc).isoformat()),
                "current_price": float(t["price"]),
                "unrealized_pnl": float(t.get("unrealizedPL", 0))
            }
        bot_state["positions"] = synced
    except Exception as e:
        log.error(f"Sync positions error: {e}")
 
def place_order(symbol, units, side):
    try:
        import oandapyV20.endpoints.orders as orders
        client = get_oanda_client()
        actual_units = units if side == "BUY" else -units
        data = {"order": {"type": "MARKET", "instrument": symbol, "units": str(actual_units)}}
        r = orders.OrderCreate(OANDA_ACCOUNT_ID, data=data)
        client.request(r)
        fill = r.response.get("orderFillTransaction", {})
        return float(fill.get("price", 0))
    except Exception as e:
        log.error(f"Order error {symbol}: {e}")
        return None
 
def close_position(symbol, trade_id):
    try:
        import oandapyV20.endpoints.trades as trades
        client = get_oanda_client()
        r = trades.TradeClose(OANDA_ACCOUNT_ID, trade_id)
        client.request(r)
        fill = r.response.get("orderFillTransaction", {})
        return float(fill.get("price", 0))
    except Exception as e:
        log.error(f"Close position error {symbol}: {e}")
        return None
 
def add_diary(symbol, text, entry_type="info"):
    entry = {"time": datetime.now(timezone.utc).strftime("%H:%M"), "symbol": symbol, "text": text, "type": entry_type}
    bot_state["diary"].insert(0, entry)
    if len(bot_state["diary"]) > 200:
        bot_state["diary"] = bot_state["diary"][:200]
 
def generate_signal(symbol):
    try:
        candles_5m = get_candles(symbol, "M5", 100)
        candles_1h = get_candles(symbol, "H1", 60)
        if len(candles_5m) < 30 or len(candles_1h) < 30:
            return {}
 
        closes_5m = [c["close"] for c in candles_5m]
        closes_1h = [c["close"] for c in candles_1h]
        price = closes_5m[-1]
        pv = pip_value(symbol)
 
        # EMAs
        ema9  = calc_ema(closes_5m, 9)
        ema21 = calc_ema(closes_5m, 21)
        ema50 = calc_ema(closes_5m, 50)
        ema50_1h = calc_ema(closes_1h, 50)
 
        if not ema9 or not ema21 or not ema50 or not ema50_1h:
            return {}
 
        rsi = calc_rsi(closes_5m)
        bb_low, bb_mid, bb_high = calc_bb(closes_5m)
        macd_h = calc_macd(closes_5m)
 
        if bb_mid is None:
            return {}
 
        bb_bw = ((bb_high - bb_low) / bb_mid) if bb_mid > 0 else 0
 
        buy_score = 0
        sell_score = 0
 
        # Trend filter (1H 50 EMA)
        regime_ok = price > ema50_1h[-1]
 
        # EMA signals
        if ema9[-1] > ema21[-1]:
            buy_score += 2
        else:
            sell_score += 2
 
        # Fresh crossover
        if len(ema9) > 1 and len(ema21) > 1:
            if ema9[-1] > ema21[-1] and ema9[-2] <= ema21[-2]:
                buy_score += 1
            elif ema9[-1] < ema21[-1] and ema9[-2] >= ema21[-2]:
                sell_score += 1
 
        # RSI
        if rsi < STRATEGY["rsi_oversold"]:
            buy_score += 2
        elif rsi > STRATEGY["rsi_overbought"]:
            sell_score += 2
 
        # Bollinger Bands
        if bb_bw >= STRATEGY["bb_min_bandwidth"]:
            if price < bb_low:
                buy_score += 1
            elif price > bb_high:
                sell_score += 1
 
        # MACD
        if macd_h > 0:
            buy_score += 1
        else:
            sell_score += 1
 
        return {
            "price": price, "rsi": round(rsi, 1), "macd_h": round(macd_h / pv, 2),
            "bb_bw": round(bb_bw * 100, 2), "buy_score": buy_score, "sell_score": sell_score,
            "ema9": round(ema9[-1], 5), "ema21": round(ema21[-1], 5),
            "ema50_1h": round(ema50_1h[-1], 5), "regime_ok": regime_ok
        }
    except Exception as e:
        log.error(f"Signal error {symbol}: {e}")
        return {}
 
def trading_loop():
    add_diary("SYSTEM", f"ForexAI EMA Bot started | SL=15pips | TP=30pips | Min score=4 | Cooldown=15min", "system")
    log.info("ForexAI EMA Bot v1.2 started")
 
    while True:
        try:
            if not is_market_open():
                bot_state["market_open"] = False
                time.sleep(60)
                continue
 
            bot_state["market_open"] = True
            get_account_info()
            sync_positions()
 
            now = datetime.now(timezone.utc)
 
            # Clear expired cooldowns
            expired = [s for s, t in bot_state["active_cooldowns"].items()
                       if (now - datetime.fromisoformat(t)).total_seconds() > STRATEGY["cooldown_minutes"] * 60]
            for s in expired:
                del bot_state["active_cooldowns"][s]
 
            for symbol in SYMBOLS:
                if bot_state["killed"]:
                    break
 
                sig = generate_signal(symbol)
                bot_state["signals"][symbol] = sig
 
                if not sig:
                    continue
 
                pv = pip_value(symbol)
                sl_price_delta = STRATEGY["stop_loss_pips"] * pv
                tp_price_delta = STRATEGY["take_profit_pips"] * pv
 
                log.info(f"{symbol} | price={sig['price']} RSI={sig['rsi']} BUY={sig['buy_score']} SELL={sig['sell_score']} regime={'OK' if sig.get('regime_ok') else 'BEAR'}")
 
                # Check exits for open positions
                if symbol in bot_state["positions"]:
                    pos = bot_state["positions"][symbol]
                    entry = pos["entry"]
                    trade_id = pos["trade_id"]
                    pnl_pips = (sig["price"] - entry) / pv
 
                    should_exit = False
                    reason = ""
 
                    if pnl_pips >= STRATEGY["take_profit_pips"]:
                        should_exit = True
                        reason = "Take profit"
                    elif pnl_pips <= -STRATEGY["stop_loss_pips"]:
                        should_exit = True
                        reason = "Stop loss"
                        bot_state["active_cooldowns"][symbol] = now.isoformat()
                    elif sig["sell_score"] >= STRATEGY["min_score"] and sig["buy_score"] < sig["sell_score"]:
                        # Only exit on signal if we've made at least 10 pips profit
                        # This prevents cutting wins short on minor signal fluctuations
                        if pnl_pips >= 10:
                            should_exit = True
                            reason = "SELL signal"
                        elif pnl_pips < 0:
                            # Allow signal exit at a loss only if loss is more than 5 pips
                            # to avoid exiting on noise
                            if pnl_pips <= -5:
                                should_exit = True
                                reason = "SELL signal"
 
                    if should_exit:
                        exit_price = close_position(symbol, trade_id)
                        if exit_price:
                            # Correct P&L calculation per currency pair:
                            # EUR/USD, GBP/USD: pnl = (exit - entry) * units (already in USD)
                            # USD/JPY: pnl = (exit - entry) * units / exit_price (convert JPY to USD)
                            if "JPY" in symbol:
                                pnl = (exit_price - entry) * pos["units"] / exit_price
                            else:
                                pnl = (exit_price - entry) * pos["units"]
                            win = pnl > 0
                            bot_state["day_pnl"] += pnl
                            bot_state["total_trades"] += 1
                            if win:
                                bot_state["win_count"] += 1
                            trade_rec = {
                                "symbol": symbol, "entry": entry, "exit": exit_price,
                                "pnl": round(pnl, 2), "pips": round(pnl_pips, 1),
                                "win": win, "reason": reason,
                                "time": now.strftime("%H:%M")
                            }
                            bot_state["closed_trades"].append(trade_rec)
                            entry_type = "win" if win else "loss"
                            add_diary(symbol, f"{'WIN' if win else 'LOSS'} | {entry:.5f} -> {exit_price:.5f} | {round(pnl_pips,1)} pips | P&L ${round(pnl,2)} | {reason}", entry_type)
                            del bot_state["positions"][symbol]
 
                # Check entries
                elif symbol not in bot_state["active_cooldowns"] and not bot_state["killed"]:
                    regime_ok = sig.get("regime_ok", True) or not STRATEGY["enabled_regime_filter"]
                    if sig["buy_score"] >= STRATEGY["min_score"] and sig["buy_score"] > sig["sell_score"] and regime_ok:
                        entry_price = place_order(symbol, STRATEGY["position_units"], "BUY")
                        if entry_price:
                            bot_state["positions"][symbol] = {
                                "symbol": symbol, "entry": entry_price,
                                "units": STRATEGY["position_units"], "trade_id": "pending",
                                "open_time": now.isoformat(), "current_price": entry_price,
                                "unrealized_pnl": 0
                            }
                            sync_positions()
                            add_diary(symbol, f"BUY | Entry {entry_price:.5f} | Score {sig['buy_score']} | RSI {sig['rsi']}", "buy")
 
        except Exception as e:
            log.error(f"Loop error: {e}")
 
        time.sleep(60)
 
threading.Thread(target=trading_loop, daemon=True).start()
 
@app.after_request
def no_cache(r):
    r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    r.headers["Pragma"] = "no-cache"
    return r
 
@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat(),
                    "version": bot_state["version"], "market_open": bot_state["market_open"]})
 
@app.route("/status")
def status():
    get_account_info()
    wins = bot_state["win_count"]
    total = bot_state["total_trades"]
    return jsonify({
        "running": bot_state["running"], "killed": bot_state["killed"],
        "paper_mode": PAPER_MODE, "market_open": bot_state["market_open"],
        "positions": bot_state["positions"], "closed_trades": bot_state["closed_trades"][-50:],
        "diary": bot_state["diary"][-100:], "day_pnl": bot_state["day_pnl"],
        "total_trades": total, "win_rate": round(wins/total*100) if total > 0 else 0,
        "signals": bot_state["signals"], "strategy": STRATEGY,
        "account_balance": bot_state["account_balance"],
        "account_equity": bot_state["account_equity"],
        "account_nav": bot_state["account_nav"],
        "active_cooldowns": bot_state["active_cooldowns"],
        "version": bot_state["version"]
    })
 
@app.route("/diary")
def diary():
    return jsonify({"diary": bot_state["diary"]})
 
@app.route("/kill", methods=["POST"])
def kill():
    bot_state["killed"] = not bot_state["killed"]
    status = "KILLED" if bot_state["killed"] else "RESUMED"
    add_diary("SYSTEM", f"Kill switch {status}", "system")
    return jsonify({"killed": bot_state["killed"]})
 
@app.route("/bars")
def bars():
    symbol = request.args.get("symbol", "EUR_USD")
    tf = request.args.get("timeframe", "M5")
    candles = get_candles(symbol, tf, 150)
    result = [{"time": int(datetime.fromisoformat(c["time"].replace("Z","")).timestamp()),
               "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"]} for c in candles]
    return jsonify(result)
 
@app.route("/")
def index():
    try:
        with open("index.html") as f:
            return f.read()
    except:
        return jsonify({"status": "ForexAI EMA Bot v1.2 running"})
 
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
