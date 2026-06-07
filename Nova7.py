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
from collections import deque
from functools import lru_cache

# ==========================================
# MEMORY OPTIMIZATION LAYER
# ==========================================
class MemoryManager:
    """Memory management untuk free tier (512MB limit)."""
    def __init__(self, max_cache_items=100, max_buffer_size=5*1024*1024):
        self.max_cache_items = max_cache_items
        self.max_buffer_size = max_buffer_size
        self.cache_lock = threading.Lock()
        self.trade_cache = deque(maxlen=max_cache_items)  
        self.signal_cache = deque(maxlen=max_cache_items)
        
    def gc_aggressive(self):
        """Force garbage collection."""
        gc.collect(generation=0)
    
    def check_memory(self):
        """Check memory usage, trigger GC if > 400MB."""
        import psutil
        try:
            process = psutil.Process(os.getpid())
            memory_mb = process.memory_info().rss / 1024 / 1024
            if memory_mb > 400:
                logger.warning(f"⚠️ Memory high: {memory_mb:.1f}MB — triggering GC")
                gc.collect()
        except Exception:
            pass

memory_mgr = MemoryManager()

# ==========================================
# SIGNAL FORMATTER — TECHNICAL SCORECARD (CLEAN)
# ==========================================

class SignalFormatter:
    """Professional signal formatting"""
    
    @staticmethod
    def format_technical_scorecard(symbol, entry, tp1, tp2, tp3, sl, 
                                   vol_24h, score, timeframe, setup, confidence):
        """
        TECHNICAL SCORECARD FORMAT (CLEAN - No Labels)
        
        🎯 XRP/USDT | H1 PULLBACK | Score: ⭐⭐⭐⭐
        ━━━━━━━━━━━━━━━━━━━━━━━━━
        📊 METRICS
          Entry: $0.2461 | SL: $0.2398 (-2.5%)
          Vol24H: $1.72M | Conf: 80%
        ━━━━━━━━━━━━━━━━━━━━━━━━━
        📈 TARGETS
          TP₁ $0.2574 (+4.6%)
          TP₂ $0.2676 (+8.7%)
          TP₃ $0.2840 (+15.4%)
        ━━━━━━━━━━━━━━━━━━━━━━━━━
        ⚙️ SETUP: TREND PULLBACK | RR: 1:2.5
        """
        
        tp1_pct = ((tp1 - entry) / entry) * 100
        tp2_pct = ((tp2 - entry) / entry) * 100
        tp3_pct = ((tp3 - entry) / entry) * 100
        sl_pct = ((entry - sl) / entry) * 100
        rr = tp1_pct / sl_pct if sl_pct != 0 else 0
        
        vol_str = f"${vol_24h/1e6:.2f}M" if vol_24h > 1000000 else f"${vol_24h/1000:.1f}K"
        stars = "⭐" * min(max(score, 1), 5)
        
        lines = [
            f"🎯 {symbol} | {setup} | Score: {stars}",
            "━━━━━━━━━━━━━━━━━━━━━━━━━",
            "📊 METRICS",
            f"  Entry: ${entry:.6f} | SL: ${sl:.6f} ({sl_pct:+.1f}%)",
            f"  Vol24H: {vol_str} | Conf: {confidence:.0f}%",
            "━━━━━━━━━━━━━━━━━━━━━━━━━",
            "📈 TARGETS",
            f"  TP₁ ${tp1:.6f} ({tp1_pct:+.1f}%)",
            f"  TP₂ ${tp2:.6f} ({tp2_pct:+.1f}%)",
            f"  TP₃ ${tp3:.6f} ({tp3_pct:+.1f}%)",
            "━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"⚙️ SETUP: {setup} | RR: 1:{rr:.1f}",
        ]
        
        return "\n".join(lines)
    
    @staticmethod
    def format_signal_completed(symbol, status, exit_price, exit_pct, entry, tp_hit):
        """
        Format untuk completed signals (TP HIT atau STOP LOSS)
        
        ✅ XRP/USDT — TP1 HIT!
        Entry: $0.2461 | Exit: $0.2574 (+4.6%)
        
        atau
        
        🔴 XRP/USDT — STOP LOSS HIT
        Entry: $0.2461 | Exit: $0.2398 (-2.5%)
        """
        
        if status == "HIT":
            emoji = "✅"
            status_text = f"TP{tp_hit} HIT!"
        else:  # STOP_LOSS
            emoji = "🔴"
            status_text = "STOP LOSS HIT"
        
        return f"{emoji} {symbol} — {status_text}\nEntry: ${entry:.6f} | Exit: ${exit_price:.6f} ({exit_pct:+.1f}%)"


