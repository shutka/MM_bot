import time
import math
import statistics
import random
import threading
import requests
import json
import os
import ccxt

# ========= CONFIG (заповнити) =========
API_KEY         = "esMwwMQF5Jl4cBFEKNwlRdyj4o8fO4KcSRx6uhJyxN4hwthsySBugVvgIwbTQuXp"
API_SECRET      = "5AjNt4AdZgVAOTsH8HT0hFhTXMPHJmWKtoH7QivUhBowQZChOq97MXflTa5IyHtF"
TELEGRAM_TOKEN  = "7696383128:AAEBiEkVQx4x4nPSc6N-_6UaySpNH1zsp9c"
CHAT_ID         = "830034385"         # числовий chat_id

SYMBOL          = "XRP/USDT"             # працюємо з XRP
QUOTE_BUDGET    = 25.0                   # $ на одну сторону (buy/sell)
TARGET_INV_USD  = 25.0                   # бажана вартість інвентаря
MAX_INV_USD     = 60.0                   # максимум інвентаря
MIN_SPREAD_PCT  = 0.25 / 100             # мінімальний спред (у відс.)
VOL_K           = 1.5                    # множник волатильності для динамічного спреду
REPRICE_GAP_PCT = 0.15 / 100             # перевиставляти, якщо ринок зрушився > цього
TTL_SEC         = 120                    # «старіння» ліміт ордерів
LOOP_SLEEP      = 5                      # сек між ітераціями (дод. джиттер нижче)
STOP_LOSS_PCT   = 3.0 / 100              # страховий стоп по інвентарю
TAKE_PROFIT_PCT = 0.8 / 100              # TP по інвентарю
OHLCV_LEN       = 30                     # хвилин вікна для воли

STATE_FILE      = "mm_state.json"
# ======================================

# --------- Біржа ---------
ex = ccxt.binance({
    "apiKey": API_KEY,
    "secret": API_SECRET,
    "enableRateLimit": True,
})
ex.load_markets()
mkt = ex.market(SYMBOL)

price_prec   = mkt["precision"]["price"] or 6
amt_prec     = mkt["precision"]["amount"] or 6
lot_min      = (mkt["limits"].get("amount") or {}).get("min", 0.0)
min_notional = (mkt["limits"].get("cost")   or {}).get("min", 5.0)  # запасний план

# --------- Глобальний стан (зберігаємо в файл) ---------
state = {
    "avg_cost": 0.0,            # середня собівартість по локальному обліку
    "inv_amount": 0.0,          # локальний облік кількості (для realized PnL)
    "realized_pnl": 0.0,        # сумарний реалізований PnL з моменту старту
    "last_trade_ts": 0          # останній час трейда (ms)
}
def load_state():
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                s = json.load(f)
            state.update(s)
        except Exception:
            pass

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass

load_state()

paused = False
last_sent_hash = None
last_update_id = None

def tg_send(text: str):
    """Телеграм без дублювань однакових повідомлень підряд."""
    global last_sent_hash
    try:
        h = hash(text)
        if h == last_sent_hash:
            return
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text}
        )
        last_sent_hash = h
    except Exception as e:
        print(f"[TG ERR] {e}")

def last_price():
    return float(ex.fetch_ticker(SYMBOL)["last"])

def fetch_volatility():
    """Стандартне відхилення 1-хв відносних змін за OHLCV_LEN хв."""
    ohlcv = ex.fetch_ohlcv(SYMBOL, timeframe="1m", limit=OHLCV_LEN)
    closes = [c[4] for c in ohlcv]
    rets = [(closes[i] - closes[i-1]) / closes[i-1]
            for i in range(1, len(closes)) if closes[i-1] > 0]
    return statistics.pstdev(rets) if rets else 0.0

def portfolio_state():
    """Фактичний баланс з біржі (ground truth)."""
    bal = ex.fetch_balance()
    base, quote = mkt["base"], mkt["quote"]
    base_amt   = float(bal["total"].get(base, 0.0))
    quote_amt  = float(bal["total"].get(quote, 0.0))
    px = last_price()
    base_usd = base_amt * px
    return base_amt, base_usd, quote_amt, px

