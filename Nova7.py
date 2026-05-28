from datetime import datetime, timezone, timedelta
from flask import Flask, request
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import matplotlib.pyplot as plt
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

# ==========================================
# KONFIGURASI & LOGGING
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("Nova7")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
bot = telebot.TeleBot(
    TELEGRAM_TOKEN,
    parse_mode="HTML") if TELEGRAM_TOKEN else None
is_scanning = True
KILL_LIST = {
    # Stablecoins (USD-pegged)
    'USDT', 'USDC', 'DAI', 'BUSD', 'TUSD', 'USDD', 'FDUSD', 'USDP', 'GUSD',
    'FRAX', 'LUSD', 'SUSD', 'USDS', 'PYUSD', 'USDE', 'USDX', 'AEUR',
    'USD1', 'RLUSD',  # V8.1: World Liberty USD, Ripple USD
    # Fiat-pegged
    'EUR', 'TRY', 'BRL', 'AUD', 'GBP', 'JPY',
    # Wrapped/LST (mirror underlying — tiada price discovery sendiri)
    'WBTC', 'WETH', 'WBNB', 'STETH', 'RETH', 'WEETH', 'CBETH', 'WSTETH', 'FRXETH',
    'BNSOL', 'WBETH',  # V8.1: Binance Staked SOL, Wrapped Beacon ETH
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

# v8: Schema version untuk migrasi automatik (klien live tidak putus)
SCHEMA_VERSION = 3  # V8.2: Rebalance defaults (8%+2.0x was contradictory)

DEFAULT_TUNING = {
    'mode': 'standard',
    # Breakout params
    'bo_rvol': 1.8,
    'bo_rsi_min': 50,
    'bo_rsi_max': 75,
    'bo_daily_filter': 1,
    # Accumulation — V8.2 REBALANCE:
    # bb_width 15%  = squeeze sebenar pada hourly (range normal 8-25%, <15% = ketat)
    # rvol 1.4x     = cukup confirm buying interest tanpa contradicting low-vol squeeze
    # rsi_max 48    = sikit lebih lebar untuk catch early reversal
    'acc_bb_width': 15.0,
    'acc_rvol': 1.4,
    'acc_rsi_max': 48,
    'acc_rsi_min': 25,
    # Higher-low: 0=soft hint (tunjuk tapi tidak block), 1=hard required
    'acc_require_higher_low': 0,
    # Radar — V8.2: lebih sensitive
    'radar_momentum': 2.0,
    'radar_min_vol': 12_000_000,
    # Cooldown
    'cd_breakout': 24,
    'cd_accumulation': 48,
    # Macro filter — V8.2: threshold lebih tolerable untuk minor dip
    'macro_btc_filter': 1,
    'macro_btc_24h_min': -2.0,
    # Confirmation queue
    'confirm_required': 1,
    'pending_expiry_h': 2,
    # SL
    'sl_atr_mult': 1.5,
    'sl_max_pct': 0.08,
    'fail_cooldown_h': 2,
    # Concurrency
    'layer2_concurrency': 5,
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
        # V8 NEW: pending_signals table untuk confirmation queue
        conn.execute('''CREATE TABLE IF NOT EXISTS pending_signals
            (symbol TEXT PRIMARY KEY, engine TEXT, detect_time REAL,
             detect_price REAL, detect_low REAL, detect_bb REAL,
             detect_rvol REAL, detect_rsi REAL, detect_ema21 REAL,
             detect_ema50 REAL, detect_atr REAL, daily_note TEXT,
             user_cap REAL, user_risk REAL, expiry REAL)''')
        # V8: Migrasi columns untuk klien sedia ada (active_trades)
        cur = conn.execute("PRAGMA table_info(active_trades)").fetchall()
        existing_cols = {row[1] for row in cur}
        for col_def in [
            ('exit_price', 'REAL DEFAULT 0'),
            ('exit_time', 'REAL DEFAULT 0'),
            ('macro_btc_pct', 'REAL DEFAULT 0'),
        ]:
            if col_def[0] not in existing_cols:
                try:
                    conn.execute(f"ALTER TABLE active_trades ADD COLUMN {col_def[0]} {col_def[1]}")
                    logger.info(f"[DB MIGRATE] Added column {col_def[0]} to active_trades")
                except Exception as e:
                    logger.warning(f"[DB MIGRATE] {col_def[0]} skipped: {e}")
        # V8: Schema version check — auto-upgrade tuning defaults sekali sahaja
        sv = conn.execute("SELECT value FROM tuning_params WHERE key='_schema_version'").fetchone()
        current_version = int(sv[0]) if sv else 0
        if current_version < SCHEMA_VERSION:
            logger.warning(f"[DB MIGRATE] Schema v{current_version} → v{SCHEMA_VERSION}. Force-upgrading tuning defaults (one-time).")
            for k, v in DEFAULT_TUNING.items():
                conn.execute(
                    "INSERT OR REPLACE INTO tuning_params VALUES (?, ?)",
                    (k, float(v) if not isinstance(v, str) else 0))
            conn.execute(
                "INSERT OR REPLACE INTO tuning_params VALUES ('_schema_version', ?)",
                (float(SCHEMA_VERSION),))
        else:
            # Init default tuning jika belum ada (fresh install)
            for k, v in DEFAULT_TUNING.items():
                conn.execute(
                    "INSERT OR IGNORE INTO tuning_params VALUES (?, ?)",
                    (k, float(v) if not isinstance(v, str) else 0))
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
            conn.execute(
                "INSERT OR REPLACE INTO tuning_params VALUES (?, ?)", (k, val))

def save_trade(msg_id, symbol, entry, sl, tp1, tp2, tp3, engine, macro_btc_pct=0.0):
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.execute('''INSERT OR REPLACE INTO active_trades
            (msg_id, symbol, entry, sl, tp1, tp2, tp3, engine, status, timestamp, macro_btc_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'TRACKING', ?, ?)''',
            (msg_id, symbol, entry, sl, tp1, tp2, tp3, engine, time.time(), macro_btc_pct))

def get_active_trades():
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM active_trades WHERE status NOT IN ('COMPLETED', 'STOP_LOSS')").fetchall()

def update_trade_status(msg_id, status, exit_price=None):
    """V8: Boleh update exit_price untuk journal."""
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        if exit_price is not None:
            conn.execute(
                "UPDATE active_trades SET status=?, exit_price=?, exit_time=? WHERE msg_id=?",
                (status, exit_price, time.time(), msg_id))
        else:
            conn.execute(
                "UPDATE active_trades SET status=? WHERE msg_id=?", (status, msg_id))

def update_trade_sl(msg_id, new_sl):
    """V8 NEW: Update SL field bila TP1 hit (move to BE)."""
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE active_trades SET sl=? WHERE msg_id=?", (new_sl, msg_id))

def save_cooldown(symbol, hours=24):
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR REPLACE INTO cooldowns VALUES (?, ?)",
            (symbol, time.time() + (hours * 3600)))

def check_cooldown(symbol):
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        row = conn.execute(
            "SELECT last_signal FROM cooldowns WHERE symbol=?", (symbol,)).fetchone()
        if row and time.time() < row[0]: 
            return True
    return False

def set_user_capital(user_id, capital, risk_pct=2.0):
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR REPLACE INTO user_profiles VALUES (?, ?, ?, ?)",
            (user_id, capital, risk_pct, time.time()))

def get_user_capital(user_id):
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        row = conn.execute(
            "SELECT capital, risk_pct FROM user_profiles WHERE user_id=?",
            (user_id,)).fetchone()
        if row: 
            return row[0], row[1]
    return 1000.0, 2.0