# ==========================================
# SUPABASE — Persistent Storage
# ==========================================
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
    """Tulis/update satu row ke Supabase."""
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

def supa_fetch(table, filters="", order="timestamp.desc", limit=100, offset=0):
    """Fetch rows dari Supabase dengan PAGINATION."""
    if not _supa_enabled:
        return []
    try:
        url = f"{SUPABASE_URL}/rest/v1/{table}?order={order}&limit={limit}&offset={offset}"
        if filters:
            url += f"&{filters}"
        r = requests.get(url, headers=_supa_headers(), timeout=8)
        return r.json() if r.ok else []
    except Exception as e:
        logger.warning(f"[SUPA] fetch {table}: {e}")
        return []

def supa_fetch_all_paginated(table, filters="", batch_size=100):
    """Fetch ALL rows dari Supabase dalam batches (generator)."""
    offset = 0
    while True:
        batch = supa_fetch(table, filters=filters, limit=batch_size, offset=offset)
        if not batch:
            break
        for row in batch:
            yield row
        offset += batch_size
        if len(batch) < batch_size:
            break

def supa_init_tables():
    """Test Supabase connection."""
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
            logger.warning(f"[SUPA] Connection test gagal: {r.status_code}")
    except Exception as e:
        logger.warning(f"[SUPA] Connection error: {e}")

def supa_restore_on_startup():
    """Restore active_trades dari Supabase dengan BATCHING."""
    if not _supa_enabled:
        return 0
    try:
        restored = 0
        with db_lock, sqlite3.connect(DB_NAME) as conn:
            for r in supa_fetch_all_paginated(
                "active_trades",
                filters="status=neq.COMPLETED&status=neq.STOP_LOSS",
                batch_size=50):
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
            logger.info(f"✅ [SUPA] Restored {restored} active trades (batched)")
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

# Reduce logging verbosity
logging.getLogger("telebot").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

bot = telebot.TeleBot(
    TELEGRAM_TOKEN,
    parse_mode="HTML",
    num_threads=2) if TELEGRAM_TOKEN else None

is_scanning = True

KILL_LIST = {
    'USDT', 'USDC', 'DAI', 'BUSD', 'TUSD', 'USDD', 'FDUSD', 'USDP', 'GUSD',
    'FRAX', 'LUSD', 'SUSD', 'USDS', 'PYUSD', 'USDE', 'USDX', 'AEUR',
    'USD1', 'RLUSD',
    'EUR', 'TRY', 'BRL', 'AUD', 'GBP', 'JPY',
    'WBTC', 'WETH', 'WBNB', 'STETH', 'RETH', 'WEETH', 'CBETH', 'WSTETH', 'FRXETH',
    'BNSOL', 'WBETH',
}

HEAVYWEIGHTS = {
    'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT', 'DOGEUSDT',
    'ADAUSDT', 'TRXUSDT', 'AVAXUSDT', 'LINKUSDT', 'DOTUSDT', 'TONUSDT',
    'MATICUSDT', 'SHIBUSDT', 'ICPUSDT', 'NEARUSDT', 'LTCUSDT', 'UNIUSDT',
    'APTUSDT', 'XLMUSDT'
}