def ensure_notional_ok(amount, price):
    return (amount * price) >= min_notional

def round_price(p):  return float(ex.price_to_precision(SYMBOL, p))
def round_amount(a): return float(ex.amount_to_precision(SYMBOL, a))

def clamp_amount_by_lot(amount):
    """Округлення під мінімальний лот."""
    if lot_min and amount < lot_min:
        return 0.0
    if lot_min and amount > 0:
        k = math.floor(amount / lot_min)
        return round_amount(k * lot_min)
    return round_amount(amount)

# активні ордери поточного циклу
active = {"buy": None, "sell": None, "ts_buy": 0.0, "ts_sell": 0.0, "ref_px": None}

def cancel_if_exists(side):
    oid = active[side]
    if oid:
        try:
            ex.cancel_order(oid, SYMBOL)
        except Exception:
            pass
        active[side] = None
        active["ts_"+side] = 0.0

def cancel_all_open():
    """Скасувати всі відкриті ордери по символу."""
    try:
        orders = ex.fetch_open_orders(SYMBOL)
        for o in orders:
            try:
                ex.cancel_order(o["id"], SYMBOL)
            except Exception:
                pass
    except Exception:
        pass

def place_orders():
    """Поставити/перевиставити пару лімітних ордерів симетрично від ціни."""
    vol = fetch_volatility()
    px  = last_price()
    spread = max(MIN_SPREAD_PCT, VOL_K * vol)
    buy_px  = round_price(px * (1 - spread))
    sell_px = round_price(px * (1 + spread))

    base_amt, base_usd, quote_amt, _ = portfolio_state()
    can_accumulate = base_usd < MAX_INV_USD
    want_reduce    = base_usd > TARGET_INV_USD

    # невелика «партія» на сторону, щоб не висмоктувати весь баланс
    buy_quote = min(QUOTE_BUDGET, max(0.0, quote_amt * 0.98))
    buy_amt   = clamp_amount_by_lot(buy_quote / buy_px)

    sell_amt  = clamp_amount_by_lot(min(base_amt, QUOTE_BUDGET / sell_px))

    # нотіонал/інвентарні обмеження
    if not ensure_notional_ok(buy_amt, buy_px):   buy_amt = 0.0
    if not ensure_notional_ok(sell_amt, sell_px): sell_amt = 0.0
    if not can_accumulate:                        buy_amt = 0.0
    if base_usd < TARGET_INV_USD * 0.3:           sell_amt = 0.0

    # перевиставити, якщо ринок далеко пішов або заявки «застаріли»
    need_reprice = False
    if active["ref_px"] is None:
        need_reprice = True
    else:
        if abs(px - active["ref_px"]) / active["ref_px"] >= REPRICE_GAP_PCT:
            need_reprice = True
    now = time.time()
    if active["ts_buy"]  and now - active["ts_buy"]  > TTL_SEC: need_reprice = True
    if active["ts_sell"] and now - active["ts_sell"] > TTL_SEC: need_reprice = True

    if need_reprice:
        cancel_if_exists("buy")
        cancel_if_exists("sell")
        active["ref_px"] = px

        if buy_amt > 0:
            try:
                o = ex.create_limit_buy_order(SYMBOL, buy_amt, buy_px)
                active["buy"] = o["id"]; active["ts_buy"] = time.time()
            except Exception as e:
                print(f"[BUY ERR] {e}")

        if sell_amt > 0 and want_reduce:
            try:
                o = ex.create_limit_sell_order(SYMBOL, sell_amt, sell_px)
                active["sell"] = o["id"]; active["ts_sell"] = time.time()
            except Exception as e:
                print(f"[SELL ERR] {e}")

