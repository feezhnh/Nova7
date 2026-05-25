from datetime import datetime, timezone
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
    'acc_bb_width': 24.0,  # FIX: dikemaskini dari 6.0 → 24.0 untuk match standard BB formula (4σ/SMA)
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
        # NEW: Audit log untuk post-trade analysis
        conn.execute('''CREATE TABLE IF NOT EXISTS audit_log
            (msg_id INTEGER PRIMARY KEY, symbol TEXT, engine TEXT,
            entry REAL, sl REAL, exit_status TEXT, exit_price REAL,
            btc_daily_trend TEXT, btc_4h_trend TEXT, btc_24h_change REAL,
            price_1h_after REAL, price_4h_after REAL, price_24h_after REAL,
            signal_time REAL, exit_time REAL)''')
        # Init default tuning jika belum ada
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

def save_trade(msg_id, symbol, entry, sl, tp1, tp2, tp3, engine):
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.execute('''INSERT OR REPLACE INTO active_trades
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'TRACKING', ?)''',
            (msg_id, symbol, entry, sl, tp1, tp2, tp3, engine, time.time()))

def get_active_trades():
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM active_trades WHERE status NOT IN ('COMPLETED', 'STOP_LOSS')").fetchall()

def update_trade_status(msg_id, status):
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.execute(
            "UPDATE active_trades SET status=? WHERE msg_id=?", (status, msg_id))

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
# AUDIT LOGGER (POST-TRADE OUTCOME TRACKING)
# ==========================================
def audit_log_signal(msg_id, symbol, engine, entry, sl, btc_regime):
    """Log signal dispatch dengan BTC context untuk analysis later."""
    try:
        with db_lock, sqlite3.connect(DB_NAME) as conn:
            conn.execute('''INSERT OR REPLACE INTO audit_log
                (msg_id, symbol, engine, entry, sl, exit_status,
                 btc_daily_trend, btc_4h_trend, btc_24h_change, signal_time)
                VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)''',
                (msg_id, symbol, engine, entry, sl,
                 btc_regime.get('daily_trend', 'UNKNOWN'),
                 btc_regime.get('h4_trend', 'UNKNOWN'),
                 btc_regime.get('h24_change', 0),
                 time.time()))
    except Exception as e:
        logger.warning(f"Audit log error: {e}")

def audit_update_exit(msg_id, status, exit_price):
    """Update audit dengan exit info bila SL/TP hit."""
    try:
        with db_lock, sqlite3.connect(DB_NAME) as conn:
            conn.execute('''UPDATE audit_log SET exit_status=?, exit_price=?, exit_time=?
                            WHERE msg_id=?''',
                (status, exit_price, time.time(), msg_id))
    except Exception as e:
        logger.warning(f"Audit exit update error: {e}")

def audit_track_post_sl(msg_id, symbol):
    """
    Track price 1H, 4H, 24H selepas SL hit.
    Run sebagai background task. Critical untuk diagnose:
    - Sambung turun = trend filter masalah
    - Bounce balik = SL terlalu ketat
    """
    async def _tracker():
        _ALLOWED_COLS = {'price_1h_after', 'price_4h_after', 'price_24h_after'}
        intervals = [(3600, 'price_1h_after'), (14400, 'price_4h_after'), (86400, 'price_24h_after')]
        for delay, col in intervals:
            await asyncio.sleep(delay)
            # FIX: Whitelist check — elak f-string dalam SQL walaupun nilai dalaman
            if col not in _ALLOWED_COLS:
                logger.error(f"Audit tracker: invalid column name '{col}' — skipped")
                continue
            try:
                async with aiohttp.ClientSession() as session:
                    url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        data = await resp.json()
                        price = float(data.get('price', 0))
                        with db_lock, sqlite3.connect(DB_NAME) as conn:
                            conn.execute(f"UPDATE audit_log SET {col}=? WHERE msg_id=?",
                                         (price, msg_id))
            except Exception as e:
                logger.warning(f"Audit track {col} error for {symbol}: {e}")
    
    try:
        # FIX: get_running_loop() adalah betul untuk Python 3.10+
        # get_event_loop() deprecated dan akan raise RuntimeError dalam Python 3.12+
        asyncio.get_running_loop().create_task(_tracker())
    except RuntimeError:
        # Tiada running loop (dipanggil dari thread lain) — spawn thread baru
        threading.Thread(target=lambda: asyncio.run(_tracker()), daemon=True).start()