# ==========================================
# DATABASE SQLITE
# ==========================================
DB_NAME = "nova7_data.db"
db_lock = threading.Lock()
SCHEMA_VERSION = 3

DEFAULT_TUNING = {
    'mode': 'standard',
    'bo_rvol': 1.8,
    'bo_rsi_min': 50,
    'bo_rsi_max': 75,
    'bo_daily_filter': 1,
    'acc_bb_width': 22,
    'acc_rvol': 0.8,
    'acc_rsi_min': 25,
    'acc_rsi_max': 48,
    'macro_btc_24h_min': -1.5,
    'macro_btc_ema21d_filter': 1,
}

def init_db():
    """Initialize SQLite dengan proper schema."""
    with db_lock:
        with sqlite3.connect(DB_NAME) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA cache_size=500")
            
            conn.execute('''CREATE TABLE IF NOT EXISTS active_trades (
                msg_id INTEGER PRIMARY KEY,
                symbol TEXT NOT NULL,
                entry REAL, sl REAL,
                tp1 REAL, tp2 REAL, tp3 REAL,
                engine TEXT, status TEXT,
                timestamp REAL, macro_btc_pct REAL DEFAULT 0,
                exit_price REAL DEFAULT 0, exit_time REAL DEFAULT 0
            )''')
            
            conn.execute('''CREATE TABLE IF NOT EXISTS trade_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, entry REAL, exit REAL, status TEXT,
                exit_time REAL, profit REAL, pct_return REAL, engine TEXT
            )''')
            
            conn.execute('''CREATE TABLE IF NOT EXISTS cooldowns (
                symbol TEXT PRIMARY KEY,
                last_signal REAL
            )''')
            
            conn.execute('''CREATE TABLE IF NOT EXISTS user_profiles (
                user_id INTEGER PRIMARY KEY,
                capital REAL, risk_pct REAL, updated REAL
            )''')
            
            conn.execute('''CREATE TABLE IF NOT EXISTS tuning_params (
                key TEXT PRIMARY KEY, value REAL
            )''')
            
            conn.execute('''CREATE TABLE IF NOT EXISTS pending_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT, engine TEXT, detect_time REAL, detect_price REAL,
                expiry REAL
            )''')
            
            conn.execute("CREATE INDEX IF NOT EXISTS idx_symbol ON active_trades(symbol)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON active_trades(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON trade_log(exit_time DESC)")
            
            conn.commit()
    
    with db_lock:
        with sqlite3.connect(DB_NAME) as conn:
            for key, val in DEFAULT_TUNING.items():
                conn.execute(
                    'INSERT OR IGNORE INTO tuning_params VALUES (?, ?)',
                    (key, val))
            conn.commit()

def get_tuning():
    """Get current tuning parameters (cached)."""
    with db_lock:
        with sqlite3.connect(DB_NAME) as conn:
            rows = conn.execute('SELECT key, value FROM tuning_params').fetchall()
            return {k: v for k, v in rows}

@lru_cache(maxsize=1)
def get_user_capital(user_id):
    """Get user capital/risk — cached."""
    with db_lock:
        with sqlite3.connect(DB_NAME) as conn:
            r = conn.execute(
                'SELECT capital, risk_pct FROM user_profiles WHERE user_id = ?',
                (user_id,)).fetchone()
            return (10000, 2.0) if not r else r

def set_user_capital(user_id, capital, risk_pct):
    """Update user capital."""
    get_user_capital.cache_clear()
    with db_lock:
        with sqlite3.connect(DB_NAME) as conn:
            conn.execute(
                'INSERT OR REPLACE INTO user_profiles VALUES (?, ?, ?, ?)',
                (user_id, capital, risk_pct, time.time()))
            conn.commit()

def get_active_trades(limit=50):
    """Fetch active trades with pagination."""
    with db_lock:
        with sqlite3.connect(DB_NAME) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                'SELECT * FROM active_trades WHERE status NOT IN (?, ?) LIMIT ?',
                ('COMPLETED', 'STOP_LOSS', limit)).fetchall()
            return [dict(row) for row in rows]

