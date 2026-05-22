import os
import time
import json
import sqlite3
import asyncio
import threading
import logging
import sys
import signal
import io
import requests
import aiohttp
import websockets
import telebot
import pandas as pd
import mplfinance as mpf
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from flask import Flask, request
from datetime import datetime, timezone

# ==========================================
# KONFIGURASI & LOGGING
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("Nova7")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML") if TELEGRAM_TOKEN else None
is_scanning = True

KILL_LIST = {
    'USDT', 'USDC', 'DAI', 'BUSD', 'TUSD', 'USDD', 'FDUSD', 'USDP', 'GUSD',
    'FRAX', 'LUSD', 'SUSD', 'EUR', 'TRY', 'BRL', 'AUD', 'GBP', 'USDS', 'PYUSD',
    'WBTC', 'WETH', 'WBNB', 'STETH', 'RETH', 'WEETH', 'CBETH', 'WSTETH', 'FRXETH'
}
HEAVYWEIGHTS = {
    'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT', 'DOGEUSDT',
    'ADAUSDT', 'TRXUSDT', 'AVAXUSDT', 'LINKUSDT', 'DOTUSDT', 'TONUSDT',
    'MATICUSDT', 'SHIBUSDT', 'ICPUSDT', 'NEARUSDT', 'LTCUSDT', 'UNIUSDT',
    'APTUSDT', 'XLMUSDT'
}

# ==========================================
# DATABASE SQLITE + TUNING PARAMS
# ==========================================
DB_NAME = "nova7_data.db"
db_lock = threading.Lock()

DEFAULT_TUNING = {
    'mode': 'standard',
    # Breakout params
    'bo_rvol': 1.8,
    'bo_rsi_min': 50,
    'bo_rsi_max': 75,
    'bo_daily_filter': 1,  # 1=on, 0=off
    # Accumulation params
    'acc_bb_width': 6.0,
    'acc_rvol': 2.0,
    'acc_rsi_max': 45,
    # Radar params
    'radar_momentum': 2.5,
    'radar_min_vol': 15_000_000,
    # Cooldown
    'cd_breakout': 24,
    'cd_accumulation': 48
}