def generate_audit_report():
    """Generate diagnostic report dari audit_log table."""
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('''SELECT * FROM audit_log 
                               WHERE exit_status IS NOT NULL 
                               AND exit_status != 'PENDING'
                               ORDER BY signal_time DESC LIMIT 50''').fetchall()
    
    if not rows:
        return ("📊 <b>AUDIT REPORT</b>\n\n"
                "<i>Tiada data audit lagi. Hantar beberapa signal dahulu, "
                "kemudian tunggu SL/TP untuk dapatkan post-trade tracking.</i>")
    
    total = len(rows)
    wins = sum(1 for r in rows if r['exit_status'] in ('TP1_HIT', 'TP2_HIT', 'COMPLETED'))
    losses = sum(1 for r in rows if r['exit_status'] == 'STOP_LOSS')
    
    # Breakdown by BTC regime
    by_regime = {}
    for r in rows:
        regime = f"{r['btc_daily_trend']}/{r['btc_4h_trend']}"
        if regime not in by_regime:
            by_regime[regime] = {'wins': 0, 'losses': 0}
        if r['exit_status'] == 'STOP_LOSS':
            by_regime[regime]['losses'] += 1
        elif r['exit_status'] in ('TP1_HIT', 'TP2_HIT', 'COMPLETED'):
            by_regime[regime]['wins'] += 1
    
    # Bounce-back analysis untuk SL trades
    sl_rows = [r for r in rows if r['exit_status'] == 'STOP_LOSS' and r['price_24h_after']]
    bounce_count = 0
    continued_down = 0
    for r in sl_rows:
        if r['price_24h_after'] and r['entry']:
            if r['price_24h_after'] >= r['entry']:
                bounce_count += 1
            elif r['price_24h_after'] < r['exit_price']:
                continued_down += 1
    
    report = (
        f"📊 <b>NOVA7 AUDIT REPORT</b>\n"
        f"<i>Last 50 closed trades</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Overall:</b> {wins}W / {losses}L "
        f"({(wins/total*100):.1f}% WR)\n\n"
        f"<b>📈 By BTC Regime:</b>\n"
    )
    for regime, stats in sorted(by_regime.items()):
        t = stats['wins'] + stats['losses']
        wr = (stats['wins'] / t * 100) if t > 0 else 0
        report += f"• {regime}: {stats['wins']}W/{stats['losses']}L ({wr:.0f}%)\n"
    
    report += f"\n<b>🔬 Post-SL Analysis (24H):</b>\n"
    if sl_rows:
        report += (f"• Sambung turun: {continued_down}/{len(sl_rows)} "
                   f"({continued_down/len(sl_rows)*100:.0f}%)\n"
                   f"• Bounce ke entry: {bounce_count}/{len(sl_rows)} "
                   f"({bounce_count/len(sl_rows)*100:.0f}%)\n")
        if continued_down / len(sl_rows) > 0.6:
            report += "\n⚠️ <b>Diagnosis:</b> Majoriti SL diikuti dengan continuation = trend filter kurang ketat\n"
        elif bounce_count / len(sl_rows) > 0.4:
            report += "\n⚠️ <b>Diagnosis:</b> Banyak bounce-back = SL terlalu ketat atau entry terlalu lewat\n"
    else:
        report += "<i>Tiada SL dengan tracking data lengkap lagi</i>\n"
    
    return report

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
        self.atr = 0.0   # ATR(14) Wilder — untuk SL adaptive
        self.k21, self.k50 = 2.0 / 22, 2.0 / 51

    def initialize(self, closes, highs, lows, volumes):
        if len(closes) < 51:
            return False
        self.closes, self.highs, self.lows, self.volumes = closes[-100:], highs[-100:], lows[-100:], volumes[-100:]
        # EMA — seed betul, iterate dari index seed
        self.ema21 = sum(closes[:21]) / 21
        for p in closes[21:]:
            self.ema21 = p * self.k21 + self.ema21 * (1 - self.k21)
        self.ema50 = sum(closes[:50]) / 50
        for p in closes[50:]:
            self.ema50 = p * self.k50 + self.ema50 * (1 - self.k50)
        # RSI — Wilder smoothing
        deltas = [closes[i] - closes[i - 1] for i in range(1, 15)]
        self.avg_gain = sum(d for d in deltas if d > 0) / 14
        self.avg_loss = sum(-d for d in deltas if d < 0) / 14
        for i in range(14, len(closes)):
            d = closes[i] - closes[i - 1]
            self.avg_gain = (self.avg_gain * 13 + (d if d > 0 else 0)) / 14
            self.avg_loss = (self.avg_loss * 13 + (-d if d < 0 else 0)) / 14
        self._update_rsi()
        self.prev_close = closes[-1]
        # ATR(14) — Wilder smoothing
        # True Range = max(H-L, |H-prevC|, |L-prevC|)
        trs = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1])
            )
            trs.append(tr)
        self.atr = sum(trs[:14]) / 14
        for tr in trs[14:]:
            self.atr = (self.atr * 13 + tr) / 14
        return True

    def _update_rsi(self):
        self.rsi = 100.0 if self.avg_loss == 0 else 100 - (100 / (1 + self.avg_gain / self.avg_loss))

    def get_rvol(self):
        if len(self.volumes) < 21:
            return 1.0
        avg = sum(self.volumes[-21:-1]) / 20
        return self.volumes[-1] / avg if avg > 0 else 1.0

    def get_rvol_series(self, n=3):
        """
        Return list RVOL untuk n candle terakhir.
        [rvol_sekarang, rvol_1_candle_lepas, rvol_2_candle_lepas, ...]
        Digunakan untuk sustainability check — elak news spike tunggal.

        FIX: Setiap candle kini menggunakan base window yang BETUL —
        20 candle SEBELUM candle tersebut (bukan base statik yg sama).
        Sebelum ini volumes[-2] dan [-3] termasuk dalam base mereka sendiri,
        menyebabkan RVOL historical kelihatan rendah palsu.
        i=0: base = volumes[-21:-1]  (20 candle sebelum candle semasa)
        i=1: base = volumes[-22:-2]  (20 candle sebelum candle -2)
        i=2: base = volumes[-23:-3]  (20 candle sebelum candle -3)
        """
        if len(self.volumes) < 22:
            return [1.0] * n
        result = []
        for i in range(n):
            idx = -(1 + i)          # candle yg dinilai: -1, -2, -3 ...
            base_start = -(21 + i)  # 20 candle sebelumnya (tidak termasuk candle idx)
            base_end   = -(1 + i)   # exclusive end: -1, -2, -3 ...
            base_vols  = self.volumes[base_start:base_end]
            if len(base_vols) < 10:
                result.append(1.0)
                continue
            avg_base = sum(base_vols) / len(base_vols)
            if avg_base <= 0:
                result.append(1.0)
                continue
            if abs(idx) <= len(self.volumes):
                result.append(self.volumes[idx] / avg_base)
            else:
                result.append(1.0)
        return result

    def get_atr_sl(self, structure_low, multiplier=1.5):
        """
        ATR-based Stop Loss.
        SL = structure_low - (multiplier × ATR14)
        
        Mengapa 1.5x ATR?
        - ATR(14) adalah purata volatility harian normal
        - 1.5x memberikan ruang untuk wick normal tanpa kena sweep
        - Kurang dari 1x = terlalu ketat (dalam wick biasa)
        - Lebih dari 2x = SL terlalu jauh, RR ratio memburuk
        
        Position size dikira semula secara automatic oleh
        calculate_position_size() — risk_usd kekal sama,
        hanya bilangan unit yang berubah.
        """
        if self.atr <= 0:
            return structure_low * 0.995  # fallback ke fixed % jika ATR tiada
        return structure_low - (multiplier * self.atr)

    def get_bb_width(self):
        if len(self.closes) < 20:
            return 10.0
        recent = self.closes[-20:]
        n = len(recent)
        sma = sum(recent) / n
        # Sample std (÷N-1) — konsisten dengan TradingView dan Bloomberg standard.
        # Population std (÷N) akan bagi nilai ~2.6% lebih kecil dari TradingView.
        # Bollinger Band Width = (Upper - Lower) / Middle × 100 = (4σ / SMA) × 100
        variance = sum((p - sma) ** 2 for p in recent) / (n - 1)
        std = variance ** 0.5
        return (4 * std / sma) * 100 if sma > 0 else 10.0

    def get_recent_high(self):
        return max(self.highs[-21:-1]) if len(self.highs) >= 21 else 0

