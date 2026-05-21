import os
import time
import requests
import threading
import numpy as np
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from flask import Flask
import signal
import sys
import html
import json
import logging
from datetime import datetime, timezone, timedelta
from journal_system import JournalSystem, _week_key

# ==========================================
# LOGGING
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ==========================================
# 1. KONFIGURASI (ENV)
# ==========================================
CG_API_KEY = os.environ.get("CG_API_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
BASE_URL = "https://api.coingecko.com/api/v3"

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")
bot.remove_webhook()
time.sleep(1)

is_scanning = True
trades_lock = threading.Lock()
journal = JournalSystem(system_name="Nova7")

# Cache untuk keyboard data (1 jam)
_keyboard_cache = {}
def get_cached_coin_data(coin_id):
    now = time.time()
    if coin_id in _keyboard_cache and now - _keyboard_cache[coin_id]['time'] < 3600:
        return _keyboard_cache[coin_id]['data']
    headers = {"x-cg-demo-api-key": CG_API_KEY} if CG_API_KEY else {}
    url = f"{BASE_URL}/coins/{coin_id}?localization=false&tickers=true&market_data=false&community_data=false&developer_data=false"
    try:
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code == 200:
            data = res.json()
            _keyboard_cache[coin_id] = {'data': data, 'time': now}
            return data
        return None
    except Exception as e:
        logger.error(f"Gagal fetch coin data: {e}")
        return None

# ==========================================
# 2. WEB SERVER (KEEP-ALIVE)
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Enjin Nova7 Aktif & Stabil 🐋"

@app.route('/webhook', methods=['POST'])
def webhook():
    from flask import request
    update = telebot.types.Update.de_json(request.get_json())
    bot.process_new_updates([update])
    return 'ok', 200

def admin_log(context, error):
    if not ADMIN_CHAT_ID: return
    try:
        msg = f"☢️ <b>[NOVA7 ERROR] {html.escape(context)}</b>\n<code>{html.escape(str(error)[:400])}</code>"
        bot.send_message(ADMIN_CHAT_ID, msg, parse_mode="HTML")
    except Exception:
        pass

# ==========================================
# 3. INDIKATOR TEKNIKAL & MATEMATIK
# ==========================================
def calculate_rsi(prices, period=14):
    arr = np.array(prices, dtype=float)
    if len(arr) < period + 1:
        return 50.0

    deltas = np.diff(arr)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)

def calculate_ema(prices, period):
    arr = np.array(prices, dtype=float)
    if len(arr) < period:
        return float(arr[-1])

    k = 2.0 / (period + 1)
    ema = float(np.mean(arr[:period]))
    for price in arr[period:]:
        ema = price * k + ema * (1.0 - k)
    return round(ema, 10)

# =========================================================
# [DIPERBAIKI] calculate_atr menggunakan full series dan smoothing
# =========================================================
def calculate_atr(prices, period=14):
    arr = np.array(prices, dtype=float)
    if len(arr) < period + 1:
        return float(arr[-1]) * 0.05
    # Kira pergerakan mutlak
    moves = np.abs(np.diff(arr))
    # ATR pertama adalah purata ringkas
    atr = np.mean(moves[:period])
    # Smoothing seterusnya
    for i in range(period, len(moves)):
        atr = (atr * (period - 1) + moves[i]) / period
    return float(atr)

def calculate_fibonacci_levels(prices):
    high_p, low_p = max(prices), min(prices)
    diff = high_p - low_p
    return {
        "Fibo_100": high_p,
        "Fibo_618": high_p - (0.618 * diff),
        "Fibo_786": high_p - (0.786 * diff),
        "Fibo_0": low_p
    }

def compute_signal_score(rsi, vol_mult, ath_change, rr_ratio, ema7, ema21):
    score = 0
    if rsi < 25:
        score += 30
    elif rsi < 30:
        score += 25
    elif rsi < 35:
        score += 18
    elif rsi < 40:
        score += 12
    elif rsi < 50:
        score += 6

    if vol_mult >= 4.0:
        score += 25
    elif vol_mult >= 3.0:
        score += 20
    elif vol_mult >= 2.0:
        score += 14
    elif vol_mult >= 1.5:
        score += 8

    if ath_change < -80:
        score += 20
    elif ath_change < -70:
        score += 16
    elif ath_change < -60:
        score += 12
    elif ath_change < -50:
        score += 8

    if rr_ratio >= 4.0:
        score += 15
    elif rr_ratio >= 3.0:
        score += 12
    elif rr_ratio >= 2.0:
        score += 8
    elif rr_ratio >= 1.5:
        score += 4

    if ema7 >= ema21:
        score += 10
    elif ema7 >= ema21 * 0.97:
        score += 5

    if score >= 80:
        grade = "⭐⭐⭐ A+ (Max Conviction)"
    elif score >= 65:
        grade = "⭐⭐ B+ (High Conviction)"
    elif score >= 50:
        grade = "⭐ C+ (Standard)"
    else:
        grade = "⚠️ D (Caution)"

    return score, grade

