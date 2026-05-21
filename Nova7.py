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

# =========================================================
# NEW: Import multi-source price feed
# =========================================================
from binance_price_feed import PriceFeed

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

# =========================================================
# NEW: Initialize PriceFeed (Binance/DexScreener/Coingecko)
# =========================================================
price_feed = PriceFeed(cg_api_key=CG_API_KEY)

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

def calculate_atr(prices, period=14):
    arr = np.array(prices, dtype=float)
    if len(arr) < period + 1:
        return float(arr[-1]) * 0.05
    moves = np.abs(np.diff(arr))
    atr = np.mean(moves[:period])
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
# 6. SIMPAN TRADE (DIUBAH: TAMBAH CONTRACT ADDRESS)
# ==========================================
def save_trade(msg_id, symbol, coin_id, sl, tp1, tp2, tp3, contract=None):
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
            "contract": contract,  # ← untuk DexScreener routing
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
# 7. ENJIN SIGNAL TELEGRAM (FORMAT LENGKAP)
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

    if "Meme" in cat_name:
        mult_sl, mult_tp1, mult_tp2, mult_tp3 = 1.5, 2.0, 3.5, 5.0
    else:
        mult_sl, mult_tp1, mult_tp2, mult_tp3 = 1.5, 2.0, 3.5, 6.0

    sl = current_price - (mult_sl * atr)
    tp1 = current_price + (mult_tp1 * atr)
    tp2 = current_price + (mult_tp2 * atr)
    tp3 = current_price + (mult_tp3 * atr)
    sl = max(sl, current_price * 0.85)

    risk = max(current_price - sl, 1e-10)
    reward = tp2 - current_price
    rr = round(reward / risk, 2) if risk > 0 else 0

    _, grade = compute_signal_score(rsi, vol_multiplier, ath_change, rr, ema7, ema21)

    if rsi < 30:
        rsi_status = "<b>STRONG OVERSOLD</b>"
    elif rsi < 40:
        rsi_status = "<b>OVERSOLD</b>"
    else:
        rsi_status = "<b>NEUTRAL</b>"

    ema_label = "🟢 Bullish" if ema7 >= ema21 else "🔴 Bearish"

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

    rank_int = int(rank) if str(rank).isdigit() else 999
    if "Layer 1" in cat_name or "DeFi" in cat_name or rank_int <= 50:
        risk_tier = "Tier-1 (High Conviction - Max 5% Modal)"
    elif "Meme" in cat_name or rank_int >= 200:
        risk_tier = "Tier-3 (Tactical/Spekulatif - Max 1.5% Modal)"
    else:
        risk_tier = "Tier-2 (Standard Risk - Max 3% Modal)"

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
            # 🔁 Pass contract address to save_trade
            save_trade(sent.message_id, symbol, coin_id, sl, tp1, tp2, tp3, contract=final_ca)
            journal.log_signal(
                symbol=symbol, coin_id=coin_id, entry_price=current_price,
                sl=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                grade=grade, coin_name=coin_name,
                risk_tier=risk_tier, msg_id=sent.message_id,
            )
    except Exception as e:
        admin_log(f"Gagal hantar signal {symbol}", e)
        logger.error(f"Mesej Telegram gagal dihantar: {e}")

# =========================================================
# 8. TRADE TRACKER (NEW – MULTI-SOURCE BINANCE/DEX/COINGECKO)
# =========================================================
def run_trade_tracker_loop():
    while True:
        time.sleep(30)

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

        unique_coins = {}
        for msg_id, trade in active_items.items():
            coin_id = trade["coin_id"]
            if coin_id not in unique_coins:
                unique_coins[coin_id] = {
                    'coin_id': coin_id,
                    'symbol': trade["symbol"],
                    'contract': trade.get("contract")
                }

        coin_list = list(unique_coins.values())

        try:
            current_prices = price_feed.get_prices(coin_list)
        except Exception as e:
            logger.error(f"PriceFeed error: {e}")
            admin_log("PriceFeed batch error", e)
            continue

        if not current_prices:
            logger.warning("[TRACKER] Tiada harga diperoleh dari semua source")
            continue

        updated = False
        for msg_id, trade in active_items.items():
            c_id = trade["coin_id"]
            if c_id not in current_prices:
                continue

            price_now = current_prices[c_id]
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
                    # ✅ Betul: satu baris tanpa kurungan terpisah
                    journal.update_outcome(coin_id=trade["coin_id"], outcome=outcome_map.get(new_status, new_status), exit_price=price_now)

                except Exception as e:
                    logger.error(f"Gagal hantar reply tracker: {e}")

        if updated:
            with trades_lock:
                try:
                    with open("active_trades.json", "w") as f:
                        json.dump(trades, f, indent=4)
                except Exception as e:
                    logger.error(f"Gagal simpan update tracker: {e}")
                    journal.update_outcome(
                        coin_id=trade["coin_id"],
                        outcome=outcome_map.get(new_status, new_status),
                        exit_price=price_now,
         app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