# ==========================================
# ENGINES (DYNAMIC TUNING)
# ==========================================
class BreakoutHunter:
    def check(self, ind, t):
        if len(ind.closes) < 51:
            return None, {"Data Sejarah": "Kurang 51 candle (Gagal)"}
        close = ind.closes[-1]
        rvol_series = ind.get_rvol_series(3)   # [now, -1, -2]
        rvol = rvol_series[0]
        recent_high = ind.get_recent_high()
        rsi_min  = t.get('bo_rsi_min', 50)
        rsi_max  = t.get('bo_rsi_max', 75)
        rvol_min = t.get('bo_rvol', 1.8)

        # RVOL sustainability: candle semasa mesti ≥ threshold,
        # DAN sekurang-kurangnya SATU dari 2 candle sebelumnya ≥ 60% threshold.
        # Ini reject news spike tunggal, tapi lulus genuine momentum.
        rvol_sustained = rvol >= rvol_min and (
            rvol_series[1] >= rvol_min * 0.6 or
            rvol_series[2] >= rvol_min * 0.6
        )

        conditions = {
            f"Pecah High 20-C ({recent_high:.6f})": close > recent_high,
            f"Atas EMA21 ({ind.ema21:.6f})": close > ind.ema21,
            "Uptrend (EMA21 > EMA50)": ind.ema21 > ind.ema50,
            f"RVOL Sustained >= {rvol_min}x [{rvol:.2f}x, prev:{rvol_series[1]:.2f}x]": rvol_sustained,
            f"RSI {rsi_min}-{rsi_max} [{ind.rsi:.1f}]": rsi_min < ind.rsi < rsi_max
        }
        if all(conditions.values()):
            structure_low = min(ind.lows[-20:])
            # ATR-based SL — adaptive kepada volatility coin
            atr_sl = ind.get_atr_sl(structure_low, multiplier=1.5)
            sig = {
                'type': 'BREAKOUT', 'rvol': rvol,
                'break_level': recent_high,
                'low': structure_low,
                'atr_sl': atr_sl,
                'atr': ind.atr
            }
            return sig, conditions
        return None, conditions

class AccumulationDetective:
    def check(self, ind, t):
        if len(ind.closes) < 51:
            return None, {}
        close = ind.closes[-1]
        bb    = ind.get_bb_width()
        rvol_series = ind.get_rvol_series(3)
        rvol  = rvol_series[0]
        bb_max   = t.get('acc_bb_width', 24.0)
        rvol_min = t.get('acc_rvol', 2.0)
        rsi_max  = t.get('acc_rsi_max', 45)

        # Sustainability: Accumulation perlu volume sustain (bukan news spike)
        rvol_sustained = rvol >= rvol_min and (
            rvol_series[1] >= rvol_min * 0.6 or
            rvol_series[2] >= rvol_min * 0.6
        )

        conditions = {
            f"BB Width < {bb_max}% [{bb:.2f}%]": bb < bb_max,
            f"RVOL Sustained >= {rvol_min}x [{rvol:.2f}x, prev:{rvol_series[1]:.2f}x]": rvol_sustained,
            "Bawah EMA50 (Accum Zone)": close < ind.ema50,
            f"RSI < {rsi_max} [{ind.rsi:.1f}]": ind.rsi < rsi_max
        }
        if all(conditions.values()):
            structure_low = min(ind.lows[-20:])
            atr_sl = ind.get_atr_sl(structure_low, multiplier=1.5)
            sig = {
                'type': 'ACCUMULATION', 'rvol': rvol, 'bb': bb,
                'low': structure_low,
                'atr_sl': atr_sl,
                'atr': ind.atr
            }
            return sig, conditions
        return None, conditions

# ==========================================
# AUTO-CHART (VISUAL PROOF) — definisi tunggal di bawah dalam seksyen PREMIUM FEATURE 1
# ==========================================

# ==========================================
# BTC MARKET REGIME GATE (3-LAYER)
# ==========================================
def _compute_ema_series(closes, period):
    """Helper: kira EMA dengan cara betul (seed SMA, kemudian iterative)."""
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for p in closes[period:]:
        ema = p * k + ema * (1 - k)
    return ema

async def _fetch_json_async(session, url):
    """Helper: fetch JSON dengan aiohttp, consistent timeout."""
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        return await resp.json()