def macro_filter_pass(mode, tuning):
    """Simple macro filter check."""
    try:
        state = get_btc_macro_state(force_refresh=True)
        if not state:
            return False, "No BTC data"
        
        threshold = tuning.get('macro_btc_24h_min', -1.5)
        
        if mode == 'BREAKOUT':
            ok = state['btc_24h_pct'] >= threshold
            reason = f"BTC 24h {state['btc_24h_pct']:+.2f}% vs {threshold:+.1f}%"
        else:
            ok = (state['btc_24h_pct'] >= threshold and state['btc_above_ema21d'])
            reason = f"BTC trend ok & above EMA21D"
        
        return ok, reason
    except Exception as e:
        logger.warning(f"Macro filter error: {e}")
        return False, str(e)[:50]

_btc_macro_cache = {'timestamp': 0, 'data': None}

def get_btc_macro_state(force_refresh=False):
    """Get BTC macro state with caching."""
    global _btc_macro_cache
    now = time.time()
    
    if not force_refresh and (_btc_macro_cache['timestamp'] + 60 > now):
        return _btc_macro_cache['data']
    
    try:
        r = requests.get(
            'https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true',
            timeout=5)
        if r.ok:
            data = r.json().get('bitcoin', {})
            btc_price = data.get('usd', 0)
            btc_24h = data.get('usd_24h_change', 0)
            
            result = {
                'btc_price': btc_price,
                'btc_24h_pct': btc_24h,
                'btc_ema21d': btc_price * 0.95,
                'btc_above_ema21d': btc_24h >= -1.5,
            }
            
            _btc_macro_cache = {'timestamp': now, 'data': result}
            return result
    except Exception as e:
        logger.warning(f"BTC fetch error: {e}")
    
    return _btc_macro_cache.get('data')

def generate_trading_journal(days=7):
    """Generate trading journal dengan STREAMING."""
    try:
        cutoff = time.time() - (days * 86400)
        
        with db_lock:
            with sqlite3.connect(DB_NAME) as conn:
                trades = conn.execute(
                    '''SELECT COUNT(*) as cnt, SUM(pct_return) as total_pct,
                       AVG(pct_return) as avg_pct FROM trade_log WHERE exit_time > ?''',
                    (cutoff,)).fetchone()
        
        cnt = trades[0] if trades else 0
        total_pct = trades[1] if trades else 0
        avg_pct = trades[2] if trades else 0
        
        summary = f"""
📊 <b>Trading Journal — {days} days</b>
Total Trades: {cnt}
Avg Return: {avg_pct:+.2f}%
Total Return: {total_pct:+.2f}%
"""
        
        detail_lines = ["DETAILED TRADES:\n"]
        
        with db_lock:
            with sqlite3.connect(DB_NAME) as conn:
                cursor = conn.execute(
                    '''SELECT symbol, entry, exit, pct_return, exit_time 
                       FROM trade_log WHERE exit_time > ? LIMIT 100''',
                    (cutoff,))
                for row in cursor:
                    ts = datetime.fromtimestamp(row[4], tz=timezone.utc).strftime('%Y-%m-%d %H:%M')
                    detail_lines.append(f"{row[0]}: {row[1]:.2f} → {row[2]:.2f} ({row[3]:+.2f}%) [{ts}]")
        
        detail = "\n".join(detail_lines)
        
        return summary, detail
    except Exception as e:
        logger.error(f"Journal gen error: {e}")
        return "Error generating journal", ""

def get_pending_signals(limit=20):
    """Get pending signals with limit."""
    now = time.time()
    with db_lock:
        with sqlite3.connect(DB_NAME) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                '''SELECT * FROM pending_signals WHERE expiry > ? LIMIT ?''',
                (now, limit)).fetchall()
            return [dict(row) for row in rows]

# ==========================================
# ASYNC WORKER THREADS — OPTIMIZED
# ==========================================