def update_position_from_trade(trade):
    """Оновлюємо локальний облік інвентаря/собівартості та realized PnL."""
    global state
    side = trade.get("side")
    price = float(trade.get("price"))
    amount = float(trade.get("amount"))
    cost = price * amount
    fee_cost = float(trade.get("fee", {}).get("cost", 0.0))
    fee_currency = trade.get("fee", {}).get("currency", "")

    # Невеличка поправка: якщо комісія в базовій валюті, зменшуємо amount
    if fee_cost and fee_currency == mkt["base"]:
        amount_net = max(0.0, amount - fee_cost)
    else:
        amount_net = amount

    avg = state["avg_cost"]
    inv = state["inv_amount"]

    if side == "buy":
        new_cost_total = avg * inv + price * amount_net
        new_inv = inv + amount_net
        state["avg_cost"] = (new_cost_total / new_inv) if new_inv > 0 else 0.0
        state["inv_amount"] = new_inv
    elif side == "sell":
        # реалізований PnL відносно локальної собівартості
        sell_amt = min(inv, amount_net)
        realized = (price - avg) * sell_amt
        state["realized_pnl"] += realized
        state["inv_amount"] = max(0.0, inv - sell_amt)
        # якщо інвентар закрився в нуль — скидаємо собівартість
        if state["inv_amount"] == 0:
            state["avg_cost"] = 0.0

    save_state()

def poll_trades_and_notify():
    """Опитуємо останні трейди, шлемо нотифікації про філи, оновлюємо локальний стан."""
    global state
    since = state["last_trade_ts"] or None
    try:
        trades = ex.fetch_my_trades(SYMBOL, since=since, limit=50)
        if not trades:
            return
        # сортуємо за часом
        trades.sort(key=lambda t: t["timestamp"])
        for t in trades:
            ts = t["timestamp"]
            if state["last_trade_ts"] and ts <= state["last_trade_ts"]:
                continue
            side = t["side"].upper()
            price = float(t["price"])
            amount = float(t["amount"])
            update_position_from_trade(t)
            tg_send(f"🧾 Fill {side}: {amount:.2f} {mkt['base']} @ {price:.5f}\n"
                    f"Inv: {state['inv_amount']:.2f} {mkt['base']} | Avg: {state['avg_cost']:.5f}\n"
                    f"Realized PnL: {state['realized_pnl']:+.2f} USDT")
            state["last_trade_ts"] = ts
        save_state()
    except Exception as e:
        print(f"[TRADES ERR] {e}")

def check_fills_and_risk():
    """Контроль інвентаря: TP/SL за ринком як страховка + обробка філів."""
    poll_trades_and_notify()  # нове: фіксуємо філи / оновлюємо стан

    base_amt, base_usd, quote_amt, px = portfolio_state()
    if base_amt <= 0:
        return
    # Для TP/SL використовуємо локальну середню собівартість (консервативно)
    avg_ref = state["avg_cost"] or (active["ref_px"] or px)
    pnl_pct = (px - avg_ref) / avg_ref if avg_ref > 0 else 0.0

    if pnl_pct >= TAKE_PROFIT_PCT:
        amt = clamp_amount_by_lot(base_amt)
        if ensure_notional_ok(amt, px) and amt > 0:
            try:
                cancel_if_exists("sell")
                ex.create_market_sell_order(SYMBOL, amt)
                tg_send(f"✅ TP: Продано {amt:.2f} {mkt['base']} ~ {px:.5f}")
            except Exception as e:
                print(f"[TP ERR] {e}")

    if pnl_pct <= -STOP_LOSS_PCT:
        amt = clamp_amount_by_lot(base_amt)
        if ensure_notional_ok(amt, px) and amt > 0:
            try:
                cancel_if_exists("sell")
                ex.create_market_sell_order(SYMBOL, amt)
                tg_send(f"⛔ SL: Продано {amt:.2f} {mkt['base']} ~ {px:.5f}")
            except Exception as e:
                print(f"[SL ERR] {e}")