async def get_btc_regime_async():
    """
    Async version — tidak block event loop.
    
    Prinsip keselamatan: Bila API error, hard_block=True (fail-SAFE).
    Dalam trading, bila data tidak pasti, lebih baik tidak trade.
    
    Mathematical basis:
    - EMA21 < EMA50 pada Daily = intermediate downtrend (industry standard)
    - 2% margin mengurangkan whipsaw bila EMA bersilang rapat
    - -3% 24H catch risk-off events yang terlalu pantas untuk EMA
    """
    _SAFE_BLOCK = {'error': None, 'hard_block': True, 'soft_tighten': False,
                   'daily_trend': 'UNKNOWN', 'h4_trend': 'UNKNOWN', 'h24_change': 0,
                   'block_reason': 'BTC data unavailable (fail-safe)'}
    try:
        async with aiohttp.ClientSession() as session:
            url_d = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=60"
            url_4h = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=4h&limit=60"
            # Fetch kedua-dua serentak — lebih cepat dari sequential
            res_d, res_4h = await asyncio.gather(
                _fetch_json_async(session, url_d),
                _fetch_json_async(session, url_4h)
            )

        if not isinstance(res_d, list) or len(res_d) < 51:
            logger.warning("[BTC GATE] Daily data insufficient — fail-safe block")
            return {**_SAFE_BLOCK, 'block_reason': 'BTC daily data insufficient'}
        if not isinstance(res_4h, list) or len(res_4h) < 51:
            logger.warning("[BTC GATE] 4H data insufficient — fail-safe block")
            return {**_SAFE_BLOCK, 'block_reason': 'BTC 4H data insufficient'}

        closes_d = [float(d[4]) for d in res_d]
        closes_4h = [float(d[4]) for d in res_4h]

        ema21_d = _compute_ema_series(closes_d, 21)
        ema50_d = _compute_ema_series(closes_d, 50)
        ema21_4h = _compute_ema_series(closes_4h, 21)
        ema50_4h = _compute_ema_series(closes_4h, 50)

        daily_bullish = ema21_d > ema50_d * 0.98
        h4_bullish = ema21_4h > ema50_4h
        h24_change = ((closes_d[-1] - closes_d[-2]) / closes_d[-2]) * 100

        hard_block = (not daily_bullish) or (h24_change < -3.0)
        soft_tighten = (not h4_bullish) and daily_bullish and (h24_change >= -3.0)

        return {
            'daily_trend': 'BULLISH' if daily_bullish else 'BEARISH',
            'h4_trend': 'BULLISH' if h4_bullish else 'BEARISH',
            'h24_change': round(h24_change, 2),
            'ema21_d': ema21_d, 'ema50_d': ema50_d,
            'hard_block': hard_block,
            'soft_tighten': soft_tighten,
            'block_reason': (
                'Daily BEARISH (EMA21<EMA50)' if not daily_bullish else
                f'BTC 24H crash ({h24_change:.1f}%)' if h24_change < -3.0 else None
            ),
            'error': None
        }
    except Exception as e:
        logger.warning(f"[BTC GATE] API error: {e} — fail-safe block aktif")
        return {**_SAFE_BLOCK, 'block_reason': f'API error: {str(e)[:50]}'}

# Cache BTC regime — async-safe dengan asyncio.Lock
_btc_regime_cache = {'data': None, 'ts': 0}
_btc_regime_alock = None  # init dalam async context (tidak boleh buat asyncio.Lock() di module level)

async def get_btc_regime_cached(ttl=60):
    """
    Async-safe cached BTC regime.
    asyncio.Lock mesti dibuat dalam running event loop — ini adalah cara betul.
    """
    global _btc_regime_alock
    if _btc_regime_alock is None:
        _btc_regime_alock = asyncio.Lock()
    async with _btc_regime_alock:
        now = time.time()
        if _btc_regime_cache['data'] is None or (now - _btc_regime_cache['ts']) > ttl:
            _btc_regime_cache['data'] = await get_btc_regime_async()
            _btc_regime_cache['ts'] = now
        return _btc_regime_cache['data']

# ==========================================
# DAILY CONFLUENCE (TRAP KILLER) — DIPERBAIKI DENGAN REVERSAL CONFIRMATION
# ==========================================
async def check_daily_confluence(symbol, current_price):
    """
    Async — tidak block event loop.
    Validate symbol confluence pada Daily timeframe.
    Guna _compute_ema_series dan _fetch_json_async helpers untuk konsisten.
    """
    def _local_rsi(close_series, period=14):
        if len(close_series) < period + 1:
            return 50.0
        gains, losses = [], []
        for i in range(1, len(close_series)):
            d = close_series[i] - close_series[i-1]
            gains.append(d if d > 0 else 0)
            losses.append(-d if d < 0 else 0)
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period-1) + gains[i]) / period
            avg_loss = (avg_loss * (period-1) + losses[i]) / period
        if avg_loss == 0:
            return 100.0
        return 100 - (100 / (1 + avg_gain / avg_loss))

    try:
        async with aiohttp.ClientSession() as session:
            url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1d&limit=60"
            res = await _fetch_json_async(session, url)

        if not isinstance(res, list) or len(res) < 50:
            return False, "Data Daily tidak cukup"

        closes  = [float(d[4]) for d in res]
        opens   = [float(d[1]) for d in res]
        lows    = [float(d[3]) for d in res]
        volumes = [float(d[5]) for d in res]

        ema50_d = _compute_ema_series(closes, 50)

        # PATH A: Uptrend — above Daily EMA50
        if current_price > ema50_d:
            return True, f"Above Daily EMA50 ({ema50_d:.6f})"

        # PATH B: 3-factor Reversal Confirmation
        low_20d = min(lows[-20:])
        if current_price >= low_20d * 1.03:
            return False, f"Below Daily EMA50 ({ema50_d:.6f}) & not at support"

        last_bullish = closes[-1] > opens[-1]
        rsi_now  = _local_rsi(closes)
        rsi_3ago = _local_rsi(closes[:-3])
        bullish_divergence = (rsi_now - rsi_3ago) >= 5.0
        avg_vol_20 = sum(volumes[-21:-1]) / 20 if len(volumes) >= 21 else 1
        rvol = volumes[-1] / avg_vol_20 if avg_vol_20 > 0 else 1
        volume_confirm = rvol >= 1.5

        if last_bullish and bullish_divergence and volume_confirm:
            return True, (f"Reversal Confirmed @ {low_20d:.6f} "
                          f"[RSI Δ+{rsi_now - rsi_3ago:.1f}, RVOL {rvol:.2f}x, Bull Candle]")

        failed = []
        if not last_bullish: failed.append("no bull candle")
        if not bullish_divergence: failed.append(f"no RSI div (Δ{rsi_now - rsi_3ago:+.1f})")
        if not volume_confirm: failed.append(f"low vol ({rvol:.2f}x)")
        return False, f"At support but {', '.join(failed)}"

    except Exception as e:
        return False, f"Error: {str(e)[:50]}"