# ==========================================
# 4. PEMETAAN KATEGORI
# ==========================================
def get_category_insight(categories):
    cat_str = ", ".join(categories).lower() if categories else ""
    if "layer 1" in cat_str or "smart contract" in cat_str:
        return "Layer 1"
    elif "defi" in cat_str or "decentralized finance" in cat_str:
        return "DeFi"
    elif "gaming" in cat_str or "play to earn" in cat_str:
        return "GameFi/Web3"
    elif "meme" in cat_str:
        return "Meme"
    elif "artificial intelligence" in cat_str or "ai" in cat_str:
        return "AI"
    elif "layer 2" in cat_str or "rollup" in cat_str:
        return "Layer 2"
    elif "real world assets" in cat_str or "rwa" in cat_str:
        return "RWA"
    else:
        return categories[0] if categories else "Altcoin"

# ==========================================
# 5. PENJANA INLINE KEYBOARD (LENGKAP - TIDAK DIUBAH)
# ==========================================
def generate_inline_keyboard(coin_id, symbol, coin_name, contract_address=None):
    markup = InlineKeyboardMarkup(row_width=2)
    data = get_cached_coin_data(coin_id)

    categories = []
    chain_name = "Native Chain"
    asset_platform_id = ""
    final_ca = contract_address

    if data:
        categories = data.get("categories", [])
        asset_platform_id = data.get("asset_platform_id", "")
        if asset_platform_id:
            chain_name = asset_platform_id.replace("-", " ").title()

        if not final_ca:
            platforms = data.get("platforms", {})
            if platforms:
                final_ca = list(platforms.values())[0]

    # BARIS 1: Cashtag + DexScreener
    cashtag_url = f"https://x.com/search?q=%24{symbol}&f=live"
    if final_ca:
        dex_url = f"https://dexscreener.com/{final_ca}"
    else:
        dex_url = f"https://dexscreener.com/search?q={symbol}"
    markup.row(
        InlineKeyboardButton("🐦 Cashtag Live", url=cashtag_url),
        InlineKeyboardButton("📊 DexScreener", url=dex_url)
    )

    # BARIS 2: TradingView Chart
    tradingview_url = f"https://www.tradingview.com/chart/?symbol=BINANCE%3A{symbol.upper()}USDT&utm_source=telegram"
    markup.row(InlineKeyboardButton("📈 TradingView Chart", url=tradingview_url))

    # BARIS 3: DEX Sniper
    if final_ca:
        platform_id_lower = asset_platform_id.lower() if asset_platform_id else ""
        if "solana" in platform_id_lower:
            markup.row(InlineKeyboardButton("🤖 Trade on BonkBot", url=f"https://t.me/bonkbot_bot?start=ref_sniper_{final_ca}"))
        else:
            markup.row(InlineKeyboardButton("🦅 Trade on Maestro", url=f"https://t.me/MaestroSniperBot?start={final_ca}-sniper"))

    # BARIS 4: CEX
    if data:
        tickers = data.get("tickers", [])
        has_binance = has_bitget = has_gate = False
        for t in tickers:
            market_name = t["market"]["name"].lower()
            target_coin = t.get("target", "").upper()
            if "USDT" in target_coin or t.get("target") == "USDT":
                if "binance" in market_name:
                    has_binance = True
                elif "bitget" in market_name:
                    has_bitget = True
                elif "gate" in market_name:
                    has_gate = True
        if has_binance:
            markup.row(InlineKeyboardButton("🟨 Trade on Binance", url=f"https://www.binance.com/en/trade/{symbol.upper()}_USDT"))
        elif has_bitget:
            markup.row(InlineKeyboardButton("🟦 Trade on Bitget", url=f"https://www.bitget.com/spot/{symbol.upper()}USDT"))
        elif has_gate:
            markup.row(InlineKeyboardButton("🟥 Trade on Gate.io", url=f"https://www.gate.io/trade/{symbol.upper()}_USDT"))

    return markup, categories, final_ca, chain_name