def place_and_manage_loop():
    tg_send(f"🚀 MM-бот стартував: {SYMBOL}")
    cancel_all_open()
    while True:
        try:
            if not paused:
                place_orders()
                check_fills_and_risk()
        except Exception as e:
            print(f"[MAIN ERR] {e}")
        time.sleep(LOOP_SLEEP + random.uniform(0, 1.5))  # невеликий джиттер

# -------- Telegram control --------
def tg_set_commands():
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setMyCommands",
            json={"commands": [
                {"command": "status",  "description": "Статус бота"},
                {"command": "balance", "description": "Показати баланс"},
                {"command": "pause",   "description": "Поставити на паузу"},
                {"command": "resume",  "description": "Продовжити роботу"},
                {"command": "cancel",  "description": "Скасувати всі ордери"},
                {"command": "reprice", "description": "Перевиставити ордери"}
            ]}
        )
    except Exception as e:
        print(f"[TG CMD ERR] {e}")

def tg_loop():
    global last_update_id, paused
    tg_set_commands()
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            if last_update_id:
                url += f"?offset={last_update_id + 1}"
            resp = requests.get(url, timeout=20).json()
            if "result" in resp:
                for upd in resp["result"]:
                    uid = upd.get("update_id")
                    if uid is None or (last_update_id and uid <= last_update_id):
                        continue
                    last_update_id = uid
                    msg = upd.get("message", {})
                    text = (msg.get("text") or "").strip().lower()
                    if not text:
                        continue

                    if text == "/pause":
                        paused = True
                        tg_send("⏸️ Пауза активована.")
                    elif text == "/resume":
                        paused = False
                        tg_send("▶️ Бот відновив роботу.")
                    elif text == "/cancel":
                        cancel_all_open()
                        tg_send("🧹 Всі відкриті ордери скасовано.")
                    elif text == "/reprice":
                        active["ref_px"] = None
                        tg_send("♻️ Запит на перевиставлення ордерів прийнято.")
                    elif text == "/balance":
                        base_amt, base_usd, quote_amt, px = portfolio_state()
                        tg_send(f"💰 Баланс\n"
                                f"{mkt['base']}: {base_amt:.2f} (~${base_usd:.2f})\n"
                                f"{mkt['quote']}: ${quote_amt:.2f}\n"
                                f"Ціна: {px:.5f}")
                    elif text == "/status":
                        base_amt, base_usd, quote_amt, px = portfolio_state()
                        orders = []
                        try:
                            orders = ex.fetch_open_orders(SYMBOL)
                        except Exception:
                            orders = []
                        obuy = next((o for o in orders if o["side"]=="buy"), None)
                        osell= next((o for o in orders if o["side"]=="sell"), None)

                        unreal_pnl = 0.0
                        if state["inv_amount"] > 0 and state["avg_cost"] > 0:
                            unreal_pnl = (px - state["avg_cost"]) * state["inv_amount"]

                        msg1 = (f"📊 {SYMBOL}\n"
                                f"Ціна: {px:.5f}\n"
                                f"Інвентар: {base_amt:.2f} {mkt['base']} (~${base_usd:.2f})\n"
                                f"USDT: ${quote_amt:.2f}\n"
                                f"Avg(cost): {state['avg_cost']:.5f}\n"
                                f"PnL: Unreal {unreal_pnl:+.2f} | Real {state['realized_pnl']:+.2f} USDT")
                        tg_send(msg1)

                        if obuy:
                            tg_send(f"Buy(ord): {float(obuy['price']):.5f} x {float(obuy['amount']):.2f}")
                        else:
                            tg_send("Buy(ord): —")
                        if osell:
                            tg_send(f"Sell(ord): {float(osell['price']):.5f} x {float(osell['amount']):.2f}")
                        else:
                            tg_send("Sell(ord): —")
        except Exception as e:
            print(f"[TG LOOP ERR] {e}")
        time.sleep(2)

if __name__ == "__main__":
    threading.Thread(target=tg_loop, daemon=True).start()
    place_and_manage_loop()