# ==========================================
# POSITION SIZING (FUND MANAGER) & AI INSIGHT ENGINE (ZERO COST)
# ==========================================
def generate_ai_insight(symbol):
    """
    Logic Engine: Membaca indikator dan menulis ringkasan 'bahasa manusia'.
    Dipanggil dari sync bot handler — guna requests adalah betul di sini
    kerana bot handler berjalan dalam thread berasingan dari asyncio loop.
    FIX: Guna EMA betul via _compute_ema_series, bukan SMA dilabel EMA.
    """
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit=100"
        res = requests.get(url, timeout=10).json()
        closes = [float(d[4]) for d in res]
        volumes = [float(d[7]) for d in res]
        if len(closes) < 51:
            return "❌ Data tidak mencukupi untuk analisis."

        close_now = closes[-1]
        # FIX: Guna EMA betul (iterative), bukan SMA yang dilabel sebagai EMA
        ema21 = _compute_ema_series(closes, 21)
        ema50 = _compute_ema_series(closes, 50)

        # Kira RVOL
        avg_vol = sum(volumes[-21:-1]) / 20 if len(volumes) > 21 else 1
        rvol = volumes[-1] / avg_vol if avg_vol > 0 else 1

        # Logik Analisis
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
    if risk_distance <= 0: 
        return 0, 0, 0
    position_usd = risk_usd / (risk_distance / entry)
    position_coins = position_usd / entry
    return position_usd, position_coins, risk_usd

# ==========================================
# TELEGRAM UI
# ==========================================
def build_keyboard(symbol):
    """Keyboard Premium 2x2 Grid dengan AI Insight."""
    base = symbol[:-4]
    markup = InlineKeyboardMarkup(row_width=2)
    # Baris 1: Chart & Trade
    markup.row(
        InlineKeyboardButton("📈 TradingView", url=f"https://www.tradingview.com/chart/?symbol=BINANCE:{symbol}"),
        InlineKeyboardButton("⚡ Binance", url=f"https://www.binance.com/en/trade/{symbol}")
    )

    # Baris 2: Social & AI
    markup.row(
        InlineKeyboardButton("🐦 Twitter Live", url=f"https://x.com/search?q=%24{base}&f=live"),
        InlineKeyboardButton("✨ AI Insight", callback_data=f"ai_summary_{symbol}")
    )

    return markup

def dispatch_signal(symbol, price, sig, ind, engine_type, chart_buf, daily_note, user_cap, user_risk, sl=None):
    if not bot or not TELEGRAM_CHAT_ID or check_cooldown(symbol):
        return None
    # Guna ATR-based SL yang dikira oleh engine, jika tiada fallback ke fixed
    if sl is None:
        structure_low = sig.get('low', price * 0.97)
        sl = sig.get('atr_sl', structure_low * 0.995)
    risk = price - sl
    if risk <= 0:
        return None

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
    atr_val = sig.get('atr', 0)
    atr_pct = (atr_val / price * 100) if price > 0 and atr_val > 0 else 0

    msg = (
        f"{emoji} <b>{title}: {symbol}</b> <i>[{mode_name}]</i>\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"💵 <b>Price:</b> <code>${price:.6f}</code>\n"
        f"{desc}\n"
        f"🔥 <b>RVOL:</b> {sig['rvol']:.2f}x | <b>RSI:</b> {ind.rsi:.1f}\n"
        f"📊 <b>EMA21:</b> ${ind.ema21:.6f} | <b>EMA50:</b> ${ind.ema50:.6f}\n"
        f"🗓️ <b>Daily TF:</b> <i>{daily_note}</i>\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"🛑 <b>SL (ATR×1.5):</b> <code>${sl:.6f}</code>"
        + (f" <i>[ATR {atr_pct:.2f}%]</i>" if atr_pct > 0 else "") + "\n"
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
            sent = bot.send_photo(TELEGRAM_CHAT_ID, chart_buf, caption=msg, parse_mode="HTML", reply_markup=build_keyboard(symbol))
        else:
            sent = bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="HTML", reply_markup=build_keyboard(symbol), disable_web_page_preview=True)

        save_trade(sent.message_id, symbol, price, sl, tp1, tp2, tp3, engine_type)
        save_cooldown(symbol, t.get('cd_breakout', 24) if engine_type == 'BREAKOUT' else t.get('cd_accumulation', 48))
        logger.info(f"✅ [SIGNAL] {symbol} ({engine_type}) dispatched.")
        return sent.message_id
    except Exception as e:
        logger.error(f"Dispatch error: {e}")
        return None