# ==========================================
# 6. SIMPAN TRADE
# ==========================================
def save_trade(msg_id, symbol, coin_id, sl, tp1, tp2, tp3):
    with trades_lock:
        trades = {}
        if os.path.exists("active_trades.json"):
            try:
                with open("active_trades.json", "r") as f:
                    trades = json.load(f)
            except Exception:
                trades = {}
        trades[str(msg_id)] = {
            "symbol": symbol,
            "coin_id": coin_id,
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "status": "TRACKING",
            "timestamp": time.time(),
        }
        try:
            with open("active_trades.json", "w") as f:
                json.dump(trades, f, indent=4)
        except Exception as e:
            logger.error(f"Gagal simpan trade: {e}")

# ==========================================
# 7. ENJIN SIGNAL TELEGRAM (FORMAT LENGKAP - TIDAK DIUBAH STRUKTUR)
# ==========================================
def dispatch_signal(chat_id, coin_name, symbol, rank, ath_change, vol_multiplier, rsi, current_price, fibo, coin_id, trend_24h, vol_24h, trend_7d, atr, ema7, ema21, passed_ca=None):
    if not TELEGRAM_TOKEN or not chat_id:
        return

    markup, categories, final_ca, chain_name = generate_inline_keyboard(coin_id, symbol, coin_name, contract_address=passed_ca)
    cat_name = get_category_insight(categories)

    safe_coin = html.escape(coin_name)
    safe_sym = html.escape(symbol)
    safe_chain = html.escape(chain_name) if chain_name else "Native Chain"
    vol_str = f"${vol_24h:,.0f}" if vol_24h else "N/A"
    ca_display = f"<code>{final_ca}</code>" if final_ca else "<i>No CA Found</i>"

    # =========================================================
    # [DIPERBAIKI] Multiplier SL/TP untuk Nova7 (scalping)
    # =========================================================
    if "Meme" in cat_name:
        mult_sl, mult_tp1, mult_tp2, mult_tp3 = 1.5, 2.0, 3.5, 5.0
    else:
        mult_sl, mult_tp1, mult_tp2, mult_tp3 = 1.5, 2.0, 3.5, 6.0

    sl = current_price - (mult_sl * atr)
    tp1 = current_price + (mult_tp1 * atr)
    tp2 = current_price + (mult_tp2 * atr)
    tp3 = current_price + (mult_tp3 * atr)
    # Hard floor SL (tidak terlalu jauh)
    sl = max(sl, current_price * 0.85)

    # Risk/Reward Ratio
    risk = max(current_price - sl, 1e-10)
    reward = tp2 - current_price
    rr = round(reward / risk, 2) if risk > 0 else 0

    # Signal Score & Grade
    _, grade = compute_signal_score(rsi, vol_multiplier, ath_change, rr, ema7, ema21)

    # RSI Status
    if rsi < 30:
        rsi_status = "<b>STRONG OVERSOLD</b>"
    elif rsi < 40:
        rsi_status = "<b>OVERSOLD</b>"
    else:
        rsi_status = "<b>NEUTRAL</b>"

    # EMA Trend Indicator
    ema_label = "🟢 Bullish" if ema7 >= ema21 else "🔴 Bearish"

    # Fibonacci Position
    price_to_100 = abs(current_price - fibo['Fibo_100'])
    price_to_618 = abs(current_price - fibo['Fibo_618'])
    price_to_786 = abs(current_price - fibo['Fibo_786'])
    price_to_0 = abs(current_price - fibo['Fibo_0'])
    closest_diff = min(price_to_100, price_to_618, price_to_786, price_to_0)

    if current_price > fibo['Fibo_100']:
        fibo_result = "Above Peak (>1.000) ⚠️"
    elif current_price < fibo['Fibo_0']:
        fibo_result = "Below Floor (<0.000) 🔴"
    elif closest_diff == price_to_100:
        fibo_result = "Retesting Peak (1.000)"
    elif closest_diff == price_to_618:
        fibo_result = "Golden Pocket (0.618)"
    elif closest_diff == price_to_786:
        fibo_result = "Deep Value (0.786)"
    else:
        fibo_result = "Absolute Bottom (0.000)"

    # Position Sizing (Risk Tier)
    rank_int = int(rank) if str(rank).isdigit() else 999
    if "Layer 1" in cat_name or "DeFi" in cat_name or rank_int <= 50:
        risk_tier = "Tier-1 (High Conviction - Max 5% Modal)"
    elif "Meme" in cat_name or rank_int >= 200:
        risk_tier = "Tier-3 (Tactical/Spekulatif - Max 1.5% Modal)"
    else:
        risk_tier = "Tier-2 (Standard Risk - Max 3% Modal)"

    # =========================================================
    # [DIPERBAIKI] Entry zone menggunakan Fibonacci 0.618 dan 0.786
    # (FORMAT MESEJ TIDAK BERUBAH, CUBA NILAI SAHAJA)
    # =========================================================
    entry_min_display = fibo['Fibo_618']
    entry_max_display = fibo['Fibo_786']

    msg = (
        f"🪙 <b>{safe_coin} ({safe_sym})</b> — <i>{safe_chain}</i>\n"
        f"💳 <b>CA:</b> {ca_display}\n"
        f"💵 <b>Price:</b> ${current_price:.6f} | 📊 <b>Rank:</b> #{rank}\n"
        "........................................................\n"
        f"📉 <b>24H Trend:</b> {trend_24h:+.2f}%\n"
        f"📉 <b>1W Trend:</b> {trend_7d:+.2f}%\n"
        f"🌊 <b>24H Vol:</b> {vol_str} [🔥 Spike: {vol_multiplier:.2f}x]\n"
        f"🩸 <b>ATH Drop:</b> {ath_change:.2f}%\n"
        "........................................................\n"
        f"🎯 <b>SIGNAL GRADE: {grade}</b>\n"
        f"🔥 <b>RSI (14D):</b> {rsi:.2f} ({rsi_status})\n"
        f"📈 <b>EMA Trend:</b> {ema_label} | 📐 <b>ATR:</b> ${atr:.6f}\n"
        f"📊 <b>Fibo (D1):</b> {fibo_result}\n"
        "........................................................\n"
        "🛠️ <b>ALGO TRADE SETUP (Chart: D1)</b>\n"
        f"🔸 <b>Entry Zone:</b> <code>${entry_min_display:.6f}</code> - <code>${entry_max_display:.6f}</code>\n"
        f"🛑 <b>Stop Loss:</b> <code>${sl:.6f}</code>\n\n"
        "🎯 <b>Targets:</b>\n"
        f"➡️ <b>TP1:</b> <code>${tp1:.6f}</code>\n"
        f"➡️ <b>TP2:</b> <code>${tp2:.6f}</code>\n"
        f"➡️ <b>TP3:</b> <code>${tp3:.6f}</code>\n"
        "........................................................\n"
        f"💼 <b>Capital Allocation:</b> {risk_tier}\n"
        "⚡ <b>Execution Protocol:</b> Pindahkan SL ke harga Entry (Break-Even) sebaik TP1 dicapai. Ambil 50% untung di TP2, biarkan baki 'Risk-Free' ke TP3.\n"
        "........................................................"
    )

    try:
        sent = bot.send_message(chat_id, msg, reply_markup=markup, disable_web_page_preview=True)
        if sent:
            save_trade(sent.message_id, symbol, coin_id, sl, tp1, tp2, tp3)
            journal.log_signal(
                symbol=symbol, coin_id=coin_id, entry_price=current_price,
                sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                grade=grade, coin_name=coin_name,
                risk_tier=risk_tier, msg_id=sent.message_id,
            )
    except Exception as e:
        admin_log(f"Gagal hantar signal {symbol}", e)
        logger.error(f"Mesej Telegram gagal dihantar: {e}")