def init_db():
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS active_trades
                        (msg_id INTEGER PRIMARY KEY, symbol TEXT, entry REAL,
                         sl REAL, tp1 REAL, tp2 REAL, tp3 REAL, engine TEXT,
                         status TEXT, timestamp REAL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS cooldowns
                        (symbol TEXT PRIMARY KEY, last_signal REAL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS user_profiles
                        (user_id INTEGER PRIMARY KEY, capital REAL, risk_pct REAL, updated REAL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS tuning_params
                        (key TEXT PRIMARY KEY, value REAL)''')
        # Init default tuning jika belum ada
        for k, v in DEFAULT_TUNING.items():
            conn.execute("INSERT OR IGNORE INTO tuning_params VALUES (?, ?)", (k, float(v) if not isinstance(v, str) else 0))
    logger.info("✅ [DB] Nova7 SQLite initialized.")

def get_tuning():
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        rows = conn.execute("SELECT key, value FROM tuning_params").fetchall()
        t = {r[0]: r[1] for r in rows}
        t['mode'] = t.get('mode', 0)
        return t

def set_tuning(params):
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        for k, v in params.items():
            val = float(v) if not isinstance(v, str) else 0
            conn.execute("INSERT OR REPLACE INTO tuning_params VALUES (?, ?)", (k, val))

def save_trade(msg_id, symbol, entry, sl, tp1, tp2, tp3, engine):
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.execute('''INSERT OR REPLACE INTO active_trades
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'TRACKING', ?)''',
                     (msg_id, symbol, entry, sl, tp1, tp2, tp3, engine, time.time()))

def get_active_trades():
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM active_trades WHERE status NOT IN ('COMPLETED', 'STOP_LOSS')").fetchall()

def update_trade_status(msg_id, status):
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE active_trades SET status=? WHERE msg_id=?", (status, msg_id))

def save_cooldown(symbol, hours=24):
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR REPLACE INTO cooldowns VALUES (?, ?)", (symbol, time.time() + (hours * 3600)))

def check_cooldown(symbol):
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        row = conn.execute("SELECT last_signal FROM cooldowns WHERE symbol=?", (symbol,)).fetchone()
        if row and time.time() < row[0]: return True
    return False

def set_user_capital(user_id, capital, risk_pct=2.0):
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR REPLACE INTO user_profiles VALUES (?, ?, ?, ?)",
                     (user_id, capital, risk_pct, time.time()))

def get_user_capital(user_id):
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        row = conn.execute("SELECT capital, risk_pct FROM user_profiles WHERE user_id=?", (user_id,)).fetchone()
        if row: return row[0], row[1]
    return 1000.0, 2.0

# ==========================================
# MATEMATIK O(1)
# ==========================================
class IncrementalIndicators:
    def __init__(self):
        self.closes, self.highs, self.lows, self.volumes = [], [], [], []
        self.ema21 = self.ema50 = None
        self.rsi = 50.0
        self.avg_gain = self.avg_loss = 0.0
        self.prev_close = None
        self.k21, self.k50 = 2.0 / 22, 2.0 / 51

    def initialize(self, closes, highs, lows, volumes):
        if len(closes) < 51: return False
        self.closes, self.highs, self.lows, self.volumes = closes[-100:], highs[-100:], lows[-100:], volumes[-100:]
        self.ema21, self.ema50 = sum(closes[:21]) / 21, sum(closes[:50]) / 50
        for p in closes[50:]:
            self.ema21 = p * self.k21 + self.ema21 * (1 - self.k21)
            self.ema50 = p * self.k50 + self.ema50 * (1 - self.k50)
        deltas = [closes[i] - closes[i-1] for i in range(1, 15)]
        self.avg_gain = sum(d for d in deltas if d > 0) / 14
        self.avg_loss = sum(-d for d in deltas if d < 0) / 14
        for i in range(14, len(closes)):
            d = closes[i] - closes[i-1]
            self.avg_gain = (self.avg_gain * 13 + (d if d > 0 else 0)) / 14
            self.avg_loss = (self.avg_loss * 13 + (-d if d < 0 else 0)) / 14
        self._update_rsi()
        self.prev_close = closes[-1]
        return True

    def _update_rsi(self):
        self.rsi = 100.0 if self.avg_loss == 0 else 100 - (100 / (1 + self.avg_gain / self.avg_loss))

    def get_rvol(self):
        if len(self.volumes) < 21: return 1.0
        avg = sum(self.volumes[-21:-1]) / 20
        return self.volumes[-1] / avg if avg > 0 else 1.0

    def get_bb_width(self):
        if len(self.closes) < 20: return 10.0
        recent = self.closes[-20:]
        sma = sum(recent) / 20
        std = (sum((p - sma) ** 2 for p in recent) / 20) ** 0.5
        return (std / sma) * 100 if sma > 0 else 10.0

    def get_recent_high(self):
        return max(self.highs[-21:-1]) if len(self.highs) >= 21 else 0

# ==========================================
# ENGINES (DYNAMIC TUNING)
# ==========================================
class BreakoutHunter:
    def check(self, ind, t):
        if len(ind.closes) < 51:
            return None, {"Data Sejarah": "Kurang 51 candle (Gagal)"}
        close, rvol, recent_high = ind.closes[-1], ind.get_rvol(), ind.get_recent_high()
        rsi_min = t.get('bo_rsi_min', 50)
        rsi_max = t.get('bo_rsi_max', 75)
        rvol_min = t.get('bo_rvol', 1.8)
        conditions = {
            f"Pecah High 20-C ({recent_high:.6f})": close > recent_high,
            f"Atas EMA21 ({ind.ema21:.6f})": close > ind.ema21,
            "Uptrend (EMA21>EMA50)": ind.ema21 > ind.ema50,
            f"RVOL >= {rvol_min}x [{rvol:.2f}x]": rvol >= rvol_min,
            f"RSI {rsi_min}-{rsi_max} [{ind.rsi:.1f}]": rsi_min < ind.rsi < rsi_max
        }
        if all(conditions.values()):
            sig = {'type': 'BREAKOUT', 'rvol': rvol, 'break_level': recent_high, 'low': min(ind.lows[-5:])}
            return sig, conditions
        return None, conditions

class AccumulationDetective:
    def check(self, ind, t):
        if len(ind.closes) < 51: return None, {}
        close, bb, rvol = ind.closes[-1], ind.get_bb_width(), ind.get_rvol()
        bb_max = t.get('acc_bb_width', 6.0)
        rvol_min = t.get('acc_rvol', 2.0)
        rsi_max = t.get('acc_rsi_max', 45)
        conditions = {
            f"BB Width < {bb_max}% [{bb:.2f}%]": bb < bb_max,
            f"RVOL >= {rvol_min}x [{rvol:.2f}x]": rvol >= rvol_min,
            "Bawah EMA50 (Accum Zone)": close < ind.ema50,
            f"RSI < {rsi_max} [{ind.rsi:.1f}]": ind.rsi < rsi_max
        }
        if all(conditions.values()):
            sig = {'type': 'ACCUMULATION', 'rvol': rvol, 'bb': bb, 'low': min(ind.lows[-5:])}
            return sig, conditions
        return None, conditions

# ==========================================
# AUTO-CHART (VISUAL PROOF)
# ==========================================
def generate_chart_image(symbol, closes, highs, lows, volumes, ema21, ema50, sig, sl, tp1, tp2, tp3):
    try:
        n = min(60, len(closes))
        df = pd.DataFrame({
            'Open': [closes[i-1] if i > 0 else closes[i] for i in range(len(closes)-n, len(closes))],
            'High': highs[-n:],
            'Low': lows[-n:],
            'Close': closes[-n:],
            'Volume': volumes[-n:]
        }, index=pd.date_range(end=datetime.now(), periods=n, freq='H'))

        addplots = []
        if ema21: addplots.append(mpf.make_addplot([ema21]*n, color='#00BFFF', width=1.2))
        if ema50: addplots.append(mpf.make_addplot([ema50]*n, color='#FFA500', width=1.2))

        hlines = dict(hlines=[sl, tp1, tp2, tp3],
                      colors=['red', 'lime', 'lime', 'gold'],
                      linestyle=['-', '--', '--', '--'],
                      linewidths=[1.2, 1, 1, 1])

        mc = mpf.make_marketcolors(up='#26A69A', down='#EF5350', edge='inherit', wick='inherit', volume='in')
        style = mpf.make_mpf_style(marketcolors=mc, gridstyle='-', gridcolor='#1E1E1E',
                                    facecolor='#0E0E0E', edgecolor='#0E0E0E', figcolor='#0E0E0E',
                                    rc={'axes.labelcolor': 'white', 'xtick.color': 'white', 'ytick.color': 'white'})

        buf = io.BytesIO()
        fig, axes = mpf.plot(df, type='candle', style=style, addplot=addplots, hlines=hlines,
                              volume=True, figsize=(10, 6), title=f"\n{symbol} | Nova7 Signal", returnfig=True)
        fig.savefig(buf, format='png', dpi=100, bbox_inches='tight', facecolor='#0E0E0E')
        plt.close(fig)
        buf.seek(0)
        return buf
    except Exception as e:
        logger.error(f"Chart error: {e}")
        return None

# ==========================================
# DAILY CONFLUENCE (TRAP KILLER)
# ==========================================
def check_daily_confluence(symbol, current_price):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1d&limit=60"
        res = requests.get(url, timeout=10).json()
        if not isinstance(res, list) or len(res) < 50: return False, "Data Daily tidak cukup"
        closes = [float(d[4]) for d in res]
        lows = [float(d[3]) for d in res]
        ema50_d = sum(closes[-50:]) / 50
        low_20d = min(lows[-20:])
        if current_price > ema50_d:
            return True, f"Above Daily EMA50 ({ema50_d:.6f})"
        if current_price < low_20d * 1.03:
            return True, f"Bouncing from 20D Support ({low_20d:.6f})"
        return False, f"Below Daily EMA50 ({ema50_d:.6f}) & No Support"
    except Exception as e:
        return False, f"Error: {str(e)[:50]}"

# ==============================================================
# POSITION SIZING (FUND MANAGER) & AI INSIGHT ENGINE (ZERO COST)
# ==============================================================

def generate_ai_insight(symbol):
    """
    Logic Engine: Membaca indikator dan menulis ringkasan 'bahasa manusia'.
    Zero Cost, No External API.
    """
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit=100"
        res = requests.get(url, timeout=10).json()
        
        closes = [float(d[4]) for d in res]
        volumes = [float(d[7]) for d in res]
        if len(closes) < 51: return "❌ Data tidak mencukupi untuk analisis."

        # Kira indikator asas
        close_now = closes[-1]
        ema21 = sum(closes[-21:]) / 21
        ema50 = sum(closes[-50:]) / 50
        
        # Kira RVOL
        avg_vol = sum(volumes[-21:-1]) / 20 if len(volumes) > 21 else 1
        rvol = volumes[-1] / avg_vol if avg_vol > 0 else 1

        # Logik Analisis
        analysis = f"🤖 **NOVA7 AI INSIGHT: {symbol}**\n"
        analysis += "┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        
        # 1. Trend Check
        if close_now > ema21 > ema50:
            analysis += "📈 <b>TREND: BULLISH KUAT</b>\nHarga berada di atas semua EMA. Momentum menaik."
        elif close_now < ema21 < ema50:
            analysis += "📉 <b>TREND: BEARISH KUAT</b>\nHarga jatuh di bawah EMA. Trend sedang menurun."
        else:
            analysis += "⚖️ <b>TREND: CHOP/SIDEWAYS</b>\nHarga bercampur-campur. Hati-hati dengan fakeout."

        # 2. Volume Check
        if rvol > 2.0:
            analysis += f"\n🔥 <b>VOLUME: TINGGI ({rvol:.1f}x)</b>\nAda minat pasaran yang luar biasa. Kemungkinan besar 'Jerung' sedang aktif."
        elif rvol < 0.8:
            analysis += f"\n🌫️ <b>VOLUME: RENDAH ({rvol:.1f}x)</b>\nPasaran sunyi. Risiko manipulasi tinggi."
        else:
            analysis += f"\n💧 <b>VOLUME: BIASA ({rvol:.1f}x)</b>"

        # 3. Summary Recommendation
        if close_now > ema21 and rvol > 1.5:
            analysis += "\n\n💡 <b>KESIMPULAN:</b> Setup positif. Sesuai untuk BUY pada pullback ke EMA21."
        elif close_now < ema50 and rvol > 2.0:
            analysis += "\n\n💡 <b>KESIMPULAN:</b> Setup Reversal/Accumulation. Sesuai untuk entry jika RSI rendah."
        else:
            analysis += "\n\n💡 <b>KESIMPULAN:</b> Setup kurang jelas. Disarankan WAIT & SEE."

        return analysis

    except Exception as e:
        return f"❌ Gagal analisis: {str(e)[:50]}"
def calculate_position_size(capital, risk_pct, entry, sl):
    risk_usd = capital * (risk_pct / 100.0)
    risk_distance = entry - sl
    if risk_distance <= 0: return 0, 0, 0
    position_usd = risk_usd / (risk_distance / entry)
    position_coins = position_usd / entry
    return position_usd, position_coins, risk_usd

# ==========================================
# TELEGRAM UI
# ==========================================
def build_keyboard(symbol):
    """Keyboard dengan Twitter sentiment button."""
    base = symbol[:-4]
    markup = InlineKeyboardMarkup(row_width=2)
    
    # Baris 1: Chart & Trade
    markup.row(
        InlineKeyboardButton("📈 View Chart", url=f"https://www.tradingview.com/chart/?symbol=BINANCE:{symbol}"),
        InlineKeyboardButton("⚡ Trade Now", url=f"https://www.binance.com/en/trade/{symbol}")
    )
    
    # Baris 2: Twitter & DexScreener
    markup.row(
        InlineKeyboardButton("🐦 Twitter Live", url=f"https://x.com/search?q=%24{base}&f=live"),
    )
    return markup
    # ✅ BUTANG BARU DI SINI
    markup.row(InlineKeyboardButton("🤖 AI Insight / Ringkasan", callback_data=f"insight_{symbol}"))
    return markup

def dispatch_signal(symbol, price, sig, ind, engine_type, chart_buf, daily_note, user_cap, user_risk):
    if not bot or not TELEGRAM_CHAT_ID or check_cooldown(symbol): 
        return
    
    sl = sig['low'] * 0.995
    risk = price - sl
    if risk <= 0: 
        return
    
    tp1, tp2, tp3 = price + (risk * 2.0), price + (risk * 3.5), price + (risk * 5.5)
    pos_usd, pos_coins, risk_usd = calculate_position_size(user_cap, user_risk, price, sl)
    
    t = get_tuning()
    mode_name = 'STANDARD'
    if t.get('mode', 0) == 1: 
        mode_name = 'LONGGAR'
    elif t.get('mode', 0) == 2: 
        mode_name = 'KETAT'

    emoji, title = ("🚀", "BREAKOUT RADAR") if engine_type == 'BREAKOUT' else ("🕵️", "ACCUMULATION SNIPER")
    desc = f"Break: <code>${sig.get('break_level', 0):.6f}</code>" if engine_type == 'BREAKOUT' else f"BB Squeeze: {sig.get('bb', 0):.2f}%"

    msg = (
        f"{emoji} <b>{title}: {symbol}</b> <i>[{mode_name}]</i>\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"💵 <b>Price: </b> <code>${price:.6f}</code>\n"
        f"{desc}\n"
        f"🔥 <b>RVOL: </b> {sig['rvol']:.2f}x | <b>RSI: </b> {ind.rsi:.1f}\n"
        f"📊 <b>EMA21: </b> ${ind.ema21:.6f} | <b>EMA50: </b> ${ind.ema50:.6f}\n"
        f"🗓️ <b>Daily TF: </b> <i>{daily_note}</i>\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"🛑 <b>SL: </b> <code>${sl:.6f}</code>\n"
        f"🎯 <b>TP1 (2R): </b> <code>${tp1:.6f}</code>\n"
        f"🎯 <b>TP2 (3.5R): </b> <code>${tp2:.6f}</code>\n"
        f"🎯 <b>TP3 (5.5R): </b> <code>${tp3:.6f}</code>\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"💼 <b>FUND MANAGER (${user_cap:,.0f}):</b>\n"
        f"   • <b>Buy: </b> {pos_coins:.4f} {symbol[:-4]} (<code>${pos_usd:,.2f}</code>)\n"
        f"   • <b>Max Loss: </b> <code>-${risk_usd:,.2f}</code> ({user_risk}%)\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"🐋 <i>Nova7 Institutional Setup</i>"
    )

    try:
        if chart_buf:
            sent = bot.send_photo(TELEGRAM_CHAT_ID, chart_buf, caption=msg, parse_mode="HTML", reply_markup=build_keyboard(symbol))
        else:
            sent = bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="HTML", reply_markup=build_keyboard(symbol), disable_web_page_preview=True)
        
        save_trade(sent.message_id, symbol, price, sl, tp1, tp2, tp3, engine_type)
        save_cooldown(symbol, t.get('cd_breakout', 24) if engine_type == 'BREAKOUT' else t.get('cd_accumulation', 48))
        logger.info(f"✅ [SIGNAL] {symbol} ({engine_type}) dispatched.")
    except Exception as e:
        logger.error(f"Dispatch error: {e}")

# ==========================================
# POST-MORTEM AUTOPSY
# ==========================================
def spot_post_mortem(symbol):
    try:
        url_sym = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit=24"
        url_btc = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=24"
        res_sym = requests.get(url_sym, timeout=10).json()
        res_btc = requests.get(url_btc, timeout=10).json()
        vols = [float(d[7]) for d in res_sym]
        avg_vol = sum(vols[:20]) / 20 if len(vols) >= 20 else 1
        rvol_now = vols[-1] / avg_vol if avg_vol > 0 else 1
        btc_start = float(res_btc[0][1])
        btc_end = float(res_btc[-1][4])
        btc_change = ((btc_end - btc_start) / btc_start) * 100
        closes = [float(d[4]) for d in res_sym]
        ema21 = sum(closes[-21:]) / 21 if len(closes) >= 21 else closes[-1]
        below_ema = closes[-1] < ema21
        reasons = []
        if rvol_now < 1.0: reasons.append(f"🩸 <b>Volume Trap:</b> RVOL {rvol_now:.2f}x")
        if btc_change < -1.5: reasons.append(f"📉 <b>Macro Drag:</b> BTC {btc_change:.2f}%")
        if below_ema: reasons.append("📉 <b>Structure Break:</b> Gagal tahan EMA21")
        if not reasons: reasons.append("🎲 <b>Market Noise:</b> Whipsaw rawak")
        return "\n".join(reasons)
    except Exception:
        return "⚠️ Data tidak mencukupi"
        # ==========================================
# SOCIAL SENTIMENT ANALYZER (TWITTER)
# ==========================================
def check_social_sentiment(symbol, base_name=None):
    """
    Check Twitter sentiment & volume untuk symbol.
    Guna public search - tiada API key required.
    """
    try:
        base = base_name if base_name else symbol[:-4]
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        # Search untuk cashtag ($SYMBOL)
        search_url = f"https://x.com/search?q=%24{base}&f=live"
        res = requests.get(search_url, headers=headers, timeout=10)
        
        if res.status_code != 200:
            return {
                'volume': 0,
                'sentiment': 'NEUTRAL',
                'score': 50,
                'error': 'Fetch failed'
            }
        
        # Kira approximate volume dari response
        # (Ini estimation - Twitter limit public data)
        html_content = res.text.lower()
        
        # Kira mention density
        mention_count = html_content.count(f'${base.lower()}')
        
        # Sentiment keywords
        positive_keywords = ['moon', 'bullish', 'pump', 'buy', 'long', 'green', 'up', 'gain', 'profit', 'rocket']
        negative_keywords = ['bearish', 'dump', 'sell', 'short', 'red', 'down', 'loss', 'crash', 'bleed']
        
        pos_count = sum(1 for kw in positive_keywords if kw in html_content)
        neg_count = sum(1 for kw in negative_keywords if kw in html_content)
        
        # Kira sentiment score
        total = pos_count + neg_count
        if total == 0:
            sentiment_score = 50  # Neutral
            sentiment_label = 'NEUTRAL'
        else:
            sentiment_score = (pos_count / total) * 100
            if sentiment_score >= 60:
                sentiment_label = 'BULLISH'
            elif sentiment_score <= 40:
                sentiment_label = 'BEARISH'
            else:
                sentiment_label = 'NEUTRAL'
        
        # Volume classification
        if mention_count >= 20:
            volume_level = 'HIGH'
        elif mention_count >= 10:
            volume_level = 'MEDIUM'
        else:
            volume_level = 'LOW'
        
        return {
            'volume': mention_count,
            'volume_level': volume_level,
            'sentiment': sentiment_label,
            'score': round(sentiment_score, 1),
            'positive': pos_count,
            'negative': neg_count,
            'error': None
        }
        
    except Exception as e:
        logger.warning(f"Social sentiment error for {symbol}: {e}")
        return {
            'volume': 0,
            'sentiment': 'UNKNOWN',
            'score': 50,
            'error': str(e)[:50]
        }
        
# ==========================================
# LAYER 1 & 2 ORCHESTRATOR (WITH ACTIVITY PULSE)
# ==========================================
latest_prices = {}
radar_history = {}
layer2_queue = set()
stats = {'radar_coins': 0, 'layer2_scans': 0, 'signals_sent': 0, 'rejected': 0}
activity_log = []  # For pulse display

def log_activity(msg):
    """Simpan activity log untuk dipaparkan dalam pulse."""
    activity_log.append(msg)
    if len(activity_log) > 20: activity_log.pop(0)
    logger.info(f"🎯 [SNIPER] {msg}")

async def layer1_radar():
    global is_scanning
    url = "wss://stream.binance.com:9443/ws/!miniTicker@arr"
    last_snapshot = 0
    last_scheduled = 0
    last_pulse = 0
    pulse_stats = {'promoted': 0, 'seen': 0}
    
    while True:
        if not is_scanning:
            await asyncio.sleep(5)
            continue
        try:
            async with websockets.connect(url, ping_interval=20, max_size=10**7) as ws:
                logger.info("✅ [RADAR] Layer 1 Connected. Scanning Mid-Caps...")
                if bot and ADMIN_CHAT_ID:
                    bot.send_message(ADMIN_CHAT_ID, "🟢 <b>HELLO, NOVA7 NOW ACTIVE.</b>\n2-Layer Radar Online.", parse_mode="HTML")
                while True:
                    if not is_scanning: break
                    msg = await ws.recv()
                    now = time.time()
                    
                    # ACTIVITY PULSE setiap 5 minit
                    if now - last_pulse >= 300:
                        logger.info(f"💓 [PULSE] Radar: {pulse_stats['seen']} coins | Promoted: {pulse_stats['promoted']} | Signals: {stats['signals_sent']} | Rejected: {stats['rejected']}")
                        if activity_log:
                            logger.info(f"📋 [RECENT] {' | '.join(activity_log[-5:])}")
                        last_pulse = now
                        pulse_stats = {'promoted': 0, 'seen': 0}
                    
                    if now - last_snapshot < 3.0: continue
                    last_snapshot = now
                    tickers = json.loads(msg)
                    t = get_tuning()
                    
                    for tk in tickers:
                        sym = tk['s']
                        if not sym.endswith('USDT') or sym in HEAVYWEIGHTS: continue
                        base = sym[:-4]
                        if base in KILL_LIST: continue
                        c, q = float(tk['c']), float(tk['q'])
                        latest_prices[sym] = {'c': c, 'q': q}
                        pulse_stats['seen'] += 1
                        
                        if sym not in radar_history: radar_history[sym] = []
                        radar_history[sym].append({'t': now, 'c': c})
                        if len(radar_history[sym]) > 15: radar_history[sym].pop(0)
                        
                        if len(radar_history[sym]) >= 6 and sym not in layer2_queue:
                            past_c = radar_history[sym][-6]['c']
                            if past_c > 0:
                                change = ((c - past_c) / past_c) * 100
                                momentum = t.get('radar_momentum', 2.5)
                                min_vol = t.get('radar_min_vol', 15_000_000)
                                if change >= momentum and q > min_vol:
                                    layer2_queue.add(sym)
                                    pulse_stats['promoted'] += 1
                                    log_activity(f"{sym} ↑{change:.1f}% → Layer 2")
                                    asyncio.create_task(layer2_sniper(sym, 'BREAKOUT'))
                    
                    if now - last_scheduled >= 7200:
                        last_scheduled = now
                        sorted_syms = sorted(latest_prices.keys(), key=lambda s: latest_prices[s]['q'], reverse=True)
                        for s in sorted_syms[:100]:
                            if s not in layer2_queue and not check_cooldown(s):
                                layer2_queue.add(s)
                                asyncio.create_task(layer2_sniper(s, 'ACCUMULATION'))
                    
                    stats['radar_coins'] = len(latest_prices)
        except Exception as e:
            logger.error(f"❌ [RADAR] Disconnected: {e}. Reconnecting...")
            await asyncio.sleep(5)

# ==========================================
# PREMIUM FEATURE 1: AUTO-CHART (VISUAL PROOF)
# ==========================================
def generate_chart_image(symbol, closes, highs, lows, volumes, ema21, ema50, sl, tp1, tp2, tp3):
    try:
        n = min(60, len(closes))
        # FIX BUG PANDAS 2.2: Tukar 'H' kepada '1h'
        df = pd.DataFrame({
            'Open': [closes[i-1] if i > 0 else closes[i] for i in range(len(closes)-n, len(closes))],
            'High': highs[-n:],
            'Low': lows[-n:],
            'Close': closes[-n:],
            'Volume': volumes[-n:]
        }, index=pd.date_range(end=datetime.now(), periods=n, freq='1h'))

        addplots = []
        if ema21: addplots.append(mpf.make_addplot([ema21]*n, color='#00BFFF', width=1.2))
        if ema50: addplots.append(mpf.make_addplot([ema50]*n, color='#FFA500', width=1.2))

        hlines = dict(hlines=[sl, tp1, tp2, tp3],
                      colors=['red', 'lime', 'lime', 'gold'],
                      linestyle=['-', '--', '--', '--'],
                      linewidths=[1.2, 1, 1, 1])

        mc = mpf.make_marketcolors(up='#26A69A', down='#EF5350', edge='inherit', wick='inherit', volume='in')
        style = mpf.make_mpf_style(marketcolors=mc, gridstyle='-', gridcolor='#1E1E1E',
                                    facecolor='#0E0E0E', edgecolor='#0E0E0E', figcolor='#0E0E0E',
                                    rc={'axes.labelcolor': 'white', 'xtick.color': 'white', 'ytick.color': 'white'})

        buf = io.BytesIO()
        fig, axes = mpf.plot(df, type='candle', style=style, addplot=addplots, hlines=hlines,
                              volume=True, figsize=(10, 6), title=f"\n{symbol} | Nova7 Signal", returnfig=True)
        fig.savefig(buf, format='png', dpi=100, bbox_inches='tight', facecolor='#0E0E0E')
        plt.close(fig)
        buf.seek(0)
        return buf
    except Exception as e:
        logger.error(f"Chart generation error: {e}")
        return None

# ==========================================
# LAYER 2 SNIPER (DENGAN AUTO-CHART FORCE)
# ==========================================
async def layer2_sniper(symbol, scan_type, force=False, chat_id=None, user_cap=1000.0, user_risk=2.0):
    try:
        t = get_tuning()
        if not force and check_cooldown(symbol): return
        async with aiohttp.ClientSession() as session:
            url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit=100"
            async with session.get(url, timeout=10) as resp:
                data = await resp.json()
                closes = [float(d[4]) for d in data]
                highs = [float(d[2]) for d in data]
                lows = [float(d[3]) for d in data]
                volumes = [float(d[7]) for d in data]

        ind = IncrementalIndicators()
        if not ind.initialize(closes, highs, lows, volumes): return

        if scan_type == 'BREAKOUT':
            sig, conditions = BreakoutHunter().check(ind, t)
        else:
            sig, conditions = AccumulationDetective().check(ind, t)

        # Kira SL/TP (Atau Dummy jika signal invalid untuk tujuan visualisasi carta)
        if sig:
            sl = sig['low'] * 0.995
            risk = closes[-1] - sl
            tp1 = closes[-1] + (risk * 2.0)
            tp2 = closes[-1] + (risk * 3.5)
            tp3 = closes[-1] + (risk * 5.5)
        else:
            sl = closes[-1] * 0.95
            tp1 = closes[-1] * 1.05
            tp2 = closes[-1] * 1.10
            tp3 = closes[-1] * 1.15

        # WAJIB JANA CARTA JIKA VALID ATAU DI-FORCE
        chart_buf = None
        if sig or force:
            chart_buf = generate_chart_image(symbol, closes, highs, lows, volumes, ind.ema21, ind.ema50, sl, tp1, tp2, tp3)

        if sig:
            daily_filter_on = t.get('bo_daily_filter', 1) == 1
            daily_ok, daily_note = True, "Filter OFF"
            if daily_filter_on:
                daily_ok, daily_note = check_daily_confluence(symbol, closes[-1])
            
            if not daily_ok and not force:
                log_activity(f"{symbol} ❌ Daily Filter: {daily_note}")
                stats['rejected'] += 1
                if chat_id and bot:
                    bot.send_message(chat_id, f"🚫 <b>{symbol}</b> ditolak: {daily_note}", parse_mode="HTML")
                return

            log_activity(f"{symbol} ✅ VALID ({scan_type}) → Dispatching")
def dispatch_signal(symbol, price, sig, ind, engine_type, chart_buf, daily_note, user_cap, user_risk):
        if not bot or not TELEGRAM_CHAT_ID or check_cooldown(symbol): return
    
        sl = sig['low'] * 0.995
        risk = price - sl
        if risk <= 0: return
       
        tp1, tp2, tp3 = price + (risk * 2.0), price + (risk * 3.5), price + (risk * 5.5)
        pos_usd, pos_coins, risk_usd = calculate_position_size(user_cap, user_risk, price, sl)
    
        t = get_tuning()
        mode_name = 'STANDARD'
        if t.get('mode', 0) == 1: mode_name = 'AGGRESSIVE'
        elif t.get('mode', 0) == 2: mode_name = 'CONSERVATIVE'

    # === SOCIAL SENTIMENT CHECK ===
    base = symbol[:-4]
    social_data = check_social_sentiment(symbol, base)
    social_emoji = "🔥" if social_data['sentiment'] == 'BULLISH' else "❄️" if social_data['sentiment'] == 'BEARISH' else "😐"
    social_text = f"{social_emoji} <b>Social:</b> {social_data['sentiment']} ({social_data['score']}/100) | Vol: {social_data.get('volume_level', 'N/A')}"
    # ===============================

    header = f"NOVA7 {engine_type} SIGNAL [ {mode_name} ]"
    
    body_text = (
        f"<blockquote>\n"
        f"<b>🪙 Asset:</b> {symbol}\n"
        f"<b>💵 Price:</b> <code>${price:.6f}</code>\n"
        f"<b>📊 Rank:</b> #{symbol} | <b>Trend:</b> {daily_note}\n"
        f"</blockquote>\n\n"
        
        f"🔹 <b>ENTRY ZONE:</b> <code>${price:.6f}</code>\n"
        f"🔻 <b>STOP LOSS:</b> <code>${sl:.6f}</code>\n\n"
        
        f" <b>TAKE PROFIT TARGETS:</b>\n"
        f"  • TP1 (2R): <code>${tp1:.6f}</code>\n"
        f"  • TP2 (3.5R): <code>${tp2:.6f}</code>\n"
        f"  • TP3 (5.5R): <code>${tp3:.6f}</code>\n\n"
        
        f" <b>INDICATOR DATA:</b>\n"
        f"  • RSI: {ind.rsi:.1f} | RVOL: {sig['rvol']:.2f}x\n"
        f"  • EMA21: ${ind.ema21:.5f} | EMA50: ${ind.ema50:.5f}\n"
        f"  • {social_text}\n\n"  # === SOCIAL SENTIMENT DI SINI ===
        
        f"💼 <b>RISK MGMT:</b> Size: {pos_coins:.2f} coins (Risk ${risk_usd:.2f})"
    )
    
    msg = f"{header}\n{body_text}"

    try:
        if chart_buf:
            sent = bot.send_photo(TELEGRAM_CHAT_ID, chart_buf, caption=msg, parse_mode="HTML", reply_markup=build_keyboard(symbol))
        else:
            sent = bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="HTML", reply_markup=build_keyboard(symbol), disable_web_page_preview=True)
        
        save_trade(sent.message_id, symbol, price, sl, tp1, tp2, tp3, engine_type)
        save_cooldown(symbol, t.get('cd_breakout', 24) if engine_type == 'BREAKOUT' else t.get('cd_accumulation', 48))
        logger.info(f"✅ [SIGNAL] {symbol} ({engine_type}) dispatched. Social: {social_data['sentiment']}")
    except Exception as e:
        logger.error(f"Dispatch error: {e}")

# ==========================================
# TRADE TRACKER
# ==========================================
async def trade_tracker():
    while True:
        await asyncio.sleep(5)
        trades = get_active_trades()
        for t in trades:
            sym = t['symbol']
            if sym not in latest_prices: continue
            price = latest_prices[sym]['c']
            status, reply, new_status = t['status'], None, t['status']
            if price <= t['sl'] and status not in ['STOP_LOSS', 'COMPLETED']:
                autopsy = spot_post_mortem(sym)
                reply = f"🛑 <b>{sym} — STOP LOSS HIT</b>\nProteksi modal pada <code>${price:.6f}</code>\n\n🔬 <b>POST-MORTEM:</b>\n{autopsy}"
                new_status = 'STOP_LOSS'
            elif price >= t['tp3'] and status != 'COMPLETED':
                reply, new_status = f"👑 <b>{sym} — TP3 MOONSHOT!</b>\n<code>${price:.6f}</code>", 'COMPLETED'
            elif price >= t['tp2'] and status not in ['TP2_HIT', 'COMPLETED']:
                reply, new_status = f"🔥 <b>{sym} — TP2 HIT!</b>\nPoketkan 50% di <code>${price:.6f}</code>", 'TP2_HIT'
            elif price >= t['tp1'] and status == 'TRACKING':
                reply, new_status = f"✅ <b>{sym} — TP1 SECURED!</b>\nSL ke BE di <code>${price:.6f}</code>", 'TP1_HIT'
            if reply:
                try:
                    bot.send_message(TELEGRAM_CHAT_ID, reply, reply_to_message_id=t['msg_id'], parse_mode="HTML")
                    update_trade_status(t['msg_id'], new_status)
                except: pass

# ==========================================
# WEEKLY TEAR SHEET
# ==========================================
def generate_tear_sheet():
    seven_days_ago = time.time() - (7 * 86400)
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.row_factory = sqlite3.Row
        trades = conn.execute("SELECT * FROM active_trades WHERE status IN ('STOP_LOSS', 'TP1_HIT', 'TP2_HIT', 'COMPLETED') AND timestamp > ?", (seven_days_ago,)).fetchall()
    if not trades:
        return "📊 <b>WEEKLY TEAR SHEET</b>\n\n<i>Tiada trade closed dalam 7 hari lepas.</i>"
    total = len(trades)
    wins = sum(1 for t in trades if t['status'] != 'STOP_LOSS')
    losses = total - wins
    win_rate = (wins / total) * 100 if total > 0 else 0
    r_map = {'STOP_LOSS': -1.0, 'TP1_HIT': 2.0, 'TP2_HIT': 3.5, 'COMPLETED': 5.5}
    total_r = sum(r_map.get(t['status'], 0) for t in trades)
    gross_profit = sum(r_map.get(t['status'], 0) for t in trades if r_map.get(t['status'], 0) > 0)
    gross_loss = abs(sum(r_map.get(t['status'], 0) for t in trades if r_map.get(t['status'], 0) < 0))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    best = max(trades, key=lambda t: r_map.get(t['status'], 0))
    worst = min(trades, key=lambda t: r_map.get(t['status'], 0))
    pf_str = f"{profit_factor:.2f}" if profit_factor != float('inf') else "∞"
    return (
        f"🏛️ <b>NOVA7 WEEKLY TEAR SHEET</b>\n"
        f"🗓️ <i>Audit 7 Hari (Spot)</i>\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"📊 <b>Total Closed:</b> {total}\n"
        f"🟢 <b>Win Rate:</b> {win_rate:.1f}% ({wins}W / {losses}L)\n"
        f"⚖️ <b>Profit Factor:</b> {pf_str}\n"
        f"📈 <b>Net Expectancy:</b> {total_r:+.1f}R\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"🏆 <b>Best:</b> {best['symbol']} ({r_map.get(best['status'], 0):+.1f}R)\n"
        f"💀 <b>Worst:</b> {worst['symbol']} ({r_map.get(worst['status'], 0):+.1f}R)\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"🔍 <i>Transparency is our edge.</i>"
    )

def tear_sheet_scheduler():
    while True:
        now = datetime.now(timezone.utc)
        if now.weekday() == 6 and now.hour == 20 and now.minute < 5:
            report = generate_tear_sheet()
            if bot and TELEGRAM_CHAT_ID:
                try:
                    bot.send_message(TELEGRAM_CHAT_ID, report, parse_mode="HTML")
                    logger.info("Weekly Tear Sheet sent.")
                except Exception as e:
                    logger.error(f"Tear sheet error: {e}")
            time.sleep(3600)
        time.sleep(60)

# ==========================================
# TELEGRAM COMMANDS (DENGAN /tune)
# ==========================================
@bot.message_handler(commands=['start', 'help'])
def cmd_start(msg):
    global is_scanning
    is_scanning = True
    bot.reply_to(msg,
        "⚡ <b>NOVA7 [PREMIUM + TUNABLE]</b>\n\n"
        "🚀 Layer 1: Real-time Radar\n"
        "🕵️ Layer 2: Sniper + Daily Confluence\n"
        "📊 Auto-Chart Visual Proof\n"
        "💼 Fund Manager Position Sizing\n\n"
        "<b>Commands:</b>\n"
        "/tune show — Lihat parameter\n"
        "/tune standard — Mode balance\n"
        "/tune longgar — Banyak signal\n"
        "/tune ketat — Sikit signal\n"
        "/tune custom key=value\n"
        "/modal 1000 — Set modal\n"
        "/force FETUSDT — Scan manual\n"
        "/report — Tear Sheet\n"
        "/status — System stats", parse_mode="HTML")

@bot.message_handler(commands=['tune'])
def cmd_tune(msg):
    args = msg.text.split()[1:]
    if not args or args[0] == 'show':
        t = get_tuning()
        modes = {0: 'STANDARD', 1: 'LONGGAR', 2: 'KETAT'}
        mode_name = modes.get(int(t.get('mode', 0)), 'UNKNOWN')
        report = (
            f"🎛️ <b>TUNING PARAMETERS</b> [{mode_name}]\n"
            f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
            f"<b>🚀 Breakout Engine:</b>\n"
            f"• RVOL threshold: <code>{t.get('bo_rvol', 1.8):.2f}x</code>\n"
            f"• RSI range: <code>{t.get('bo_rsi_min', 50):.0f}-{t.get('bo_rsi_max', 75):.0f}</code>\n"
            f"• Daily Filter: <code>{'ON' if t.get('bo_daily_filter', 1) == 1 else 'OFF'}</code>\n"
            f"<b>🕵️ Accumulation Engine:</b>\n"
            f"• BB Width max: <code>{t.get('acc_bb_width', 6.0):.1f}%</code>\n"
            f"• RVOL threshold: <code>{t.get('acc_rvol', 2.0):.2f}x</code>\n"
            f"• RSI max: <code>{t.get('acc_rsi_max', 45):.0f}</code>\n"
            f"<b>🐋 Radar:</b>\n"
            f"• Momentum trigger: <code>{t.get('radar_momentum', 2.5):.1f}%</code>\n"
            f"• Min volume: <code>${t.get('radar_min_vol', 15000000)/1e6:.1f}M</code>\n"
            f"<b>⏱️ Cooldowns:</b>\n"
            f"• Breakout: <code>{t.get('cd_breakout', 24):.0f}h</code>\n"
            f"• Accumulation: <code>{t.get('cd_accumulation', 48):.0f}h</code>"
        )
        bot.reply_to(msg, report, parse_mode="HTML")
        return

    if args[0] == 'standard':
        set_tuning({**DEFAULT_TUNING, 'mode': 0})
        bot.reply_to(msg, "✅ <b>Mode STANDARD aktif.</b>\nBalance antara kualiti & kuantiti.", parse_mode="HTML")
    elif args[0] == 'longgar':
        set_tuning({
            'mode': 1,
            'bo_rvol': 1.4, 'bo_rsi_min': 45, 'bo_rsi_max': 80, 'bo_daily_filter': 0,
            'acc_bb_width': 10.0, 'acc_rvol': 1.6, 'acc_rsi_max': 55,
            'radar_momentum': 1.8, 'radar_min_vol': 8_000_000,
            'cd_breakout': 12, 'cd_accumulation': 24
        })
        bot.reply_to(msg, "🟢 <b>Mode LONGGAR aktif.</b>\nLebih banyak signal, sesuai untuk Bull Market.", parse_mode="HTML")
    elif args[0] == 'ketat':
        set_tuning({
            'mode': 2,
            'bo_rvol': 2.2, 'bo_rsi_min': 55, 'bo_rsi_max': 70, 'bo_daily_filter': 1,
            'acc_bb_width': 4.0, 'acc_rvol': 2.5, 'acc_rsi_max': 40,
            'radar_momentum': 3.0, 'radar_min_vol': 20_000_000,
            'cd_breakout': 48, 'cd_accumulation': 72
        })
        bot.reply_to(msg, "🔴 <b>Mode KETAT aktif.</b>\nSangat selective, sesuai untuk Bear/Sideways.", parse_mode="HTML")
    elif args[0] == 'custom' and len(args) >= 2:
        try:
            updates = {}
            for pair in args[1:]:
                if '=' in pair:
                    k, v = pair.split('=')
                    if k in DEFAULT_TUNING:
                        updates[k] = float(v)
            if updates:
                set_tuning(updates)
                bot.reply_to(msg, f"✅ <b>Custom tuning updated:</b>\n" + "\n".join([f"• {k} = {v}" for k, v in updates.items()]), parse_mode="HTML")
            else:
                bot.reply_to(msg, "⚠️ Tiada parameter valid. Contoh: <code>/tune custom bo_rvol=1.5 acc_bb_width=8.0</code>", parse_mode="HTML")
        except Exception as e:
            bot.reply_to(msg, f"❌ Error: {str(e)[:100]}", parse_mode="HTML")
    else:
        bot.reply_to(msg, "⚠️ Guna: <code>/tune show</code>, <code>/tune standard</code>, <code>/tune longgar</code>, <code>/tune ketat</code>, atau <code>/tune custom key=value</code>", parse_mode="HTML")

@bot.message_handler(commands=['modal'])
def cmd_modal(msg):
    args = msg.text.split()
    if len(args) < 2:
        cap, risk = get_user_capital(msg.from_user.id)
        bot.reply_to(msg, f"💼 <b>Modal Semasa:</b> ${cap:,.2f}\n⚠️ <b>Risk:</b> {risk}%\n\nCara set: <code>/modal 1000</code>", parse_mode="HTML")
        return
    try:
        new_cap = float(args[1])
        if new_cap < 10:
            bot.reply_to(msg, "⚠️ Minimum modal: $10", parse_mode="HTML")
            return
        set_user_capital(msg.from_user.id, new_cap)
        bot.reply_to(msg, f"✅ <b>Modal Dikemas Kini:</b> ${new_cap:,.2f}\nRisk default: 2% (${new_cap * 0.02:,.2f} per trade)", parse_mode="HTML")
    except ValueError:
        bot.reply_to(msg, "⚠️ Format: <code>/modal 1000</code>", parse_mode="HTML")

@bot.message_handler(commands=['stop'])
def cmd_stop(msg):
    global is_scanning
    is_scanning = False
    bot.reply_to(msg, "🛑 <b>Enjin Nova7 Dihentikan.</b>", parse_mode="HTML")

@bot.message_handler(commands=['status'])
def cmd_status(msg):
    status = "🟢 AKTIF" if is_scanning else "🔴 STANDBY"
    t = get_tuning()
    modes = {0: 'STANDARD', 1: 'LONGGAR', 2: 'KETAT'}
    text = (
        f"📊 <b>NOVA7 STATUS [{status}]</b>\n"
        f"🎛️ <b>Mode:</b> {modes.get(int(t.get('mode', 0)), 'UNKNOWN')}\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"🐋 Radar: {stats['radar_coins']} coins\n"
        f"🎯 L2 Scans: {stats['layer2_scans']}\n"
        f"📈 Signals: {stats['signals_sent']}\n"
        f"❌ Rejected: {stats['rejected']}\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    )
    bot.reply_to(msg, text, parse_mode="HTML")

@bot.message_handler(commands=['report'])
def cmd_report(msg):
    bot.reply_to(msg, "⏳ <i>Menjana Tear Sheet...</i>", parse_mode="HTML")
    bot.reply_to(msg, generate_tear_sheet(), parse_mode="HTML")

@bot.message_handler(commands=['force'])
def cmd_force(msg):
    args = msg.text.split()
    if len(args) < 2:
        bot.reply_to(msg, "⚠️ Format: <code>/force FETUSDT</code>", parse_mode="HTML")
        return
    sym = args[1].upper()
    if not sym.endswith('USDT'): sym += 'USDT'
    user_cap, user_risk = get_user_capital(msg.from_user.id)
    bot.reply_to(msg, f"🎯 <b>Sniper Deployed: </b> {sym} (Modal: ${user_cap:,.0f})", parse_mode="HTML")
    threading.Thread(target=lambda: asyncio.run(layer2_sniper(sym, 'BREAKOUT', force=True, chat_id=msg.chat.id, user_cap=user_cap, user_risk=user_risk)), daemon=True).start()

# === KOD BARU DI SINI (Pastikan tanda @ ada di Column 0 paling kiri) ===
@bot.callback_query_handler(func=lambda call: call.data.startswith("insight_"))
def handle_ai_insight(call):
    bot.answer_callback_query(call.id, text="Sedang menganalisis pasaran... ⏳")
    
    # Extract symbol from callback_data
    symbol = call.data.replace("insight_", "")
    
    # Call Logic Engine
    text = generate_ai_insight(symbol)
    
    bot.send_message(call.message.chat.id, text, parse_mode="HTML")
# ==========================================
# FLASK & SHUTDOWN
# ==========================================
app = Flask(__name__)
@app.route('/')
def home(): return "Enjin Nova7 Premium Aktif 🐋"
@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        bot.process_new_updates([telebot.types.Update.de_json(request.get_json())])
    return 'ok', 200

def graceful_shutdown(*args):
    if TELEGRAM_TOKEN and ADMIN_CHAT_ID:
        try: bot.send_message(ADMIN_CHAT_ID, "🔴 <b>[OFFLINE] NOVA7 DISCONNECTED.</b>", parse_mode="HTML")
        except: pass
    sys.exit(0)

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))

# ==========================================
# MAIN ORCHESTRATOR
# ==========================================
if __name__ == "__main__":
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)
    init_db()

    RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if bot:
        bot.remove_webhook()
        time.sleep(2)

    if RENDER_URL:
        logger.info("[ENV] Render Cloud detected. Webhook Mode...")
        if bot:
            bot.set_webhook(url=f"{RENDER_URL}/webhook")
            logger.info(f"[WEBHOOK] Aktif: {RENDER_URL}/webhook")
        threading.Thread(target=lambda: asyncio.run(trade_tracker()), daemon=True).start()
        threading.Thread(target=tear_sheet_scheduler, daemon=True).start()
        threading.Thread(target=run_flask, daemon=True).start()
        try: asyncio.run(layer1_radar())
        except KeyboardInterrupt: graceful_shutdown()
    else:
        logger.info("[ENV] Localhost. Polling Mode...")
        threading.Thread(target=lambda: asyncio.run(trade_tracker()), daemon=True).start()
        threading.Thread(target=tear_sheet_scheduler, daemon=True).start()
        threading.Thread(target=lambda: asyncio.run(layer1_radar()), daemon=True).start()
        if bot:
            try: bot.infinity_polling()
            except KeyboardInterrupt: graceful_shutdown()