# ==========================================
# POST-MORTEM AUTOPSY
# ==========================================
async def spot_post_mortem(symbol):
    """Async — tidak block trade_tracker event loop."""
    try:
        async with aiohttp.ClientSession() as session:
            url_sym = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit=24"
            url_btc = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=24"
            res_sym, res_btc = await asyncio.gather(
                _fetch_json_async(session, url_sym),
                _fetch_json_async(session, url_btc)
            )
        vols = [float(d[7]) for d in res_sym]
        avg_vol = sum(vols[:20]) / 20 if len(vols) >= 20 else 1
        rvol_now = vols[-1] / avg_vol if avg_vol > 0 else 1
        btc_start = float(res_btc[0][1])
        btc_end   = float(res_btc[-1][4])
        btc_change = ((btc_end - btc_start) / btc_start) * 100
        closes = [float(d[4]) for d in res_sym]
        # FIX: Guna EMA betul, bukan SMA
        ema21 = _compute_ema_series(closes, 21) if len(closes) >= 21 else closes[-1]
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
# SOCIAL SENTIMENT ANALYZER (TWITTER)
# ==========================================
def check_social_sentiment(symbol, base_name=None):
    """
    Check news sentiment & volume untuk symbol menggunakan CryptoPanic public API.
    Tiada API key required untuk free tier. Menggantikan Twitter scraper yang
    tidak berfungsi sejak X memerlukan auth (2023+).
    """
    try:
        base = base_name if base_name else symbol[:-4]
        headers = {
            'User-Agent': 'Mozilla/5.0 (Nova7-Bot/1.0)'
        }
        # CryptoPanic public endpoint — free, no key needed for basic queries
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

        # CryptoPanic provides vote signals per post (positive/negative/important/etc)
        pos_count = 0
        neg_count = 0
        for post in posts:
            votes = post.get('votes', {}) or {}
            pos_count += int(votes.get('positive', 0) or 0)
            pos_count += int(votes.get('important', 0) or 0)
            neg_count += int(votes.get('negative', 0) or 0)
            neg_count += int(votes.get('toxic', 0) or 0)

            # Fallback: scan title for sentiment keywords if votes are absent
            title = (post.get('title', '') or '').lower()
            positive_keywords = ['surge', 'rally', 'bullish', 'soar', 'breakout', 'gain', 'rocket', 'pump']
            negative_keywords = ['crash', 'dump', 'bearish', 'plunge', 'fall', 'loss', 'hack', 'exploit']
            pos_count += sum(1 for kw in positive_keywords if kw in title)
            neg_count += sum(1 for kw in negative_keywords if kw in title)

        mention_count = len(posts)

        # Kira sentiment score
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

        # Volume classification berdasarkan bilangan news posts
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
stats_lock = threading.Lock()  # FIX: lock untuk thread-safe stats updates
queue_lock = threading.Lock()  # FIX: lock untuk layer2_queue (atomic check+add)
activity_log = []  # For pulse display

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
    """Simpan activity log untuk dipaparkan dalam pulse.
    FIX: Deduplication — jika coin yang sama sudah ada dalam log dengan
    status failure (❌/🚫), gantikan entry lama supaya log tidak penuh
    dengan coin yang sama berulang-ulang dari batch accumulation scan.
    """
    symbol_prefix = msg.split()[0] if msg else ''
    is_failure = '❌' in msg or '🚫' in msg
    if is_failure and symbol_prefix:
        # Buang entry lama untuk coin yang sama (failure sahaja)
        for i in range(len(activity_log) - 1, -1, -1):
            entry = activity_log[i]
            if entry.startswith(symbol_prefix) and ('❌' in entry or '🚫' in entry):
                activity_log.pop(i)
                break
    activity_log.append(msg)
    if len(activity_log) > 20:
        activity_log.pop(0)
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
                    if not is_scanning: 
                        break
                    msg = await ws.recv()
                    now = time.time()

                    # ACTIVITY PULSE setiap 5 minit
                    if now - last_pulse >= 300:
                        snap = get_stats_snapshot()  # FIX: thread-safe read
                        # FIX Bug 2: Kira delta (bukan kumulatif) supaya konsisten
                        # dengan pulse_stats['promoted'] yang sudah reset tiap 5 min.
                        delta_signals  = snap['signals_sent'] - pulse_stats.get('prev_signals', 0)
                        delta_rejected = snap['rejected']      - pulse_stats.get('prev_rejected', 0)
                        logger.info(f"💓 [PULSE] Radar: {pulse_stats['seen']} coins | Promoted: {pulse_stats['promoted']} | Signals: {delta_signals} | Rejected: {delta_rejected}")
                        if activity_log:
                            logger.info(f"📋 [RECENT] {' | '.join(activity_log[-5:])}")
                        last_pulse = now
                        pulse_stats = {
                            'promoted': 0, 'seen': 0,
                            'prev_signals':  snap['signals_sent'],
                            'prev_rejected': snap['rejected']
                        }

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

                        # FIX: atomic check+add untuk elak race condition
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
                            # FIX: atomic check+add untuk Accumulation queue juga
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
# PREMIUM FEATURE 1: AUTO-CHART (VISUAL PROOF)
# ==========================================
def generate_chart_image(symbol, closes, highs, lows, volumes, ema21, ema50, sl, tp1, tp2, tp3):
    try:
        n = min(60, len(closes))
        # FIX BUG PANDAS 2.2: Tukar 'H' kepada '1h'
        df = pd.DataFrame({
            'Open': [closes[i - 1] if i > 0 else closes[i] for i in range(len(closes) - n, len(closes))],
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
# LAYER 2 SNIPER (DENGAN AUTO-CHART FORCE)
# ==========================================
async def layer2_sniper(symbol, scan_type, force=False, chat_id=None, user_cap=1000.0, user_risk=2.0):
    try:
        t = get_tuning()
        if not force and check_cooldown(symbol): 
            return
        
        # ═══════════════════════════════════════════════════════
        # BTC MARKET REGIME GATE (LAYER 0 — runs before anything else)
        # ═══════════════════════════════════════════════════════
        btc_regime = await get_btc_regime_cached()
        if not force:
            if btc_regime.get('hard_block'):
                reason = btc_regime.get('block_reason', 'BTC regime risk')
                log_activity(f"{symbol} 🚫 BTC GATE: {reason}")
                bump_stat('rejected')
                if chat_id and bot:
                    bot.send_message(chat_id, 
                        f"🚫 <b>{symbol}</b> blocked: {reason}\n"
                        f"<i>BTC Daily: {btc_regime.get('daily_trend')} | "
                        f"24H: {btc_regime.get('h24_change')}%</i>",
                        parse_mode="HTML")
                return
            
            # SOFT TIGHTEN: clone tuning dan tighten parameters
            if btc_regime.get('soft_tighten'):
                t = dict(t)  # shallow copy
                t['bo_rvol'] = t.get('bo_rvol', 1.8) + 0.3
                t['bo_rsi_min'] = max(t.get('bo_rsi_min', 50), 55)
                t['bo_rsi_max'] = min(t.get('bo_rsi_max', 75), 70)
                t['acc_rvol'] = t.get('acc_rvol', 2.0) + 0.3
                log_activity(f"{symbol} ⚠️ BTC 4H bearish — tightening params")
        
        async with aiohttp.ClientSession() as session:
            url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit=100"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                closes = [float(d[4]) for d in data]
                highs = [float(d[2]) for d in data]
                lows = [float(d[3]) for d in data]
                volumes = [float(d[7]) for d in data]

                ind = IncrementalIndicators()
                if not ind.initialize(closes, highs, lows, volumes): 
                    return

                if scan_type == 'BREAKOUT':
                    sig, conditions = BreakoutHunter().check(ind, t)
                else:
                    sig, conditions = AccumulationDetective().check(ind, t)

                if sig:
                    # Guna ATR-based SL dari engine — adaptive kepada volatility
                    sl = sig.get('atr_sl', sig['low'] * 0.995)
                    risk = closes[-1] - sl
                    tp1 = closes[-1] + (risk * 2.0)
                    tp2 = closes[-1] + (risk * 3.5)
                    tp3 = closes[-1] + (risk * 5.5)
                else:
                    sl = closes[-1] * 0.95
                    tp1 = closes[-1] * 1.05
                    tp2 = closes[-1] * 1.10
                    tp3 = closes[-1] * 1.15

                # WAJIB JANA CARTA JIKA VALID ATA U DI-FORCE
                chart_buf = None
                if sig or force:
                    chart_buf = generate_chart_image(symbol, closes, highs, lows, volumes, ind.ema21, ind.ema50, sl, tp1, tp2, tp3)

                if sig:
                    daily_filter_on = t.get('bo_daily_filter', 1) == 1
                    daily_ok, daily_note = True, "Filter OFF"
                    if daily_filter_on:
                        daily_ok, daily_note = await check_daily_confluence(symbol, closes[-1])

                    if not daily_ok and not force:
                        log_activity(f"{symbol} ❌ Daily Filter: {daily_note}")
                        bump_stat('rejected')
                        if chat_id and bot:
                            bot.send_message(chat_id, f"🚫 <b>{symbol}</b> ditolak: {daily_note}", parse_mode="HTML")
                        return

                    log_activity(f"{symbol} ✅ VALID ({scan_type}) → Dispatching")
                    msg_id = dispatch_signal(symbol, closes[-1], sig, ind, scan_type, chart_buf, daily_note, user_cap, user_risk, sl=sl)
                    # Audit log
                    if msg_id:
                        audit_log_signal(msg_id, symbol, scan_type, closes[-1], sl, btc_regime)
                    bump_stat('signals_sent')
                else:
                    # Log kenapa gagal (ringkas)
                    failed = [k for k, v in conditions.items() if not v]
                    if failed:
                        main_reason = failed[0].split('[')[0].strip()[:40]
                        log_activity(f"{symbol} ❌ {main_reason}")
                        bump_stat('rejected')

                    if chat_id and bot and conditions:
                        report = f"🔍 <b>Diagnostic {symbol}</b>\n❌ Setup TIDAK VALID.\n\n"
                        for condition, passed in conditions.items():
                            report += f"{'✅' if passed else '❌'} {condition}\n"
                        bot.send_message(chat_id, report, parse_mode="HTML")

                bump_stat('layer2_scans')
    except Exception as e:
        logger.error(f"Sniper error {symbol}: {e}")
    finally:
        with queue_lock:
            layer2_queue.discard(symbol)

# ==========================================
# TRADE TRACKER
# ==========================================
async def trade_tracker():
    while True:
        await asyncio.sleep(5)
        trades = get_active_trades()
        for t in trades:
            sym = t['symbol']
            if sym not in latest_prices: 
                continue
            price = latest_prices[sym]['c']  # FIX: latest_p rices -> latest_prices
            status, reply, new_status = t['status'], None, t['status']
            if price <= t['sl'] and status not in ['STOP_LOSS', 'COMPLETED']:
                autopsy = await spot_post_mortem(sym)
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
                    # AUDIT: update exit info + trigger post-SL tracking
                    audit_update_exit(t['msg_id'], new_status, price)
                    if new_status == 'STOP_LOSS':
                        audit_track_post_sl(t['msg_id'], sym)
                except Exception as e:
                    logger.warning(f"Trade tracker notify error for {sym}: {e}")

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
            f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
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
        "/report — Weekly tear sheet\n"
        "/audit — Diagnostic report\n"
        "/regime — BTC market regime\n"
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
            f"• BB Width max: <code>{t.get('acc_bb_width', 24.0):.1f}%</code>\n"
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
            'acc_bb_width': 40.0, 'acc_rvol': 1.6, 'acc_rsi_max': 55,  # FIX: 10.0 → 40.0 (standard BB)
            'radar_momentum': 1.8, 'radar_min_vol': 8_000_000,
            'cd_breakout': 12, 'cd_accumulation': 24
        })
        bot.reply_to(msg, "🟢 <b>Mode LONGGAR aktif.</b>\nLebih banyak signal, sesuai untuk Bull Market.", parse_mode="HTML")
    elif args[0] == 'ketat':
        set_tuning({
            'mode': 2,
            'bo_rvol': 2.2, 'bo_rsi_min': 55, 'bo_rsi_max': 70, 'bo_daily_filter': 1,
            'acc_bb_width': 16.0, 'acc_rvol': 2.5, 'acc_rsi_max': 40,  # FIX: 4.0 → 16.0 (standard BB)
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
    s = get_stats_snapshot()  # FIX: thread-safe snapshot
    modes = {0: 'STANDARD', 1: 'LONGGAR', 2: 'KETAT'}
    text = (
        f"📊 <b>NOVA7 STATUS [{status}]</b>\n"
        f"🎛️ <b>Mode:</b> {modes.get(int(t.get('mode', 0)), 'UNKNOWN')}\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"🐋 Radar: {s['radar_coins']} coins\n"
        f"🎯 L2 Scans: {s['layer2_scans']}\n"
        f"📈 Signals: {s['signals_sent']}\n"
        f"❌ Rejected: {s['rejected']}\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    )
    bot.reply_to(msg, text, parse_mode="HTML")

@bot.message_handler(commands=['report'])
def cmd_report(msg):
    bot.reply_to(msg, "⏳ <i>Menjana Tear Sheet...</i>", parse_mode="HTML")
    bot.reply_to(msg, generate_tear_sheet(), parse_mode="HTML")

@bot.message_handler(commands=['audit'])
def cmd_audit(msg):
    """Generate audit report dengan post-trade analysis."""
    bot.reply_to(msg, "⏳ <i>Menjana Audit Report...</i>", parse_mode="HTML")
    bot.reply_to(msg, generate_audit_report(), parse_mode="HTML")

@bot.message_handler(commands=['regime'])
def cmd_regime(msg):
    """Show current BTC market regime."""
    import concurrent.futures
    def _get():
        return asyncio.run(get_btc_regime_cached(ttl=30))
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        r = ex.submit(_get).result(timeout=15)
    if r.get('error') and r.get('hard_block') and r.get('daily_trend') == 'UNKNOWN':
        bot.reply_to(msg, f"❌ BTC data error: {r.get('block_reason', 'unknown')}", parse_mode="HTML")
        return
    status_emoji = "🟢" if not r['hard_block'] and not r['soft_tighten'] else (
        "🔴" if r['hard_block'] else "🟡")
    text = (
        f"{status_emoji} <b>BTC MARKET REGIME</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 <b>Daily:</b> {r['daily_trend']}\n"
        f"⏰ <b>4H:</b> {r['h4_trend']}\n"
        f"📊 <b>24H Change:</b> {r['h24_change']}%\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Signal Gate:</b> "
        f"{'🚫 HARD BLOCK' if r['hard_block'] else ('⚠️ TIGHTENED' if r['soft_tighten'] else '✅ OPEN')}\n"
    )
    if r['hard_block']:
        text += f"<i>Reason: {r.get('block_reason', 'N/A')}</i>\n"
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
    threading.Thread(target=lambda: asyncio.run(layer2_sniper(sym, 'BREAKOUT', force=True, chat_id=msg.chat.id, user_cap=user_cap, user_risk=user_risk)), daemon=True).start()

# ==========================================
# AI INSIGHT CALLBACK HANDLER
# ==========================================
@bot.callback_query_handler(func=lambda call: call.data.startswith("ai_summary_"))
def handle_ai_insight(call):
    """Handle AI Insight button click — panggil engine sebenar, bukan template statik."""
    symbol = call.data.split("_")[-1]
    bot.answer_callback_query(call.id, "🤖 Menjana analisis...", show_alert=False)

    # FIX: Panggil generate_ai_insight() sebenar (yg sudah ditakrif di atas) untuk
    # market analysis berasaskan indikator live, bukan template statik.
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
# FLASK & SHUTDOWN
# ==========================================
app = Flask(__name__)  # FIX: Flask(name) -> Flask(__name__)

@app.route('/')
def home(): 
    return "Enjin Nova7 Premium Aktif 🐋"

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        bot.process_new_updates([telebot.types.Update.de_json(request.get_json())])
        return 'ok', 200

def graceful_shutdown(*args):
    if TELEGRAM_TOKEN and ADMIN_CHAT_ID:
        try: 
            bot.send_message(ADMIN_CHAT_ID, "🔴 <b>[OFFLINE] NOVA7 DISCONNECTED.</b>", parse_mode="HTML")
        except Exception:
            pass
    sys.exit(0)

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))

# ==========================================
# MAIN ORCHESTRATOR
# ==========================================
if __name__ == "__main__":  # FIX: if name == "main" -> if __name__ == "__main__"
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)
    init_db()
    RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", " ").rstrip("/")
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
        threading.Thread(target=run_flask, daemon=True).start()  # FIX: threa d -> thread
        try: 
            asyncio.run(layer1_radar())
        except KeyboardInterrupt: 
            graceful_shutdown()
    else:
        logger.info("[ENV] Localhost. Polling Mode...")
        threading.Thread(target=lambda: asyncio.run(trade_tracker()), daemon=True).start()
        threading.Thread(target=tear_sheet_scheduler, daemon=True).start()
        threading.Thread(target=lambda: asyncio.run(layer1_radar()), daemon=True).start()  # FIX: threa d -> thread
        if bot:
            try: 
                bot.infinity_polling()
            except KeyboardInterrupt: 
                graceful_shutdown()