# ==========================================
# 8. TRADE TRACKER (Nova7: setiap 30 saat untuk scalping)
# ==========================================
def run_trade_tracker_loop():
    while True:
        time.sleep(30)  # 30 saat untuk scalping
        if not TELEGRAM_CHAT_ID or not os.path.exists("active_trades.json"):
            continue

        with trades_lock:
            try:
                with open("active_trades.json", "r") as f:
                    trades = json.load(f)
            except Exception as e:
                logger.error(f"Gagal baca trades: {e}")
                continue

        active_items = {k: v for k, v in trades.items() if v["status"] not in ["COMPLETED", "STOP_LOSS"]}
        if not active_items:
            continue

        coin_ids = list(set([v["coin_id"] for v in active_items.values()]))
        ids_str = ",".join(coin_ids)

        headers = {"x-cg-demo-api-key": CG_API_KEY} if CG_API_KEY else {}

        current_prices = {}
        for attempt in range(3):
            try:
                res = requests.get(f"{BASE_URL}/simple/price?ids={ids_str}&vs_currencies=usd", headers=headers, timeout=15)
                if res.status_code == 429:
                    wait = 60 * (attempt + 1)
                    logger.warning(f"Tracker rate limit, tunggu {wait}s")
                    time.sleep(wait)
                    continue
                if res.status_code == 200:
                    current_prices = res.json()
                    break
            except Exception as e:
                logger.error(f"Tracker API error attempt {attempt}: {e}")
                time.sleep(10)
        else:
            continue

        updated = False
        for msg_id, trade in active_items.items():
            c_id = trade["coin_id"]
            if c_id not in current_prices:
                continue

            price_now = current_prices[c_id]["usd"]
            status = trade["status"]
            sym = trade["symbol"]

            reply_text = ""
            new_status = status

            if price_now <= trade["sl"]:
                reply_text = f"🛑 <b>{sym} — STOP LOSS HIT</b>\nProteksi modal diaktifkan pada harga <code>${price_now:.6f}</code>. Sila keluar dari pasaran."
                new_status = "STOP_LOSS"
            elif price_now >= trade["tp3"] and status != "TP2_HIT":
                reply_text = f"👑 <b>{sym} — TP3 MAX TARGET HIT!</b>\nMoonshot selesai sempurna di harga <code>${price_now:.6f}</code>! 100% sasaran hancur ditewaskan. 🎉🚀"
                new_status = "COMPLETED"
            elif price_now >= trade["tp2"] and status not in ["TP2_HIT", "COMPLETED"]:
                reply_text = f"🔥 <b>{sym} — TARGET TP2 ACHIEVED!</b>\nGolden Pocket ditembus pada harga <code>${price_now:.6f}</code>. Poketkan 50% profit, biarkan baki berjalan 'Risk-Free'!"
                new_status = "TP2_HIT"
            elif price_now >= trade["tp1"] and status == "TRACKING":
                reply_text = f"✅ <b>{sym} — TARGET TP1 SECURED!</b>\nLantunan pertama disahkan pada harga <code>${price_now:.6f}</code>. Alihkan Stop Loss kau ke harga Entry (Break-Even) SEKARANG! ⚡"
                new_status = "TP1_HIT"

            if reply_text:
                try:
                    bot.send_message(TELEGRAM_CHAT_ID, reply_text, reply_to_message_id=int(msg_id), parse_mode="HTML")
                    trades[msg_id]["status"] = new_status
                    updated = True

                    outcome_map = {
                        "TP1_HIT": "TP1_HIT",
                        "TP2_HIT": "TP2_HIT",
                        "COMPLETED": "TP3_HIT",
                        "STOP_LOSS": "STOP_LOSS"
                    }
                    journal.update_outcome(
                        coin_id=trade["coin_id"],
                        outcome=outcome_map.get(new_status, new_status),
                        exit_price=price_now,
                    )
                except Exception as e:
                    logger.error(f"Gagal hantar reply tracker: {e}")

        if updated:
            with trades_lock:
                try:
                    with open("active_trades.json", "w") as f:
                        json.dump(trades, f, indent=4)
                except Exception as e:
                    logger.error(f"Gagal simpan update tracker: {e}")

