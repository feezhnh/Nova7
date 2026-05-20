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

# ==========================================
# LOGGING SISTEM
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ==========================================
# 1. KONFIGURASI & KESELAMATAN (ENV) [LOCKED]
# ==========================================
CG_API_KEY      = os.environ.get("CG_API_KEY", "")
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ADMIN_CHAT_ID   = os.environ.get("ADMIN_CHAT_ID")
BASE_URL        = "https://api.coingecko.com/api/v3"

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")

# Thread-safe controls
scan_event   = threading.Event()
scan_event.set()                    # Default: AKTIF
trades_lock  = threading.Lock()

# ==========================================
# 2. DUMMY WEB SERVER (RENDER KEEP-ALIVE)
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    status = "AKTIF" if scan_event.is_set() else "STANDBY"
    return f"Engine Nova7 v2.0 [{status}] ✅"

# ==========================================
# 2.5 ADMIN LOG (GOD MODE)
# ==========================================
def admin_log(context: str, error: Exception):
    if not ADMIN_CHAT_ID:
        return
    try:
        msg = (
            f"☢️ <b>[NOVA7 ERROR]</b> {html.escape(context)}\n"
            f"<code>{html.escape(str(error)[:400])}</code>"
        )
        bot.send_message(ADMIN_CHAT_ID, msg, parse_mode="HTML")
    except Exception:
        pass

# ==========================================
# 3. INDIKATOR TEKNIKAL (FIXED & ACCURATE)
# ==========================================

def calculate_rsi(prices: list, period: int = 14) -> float:
    """
    Wilder's Smoothed RSI — betul secara matematik.
    Tiada zero-array bug seperti versi lama.
    """
    arr = np.array(prices, dtype=float)
    if len(arr) < period + 1:
        return 50.0

    deltas = np.diff(arr)
    gains  = np.where(deltas > 0,  deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    # Seed: purata mudah bagi 'period' pertama
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))

    # Wilder smoothing untuk baki data
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i])  / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0.0:
        return 100.0

    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def calculate_ema(prices: list, period: int) -> float:
    """
    Exponential Moving Average — standard formula.
    """
    arr = np.array(prices, dtype=float)
    if len(arr) < period:
        return float(arr[-1])

    k   = 2.0 / (period + 1)
    ema = float(np.mean(arr[:period]))   # SMA sebagai seed
    for price in arr[period:]:
        ema = price * k + ema * (1.0 - k)
    return round(ema, 10)


def calculate_atr(prices: list, period: int = 14) -> float:
    """
    Pseudo-ATR dari close prices harian.
    ATR ≈ purata |close[i] - close[i-1]| untuk N hari.
    Digunakan untuk sizing SL/TP berdasarkan volatiliti sebenar.
    """
    arr = np.array(prices, dtype=float)
    if len(arr) < period + 1:
        return float(arr[-1]) * 0.05   # Fallback: 5% harga

    daily_moves = np.abs(np.diff(arr[-(period + 1):]))
    return float(np.mean(daily_moves))


def calculate_fibonacci_levels(prices: list) -> dict:
    high_p = max(prices)
    low_p  = min(prices)
    diff   = high_p - low_p
    return {
        "Fibo_100": high_p,
        "Fibo_618": high_p - (0.618 * diff),
        "Fibo_786": high_p - (0.786 * diff),
        "Fibo_0":   low_p,
    }


def get_fibo_zone(current_price: float, fibo: dict) -> str:
    """Tentukan zon Fibonacci semasa secara tepat."""
    if current_price > fibo['Fibo_100']:
        return "Above Peak (>1.000) ⚠️"
    if current_price < fibo['Fibo_0']:
        return "Below Floor (<0.000) 🔴"

    zones = {
        "Near Peak (1.000)":    fibo['Fibo_100'],
        "Golden Pocket (0.618)": fibo['Fibo_618'],
        "Deep Value (0.786)":   fibo['Fibo_786'],
        "Floor Zone (0.000)":   fibo['Fibo_0'],
    }
    return min(zones, key=lambda z: abs(current_price - zones[z]))