# ==========================================
# V8 NEW: PENDING SIGNALS HELPERS (Confirmation Queue)
# ==========================================
def save_pending_signal(p):
    """Save pending signal untuk confirmation pada candle seterusnya."""
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.execute('''INSERT OR REPLACE INTO pending_signals
            (symbol, engine, detect_time, detect_price, detect_low, detect_bb,
             detect_rvol, detect_rsi, detect_ema21, detect_ema50, detect_atr,
             daily_note, user_cap, user_risk, expiry)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (p['symbol'], p['engine'], p['detect_time'], p['detect_price'],
             p['detect_low'], p['detect_bb'], p['detect_rvol'], p['detect_rsi'],
             p['detect_ema21'], p['detect_ema50'], p['detect_atr'],
             p['daily_note'], p['user_cap'], p['user_risk'], p['expiry']))

def get_pending_signals():
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM pending_signals").fetchall()

def drop_pending(symbol):
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.execute("DELETE FROM pending_signals WHERE symbol=?", (symbol,))

# ==========================================
# MATEMATIK O(1) — V8: Tambah ATR + opens tracking
# ==========================================
class IncrementalIndicators:
    def __init__(self):
        self.closes, self.highs, self.lows, self.volumes = [], [], [], []
        self.opens = []  # V8 NEW: track opens untuk candle direction & chart real OHLC
        self.ema21 = self.ema50 = None
        self.rsi = 50.0
        self.avg_gain = self.avg_loss = 0.0
        self.prev_close = None
        self.atr = 0.0  # V8 NEW: ATR(14)
        self.k21, self.k50 = 2.0 / 22, 2.0 / 51

    def initialize(self, opens, closes, highs, lows, volumes):
        """V8: Sekarang ambil opens juga untuk candle direction & ATR true range."""
        if len(closes) < 51: 
            return False
        self.opens = opens[-100:]
        self.closes, self.highs, self.lows, self.volumes = closes[-100:], highs[-100:], lows[-100:], volumes[-100:]
        # EMA initialization — each EMA must iterate from its own seed index
        self.ema21 = sum(closes[:21]) / 21
        for p in closes[21:]:
            self.ema21 = p * self.k21 + self.ema21 * (1 - self.k21)
        self.ema50 = sum(closes[:50]) / 50
        for p in closes[50:]:
            self.ema50 = p * self.k50 + self.ema50 * (1 - self.k50)
        deltas = [closes[i] - closes[i - 1] for i in range(1, 15)]
        self.avg_gain = sum(d for d in deltas if d > 0) / 14
        self.avg_loss = sum(-d for d in deltas if d < 0) / 14
        for i in range(14, len(closes)):
            d = closes[i] - closes[i - 1]
            self.avg_gain = (self.avg_gain * 13 + (d if d > 0 else 0)) / 14
            self.avg_loss = (self.avg_loss * 13 + (-d if d < 0 else 0)) / 14
        self._update_rsi()
        self.prev_close = closes[-1]
        # V8 NEW: kira ATR(14) — Wilder's smoothing
        self._compute_atr()
        return True

    def _update_rsi(self):
        self.rsi = 100.0 if self.avg_loss == 0 else 100 - (100 / (1 + self.avg_gain / self.avg_loss))

    def _compute_atr(self, period=14):
        """V8 NEW: ATR(14) menggunakan Wilder smoothing — institutional standard."""
        if len(self.closes) < period + 1:
            self.atr = 0.0
            return
        # True Range untuk setiap candle (kecuali yang pertama)
        trs = []
        for i in range(1, len(self.closes)):
            h, l, pc = self.highs[i], self.lows[i], self.closes[i - 1]
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        # Wilder smoothing: first ATR = sum(first 14 TRs) / 14, kemudian smoothed
        if len(trs) < period:
            self.atr = sum(trs) / len(trs) if trs else 0.0
            return
        atr = sum(trs[:period]) / period
        for tr in trs[period:]:
            atr = ((atr * (period - 1)) + tr) / period
        self.atr = atr

    def get_rvol(self):
        if len(self.volumes) < 21: 
            return 1.0
        avg = sum(self.volumes[-21:-1]) / 20
        return self.volumes[-1] / avg if avg > 0 else 1.0

    def get_bb_width(self):
        if len(self.closes) < 20: 
            return 10.0
        recent = self.closes[-20:]
        sma = sum(recent) / 20
        std = (sum((p - sma) ** 2 for p in recent) / 20) ** 0.5
        # Standard Bollinger Band Width = (Upper - Lower) / Middle * 100
        return (4 * std / sma) * 100 if sma > 0 else 10.0

    def get_recent_high(self):
        return max(self.highs[-21:-1]) if len(self.highs) >= 21 else 0

    # V8 NEW: candle direction helpers
    def is_current_green(self):
        """Current candle ditutup hijau (close > open)."""
        if not self.opens or not self.closes:
            return False
        return self.closes[-1] > self.opens[-1]

    def is_recent_higher_low(self):
        """Current candle's low > previous candle's low (early bounce signal)."""
        if len(self.lows) < 2:
            return False
        return self.lows[-1] > self.lows[-2]

# ==========================================
# ENGINES (DYNAMIC TUNING) — V8: Strict accumulation + ATR return
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
            "Uptrend (EMA21 >EMA50)": ind.ema21 > ind.ema50,
            f"RVOL >= {rvol_min}x [{rvol:.2f}x]": rvol >= rvol_min,
            f"RSI {rsi_min}-{rsi_max} [{ind.rsi:.1f}]": rsi_min < ind.rsi < rsi_max,
        }
        if all(conditions.values()):
            # SL dari 20-candle structure low (robust)
            structure_low = min(ind.lows[-20:])
            sig = {
                'type': 'BREAKOUT', 'rvol': rvol, 'break_level': recent_high,
                'low': structure_low, 'atr': ind.atr,  # V8: return ATR untuk SL calc
            }
            return sig, conditions
        return None, conditions

class AccumulationDetective:
    """V8.2 REBALANCE: Balanced accumulation detection.
    - 4 HARD conditions (wajib semua lulus)
    - 2 SOFT indicators (bonus info, tidak block signal)
    Higher-low: soft by default, boleh jadi hard via acc_require_higher_low=1
    """
    def check(self, ind, t):
        if len(ind.closes) < 51:
            return None, {}
        close, bb, rvol = ind.closes[-1], ind.get_bb_width(), ind.get_rvol()
        bb_max = t.get('acc_bb_width', 15.0)
        rvol_min = t.get('acc_rvol', 1.4)
        rsi_max = t.get('acc_rsi_max', 48)
        rsi_min = t.get('acc_rsi_min', 25)
        require_hl = int(t.get('acc_require_higher_low', 0)) == 1
        is_green = ind.is_current_green()
        higher_low = ind.is_recent_higher_low()

        # 4 HARD CONDITIONS — semua wajib lulus
        hard_conditions = {
            f"BB Width < {bb_max}% [{bb:.2f}%] (Squeeze)": bb < bb_max,
            f"RVOL >= {rvol_min}x [{rvol:.2f}x]": rvol >= rvol_min,
            "Bawah EMA50 (Accum Zone)": close < ind.ema50,
            f"RSI {rsi_min}-{rsi_max} [{ind.rsi:.1f}]": rsi_min < ind.rsi < rsi_max,
            "Candle Hijau (Buying Pressure)": is_green,
        }

        # SOFT / CONFIGURABLE: Higher Low
        # Jika acc_require_higher_low=1, ia menjadi hard condition
        # Jika 0 (default), ia hanya ditunjuk sebagai info dalam /force diagnostic
        if require_hl:
            hard_conditions["Higher Low (Bounce Hint) [HARD]"] = higher_low
        else:
            # Soft — record untuk diagnostic tapi tidak block
            soft_hint = "✓" if higher_low else "–"
            # Tambah sebagai info key (always True dari segi gating)
            hard_conditions[f"Higher Low (Soft Hint {soft_hint})"] = True

        if all(hard_conditions.values()):
            structure_low = min(ind.lows[-20:])
            sig = {
                'type': 'ACCUMULATION', 'rvol': rvol, 'bb': bb,
                'low': structure_low, 'atr': ind.atr,
                'higher_low': higher_low,  # save untuk mesej signal
            }
            return sig, hard_conditions
        return None, hard_conditions

# ==========================================
# V8 NEW: MACRO FILTER (BTC TREND PRE-SIGNAL)
# ==========================================
_btc_macro_cache = {'time': 0, 'data': None}
_btc_macro_lock = threading.Lock()

def get_btc_macro_state(force_refresh=False):
    """Cached BTC macro state. Refresh setiap 5 minit untuk elak hammer API."""
    with _btc_macro_lock:
        now = time.time()
        if not force_refresh and now - _btc_macro_cache['time'] < 300 and _btc_macro_cache['data']:
            return _btc_macro_cache['data']
        try:
            url_24h = "https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT"
            r1 = requests.get(url_24h, timeout=10).json()
            btc_24h_pct = float(r1.get('priceChangePercent', 0))
            btc_price = float(r1.get('lastPrice', 0))
            # Daily EMA21 untuk trend macro
            url_d = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=60"
            r2 = requests.get(url_d, timeout=10).json()
            closes = [float(d[4]) for d in r2]
            if len(closes) < 22:
                btc_above_ema21d = True
                ema21d = closes[-1] if closes else 0
            else:
                k = 2.0 / 22
                ema21d = sum(closes[:21]) / 21
                for p in closes[21:]:
                    ema21d = p * k + ema21d * (1 - k)
                btc_above_ema21d = closes[-1] > ema21d
            data = {
                'btc_price': btc_price,
                'btc_24h_pct': btc_24h_pct,
                'btc_above_ema21d': btc_above_ema21d,
                'btc_ema21d': ema21d,
                'fetched_at': now,
            }
            _btc_macro_cache['time'] = now
            _btc_macro_cache['data'] = data
            return data
        except Exception as e:
            logger.warning(f"BTC macro fetch error: {e}")
            return _btc_macro_cache.get('data')  # last known (fail-open)

def macro_filter_pass(engine_type, t):
    """V8 NEW: Pre-signal macro check. Return (pass, reason).
    Fail-open jika data outage — jangan blok perdagangan kerana API down.
    """
    if int(t.get('macro_btc_filter', 1)) != 1:
        return True, "Macro filter OFF"
    state = get_btc_macro_state()
    if not state:
        return True, "Macro data unavailable (fail-open)"
    min_24h = t.get('macro_btc_24h_min', -1.5)
    if state['btc_24h_pct'] < min_24h:
        return False, f"BTC dump {state['btc_24h_pct']:+.2f}% (limit {min_24h:+.1f}%)"
    # Accumulation lebih strict — perlu daily trend juga
    if engine_type == 'ACCUMULATION' and not state['btc_above_ema21d']:
        return False, f"BTC bawah Daily EMA21 (${state['btc_ema21d']:.0f})"
    return True, f"BTC OK ({state['btc_24h_pct']:+.2f}%)"

# ==========================================
# DAILY CONFLUENCE (TRAP KILLER) — V8 OVERHAUL
# ==========================================
def check_daily_confluence(symbol, current_price):
    """V8: Real bounce confirmation, bukan falling knife detection."""
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1d&limit=60"
        res = requests.get(url, timeout=10).json()
        if not isinstance(res, list) or len(res) < 50: 
            return False, "Data Daily tidak cukup"
        opens = [float(d[1]) for d in res]
        highs = [float(d[2]) for d in res]
        lows = [float(d[3]) for d in res]
        closes = [float(d[4]) for d in res]
        ema50_d = sum(closes[-50:]) / 50
        low_20d = min(lows[-20:])
        # Confluence 1: Above EMA50 dengan trend menaik (5-day momentum)
        recent_trend_up = closes[-1] > closes[-5]
        if current_price > ema50_d and recent_trend_up:
            return True, f"Above Daily EMA50 ({ema50_d:.6f}), trending up"
        # Confluence 2: Real bounce dari 20D support
        # Syarat: dekat support + last daily candle green + higher low than yesterday
        last_close_green = closes[-1] > opens[-1]
        higher_low_d = lows[-1] > lows[-2]
        near_support = current_price < low_20d * 1.05  # dalam 5% dari support
        if near_support and last_close_green and higher_low_d:
            return True, f"Confirmed bounce 20D Support ({low_20d:.6f})"
        # Confluence 3: Above EMA50 tetapi tanpa strong trend (partial OK)
        if current_price > ema50_d:
            return True, f"Above Daily EMA50 ({ema50_d:.6f})"
        return False, f"No confluence (EMA50: {ema50_d:.6f}, 20D Low: {low_20d:.6f})"
    except Exception as e:
        return False, f"Error: {str(e)[:50]}"

# ==========================================
# AI INSIGHT — V8: Real EMA bukan SMA
# ==========================================
def generate_ai_insight(symbol):
    """
    V8: Guna real EMA (Wilder/exponential) supaya konsisten dengan engine.
    Sebelum ini guna SMA yang dilabel sebagai EMA — mengelirukan.
    """
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit=150"
        res = requests.get(url, timeout=10).json()
        closes = [float(d[4]) for d in res]
        volumes = [float(d[7]) for d in res]
        if len(closes) < 51: 
            return "❌ Data tidak mencukupi untuk analisis."

        close_now = closes[-1]

        # V8 FIX: Real EMA21 & EMA50 dengan iterative smoothing
        k21, k50 = 2.0 / 22, 2.0 / 51
        ema21 = sum(closes[:21]) / 21
        for p in closes[21:]:
            ema21 = p * k21 + ema21 * (1 - k21)
        ema50 = sum(closes[:50]) / 50
        for p in closes[50:]:
            ema50 = p * k50 + ema50 * (1 - k50)

        # Kira RVOL
        avg_vol = sum(volumes[-21:-1]) / 20 if len(volumes) > 21 else 1
        rvol = volumes[-1] / avg_vol if avg_vol > 0 else 1

        # Macro context
        macro = get_btc_macro_state()
        macro_line = ""
        if macro:
            macro_line = f"\n🌐 <b>Macro:</b> BTC {macro['btc_24h_pct']:+.2f}% (24h)"

        analysis = f"🤖 <b>NOVA7 AI INSIGHT: {symbol}</b>\n"
        analysis += "┈┈┈┈┈┈┈┈┈┈\n"

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

        analysis += macro_line

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

# ==========================================
# POSITION SIZING — V8: Cap kepada available capital
# ==========================================
def calculate_position_size(capital, risk_pct, entry, sl):
    """V8: Position size capped kepada capital. Tiada lagi 155% modal."""
    risk_usd = capital * (risk_pct / 100.0)
    risk_distance = entry - sl
    if risk_distance <= 0: 
        return 0, 0, 0
    position_usd_raw = risk_usd / (risk_distance / entry)
    # V8 FIX: Cap kepada available capital (spot, no leverage)
    position_usd = min(position_usd_raw, capital)
    position_coins = position_usd / entry
    # Re-kira actual risk selepas cap (kalau capped, risk effective lebih kecil)
    actual_risk_usd = position_coins * risk_distance
    return position_usd, position_coins, actual_risk_usd

# ==========================================
# V8 NEW: ATR-based SL calculation
# ==========================================
def compute_final_sl(entry, structure_low, atr, t):
    """
    Combine ATR-based SL dengan structure SL.
    Pilih yang lebih jauh dari entry (lebih konservatif untuk crypto noise).
    Cap maksimum SL distance untuk elak position size mikroskopik.
    """
    sl_atr_mult = t.get('sl_atr_mult', 1.5)
    sl_max_pct = t.get('sl_max_pct', 0.08)
    sl_atr = entry - (sl_atr_mult * atr) if atr > 0 else entry * 0.98
    sl_structure = structure_low * 0.995  # buffer 0.5% bawah structure
    sl_raw = min(sl_atr, sl_structure)  # ambil yang lebih jauh (lower price)
    sl_floor = entry * (1.0 - sl_max_pct)  # cap: tidak lebih 8% dari entry
    final_sl = max(sl_raw, sl_floor)
    return final_sl

# ==========================================
# TELEGRAM UI
# ==========================================
def build_keyboard(symbol):
    """Keyboard Premium 2x2 Grid dengan AI Insight."""
    base = symbol[:-4]
    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton("📈 TradingView", url=f"https://www.tradingview.com/chart/?symbol=BINANCE:{symbol}"),
        InlineKeyboardButton("⚡ Binance", url=f"https://www.binance.com/en/trade/{symbol}")
    )
    markup.row(
        InlineKeyboardButton("🐦 Twitter Live", url=f"https://x.com/search?q=%24{base}&f=live"),
        InlineKeyboardButton("✨ AI Insight", callback_data=f"ai_summary_{symbol}")
    )
    return markup

# ==========================================
# DISPATCH SIGNAL — V8: Macro filter + ATR SL + capital cap + confirmation queue
# ==========================================
def dispatch_signal(symbol, price, sig, ind, engine_type, chart_buf, daily_note,
                    user_cap, user_risk, from_pending=False):
    """V8: Major overhaul.
    - Macro filter pre-check
    - Confirmation queue untuk ACCUMULATION (kecuali from_pending=True)
    - ATR-based SL
    - Position size capping
    - Record macro snapshot ke DB untuk journal
    """
    if not bot or not TELEGRAM_CHAT_ID or check_cooldown(symbol):
        return

    t = get_tuning()

    # V8: Macro filter pre-signal
    macro_ok, macro_reason = macro_filter_pass(engine_type, t)
    macro_state = get_btc_macro_state()
    macro_btc_pct = macro_state['btc_24h_pct'] if macro_state else 0.0
    if not macro_ok:
        log_activity(f"{symbol} 🌐 Macro BLOCK: {macro_reason}")
        bump_stat('rejected')
        save_cooldown(symbol, int(t.get('fail_cooldown_h', 2)))
        return

    # V8: Confirmation queue untuk ACCUMULATION
    if (engine_type == 'ACCUMULATION'
        and int(t.get('confirm_required', 1)) == 1
        and not from_pending):
        # Move ke pending queue, akan diconfirm pada candle seterusnya
        expiry_h = t.get('pending_expiry_h', 2)
        pending = {
            'symbol': symbol, 'engine': engine_type,
            'detect_time': time.time(), 'detect_price': price,
            'detect_low': sig.get('low', 0), 'detect_bb': sig.get('bb', 0),
            'detect_rvol': sig.get('rvol', 0), 'detect_rsi': ind.rsi,
            'detect_ema21': ind.ema21, 'detect_ema50': ind.ema50,
            'detect_atr': sig.get('atr', 0),
            'daily_note': daily_note, 'user_cap': user_cap, 'user_risk': user_risk,
            'expiry': time.time() + (expiry_h * 3600),
        }
        save_pending_signal(pending)
        log_activity(f"{symbol} 🕒 PENDING (confirmation in ~1h)")
        return

    # V8: SL menggunakan ATR + structure + cap
    structure_low = sig['low']
    atr = sig.get('atr', 0)
    sl = compute_final_sl(price, structure_low, atr, t)
    risk = price - sl
    if risk <= 0:
        logger.warning(f"{symbol}: SL >= entry, abort dispatch")
        return

    tp1, tp2, tp3 = price + (risk * 2.0), price + (risk * 3.5), price + (risk * 5.5)
    pos_usd, pos_coins, risk_usd = calculate_position_size(user_cap, user_risk, price, sl)

    mode_name = 'STANDARD'
    if int(t.get('mode', 0)) == 1:
        mode_name = 'LONGGAR'
    elif int(t.get('mode', 0)) == 2:
        mode_name = 'KETAT'

    emoji, title = ("🚀", "BREAKOUT RADAR") if engine_type == 'BREAKOUT' else ("🕵️", "ACCUMULATION SNIPER")
    desc = (f"Break: <code>${sig.get('break_level', 0):.6f}</code>"
            if engine_type == 'BREAKOUT'
            else f"BB Squeeze: {sig.get('bb', 0):.2f}%")

    # V8: Macro line untuk transparency
    macro_line = ""
    if macro_state:
        arrow = "🟢" if macro_state['btc_24h_pct'] >= 0 else "🔴"
        macro_line = f"\n🌐 <b>Macro:</b> {arrow} BTC {macro_state['btc_24h_pct']:+.2f}% (24h)"

    # V8: Tag confirmation untuk akumulasi yang dah confirmed
    conf_tag = " ✓CONF" if (engine_type == 'ACCUMULATION' and from_pending) else ""

    msg = (
        f"{emoji} <b>{title}: {symbol}</b> <i>[{mode_name}{conf_tag}]</i>\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"💵 <b>Price:</b> <code>${price:.6f}</code>\n"
        f"{desc}\n"
        f"🔥 <b>RVOL:</b> {sig['rvol']:.2f}x | <b>RSI:</b> {ind.rsi:.1f}\n"
        f"📊 <b>EMA21:</b> ${ind.ema21:.6f} | <b>EMA50:</b> ${ind.ema50:.6f}\n"
        f"📐 <b>ATR(14):</b> ${atr:.6f}\n"
        f"🗓️ <b>Daily TF:</b> <i>{daily_note}</i>"
        f"{macro_line}\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"🛑 <b>SL:</b> <code>${sl:.6f}</code> ({((sl-price)/price*100):+.2f}%)\n"
        f"🎯 <b>TP1 (2R):</b> <code>${tp1:.6f}</code>\n"
        f"🎯 <b>TP2 (3.5R):</b> <code>${tp2:.6f}</code>\n"
        f"🎯 <b>TP3 (5.5R):</b> <code>${tp3:.6f}</code>\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"💼 <b>FUND MANAGER (${user_cap:,.0f}):</b>\n"
        f"   • <b>Buy:</b> {pos_coins:.4f} {symbol[:-4]} (<code>${pos_usd:,.2f}</code>)\n"
        f"   • <b>Max Loss:</b> <code>-${risk_usd:,.2f}</code> ({user_risk}%)\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"🐋 <i>Nova7 Institutional Setup</i>"
    )

    try:
        if chart_buf:
            sent = bot.send_photo(TELEGRAM_CHAT_ID, chart_buf, caption=msg,
                                  parse_mode="HTML", reply_markup=build_keyboard(symbol))
        else:
            sent = bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="HTML",
                                    reply_markup=build_keyboard(symbol),
                                    disable_web_page_preview=True)

        save_trade(sent.message_id, symbol, price, sl, tp1, tp2, tp3, engine_type,
                   macro_btc_pct=macro_btc_pct)
        save_cooldown(symbol,
                      t.get('cd_breakout', 24) if engine_type == 'BREAKOUT'
                      else t.get('cd_accumulation', 48))
        logger.info(f"✅ [SIGNAL] {symbol} ({engine_type}{conf_tag}) dispatched.")
        bump_stat('signals_sent')
    except Exception as e:
        logger.error(f"Dispatch error {symbol}: {e}")

# ==========================================
# POST-MORTEM AUTOPSY (kekal, minor fix vols slice)
# ==========================================
def spot_post_mortem(symbol):
    try:
        url_sym = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit=24"
        url_btc = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=24"
        res_sym = requests.get(url_sym, timeout=10).json()
        res_btc = requests.get(url_btc, timeout=10).json()
        vols = [float(d[7]) for d in res_sym]
        # V8: konsisten dengan RVOL formula di tempat lain (-1 sebagai current candle)
        avg_vol = sum(vols[:-1]) / (len(vols) - 1) if len(vols) >= 2 else 1
        rvol_now = vols[-1] / avg_vol if avg_vol > 0 else 1
        btc_start = float(res_btc[0][1])
        btc_end = float(res_btc[-1][4])
        btc_change = ((btc_end - btc_start) / btc_start) * 100
        closes = [float(d[4]) for d in res_sym]
        # V8: Real EMA21 untuk konsistensi (sebelum ini SMA yang dilabel EMA)
        if len(closes) >= 21:
            k = 2.0 / 22
            ema21 = sum(closes[:21]) / 21
            for p in closes[21:]:
                ema21 = p * k + ema21 * (1 - k)
        else:
            ema21 = closes[-1] if closes else 0
        below_ema = closes[-1] < ema21
        reasons = []
        if rvol_now < 1.0: 
            reasons.append(f"🩸 <b>Volume Trap:</b> RVOL {rvol_now:.2f}x")
        if btc_change < -1.5: 
            reasons.append(f"📉 <b>Macro Drag:</b> BTC {btc_change:.2f}%")
        if below_ema: 
            reasons.append("📉 <b>Structure Break:</b> Gagal tahan EMA21")
        if not reasons: 
            reasons.append("🎲 <b>Market Noise:</b> Whipsaw rawak")
        return "\n".join(reasons)
    except Exception:
        return "⚠️ Data tidak mencukupi"

# ==========================================
# SOCIAL SENTIMENT ANALYZER (TWITTER/CryptoPanic) — KEKAL (tidak diusik)
# ==========================================
def check_social_sentiment(symbol, base_name=None):
    """
    Check news sentiment & volume untuk symbol menggunakan CryptoPanic public API.
    Tiada API key required untuk free tier.
    """
    try:
        base = base_name if base_name else symbol[:-4]
        headers = {
            'User-Agent': 'Mozilla/5.0 (Nova7-Bot/1.0)'
        }
        api_url = f"https://cryptopanic.com/api/v1/posts/?currencies={base}&public=true"
        res = requests.get(api_url, headers=headers, timeout=10)

        if res.status_code != 200:
            return {
                'volume': 0,
                'volume_level': 'LOW',
                'sentiment': 'NEUTRAL',
                'score': 50,
                'positive': 0,
                'negative': 0,
                'error': f'API status {res.status_code}'
            }

        data = res.json()
        posts = data.get('results', [])

        if not posts:
            return {
                'volume': 0,
                'volume_level': 'LOW',
                'sentiment': 'NEUTRAL',
                'score': 50,
                'positive': 0,
                'negative': 0,
                'error': None
            }

        pos_count = 0
        neg_count = 0
        for post in posts:
            votes = post.get('votes', {}) or {}
            pos_count += int(votes.get('positive', 0) or 0)
            pos_count += int(votes.get('important', 0) or 0)
            neg_count += int(votes.get('negative', 0) or 0)
            neg_count += int(votes.get('toxic', 0) or 0)
            title = (post.get('title', '') or '').lower()
            positive_keywords = ['surge', 'rally', 'bullish', 'soar', 'breakout', 'gain', 'rocket', 'pump']
            negative_keywords = ['crash', 'dump', 'bearish', 'plunge', 'fall', 'loss', 'hack', 'exploit']
            pos_count += sum(1 for kw in positive_keywords if kw in title)
            neg_count += sum(1 for kw in negative_keywords if kw in title)

        mention_count = len(posts)

        total = pos_count + neg_count
        if total == 0:
            sentiment_score = 50
            sentiment_label = 'NEUTRAL'
        else:
            sentiment_score = (pos_count / total) * 100
            if sentiment_score >= 60:
                sentiment_label = 'BULLISH'
            elif sentiment_score <= 40:
                sentiment_label = 'BEARISH'
            else:
                sentiment_label = 'NEUTRAL'

        if mention_count >= 15:
            volume_level = 'HIGH'
        elif mention_count >= 5:
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
            'volume_level': 'LOW',
            'sentiment': 'UNKNOWN',
            'score': 50,
            'positive': 0,
            'negative': 0,
            'error': str(e)[:50]
        }

# ==========================================
# LAYER 1 & 2 ORCHESTRATOR (WITH ACTIVITY PULSE)
# ==========================================
latest_prices = {}
radar_history = {}
layer2_queue = set()
stats = {'radar_coins': 0, 'layer2_scans': 0, 'signals_sent': 0, 'rejected': 0}
stats_lock = threading.Lock()
queue_lock = threading.Lock()
activity_log = []  # For pulse display

# V8 NEW: Semaphore untuk layer2 concurrency limit (lazy init dalam event loop)
_layer2_sem = None
_pending_sem = None

def bump_stat(key, n=1):
    """Thread-safe increment untuk stats dict."""
    with stats_lock:
        stats[key] = stats.get(key, 0) + n

def set_stat(key, value):
    """Thread-safe set untuk stats dict."""
    with stats_lock:
        stats[key] = value

def get_stats_snapshot():
    """Ambil snapshot stats tanpa race."""
    with stats_lock:
        return dict(stats)

def log_activity(msg):
    """Simpan activity log untuk dipaparkan dalam pulse."""
    activity_log.append(msg)
    if len(activity_log) > 20: 
        activity_log.pop(0)
    logger.info(f"🎯 [SNIPER] {msg}")

async def layer1_radar():
    """V8: Tambah semaphore + fail-cooldown untuk concurrency control."""
    global is_scanning, _layer2_sem
    # Init semaphore dalam event loop ini
    t_init = get_tuning()
    _layer2_sem = asyncio.Semaphore(int(t_init.get('layer2_concurrency', 5)))
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
                    bot.send_message(ADMIN_CHAT_ID, "🟢 <b>HELLO, NOVA7 v8 NOW ACTIVE.</b>\n2-Layer Radar + Macro Filter + Confirmation Queue Online.", parse_mode="HTML")
                while True:
                    if not is_scanning: 
                        break
                    msg = await ws.recv()
                    now = time.time()

                    # ACTIVITY PULSE setiap 5 minit
                    if now - last_pulse >= 300:
                        snap = get_stats_snapshot()
                        logger.info(f"💓 [PULSE] Radar: {pulse_stats['seen']} coins | Promoted: {pulse_stats['promoted']} | Signals: {snap['signals_sent']} | Rejected: {snap['rejected']}")
                        if activity_log:
                            logger.info(f"📋 [RECENT] {' | '.join(activity_log[-5:])}")
                        last_pulse = now
                        pulse_stats = {'promoted': 0, 'seen': 0}

                    if now - last_snapshot < 3.0: 
                        continue
                    last_snapshot = now
                    tickers = json.loads(msg)
                    t = get_tuning()

                    for tk in tickers:
                        sym = tk['s']
                        if not sym.endswith('USDT') or sym in HEAVYWEIGHTS: 
                            continue
                        base = sym[:-4]
                        if base in KILL_LIST: 
                            continue
                        c, q = float(tk['c']), float(tk['q'])
                        latest_prices[sym] = {'c': c, 'q': q}
                        pulse_stats['seen'] += 1

                        if sym not in radar_history: 
                            radar_history[sym] = []
                        radar_history[sym].append({'t': now, 'c': c})
                        if len(radar_history[sym]) > 15: 
                            radar_history[sym].pop(0)

                        promote_breakout = False
                        if len(radar_history[sym]) >= 6:
                            past_c = radar_history[sym][-6]['c']
                            if past_c > 0:
                                change = ((c - past_c) / past_c) * 100
                                momentum = t.get('radar_momentum', 2.5)
                                min_vol = t.get('radar_min_vol', 15_000_000)
                                if change >= momentum and q > min_vol:
                                    with queue_lock:
                                        if sym not in layer2_queue:
                                            layer2_queue.add(sym)
                                            promote_breakout = True
                                    if promote_breakout:
                                        pulse_stats['promoted'] += 1
                                        log_activity(f"{sym} ↑{change:.1f}% → Layer 2")
                                        asyncio.create_task(layer2_sniper(sym, 'BREAKOUT'))

                    if now - last_scheduled >= 7200:
                        last_scheduled = now
                        sorted_syms = sorted(latest_prices.keys(), key=lambda s: latest_prices[s]['q'], reverse=True)
                        for s in sorted_syms[:100]:
                            if check_cooldown(s):
                                continue
                            queued = False
                            with queue_lock:
                                if s not in layer2_queue:
                                    layer2_queue.add(s)
                                    queued = True
                            if queued:
                                asyncio.create_task(layer2_sniper(s, 'ACCUMULATION'))

                    set_stat('radar_coins', len(latest_prices))
        except Exception as e:
            logger.error(f"❌ [RADAR] Disconnected: {e}. Reconnecting...")
            await asyncio.sleep(5)

# ==========================================
# PREMIUM FEATURE 1: AUTO-CHART (VISUAL PROOF) — V8: Real opens
# ==========================================
def generate_chart_image(symbol, opens, closes, highs, lows, volumes, ema21, ema50, sl, tp1, tp2, tp3):
    """V8: Accept real opens, bukan derive dari previous close."""
    try:
        n = min(60, len(closes))
        df = pd.DataFrame({
            'Open': opens[-n:],  # V8 FIX: real opens
            'High': highs[-n:],
            'Low': lows[-n:],
            'Close': closes[-n:],
            'Volume': volumes[-n:]
        }, index=pd.date_range(end=datetime.now(), periods=n, freq='1h'))
        addplots = []
        if ema21: 
            addplots.append(mpf.make_addplot([ema21] * n, color='#00BFFF', width=1.2))
        if ema50: 
            addplots.append(mpf.make_addplot([ema50] * n, color='#FFA500', width=1.2))

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
# LAYER 2 SNIPER — V8: Semaphore + fetch opens + fail cooldown
# ==========================================
async def layer2_sniper(symbol, scan_type, force=False, chat_id=None, user_cap=1000.0, user_risk=2.0):
    """V8: Semaphore throttle + opens fetching + fail cooldown for repeat blockers."""
    sem = _layer2_sem
    try:
        # Semaphore: kalau dah init, throttle. Kalau tak (force/pending), bypass.
        if sem is not None and not force:
            async with sem:
                await _layer2_sniper_impl(symbol, scan_type, force, chat_id, user_cap, user_risk)
        else:
            await _layer2_sniper_impl(symbol, scan_type, force, chat_id, user_cap, user_risk)
    except Exception as e:
        logger.error(f"Sniper wrapper error {symbol}: {e}")
    finally:
        with queue_lock:
            layer2_queue.discard(symbol)

async def _layer2_sniper_impl(symbol, scan_type, force, chat_id, user_cap, user_risk):
    t = get_tuning()
    if not force and check_cooldown(symbol):
        return
    async with aiohttp.ClientSession() as session:
        # V8: limit dinaikkan ke 150 untuk EMA50 stability
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit=150"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
        except Exception as e:
            logger.warning(f"Klines fetch error {symbol}: {e}")
            return

        if not isinstance(data, list) or len(data) < 51:
            return

        opens = [float(d[1]) for d in data]   # V8: ambil opens
        highs = [float(d[2]) for d in data]
        lows = [float(d[3]) for d in data]
        closes = [float(d[4]) for d in data]
        volumes = [float(d[7]) for d in data]

        ind = IncrementalIndicators()
        if not ind.initialize(opens, closes, highs, lows, volumes):
            return

        if scan_type == 'BREAKOUT':
            sig, conditions = BreakoutHunter().check(ind, t)
        else:
            sig, conditions = AccumulationDetective().check(ind, t)

        # Kira SL/TP (atau dummy jika invalid, untuk visualisasi chart)
        if sig:
            sl_preview = compute_final_sl(closes[-1], sig['low'], sig.get('atr', 0), t)
            risk_preview = closes[-1] - sl_preview
            tp1 = closes[-1] + (risk_preview * 2.0)
            tp2 = closes[-1] + (risk_preview * 3.5)
            tp3 = closes[-1] + (risk_preview * 5.5)
            sl = sl_preview
        else:
            sl = closes[-1] * 0.95
            tp1 = closes[-1] * 1.05
            tp2 = closes[-1] * 1.10
            tp3 = closes[-1] * 1.15

        chart_buf = None
        if sig or force:
            chart_buf = generate_chart_image(symbol, opens, closes, highs, lows, volumes,
                                             ind.ema21, ind.ema50, sl, tp1, tp2, tp3)

        if sig:
            daily_filter_on = int(t.get('bo_daily_filter', 1)) == 1
            daily_ok, daily_note = True, "Filter OFF"
            if daily_filter_on:
                daily_ok, daily_note = check_daily_confluence(symbol, closes[-1])

            if not daily_ok and not force:
                log_activity(f"{symbol} ❌ Daily Filter: {daily_note}")
                bump_stat('rejected')
                save_cooldown(symbol, int(t.get('fail_cooldown_h', 2)))  # V8: mini cooldown
                if chat_id and bot:
                    bot.send_message(chat_id, f"🚫 <b>{symbol}</b> ditolak: {daily_note}", parse_mode="HTML")
                return

            log_activity(f"{symbol} ✅ VALID ({scan_type}) → Dispatching")
            dispatch_signal(symbol, closes[-1], sig, ind, scan_type, chart_buf, daily_note,
                            user_cap, user_risk)
        else:
            # Log kenapa gagal (ringkas) + cooldown ringan untuk elak repeat scan
            failed = [k for k, v in conditions.items() if not v]
            if failed:
                main_reason = failed[0].split('[')[0].strip()[:40]
                log_activity(f"{symbol} ❌ {main_reason}")
                bump_stat('rejected')
                if not force:
                    save_cooldown(symbol, int(t.get('fail_cooldown_h', 2)))  # V8: mini cooldown

            if chat_id and bot and conditions:
                report = f"🔍 <b>Diagnostic {symbol}</b>\n❌ Setup TIDAK VALID.\n\n"
                for condition, passed in conditions.items():
                    report += f"{'✅' if passed else '❌'} {condition}\n"
                bot.send_message(chat_id, report, parse_mode="HTML")

        bump_stat('layer2_scans')

# ==========================================
# V8 NEW: PENDING SIGNAL PROCESSOR (Confirmation Worker)
# ==========================================
async def check_confirmation_async(p):
    """V8: Confirmation logic — periksa candle SELEPAS detection.
    Return: (confirmed: bool|None, reason: str). None = data tidak cukup, retry later.
    """
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.binance.com/api/v3/klines?symbol={p['symbol']}&interval=1h&limit=5"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
        if not isinstance(data, list) or len(data) < 3:
            return None, "Data unavailable"

        # data[-1] = candle yang sedang terbentuk (current)
        # data[-2] = candle terakhir DITUTUP (ini candle confirmation kita)
        # data[-3] = candle detection (atau dekat dengan masa detection)
        conf_open = float(data[-2][1])
        conf_high = float(data[-2][2])
        conf_low = float(data[-2][3])
        conf_close = float(data[-2][4])
        conf_volume = float(data[-2][7])
        conf_open_time_ms = int(data[-2][0])
        conf_open_time = conf_open_time_ms / 1000.0

        # Pastikan candle confirmation memang SELEPAS detection (>=1 candle later)
        if conf_open_time < p['detect_time']:
            return None, "Candle belum selesai bentuk"

        # Rule 1: Conf candle ditutup hijau
        if conf_close <= conf_open:
            return False, f"Conf candle merah ({conf_open:.6f}→{conf_close:.6f})"

        # Rule 2: Close tidak jatuh > 1% bawah detection price
        if conf_close < p['detect_price'] * 0.99:
            return False, f"Harga jatuh ke ${conf_close:.6f}"

        # Rule 3: Structure low pegang (low candle conf >= detect_low)
        if conf_low < p['detect_low']:
            return False, f"Structure low pecah (${conf_low:.6f} < ${p['detect_low']:.6f})"

        # Rule 4: Volume sustained (>= 50% average recent)
        vols = [float(d[7]) for d in data[:-2]]
        if vols:
            avg_vol = sum(vols) / len(vols)
            if avg_vol > 0 and conf_volume < avg_vol * 0.5:
                return False, f"Volume drop ({conf_volume/avg_vol:.2f}x avg)"

        return True, f"Confirmed @ ${conf_close:.6f}"
    except Exception as e:
        return None, f"Error: {str(e)[:50]}"

async def dispatch_from_pending(p):
    """V8: Dispatch signal yang dah dapat confirmation. Re-fetch live data, re-validate macro."""
    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.binance.com/api/v3/klines?symbol={p['symbol']}&interval=1h&limit=150"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
        if not isinstance(data, list) or len(data) < 51:
            return
        opens = [float(d[1]) for d in data]
        highs = [float(d[2]) for d in data]
        lows = [float(d[3]) for d in data]
        closes = [float(d[4]) for d in data]
        volumes = [float(d[7]) for d in data]

        ind = IncrementalIndicators()
        if not ind.initialize(opens, closes, highs, lows, volumes):
            return

        # Re-construct sig dict dari detection data + fresh values
        live_low = min(ind.lows[-20:])
        sig = {
            'type': 'ACCUMULATION',
            'rvol': ind.get_rvol(),
            'bb': ind.get_bb_width(),
            'low': live_low,
            'atr': ind.atr,
        }
        chart_buf = None
        sl_preview = compute_final_sl(closes[-1], live_low, ind.atr, get_tuning())
        risk_preview = closes[-1] - sl_preview
        if risk_preview > 0:
            tp1 = closes[-1] + (risk_preview * 2.0)
            tp2 = closes[-1] + (risk_preview * 3.5)
            tp3 = closes[-1] + (risk_preview * 5.5)
            chart_buf = generate_chart_image(p['symbol'], opens, closes, highs, lows, volumes,
                                             ind.ema21, ind.ema50, sl_preview, tp1, tp2, tp3)

        # Dispatch dengan from_pending=True (skip queue loop)
        dispatch_signal(p['symbol'], closes[-1], sig, ind, 'ACCUMULATION',
                        chart_buf, p['daily_note'], p['user_cap'], p['user_risk'],
                        from_pending=True)
    except Exception as e:
        logger.error(f"Dispatch from pending error {p['symbol']}: {e}")

async def pending_signal_processor():
    """V8: Worker yang check pending signals setiap 60s untuk confirmation."""
    logger.info("✅ [PENDING] Confirmation worker started.")
    while True:
        await asyncio.sleep(60)
        try:
            pendings = get_pending_signals()
            now = time.time()
            for p in pendings:
                sym = p['symbol']
                # Expired?
                if now > p['expiry']:
                    drop_pending(sym)
                    save_cooldown(sym, int(get_tuning().get('fail_cooldown_h', 2)))
                    log_activity(f"{sym} ⏰ Pending expired")
                    continue
                # Belum cukup masa (perlu min 1H selepas detection)
                if now - p['detect_time'] < 3600:
                    continue
                confirmed, reason = await check_confirmation_async(dict(p))
                if confirmed is None:
                    continue  # retry next tick
                if confirmed:
                    log_activity(f"{sym} ✓ Confirmed: {reason}")
                    await dispatch_from_pending(dict(p))
                    drop_pending(sym)
                else:
                    drop_pending(sym)
                    save_cooldown(sym, int(get_tuning().get('fail_cooldown_h', 2)))
                    log_activity(f"{sym} ✗ Conf gagal: {reason}")
        except Exception as e:
            logger.error(f"Pending processor error: {e}")

# ==========================================
# TRADE TRACKER — V8: SL move to BE on TP1, record exit_price for journal
# ==========================================
async def trade_tracker():
    """V8 OVERHAUL: 
    - Bila TP1 hit: SL update ke break-even (entry) dalam DB
    - Record exit_price untuk journal
    - Logging untuk failed sends (tiada lagi silent swallow)
    """
    logger.info("✅ [TRACKER] Trade tracker started.")
    while True:
        await asyncio.sleep(5)
        try:
            trades = get_active_trades()
        except Exception as e:
            logger.error(f"Tracker get_active_trades error: {e}")
            continue
        for t in trades:
            sym = t['symbol']
            if sym not in latest_prices: 
                continue
            price = latest_prices[sym]['c']
            status = t['status']
            reply = None
            new_status = status
            new_sl = None
            record_exit = False

            if price <= t['sl'] and status not in ['STOP_LOSS', 'COMPLETED']:
                autopsy = spot_post_mortem(sym)
                # V8: nyatakan sama ada ini SL asal atau SL-to-BE
                sl_desc = "Proteksi modal" if status == 'TRACKING' else "SL @ BE/Trailing"
                reply = (f"🛑 <b>{sym} — STOP LOSS HIT</b>\n"
                         f"{sl_desc} pada <code>${price:.6f}</code>\n\n"
                         f"🔬 <b>POST-MORTEM:</b>\n{autopsy}")
                new_status = 'STOP_LOSS'
                record_exit = True
            elif price >= t['tp3'] and status != 'COMPLETED':
                reply = f"👑 <b>{sym} — TP3 MOONSHOT!</b>\n<code>${price:.6f}</code>"
                new_status = 'COMPLETED'
                record_exit = True
            elif price >= t['tp2'] and status not in ['TP2_HIT', 'COMPLETED']:
                reply = f"🔥 <b>{sym} — TP2 HIT!</b>\nPoketkan 50% di <code>${price:.6f}</code>"
                new_status = 'TP2_HIT'
            elif price >= t['tp1'] and status == 'TRACKING':
                # V8 FIX: SL update ke BE (entry) supaya promise dalam mesej match dengan kelakuan
                new_sl = t['entry']
                reply = (f"✅ <b>{sym} — TP1 SECURED!</b>\n"
                         f"SL dipindah ke BE (<code>${new_sl:.6f}</code>) di <code>${price:.6f}</code>")
                new_status = 'TP1_HIT'

            if reply:
                try:
                    bot.send_message(TELEGRAM_CHAT_ID, reply,
                                     reply_to_message_id=t['msg_id'], parse_mode="HTML")
                    # V8: update SL dalam DB jika perlu
                    if new_sl is not None:
                        update_trade_sl(t['msg_id'], new_sl)
                    # V8: record exit_price untuk journal jika trade closed
                    if record_exit:
                        update_trade_status(t['msg_id'], new_status, exit_price=price)
                    else:
                        update_trade_status(t['msg_id'], new_status)
                except Exception as e:
                    # V8 FIX: tidak lagi silent — log error
                    logger.error(f"Tracker send/update error {sym}: {e}")

# ==========================================
# WEEKLY TEAR SHEET (kekal sebagai overview cepat)
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
            f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
            f"🏆 <b>Best:</b> {best['symbol']} ({r_map.get(best['status'], 0):+.1f}R)\n"
            f"💀 <b>Worst:</b> {worst['symbol']} ({r_map.get(worst['status'], 0):+.1f}R)\n"
            f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
            f"🔍 <i>Transparency is our edge.</i>"
        )

def tear_sheet_scheduler():
    """KEKAL: Sunday 20:00 UTC tear sheet."""
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
# V8 NEW: TRADING JOURNAL (Detailed Audit)
# ==========================================
MYT_OFFSET = timedelta(hours=8)  # Malaysia Time = UTC+8

def _fmt_myt(ts):
    """Format timestamp ke Malaysia Time."""
    if not ts:
        return "—"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) + MYT_OFFSET
    return dt.strftime("%a %d %b %H:%M")

def _calc_r_realized(t):
    """Kira R-multiple realized — combine nominal status + exit_price jika tersedia."""
    r_map = {'STOP_LOSS': -1.0, 'TP1_HIT': 2.0, 'TP2_HIT': 3.5, 'COMPLETED': 5.5,
             'TRACKING': 0.0}
    return r_map.get(t['status'], 0.0)

def generate_trading_journal(days=7):
    """V8 NEW: Trading journal komprehensif untuk audit transparency.
    Return (summary_text, detail_text). Summary masuk Telegram, detail boleh attach sebagai file.
    """
    period_start = time.time() - (days * 86400)
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.row_factory = sqlite3.Row
        trades = conn.execute(
            "SELECT * FROM active_trades WHERE timestamp > ? ORDER BY timestamp ASC",
            (period_start,)).fetchall()
        pending = conn.execute("SELECT * FROM pending_signals").fetchall()

    closed = [t for t in trades if t['status'] in ('STOP_LOSS', 'TP1_HIT', 'TP2_HIT', 'COMPLETED')]
    open_trades = [t for t in trades if t['status'] in ('TRACKING',)]

    period_label = f"{datetime.fromtimestamp(period_start, tz=timezone.utc).strftime('%d %b')} – {datetime.now(timezone.utc).strftime('%d %b %Y')}"

    if not closed and not open_trades and not pending:
        empty = (f"📓 <b>NOVA7 TRADING JOURNAL</b>\n"
                 f"🗓️ <i>{period_label} | {days} hari</i>\n"
                 f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
                 f"<i>Tiada aktiviti dalam tempoh ini.</i>")
        return empty, None

    # ============ AGGREGATE STATS ============
    total_closed = len(closed)
    wins = [t for t in closed if t['status'] != 'STOP_LOSS']
    losses = [t for t in closed if t['status'] == 'STOP_LOSS']
    win_rate = (len(wins) / total_closed * 100) if total_closed > 0 else 0
    total_r = sum(_calc_r_realized(t) for t in closed)
    gross_profit = sum(_calc_r_realized(t) for t in wins)
    gross_loss = abs(sum(_calc_r_realized(t) for t in losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float('inf') if gross_profit > 0 else 0)
    avg_win = (gross_profit / len(wins)) if wins else 0
    avg_loss = (gross_loss / len(losses)) if losses else 0
    pf_str = f"{profit_factor:.2f}" if profit_factor != float('inf') else "∞"

    # ============ ENGINE BREAKDOWN ============
    by_engine = {'BREAKOUT': [], 'ACCUMULATION': []}
    for t in closed:
        eng = t['engine'] if t['engine'] in by_engine else 'BREAKOUT'
        by_engine[eng].append(t)

    engine_lines = []
    for eng, lst in by_engine.items():
        if not lst:
            continue
        w = sum(1 for x in lst if x['status'] != 'STOP_LOSS')
        l = len(lst) - w
        wr = (w / len(lst) * 100) if lst else 0
        r = sum(_calc_r_realized(x) for x in lst)
        engine_lines.append(f"• <b>{eng}:</b> {len(lst)} trade ({w}W/{l}L, {wr:.0f}%) | {r:+.1f}R")

    # ============ MACRO CORRELATION ============
    win_macro = [t['macro_btc_pct'] for t in wins if t['macro_btc_pct'] != 0]
    loss_macro = [t['macro_btc_pct'] for t in losses if t['macro_btc_pct'] != 0]
    macro_line = ""
    if win_macro and loss_macro:
        avg_win_macro = sum(win_macro) / len(win_macro)
        avg_loss_macro = sum(loss_macro) / len(loss_macro)
        macro_line = (f"\n🌐 <b>Macro di signal:</b>\n"
                      f"   • Wins: BTC avg {avg_win_macro:+.2f}%\n"
                      f"   • Losses: BTC avg {avg_loss_macro:+.2f}%")

    # ============ TOP/WORST ============
    top_lines = []
    worst_lines = []
    if closed:
        sorted_trades = sorted(closed, key=lambda x: _calc_r_realized(x), reverse=True)
        for t in sorted_trades[:3]:
            r = _calc_r_realized(t)
            top_lines.append(f"   • {t['symbol']} ({t['engine']}) {r:+.1f}R")
        for t in sorted_trades[-3:][::-1]:
            r = _calc_r_realized(t)
            worst_lines.append(f"   • {t['symbol']} ({t['engine']}) {r:+.1f}R")

    # ============ TIME-OF-DAY ANALYSIS (UTC hour bucket) ============
    hour_buckets = {}
    for t in closed:
        hr = datetime.fromtimestamp(t['timestamp'], tz=timezone.utc).hour
        hour_buckets.setdefault(hr, []).append(t)
    best_hour_line = ""
    if hour_buckets:
        # cari hour dengan win rate terbaik (min 2 trades)
        candidates = [(h, lst) for h, lst in hour_buckets.items() if len(lst) >= 2]
        if candidates:
            best_h, best_lst = max(candidates,
                key=lambda x: sum(1 for t in x[1] if t['status'] != 'STOP_LOSS') / len(x[1]))
            w = sum(1 for t in best_lst if t['status'] != 'STOP_LOSS')
            wr = w / len(best_lst) * 100
            myt_h = (best_h + 8) % 24
            best_hour_line = f"\n🕐 <b>Best UTC hour:</b> {best_h:02d}:00 ({myt_h:02d}:00 MYT) — {wr:.0f}% win ({len(best_lst)} trades)"

    # ============ INSIGHT GENERATION ============
    insights = []
    if losses and len(losses) >= 3:
        # ACCUMULATION underperform?
        acc_losses = [t for t in losses if t['engine'] == 'ACCUMULATION']
        if len(acc_losses) >= 3 and len(acc_losses) / max(1, len(losses)) > 0.6:
            insights.append("⚠️ Majoriti loss datang dari ACCUMULATION. Pertimbang /tune ketat atau matikan acc filter.")
        # Macro correlation
        if loss_macro and sum(loss_macro)/len(loss_macro) < -1.0:
            insights.append("⚠️ Losses kerap berlaku semasa BTC dump (>-1% 24h). Macro filter dah aktif, pertimbang threshold lebih ketat.")
    if win_rate > 50:
        insights.append("✅ Win rate sihat. Kekalkan tuning semasa.")
    if profit_factor != float('inf') and profit_factor < 1.0 and total_closed >= 5:
        insights.append("🔴 Profit factor < 1.0. Sistem sedang rugi. Pertimbang /tune ketat dan audit individual trade.")
    if not insights:
        insights.append("📊 Sample size kecil. Tunggu lebih banyak trade untuk pattern jelas.")

    # ============ SUMMARY TEXT (Telegram) ============
    summary = (
        f"📓 <b>NOVA7 TRADING JOURNAL</b>\n"
        f"🗓️ <i>{period_label} ({days} hari)</i>\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"📊 <b>AGGREGATE</b>\n"
        f"• Total Closed: {total_closed}\n"
        f"• Win Rate: {win_rate:.1f}% ({len(wins)}W / {len(losses)}L)\n"
        f"• Profit Factor: {pf_str}\n"
        f"• Net R: {total_r:+.2f}R\n"
        f"• Avg Win: +{avg_win:.2f}R | Avg Loss: -{avg_loss:.2f}R\n"
        f"• Open: {len(open_trades)} | Pending: {len(pending)}"
        f"{macro_line}"
        f"\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"🔥 <b>ENGINE BREAKDOWN</b>\n"
        + ("\n".join(engine_lines) if engine_lines else "<i>Tiada data engine</i>")
        + f"{best_hour_line}\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
    )
    if top_lines:
        summary += "🏆 <b>TOP TRADES</b>\n" + "\n".join(top_lines) + "\n"
    if worst_lines:
        summary += "💀 <b>WORST TRADES</b>\n" + "\n".join(worst_lines) + "\n"
    summary += (
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"💡 <b>INSIGHT</b>\n"
        + "\n".join(insights)
        + "\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"🔍 <i>Audit transparent — Nova7 Institutional</i>"
    )

    # ============ DETAIL TEXT (File attachment) ============
    detail_lines = []
    detail_lines.append("=" * 70)
    detail_lines.append(f"NOVA7 TRADING JOURNAL — DETAILED AUDIT")
    detail_lines.append(f"Period: {period_label} ({days} days)")
    detail_lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    detail_lines.append("=" * 70)
    detail_lines.append("")
    detail_lines.append(f"SUMMARY")
    detail_lines.append(f"  Total Closed     : {total_closed}")
    detail_lines.append(f"  Wins / Losses    : {len(wins)}W / {len(losses)}L  ({win_rate:.2f}% win rate)")
    detail_lines.append(f"  Profit Factor    : {pf_str}")
    detail_lines.append(f"  Net R            : {total_r:+.2f}R")
    detail_lines.append(f"  Gross Profit / Loss: +{gross_profit:.2f}R / -{gross_loss:.2f}R")
    detail_lines.append(f"  Avg Win / Loss   : +{avg_win:.2f}R / -{avg_loss:.2f}R")
    detail_lines.append(f"  Open Trades      : {len(open_trades)}")
    detail_lines.append(f"  Pending Signals  : {len(pending)}")
    detail_lines.append("")
    detail_lines.append(f"ENGINE BREAKDOWN")
    for eng, lst in by_engine.items():
        if not lst:
            continue
        w = sum(1 for x in lst if x['status'] != 'STOP_LOSS')
        l = len(lst) - w
        r = sum(_calc_r_realized(x) for x in lst)
        detail_lines.append(f"  {eng:14s}: {len(lst):3d} trades | {w}W/{l}L | {r:+.2f}R")
    detail_lines.append("")
    detail_lines.append("=" * 70)
    detail_lines.append("INDIVIDUAL TRADES")
    detail_lines.append("=" * 70)
    detail_lines.append("")
    detail_lines.append(f"{'#':<3} {'Symbol':<12} {'Engine':<13} {'Entry':<12} {'Exit':<12} {'SL':<12} {'Status':<11} {'R':<7} {'BTC%':<7} {'Time (MYT)':<18}")
    detail_lines.append("-" * 110)
    for i, t in enumerate(closed, 1):
        exit_p = t['exit_price'] if t['exit_price'] else 0
        r = _calc_r_realized(t)
        btc_m = t['macro_btc_pct'] if t['macro_btc_pct'] else 0
        detail_lines.append(
            f"{i:<3} {t['symbol']:<12} {t['engine']:<13} "
            f"{t['entry']:<12.6f} {exit_p:<12.6f} {t['sl']:<12.6f} "
            f"{t['status']:<11} {r:+.2f}R  {btc_m:+.2f}%  {_fmt_myt(t['timestamp']):<18}"
        )
    if open_trades:
        detail_lines.append("")
        detail_lines.append("OPEN TRADES (still tracking)")
        detail_lines.append("-" * 70)
        for t in open_trades:
            detail_lines.append(
                f"  {t['symbol']:<12} {t['engine']:<13} entry={t['entry']:.6f} sl={t['sl']:.6f} since {_fmt_myt(t['timestamp'])}"
            )
    if pending:
        detail_lines.append("")
        detail_lines.append("PENDING SIGNALS (awaiting confirmation)")
        detail_lines.append("-" * 70)
        for p in pending:
            detail_lines.append(
                f"  {p['symbol']:<12} detect={p['detect_price']:.6f} expires={_fmt_myt(p['expiry'])}"
            )
    detail_lines.append("")
    detail_lines.append("=" * 70)
    detail_lines.append("INSIGHTS")
    detail_lines.append("=" * 70)
    for ins in insights:
        detail_lines.append(f"  • {ins}")
    detail_lines.append("")
    detail_lines.append("End of report.")

    detail = "\n".join(detail_lines)
    return summary, detail

def send_trading_journal(target_chat_id=None):
    """V8 NEW: Send journal — summary message + detail file attachment."""
    if not bot:
        return
    chat_id = target_chat_id or TELEGRAM_CHAT_ID
    if not chat_id:
        return
    try:
        summary, detail = generate_trading_journal(days=7)
        bot.send_message(chat_id, summary, parse_mode="HTML")
        if detail:
            buf = io.BytesIO(detail.encode('utf-8'))
            buf.name = f"nova7_journal_{datetime.now(timezone.utc).strftime('%Y%m%d')}.txt"
            try:
                bot.send_document(chat_id, buf, caption="📓 Detailed audit log (text file)")
            except Exception as e:
                logger.error(f"Journal file send error: {e}")
        logger.info("Trading Journal sent.")
    except Exception as e:
        logger.error(f"Journal generation error: {e}")
        try:
            bot.send_message(chat_id, f"❌ Journal error: {str(e)[:200]}", parse_mode="HTML")
        except Exception:
            pass

def journal_scheduler():
    """V8 NEW: Auto journal setiap Sabtu malam Malaysia (22:00 MYT = 14:00 UTC)."""
    logger.info("✅ [JOURNAL] Scheduler started (Saturday 22:00 MYT auto-run).")
    while True:
        now_utc = datetime.now(timezone.utc)
        # Saturday = weekday() == 5; 22:00 MYT = 14:00 UTC
        if now_utc.weekday() == 5 and now_utc.hour == 14 and now_utc.minute < 5:
            try:
                send_trading_journal()
            except Exception as e:
                logger.error(f"Journal scheduler error: {e}")
            time.sleep(3600)  # sleep 1h supaya tidak retrigger
        time.sleep(60)

# ==========================================
# TELEGRAM COMMANDS (DENGAN /tune, /journal, /pending) — V8: tambah /journal, /pending
# ==========================================
@bot.message_handler(commands=['start', 'help'])
def cmd_start(msg):
    global is_scanning
    is_scanning = True
    bot.reply_to(msg,
        "⚡ <b>NOVA7 v8 [PREMIUM + TUNABLE]</b>\n\n"
        "🚀 Layer 1: Real-time Radar\n"
        "🕵️ Layer 2: Sniper + Daily Confluence\n"
        "🌐 Macro Filter (BTC pre-signal)\n"
        "🕒 Confirmation Queue (Accumulation)\n"
        "📐 ATR-based SL\n"
        "📊 Auto-Chart Visual Proof\n"
        "💼 Fund Manager Position Sizing\n"
        "📓 Trading Journal (auto Sabtu 22:00 MYT)\n\n"
        "<b>Commands:</b>\n"
        "/tune show — Lihat parameter\n"
        "/tune standard — Mode balance\n"
        "/tune longgar — Banyak signal\n"
        "/tune ketat — Sikit signal\n"
        "/tune custom key=value\n"
        "/modal 1000 — Set modal\n"
        "/force FETUSDT — Scan manual\n"
        "/report — Tear Sheet ringkas\n"
        "/journal — Trading Journal penuh\n"
        "/pending — Lihat pending signals\n"
        "/macro — Status BTC macro\n"
        "/diagnose — Debug kenapa tiada signal\n"
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
            f"• Daily Filter: <code>{'ON' if int(t.get('bo_daily_filter', 1)) == 1 else 'OFF'}</code>\n"
            f"<b>🕵️ Accumulation Engine:</b>\n"
            f"• BB Width max: <code>{t.get('acc_bb_width', 8.0):.1f}%</code>\n"
            f"• RVOL threshold: <code>{t.get('acc_rvol', 2.0):.2f}x</code>\n"
            f"• RSI range: <code>{t.get('acc_rsi_min', 25):.0f}-{t.get('acc_rsi_max', 45):.0f}</code>\n"
            f"<b>🌐 Macro Filter:</b>\n"
            f"• Status: <code>{'ON' if int(t.get('macro_btc_filter', 1)) == 1 else 'OFF'}</code>\n"
            f"• BTC 24h min: <code>{t.get('macro_btc_24h_min', -1.5):+.1f}%</code>\n"
            f"<b>🕒 Confirmation Queue:</b>\n"
            f"• Status: <code>{'ON' if int(t.get('confirm_required', 1)) == 1 else 'OFF'}</code>\n"
            f"• Expiry: <code>{t.get('pending_expiry_h', 2):.0f}h</code>\n"
            f"<b>📐 SL Calculation:</b>\n"
            f"• ATR multiplier: <code>{t.get('sl_atr_mult', 1.5):.1f}x</code>\n"
            f"• Max SL distance: <code>{t.get('sl_max_pct', 0.08)*100:.0f}%</code>\n"
            f"<b>🐋 Radar:</b>\n"
            f"• Momentum trigger: <code>{t.get('radar_momentum', 2.0):.1f}%</code>\n"
            f"• Min volume: <code>${t.get('radar_min_vol', 12000000)/1e6:.1f}M</code>\n"
            f"• Higher Low mode: <code>{'HARD' if int(t.get('acc_require_higher_low', 0)) == 1 else 'SOFT (hint only)'}</code>\n"
            f"<b>⏱️ Cooldowns:</b>\n"
            f"• Breakout: <code>{t.get('cd_breakout', 24):.0f}h</code>\n"
            f"• Accumulation: <code>{t.get('cd_accumulation', 48):.0f}h</code>\n"
            f"• Fail cooldown: <code>{t.get('fail_cooldown_h', 2):.0f}h</code>"
        )
        bot.reply_to(msg, report, parse_mode="HTML")
        return
    if args[0] == 'standard':
        set_tuning({**DEFAULT_TUNING, 'mode': 0})
        bot.reply_to(msg, "✅ <b>Mode STANDARD aktif.</b>\nBalance antara kualiti & kuantiti.\nMacro filter ON. Confirmation ON.", parse_mode="HTML")
    elif args[0] == 'longgar':
        set_tuning({
            'mode': 1,
            'bo_rvol': 1.4, 'bo_rsi_min': 45, 'bo_rsi_max': 80, 'bo_daily_filter': 0,
            'acc_bb_width': 20.0, 'acc_rvol': 1.2, 'acc_rsi_max': 55, 'acc_rsi_min': 20,
            'acc_require_higher_low': 0,
            'radar_momentum': 1.6, 'radar_min_vol': 8_000_000,
            'cd_breakout': 12, 'cd_accumulation': 24,
            'macro_btc_filter': 1, 'macro_btc_24h_min': -3.0,
            'confirm_required': 0,
            'sl_atr_mult': 1.2, 'sl_max_pct': 0.10,
            'fail_cooldown_h': 1,
        })
        bot.reply_to(msg, "🟢 <b>Mode LONGGAR aktif.</b>\nLebih banyak signal, sesuai untuk Bull Market.\n⚠️ Confirmation OFF — entry lebih agresif.", parse_mode="HTML")
    elif args[0] == 'ketat':
        set_tuning({
            'mode': 2,
            'bo_rvol': 2.2, 'bo_rsi_min': 55, 'bo_rsi_max': 70, 'bo_daily_filter': 1,
            'acc_bb_width': 10.0, 'acc_rvol': 1.8, 'acc_rsi_max': 40, 'acc_rsi_min': 28,
            'acc_require_higher_low': 1,  # ketat = higher low wajib
            'radar_momentum': 3.0, 'radar_min_vol': 20_000_000,
            'cd_breakout': 48, 'cd_accumulation': 72,
            # V8: macro extra strict
            'macro_btc_filter': 1, 'macro_btc_24h_min': -1.0,
            'confirm_required': 1, 'pending_expiry_h': 2,
            'sl_atr_mult': 2.0, 'sl_max_pct': 0.06,
            'fail_cooldown_h': 4,
        })
        bot.reply_to(msg, "🔴 <b>Mode KETAT aktif.</b>\nSangat selective, sesuai untuk Bear/Sideways.\nMacro & Confirmation strict.", parse_mode="HTML")
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
    status_label = "🟢 AKTIF" if is_scanning else "🔴 STANDBY"
    t = get_tuning()
    s = get_stats_snapshot()
    modes = {0: 'STANDARD', 1: 'LONGGAR', 2: 'KETAT'}
    pending_count = len(get_pending_signals())
    open_count = len(get_active_trades())
    text = (
        f"📊 <b>NOVA7 STATUS [{status_label}]</b>\n"
        f"🎛️ <b>Mode:</b> {modes.get(int(t.get('mode', 0)), 'UNKNOWN')}\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"🐋 Radar: {s['radar_coins']} coins\n"
        f"🎯 L2 Scans: {s['layer2_scans']}\n"
        f"📈 Signals: {s['signals_sent']}\n"
        f"❌ Rejected: {s['rejected']}\n"
        f"🕒 Pending: {pending_count}\n"
        f"📂 Open Trades: {open_count}\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')} | "
        f"{(datetime.now(timezone.utc) + MYT_OFFSET).strftime('%H:%M MYT')}"
    )
    bot.reply_to(msg, text, parse_mode="HTML")

@bot.message_handler(commands=['report'])
def cmd_report(msg):
    bot.reply_to(msg, "⏳ <i>Menjana Tear Sheet...</i>", parse_mode="HTML")
    bot.reply_to(msg, generate_tear_sheet(), parse_mode="HTML")

# V8.2 NEW: /diagnose — debug kenapa tiada signal
@bot.message_handler(commands=['diagnose'])
def cmd_diagnose(msg):
    """
    Ambil 5 coin teratas dari latest_prices, jalankan engine,
    dan tunjuk breakdown SETIAP condition — untuk debug.
    """
    args = msg.text.split()
    t = get_tuning()
    # Jika user bagi symbol: /diagnose FETUSDT
    if len(args) >= 2:
        targets = [args[1].upper()]
        if not targets[0].endswith('USDT'): targets[0] += 'USDT'
    else:
        # Ambil 8 coin highest vol dari radar
        sorted_syms = sorted(latest_prices.keys(),
                             key=lambda s: latest_prices[s]['q'], reverse=True)
        targets = sorted_syms[:8]

    bot.reply_to(msg, f"🔬 <b>DIAGNOSE MODE</b> — checking {len(targets)} symbols...", parse_mode="HTML")

    results = []
    for sym in targets:
        try:
            import requests as _req
            url = f"https://api.binance.com/api/v3/klines?symbol={sym}&interval=1h&limit=150"
            res = _req.get(url, timeout=8).json()
            if not isinstance(res, list) or len(res) < 51:
                results.append(f"• {sym}: kurang data")
                continue
            opens = [float(d[1]) for d in res]
            highs = [float(d[2]) for d in res]
            lows  = [float(d[3]) for d in res]
            closes = [float(d[4]) for d in res]
            vols  = [float(d[7]) for d in res]
            ind = IncrementalIndicators()
            if not ind.initialize(opens, closes, highs, lows, vols):
                results.append(f"• {sym}: init fail")
                continue
            bb = ind.get_bb_width()
            rvol = ind.get_rvol()
            macro_ok, macro_reason = macro_filter_pass('ACCUMULATION', t)
            bb_ok  = bb < t.get('acc_bb_width', 15.0)
            rv_ok  = rvol >= t.get('acc_rvol', 1.4)
            ema_ok = closes[-1] < ind.ema50
            rsi_ok = t.get('acc_rsi_min', 25) < ind.rsi < t.get('acc_rsi_max', 48)
            grn_ok = ind.is_current_green()
            hl_ok  = ind.is_recent_higher_low()
            score  = sum([bb_ok, rv_ok, ema_ok, rsi_ok, grn_ok])
            # Buat bar ringkas
            bar = (f"BB:{'✅' if bb_ok else '❌'}{bb:.1f}% "
                   f"RVOL:{'✅' if rv_ok else '❌'}{rvol:.2f}x "
                   f"EMA:{'✅' if ema_ok else '❌'} "
                   f"RSI:{'✅' if rsi_ok else '❌'}{ind.rsi:.0f} "
                   f"GRN:{'✅' if grn_ok else '❌'} "
                   f"HL:{'✅' if hl_ok else '–'}")
            results.append(f"<b>{sym}</b> [{score}/5]\n  {bar}")
        except Exception as e:
            results.append(f"• {sym}: error {str(e)[:30]}")

    macro_ok, macro_reason = macro_filter_pass('ACCUMULATION', t)
    macro_line = f"🌐 Macro: {'✅ PASS' if macro_ok else '❌ BLOCK'} — {macro_reason}"
    t_summary = (f"⚙️ Tuning: BB<{t.get('acc_bb_width',15)}% | "
                 f"RVOL>{t.get('acc_rvol',1.4):.1f}x | "
                 f"RSI {t.get('acc_rsi_min',25):.0f}–{t.get('acc_rsi_max',48):.0f}")

    reply = (f"🔬 <b>DIAGNOSE RESULT</b>\n"
             f"{macro_line}\n{t_summary}\n"
             f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
             + "\n".join(results)
             + "\n┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"
             "\n<i>Score 5/5 = siap dispatch. Kurang 1 = mana yang block.</i>")
    bot.reply_to(msg, reply, parse_mode="HTML")

# V8 NEW: /journal command — boleh dipaksa
@bot.message_handler(commands=['journal'])
def cmd_journal(msg):
    bot.reply_to(msg, "📓 <i>Menjana Trading Journal penuh...</i>", parse_mode="HTML")
    # Boleh terima arg: /journal 14 untuk 14 hari
    args = msg.text.split()
    days = 7
    if len(args) >= 2:
        try:
            days = max(1, min(90, int(args[1])))
        except ValueError:
            pass
    try:
        summary, detail = generate_trading_journal(days=days)
        bot.send_message(msg.chat.id, summary, parse_mode="HTML")
        if detail:
            buf = io.BytesIO(detail.encode('utf-8'))
            buf.name = f"nova7_journal_{datetime.now(timezone.utc).strftime('%Y%m%d')}_{days}d.txt"
            try:
                bot.send_document(msg.chat.id, buf,
                                  caption=f"📓 Detailed audit log ({days} hari)")
            except Exception as e:
                logger.error(f"Journal doc send error: {e}")
                bot.send_message(msg.chat.id, f"⚠️ File attachment gagal: {str(e)[:80]}", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Journal cmd error: {e}")
        bot.reply_to(msg, f"❌ Error: {str(e)[:200]}", parse_mode="HTML")

# V8 NEW: /pending command
@bot.message_handler(commands=['pending'])
def cmd_pending(msg):
    try:
        pendings = get_pending_signals()
        if not pendings:
            bot.reply_to(msg, "📭 <i>Tiada pending signals.</i>", parse_mode="HTML")
            return
        lines = ["🕒 <b>PENDING SIGNALS</b> (menunggu confirmation)\n"]
        for p in pendings:
            mins_since = int((time.time() - p['detect_time']) / 60)
            mins_to_expiry = int((p['expiry'] - time.time()) / 60)
            lines.append(
                f"• <b>{p['symbol']}</b> ({p['engine']})\n"
                f"  Detect: ${p['detect_price']:.6f} ({mins_since}m ago)\n"
                f"  Expires in: {mins_to_expiry}m"
            )
        bot.reply_to(msg, "\n".join(lines), parse_mode="HTML")
    except Exception as e:
        bot.reply_to(msg, f"❌ Error: {str(e)[:200]}", parse_mode="HTML")

# V8 NEW: /macro command
@bot.message_handler(commands=['macro'])
def cmd_macro(msg):
    state = get_btc_macro_state(force_refresh=True)
    if not state:
        bot.reply_to(msg, "❌ Macro data tidak tersedia.", parse_mode="HTML")
        return
    t = get_tuning()
    threshold = t.get('macro_btc_24h_min', -1.5)
    arrow = "🟢" if state['btc_24h_pct'] >= threshold else "🔴"
    trend = "🟢 BULLISH" if state['btc_above_ema21d'] else "🔴 BEARISH"
    text = (
        f"🌐 <b>MACRO STATUS (BTC)</b>\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"💵 BTC Price: <code>${state['btc_price']:,.2f}</code>\n"
        f"📊 24h Change: {arrow} <code>{state['btc_24h_pct']:+.2f}%</code>\n"
        f"📈 Daily EMA21: <code>${state['btc_ema21d']:,.2f}</code>\n"
        f"🎯 Daily Trend: {trend}\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"⚙️ Threshold: <code>{threshold:+.1f}%</code>\n"
        f"🚦 Filter Status: <b>{'PASS' if state['btc_24h_pct'] >= threshold else 'BLOCK'}</b> (Breakout)\n"
        f"🚦 Filter Status: <b>{'PASS' if (state['btc_24h_pct'] >= threshold and state['btc_above_ema21d']) else 'BLOCK'}</b> (Accumulation)"
    )
    bot.reply_to(msg, text, parse_mode="HTML")

@bot.message_handler(commands=['force'])
def cmd_force(msg):
    args = msg.text.split()
    if len(args) < 2:
        bot.reply_to(msg, "⚠️ Format: <code>/force FETUSDT</code>", parse_mode="HTML")
        return
    sym = args[1].upper()
    if not sym.endswith('USDT'): 
        sym += 'USDT'
    user_cap, user_risk = get_user_capital(msg.from_user.id)
    bot.reply_to(msg, f"🎯 <b>Sniper Deployed:</b> {sym} (Modal: ${user_cap:,.0f})", parse_mode="HTML")
    threading.Thread(
        target=lambda: asyncio.run(
            layer2_sniper(sym, 'BREAKOUT', force=True, chat_id=msg.chat.id,
                          user_cap=user_cap, user_risk=user_risk)),
        daemon=True).start()

# ==========================================
# AI INSIGHT CALLBACK HANDLER
# ==========================================
@bot.callback_query_handler(func=lambda call: call.data.startswith("ai_summary_"))
def handle_ai_insight(call):
    """Handle AI Insight button click — panggil engine sebenar."""
    symbol = call.data.split("_")[-1]
    bot.answer_callback_query(call.id, "🤖 Menjana analisis...", show_alert=False)
    try:
        insight_text = generate_ai_insight(symbol)
        bot.send_message(call.message.chat.id, insight_text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"AI insight error for {symbol}: {e}")
        bot.send_message(
            call.message.chat.id,
            f"❌ <b>Gagal jana insight untuk {symbol}:</b> {str(e)[:80]}",
            parse_mode="HTML"
        )

# ==========================================
# FLASK APP (KEEP-ALIVE / WEBHOOK)
# ==========================================
app = Flask(__name__)

@app.route('/', methods=['GET', 'HEAD'])
def home():
    return "🟢 Nova7 v8 Online — 2-Layer Radar + Macro Filter + Confirmation Queue + Journal", 200

@app.route('/webhook', methods=['POST'])
def webhook():
    if bot is None:
        return "Bot not initialized", 500
    try:
        update = telebot.types.Update.de_json(request.get_data().decode('utf-8'))
        bot.process_new_updates([update])
        return "OK", 200
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return "Error", 500

def graceful_shutdown(signum, frame):
    logger.info("⚙️ Graceful shutdown initiated...")
    global is_scanning
    is_scanning = False
    if bot:
        try:
            bot.stop_polling()
        except Exception:
            pass
    sys.exit(0)

signal.signal(signal.SIGTERM, graceful_shutdown)
signal.signal(signal.SIGINT, graceful_shutdown)

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# ==========================================
# MAIN ORCHESTRATOR — V8: Tambah journal_scheduler + pending_signal_processor
# ==========================================
if __name__ == "__main__":
    init_db()
    logger.info("🚀 Starting Nova7 v8 (Tier 1–4 fixes + Journal)...")

    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if render_url:
        # ===== RENDER WEBHOOK MODE =====
        webhook_url = f"{render_url.rstrip('/')}/webhook"
        try:
            bot.remove_webhook()
            time.sleep(1)
            bot.set_webhook(url=webhook_url)
            logger.info(f"✅ Webhook set: {webhook_url}")
        except Exception as e:
            logger.error(f"Webhook setup error: {e}")

        # Background threads
        threading.Thread(target=lambda: asyncio.run(trade_tracker()), daemon=True).start()
        threading.Thread(target=tear_sheet_scheduler, daemon=True).start()

        # V8 NEW threads
        threading.Thread(target=lambda: asyncio.run(pending_signal_processor()), daemon=True).start()
        threading.Thread(target=journal_scheduler, daemon=True).start()

        # Layer 1 radar
        threading.Thread(target=lambda: asyncio.run(layer1_radar()), daemon=True).start()

        # Flask di main thread (Render perlukan ini bind ke PORT)
        run_flask()
    else:
        # ===== LOCAL POLLING MODE =====
        logger.info("🏠 Local mode: polling")
        try:
            bot.remove_webhook()
        except Exception:
            pass

        threading.Thread(target=lambda: asyncio.run(trade_tracker()), daemon=True).start()
        threading.Thread(target=tear_sheet_scheduler, daemon=True).start()

        # V8 NEW threads
        threading.Thread(target=lambda: asyncio.run(pending_signal_processor()), daemon=True).start()
        threading.Thread(target=journal_scheduler, daemon=True).start()

        threading.Thread(target=lambda: asyncio.run(layer1_radar()), daemon=True).start()

        try:
            bot.infinity_polling(skip_pending=True, timeout=30)
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received.")
            graceful_shutdown(None, None)