# ==========================================
# 9. WEEKLY REPORT SCHEDULER
# ==========================================
def schedule_weekly_report():
    while True:
        now = datetime.now(timezone.utc)
        days_until_sunday = (6 - now.weekday()) % 7
        if days_until_sunday == 0 and now.hour >= 20:
            days_until_sunday = 7
        target = now.replace(hour=20, minute=0, second=0, microsecond=0) + timedelta(days=days_until_sunday)
        sleep_seconds = (target - now).total_seconds()
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

        last_week = _week_key(time.time() - 86400 * 7)
        report = journal.get_weekly_report_text(week_key=last_week)
        try:
            bot.send_message(ADMIN_CHAT_ID, report, parse_mode="HTML")
            logger.info("Weekly report sent to admin")
        except Exception as e:
            logger.error(f"Gagal hantar weekly report: {e}")
        time.sleep(86400 * 7)

# ==========================================
# 10. SCANNER LOOP (DENGAN ATR MINIMUM)
# ==========================================
def run_scanner_loop():
    global is_scanning

    print("[SYSTEM] Boot Delay aktif. Menunggu 30 saat sebelum merempuh pasaran...")
    time.sleep(30)

    KILL_LIST = {"btc", "eth", "usdt", "usdc", "fdusd", "dai", "wbtc", "steth", "weeth", "weth", "tusd", "usde"}
    COOLDOWN_FILE = "signal_cooldown.json"

    def get_cooldowns():
        if os.path.exists(COOLDOWN_FILE):
            try:
                with open(COOLDOWN_FILE, "r") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def save_cooldown(coin_id):
        db = get_cooldowns()
        db[coin_id] = time.time()
        db = {k: v for k, v in db.items() if time.time() - v < 86400}
        try:
            with open(COOLDOWN_FILE, "w") as f:
                json.dump(db, f)
        except Exception:
            pass

    while True:
        if not is_scanning:
            time.sleep(10)
            continue

        logger.info("Kitaran makro bermula. Menganalisis Cuaca Makro...")
        headers = {"x-cg-demo-api-key": CG_API_KEY} if CG_API_KEY else {}

        try:
            btc_res = requests.get(f"{BASE_URL}/coins/markets?vs_currency=usd&ids=bitcoin", headers=headers, timeout=15).json()
            btc_trend_24h = btc_res[0].get('price_change_percentage_24h', 0)

            if btc_trend_24h < -4.0:
                logger.warning(f"[DEFENSE MODE] BTC berdarah ({btc_trend_24h:.2f}%). Menghentikan imbasan.")
                if ADMIN_CHAT_ID:
                    try:
                        bot.send_message(ADMIN_CHAT_ID, f"⚠️ <b>[DEFENSE MODE AKTIF]</b> BTC: <code>{btc_trend_24h:.2f}%</code>. Siklus ditunda 6 Jam.", parse_mode="HTML")
                    except Exception:
                        pass
                time.sleep(21600)
                continue

            global_res = requests.get(f"{BASE_URL}/global", headers=headers, timeout=15).json()
            if 'data' not in global_res:
                raise Exception("Respon tidak lengkap dari CoinGecko")
            btc_dominance = global_res['data']['market_cap_percentage']['btc']

            rsi_limit = 32 if (btc_dominance > 50.0 and btc_trend_24h < 0) else 40
            logger.info(f"[GLOBAL PULSE] BTC.D: {btc_dominance:.2f}% | RSI Limit: {rsi_limit}")

        except Exception as e:
            admin_log("Ralat API Cuaca Makro", e)
            logger.error(f"Ralat Cuaca Makro: {e}")
            time.sleep(60)
            continue

        top_coins = []
        for page in range(5, 7):
            if not is_scanning:
                break
            url = f"{BASE_URL}/coins/markets"
            params = {"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": page, "sparkline": "false"}
            try:
                response = requests.get(url, params=params, headers=headers, timeout=15)
                if response.status_code == 429:
                    logger.warning("[RATE LIMIT] Page scan - tunggu 90s")
                    time.sleep(90)
                    continue
                if response.status_code == 200:
                    top_coins.extend(response.json())
                time.sleep(3)
            except Exception as e:
                logger.error(f"Page fetch error: {e}")
                time.sleep(3)

        cooldown_db = get_cooldowns()
        current_time = time.time()
        candidates = []

        for coin in top_coins:
            if not is_scanning:
                break
            try:
                coin_id = coin['id']
                if coin_id in cooldown_db and current_time - cooldown_db[coin_id] < 86400:
                    continue

                symbol_lower = coin['symbol'].lower()
                if symbol_lower in KILL_LIST:
                    continue

                symbol = coin['symbol'].upper()
                ath_change = coin.get('ath_change_percentage')
                current_vol = coin.get('total_volume')

                if ath_change is None or ath_change > -50:
                    continue
                if current_vol is None or current_vol < 500000:
                    continue

                hist_url = f"{BASE_URL}/coins/{coin_id}/market_chart"
                params_hist = {"vs_currency": "usd", "days": "7", "interval": "hourly"}
                hist_res = requests.get(hist_url, params=params_hist, headers=headers, timeout=15)

                if hist_res.status_code == 429:
                    wait = 90
                    logger.warning(f"[RATE LIMIT] {coin_id} — tunggu {wait}s")
                    time.sleep(wait)
                    hist_res = requests.get(hist_url, params=params_hist, headers=headers, timeout=15)
                    if hist_res.status_code != 200:
                        continue
                elif hist_res.status_code != 200:
                    continue

                data = hist_res.json()
                prices = [p[1] for p in data['prices']]
                volumes = [v[1] for v in data['total_volumes']]
                if len(prices) < 30:
                    continue

                avg_vol_7d = np.mean(volumes[-8:-1]) if len(volumes) >= 9 else 1
                if avg_vol_7d == 0:
                    continue

                vol_mult = current_vol / avg_vol_7d
                if vol_mult < 1.5:
                    continue

                rsi_14 = calculate_rsi(prices, period=14)
                ema7 = calculate_ema(prices, 7)
                ema21 = calculate_ema(prices, 21)
                atr_val = calculate_atr(prices, period=14)  # period 14 untuk ATR yang stabil

                if vol_mult >= 2.0:
                    if rsi_14 > 50:
                        continue
                else:
                    if rsi_14 >= rsi_limit:
                        continue

                # EMA Filter
                if ema7 < ema21 * 0.94 and rsi_14 >= 30:
                    logger.debug(f"[FILTER] {symbol} ditolak: EMA trend menurun")
                    continue

                fib = calculate_fibonacci_levels(prices)
                historical_close = prices[-1]
                trend_7d_hist = ((historical_close - prices[-8]) / prices[-8]) * 100 if len(prices) >= 8 else 0

                candidates.append({
                    'coin': coin,
                    'coin_id': coin_id,
                    'symbol': symbol,
                    'ath_change': ath_change,
                    'current_vol': current_vol,
                    'vol_mult': vol_mult,
                    'rsi': rsi_14,
                    'ema7': ema7,
                    'ema21': ema21,
                    'atr': atr_val,
                    'fibo': fib,
                    'trend_7d_hist': trend_7d_hist,
                    'historical_close': historical_close
                })
                time.sleep(3)

            except Exception as e:
                logger.error(f"Error processing candidate: {e}")
                time.sleep(2)

        # Batch live price
        if candidates:
            ids_str = ",".join([c['coin_id'] for c in candidates])
            live_prices = {}
            try:
                live_res = requests.get(f"{BASE_URL}/simple/price?ids={ids_str}&vs_currencies=usd", headers=headers, timeout=15)
                if live_res.status_code == 200:
                    live_prices = live_res.json()
                time.sleep(3)
            except Exception as e:
                logger.error(f"Batch live price error: {e}")

            for cand in candidates:
                coin_id = cand['coin_id']
                live_price = live_prices.get(coin_id, {}).get("usd", cand['historical_close'])
                if live_price is None:
                    continue

                # =========================================================
                # [DIPERBAIKI] ATR minimum 0.5% dari harga dan entry zone Fibonacci
                # =========================================================
                min_atr = live_price * 0.005  # 0.5%
                atr_effective = max(cand['atr'], min_atr)

                entry_min_fibo = cand['fibo']['Fibo_618']
                entry_max_fibo = cand['fibo']['Fibo_786']

                if entry_min_fibo <= live_price <= entry_max_fibo:
                    trend_24 = cand['coin'].get('price_change_percentage_24h', 0)
                    dispatch_signal(
                        TELEGRAM_CHAT_ID, cand['coin']['name'], cand['symbol'],
                        cand['coin'].get('market_cap_rank', 'N/A'), cand['ath_change'],
                        cand['vol_mult'], cand['rsi'], live_price, cand['fibo'], coin_id,
                        trend_24, cand['current_vol'], cand['trend_7d_hist'],
                        atr_effective, cand['ema7'], cand['ema21']
                    )
                    save_cooldown(coin_id)

        if is_scanning:
            if ADMIN_CHAT_ID:
                try:
                    bot.send_message(ADMIN_CHAT_ID, "⏳ <b>[STANDBY]</b> Scanning makro selesai. Engine Cooling (6Hrs).", parse_mode="HTML")
                except Exception:
                    pass
            time.sleep(21600)

