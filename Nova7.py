from datetime import datetime, timezone, timedelta
from flask import Flask, request
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import os
import time
import json
import sqlite3
import asyncio
import threading
import logging
import sys
import signal
import gc
import io
import requests
import aiohttp
import websockets
import telebot

# ==========================================
# SUPABASE — Persistent Storage (kekalkan data walaupun restart)
# ==========================================
# Dapatkan URL dan KEY dari: https://supabase.com → Project Settings → API
# Letak dalam Render Environment Variables:
#   SUPABASE_URL  = https://xxxx.supabase.co
#   SUPABASE_KEY  = eyJhbGci...  (anon/public key)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
_supa_enabled = bool(SUPABASE_URL and SUPABASE_KEY)

def _supa_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates"
    }

def supa_upsert(table, data):
    """Tulis/update satu row ke Supabase. Fail-silent jika tidak configured."""
    if not _supa_enabled:
        return
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/{table}",
            headers={**_supa_headers(), "Prefer": "resolution=merge-duplicates"},
            json=data, timeout=5)
    except Exception as e:
        logger.warning(f"[SUPA] upsert {table}: {e}")

def supa_update(table, match_col, match_val, data):
    """Update row yang match dalam Supabase."""
    if not _supa_enabled:
        return
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/{table}?{match_col}=eq.{match_val}",
            headers=_supa_headers(),
            json=data, timeout=5)
    except Exception as e:
        logger.warning(f"[SUPA] update {table}: {e}")

def supa_fetch(table, filters="", order="timestamp.desc"):
    """Fetch rows dari Supabase. Return list atau []."""
    if not _supa_enabled:
        return []
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}?order={order}"
        if filters:
            url += f"&{filters}"
        r = requests.get(url, headers=_supa_headers(), timeout=8)
        return r.json() if r.ok else []
    except Exception as e:
        logger.warning(f"[SUPA] fetch {table}: {e}")
        return []

def supa_init_tables():
    """
    Cipta tables dalam Supabase jika belum wujud.
    Jalankan SQL ini sekali dalam Supabase SQL Editor:

    -- active_trades
    CREATE TABLE IF NOT EXISTS active_trades (
        msg_id BIGINT PRIMARY KEY,
        symbol TEXT, entry FLOAT, sl FLOAT,
        tp1 FLOAT, tp2 FLOAT, tp3 FLOAT,
        engine TEXT, status TEXT,
        timestamp FLOAT, macro_btc_pct FLOAT DEFAULT 0,
        exit_price FLOAT DEFAULT 0, exit_time FLOAT DEFAULT 0
    );
    -- cooldowns
    CREATE TABLE IF NOT EXISTS cooldowns (
        symbol TEXT PRIMARY KEY, last_signal FLOAT
    );
    -- user_profiles
    CREATE TABLE IF NOT EXISTS user_profiles (
        user_id BIGINT PRIMARY KEY,
        capital FLOAT, risk_pct FLOAT, updated FLOAT
    );
    -- tuning_params
    CREATE TABLE IF NOT EXISTS tuning_params (
        key TEXT PRIMARY KEY, value FLOAT
    );
    """
    # Test connection
    if not _supa_enabled:
        logger.warning("[SUPA] Supabase tidak dikonfigurasi — guna SQLite sahaja")
        return
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/active_trades?limit=1",
            headers=_supa_headers(), timeout=8)
        if r.ok:
            logger.info("✅ [SUPA] Supabase connected — persistent storage aktif")
        else:
            logger.warning(f"[SUPA] Connection test gagal: {r.status_code} — "
                           "Pastikan tables sudah dicipta dalam Supabase SQL Editor")
    except Exception as e:
        logger.warning(f"[SUPA] Connection error: {e}")

