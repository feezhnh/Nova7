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
            "Uptrend (EMA21 > EMA50)": ind.ema21 > ind.ema50,
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

# ==========================================
# POSITION SIZING (FUND MANAGER)
# ==========================================
def calculate_position_size(capital, risk_pct, entry, sl):
    risk_usd = capital * (risk_pct / 100.0)
    risk_distance = entry - sl
    if risk_distance <= 0: return 0, 0, 0
    position_usd = risk_usd / (risk_distance / entry)
    position_coins = position_usd / entry
    return position_usd, position_coins, risk_usd

# ==========================================
# TELEGRAM UI (DENGAN AMARAN DINAMIK - TAMBAHAN SAHAJA)
# ==========================================
def build_keyboard(symbol):
    base = symbol[:-4]
    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/search?q={base}"),
        InlineKeyboardButton("📈 TradingView", url=f"https://www.tradingview.com/chart/?symbol=BINANCE:{symbol}")
    )
    markup.row(InlineKeyboardButton("🟨 Trade on Binance", url=f"https://www.binance.com/en/trade/{symbol}"))
    markup.row(InlineKeyboardButton("🐦 Twitter Search", url=f"https://