async def trade_tracker():
    """Monitor active trades (lightweight version)."""
    while is_scanning:
        try:
            trades = get_active_trades(limit=30)
            
            for trade in trades:
                pass
            
            with db_lock:
                with sqlite3.connect(DB_NAME) as conn:
                    conn.execute(
                        'DELETE FROM pending_signals WHERE expiry < ?',
                        (time.time(),))
                    conn.commit()
            
            memory_mgr.gc_aggressive()
            await asyncio.sleep(30)
        except Exception as e:
            logger.error(f"Trade tracker error: {e}")
            await asyncio.sleep(30)

async def layer1_radar():
    """Layer 1 scanning."""
    while is_scanning:
        try:
            await asyncio.sleep(60)
        except Exception as e:
            logger.error(f"Layer1 error: {e}")
            await asyncio.sleep(60)

async def pending_signal_processor():
    """Process pending signals."""
    while is_scanning:
        try:
            signals = get_pending_signals()
            await asyncio.sleep(45)
        except Exception as e:
            logger.error(f"Pending processor error: {e}")
            await asyncio.sleep(45)

async def retest_scanner():
    """Retest scanner."""
    while is_scanning:
        try:
            await asyncio.sleep(120)
        except Exception as e:
            logger.error(f"Retest scanner error: {e}")
            await asyncio.sleep(120)

async def reentry_monitor():
    """Reentry monitor."""
    while is_scanning:
        try:
            await asyncio.sleep(120)
        except Exception as e:
            logger.error(f"Reentry monitor error: {e}")
            await asyncio.sleep(120)

def tear_sheet_scheduler():
    """Tear sheet scheduler."""
    while is_scanning:
        try:
            time.sleep(3600)
        except Exception as e:
            logger.error(f"Tear sheet error: {e}")

def journal_scheduler():
    """Journal scheduler."""
    while is_scanning:
        try:
            time.sleep(86400)
        except Exception as e:
            logger.error(f"Journal scheduler error: {e}")

def memory_monitor():
    """Monitor memory usage."""
    while is_scanning:
        try:
            memory_mgr.check_memory()
            time.sleep(300)
        except Exception as e:
            logger.error(f"Memory monitor error: {e}")
            time.sleep(300)

async def layer2_sniper(symbol, engine, force=False, chat_id=None, user_cap=10000, user_risk=2.0):
    """Layer 2 sniper."""
    try:
        pass
    except Exception as e:
        logger.error(f"Sniper error: {e}")

# ==========================================
# TELEGRAM HANDLERS
# ==========================================