# ==========================================
# 11. TELEGRAM COMMAND HANDLERS
# ==========================================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    status = "🟢 AKTIF" if is_scanning else "🔴 STANDBY"
    bot.reply_to(message, f"⚡ <b>NOVA7 [{status}]</b>\nArahan tersedia: <code>/ca</code>, <code>/scan</code>, <code>/stop</code>, <code>/report</code>", parse_mode="HTML")

@bot.message_handler(commands=['scan'])
def start_scan_cmd(message):
    global is_scanning
    is_scanning = True
    bot.reply_to(message, "✅ <b>Enjin Nova7 Diaktifkan.</b> Bot sedang merempuh pasaran.", parse_mode="HTML")

@bot.message_handler(commands=['stop'])
def stop_scan_cmd(message):
    global is_scanning
    is_scanning = False
    bot.reply_to(message, "🛑 <b>Enjin Nova7 Dihentikan Sementara.</b>", parse_mode="HTML")

@bot.message_handler(commands=['ca'])
def manual_ca_check(message):
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "⚠️ <b>Sila masukkan Contract Address atau ID.</b>", parse_mode="HTML")
        return

    query = args[1].lower()
    bot.reply_to(message, f"🔍 <i>Menganalisis {html.escape(query)}...</i>", parse_mode="HTML")
    headers = {"x-cg-demo-api-key": CG_API_KEY} if CG_API_KEY else {}

    try:
        passed_address = query if query.startswith("0x") else None
        search_res = requests.get(f"{BASE_URL}/search?query={query}", headers=headers, timeout=15).json()
        if not search_res.get("coins"):
            bot.reply_to(message, "❌ <b>Aset tidak dijumpai.</b>", parse_mode="HTML")
            return

        coin_id = search_res["coins"][0]["id"]
        market_res = requests.get(f"{BASE_URL}/coins/markets?vs_currency=usd&ids={coin_id}", headers=headers, timeout=15).json()[0]

        coin_name = market_res['name']
        symbol = market_res['symbol'].upper()
        rank = market_res.get('market_cap_rank', 'N/A')
        ath_change = market_res.get('ath_change_percentage', 0)
        current_price_live = market_res['current_price']
        trend_24 = market_res.get('price_change_percentage_24h', 0)
        vol_24 = market_res.get('total_volume', 0)

        hist_url = f"{BASE_URL}/coins/{coin_id}/market_chart"
        hist_data = requests.get(hist_url, params={"vs_currency": "usd", "days": "7", "interval": "hourly"}, headers=headers, timeout=15).json()
        prices = [p[1] for p in hist_data['prices']]
        volumes = [v[1] for v in hist_data.get('total_volumes', [])]

        rsi_14 = calculate_rsi(prices, 14) if len(prices) >= 30 else 0.0
        fib = calculate_fibonacci_levels(prices) if len(prices) >= 30 else {"Fibo_100": current_price_live, "Fibo_786": current_price_live, "Fibo_618": current_price_live, "Fibo_0": current_price_live}
        ema7 = calculate_ema(prices, 7)
        ema21 = calculate_ema(prices, 21)
        atr_val = calculate_atr(prices, period=14)

        trend_7d = 0.0
        if len(prices) >= 8:
            trend_7d = ((prices[-1] - prices[-8]) / prices[-8]) * 100

        avg_vol7 = np.mean(volumes[-8:-1]) if len(volumes) >= 8 else vol_24
        vol_mult = vol_24 / avg_vol7 if avg_vol7 > 0 else 1.0

        # ATR minimum untuk manual
        min_atr = current_price_live * 0.005
        atr_final = max(atr_val, min_atr)

        dispatch_signal(
            TELEGRAM_CHAT_ID, coin_name, symbol, rank, ath_change, vol_mult,
            rsi_14, current_price_live, fib, coin_id, trend_24, vol_24, trend_7d,
            atr_final, ema7, ema21, passed_ca=passed_address
        )
        bot.reply_to(message, "✅ <b>Analisis Selesai!</b>", parse_mode="HTML")

    except Exception as e:
        logger.error(f"Ralat /ca manual: {e}")
        bot.reply_to(message, f"❌ <b>Ralat Teknikal:</b> Gagal memproses data pasaran.", parse_mode="HTML")