def compute_signal_score(rsi: float, vol_mult: float, ath_change: float,
                         rr_ratio: float, ema7: float, ema21: float) -> tuple:
    """
    Scoring signal 0-100 berdasarkan 5 faktor.
    Return: (score, grade_str)
    """
    score = 0

    # RSI (max 30 pts)
    if rsi < 25:   score += 30
    elif rsi < 30: score += 25
    elif rsi < 35: score += 18
    elif rsi < 40: score += 12
    elif rsi < 50: score += 6

    # Volume spike (max 25 pts)
    if vol_mult >= 4.0:   score += 25
    elif vol_mult >= 3.0: score += 20
    elif vol_mult >= 2.0: score += 14
    elif vol_mult >= 1.5: score += 8

    # ATH jarak (max 20 pts)
    if ath_change < -80:  score += 20
    elif ath_change < -70: score += 16
    elif ath_change < -60: score += 12
    elif ath_change < -50: score += 8

    # R:R ratio (max 15 pts)
    if rr_ratio >= 4.0:   score += 15
    elif rr_ratio >= 3.0: score += 12
    elif rr_ratio >= 2.0: score += 8
    elif rr_ratio >= 1.5: score += 4

    # EMA trend (max 10 pts)
    if ema7 >= ema21:     score += 10
    elif ema7 >= ema21 * 0.97: score += 5

    if score >= 80:   grade = "⭐⭐⭐ A+ (Max Conviction)"
    elif score >= 65: grade = "⭐⭐ B+ (High Conviction)"
    elif score >= 50: grade = "⭐ C+ (Standard)"
    else:             grade = "⚠️ D (Caution — Low Conviction)"

    return score, grade


# ==========================================
# 4. KATEGORI & RISK TIER (DATA-DRIVEN)
# ==========================================

def get_category_name(categories: list) -> str:
    """Nama kategori sahaja. Tiada teks rekaan / on-chain palsu."""
    cat_str = ", ".join(categories).lower() if categories else ""

    if "layer 1" in cat_str or "smart contract" in cat_str:
        return "Layer 1 (L1)"
    if "layer 2" in cat_str or "rollup" in cat_str:
        return "Layer 2 (L2)"
    if "defi" in cat_str or "decentralized finance" in cat_str:
        return "DeFi"
    if "gaming" in cat_str or "play to earn" in cat_str:
        return "GameFi / Web3 Gaming"
    if "meme" in cat_str:
        return "Meme Token ⚠️"
    if "artificial intelligence" in cat_str or " ai" in cat_str:
        return "AI / Compute"
    if "real world assets" in cat_str or "rwa" in cat_str:
        return "RWA"
    if "exchange-based token" in cat_str:
        return "Exchange Token"

    return categories[0] if categories else "Altcoin"


def get_risk_tier(cat_name: str, rank) -> str:
    rank_int = int(rank) if str(rank).isdigit() else 9999

    if "Meme" in cat_name or rank_int > 300:
        return "Tier-3 🔴  Spekulatif — Max 1% Modal"
    if rank_int <= 50 or "Layer 1" in cat_name or "Layer 2" in cat_name:
        return "Tier-1 🟢  High Conviction — Max 5% Modal"
    return "Tier-2 🟡  Standard — Max 3% Modal"


# ==========================================
# 5. PENGURUSAN FAIL KEKAL (THREAD-SAFE)
# ==========================================
COOLDOWN_FILE = "signal_cooldown.json"
TRADES_FILE   = "active_trades.json"