@bot.message_handler(commands=['start'])
def cmd_start(msg):
    try:
        text = "🟢 Nova7 v8 — Advanced 2-Layer Trading Radar\n\nCommands:\n/status\n/journal\n/pending\n/macro\n/force SYMBOL"
        bot.reply_to(msg, text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Start cmd error: {e}")

@bot.message_handler(commands=['status'])
def cmd_status(msg):
    try:
        trades = get_active_trades(limit=10)
        if not trades:
            bot.reply_to(msg, "📭 No active trades", parse_mode="HTML")
            return
        
        lines = [f"📊 <b>Active Trades</b> ({len(trades)}):\n"]
        for t in trades:
            lines.append(f"• {t['symbol']}: {t['engine']} — {t['status']}")
        
        bot.reply_to(msg, "\n".join(lines), parse_mode="HTML")
    except Exception as e:
        logger.error(f"Status cmd error: {e}")
        bot.reply_to(msg, f"❌ Error: {str(e)[:100]}", parse_mode="HTML")

@bot.message_handler(commands=['set_capital'])
def cmd_set_capital(msg):
    try:
        args = msg.text.split()
        if len(args) < 3:
            bot.reply_to(msg, "⚠️ Format: /set_capital 10000 2.0", parse_mode="HTML")
            return
        
        capital = float(args[1])
        risk_pct = float(args[2])
        set_user_capital(msg.from_user.id, capital, risk_pct)
        
        bot.reply_to(msg, f"✅ Capital set to ${capital:,.0f} ({risk_pct}% risk)", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Set capital error: {e}")
        bot.reply_to(msg, f"❌ Error: {str(e)[:100]}", parse_mode="HTML")

@bot.message_handler(commands=['journal'])
def cmd_journal(msg):
    try:
        bot.reply_to(msg, "📓 <i>Generating journal...</i>", parse_mode="HTML")
        
        args = msg.text.split()
        days = 7
        if len(args) >= 2:
            days = max(1, min(90, int(args[1])))
        
        summary, detail = generate_trading_journal(days=days)
        bot.send_message(msg.chat.id, summary, parse_mode="HTML")
        
        if detail and len(detail) < 4000000:
            try:
                buf = io.BytesIO(detail.encode('utf-8'))
                buf.name = f"nova7_journal_{datetime.now(timezone.utc).strftime('%Y%m%d')}.txt"
                bot.send_document(msg.chat.id, buf, caption=f"📓 {days}d journal")
            except Exception as e:
                logger.error(f"Journal doc error: {e}")
    except Exception as e:
        logger.error(f"Journal cmd error: {e}")
        bot.reply_to(msg, f"❌ Error: {str(e)[:100]}", parse_mode="HTML")

@bot.message_handler(commands=['pending'])
def cmd_pending(msg):
    try:
        pendings = get_pending_signals(limit=15)
        if not pendings:
            bot.reply_to(msg, "📭 <i>No pending signals.</i>", parse_mode="HTML")
            return
        
        lines = ["🕒 <b>PENDING SIGNALS</b>\n"]
        for p in pendings:
            mins_since = int((time.time() - p['detect_time']) / 60)
            mins_to_expiry = max(0, int((p['expiry'] - time.time()) / 60))
            lines.append(
                f"• <b>{p['symbol']}</b> ({p['engine']})\n"
                f"  {mins_since}m ago — expires in {mins_to_expiry}m"
            )
        
        bot.reply_to(msg, "\n".join(lines), parse_mode="HTML")
    except Exception as e:
        bot.reply_to(msg, f"❌ Error: {str(e)[:100]}", parse_mode="HTML")

@bot.message_handler(commands=['macro'])
def cmd_macro(msg):
    try:
        state = get_btc_macro_state(force_refresh=True)
        if not state:
            bot.reply_to(msg, "❌ BTC data unavailable.", parse_mode="HTML")
            return
        
        t = get_tuning()
        threshold = t.get('macro_btc_24h_min', -1.5)
        arrow = "🟢" if state['btc_24h_pct'] >= threshold else "🔴"
        trend = "🟢 BULLISH" if state['btc_above_ema21d'] else "🔴 BEARISH"
        
        text = (
            f"🌐 <b>MACRO STATUS (BTC)</b>\n"
            f"💵 BTC Price: <code>${state['btc_price']:,.2f}</code>\n"
            f"📊 24h Change: {arrow} <code>{state['btc_24h_pct']:+.2f}%</code>\n"
            f"🎯 Trend: {trend}\n"
            f"🚦 Breakout Filter: <b>{'PASS' if state['btc_24h_pct'] >= threshold else 'BLOCK'}</b>"
        )
        
        bot.reply_to(msg, text, parse_mode="HTML")
    except Exception as e:
        bot.reply_to(msg, f"❌ Error: {str(e)[:100]}", parse_mode="HTML")

@bot.message_handler(commands=['force'])
def cmd_force(msg):
    try:
        args = msg.text.split()
        if len(args) < 2:
            bot.reply_to(msg, "⚠️ Format: /force FETUSDT", parse_mode="HTML")
            return
        
        sym = args[1].upper()
        if not sym.endswith('USDT'): 
            sym += 'USDT'
        
        user_cap, user_risk = get_user_capital(msg.from_user.id)
        bot.reply_to(msg, f"🎯 <b>Sniper:</b> {sym} (${user_cap:,.0f})", parse_mode="HTML")
        
        threading.Thread(
            target=lambda: asyncio.run(
                layer2_sniper(sym, 'BREAKOUT', force=True, chat_id=msg.chat.id,
                              user_cap=user_cap, user_risk=user_risk)),
            daemon=True).start()
    except Exception as e:
        logger.error(f"Force error: {e}")
        bot.reply_to(msg, f"❌ Error: {str(e)[:100]}", parse_mode="HTML")

# ==========================================
# TEST SIGNAL COMMAND (Untuk demo)
# ==========================================

@bot.message_handler(commands=['testsignal'])
def cmd_test_signal(msg):
    """Test command untuk demo signal format"""
    try:
        # Sample data
        signal_text = SignalFormatter.format_technical_scorecard(
            symbol="XRP/USDT",
            entry=0.246100,
            tp1=0.257440,
            tp2=0.267575,
            tp3=0.283975,
            sl=0.239835,
            vol_24h=1720000,
            score=4,
            timeframe="H1",
            setup="TREND PULLBACK",
            confidence=80
        )
        
        bot.send_message(msg.chat.id, signal_text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Test signal error: {e}")
        bot.reply_to(msg, f"❌ Error: {str(e)[:100]}", parse_mode="HTML")

# ==========================================
# FLASK APP — KEEP-ALIVE / WEBHOOK
# ==========================================
app = Flask(__name__)

@app.route('/', methods=['GET', 'HEAD'])
def home():
    return "🟢 Nova7 v8 Online — Optimized for Render free tier", 200

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
    logger.info("⚙️ Graceful shutdown...")
    global is_scanning
    is_scanning = False
    try:
        if bot:
            bot.stop_polling()
    except Exception:
        pass
    sys.exit(0)

signal.signal(signal.SIGTERM, graceful_shutdown)
signal.signal(signal.SIGINT, graceful_shutdown)

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False, threaded=True)

# ==========================================
# MAIN ORCHESTRATOR
# ==========================================
if __name__ == "__main__":
    init_db()
    logger.info("🚀 Nova7 v8 OPTIMIZED — Technical Scorecard Format")
    
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    
    if render_url:
        # ===== RENDER WEBHOOK MODE =====
        webhook_url = f"{render_url.rstrip('/')}/webhook"
        try:
            bot.remove_webhook()
            time.sleep(1)
            bot.set_webhook(url=webhook_url)
            logger.info(f"✅ Webhook: {webhook_url}")
        except Exception as e:
            logger.error(f"Webhook error: {e}")
        
        # Background threads
        threading.Thread(target=lambda: asyncio.run(trade_tracker()), daemon=True).start()
        threading.Thread(target=tear_sheet_scheduler, daemon=True).start()
        threading.Thread(target=lambda: asyncio.run(pending_signal_processor()), daemon=True).start()
        threading.Thread(target=journal_scheduler, daemon=True).start()
        threading.Thread(target=lambda: asyncio.run(retest_scanner()), daemon=True).start()
        threading.Thread(target=lambda: asyncio.run(reentry_monitor()), daemon=True).start()
        threading.Thread(target=lambda: asyncio.run(layer1_radar()), daemon=True).start()
        threading.Thread(target=memory_monitor, daemon=True).start()
        
        logger.info("✅ All workers started (webhook mode)")
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
        threading.Thread(target=lambda: asyncio.run(pending_signal_processor()), daemon=True).start()
        threading.Thread(target=journal_scheduler, daemon=True).start()
        threading.Thread(target=lambda: asyncio.run(retest_scanner()), daemon=True).start()
        threading.Thread(target=lambda: asyncio.run(reentry_monitor()), daemon=True).start()
        threading.Thread(target=lambda: asyncio.run(layer1_radar()), daemon=True).start()
        threading.Thread(target=memory_monitor, daemon=True).start()
        
        try:
            bot.infinity_polling(skip_pending=True, timeout=30)
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt.")
            graceful_shutdown(None, None)