@bot.message_handler(commands=['report'])
def cmd_report(message):
    if str(message.chat.id) != ADMIN_CHAT_ID:
        bot.reply_to(message, "Akses ditolak. Command ini untuk admin sahaja.")
        return
    week_key = _week_key(time.time())
    report = journal.get_weekly_report_text(week_key=week_key)
    bot.reply_to(message, report, parse_mode="HTML")

# ==========================================
# 12. SISTEM KAWALAN UTAMA
# ==========================================
def graceful_shutdown(*args):
    if TELEGRAM_TOKEN and ADMIN_CHAT_ID:
        try:
            bot.send_message(ADMIN_CHAT_ID, "🔴 <b>[OFFLINE] NOVA7 DISCONNECTED.</b> Render shutting down.", parse_mode="HTML")
        except Exception:
            pass
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)

    RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if RENDER_URL:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=f"{RENDER_URL}/webhook")
        print(f"[WEBHOOK] Aktif: {RENDER_URL}/webhook")
    else:
        print("[WARNING] RENDER_EXTERNAL_URL tiada — webhook tidak ditetapkan.")

    if TELEGRAM_TOKEN and ADMIN_CHAT_ID:
        try:
            bot.send_message(ADMIN_CHAT_ID, "🟢 <b>HELLO, NOVA7 NOW ACTIVE.</b>\nLink to Render established.", parse_mode="HTML")
        except Exception:
            pass

    threading.Thread(target=run_trade_tracker_loop, daemon=True).start()
    threading.Thread(target=run_scanner_loop, daemon=True).start()
    threading.Thread(target=schedule_weekly_report, daemon=True).start()

    journal.register_commands(bot, ADMIN_CHAT_ID)

    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))