def get_cooldowns() -> dict:
    if not os.path.exists(COOLDOWN_FILE):
        return {}
    try:
        with open(COOLDOWN_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cooldown(coin_id: str):
    try:
        db = get_cooldowns()
        db[coin_id] = time.time()
        # Auto-prune: buang entri > 24 jam
        db = {k: v for k, v in db.items() if time.time() - v < 86400}
        with open(COOLDOWN_FILE, "w") as f:
            json.dump(db, f)
    except Exception as e:
        logger.error(f"Gagal simpan cooldown: {e}")


def save_trade(msg_id: int, symbol: str, coin_id: str,
               sl: float, tp1: float, tp2: float, tp3: float):
    """Simpan trade baru ke JSON untuk tracker loop."""
    with trades_lock:
        trades = {}
        if os.path.exists(TRADES_FILE):
            try:
                with open(TRADES_FILE, "r") as f:
                    trades = json.load(f)
            except Exception:
                trades = {}

        trades[str(msg_id)] = {
            "symbol":    symbol,
            "coin_id":   coin_id,
            "sl":        sl,
            "tp1":       tp1,
            "tp2":       tp2,
            "tp3":       tp3,
            "status":    "TRACKING",
            "timestamp": time.time(),
        }

        try:
            with open(TRADES_FILE, "w") as f:
                json.dump(trades, f, indent=4)
        except Exception as e:
            logger.error(f"Gagal simpan trade: {e}")


# ==========================================
# 6. PENJANA INLINE KEYBOARD
# ==========================================

def generate_inline_keyboard(coin_id: str, symbol: str,
                              contract_address=None) -> tuple:
    markup   = InlineKeyboardMarkup(row_width=2)
    headers  = {"x-cg-demo-api-key": CG_API_KEY} if CG_API_KEY else {}
    categories        = []
    chain_name        = "Native Chain"
    asset_platform_id = ""

    try:
        url = (
            f"{BASE_URL}/coins/{coin_id}"
            f"?localization=false&tickers=true"
            f"&market_data=false&community_data=false&developer_data=false"
        )
        res  = requests.get(url, headers=headers, timeout=15)
        res.raise_for_status()
        data = res.json()

        categories        = data.get("categories", [])
        asset_platform_id = data.get("asset_platform_id", "")
        if asset_platform_id:
            chain_name = asset_platform_id.replace("-", " ").title()

        if not contract_address:
            platforms = data.get("platforms", {})
            if platforms:
                contract_address = list(platforms.values())[0]

        # Row 1 — Cashtag & DexScreener
        dex_url = (
            f"https://dexscreener.com/search?q={contract_address}"
            if contract_address
            else f"https://dexscreener.com/search?q={symbol}"
        )
        markup.row(
            InlineKeyboardButton("🐦 Cashtag Live",
                url=f"https://twitter.com/search?q=%24{symbol}&f=live"),
            InlineKeyboardButton("📊 DexScreener", url=dex_url),
        )

        # Row 2 — DEX Sniper
        if contract_address:
            p_lower = asset_platform_id.lower()
            if "solana" in p_lower:
                markup.row(InlineKeyboardButton(
                    "🤖 Fast Snipe on BonkBot",
                    url=f"https://t.me/bonkbot_bot?start=ref_krypton_{contract_address}",
                ))
            else:
                markup.row(InlineKeyboardButton(
                    "🦅 Fast Snipe on Maestro",
                    url=f"https://t.me/MaestroSniperBot?start={contract_address}-krypton",
                ))

        # Row 3 — CEX (Binance > Bitget > Gate)
        tickers     = data.get("tickers", [])
        has_binance = has_bitget = has_gate = False
        for t in tickers:
            market = t["market"]["name"].lower()
            if t.get("target", "").upper() == "USDT":
                if "binance" in market:   has_binance = True
                elif "bitget" in market:  has_bitget  = True
                elif "gate" in market:    has_gate    = True

        if has_binance:
            markup.row(InlineKeyboardButton(
                "🟨 Trade on Binance",
                url=f"https://www.binance.com/en/trade/{symbol.upper()}_USDT",
            ))
        elif has_bitget:
            markup.row(InlineKeyboardButton(
                "🟦 Trade on Bitget",
                url=f"https://www.bitget.com/spot/{symbol.upper()}USDT",
            ))
        elif has_gate:
            markup.row(InlineKeyboardButton(
                "🟥 Trade on Gate.io",
                url=f"https://www.gate.io/trade/{symbol.upper()}_USDT",
            ))

    except Exception as e:
        admin_log(f"Ralat Keyboard ({symbol})", e)
        logger.error(f"Ralat keyboard {symbol}: {e}")

    return markup, categories, contract_address, chain_name


# ==========================================
# 7. ENJIN SIGNAL TELEGRAM
# ==========================================

def dispatch_signal(chat_id, coin_name, symbol, rank, ath_change,
                    vol_multiplier, rsi, current_price, fibo, coin_id,
                    trend_24h, vol_24h, trend_7d, atr, ema7, ema21,
                    passed_ca=None):
    if not TELEGRAM_TOKEN or not chat_id:
        return

    markup, categories, final_ca, chain_name = generate_inline_keyboard(
        coin_id, symbol, contract_address=passed_ca
    )

    cat_name  = get_category_name(categories)
    risk_tier = get_risk_tier(cat_name, rank)

    # --- ATR-based SL/TP (volatility-adjusted) ---
    # Meme tokens: lebih aggressive multiplier kerana swing lebih gila
    if "Meme" in cat_name:
        mult_sl, mult_tp1, mult_tp2, mult_tp3 = 1.5, 2.5, 4.5, 7.0
    elif "Layer 1" in cat_name or "Layer 2" in cat_name:
        mult_sl, mult_tp1, mult_tp2, mult_tp3 = 2.0, 1.5, 3.0, 5.0
    else:
        mult_sl, mult_tp1, mult_tp2, mult_tp3 = 2.0, 1.5, 3.0, 5.0

    sl  = current_price - (mult_sl  * atr)
    tp1 = current_price + (mult_tp1 * atr)
    tp2 = current_price + (mult_tp2 * atr)
    tp3 = current_price + (mult_tp3 * atr)

    # Hard floor: SL tidak melebihi 3% bawah Fibo_0 (floor sejarah 30D)
    sl = min(sl, fibo['Fibo_0'] * 0.97)

    # R:R Ratio (ke TP2)
    risk   = max(current_price - sl, 1e-10)
    reward = tp2 - current_price
    rr     = round(reward / risk, 2)

    # Signal Score
    _, grade = compute_signal_score(rsi, vol_multiplier, ath_change, rr, ema7, ema21)

    # Formatting
    vol_str    = f"${vol_24h:,.0f}" if vol_24h else "N/A"
    ca_display = f"<code>{final_ca}</code>" if final_ca else "<i>No CA Found</i>"
    fibo_zone  = get_fibo_zone(current_price, fibo)

    rsi_label = (
        "🟢 STRONG OVERSOLD" if rsi < 30 else
        "🟡 OVERSOLD"        if rsi < 40 else
        "⚪ NEUTRAL-BEARISH"  if rsi < 50 else
        "🔴 NEUTRAL"
    )

    ema_label = (
        f"🟢 Bullish (EMA7 > EMA21)"
        if ema7 >= ema21 else
        f"🔴 Bearish (EMA7 < EMA21)"
    )

    msg = (
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 <b>{html.escape(coin_name)} ({html.escape(symbol)})</b>\n"
        f"🔗 <b>Chain:</b> {html.escape(chain_name)}  |  🏆 <b>Rank:</b> #{rank}\n"
        f"🏷️ <b>Sektor:</b> {html.escape(cat_name)}\n"
        f"💳 <b>CA:</b> {ca_display}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>Harga:</b> <code>${current_price:.6f}</code>\n"
        f"📉 <b>24H:</b> {trend_24h:+.2f}%  |  <b>7D:</b> {trend_7d:+.2f}%\n"
        f"🩸 <b>Dari ATH:</b> {ath_change:.2f}%\n"
        f"🌊 <b>Vol 24H:</b> {vol_str}  [🔥 Spike: {vol_multiplier:.2f}x]\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>ANALISIS TEKNIKAL (D1)</b>\n"
        f"🔹 <b>RSI-14:</b> {rsi:.1f}  —  {rsi_label}\n"
        f"🔹 <b>EMA Trend:</b> {ema_label}\n"
        f"🔹 <b>Zon Fibo:</b> {fibo_zone}\n"
        f"🔹 <b>ATR-14:</b> <code>${atr:.6f}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>SIGNAL GRADE: {grade}</b>\n"
        f"📐 <b>R:R Ratio:</b> 1 : {rr}  (ke TP2)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🛠️ <b>TRADE SETUP (D1 Chart)</b>\n"
        f"🟦 <b>Entry Zone:</b> <code>${current_price:.6f}</code> — <code>${fibo['Fibo_786']:.6f}</code>\n"
        f"🛑 <b>Stop Loss:</b>  <code>${sl:.6f}</code>  ({mult_sl}× ATR)\n\n"
        f"🎯 <b>Targets:</b>\n"
        f"  ✅ <b>TP1:</b> <code>${tp1:.6f}</code>  —  Alih SL ke Entry (Break-Even)\n"
        f"  🔥 <b>TP2:</b> <code>${tp2:.6f}</code>  —  Ambil 50% Profit\n"
        f"  👑 <b>TP3:</b> <code>${tp3:.6f}</code>  —  Biarkan Risk-Free\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 <b>Peruntukan:</b> {risk_tier}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱️ <i>Nova7 Engine v2.0</i>"
    )

    try:
        sent = bot.send_message(chat_id, msg, reply_markup=markup,
                                disable_web_page_preview=True)
        if sent:
            save_trade(sent.message_id, symbol, coin_id, sl, tp1, tp2, tp3)
        logger.info(f"[SIGNAL] {symbol} | {grade} | R:R {rr} | RSI {rsi:.1f} | Vol {vol_multiplier:.2f}x")
    except Exception as e:
        admin_log(f"Gagal hantar signal {symbol}", e)
        logger.error(f"Telegram gagal: {e}")


# ==========================================
# 8. TRADE TRACKER LOOP (FIXED — FULLY FUNCTIONAL)
# ==========================================

def run_trade_tracker_loop():
    logger.info("[TRACKER] Loop dimulakan.")
    while True:
        time.sleep(300)   # Semak setiap 5 minit

        if not TELEGRAM_CHAT_ID or not os.path.exists(TRADES_FILE):
            continue

        with trades_lock:
            try:
                with open(TRADES_FILE, "r") as f:
                    trades = json.load(f)
            except Exception as e:
                logger.error(f"Gagal baca trades: {e}")
                continue

        active = {
            k: v for k, v in trades.items()
            if v.get("status") not in ["COMPLETED", "STOP_LOSS"]
        }
        if not active:
            continue

        ids_str = ",".join(set(v["coin_id"] for v in active.values()))
        headers = {"x-cg-demo-api-key": CG_API_KEY} if CG_API_KEY else {}

        try:
            res = requests.get(
                f"{BASE_URL}/simple/price?ids={ids_str}&vs_currencies=usd",
                headers=headers, timeout=15,
            )
            if res.status_code != 200:
                continue
            live_prices = res.json()
        except Exception as e:
            logger.error(f"Tracker API error: {e}")
            continue

        updated = False
        for msg_id, trade in active.items():
            cid      = trade["coin_id"]
            if cid not in live_prices:
                continue

            now    = live_prices[cid]["usd"]
            status = trade["status"]
            sym    = trade["symbol"]
            text   = ""
            new_st = status

            if now <= trade["sl"]:
                text   = (
                    f"🛑 <b>{sym} — STOP LOSS HIT</b>\n"
                    f"Harga: <code>${now:.6f}</code>\n"
                    f"Proteksi modal diaktifkan. Keluar pasaran sekarang."
                )
                new_st = "STOP_LOSS"

            elif now >= trade["tp3"] and status != "COMPLETED":
                text   = (
                    f"👑 <b>{sym} — TP3 MOONSHOT!</b>\n"
                    f"Harga: <code>${now:.6f}</code>\n"
                    f"Semua target dihancurkan. Tahniah! 🎉🚀"
                )
                new_st = "COMPLETED"

            elif now >= trade["tp2"] and status not in ["TP2_HIT", "COMPLETED"]:
                text   = (
                    f"🔥 <b>{sym} — TP2 HIT!</b>\n"
                    f"Harga: <code>${now:.6f}</code>\n"
                    f"Poket 50% profit. Baki berjalan risk-free ke TP3."
                )
                new_st = "TP2_HIT"

            elif now >= trade["tp1"] and status == "TRACKING":
                text   = (
                    f"✅ <b>{sym} — TP1 SECURED!</b>\n"
                    f"Harga: <code>${now:.6f}</code>\n"
                    f"⚡ Pindah SL ke Entry (Break-Even) SEKARANG!"
                )
                new_st = "TP1_HIT"

            if text:
                try:
                    bot.send_message(
                        TELEGRAM_CHAT_ID, text,
                        reply_to_message_id=int(msg_id),
                        parse_mode="HTML",
                    )
                    trades[msg_id]["status"] = new_st
                    updated = True
                    logger.info(f"[TRACKER] {sym} → {new_st}")
                except Exception as e:
                    logger.error(f"Tracker reply gagal: {e}")

        if updated:
            with trades_lock:
                try:
                    with open(TRADES_FILE, "w") as f:
                        json.dump(trades, f, indent=4)
                except Exception as e:
                    logger.error(f"Gagal update trades: {e}")


# ==========================================
# 9. ENJIN PENGIMBASAN UTAMA
# ==========================================
KILL_LIST = {
    "btc", "eth", "usdt", "usdc", "fdusd", "dai", "wbtc",
    "steth", "weeth", "weth", "tusd", "usde", "busd", "usds",
    "pyusd", "usdd", "frax", "lusd", "susd",
}


def run_scanner_loop():
    logger.info("[SCANNER] Nova7 Engine dimulakan.")
    time.sleep(30)   # Boot delay

    while True:
        if not scan_event.is_set():
            time.sleep(10)
            continue

        logger.info("[SCANNER] Kitaran baru — analisis makro...")
        headers = {"x-cg-demo-api-key": CG_API_KEY} if CG_API_KEY else {}

        # ── PERISAI MAKRO ────────────────────────────────────────────
        try:
            btc_data     = requests.get(
                f"{BASE_URL}/coins/markets?vs_currency=usd&ids=bitcoin",
                headers=headers, timeout=15,
            ).json()
            btc_24h      = btc_data[0].get("price_change_percentage_24h", 0.0)

            if btc_24h < -4.0:
                msg_def = (
                    f"⚠️ <b>[DEFENSE MODE]</b> BTC: <code>{btc_24h:.2f}%</code>\n"
                    f"Imbasan altcoin ditangguh 6 jam."
                )
                logger.warning(f"[DEFENSE] BTC {btc_24h:.2f}% — 6h cooldown.")
                if ADMIN_CHAT_ID:
                    try: bot.send_message(ADMIN_CHAT_ID, msg_def, parse_mode="HTML")
                    except Exception: pass
                time.sleep(21600)
                continue

            global_data  = requests.get(
                f"{BASE_URL}/global", headers=headers, timeout=15
            ).json()
            if "data" not in global_data:
                raise ValueError(f"Global data tidak lengkap: {global_data}")

            btc_dom      = global_data["data"]["market_cap_percentage"]["btc"]
            rsi_limit    = 32 if (btc_dom > 50.0 and btc_24h < 0) else 40
            logger.info(f"[MAKRO] BTC.D: {btc_dom:.1f}% | BTC 24H: {btc_24h:.2f}% | RSI Limit: {rsi_limit}")

        except Exception as e:
            admin_log("Ralat API Makro", e)
            logger.error(f"Ralat makro: {e}")
            time.sleep(60)
            continue

        # ── TARIK SENARAI KOIN ───────────────────────────────────────
        top_coins = []
        # NOVA7: Page 5-6 (rank ~1001-1500)
        for page in range(5, 7):
            if not scan_event.is_set(): break
            try:
                resp = requests.get(
                    f"{BASE_URL}/coins/markets",
                    params={
                        "vs_currency":          "usd",
                        "order":                "market_cap_desc",
                        "per_page":             250,
                        "page":                 page,
                        "sparkline":            "false",
                        "price_change_percentage": "24h,7d",
                    },
                    headers=headers, timeout=20,
                )
                if resp.status_code == 200:
                    top_coins.extend(resp.json())
                    logger.info(f"[SCANNER] Page {page}: {len(resp.json())} koin")
                elif resp.status_code == 429:
                    logger.warning("[RATE LIMIT] Tunggu 60s...")
                    time.sleep(60)
                time.sleep(3)
            except Exception as e:
                logger.error(f"Ralat tarik page {page}: {e}")
                time.sleep(3)

        cooldown_db  = get_cooldowns()
        now_ts       = time.time()
        signal_count = 0

        # ── LOOP UTAMA SETIAP KOIN ───────────────────────────────────
        for coin in top_coins:
            if not scan_event.is_set(): break

            coin_id = coin.get("id", "")
            symbol  = coin.get("symbol", "").upper()

            try:
                # Filter stablecoin & BTC/ETH
                if coin.get("symbol", "").lower() in KILL_LIST: continue

                # Semak cooldown 24 jam
                if coin_id in cooldown_db:
                    if now_ts - cooldown_db[coin_id] < 86400: continue

                ath_change    = coin.get("ath_change_percentage")
                current_vol   = coin.get("total_volume")
                current_price = coin.get("current_price")

                # ── Filter Pintu ─────────────────────────────────────
                if ath_change is None or ath_change > -50: continue     # ATH drop > 50%
                if current_vol is None or current_vol < 500_000: continue
                if current_price is None or current_price <= 0: continue

                # ── Tarik Data Sejarah 30D ───────────────────────────
                hist_res = requests.get(
                    f"{BASE_URL}/coins/{coin_id}/market_chart",
                    params={"vs_currency": "usd", "days": "30", "interval": "daily"},
                    headers=headers, timeout=20,
                )

                if hist_res.status_code == 429:
                    logger.warning(f"[RATE LIMIT] {coin_id} — tunggu 60s")
                    time.sleep(60)
                    continue
                if hist_res.status_code != 200:
                    time.sleep(2)
                    continue

                hist_data = hist_res.json()
                prices    = [p[1] for p in hist_data.get("prices", [])]
                volumes   = [v[1] for v in hist_data.get("total_volumes", [])]

                if len(prices) < 20:
                    time.sleep(2)
                    continue

                # ── Indikator ────────────────────────────────────────
                avg_vol_7d = np.mean(volumes[-8:-1]) if len(volumes) >= 8 else 0
                if avg_vol_7d == 0: continue

                vol_mult = current_vol / avg_vol_7d
                if vol_mult < 1.5: continue

                rsi_14 = calculate_rsi(prices)
                ema7   = calculate_ema(prices, 7)
                ema21  = calculate_ema(prices, 21)
                atr    = calculate_atr(prices)
                fibo   = calculate_fibonacci_levels(prices)
                price  = prices[-1]

                # ── RSI Filter ───────────────────────────────────────
                if vol_mult >= 2.0:
                    if rsi_14 > 50: continue
                else:
                    if rsi_14 >= rsi_limit: continue

                # ── Fibo Filter: harga mesti dalam zon value ─────────
                if price > fibo["Fibo_618"]: continue

                # ── EMA Filter: jangan signal masa trend sangat bearish
                # Pengecualian: RSI sangat oversold (< 30) = kemungkinan reversal
                if ema7 < ema21 * 0.94 and rsi_14 >= 30: continue

                # ── 7D Trend ─────────────────────────────────────────
                trend_7d  = 0.0
                if len(prices) >= 8:
                    trend_7d = ((price - prices[-8]) / prices[-8]) * 100

                trend_24h = coin.get("price_change_percentage_24h", 0.0) or 0.0

                # ── Tembak Signal ────────────────────────────────────
                dispatch_signal(
                    TELEGRAM_CHAT_ID,
                    coin["name"], symbol,
                    coin.get("market_cap_rank", "N/A"),
                    ath_change, vol_mult, rsi_14, price,
                    fibo, coin_id, trend_24h, current_vol,
                    trend_7d, atr, ema7, ema21,
                )

                save_cooldown(coin_id)
                cooldown_db[coin_id] = now_ts
                signal_count += 1
                time.sleep(3)

            except Exception as e:
                logger.error(f"[SCANNER] Error {coin_id}: {e}")
                time.sleep(2)

        # ── Tamat Kitaran ────────────────────────────────────────────
        summary = (
            f"⏳ <b>[STANDBY]</b> Kitaran selesai.\n"
            f"📤 <b>{signal_count} signal</b> dihantar hari ini.\n"
            f"🕐 Cooling 6 jam."
        )
        logger.info(f"[SCANNER] {signal_count} signal dihantar. Cooling 6 jam.")
        if ADMIN_CHAT_ID:
            try: bot.send_message(ADMIN_CHAT_ID, summary, parse_mode="HTML")
            except Exception: pass

        time.sleep(21600)


# ==========================================
# 10. TELEGRAM COMMAND HANDLERS
# ==========================================

@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    status = "🟢 AKTIF" if scan_event.is_set() else "🔴 STANDBY"
    bot.reply_to(message,
        f"⚡ <b>Nova7 Engine v2.0 [{status}]</b>\n\n"
        f"<code>/scan</code>   — Aktifkan enjin\n"
        f"<code>/stop</code>   — Tangguh enjin\n"
        f"<code>/ca [id]</code> — Analisis manual\n"
        f"<code>/status</code> — Laporan sistem",
        parse_mode="HTML",
    )


@bot.message_handler(commands=["status"])
def show_status(message):
    trades = {}
    if os.path.exists(TRADES_FILE):
        try:
            with open(TRADES_FILE, "r") as f:
                trades = json.load(f)
        except Exception:
            pass

    active    = sum(1 for t in trades.values() if t.get("status") not in ["COMPLETED", "STOP_LOSS"])
    completed = sum(1 for t in trades.values() if t.get("status") == "COMPLETED")
    stopped   = sum(1 for t in trades.values() if t.get("status") == "STOP_LOSS")

    bot.reply_to(message,
        f"📊 <b>Nova7 Status</b>\n\n"
        f"🔧 Engine: {'🟢 AKTIF' if scan_event.is_set() else '🔴 STANDBY'}\n"
        f"📈 Trade Aktif:   {active}\n"
        f"✅ Completed:     {completed}\n"
        f"🛑 Stop Loss:     {stopped}\n"
        f"📁 Total Tracked: {len(trades)}",
        parse_mode="HTML",
    )


@bot.message_handler(commands=["scan"])
def start_scan_cmd(message):
    scan_event.set()
    bot.reply_to(message, "✅ <b>Engine Nova7 Diaktifkan.</b>", parse_mode="HTML")


@bot.message_handler(commands=["stop"])
def stop_scan_cmd(message):
    scan_event.clear()
    bot.reply_to(message, "🛑 <b>Enjin Ditangguh.</b> Guna /scan untuk aktif semula.", parse_mode="HTML")


@bot.message_handler(commands=["ca"])
def manual_ca_check(message):
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "⚠️ Guna: <code>/ca [coin_id atau contract_address]</code>", parse_mode="HTML")
        return

    query = args[1].strip().lower()
    bot.reply_to(message, f"🔍 <i>Menganalisis <b>{html.escape(query)}</b>...</i>", parse_mode="HTML")

    headers       = {"x-cg-demo-api-key": CG_API_KEY} if CG_API_KEY else {}
    passed_address = query if (query.startswith("0x") or len(query) > 30) else None

    try:
        search = requests.get(
            f"{BASE_URL}/search?query={query}", headers=headers, timeout=15
        ).json()

        if not search.get("coins"):
            bot.reply_to(message, "❌ <b>Aset tidak dijumpai dalam CoinGecko.</b>", parse_mode="HTML")
            return

        coin_id = search["coins"][0]["id"]

        mkt = requests.get(
            f"{BASE_URL}/coins/markets?vs_currency=usd&ids={coin_id}",
            headers=headers, timeout=15,
        ).json()

        if not mkt:
            bot.reply_to(message, "❌ <b>Data pasaran tidak tersedia.</b>", parse_mode="HTML")
            return

        m             = mkt[0]
        coin_name     = m["name"]
        symbol        = m["symbol"].upper()
        rank          = m.get("market_cap_rank", "N/A")
        ath_change    = m.get("ath_change_percentage", 0.0) or 0.0
        current_price = m.get("current_price", 0.0) or 0.0
        trend_24h     = m.get("price_change_percentage_24h", 0.0) or 0.0
        vol_24h       = m.get("total_volume", 0.0) or 0.0

        hist = requests.get(
            f"{BASE_URL}/coins/{coin_id}/market_chart",
            params={"vs_currency": "usd", "days": "30", "interval": "daily"},
            headers=headers, timeout=20,
        ).json()

        prices  = [p[1] for p in hist.get("prices", [])]
        volumes = [v[1] for v in hist.get("total_volumes", [])]

        if len(prices) < 14:
            bot.reply_to(message, "⚠️ <b>Data sejarah tidak mencukupi.</b>", parse_mode="HTML")
            return

        rsi_14    = calculate_rsi(prices)
        ema7      = calculate_ema(prices, 7)
        ema21     = calculate_ema(prices, 21)
        atr       = calculate_atr(prices)
        fibo      = calculate_fibonacci_levels(prices)
        trend_7d  = ((prices[-1] - prices[-8]) / prices[-8] * 100) if len(prices) >= 8 else 0.0
        avg_vol7  = np.mean(volumes[-8:-1]) if len(volumes) >= 8 else vol_24h
        vol_mult  = vol_24h / avg_vol7 if avg_vol7 > 0 else 1.0

        dispatch_signal(
            TELEGRAM_CHAT_ID, coin_name, symbol, rank, ath_change,
            vol_mult, rsi_14, current_price, fibo, coin_id,
            trend_24h, vol_24h, trend_7d, atr, ema7, ema21,
            passed_ca=passed_address,
        )

        bot.reply_to(message, "✅ <b>Analisis dihantar ke channel.</b>", parse_mode="HTML")

    except Exception as e:
        admin_log("Ralat /ca command", e)
        logger.error(f"Ralat /ca: {e}")
        bot.reply_to(message, "❌ <b>Ralat teknikal semasa memproses data.</b>", parse_mode="HTML")


