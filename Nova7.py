import os
import time
import json
import sqlite3
import asyncio
import threading
import logging
import sys
import signal
import aiohttp
import websockets
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from flask import Flask, request
from datetime import datetime, timezone

# ==========================================
# KONFIGURASI & LOGGING (NOVA7 LEGACY)
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger("Nova7")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML") if TELEGRAM_TOKEN else None
is_scanning = True  # Legacy flag untuk /stop dan /start

# Filter Institusi: Stablecoins, Wrapped, Fiat, & HEAVYWEIGHTS (Top 20)
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
# DATABASE SQLITE (PENGGANTI JSON)
# ==========================================
DB_NAME = "nova7_data.db"
db_lock = threading.Lock()

def init_db():
    with db_lock, sqlite3.connect(DB_NAME) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS active_trades 
                        (msg_id INTEGER PRIMARY KEY, symbol TEXT, entry REAL, 
                         sl REAL, tp1 REAL, tp2 REAL, tp3 REAL, engine TEXT, 
                         status TEXT, timestamp REAL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS cooldowns 
                        (symbol TEXT PRIMARY KEY, last_signal REAL)''')
    logger.info("✅ [DB] Nova7 SQLite initialized.")

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

# ==========================================
# LAYER 2: SNIPER (MATEMATIK O(1) & LOGIK)
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

class BreakoutHunter:
    def check(self, ind):
        if len(ind.closes) < 51: 
            return None, {"Data Sejarah": "Kurang 51 candle (Gagal)"}
        
        close = ind.closes[-1]
        rvol = ind.get_rvol()
        recent_high = ind.get_recent_high()
        
        # Enjin Self-Documenting: Semua syarat & nilai atual direkodkan secara dinamik
        conditions = {
            f"Pecah High 20-Candle ({recent_high:.6f})": close > recent_high,
            f"Harga Atas EMA21 ({ind.ema21:.6f})": close > ind.ema21,
            "Struktur Uptrend (EMA21 > EMA50)": ind.ema21 > ind.ema50,
            f"Volume Spike (RVOL >= 1.8x) [Aktual: {rvol:.2f}x]": rvol >= 1.8,
            f"Momentum RSI (50 - 75) [Aktual: {ind.rsi:.1f}]": 50 < ind.rsi < 75
        }
        
        # Jika SEMUA syarat True, ia VALID
        if all(conditions.values()):
            sig = {'type': 'BREAKOUT', 'rvol': rvol, 'break_level': recent_high, 'low': min(ind.lows[-5:])}
            return sig, conditions
        return None, conditions

class AccumulationDetective:
    def check(self, ind):
        if len(ind.closes) < 51: return None
        close, bb, rvol = ind.closes[-1], ind.get_bb_width(), ind.get_rvol()
        if bb < 6.0 and rvol >= 2.0 and close < ind.ema50 and ind.rsi < 45:
            return {'type': 'ACCUMULATION', 'rvol': rvol, 'bb': bb, 'low': min(ind.lows[-5:])}
        return None

# ==========================================
# TELEGRAM DISPATCH & UI
# ==========================================
def build_keyboard(symbol):
    base = symbol[:-4]
    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton("📊 DexScreener", url=f"https://dexscreener.com/search?q={base}"),
        InlineKeyboardButton("📈 TradingView", url=f"https://www.tradingview.com/chart/?symbol=BINANCE:{symbol}")
    )
    markup.row(InlineKeyboardButton("🟨 Trade on Binance", url=f"https://www.binance.com/en/trade/{symbol}"))
    markup.row(InlineKeyboardButton("🐦 Twitter Search", url=f"https://x.com/search?q=%24{base}&f=live"))
    return markup

def dispatch_signal(symbol, price, sig, ind, engine_type):
    if not bot or not TELEGRAM_CHAT_ID or check_cooldown(symbol): return
    sl = sig['low'] * 0.995
    risk = price - sl
    if risk <= 0: return
    tp1, tp2, tp3 = price + (risk * 2.0), price + (risk * 3.5), price + (risk * 5.5)
    
    emoji, title = ("🚀", "BREAKOUT RADAR") if engine_type == 'BREAKOUT' else ("🕵️", "ACCUMULATION SNIPER")
    desc = f"Break Level: <code>${sig['break_level']:.6f}</code>" if engine_type == 'BREAKOUT' else f"BB Squeeze: {sig['bb']:.2f}%"
    
    msg = (
        f"{emoji} <b>{title}: {symbol}</b>\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"💵 <b>Price:</b> <code>${price:.6f}</code>\n"
        f"{desc}\n"
        f"🔥 <b>RVOL:</b> {sig['rvol']:.2f}x | <b>RSI:</b> {ind.rsi:.1f}\n"
        f"📊 <b>EMA21:</b> ${ind.ema21:.6f} | <b>EMA50:</b> ${ind.ema50:.6f}\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"🛑 <b>SL:</b> <code>${sl:.6f}</code>\n"
        f"🎯 <b>TP1 (2R):</b> <code>${tp1:.6f}</code>\n"
        f"🎯 <b>TP2 (3.5R):</b> <code>${tp2:.6f}</code>\n"
        f"🎯 <b>TP3 (5.5R):</b> <code>${tp3:.6f}</code>\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"🐋 <i>Nova7 Institutional Setup (Asymmetric 5x-20x)</i>"
    )
    try:
        sent = bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode="HTML", reply_markup=build_keyboard(symbol), disable_web_page_preview=True)
        save_trade(sent.message_id, symbol, price, sl, tp1, tp2, tp3, engine_type)
        save_cooldown(symbol, 24 if engine_type == 'BREAKOUT' else 48)
        logger.info(f"✅ [SIGNAL] {symbol} ({engine_type}) dispatched.")
    except Exception as e: logger.error(f"Dispatch error: {e}")

# ==========================================
# LAYER 1 & 2 ORCHESTRATOR (THE BRAIN)
# ==========================================
latest_prices = {}
radar_history = {}
layer2_queue = set()
stats = {'radar_coins': 0, 'layer2_scans': 0, 'signals_sent': 0}

async def layer1_radar():
    global is_scanning
    url = "wss://stream.binance.com:9443/ws/!miniTicker@arr"
    last_snapshot = 0
    last_scheduled = 0
    
    while True:
        if not is_scanning:
            await asyncio.sleep(5)
            continue
            
        try:
            async with websockets.connect(url, ping_interval=20, max_size=10**7) as ws:
                logger.info("✅ [RADAR] Layer 1 Connected. Scanning Mid-Caps...")
                if bot and ADMIN_CHAT_ID:
                    bot.send_message(ADMIN_CHAT_ID, "🟢 <b>HELLO, NOVA7 NOW ACTIVE.</b>\n2-Layer Radar Online. Link to Render established.", parse_mode="HTML")
                
                while True:
                    if not is_scanning: break
                    msg = await ws.recv()
                    now = time.time()
                    if now - last_snapshot < 3.0: continue
                    last_snapshot = now
                    
                    tickers = json.loads(msg)
                    for t in tickers:
                        sym = t['s']
                        if not sym.endswith('USDT') or sym in HEAVYWEIGHTS: continue
                        base = sym[:-4]
                        if base in KILL_LIST: continue
                        
                        c, q = float(t['c']), float(t['q'])
                        latest_prices[sym] = {'c': c, 'q': q}
                        
                        if sym not in radar_history: radar_history[sym] = []
                        radar_history[sym].append({'t': now, 'c': c})
                        if len(radar_history[sym]) > 15: radar_history[sym].pop(0)
                        
                        if len(radar_history[sym]) >= 6 and sym not in layer2_queue:
                            past_c = radar_history[sym][-6]['c']
                            if past_c > 0:
                                change = ((c - past_c) / past_c) * 100
                                if change >= 2.5 and q > 15_000_000:
                                    layer2_queue.add(sym)
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

async def layer2_sniper(symbol, scan_type, force=False):
    try:
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
            sig, _ = BreakoutHunter().check(ind)
        else:
            sig = AccumulationDetective().check(ind)
            
        if sig:
            dispatch_signal(symbol, closes[-1], sig, ind, scan_type)
            stats['signals_sent'] += 1
            
        stats['layer2_scans'] += 1
    except Exception as e:
        logger.error(f"Sniper error {symbol}: {e}")
    finally:
        layer2_queue.discard(symbol)

# ==========================================
# TRADE TRACKER (REAL-TIME)
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
                reply, new_status = f"🛑 <b>{sym} — STOP LOSS HIT</b>\nProteksi modal diaktifkan pada <code>${price:.6f}</code>", 'STOP_LOSS'
            elif price >= t['tp3'] and status != 'COMPLETED':
                reply, new_status = f"👑 <b>{sym} — TP3 MOONSHOT!</b>\n100% sasaran hancur ditewaskan di <code>${price:.6f}</code>", 'COMPLETED'
            elif price >= t['tp2'] and status not in ['TP2_HIT', 'COMPLETED']:
                reply, new_status = f"🔥 <b>{sym} — TP2 HIT!</b>\nPoketkan 50% profit di <code>${price:.6f}</code>", 'TP2_HIT'
            elif price >= t['tp1'] and status == 'TRACKING':
                reply, new_status = f"✅ <b>{sym} — TP1 SECURED!</b>\nAlihkan SL ke Break-Even SEKARANG di <code>${price:.6f}</code>", 'TP1_HIT'
                
            if reply:
                try:
                    bot.send_message(TELEGRAM_CHAT_ID, reply, reply_to_message_id=t['msg_id'], parse_mode="HTML")
                    update_trade_status(t['msg_id'], new_status)
                except: pass

# ==========================================
# TELEGRAM COMMANDS (LEGACY RESTORED)
# ==========================================
@bot.message_handler(commands=['start', 'help'])
def cmd_start(msg):
    global is_scanning
    is_scanning = True
    status = "🟢 AKTIF" if is_scanning else "🔴 STANDBY"
    bot.reply_to(msg, f"⚡ <b>NOVA7 [{status}]</b>\n\nEnjin Institusi 2-Layer aktif.\nArahan: <code>/stop</code>, <code>/status</code>, <code>/force [SYMBOL]</code>", parse_mode="HTML")

@bot.message_handler(commands=['stop'])
def cmd_stop(msg):
    global is_scanning
    is_scanning = False
    bot.reply_to(msg, "🛑 <b>Enjin Nova7 Dihentikan Sementara.</b>\nRadar paused.", parse_mode="HTML")

@bot.message_handler(commands=['status'])
def cmd_status(msg):
    status = "🟢 AKTIF" if is_scanning else "🔴 STANDBY"
    text = (
        f"📊 <b>NOVA7 RADAR STATUS [{status}]</b>\n"
        f"┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"🐋 Layer 1 (Scanning): {stats['radar_coins']} coins\n"
        f"🎯 Layer 2 (Analyzing): {len(layer2_queue)} coins\n"
        f"📈 Signals Sent: {stats['signals_sent']}\n"
        f"🕐 {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    )
    bot.reply_to(msg, text, parse_mode="HTML")

def cmd_force
# ==========================================
# FLASK WEBHOOK & SHUTDOWN
# ==========================================
app = Flask(__name__)
@app.route('/')
def home(): return "Enjin Nova7 Aktif & Stabil 🐋"
@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        bot.process_new_updates([telebot.types.Update.de_json(request.get_json())])
    return 'ok', 200

def graceful_shutdown(*args):
    if TELEGRAM_TOKEN and ADMIN_CHAT_ID:
        try: bot.send_message(ADMIN_CHAT_ID, "🔴 <b>[OFFLINE] NOVA7 DISCONNECTED.</b> Render shutting down.", parse_mode="HTML")
        except: pass
    sys.exit(0)

# ==========================================
# MAIN ORCHESTRATOR
# ==========================================
# ==========================================
# MAIN ORCHESTRATOR (KALIS PELURU - ANTI 409)
# ==========================================
def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)
    init_db()
    
    RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    
    # PENTING: Paksa buang webhook lama dan tunggu 2 saat untuk elak Race Condition
    if bot:
        bot.remove_webhook()
        time.sleep(2) 
    
    if RENDER_URL:
        # ==========================================
        # MODE A: BERJALAN DI RENDER (GUNA WEBHOOK)
        # ==========================================
        logger.info("[ENV] Render Cloud detected. Activating Webhook Mode...")
        if bot:
            bot.set_webhook(url=f"{RENDER_URL}/webhook")
            logger.info(f"[WEBHOOK] Aktif: {RENDER_URL}/webhook")
        
        # Jalankan Trade Tracker
        threading.Thread(target=lambda: asyncio.run(trade_tracker()), daemon=True).start()
        
        # Jalankan Flask (Penerima Webhook) di background
        threading.Thread(target=run_flask, daemon=True).start()
        
        # Jalankan Radar (Main Thread)
        try: 
            asyncio.run(layer1_radar())
        except KeyboardInterrupt: 
            graceful_shutdown()
            
    else:
        # ==========================================
        # MODE B: BERJALAN DI KOMPUTER LOKAL (GUNA POLLING)
        # ==========================================
        logger.info("[ENV] Localhost detected. Activating Polling Mode...")
        
        # Jalankan Trade Tracker
        threading.Thread(target=lambda: asyncio.run(trade_tracker()), daemon=True).start()
        
        # Jalankan Radar di background
        threading.Thread(target=lambda: asyncio.run(layer1_radar()), daemon=True).start()
        
        # Jalankan Polling di Main Thread (Hanya untuk test lokal)
        if bot:
            try:
                bot.infinity_polling()
            except KeyboardInterrupt:
                graceful_shutdown()