def supa_restore_on_startup():
    """
    Pulihkan active_trades dari Supabase ke SQLite tempatan selepas restart.
    Ini yang menyelesaikan masalah trade hilang bila Render restart.
    """
    if not _supa_enabled:
        return 0
    try:
        # Ambil trades yang masih aktif (bukan COMPLETED atau STOP_LOSS)
        rows = supa_fetch(
            "active_trades",
            filters="status=neq.COMPLETED&status=neq.STOP_LOSS")
        if not rows:
            return 0
        restored = 0
        with db_lock, sqlite3.connect(DB_NAME) as conn:
            for r in rows:
                try:
                    conn.execute('''INSERT OR IGNORE INTO active_trades
                        (msg_id, symbol, entry, sl, tp1, tp2, tp3, engine,
                         status, timestamp, macro_btc_pct, exit_price, exit_time)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (r['msg_id'], r['symbol'], r['entry'], r['sl'],
                         r['tp1'], r['tp2'], r['tp3'], r['engine'],
                         r['status'], r['timestamp'], r.get('macro_btc_pct', 0),
                         r.get('exit_price', 0), r.get('exit_time', 0)))
                    restored += 1
                except Exception:
                    pass
        if restored:
            logger.info(f"✅ [SUPA] Restored {restored} active trades dari Supabase")
        return restored
    except Exception as e:
        logger.warning(f"[SUPA] Restore error: {e}")
        return 0

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
    # ── Stablecoins USD-pegged ─────────────────────────────────────────────────
    'USDT', 'USDC', 'DAI', 'BUSD', 'TUSD', 'USDD', 'FDUSD', 'USDP', 'GUSD',
    'FRAX', 'LUSD', 'SUSD', 'USDS', 'PYUSD', 'USDE', 'USDX', 'AEUR',
    'USD1', 'RLUSD',
    'USDV', 'CUSD', 'CRVUSD', 'ALUSD', 'MUSD', 'OUSD', 'DOLA', 'RAI',
    'FEI', 'UST', 'USTC', 'USDN', 'USDW', 'USDB', 'USDK',
    'UUSD', 'U',                        # UUSDT yang lepas sebelum ini
    # ── Stablecoins Fiat-pegged ────────────────────────────────────────────────
    'EUR', 'TRY', 'BRL', 'AUD', 'GBP', 'JPY',
    'EURS', 'EURI', 'EURC', 'EUROC', 'EURT', 'AGEUR', 'XEUR',
    'CADC', 'XSGD', 'CNHT', 'BIDR', 'IDRT', 'TRYB', 'BRZ',
    # ── Wrapped (mirror underlying) ────────────────────────────────────────────
    'WBTC', 'WETH', 'WBNB', 'STETH', 'RETH', 'WEETH', 'CBETH', 'WSTETH', 'FRXETH',
    'BNSOL', 'WBETH',
    'WSOL', 'WMATIC', 'WAVAX', 'WDOGE', 'WFIL', 'WKAVA',
    'BTCB', 'HBTC',                     # Binance-bridged BTC
    # ── Liquid Staking Tokens ──────────────────────────────────────────────────
    'MSOL', 'JSOL', 'STSOL', 'SCNSOL',
    'SFRXETH', 'OSETH', 'SWETH', 'LSETH',
    'ANKRETH', 'ANKRBNB', 'ANKRMATIC',
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
    # bb_width 22%  = realistic squeeze pada 1H (range normal 8-30%, <22% = meaningful squeeze)
    # rvol 0.8x     = floor sahaja — accumulation = quiet vol, bukan spike
    # rsi 25-48     = cukup lebar untuk catch early reversal
    'acc_bb_width': 22.0,
    'acc_rvol': 0.8,
    'acc_rsi_max': 40,
    'acc_rsi_min': 25,
    # Higher-low: 0=soft hint (tunjuk tapi tidak block), 1=hard required
    'acc_require_higher_low': 0,
    # Radar — V8.2: lebih sensitive
    'radar_momentum': 2.0,
    'radar_min_vol': 12_000_000,
    # Cooldown
    'cd_breakout': 24,
    'cd_accumulation': 48,
    # Macro filter
    'macro_btc_filter': 1,
    'macro_btc_24h_min': -2.0,
    # Confirmation queue — V8.3: DISABLE (0) supaya signal hantar TERUS.
    # Sebelum: confirm_required=1 = tunggu ~1 jam untuk candle confirmation.
    # Masalah: peluang terlepas, user marah signal lambat/tak masuk.
    # Sekarang: hantar segera, tambah label ⚡ jika market kurang ideal.
    'confirm_required': 0,
    'pending_expiry_h': 2,
    # SL
    'sl_atr_mult': 1.5,
    'sl_max_pct': 0.08,
    # FIX KRITIKAL: fail_cooldown_h ditukar dari JAM kepada MINIT.
    # Nilai 2 sebelum ini = 2 JAM cooldown setiap rejection = hampir semua coin
    # diblock walaupun setup berubah dalam masa 30 minit.
    # Sekarang: 0.33 = 20 minit — cukup untuk elak repeat scan sia-sia,
    # tapi tidak terlalu lama kalau market berubah cepat.
    'fail_cooldown_h': 0.33,
    # Concurrency — MESTI 3 untuk Render Free 512MB
    # 5 task × ~80MB = 400MB + base 100MB = OOM
    # 3 task × ~80MB = 240MB + base 100MB = 340MB (selamat)
    'layer2_concurrency': 3,
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
        conn.execute('''CREATE TABLE IF NOT EXISTS reentry_watchlist
            (symbol TEXT PRIMARY KEY, original_engine TEXT,
             sl_hit_time REAL, sl_hit_price REAL,
             ob_top REAL DEFAULT 0, ob_bot REAL DEFAULT 0,
             fvg_top REAL DEFAULT 0, fvg_bot REAL DEFAULT 0,
             expiry REAL, reentry_count INTEGER DEFAULT 0)''')
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
    # FIX: Auto-repair nilai DB yang contradictory dari deployment lama
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        bad = conn.execute("SELECT value FROM tuning_params WHERE key='acc_rvol'").fetchone()
        if bad and float(bad[0]) > 1.1:
            conn.execute("INSERT OR REPLACE INTO tuning_params VALUES ('acc_rvol', 0.8)")
            logger.warning(f"[DB REPAIR] acc_rvol was {bad[0]} → fixed to 0.8")
        bad2 = conn.execute("SELECT value FROM tuning_params WHERE key='acc_bb_width'").fetchone()
        if bad2 and float(bad2[0]) < 12.0:
            conn.execute("INSERT OR REPLACE INTO tuning_params VALUES ('acc_bb_width', 22.0)")
            logger.warning(f"[DB REPAIR] acc_bb_width was {bad2[0]} (too tight) → fixed to 22.0")
        # FIX KRITIKAL: fail_cooldown_h = 2 (jam) menyebabkan semua coin diblock 2 jam
        # selepas setiap rejection. Ini punca utama "Promoted: 0" walaupun market bergerak.
        # PAKSA confirm_required=0 — DB lama mungkin ada nilai 1
        # yang menyebabkan semua ACCUMULATION signal masuk queue bukan Telegram
        conn.execute("INSERT OR REPLACE INTO tuning_params VALUES ('confirm_required', 0)")
        bad3 = conn.execute("SELECT value FROM tuning_params WHERE key='fail_cooldown_h'").fetchone()
        if bad3 and float(bad3[0]) >= 1.0:
            conn.execute("INSERT OR REPLACE INTO tuning_params VALUES ('fail_cooldown_h', 0.33)")
            logger.warning(f"[DB REPAIR] fail_cooldown_h was {bad3[0]}h (too long!) → fixed to 0.33h (20min)")
        # FIX: acc_bb_width 15.0 terlalu ketat untuk 1H — hampir tiada coin lulus
        bad4 = conn.execute("SELECT value FROM tuning_params WHERE key='acc_bb_width'").fetchone()
        if bad4 and float(bad4[0]) <= 15.0:
            conn.execute("INSERT OR REPLACE INTO tuning_params VALUES ('acc_bb_width', 22.0)")
            logger.warning(f"[DB REPAIR] acc_bb_width was {bad4[0]}% (too tight for 1H) → fixed to 22.0%")
        # Buang fail-cooldowns lama dari setting fail_cooldown_h=2 (2 jam)
        # Mana-mana cooldown yang tamat dalam 2 jam AKAN DATANG adalah fail-cooldown lama
        # last_signal field = unix timestamp TAMAT cooldown (bukan masa signal dihantar)
        # Buang yang tamat SEBELUM 2 jam dari sekarang — ini yang dijana oleh setting lama
        removed = conn.execute(
            "DELETE FROM cooldowns WHERE last_signal BETWEEN ? AND ?",
            (time.time(), time.time() + 7200)
        ).rowcount
        if removed > 0:
            logger.warning(f"[DB REPAIR] Cleared {removed} stale 2h fail-cooldowns")
    logger.info("✅ [DB] Nova7 SQLite initialized.")
    # Supabase: init connection test + restore active trades dari cloud
    supa_init_tables()
    supa_restore_on_startup()

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
    data = {
        'msg_id': msg_id, 'symbol': symbol, 'entry': entry, 'sl': sl,
        'tp1': tp1, 'tp2': tp2, 'tp3': tp3, 'engine': engine,
        'status': 'TRACKING', 'timestamp': time.time(),
        'macro_btc_pct': macro_btc_pct, 'exit_price': 0, 'exit_time': 0
    }
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.execute('''INSERT OR REPLACE INTO active_trades
            (msg_id, symbol, entry, sl, tp1, tp2, tp3, engine, status, timestamp, macro_btc_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'TRACKING', ?, ?)''',
            (msg_id, symbol, entry, sl, tp1, tp2, tp3, engine, time.time(), macro_btc_pct))
    # Supabase dual-write (async-safe via threading)
    threading.Thread(target=supa_upsert, args=('active_trades', data), daemon=True).start()

def get_active_trades():
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM active_trades WHERE status NOT IN ('COMPLETED', 'STOP_LOSS')").fetchall()

def update_trade_status(msg_id, status, exit_price=None):
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        if exit_price is not None:
            conn.execute(
                "UPDATE active_trades SET status=?, exit_price=?, exit_time=? WHERE msg_id=?",
                (status, exit_price, time.time(), msg_id))
            threading.Thread(target=supa_update, args=(
                'active_trades', 'msg_id', msg_id,
                {'status': status, 'exit_price': exit_price, 'exit_time': time.time()}
            ), daemon=True).start()
        else:
            conn.execute(
                "UPDATE active_trades SET status=? WHERE msg_id=?", (status, msg_id))
            threading.Thread(target=supa_update, args=(
                'active_trades', 'msg_id', msg_id, {'status': status}
            ), daemon=True).start()

def update_trade_sl(msg_id, new_sl):
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE active_trades SET sl=? WHERE msg_id=?", (new_sl, msg_id))
    threading.Thread(target=supa_update, args=(
        'active_trades', 'msg_id', msg_id, {'sl': new_sl}
    ), daemon=True).start()

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

# ── Re-entry Watchlist helpers ────────────────────────────────────────────────
def save_reentry_watch(symbol, engine, sl_price, ob_top, ob_bot, fvg_top, fvg_bot):
    expiry = time.time() + (7 * 86400)   # 7 hari TTL
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO reentry_watchlist
               VALUES (?,?,?,?,?,?,?,?,?,0)""",
            (symbol, engine, time.time(), sl_price,
             ob_top, ob_bot, fvg_top, fvg_bot, expiry))

def get_reentry_watchlist():
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM reentry_watchlist").fetchall()

def drop_reentry_watch(symbol):
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.execute("DELETE FROM reentry_watchlist WHERE symbol=?", (symbol,))

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
    return 50.0, 2.0  # Default modal $50

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
        self.opens = opens[-60:]
        self.closes, self.highs, self.lows, self.volumes = closes[-60:], highs[-60:], lows[-60:], volumes[-60:]
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

    def get_structure_low(self, lookback=15):
        """
        Cari swing low PALING TERKINI yang bermakna — bukan min() semua candle.

        MASALAH min(lows[-20:]):
          Mungkin return low dari 18 jam lepas semasa wick panjang.
          Tiada kaitan dengan struktur support semasa.

        PENYELESAIAN:
          Scan dari candle terkini ke belakang.
          Swing low = lows[i] lebih rendah dari 2 candle setiap sisi.
          Return yang pertama dijumpai = paling dekat dan paling relevan.

        Fallback: min(lows[-10:]) jika tiada swing low dijumpai.
        """
        if len(self.lows) < 6:
            return min(self.lows) if self.lows else 0
        n = min(lookback, len(self.lows) - 3)
        # Scan dari terkini ke belakang
        for i in range(-3, -(n + 1), -1):
            if abs(i) + 2 >= len(self.lows):
                continue
            if (self.lows[i] < self.lows[i - 1]
                    and self.lows[i] < self.lows[i - 2]
                    and self.lows[i] < self.lows[i + 1]
                    and self.lows[i] < self.lows[i + 2]):
                return self.lows[i]
        return min(self.lows[-10:])

# ==========================================
# ENGINES (DYNAMIC TUNING) — V8: Strict accumulation + ATR return
# ==========================================
class BreakoutHunter:
    """
    3/5 scoring — signal lulus jika ≥ 3 dari 5 syarat dipenuhi.
    Score dan syarat mana yang gagal akan dipaparkan dalam mesej Telegram.
    """
    def check(self, ind, t):
        if len(ind.closes) < 51:
            return None, {"Data Sejarah": "Kurang 51 candle (Gagal)"}
        close, rvol, recent_high = ind.closes[-1], ind.get_rvol(), ind.get_recent_high()
        rsi_min  = t.get('bo_rsi_min', 50)
        rsi_max  = t.get('bo_rsi_max', 75)
        rvol_min = t.get('bo_rvol', 1.8)
        conditions = {
            f"Pecah High 20-C ({recent_high:.6f})": close > recent_high,
            f"Atas EMA21 ({ind.ema21:.6f})":        close > ind.ema21,
            "Uptrend (EMA21 > EMA50)":              ind.ema21 > ind.ema50,
            f"RVOL >= {rvol_min}x [{rvol:.2f}x]":  rvol >= rvol_min,
            f"RSI {rsi_min}-{rsi_max} [{ind.rsi:.1f}]": rsi_min < ind.rsi < rsi_max,
        }
        score = sum(1 for v in conditions.values() if v)
        if score >= 3:
            structure_low = min(ind.lows[-20:])
            sig = {
                'type': 'BREAKOUT', 'rvol': rvol, 'break_level': recent_high,
                'low': structure_low, 'atr': ind.atr,
                'score': score, 'score_max': 5,
                'conditions': conditions,
                # Structure-based TP data
                'structure_measured': recent_high - structure_low,
            }
            return sig, conditions
        return None, conditions

# ==========================================
# RETEST HUNTER ENGINE — Entry di Zon Support/Resistance
# ==========================================
class RetestHunter:
    """
    Tangkap entry yang LEBIH AWAL dari breakout biasa.
    Scenario: Harga pecah resistance → pullback ke level yang sama
    (kini jadi support) → wick rejection candle di situ = ENTRY PREMIUM.

    Ini adalah scenario INJ $6.0 — resistance lama, retest, wick, naik semula.

    MATEMATIK:
    1. Cari resistance cluster — kawasan di mana ≥3 candle highs berkumpul
       dalam tolerance ±0.8% (equal highs = liquidity pool)

    2. Sahkan breakout — close semasa mesti pernah naik ≥1.5% atas cluster
       dalam 8 candle terkini (breakout berlaku baru-baru ini)

    3. Retest — harga kembali ke dalam ±1.5% dari level resistance lama
       (sekarang jadi support)

    4. Wick Rejection — lower wick ≥ 55% daripada julat candle penuh
       Lower wick = min(open, close) - low
       Total range = high - low
       Wick ratio = lower_wick / total_range

    5. Volume — RVOL semasa ≤ 1.8x (volume tidak spike = bukan fakeout,
       selling pressure telah habis di zon tersebut)

    6. RSI — 40–65 (tidak oversold, tidak overbought — kawasan momentum awal)

    KENAPA LEBIH BAIK DARI BREAKOUT ENTRY:
    Breakout entry = harga dah naik 3–5% dari base
    Retest entry = entry hampir sama dengan harga breakout asal
    Hasil: SL lebih ketat, RR lebih baik untuk move yang sama
    """

    def _find_resistance_cluster(self, highs, tolerance=0.008):
        """
        Cari kawasan di mana ≥3 highs berkumpul dalam ±0.8%.
        Return senarai {'level': float, 'count': int, 'idx_last': int}
        """
        clusters = []
        used = set()
        for i in range(len(highs) - 1, -1, -1):
            if i in used:
                continue
            group = [i]
            ref = highs[i]
            for j in range(i - 1, max(i - 50, -1), -1):
                if j not in used and abs(highs[j] - ref) / ref <= tolerance:
                    group.append(j)
            if len(group) >= 3:
                level = sum(highs[k] for k in group) / len(group)
                clusters.append({
                    'level':    level,
                    'count':    len(group),
                    'idx_last': max(group),
                    'idx_first': min(group),
                })
                used.update(group)
        return sorted(clusters, key=lambda x: x['idx_last'], reverse=True)

    def check(self, ind, t):
        if len(ind.closes) < 30:
            return None, {}

        closes  = ind.closes
        highs   = ind.highs
        lows    = ind.lows
        opens   = ind.opens
        price   = closes[-1]
        rvol    = ind.get_rvol()

        # Cari cluster resistance dalam 50 candle lepas
        clusters = self._find_resistance_cluster(highs[-50:], tolerance=0.008)
        if not clusters:
            return None, {"Resistance cluster": False}

        best_cluster = None
        for cl in clusters:
            level = cl['level']
            # Cluster mesti bawah harga semasa (sudah pecah dan kini jadi support)
            if price < level * 0.985:
                continue
            # Breakout: dalam 8 candle terkini, ada close ≥ 1.5% atas level
            recent_closes = closes[-8:]
            breakout_happened = any(c > level * 1.015 for c in recent_closes)
            if not breakout_happened:
                continue
            # Retest: harga semasa dalam ±1.5% dari level
            in_retest_zone = abs(price - level) / level <= 0.015
            if not in_retest_zone:
                continue
            best_cluster = cl
            break

        if not best_cluster:
            conditions = {
                "Resistance cluster dijumpai": bool(clusters),
                "Breakout berlaku (8 candle)": False,
                "Retest zone (±1.5%)": False,
            }
            return None, conditions

        level = best_cluster['level']

        # Wick rejection pada candle semasa dan candle lepas (ambil yang terbaik)
        best_wick_ratio = 0.0
        for idx in [-1, -2]:  # semak candle semasa dan sebelumnya
            if abs(idx) > len(closes):
                continue
            c_open  = opens[idx]
            c_close = closes[idx]
            c_high  = highs[idx]
            c_low   = lows[idx]
            body_bot = min(c_open, c_close)
            total_range = c_high - c_low
            lower_wick  = body_bot - c_low
            wick_ratio  = lower_wick / total_range if total_range > 0 else 0
            if wick_ratio > best_wick_ratio:
                best_wick_ratio = wick_ratio

        rsi     = ind.rsi
        rvol_ok = rvol <= 1.8   # volume tidak gila = bukan fakeout
        wick_ok = best_wick_ratio >= 0.55
        rsi_ok  = 40 < rsi < 65

        conditions = {
            f"Resistance cluster @ ${level:.4f} [{best_cluster['count']} touch]": True,
            f"Breakout berlaku (8 candle)":  True,
            f"Retest zone ±1.5% [{price:.4f}]":     True,
            f"Wick Rejection [{best_wick_ratio*100:.0f}% ≥ 55%]": wick_ok,
            f"RVOL ≤ 1.8x [{rvol:.2f}x — tiada spike]": rvol_ok,
            f"RSI 40–65 [{rsi:.1f}]": rsi_ok,
        }

        if all(conditions.values()):
            structure_low = min(lows[-10:])
            sig = {
                'type':          'RETEST',
                'rvol':          rvol,
                'break_level':   level,
                'wick_ratio':    best_wick_ratio,
                'cluster_count': best_cluster['count'],
                'low':           structure_low,
                'atr':           ind.atr,
            }
            return sig, conditions

        return None, conditions

class AccumulationDetective:
    """
    3/4 scoring + 2 HARD GATE TAMBAHAN (wajib lulus, tidak masuk score):

    HARD GATE 1 — EMA50 Proximity:
      Harga mesti dalam 7% dari EMA50.
      Jika > 7% bawah EMA50 = coin dalam downtrend dalam, bukan accumulation.
      PORTOUSDT -15% bawah EMA50 = downtrend, bukan squeeze.

    HARD GATE 2 — No Fresh Lower Low:
      min(lows[-3:]) >= min(lows[-8:-3]) × 0.985
      3 candle terkini tidak boleh buat low baru berbanding 5 candle sebelumnya.
      Kalau masih turun = compression belum berlaku, masih dalam selling phase.
    """
    def check(self, ind, t):
        if len(ind.closes) < 51:
            return None, {}
        close, bb, rvol = ind.closes[-1], ind.get_bb_width(), ind.get_rvol()
        bb_max   = t.get('acc_bb_width', 22.0)
        rvol_min = t.get('acc_rvol', 0.8)
        rsi_max  = t.get('acc_rsi_max', 48)
        rsi_min  = t.get('acc_rsi_min', 25)
        higher_low = ind.is_recent_higher_low()
        is_green   = ind.is_current_green()

        # ── HARD GATE 1: EMA50 proximity ─────────────────────────────────────
        # Harga tidak boleh lebih 7% bawah EMA50
        # > 7% = downtrend aktif, bukan accumulation zone
        ema50_dist_pct = (ind.ema50 - close) / ind.ema50 * 100 if ind.ema50 > 0 else 99
        gate_ema_proximity = ema50_dist_pct <= 7.0
        if not gate_ema_proximity:
            return None, {
                f"EMA50 Proximity [{ema50_dist_pct:.1f}% > 7%]": False
            }

        # ── HARD GATE 2: No fresh lower low (body, bukan wick) ───────────────
        # Guna min(open,close) — body candle, bukan wick
        # Sweep H4 buat wick panjang tapi body tutup atas = bukan lower low sebenar
        # Downtrend sebenar: body candle memang rendah = gate gagal betul
        if len(ind.lows) >= 8 and len(ind.opens) >= 8 and len(ind.closes) >= 8:
            body_lows_recent = [min(ind.opens[i], ind.closes[i]) for i in range(-3, 0)]
            body_lows_prev   = [min(ind.opens[i], ind.closes[i]) for i in range(-8, -3)]
            recent_body_low  = min(body_lows_recent)
            prev_body_low    = min(body_lows_prev)
            gate_no_new_low  = recent_body_low >= prev_body_low * 0.985
        else:
            gate_no_new_low = True
        if not gate_no_new_low:
            return None, {
                f"No Fresh Lower Low (body) [{recent_body_low:.6f} < {prev_body_low:.6f}]": False
            }

        # ── HARD GATE 3: RVOL minimum wajib ──────────────────────────────────
        # RVOL < threshold = tiada volume = mudah kena manipulation
        # Signal dengan RVOL 0.09x-0.19x hampir semua kena SL
        if rvol < rvol_min:
            return None, {
                f"RVOL Hard Gate >= {rvol_min}x [{rvol:.2f}x]": False
            }

        # ── SOFT CHECK: Recovery candle ───────────────────────────────────────
        # Candle semasa hijau (close > open) = momentum shift berlaku
        # Tidak block signal — hanya dipapar dalam info box sebagai awareness
        recovery_candle = ind.is_current_green()  # True/False sahaja

        # ── 4 SYARAT SCORING ─────────────────────────────────────────────────
        conditions = {
            f"BB Squeeze < {bb_max}% [{bb:.2f}%]":               bb < bb_max,
            f"Vol Aktif >= {rvol_min}x [{rvol:.2f}x]":           rvol >= rvol_min,
            "Dalam Accum Zone (bawah EMA50)":                     close < ind.ema50,
            f"RSI Oversold {rsi_min:.0f}-{rsi_max:.0f} [{ind.rsi:.1f}]": rsi_min < ind.rsi < rsi_max,
        }
        score = sum(1 for v in conditions.values() if v)

        if score >= 3:
            # Guna get_structure_low() — swing low terkini, bukan min(-20:) mutlak
            structure_low = ind.get_structure_low(lookback=15)
            sig = {
                'type': 'ACCUMULATION', 'rvol': rvol, 'bb': bb,
                'low': structure_low, 'atr': ind.atr,
                'score': score, 'score_max': 4,
                'higher_low': higher_low, 'is_green': is_green,
                'recovery_candle': recovery_candle,
                'conditions': conditions,
                'tp_ema21':      ind.ema21,
                'tp_ema50':      ind.ema50,
                'tp_swing_high': max(ind.highs[-20:]),
            }
            return sig, conditions
        return None, conditions

# ==========================================
# CANDLE PATTERN DETECTOR
# ==========================================
class CandlePatternDetector:
    """
    Kesan pattern candle reversal pada 1H data.
    Semua pattern dikira dari OHLC — bukan indicator berasaskan.

    MATEMATIK SETIAP PATTERN:
    ─────────────────────────────────────────────────────────────
    HAMMER / PIN BAR:
      body     = |close - open|
      total    = high - low
      lower_wick = min(open,close) - low
      upper_wick = high - max(open,close)
      Syarat: lower_wick/total >= 0.60 DAN body/total <= 0.30
      Maksud: penolakan kuat dari bawah — seller gagal kekalkan tekanan

    BULLISH ENGULFING:
      candle[-2] = bearish (close < open)
      candle[-1] = bullish (close > open)
      Syarat: open[-1] <= close[-2] DAN close[-1] >= open[-2]
      Maksud: buyer menelan sepenuhnya candle seller sebelum = momentum bertukar

    MORNING STAR (3 candle):
      candle[-3] = bearish besar (body >= 60% total)
      candle[-2] = badan kecil (body/total <= 35%) = ketidaktentuan
      candle[-1] = bullish (close >= midpoint candle[-3])
      Maksud: tekanan jual habis, momentum bertukar ke bullish

    BULLISH MARUBOZU (modified — tidak ketat):
      body/total >= 0.75 DAN close > open
      Hampir tiada wick = buyer dominan dari awal hingga akhir
    ─────────────────────────────────────────────────────────────
    """

    def _body(self, o, c): return abs(c - o)
    def _range(self, h, l): return h - l if h > l else 0.0001

    def detect(self, opens, closes, highs, lows, lookback=3):
        """
        Semak lookback candle terkini. Return dict:
        {
          'pattern': str,       # nama pattern terbaik
          'strength': float,    # 0.0–1.0
          'candle_idx': int,    # -1 = semasa, -2 = sebelum
          'found': bool
        }
        """
        best = {'pattern': 'NONE', 'strength': 0.0, 'candle_idx': -1, 'found': False}
        n = len(closes)
        if n < 3:
            return best

        for idx in range(-1, -(lookback + 1), -1):
            if abs(idx) > n:
                break
            o  = opens[idx];  c  = closes[idx]
            h  = highs[idx];  l  = lows[idx]
            rng = self._range(h, l)
            body = self._body(o, c)
            lower_wick  = min(o, c) - l
            upper_wick  = h - max(o, c)

            # 1. HAMMER
            lw_ratio   = lower_wick / rng
            body_ratio = body / rng
            if lw_ratio >= 0.60 and body_ratio <= 0.30:
                strength = lw_ratio * (1 - body_ratio)
                if strength > best['strength']:
                    best = {'pattern': 'HAMMER', 'strength': round(strength, 3),
                            'candle_idx': idx, 'found': True}

            # 2. BULLISH MARUBOZU
            if c > o and body_ratio >= 0.75:
                strength = body_ratio
                if strength > best['strength']:
                    best = {'pattern': 'MARUBOZU', 'strength': round(strength, 3),
                            'candle_idx': idx, 'found': True}

            # 3. BULLISH ENGULFING (perlu idx-1 juga)
            if abs(idx) + 1 <= n:
                o2 = opens[idx - 1]; c2 = closes[idx - 1]
                if c2 < o2 and c > o:               # prev bearish, curr bullish
                    if o <= c2 and c >= o2:         # engulf: curr badan telan prev
                        curr_body = self._body(o, c)
                        prev_body = self._body(o2, c2)
                        curr_body_ratio = curr_body / rng
                        # Syarat tambah: badan semasa mesti significant
                        # DAN lebih besar dari badan sebelumnya (true engulfing)
                        body_significant = curr_body_ratio >= 0.50
                        body_bigger      = prev_body > 0 and curr_body >= prev_body * 0.70
                        if body_significant and body_bigger:
                            eng_pct = body / self._range(
                                max(h, highs[idx-1]), min(l, lows[idx-1]))
                            if eng_pct >= 0.60:
                                strength = eng_pct
                                if strength > best['strength']:
                                    best = {'pattern': 'ENGULFING',
                                            'strength': round(strength, 3),
                                            'candle_idx': idx, 'found': True}

        # 4. MORNING STAR (khusus 3 candle terkini)
        if n >= 3:
            o3,c3,h3,l3 = opens[-3],closes[-3],highs[-3],lows[-3]  # candle 1
            o2,c2,h2,l2 = opens[-2],closes[-2],highs[-2],lows[-2]  # candle 2 (doji)
            o1,c1,h1,l1 = opens[-1],closes[-1],highs[-1],lows[-1]  # candle 3
            rng3  = self._range(h3, l3)
            body3 = self._body(o3, c3)
            rng2  = self._range(h2, l2)
            body2 = self._body(o2, c2)
            mid3  = (o3 + c3) / 2
            if (c3 < o3 and body3/rng3 >= 0.55   # candle 1: bearish besar
                and body2/rng2 <= 0.35             # candle 2: kecil (uncertainty)
                and c1 > o1                         # candle 3: bullish
                and c1 >= mid3):                    # candle 3 menutup separuh candle 1
                strength = (c1 - mid3) / rng3
                strength = min(strength, 1.0)
                if strength > best['strength']:
                    best = {'pattern': 'MORNING STAR', 'strength': round(strength, 3),
                            'candle_idx': -1, 'found': True}

        return best

# ==========================================
# SWEEP REVERSAL ENGINE — Entry Selepas Liquidity Sweep
# ==========================================
class SweepReversalEngine:
    """
    Engine premium — masuk SELEPAS liquidity sweep berlaku, bukan semasa squeeze.

    LOGIK PENUH:
    ─────────────────────────────────────────────────────────────
    FASA 1 — RANGE SQUEEZE:
      Coin dalam squeeze (BB Width < 18%) selama ≥ 6 candle.
      Range high dan low dikenal pasti dari 30 candle terkini.

    FASA 2 — LIQUIDITY SWEEP:
      Harga wick/close bawah range low dengan ≥ 0.3% breach.
      Ini adalah "stop hunt" — retail kena SL, SM ambil order.
      KRITIKAL: Candle mesti CLOSE BALIK atas range low.
      Jika tidak, ini adalah breakdown sebenar, bukan sweep.

    FASA 3 — REVERSAL CANDLE:
      Pada atau selepas sweep candle, ada:
      - Hammer / Pin Bar (wick 60%+ dari range)
      - Bullish Engulfing
      - Morning Star (3 candle)
      - Marubozu (body 75%+)

    FASA 4 — VOLUME KONFIRMASI:
      RVOL pada reversal candle ≥ 1.5x
      Ini beza dari accumulation (0.8x floor).
      Volume naik semasa recovery = SM sedang beli.

    FASA 5 — FALSE SWEEP FILTER:
      Harga semasa > range low (bukan turun terus).
      Jarak dari sweep low ke harga kini ≥ 0.5%
      (menunjukkan recovery sudah bermula).

    FASA 6 — STRUKTUR TIDAK ROSAK:
      EMA21 berjarak tidak lebih 5% dari range.
      (EMA21 terlalu jauh = trend sudah berubah, bukan pullback)
    ─────────────────────────────────────────────────────────────
    """

    def _find_range(self, highs, lows, closes, lookback=30, squeeze_thresh=18.0):
        """Cari range yang terbentuk semasa squeeze."""
        if len(closes) < lookback:
            return None
        window_c = closes[-lookback:]
        window_h = highs[-lookback:]
        window_l = lows[-lookback:]
        sma = sum(window_c) / len(window_c)
        std = (sum((p - sma) ** 2 for p in window_c) / len(window_c)) ** 0.5
        bb_width = (4 * std / sma * 100) if sma > 0 else 99.0
        if bb_width > squeeze_thresh:
            return None      # tiada squeeze = tiada range valid
        # Range high/low dari window (buang outlier dengan percentile kasar)
        sorted_h = sorted(window_h)
        sorted_l = sorted(window_l)
        pct_cut  = max(1, len(sorted_h) // 10)  # buang 10% tertinggi/terendah
        range_high = sum(sorted_h[-(pct_cut+1):]) / (pct_cut + 1)
        range_low  = sum(sorted_l[:pct_cut+1])  / (pct_cut + 1)
        return {
            'high':     range_high,
            'low':      range_low,
            'bb_width': round(bb_width, 2),
            'sma':      sma,
        }

    def check(self, ind, t):
        if len(ind.closes) < 35:
            return None, {}

        closes = ind.closes
        highs  = ind.highs
        lows   = ind.lows
        opens  = ind.opens
        vols   = ind.volumes
        price  = closes[-1]

        # ── Gate 1: Range formation ──────────────────────────────────
        rng = self._find_range(highs, lows, closes)
        if not rng:
            return None, {"Range Squeeze &lt; 18%": False}

        range_low  = rng['low']
        range_high = rng['high']
        range_size = range_high - range_low
        if range_size <= 0:
            return None, {"Range size valid": False}

        # ── Gate 2: Liquidity Sweep berlaku ──────────────────────────
        # Cari dalam 8 candle terkini
        sweep_found   = False
        sweep_low_val = range_low
        sweep_idx     = -1

        for i in range(-1, -9, -1):
            if abs(i) > len(lows):
                break
            c_low   = lows[i]
            c_close = closes[i]
            # Wick tembus bawah range_low ≥ 0.3%
            breach = (range_low - c_low) / range_low
            if breach >= 0.003:
                # Wajib close balik ATAS range_low (confirm sweep, bukan breakdown)
                if c_close > range_low:
                    sweep_found   = True
                    sweep_low_val = min(sweep_low_val, c_low)
                    sweep_idx     = i
                    break

        cond_sweep = sweep_found
        conditions = {
            f"Range Squeeze {rng['bb_width']:.1f}% &lt; 18%": True,
            f"Liquidity Sweep bawah ${range_low:.6f}":          cond_sweep,
        }
        if not cond_sweep:
            return None, conditions

        # ── Gate 3: Reversal candle ───────────────────────────────────
        detector  = CandlePatternDetector()
        pattern   = detector.detect(opens, closes, highs, lows, lookback=4)
        cond_pattern = pattern['found']
        conditions[f"Reversal [{pattern['pattern']} {pattern['strength']:.0%}]"] = cond_pattern

        # ── Gate 4: Volume pada reversal ≥ 1.5x ──────────────────────
        if len(vols) >= 21:
            avg_vol  = sum(vols[-21:-1]) / 20
            rev_idx  = pattern.get('candle_idx', -1)
            rev_vol  = vols[rev_idx] if abs(rev_idx) <= len(vols) else vols[-1]
            rvol_rev = rev_vol / avg_vol if avg_vol > 0 else 1.0
        else:
            rvol_rev = ind.get_rvol()
        cond_vol = rvol_rev >= 1.5
        conditions[f"RVOL Reversal &gt;= 1.5x [{rvol_rev:.2f}x]"] = cond_vol

        # ── Gate 5: Recovery bermula (harga atas sweep low + ≥ 0.5%) ─
        recovery_pct = (price - sweep_low_val) / sweep_low_val * 100 if sweep_low_val > 0 else 0
        cond_recovery = price > range_low and recovery_pct >= 0.5
        conditions[f"Recovery &gt;= 0.5% [{recovery_pct:.1f}%]"] = cond_recovery

        # ── Gate 6: Struktur EMA21 tidak rosak ───────────────────────
        ema_dist = abs(ind.ema21 - range_high) / range_high * 100
        cond_ema = ema_dist <= 5.0
        conditions[f"EMA21 dekat range [dist {ema_dist:.1f}%]"] = cond_ema

        if all(conditions.values()):
            # SL = bawah sweep low + buffer ATR kecil
            atr_buf = ind.atr * 0.3 if ind.atr > 0 else sweep_low_val * 0.003
            sl_raw  = sweep_low_val - atr_buf
            sig = {
                'type':        'SWEEP_REVERSAL',
                'rvol':        rvol_rev,
                'low':         sweep_low_val,
                'range_high':  range_high,
                'range_low':   range_low,
                'range_size':  range_size,
                'sweep_low':   sweep_low_val,
                'pattern':     pattern['pattern'],
                'pattern_str': pattern['strength'],
                'atr':         ind.atr,
                'sl_raw':      sl_raw,
                'score':       5,
                'score_max':   5,
                'conditions':  conditions,
            }
            return sig, conditions

        return None, conditions

# ==========================================
# SMC ANALYZER — HTF Bias + BOS/CHoCH + FVG + Order Block
# ==========================================
_smc_cache      = {}
_smc_cache_lock = threading.Lock()

class SMCAnalyzer:
    """
    Smart Money Concepts — 4 lapisan analisis tambahan.
    Semua FAIL-SILENT: kalau API gagal, signal tetap keluar tanpa SMC data.
    Tidak memblock signal — memberi konteks dan naik taraf kualiti sahaja.

    ┌─────────────────────────────────────────────────────────────────────┐
    │  1. HTF BIAS (1D + 4H)                                             │
    │     1D EMA50: atas = bullish makro, bawah = bearish makro          │
    │     4H EMA21: atas = medium-term bullish, bawah = bearish          │
    │                                                                     │
    │  2. BOS / CHoCH (M15 — lebih accurate dari M5 untuk 1H entry)     │
    │     M5 terlalu noisy (micro-swing tidak relevan untuk 1H target)   │
    │     M15 tunjuk structural shift sebelum nampak pada 1H             │
    │                                                                     │
    │     Swing High = highs[i] > highs[i-1] dan highs[i] > highs[i+1] │
    │     Swing Low  = lows[i]  < lows[i-1]  dan lows[i]  < lows[i+1] │
    │                                                                     │
    │     BOS Bullish  = close > last swing high → buyer in control      │
    │     CHoCH Bull   = lower-low trend broken, price > last swing high │
    │     CHoCH Bear   = higher-high trend broken, price < last swing low│
    │                                                                     │
    │  3. FAIR VALUE GAP (FVG) pada 1H                                  │
    │     Bullish FVG: lows[i] > highs[i-2]                             │
    │     (candle[i] naik terlalu cepat — gap tidak diisi)               │
    │     Harga balik ke FVG = probability support tinggi               │
    │                                                                     │
    │  4. ORDER BLOCK (OB) pada 1H                                       │
    │     Last bearish candle SEBELUM impulsive bullish move ≥ 1.5%     │
    │     Kawasan OB = institusi letak buy order                         │
    │     Harga balik ke OB = sharp entry point                         │
    └─────────────────────────────────────────────────────────────────────┘
    """

    # ── Cache helpers ─────────────────────────────────────────────────────
    def _get_cache(self, key, ttl):
        with _smc_cache_lock:
            e = _smc_cache.get(key)
            if e and time.time() - e['t'] < ttl:
                return e['d']
        return None

    def _set_cache(self, key, data):
        with _smc_cache_lock:
            _smc_cache[key] = {'d': data, 't': time.time()}
            # Bersihkan entries yang expired setiap kali cache ditulis
            # Elak _smc_cache membesar tanpa had selepas berjam-jam beroperasi
            now = time.time()
            stale = [k for k, v in _smc_cache.items()
                     if now - v['t'] > 7200]   # buang yang > 2 jam
            for k in stale:
                del _smc_cache[k]
            if stale:
                logger.debug(f"[SMC CACHE] Evicted {len(stale)} stale entries")

    # ── HTF fetch (1D + 4H) ───────────────────────────────────────────────
    async def _fetch_htf(self, symbol, session):
        key_1d = f"{symbol}_1d"
        key_4h = f"{symbol}_4h"
        cached_1d = self._get_cache(key_1d, 3600)   # 1h TTL
        cached_4h = self._get_cache(key_4h, 1800)   # 30min TTL

        try:
            if not cached_1d:
                async with session.get(
                    f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1d&limit=55",
                    timeout=aiohttp.ClientTimeout(total=8)) as r:
                    cached_1d = await r.json()
                    self._set_cache(key_1d, cached_1d)

            if not cached_4h:
                async with session.get(
                    f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=4h&limit=50",
                    timeout=aiohttp.ClientTimeout(total=8)) as r:
                    cached_4h = await r.json()
                    self._set_cache(key_4h, cached_4h)
        except Exception:
            return None, None
        return cached_1d, cached_4h

    def _htf_bias(self, klines, ema_period):
        """Kira EMA dan semak harga vs EMA."""
        if not klines or len(klines) < ema_period + 1:
            return 'NEUTRAL'
        closes = [float(d[4]) for d in klines]
        k = 2.0 / (ema_period + 1)
        ema = sum(closes[:ema_period]) / ema_period
        for p in closes[ema_period:]:
            ema = p * k + ema * (1 - k)
        return 'BULL' if closes[-1] > ema else 'BEAR'

    # ── M15 BOS/CHoCH ─────────────────────────────────────────────────────
    async def _fetch_m15(self, symbol, session):
        key = f"{symbol}_m15"
        cached = self._get_cache(key, 300)   # 5min TTL
        if cached:
            return cached
        try:
            async with session.get(
                f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=15m&limit=60",
                timeout=aiohttp.ClientTimeout(total=8)) as r:
                data = await r.json()
                self._set_cache(key, data)
                return data
        except Exception:
            return None

    def _detect_bos_choch(self, klines):
        """
        Swing detection dengan lookback 2 candle kiri & kanan.
        Return {'type': str, 'level': float, 'label': str}
        """
        if not klines or len(klines) < 10:
            return {'type': 'NEUTRAL', 'level': 0, 'label': '—'}

        highs  = [float(d[2]) for d in klines]
        lows   = [float(d[3]) for d in klines]
        closes = [float(d[4]) for d in klines]

        # Kesan swing highs dan swing lows
        # Lookback 3 candle setiap sisi — M15 × 3 = 45 min, lebih signifikan dari 2 candle (30 min)
        sh_list = []
        sl_list = []
        for i in range(3, len(highs) - 3):
            if (highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i-3]
                    and highs[i] > highs[i+1] and highs[i] > highs[i+2] and highs[i] > highs[i+3]):
                sh_list.append((i, highs[i]))
            if (lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i-3]
                    and lows[i] < lows[i+1] and lows[i] < lows[i+2] and lows[i] < lows[i+3]):
                sl_list.append((i, lows[i]))

        if not sh_list or not sl_list:
            return {'type': 'NEUTRAL', 'level': 0, 'label': '—'}

        last_sh = sh_list[-1][1]
        last_sl = sl_list[-1][1]
        price   = closes[-1]

        prev_highs = [p for _, p in sh_list[-5:]]
        prev_lows  = [p for _, p in sl_list[-5:]]

        # Trend confirmation perlu minimum 3 swing dalam arah yang sama
        # Uptrend:   HH + HL + HL (dua HL berturut = trend sah)
        # Downtrend: LH + LH + LL (dua LH berturut = trend sah)
        was_uptrend = (
            len(prev_highs) >= 3 and prev_highs[-1] > prev_highs[-2]   # HH terkini
            and len(prev_lows) >= 3
            and prev_lows[-1] > prev_lows[-2]                           # HL terkini
            and prev_lows[-2] > prev_lows[-3]                           # HL sebelumnya juga naik
        )
        was_downtrend = (
            len(prev_highs) >= 3
            and prev_highs[-1] < prev_highs[-2]                         # LH terkini
            and prev_highs[-2] < prev_highs[-3]                         # LH sebelumnya juga turun
            and len(prev_lows) >= 3 and prev_lows[-1] < prev_lows[-2]  # LL terkini
        )

        if was_downtrend and price > last_sh:
            return {'type': 'CHOCH_BULL', 'level': last_sh,
                    'label': 'CHoCH ↑'}   # Downtrend flip ke bullish
        if was_uptrend and price < last_sl:
            return {'type': 'CHOCH_BEAR', 'level': last_sl,
                    'label': 'CHoCH ↓ ⚠️'}  # Uptrend flip ke bearish
        if price > last_sh:
            return {'type': 'BOS_BULL', 'level': last_sh,
                    'label': 'BOS ↑'}       # Breakout structure
        if price < last_sl:
            return {'type': 'BOS_BEAR', 'level': last_sl,
                    'label': 'BOS ↓'}       # Breakdown structure
        return {'type': 'NEUTRAL', 'level': 0, 'label': 'Ranging'}

    # ── FVG Detection (guna ind data — tiada API tambahan) ────────────────
    def _detect_fvg(self, highs, lows, closes):
        """
        Bullish FVG: lows[i] > highs[i-2]
        Cari FVG yang paling dekat dan belum diisi, bawah harga semasa.
        """
        price = closes[-1]
        # Scan dari candle ke-3 supaya ada i-2
        for i in range(len(closes) - 4, 2, -1):
            # Bullish FVG
            if lows[i] > highs[i - 2]:
                fvg_bot = highs[i - 2]
                fvg_top = lows[i]
                # FVG mestilah bawah harga semasa (potential support)
                if fvg_top >= price:
                    continue
                # Semak jika sudah diisi (mana-mana candle selepas FVG tutup bawah fvg_bot)
                filled = any(lows[j] < fvg_bot for j in range(i + 1, len(lows)))
                if not filled:
                    return {
                        'found':    True,
                        'bot':      round(fvg_bot, 8),
                        'top':      round(fvg_top, 8),
                        'dist_pct': round((price - fvg_top) / price * 100, 2),
                    }
        return {'found': False}

    # ── Order Block Detection (guna ind data — tiada API tambahan) ────────
    def _detect_order_block(self, opens, closes, highs, lows):
        """
        Last bearish candle sebelum impulsive bullish move ≥ 1.5%.
        Scan 30 candle terkini.
        """
        price = closes[-1]
        n = min(30, len(closes) - 2)
        for i in range(len(closes) - n, len(closes) - 2):
            # Semak impulse bullish pada candle i+1 atau i+2
            for j in [i + 1, i + 2]:
                if j >= len(closes):
                    continue
                move = (closes[j] - opens[j]) / opens[j] if opens[j] > 0 else 0
                if move >= 0.015 and closes[j] > opens[j]:   # bullish ≥1.5%
                    # Candle i mesti bearish (order block)
                    if closes[i] < opens[i]:
                        ob_top = highs[i]
                        ob_bot = lows[i]
                        # OB mestilah bawah atau dekat harga semasa (support zone)
                        if ob_top >= price * 1.03:
                            continue   # OB terlalu tinggi
                        in_zone = ob_bot <= price <= ob_top * 1.02
                        return {
                            'found':   True,
                            'top':     round(ob_top, 8),
                            'bot':     round(ob_bot, 8),
                            'in_zone': in_zone,
                            'strength': round(abs(closes[i] - opens[i]) / opens[i] * 100, 2),
                        }
        return {'found': False}

    # ── Fresh Zone Detection ──────────────────────────────────────────────────
    def _detect_fresh_zones(self, opens, closes, highs, lows, current_price,
                             min_drop_pct=2.5, lookback=80):
        """
        Fresh Resistance Zone — matematik sebenar.

        Swing High: highs[i] > highs[i-2..i-1] DAN highs[i] > highs[i+1..i+2]
        Lookback 2 candle setiap sisi — elak noise micro-fluctuation.
        H1 threshold: 2.5% drop (1.5% terlalu biasa dalam crypto)
        M15 threshold: 1.5% (lebih sensitif untuk TF rendah)
        """
        zones = []
        n = min(len(closes), lookback + 5)
        if n < 8:
            return zones

        for i in range(2, n - 5):
            # Swing high — 2 candle setiap sisi
            if not (highs[i] > highs[i-1] and highs[i] > highs[i-2]
                    and highs[i] > highs[i+1] and highs[i] > highs[i+2]):
                continue

            zone_top = highs[i]
            zone_bot = max(opens[i], closes[i])  # body top

            if zone_bot <= current_price:
                continue

            # Confirm significant drop selepas swing high
            future_window = closes[i + 1: min(i + 6, len(closes))]
            if not future_window:
                continue
            drop_pct = (zone_top - min(future_window)) / zone_top * 100
            if drop_pct < min_drop_pct:
                continue

            # Freshness: tiada candle selepas yang masuk zone_bot
            highs_after = highs[i + 1:]
            if highs_after and max(highs_after) >= zone_bot:
                continue  # MITIGATED

            zones.append({
                'top':      round(zone_top, 8),
                'bot':      round(zone_bot, 8),
                'dist_pct': round((zone_bot - current_price) / current_price * 100, 2),
                'drop_pct': round(drop_pct, 2),
                'fresh':    True,
            })

        zones.sort(key=lambda z: z['dist_pct'])
        return zones

    # ── Main entry point ──────────────────────────────────────────────────
    async def analyze(self, symbol, ind, session):
        """
        Jalankan semua analisis serentak.
        Return context dict — fail-silent.
        """
        ctx = {
            'htf_1d':    'NEUTRAL',
            'htf_4h':    'NEUTRAL',
            'bos_choch': {'type': 'NEUTRAL', 'level': 0, 'label': '—'},
            'fvg':       {'found': False},
            'ob':        {'found': False},
            'fresh_m15': [],   # Fresh zones dari M15 (untuk TP1)
            'fresh_h1':  [],   # Fresh zones dari H1 (untuk TP2/TP3)
        }
        price = ind.closes[-1] if ind.closes else 0
        try:
            klines_1d, klines_4h = await self._fetch_htf(symbol, session)
            if klines_1d:
                ctx['htf_1d'] = self._htf_bias(klines_1d, 50)
            if klines_4h:
                ctx['htf_4h'] = self._htf_bias(klines_4h, 21)

            klines_m15 = await self._fetch_m15(symbol, session)
            if klines_m15:
                ctx['bos_choch'] = self._detect_bos_choch(klines_m15)
                if price > 0:
                    m15_o = [float(d[1]) for d in klines_m15]
                    m15_h = [float(d[2]) for d in klines_m15]
                    m15_l = [float(d[3]) for d in klines_m15]
                    m15_c = [float(d[4]) for d in klines_m15]
                    ctx['fresh_m15'] = self._detect_fresh_zones(
                        m15_o, m15_c, m15_h, m15_l, price,
                        min_drop_pct=1.5, lookback=60)   # M15: 1.5%

            if price > 0:
                ctx['fresh_h1'] = self._detect_fresh_zones(
                    ind.opens, ind.closes, ind.highs, ind.lows, price,
                    min_drop_pct=2.5, lookback=80)        # H1: 2.5%

            ctx['fvg'] = self._detect_fvg(ind.highs, ind.lows, ind.closes)
            ctx['ob']  = self._detect_order_block(
                ind.opens, ind.closes, ind.highs, ind.lows)

        except Exception as e:
            logger.debug(f"[SMC] {symbol} analyze error: {e}")
        return ctx


_smc_instance = SMCAnalyzer()   # singleton — satu instance untuk semua calls

_btc_macro_cache = {'time': 0, 'data': None}
_btc_macro_lock  = threading.Lock()

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
                'btc_price':        btc_price,
                'btc_24h_pct':      btc_24h_pct,
                'btc_above_ema21d': btc_above_ema21d,
                'btc_ema21d':       ema21d,
                'fetched_at':       now,
            }
            # SANITY CHECK: guna btc_price dari ticker (paling terkini) untuk
            # semak semula btc_above_ema21d — elak klines data stale menipu.
            # Jika harga ticker lain >5% dari closes[-1], guna ticker sebagai hakim.
            if btc_price > 0 and closes:
                kline_last = closes[-1]
                drift_pct = abs(btc_price - kline_last) / kline_last * 100
                if drift_pct > 5:
                    logger.warning(
                        f"[MACRO] BTC klines stale! ticker=${btc_price:,.0f} "
                        f"vs klines[-1]=${kline_last:,.0f} (drift {drift_pct:.1f}%)"
                        f" — overriding btc_above_ema21d with ticker price")
                    data['btc_above_ema21d'] = btc_price > ema21d
                    data['btc_price_source'] = 'ticker_override'
                else:
                    data['btc_price_source'] = 'klines'
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
        return False, (f"BTC bawah Daily EMA21 "
                       f"(BTC=${state['btc_price']:,.0f} < EMA21=${state['btc_ema21d']:,.0f})")
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

# ==========================================
# CAP CATEGORY — berdasarkan volume 24H Binance (real, tiada API tambahan)
# ==========================================
def get_cap_category(symbol):
    """
    Kategorikan coin berdasarkan volume 24H dari latest_prices.
    Guna data yang sudah ada dalam WebSocket — tiada API call tambahan.
    Volume 24H > $50M = LARGE CAP
    Volume 24H $5M-$50M = MID CAP
    Volume 24H < $5M   = SMALL CAP
    """
    data = latest_prices.get(symbol, {})
    vol  = data.get("q", 0)  # quote volume 24H dalam USDT
    if vol >= 50_000_000:
        return "LARGE CAP"
    elif vol >= 5_000_000:
        return "MID CAP"
    elif vol > 0:
        return "SMALL CAP"
    return "—"

def calculate_position_size(capital, risk_pct, entry, sl):
    """
    Position size dengan double cap:
    1. Cap kepada capital (no leverage)
    2. Warn jika melebihi 50% capital dalam satu trade
    """
    risk_usd = capital * (risk_pct / 100.0)
    risk_distance = entry - sl
    if risk_distance <= 0:
        return 0, 0, 0
    position_usd_raw = risk_usd / (risk_distance / entry)
    # Cap 1: tidak melebihi modal penuh
    position_usd = min(position_usd_raw, capital)
    # Cap 2: tidak melebihi 50% modal dalam satu trade (risk management)
    position_usd = min(position_usd, capital * 0.50)
    position_coins = position_usd / entry
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
    sl_atr_mult = t.get('sl_atr_mult', 2.0)
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
    """Keyboard minimalis — 3 butang sahaja."""
    base = symbol[:-4]
    markup = InlineKeyboardMarkup(row_width=3)
    markup.row(
        InlineKeyboardButton("📈 Chart", url=f"https://www.tradingview.com/chart/?symbol=BINANCE:{symbol}"),
        InlineKeyboardButton("⚡ Trade", url=f"https://www.binance.com/en/trade/{symbol}"),
        InlineKeyboardButton("🐦 News",  url=f"https://x.com/search?q=%24{base}&f=live"),
    )
    return markup

# ==========================================
# DISPATCH SIGNAL — V8: Macro filter + ATR SL + capital cap + confirmation queue
# ==========================================
def dispatch_signal(symbol, price, sig, ind, engine_type, daily_note,
                    user_cap, user_risk, from_pending=False):
    if not bot or not TELEGRAM_CHAT_ID or check_cooldown(symbol):
        return

    t = get_tuning()

    # MACRO — info sahaja, tidak block
    macro_ok, macro_reason = macro_filter_pass(engine_type, t)
    macro_state  = get_btc_macro_state()
    macro_btc_pct = macro_state['btc_24h_pct'] if macro_state else 0.0
    macro_warn   = f"  ⚠️ {macro_reason}" if not macro_ok else ""
    if not macro_ok:
        log_activity(f"{symbol} ⚠️ Macro Warning: {macro_reason}")

    # ── SL ────────────────────────────────────────────────────────────────────
    structure_low = sig['low']
    atr = sig.get('atr', 0)

    if engine_type == 'SWEEP_REVERSAL':
        sl  = sig.get('sl_raw', compute_final_sl(price, structure_low, atr, t))
        sl  = max(sl, price * 0.90)
    elif engine_type == 'REENTRY':
        # SL dikira dalam reentry_monitor (bawah zone bottom)
        sl  = sig.get('reentry_sl', compute_final_sl(price, structure_low, atr, t))
    else:
        sl  = compute_final_sl(price, structure_low, atr, t)

    risk = price - sl
    if risk <= 0:
        logger.warning(f"{symbol}: SL >= entry, abort dispatch")
        return

    # ── TP — Structure-based untuk semua engine ───────────────────────────────
    if engine_type == 'SWEEP_REVERSAL':
        range_high  = sig.get('range_high', price * 1.10)
        range_size  = sig.get('range_size', risk * 2)
        tp1 = price + (range_high - price) * 0.50
        tp2 = range_high
        tp3 = range_high + range_size * 0.618

    elif engine_type == 'BREAKOUT':
        measured = sig.get('structure_measured', risk * 2)
        if measured <= 0: measured = risk * 2
        tp1 = price + measured * 0.50
        tp2 = price + measured * 1.00
        tp3 = price + measured * 1.618

    else:  # ACCUMULATION / RETEST — fresh zone TP
        smc_data   = sig.get('smc') or {}
        fresh_m15  = smc_data.get('fresh_m15', [])
        fresh_h1   = smc_data.get('fresh_h1',  [])

        # Swing high fallback jika tiada fresh zone
        swing_high = max(ind.highs[-50:]) if len(ind.highs) >= 50 else price * 1.10

        # TP1 — nearest M15 fresh zone bot
        # Fallback: nearest H1 zone atau 1.5R
        if fresh_m15:
            tp1 = fresh_m15[0]['bot']
        elif fresh_h1:
            tp1 = fresh_h1[0]['bot']
        else:
            tp1 = price + risk * 1.5

        # TP2 — nearest H1 fresh zone bot
        # Fallback: second M15 zone atau 3R
        if fresh_h1:
            tp2 = fresh_h1[0]['bot']
        elif len(fresh_m15) >= 2:
            tp2 = fresh_m15[1]['bot']
        else:
            tp2 = price + risk * 3.0

        # TP3 — second H1 zone atau swing high
        if len(fresh_h1) >= 2:
            tp3 = fresh_h1[1]['bot']
        elif fresh_h1:
            tp3 = fresh_h1[0]['top']   # atas zon H1 pertama
        else:
            tp3 = swing_high           # swing high 50 candle H1

        # Pastikan TP1 < TP2 < TP3 dan semua atas entry
        if tp1 <= price: tp1 = price + risk * 1.0
        if tp2 <= tp1:   tp2 = tp1 + risk * 0.5
        if tp3 <= tp2:   tp3 = tp2 + risk * 0.5

    rr1 = (tp1 - price) / risk if risk > 0 else 0
    rr2 = (tp2 - price) / risk if risk > 0 else 0
    rr3 = (tp3 - price) / risk if risk > 0 else 0

    # ── Filter 1: Stablecoin / Pegged Asset ───────────────────────────────────
    bb_now = sig.get('bb', 0) or (ind.get_bb_width() if hasattr(ind,'get_bb_width') else 0)
    if 0.98 <= price <= 1.02 and bb_now < 0.5:
        logger.info(f"[STABLECOIN FILTER] {symbol} ditolak: harga ${price:.4f} ≈ $1.00, BB {bb_now:.2f}%")
        log_activity(f"{symbol} ❌ Stablecoin/pegged — diabaikan")
        bump_stat('rejected')
        save_cooldown(symbol, 48)   # stable — block lama
        return

    # ── Filter 2: Minimum TP1 jarak 1.5% ─────────────────────────────────────
    tp1_pct = (tp1 - price) / price * 100 if price > 0 else 0
    if tp1_pct < 1.5:
        logger.info(f"[TP1 FILTER] {symbol} ditolak: TP1 hanya +{tp1_pct:.2f}% (minimum 1.5%)")
        log_activity(f"{symbol} ❌ TP1 terlalu rendah ({tp1_pct:.1f}% < 1.5%)")
        bump_stat('rejected')
        save_cooldown(symbol, float(t.get('fail_cooldown_h', 0.33)))
        return

    # ── Filter 3: Max 1 open trade per coin ───────────────────────────────────
    # Elak coin yang sama dibuka berkali-kali semasa crash.
    # REENTRY dikecualikan — trade asal sudah STOP_LOSS (tiada dalam active),
    # jadi check ini akan pass secara semula jadi untuk re-entry.
    if engine_type != 'REENTRY':
        active_symbols = [t['symbol'] for t in get_active_trades()]
        if symbol in active_symbols:
            logger.info(f"[MAX 1 PER COIN] {symbol} ditolak: sudah ada open trade")
            log_activity(f"{symbol} ❌ Sudah ada open trade")
            bump_stat('rejected')
            save_cooldown(symbol, float(t.get('fail_cooldown_h', 0.33)))
            return

    # ── Filter 4: Circuit Breaker — BTC crash harian ──────────────────────────
    # Jika BTC turun > 4% dalam 24 jam, pause ACCUMULATION sepenuhnya.
    # BREAKOUT, SWEEP, RETEST, REENTRY masih boleh keluar.
    if engine_type == 'ACCUMULATION':
        _macro = get_btc_macro_state()
        if _macro and _macro['btc_24h_pct'] < -4.0:
            logger.info(
                f"[CIRCUIT BREAKER] {symbol} ACCUM ditolak: "
                f"BTC {_macro['btc_24h_pct']:+.2f}% (had -4.0%)")
            log_activity(
                f"{symbol} ❌ Circuit breaker: BTC {_macro['btc_24h_pct']:+.2f}%")
            bump_stat('rejected')
            save_cooldown(symbol, 1.0)   # 1 jam — BTC condition berubah perlahan
            return

    pos_usd, pos_coins, risk_usd = calculate_position_size(
        user_cap, user_risk, price, sl)

    # ── Labels ────────────────────────────────────────────────────────────────
    labels = {
        'BREAKOUT':       ('🚀', 'BREAKOUT'),
        'RETEST':         ('🎯', 'RETEST'),
        'SWEEP_REVERSAL': ('🎣', 'SWEEP'),
        'ACCUMULATION':   ('🕵️', 'ACCUM'),
        'REENTRY':        ('🔄', 'RE-ENTRY'),
    }
    emoji, cat_short = labels.get(engine_type, ('📡', engine_type))

    score     = sig.get('score', 5)
    score_max = sig.get('score_max', 5)
    bar       = "●" * score + "○" * (score_max - score)

    # Cap category dari WebSocket volume (tiada API)
    coin_cat = get_cap_category(symbol)
    validity = round(score / score_max * 100) if score_max > 0 else 0

    # Syarat gagal — tunjuk yang PERTAMA GAGAL sahaja, pendek
    failed_line = ""
    if score < score_max and sig.get('conditions'):
        for cname, passed in sig['conditions'].items():
            if not passed:
                short = cname.split('[')[0].strip()[:28]
                # Escape HTML tapi JAGA >= dan <= (tukar kepada entiti dulu)
                short = short.replace('>=', '≥').replace('<=', '≤')
                short = short.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                failed_line = f"     ✗  {short}\n"
                break

    # Desc baris — TANPA BB (BB ada di bawah bersama RSI/RVOL)
    if engine_type == 'SWEEP_REVERSAL':
        patt = sig.get('pattern', '?')
        entry_cat = patt   # HAMMER / ENGULFING / MORNING STAR dll
        desc = f"     Sweep  <code>${sig.get('sweep_low',0):.6f}</code>"
    elif engine_type in ('BREAKOUT', 'RETEST'):
        entry_cat = engine_type
        desc = f"     Break  <code>${sig.get('break_level',0):.6f}</code>"
    else:
        entry_cat = 'ACCUM ZONE'
        desc = ""   # kosong — BB sudah ada di baris info

    # BTC — satu baris pendek, tidak wrap
    btc_line = ""
    if macro_state:
        arrow = "🟢" if macro_state['btc_24h_pct'] >= 0 else "🔴"
        warn  = "  ⚠️" if not macro_ok else ""
        btc_line = f"🌐  BTC {arrow} <code>{macro_state['btc_24h_pct']:+.2f}%</code>{warn}\n"

    # RSI · RVOL · BB — satu baris
    bb_val = sig.get('bb', 0) or (ind.get_bb_width() if hasattr(ind,'get_bb_width') else 0)

    # SMC context
    smc_ctx  = sig.get('smc')
    htf_line = ""
    ob_line  = ""
    fvg_line = ""
    if smc_ctx:
        d1 = "🟢" if smc_ctx['htf_1d'] == 'BULL' else ("🔴" if smc_ctx['htf_1d'] == 'BEAR' else "⚪")
        h4 = "🟢" if smc_ctx['htf_4h'] == 'BULL' else ("🔴" if smc_ctx['htf_4h'] == 'BEAR' else "⚪")
        bc_type  = smc_ctx['bos_choch'].get('type', 'NEUTRAL')
        bc_label = smc_ctx['bos_choch'].get('label', 'Ranging')
        m15_warn = "  ⚠️" if bc_type in ('CHOCH_BEAR', 'BOS_BEAR') else ""
        htf_line = f"📐  1D {d1}  4H {h4}  ·  M15 {bc_label}{m15_warn}\n"
        fvg = smc_ctx.get('fvg', {})
        ob  = smc_ctx.get('ob', {})
        if fvg.get('found'):
            fvg_line = (f"🌀  FVG  <code>${fvg['bot']:.6f}</code>–"
                        f"<code>${fvg['top']:.6f}</code>"
                        f"  [{fvg['dist_pct']:.1f}%]\n")
        if ob.get('found'):
            zone_tag = "  ✦ DALAM ZONE" if ob.get('in_zone') else ""
            ob_line  = (f"📦  OB   <code>${ob['bot']:.6f}</code>–"
                        f"<code>${ob['top']:.6f}</code>{zone_tag}\n")

    # Re-entry context (hanya untuk REENTRY engine)
    reentry_line = ""
    if engine_type == 'REENTRY':
        zone = sig.get('reentry_zone', '—')
        patt = sig.get('reentry_pattern', '—')
        orig = sig.get('original_engine', '—')
        reentry_line = (f"🔄  Re-entry dari {orig}\n"
                        f"     Zon: {zone}  ·  M15: {patt}\n")

    # Full category name
    cat_full = {
        'BREAKOUT':       'BREAKOUT',
        'RETEST':         'RETEST',
        'SWEEP_REVERSAL': 'SWEEP REVERSAL',
        'ACCUMULATION':   'ACCUMULATE ZONE',
    }.get(engine_type, engine_type)

    SEP = "—" * 17

    msg = (
        f"{emoji}  <code>{symbol}</code>  ●  Nova7  ●\n"
        f"{SEP}\n"
        f"📊    <code>{bar}</code>  {score}/{score_max}\n"
        f"{failed_line}"
        f"{SEP}\n"
        f"💵   <code>${price:.6f}</code>\n"
        f"🛑   <code>${sl:.6f}</code>  {((sl-price)/price*100):+.1f}%\n"
        f"🎯   <code>${tp1:.6f}</code>  {((tp1-price)/price*100):+.1f}%  {rr1:.1f}R\n"
        f"🎯   <code>${tp2:.6f}</code>  {((tp2-price)/price*100):+.1f}%  {rr2:.1f}R\n"
        f"🎯   <code>${tp3:.6f}</code>  {((tp3-price)/price*100):+.1f}%  {rr3:.1f}R\n"
        f"{SEP}\n"
        f"{btc_line}"
        f"{htf_line}"
        f"{reentry_line}"
        f"{ob_line}"
        f"{fvg_line}"
        f"{SEP}\n"
        f"     RSI {ind.rsi:.0f}  ·  RVOL {sig['rvol']:.2f}x  ·  BB {bb_val:.1f}%\n"
        f"{'     🟢 Recovery Candle' + chr(10) if sig.get('recovery_candle') else ''}"
        f"     {cat_full}  ·  {validity}%"
    )

    try:
        sent = bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="HTML",
                                reply_markup=build_keyboard(symbol),
                                disable_web_page_preview=True)
        save_trade(sent.message_id, symbol, price, sl, tp1, tp2, tp3,
                   engine_type, macro_btc_pct=macro_btc_pct)
        save_cooldown(symbol,
                      t.get('cd_breakout', 24) if engine_type == 'BREAKOUT'
                      else t.get('cd_accumulation', 48))
        logger.info(f"✅ [SIGNAL] {symbol} ({engine_type}) dispatched.")
        log_activity(f"{symbol} 🚀 SIGNAL SENT ({engine_type})")
        bump_stat('signals_sent')
    except Exception as e:
        logger.error(f"Dispatch error {symbol}: {e}")

# ==========================================
# POST-MORTEM AUTOPSY (kekal, minor fix vols slice)
# ==========================================
# Semaphore: hadkan _check_reentry_opportunity concurrent
# Semasa crash BTC, 10-20 SL hits serentak = 10-20 aiohttp sessions tanpa had
# Max 2 cukup — reentry bukan masa-kritikal, boleh tunggu
_reentry_check_sem = asyncio.Semaphore(2) if False else None  # lazy init

async def _check_reentry_opportunity(symbol, sl_price, engine):
    """
    Dipanggil selepas SL hit. Semak jika ada OB/FVG di bawah SL.
    Jika ada → masuk reentry_watchlist (7 hari TTL, max 1 re-entry).
    Jika tiada → cooldown biasa dikekali.
    Semaphore(2): hadkan concurrent checks — elak OOM semasa crash.
    """
    global _reentry_check_sem
    if _reentry_check_sem is None:
        try:
            _reentry_check_sem = asyncio.Semaphore(2)
        except RuntimeError:
            pass
    sem = _reentry_check_sem
    try:
        if sem:
            async with sem:
                await _do_reentry_check(symbol, sl_price, engine)
        else:
            await _do_reentry_check(symbol, sl_price, engine)
    except Exception as e:
        logger.debug(f"[REENTRY] {symbol} outer error: {e}")

async def _do_reentry_check(symbol, sl_price, engine):
    """Inner logic untuk _check_reentry_opportunity."""
    try:
        async with aiohttp.ClientSession() as session:
            url = (f"https://api.binance.com/api/v3/klines"
                   f"?symbol={symbol}&interval=1h&limit=100")
            async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                data = await r.json()
        if not data or len(data) < 20:
            return
        opens  = [float(d[1]) for d in data]
        closes = [float(d[4]) for d in data]
        highs  = [float(d[2]) for d in data]
        lows   = [float(d[3]) for d in data]

        # Detect OB dan FVG yang berada di bawah SL hit price
        ob  = _smc_instance._detect_order_block(opens, closes, highs, lows)
        fvg = _smc_instance._detect_fvg(highs, lows, closes)

        ob_top  = ob['top']  if ob.get('found')  and ob['top']  < sl_price else 0
        ob_bot  = ob['bot']  if ob_top  > 0 else 0
        fvg_top = fvg['top'] if fvg.get('found') and fvg['top'] < sl_price else 0
        fvg_bot = fvg['bot'] if fvg_top > 0 else 0

        if ob_top > 0 or fvg_top > 0:
            save_reentry_watch(symbol, engine, sl_price,
                               ob_top, ob_bot, fvg_top, fvg_bot)
            zones = []
            if ob_top  > 0: zones.append(f"OB ${ob_bot:.6f}–${ob_top:.6f}")
            if fvg_top > 0: zones.append(f"FVG ${fvg_bot:.6f}–${fvg_top:.6f}")
            log_activity(f"{symbol} 📍 Re-entry watch: {' | '.join(zones)}")
            logger.info(f"[REENTRY] {symbol} masuk watchlist — {' | '.join(zones)}")
        else:
            logger.debug(f"[REENTRY] {symbol}: tiada OB/FVG bawah SL, cooldown biasa")
    except Exception as e:
        logger.debug(f"[REENTRY] {symbol} check error: {e}")


async def reentry_monitor():
    """
    Monitor setiap 5 minit. Bila harga masuk OB/FVG zone:
    → Semak M15 untuk konfirmasi reversal
    → CHoCH ↑ / BOS ↑ / Bullish Engulfing + RVOL ≥ 1.5x
    → Hantar signal 🔄 RE-ENTRY
    → Buang dari watchlist, cooldown biasa selepas tu
    """
    logger.info("✅ [REENTRY] Re-entry monitor started.")
    await asyncio.sleep(60)   # bagi masa sistem stabilize dulu
    while True:
        await asyncio.sleep(300)   # setiap 5 minit
        try:
            watches = get_reentry_watchlist()
            if not watches:
                continue
            t = get_tuning()
            user_cap, user_risk = get_user_capital(
                int(TELEGRAM_CHAT_ID) if TELEGRAM_CHAT_ID else 0)

            for w in watches:
                symbol = w['symbol']
                now    = time.time()

                # Expiry check — 7 hari
                if now > w['expiry']:
                    drop_reentry_watch(symbol)
                    save_cooldown(symbol, 24)
                    log_activity(f"{symbol} ⏰ Re-entry watchlist expired")
                    continue

                # Ambil harga semasa dari WebSocket cache
                cur = latest_prices.get(symbol)
                if not cur:
                    continue
                cur_price = cur['c']

                # Tentukan zon aktif
                ob_zone  = (w['ob_bot'],  w['ob_top'])  if w['ob_top']  > 0 else None
                fvg_zone = (w['fvg_bot'], w['fvg_top']) if w['fvg_top'] > 0 else None

                in_ob  = ob_zone  and ob_zone[0]  <= cur_price <= ob_zone[1]
                in_fvg = fvg_zone and fvg_zone[0] <= cur_price <= fvg_zone[1]

                if not (in_ob or in_fvg):
                    continue   # Harga belum sampai zone

                zone_name = "OB" if in_ob else "FVG"
                active_bot = w['ob_bot'] if in_ob else w['fvg_bot']
                log_activity(f"{symbol} 📍 Harga masuk {zone_name} → M15 analisis")

                # ── M15 konfirmasi reversal ───────────────────────────────────
                try:
                    async with aiohttp.ClientSession() as session:
                        klines_m15 = await _smc_instance._fetch_m15(
                            symbol, session)
                    if not klines_m15 or len(klines_m15) < 10:
                        continue

                    bos_choch = _smc_instance._detect_bos_choch(klines_m15)
                    bc_type   = bos_choch.get('type', 'NEUTRAL')

                    # Semak candle pattern M15
                    m15_o = [float(d[1]) for d in klines_m15]
                    m15_h = [float(d[2]) for d in klines_m15]
                    m15_l = [float(d[3]) for d in klines_m15]
                    m15_c = [float(d[4]) for d in klines_m15]
                    m15_v = [float(d[7]) for d in klines_m15]

                    pattern = CandlePatternDetector().detect(
                        m15_o, m15_c, m15_h, m15_l, lookback=3)

                    # M15 RVOL
                    avg_vol   = sum(m15_v[-21:-1]) / 20 if len(m15_v) >= 21 else 1
                    m15_rvol  = m15_v[-1] / avg_vol if avg_vol > 0 else 0

                    strong_struct   = bc_type in ('CHOCH_BULL', 'BOS_BULL')
                    strong_pattern  = (pattern.get('found') and
                                       pattern.get('pattern') in
                                       ('HAMMER','ENGULFING','MORNING STAR'))
                    vol_ok          = m15_rvol >= 1.5

                    if not ((strong_struct or strong_pattern) and vol_ok):
                        logger.debug(
                            f"[REENTRY] {symbol}: dalam zone tapi konfirmasi belum cukup"
                            f" (struct={strong_struct} pat={strong_pattern} rvol={m15_rvol:.2f}x)")
                        continue

                    # Kumpul pattern labels untuk info box
                    found_labels = []
                    if strong_struct:
                        found_labels.append(bos_choch.get('label',''))
                    if strong_pattern:
                        found_labels.append(pattern.get('pattern',''))
                    m15_confirm_str = " · ".join(filter(None, found_labels))

                    # ── Fetch 1H data untuk dispatch ─────────────────────────
                    async with aiohttp.ClientSession() as session:
                        url = (f"https://api.binance.com/api/v3/klines"
                               f"?symbol={symbol}&interval=1h&limit=100")
                        async with session.get(
                                url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                            h1_data = await r.json()
                        if not h1_data or len(h1_data) < 51:
                            continue

                        h1_o = [float(d[1]) for d in h1_data]
                        h1_c = [float(d[4]) for d in h1_data]
                        h1_h = [float(d[2]) for d in h1_data]
                        h1_l = [float(d[3]) for d in h1_data]
                        h1_v = [float(d[7]) for d in h1_data]

                        ind = IncrementalIndicators()
                        if not ind.initialize(h1_o, h1_c, h1_h, h1_l, h1_v):
                            continue

                        # SMC analysis untuk TP targets (fresh zones)
                        smc_ctx = await _smc_instance.analyze(
                            symbol, ind, session)

                    # SL = 0.5% bawah zone bottom
                    sl_reentry = active_bot * 0.995
                    sl_reentry = max(sl_reentry, cur_price * 0.92)  # cap 8%

                    rvol = ind.get_rvol()

                    sig = {
                        'type':             'REENTRY',
                        'rvol':             rvol,
                        'low':              active_bot * 0.995,
                        'atr':              ind.atr,
                        'score':            5,
                        'score_max':        5,
                        'conditions':       {},
                        'smc':              smc_ctx,
                        'reentry_zone':     zone_name,
                        'reentry_pattern':  m15_confirm_str,
                        'reentry_sl':       sl_reentry,
                        'ob_zone':          ob_zone,
                        'fvg_zone':         fvg_zone,
                        'original_engine':  w['original_engine'],
                    }

                    # Bersihkan cooldown supaya signal boleh keluar
                    with db_lock, sqlite3.connect(DB_NAME) as conn:
                        conn.execute(
                            "DELETE FROM cooldowns WHERE symbol=?", (symbol,))

                    log_activity(
                        f"{symbol} 🔄 RE-ENTRY dispatch ({zone_name} + M15: {m15_confirm_str})")
                    dispatch_signal(symbol, cur_price, sig, ind,
                                    'REENTRY', '', user_cap, user_risk)

                    # Buang dari watchlist selepas signal dihantar
                    drop_reentry_watch(symbol)
                    save_cooldown(symbol, 24)

                except Exception as e:
                    logger.error(f"[REENTRY] {symbol} monitor error: {e}")

        except Exception as e:
            logger.error(f"[REENTRY MONITOR] Loop error: {e}")


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
    global is_scanning, _layer2_sem, latest_prices, radar_history
    # Init semaphore dalam event loop ini
    t_init = get_tuning()
    _layer2_sem = asyncio.Semaphore(int(t_init.get('layer2_concurrency', 3)))
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
                        delta_signals  = snap['signals_sent'] - pulse_stats.get('prev_signals', 0)
                        delta_rejected = snap['rejected'] - pulse_stats.get('prev_rejected', 0)
                        logger.info(f"💓 [PULSE] Radar: {pulse_stats['seen']} coins | Promoted: {pulse_stats['promoted']} | Signals: {delta_signals} | Rejected: {delta_rejected}")
                        if activity_log:
                            logger.info(f"📋 [RECENT] {' | '.join(activity_log[-5:])}")
                        last_pulse = now
                        pulse_stats = {
                            'promoted': 0, 'seen': 0,
                            'prev_signals': snap['signals_sent'],
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
                        # Skip stablecoin/pegged yang tidak dikenali dalam KILL_LIST
                        # Harga $0.97–$1.03 = hampir pasti pegged asset
                        if 0.97 <= c <= 1.03:
                            continue
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
                                momentum = t.get('radar_momentum', 2.0)
                                min_vol = t.get('radar_min_vol', 12_000_000)
                                # FIX: check cooldown SEBELUM promote — elak spam repeat
                                if change >= momentum and q > min_vol and not check_cooldown(sym):
                                    with queue_lock:
                                        if sym not in layer2_queue:
                                            layer2_queue.add(sym)
                                            promote_breakout = True
                                    if promote_breakout:
                                        pulse_stats['promoted'] += 1
                                        log_activity(f"{sym} ↑{change:.1f}% → Layer 2")
                                        asyncio.create_task(layer2_sniper(sym, 'BREAKOUT'))
                                        # Tambah ke retest watchlist — pantau pullback
                                        with _retest_lock:
                                            _retest_watchlist[sym] = {
                                                'price_at_promote': c,
                                                'time': now
                                            }

                    if now - last_scheduled >= 7200:
                        last_scheduled = now
                        sorted_syms = sorted(latest_prices.keys(),
                            key=lambda s: latest_prices[s]['q'], reverse=True)
                        # Stagger: hantar 1 task setiap 2 saat supaya tidak queue 50 task serentak
                        # 50 task × 2s = 100s untuk habis queue — selamat untuk RAM
                        async def _staggered_acc_scan(syms):
                            for s in syms:
                                if check_cooldown(s):
                                    continue
                                with queue_lock:
                                    if s not in layer2_queue:
                                        layer2_queue.add(s)
                                        asyncio.create_task(
                                            layer2_sniper(s, 'ACCUMULATION'))
                                await asyncio.sleep(2)
                                # Sweep reversal juga discan untuk coin yang sama
                                # Stagger 1 saat selepas accumulation scan
                                with queue_lock:
                                    sweep_key = f"{s}_SWEEP"
                                    if sweep_key not in layer2_queue:
                                        layer2_queue.add(sweep_key)
                                        asyncio.create_task(
                                            layer2_sniper(s, 'SWEEP_REVERSAL'))
                                await asyncio.sleep(1)
                        asyncio.create_task(_staggered_acc_scan(sorted_syms[:25]))

                    set_stat('radar_coins', len(latest_prices))
        except Exception as e:
            logger.error(f"❌ [RADAR] Disconnected: {e}. Reconnecting...")
            await asyncio.sleep(5)

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
            layer2_queue.discard(f"{symbol}_SWEEP")

async def _layer2_sniper_impl(symbol, scan_type, force, chat_id, user_cap, user_risk):
    t = get_tuning()
    if not force and check_cooldown(symbol):
        return
    async with aiohttp.ClientSession() as session:
        # 100 candle cukup untuk EMA50 stable (perlu min 50 warmup + 50 active)
        # 150 sebelum ini = +50% data = +RAM sia-sia
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit=100"
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
        elif scan_type == 'RETEST':
            sig, conditions = RetestHunter().check(ind, t)
        elif scan_type == 'SWEEP_REVERSAL':
            sig, conditions = SweepReversalEngine().check(ind, t)
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

        chart_buf = None  # Chart dibuang — jimat ~150MB RAM per signal

        if sig:
            # Daily confluence — info sahaja, tidak block signal
            loop = asyncio.get_event_loop()
            daily_filter_on = int(t.get('bo_daily_filter', 1)) == 1
            daily_ok, daily_note = True, "Filter OFF"
            if daily_filter_on:
                daily_ok, daily_note = await loop.run_in_executor(
                    None, check_daily_confluence, symbol, closes[-1])
            if daily_filter_on and not daily_ok and not force:
                log_activity(f"{symbol} ❌ Daily: {daily_note[:40]}")
                bump_stat('rejected')
                save_cooldown(symbol, float(t.get('fail_cooldown_h', 0.33)))
                return

            # SMC Analysis — jalankan selepas sig confirmed
            # Guna session yang sama untuk jimat connection
            try:
                async with aiohttp.ClientSession() as smc_session:
                    smc_ctx = await _smc_instance.analyze(symbol, ind, smc_session)
                sig['smc'] = smc_ctx
            except Exception as e:
                logger.debug(f"[SMC] {symbol} skipped: {e}")
                sig['smc'] = None

            log_activity(f"{symbol} ✅ {scan_type} {sig['score']}/{sig['score_max']} → SENDING...")
            dispatch_signal(symbol, closes[-1], sig, ind, scan_type, daily_note,
                            user_cap, user_risk)
        else:
            # Log kenapa gagal (ringkas) + cooldown ringan untuk elak repeat scan
            failed = [k for k, v in conditions.items() if not v]
            if failed:
                main_reason = failed[0].split('[')[0].strip()[:40]
                log_activity(f"{symbol} ❌ {main_reason}")
                bump_stat('rejected')
                if not force:
                    save_cooldown(symbol, float(t.get('fail_cooldown_h', 0.33)))  # V8: mini cooldown

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
        # Dispatch dengan from_pending=True (skip queue loop)
        dispatch_signal(p['symbol'], closes[-1], sig, ind, 'ACCUMULATION',
                        None, p['daily_note'], p['user_cap'], p['user_risk'],
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
                if now > p['expiry']:
                    drop_pending(sym)
                    save_cooldown(sym, float(get_tuning().get('fail_cooldown_h', 0.33)))
                    log_activity(f"{sym} ⏰ Pending expired")
                    continue
                if now - p['detect_time'] < 3600:
                    continue
                confirmed, reason = await check_confirmation_async(dict(p))
                if confirmed is None:
                    continue
                if confirmed:
                    log_activity(f"{sym} ✓ Confirmed: {reason}")
                    await dispatch_from_pending(dict(p))
                    drop_pending(sym)
                else:
                    drop_pending(sym)
                    save_cooldown(sym, float(get_tuning().get('fail_cooldown_h', 0.33)))
                    log_activity(f"{sym} ✗ Conf gagal: {reason}")
        except Exception as e:
            logger.error(f"Pending processor error: {e}")

# ==========================================
# RETEST SCANNER — Tangkap Entry Awal di Zon Support
# ==========================================
# Simpan rekod coin yang pernah ada momentum breakout (promoted ke Layer 2)
# Pantau selama 4 jam untuk retest entry
_retest_watchlist = {}  # {symbol: {'price_at_promote': float, 'time': float}}
_retest_lock = threading.Lock()

async def retest_scanner():
    """
    Jalan setiap 3 minit. Pantau coin dalam _retest_watchlist.
    Bila coin pullback ke zon resistance lama → scan dengan RetestHunter.

    Kenapa perlu scanner berasingan?
    Layer 1 radar cuma promote bila ada MOMENTUM (naik 2%).
    Retest berlaku semasa harga TURUN balik ke level — tiada momentum,
    Layer 1 tidak akan promote. Scanner ini mengisi jurang itu.
    """
    global _layer2_sem
    if _layer2_sem is None:
        _layer2_sem = asyncio.Semaphore(int(get_tuning().get('layer2_concurrency', 3)))

    logger.info("✅ [RETEST] Retest scanner started.")
    while True:
        await asyncio.sleep(180)  # setiap 3 minit
        try:
            now = time.time()
            with _retest_lock:
                # Buang entry lama > 4 jam
                stale = [s for s, d in _retest_watchlist.items()
                         if now - d['time'] > 14400]
                for s in stale:
                    del _retest_watchlist[s]
                candidates = list(_retest_watchlist.items())

            if not candidates:
                continue

            t = get_tuning()
            for sym, meta in candidates:
                if check_cooldown(sym):
                    with _retest_lock:
                        _retest_watchlist.pop(sym, None)
                    continue

                # Semak harga semasa dari latest_prices (tiada HTTP)
                cur_data = latest_prices.get(sym)
                if not cur_data:
                    continue
                cur_price = cur_data['c']
                promote_price = meta['price_at_promote']

                # Hanya scan kalau harga dah turun balik ≥ 1.5% dari promote price
                # (bermakna ada pullback, mungkin ada retest)
                pullback_pct = (promote_price - cur_price) / promote_price * 100
                if pullback_pct < 1.5:
                    continue

                logger.debug(f"[RETEST] {sym} pullback {pullback_pct:.1f}% → scanning")
                # WAJIB guna semaphore — jangan bypass seperti create_task biasa
                # Tanpa semaphore, 20 retest tasks boleh spawn serentak = OOM
                with queue_lock:
                    if sym not in layer2_queue:
                        layer2_queue.add(sym)
                        asyncio.create_task(
                            layer2_sniper(sym, 'RETEST',
                                          force=False, chat_id=None,
                                          user_cap=1000.0, user_risk=2.0))

        except Exception as e:
            logger.error(f"[RETEST SCANNER] Error: {e}")

# ==========================================
# TRADE TRACKER — V8: SL move to BE on TP1, record exit_price for journal
# ==========================================
# Guard terhadap duplicate SL/TP notification.
# msg_id dimasukkan serta-merta bila status ditetapkan,
# sebelum DB update — elak iterasi 5-saat berikut proses semula.
_notified_trades: set = set()

async def trade_tracker():
    """
    Trade tracker dengan REST fallback.
    Bila WebSocket baru connect, latest_prices kosong untuk beberapa saat.
    Tracker kini fetch harga dari REST bila symbol tiada dalam cache.
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
            # Ambil harga dari WebSocket cache dulu, fallback ke REST
            if sym in latest_prices:
                price = latest_prices[sym]['c']
            else:
                # REST fallback — lambat sikit tapi reliable semasa startup
                try:
                    r = requests.get(
                        f"https://api.binance.com/api/v3/ticker/price?symbol={sym}",
                        timeout=5)
                    price = float(r.json().get('price', 0))
                    if price <= 0:
                        continue
                except Exception:
                    continue
            status = t['status']
            reply = None
            new_status = status
            new_sl = None
            record_exit = False

            # Bina kunci unik: msg_id + status_yang_akan_ditukar
            # Ini halang iterasi seterusnya proses trade yang sama
            # sebelum DB update sempat commit
            def _guard(new_st):
                key = f"{t['msg_id']}:{new_st}"
                if key in _notified_trades:
                    return True   # sudah diproses
                _notified_trades.add(key)
                return False

            if price <= t['sl'] and status not in ['STOP_LOSS', 'COMPLETED']:
                if _guard('STOP_LOSS'): continue
                loop = asyncio.get_event_loop()
                autopsy = await loop.run_in_executor(None, spot_post_mortem, sym)
                sl_desc = "Proteksi modal" if status == 'TRACKING' else "SL @ BE/Trailing"
                reply = (f"🛑 <b>{sym} — STOP LOSS HIT</b>\n"
                         f"{sl_desc} pada <code>${price:.6f}</code>\n\n"
                         f"🔬 <b>POST-MORTEM:</b>\n{autopsy}")
                new_status = 'STOP_LOSS'
                record_exit = True
                asyncio.ensure_future(
                    _check_reentry_opportunity(sym, price, t['engine']))
            elif price >= t['tp3'] and status != 'COMPLETED':
                if _guard('COMPLETED'): continue
                reply = f"👑 <b>{sym} — TP3 MOONSHOT!</b>\n<code>${price:.6f}</code>"
                new_status = 'COMPLETED'
                record_exit = True
            elif price >= t['tp2'] and status not in ['TP2_HIT', 'COMPLETED']:
                if _guard('TP2_HIT'): continue
                reply = f"🔥 <b>{sym} — TP2 HIT!</b>\nPoketkan 50% di <code>${price:.6f}</code>"
                new_status = 'TP2_HIT'
            elif price >= t['tp1'] and status == 'TRACKING':
                if _guard('TP1_HIT'): continue
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
        acc_losses = [t for t in losses if t['engine'] == 'ACCUMULATION']
        if len(acc_losses) >= 3 and len(acc_losses) / max(1, len(losses)) > 0.6:
            insights.append("⚠️ Majoriti loss dari ACCUMULATION. Pertimbang /tune ketat.")
        if loss_macro and sum(loss_macro)/len(loss_macro) < -1.0:
            insights.append("⚠️ Losses kerap semasa BTC dump. Pertimbang threshold lebih ketat.")
    if win_rate > 50:
        insights.append("✅ Win rate sihat. Kekalkan tuning semasa.")
    if profit_factor != float('inf') and profit_factor < 1.0 and total_closed >= 5:
        insights.append("🔴 Profit factor rendah. Pertimbang /tune ketat dan audit trade.")
    if not insights:
        insights.append("📊 Sample size kecil. Tunggu lebih banyak trade.")

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
            f"• BB Width max: <code>{t.get('acc_bb_width', 20.0):.1f}%</code>\n"
            f"• RVOL threshold: <code>{t.get('acc_rvol', 0.8):.2f}x</code> (floor, bukan spike)\n"
            f"• RSI range: <code>{t.get('acc_rsi_min', 25):.0f}-{t.get('acc_rsi_max', 45):.0f}</code>\n"
            f"<b>🌐 Macro Filter:</b>\n"
            f"• Status: <code>{'ON' if int(t.get('macro_btc_filter', 1)) == 1 else 'OFF'}</code>\n"
            f"• BTC 24h min: <code>{t.get('macro_btc_24h_min', -1.5):+.1f}%</code>\n"
            f"<b>🕒 Confirmation Queue:</b>\n"
            f"• Status: <code>DILUMPUHKAN (signal hantar terus)</code>\n"
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
        t_now = get_tuning()
        bot.reply_to(msg, f"✅ <b>Mode STANDARD aktif.</b>\nBalance antara kualiti & kuantiti.\nacc_bb_width={t_now.get('acc_bb_width',20.0):.1f}% | acc_rvol={t_now.get('acc_rvol',0.8):.2f}x", parse_mode="HTML")
    elif args[0] == 'longgar':
        set_tuning({
            'mode': 1,
            'bo_rvol': 1.4, 'bo_rsi_min': 45, 'bo_rsi_max': 80, 'bo_daily_filter': 0,
            'acc_bb_width': 25.0, 'acc_rvol': 0.6, 'acc_rsi_max': 58, 'acc_rsi_min': 18,
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
            'acc_bb_width': 12.0, 'acc_rvol': 1.0, 'acc_rsi_max': 40, 'acc_rsi_min': 28,
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
            rv_ok  = rvol >= t.get('acc_rvol', 0.8)
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
    t_summary = (f"⚙️ Tuning: BB &lt;{t.get('acc_bb_width',15)}% | "
                 f"RVOL &gt;{t.get('acc_rvol',0.8):.1f}x | "
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
        threading.Thread(target=lambda: asyncio.run(retest_scanner()), daemon=True).start()
        threading.Thread(target=lambda: asyncio.run(reentry_monitor()), daemon=True).start()

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
        threading.Thread(target=lambda: asyncio.run(retest_scanner()), daemon=True).start()
        threading.Thread(target=lambda: asyncio.run(reentry_monitor()), daemon=True).start()

        threading.Thread(target=lambda: asyncio.run(layer1_radar()), daemon=True).start()

        try:
            bot.infinity_polling(skip_pending=True, timeout=30)
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received.")
            graceful_shutdown(None, None)