# ==========================================
# 11. SISTEM KAWALAN UTAMA
# ==========================================

def graceful_shutdown(*args):
    logger.info("[SYSTEM] Shutdown...")
    if TELEGRAM_TOKEN and ADMIN_CHAT_ID:
        try: bot.send_message(ADMIN_CHAT_ID, "🔴 <b>[OFFLINE] NOVA7 DISCONNECTED.</b>", parse_mode="HTML")
        except Exception: pass
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT,  graceful_shutdown)

    if TELEGRAM_TOKEN and ADMIN_CHAT_ID:
        try:
            bot.send_message(
                ADMIN_CHAT_ID,
                "🟢 <b>NOVA7 v2.0 ONLINE</b>\n"
                "✅ RSI Fixed (Wilder)\n"
                "✅ ATR-based SL/TP\n"
                "✅ EMA7/EMA21 Filter\n"
                "✅ Trade Tracker Fixed\n"
                "✅ Thread-Safe Scan Control\n"
                "✅ Signal Score System",
                parse_mode="HTML",
            )
        except Exception: pass

    threading.Thread(target=run_trade_tracker_loop, daemon=True).start()
    threading.Thread(target=run_scanner_loop,       daemon=True).start()
    threading.Thread(target=bot.infinity_polling,   daemon=True).start()

    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
