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
keyboard_cache = {}
def get_coin_data(coin_id):
    now = time.time()
    if coin_id in keyboard_cache and now - keyboard_cache[coin_id]['t'] < 3600:
        return keyboard_cache[coin_id]['d']
    headers = {"x-cg-demo-api-key": CG_API_KEY} if CG_API_KEY else {}
    url = f"{BASE_URL}/coins/{coin_id}?localization=false&tickers=true&market_data=false&community_data=false&developer_data=false"
    try:
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            keyboard_cache[coin_id] = {'d': data, 't': now}
            return data
    except:
        pass
    return None

# ==========================================
# 2. WEB SERVER
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Nova7 Aktif"

@app.route('/webhook', methods=['POST'])
def webhook():
    from flask import request
    update = telebot.types.Update.de_json(request.get_json())
    bot.process_new_updates([update])
    return 'ok', 200

def admin_log(ctx, err):
    if ADMIN_CHAT_ID:
        try:
            bot.send_message(ADMIN_CHAT_ID, f"☢️ [NOVA7] {ctx}\n<code>{str(err)[:300]}</code>", parse_mode="HTML")
        except:
            pass

# ==========================================
# 3. INDIKATOR
# ==========================================
def rsi(prices, p=14):
    if len(prices) < p+1: return 50
    arr = np.array(prices)
    deltas = np.diff(arr)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[:p])
    avg_loss = np.mean(losses[:p])
    for i in range(p, len(deltas)):
        avg_gain = (avg_gain*(p-1) + gains[i]) / p
        avg_loss = (avg_loss*(p-1) + losses[i]) / p
    if avg_loss == 0: return 100
    return round(100 - 100/(1 + avg_gain/avg_loss), 2)

def ema(prices, p):
    arr = np.array(prices)
    if len(arr) < p: return arr[-1]
    k = 2/(p+1)
    e = np.mean(arr[:p])
    for price in arr[p:]:
        e = price*k + e*(1-k)
    return round(e, 10)

def atr(prices, p=14):
    arr = np.array(prices)
    if len(arr) < p+1: return arr[-1]*0.05
    moves = np.abs(np.diff(arr[-(p+1):]))
    return float(np.mean(moves))

def fibo(prices):
    h, l = max(prices), min(prices)
    d = h - l
    return {'100': h, '618': h - 0.618*d, '786': h - 0.786*d, '0': l}

def signal_score(rsi, vol, ath, rr, e7, e21):
    s = 0
    if rsi < 25: s+=30
    elif rsi < 30: s+=25
    elif rsi < 35: s+=18
    elif rsi < 40: s+=12
    elif rsi < 50: s+=6
    if vol >= 4: s+=25
    elif vol >= 3: s+=20
    elif vol >= 2: s+=14
    elif vol >= 1.5: s+=8
    if ath < -80: s+=20
    elif ath < -70: s+=16
    elif ath < -60: s+=12
    elif ath < -50: s+=8
    if rr >= 4: s+=15
    elif rr >= 3: s+=12
    elif rr >= 2: s+=8
    elif rr >= 1.5: s+=4
    if e7 >= e21: s+=10
    elif e7 >= e21*0.97: s+=5
    if s >= 80: g = "⭐⭐⭐ A+"
    elif s >= 65: g = "⭐⭐ B+"
    elif s >= 50: g = "⭐ C+"
    else: g = "⚠️ D"
    return s, g

def cat_insight(cats):
    c = " ".join(cats).lower()
    if "layer 1" in c: return "Layer 1"
    if "defi" in c: return "DeFi"
    if "game" in c: return "GameFi"
    if "meme" in c: return "Meme"
    if "ai" in c: return "AI"
    if "layer 2" in c: return "Layer 2"
    if "rwa" in c: return "RWA"
    return cats[0] if cats else "Altcoin"

# ==========================================
# 4. KEYBOARD
# ==========================================
def make_keyboard(coin_id, sym, contract=None):
    markup = InlineKeyboardMarkup(row_width=2)
    data = get_coin_data(coin_id)
    cats = []
    chain = "Native"
    addr = contract
    if data:
        cats = data.get("categories", [])
        plat = data.get("asset_platform_id", "")
        if plat: chain = plat.replace("-", " ").title()
        if not addr and data.get("platforms"):
            addr = list(data["platforms"].values())[0]
    cashtag = f"https://twitter.com/search?q=%24{sym}&f=live"
    dex = f"https://dexscreener.com/search?q={addr}" if addr else f"https://dexscreener.com/search?q={sym}"
    markup.row(InlineKeyboardButton("🐦 Cashtag", url=cashtag), InlineKeyboardButton("📊 DexScreener", url=dex))
    if addr:
        if addr.startswith("0x"):
            markup.row(InlineKeyboardButton("🦅 Maestro", url=f"https://t.me/MaestroSniperBot?start={addr}-nova"))
        else:
            markup.row(InlineKeyboardButton("🤖 BonkBot", url=f"https://t.me/bonkbot_bot?start=ref_nova_{addr}"))
    if data and data.get("tickers"):
        has_bin = has_bit = has_gate = False
        for t in data["tickers"]:
            mkt = t["market"]["name"].lower()
            tgt = t.get("target", "").upper()
            if "USDT" in tgt:
                if "binance" in mkt: has_bin = True
                elif "bitget" in mkt: has_bit = True
                elif "gate" in mkt: has_gate = True
        if has_bin:
            markup.row(InlineKeyboardButton("🟨 Binance", url=f"https://www.binance.com/en/trade/{sym}_USDT"))
        elif has_bit:
            markup.row(InlineKeyboardButton("🟦 Bitget", url=f"https://www.bitget.com/spot/{sym}USDT"))
        elif has_gate:
            markup.row(InlineKeyboardButton("🟥 Gate.io", url=f"https://www.gate.io/trade/{sym}_USDT"))
    return markup, cats, addr, chain

# ==========================================
# 5. SIMPAN TRADE
# ==========================================
def save_trade(mid, sym, cid, sl, tp1, tp2, tp3):
    with trades_lock:
        trades = {}
        if os.path.exists("active_trades.json"):
            try:
                with open("active_trades.json") as f:
                    trades = json.load(f)
            except:
                pass
        trades[str(mid)] = {"symbol": sym, "coin_id": cid, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3, "status": "TRACKING", "timestamp": time.time()}
        with open("active_trades.json", "w") as f:
            json.dump(trades, f, indent=4)

# ==========================================
# 6. DISPATCH SIGNAL
# ==========================================
def dispatch(chat_id, name, sym, rank, ath, vol, rsi_val, price, fib, cid, trend24, vol24, trend7, atr_val, e7, e21, ca=None):
    markup, cats, final_ca, chain = make_keyboard(cid, sym, ca)
    cat = cat_insight(cats)
    safe_name = html.escape(name)
    safe_sym = html.escape(sym)
    safe_chain = html.escape(chain)
    ca_disp = f"<code>{final_ca}</code>" if final_ca else "<i>No CA</i>"
    vol_str = f"${vol24:,.0f}" if vol24 else "N/A"

    if "Meme" in cat:
        m_sl, m1, m2, m3 = 1.5, 2.5, 4.5, 7.0
    else:
        m_sl, m1, m2, m3 = 2.0, 1.5, 3.0, 5.0
    sl = max(price - m_sl*atr_val, fib['0']*0.97)
    tp1 = price + m1*atr_val
    tp2 = price + m2*atr_val
    tp3 = price + m3*atr_val

    rr = round((tp2-price)/(price-sl), 2)
    _, grade = signal_score(rsi_val, vol, ath, rr, e7, e21)

    rsi_status = "STRONG OVERSOLD" if rsi_val < 30 else "OVERSOLD" if rsi_val < 40 else "NEUTRAL"
    ema_label = "🟢 Bullish" if e7 >= e21 else "🔴 Bearish"

    closest = min(abs(price-fib['100']), abs(price-fib['618']), abs(price-fib['786']), abs(price-fib['0']))
    if price > fib['100']: fib_res = "Above Peak"
    elif price < fib['0']: fib_res = "Below Floor"
    elif closest == abs(price-fib['100']): fib_res = "Retesting Peak"
    elif closest == abs(price-fib['618']): fib_res = "Golden Pocket"
    elif closest == abs(price-fib['786']): fib_res = "Deep Value"
    else: fib_res = "Absolute Bottom"

    rank_int = int(rank) if str(rank).isdigit() else 999
    if "Layer 1" in cat or "DeFi" in cat or rank_int <= 50:
        tier = "Tier-1 (Max 5%)"
    elif "Meme" in cat or rank_int >= 200:
        tier = "Tier-3 (Max 1.5%)"
    else:
        tier = "Tier-2 (Max 3%)"

    msg = (
        f"🪙 <b>{safe_name} ({safe_sym})</b> — <i>{safe_chain}</i>\n"
        f"💳 CA: {ca_disp}\n"
        f"💵 Price: ${price:.6f} | Rank: #{rank}\n"
        "........................................................\n"
        f"📉 24H: {trend24:+.2f}% | 1W: {trend7:+.2f}%\n"
        f"🌊 Vol: {vol_str} [Spike: {vol:.2f}x]\n"
        f"🩸 ATH Drop: {ath:.2f}%\n"
        "........................................................\n"
        f"🎯 GRADE: {grade}\n"
        f"🔥 RSI: {rsi_val:.2f} ({rsi_status})\n"
        f"📈 EMA: {ema_label} | ATR: ${atr_val:.6f}\n"
        f"📊 Fibo: {fib_res}\n"
        "........................................................\n"
        f"🔸 Entry: ${price:.6f} - ${fib['786']:.6f}\n"
        f"🛑 SL: ${sl:.6f}\n"
        f"🎯 TP1: ${tp1:.6f} | TP2: ${tp2:.6f} | TP3: ${tp3:.6f}\n"
        "........................................................\n"
        f"💼 Capital: {tier}\n"
        "⚡ Protocol: Break-even di TP1, 50% di TP2, risk-free ke TP3."
    )

    try:
        sent = bot.send_message(chat_id, msg, reply_markup=markup, disable_web_page_preview=True)
        if sent:
            save_trade(sent.message_id, sym, cid, sl, tp1, tp2, tp3)
            journal.log_signal(symbol=sym, coin_id=cid, entry_price=price, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3, grade=grade, coin_name=name, risk_tier=tier, msg_id=sent.message_id)
    except Exception as e:
        admin_log(f"Signal fail {sym}", e)

# ==========================================
# 7. TRADE TRACKER (60s)
# ==========================================
def tracker():
    while True:
        time.sleep(60)
        if not TELEGRAM_CHAT_ID or not os.path.exists("active_trades.json"): continue
        with trades_lock:
            try:
                with open("active_trades.json") as f:
                    trades = json.load(f)
            except:
                continue
        active = {k:v for k,v in trades.items() if v["status"] not in ["COMPLETED","STOP_LOSS"]}
        if not active: continue
        ids = ",".join(set(v["coin_id"] for v in active.values()))
        headers = {"x-cg-demo-api-key": CG_API_KEY} if CG_API_KEY else {}
        for _ in range(3):
            try:
                r = requests.get(f"{BASE_URL}/simple/price?ids={ids}&vs_currencies=usd", headers=headers, timeout=15)
                if r.status_code == 429:
                    time.sleep(60)
                    continue
                if r.status_code == 200:
                    prices = r.json()
                    break
            except:
                time.sleep(10)
        else:
            continue
        updated = False
        for mid, trade in active.items():
            cid = trade["coin_id"]
            if cid not in prices: continue
            p = prices[cid]["usd"]
            stat = trade["status"]
            sym = trade["symbol"]
            reply = ""
            new = stat
            if p <= trade["sl"]:
                reply = f"🛑 {sym} — STOP LOSS HIT at ${p:.6f}"
                new = "STOP_LOSS"
            elif p >= trade["tp3"] and stat != "TP2_HIT":
                reply = f"👑 {sym} — TP3 MAX HIT at ${p:.6f} 🚀"
                new = "COMPLETED"
            elif p >= trade["tp2"] and stat not in ["TP2_HIT","COMPLETED"]:
                reply = f"🔥 {sym} — TP2 HIT at ${p:.6f}"
                new = "TP2_HIT"
            elif p >= trade["tp1"] and stat == "TRACKING":
                reply = f"✅ {sym} — TP1 HIT at ${p:.6f} → Move SL to entry"
                new = "TP1_HIT"
            if reply:
                try:
                    bot.send_message(TELEGRAM_CHAT_ID, reply, reply_to_message_id=int(mid), parse_mode="HTML")
                    trades[mid]["status"] = new
                    updated = True
                    omap = {"TP1_HIT":"TP1_HIT","TP2_HIT":"TP2_HIT","COMPLETED":"TP3_HIT","STOP_LOSS":"STOP_LOSS"}
                    journal.update_outcome(coin_id=cid, outcome=omap.get(new, new), exit_price=p)
                except:
                    pass
        if updated:
            with trades_lock:
                with open("active_trades.json","w") as f:
                    json.dump(trades, f, indent=4)

# ==========================================
# 8. SCANNER LOOP (Nova7: page 5-7, hourly)
# ==========================================
def scanner():
    global is_scanning
    time.sleep(30)
    KILL = {"btc","eth","usdt","usdc","dai","wbtc","steth","weth","tusd","usde"}
    COOLDOWN_FILE = "signal_cooldown.json"
    def get_cd():
        if os.path.exists(COOLDOWN_FILE):
            try:
                with open(COOLDOWN_FILE) as f:
                    return json.load(f)
            except:
                return {}
        return {}
    def save_cd(cid):
        db = get_cd()
        db[cid] = time.time()
        db = {k:v for k,v in db.items() if time.time()-v < 86400}
        with open(COOLDOWN_FILE,"w") as f:
            json.dump(db,f)

    while True:
        if not is_scanning:
            time.sleep(10)
            continue
        logger.info("Nova7 scanning cycle start")
        headers = {"x-cg-demo-api-key": CG_API_KEY} if CG_API_KEY else {}
        try:
            btc = requests.get(f"{BASE_URL}/coins/markets?vs_currency=usd&ids=bitcoin", headers=headers, timeout=15).json()
            btc24 = btc[0].get('price_change_percentage_24h',0)
            if btc24 < -4:
                logger.warning(f"BTC {btc24:.2f}% -> defense mode")
                if ADMIN_CHAT_ID:
                    bot.send_message(ADMIN_CHAT_ID, f"⚠️ BTC {btc24:.2f}% -> pause 6h")
                time.sleep(21600)
                continue
            global_data = requests.get(f"{BASE_URL}/global", headers=headers, timeout=15).json()
            btc_dom = global_data['data']['market_cap_percentage']['btc']
            rsi_limit = 32 if (btc_dom>50 and btc24<0) else 40
            logger.info(f"BTC.D: {btc_dom:.1f}%, RSI limit: {rsi_limit}")
        except Exception as e:
            admin_log("Macro error", e)
            time.sleep(60)
            continue

        all_coins = []
        for page in range(5,7):
            if not is_scanning: break
            params = {"vs_currency":"usd","order":"market_cap_desc","per_page":250,"page":page,"sparkline":"false"}
            try:
                r = requests.get(f"{BASE_URL}/coins/markets", params=params, headers=headers, timeout=15)
                if r.status_code == 429:
                    time.sleep(90)
                    continue
                if r.status_code == 200:
                    all_coins.extend(r.json())
                time.sleep(3)
            except:
                time.sleep(3)

        cd = get_cd()
        now = time.time()
        candidates = []
        for coin in all_coins:
            if not is_scanning: break
            cid = coin['id']
            if cid in cd and now - cd[cid] < 86400: continue
            if coin['symbol'].lower() in KILL: continue
            ath = coin.get('ath_change_percentage')
            vol = coin.get('total_volume')
            if ath is None or ath > -50: continue
            if vol is None or vol < 500000: continue
            hist = requests.get(f"{BASE_URL}/coins/{cid}/market_chart", params={"vs_currency":"usd","days":"7","interval":"hourly"}, headers=headers, timeout=15)
            if hist.status_code == 429:
                time.sleep(90)
                hist = requests.get(f"{BASE_URL}/coins/{cid}/market_chart", params={"vs_currency":"usd","days":"7","interval":"hourly"}, headers=headers, timeout=15)
            if hist.status_code != 200: continue
            data = hist.json()
            prices = [p[1] for p in data['prices']]
            volumes = [v[1] for v in data['total_volumes']]
            if len(prices) < 30: continue
            avg_vol7 = np.mean(volumes[-8:-1]) if len(volumes)>=9 else 1
            if avg_vol7 == 0: continue
            vol_mult = vol / avg_vol7
            if vol_mult < 1.5: continue
            rsi_val = rsi(prices,14)
            ema7 = ema(prices,7)
            ema21 = ema(prices,21)
            atr_val = atr(prices,7)
            if vol_mult >= 2.0:
                if rsi_val > 50: continue
            else:
                if rsi_val >= rsi_limit: continue
            if ema7 < ema21 * 0.94 and rsi_val >= 30: continue
            fib = fibo(prices)
            hist_close = prices[-1]
            trend7_hist = ((hist_close - prices[-8])/prices[-8])*100 if len(prices)>=8 else 0
            candidates.append({
                'coin': coin, 'cid': cid, 'sym': coin['symbol'].upper(),
                'ath': ath, 'vol': vol, 'vol_mult': vol_mult,
                'rsi': rsi_val, 'ema7': ema7, 'ema21': ema21,
                'atr': atr_val, 'fibo': fib, 'trend7_hist': trend7_hist,
                'hist_close': hist_close
            })
            time.sleep(3)

        if candidates:
            ids_str = ",".join([c['cid'] for c in candidates])
            live = {}
            try:
                r = requests.get(f"{BASE_URL}/simple/price?ids={ids_str}&vs_currencies=usd", headers=headers, timeout=15)
                if r.status_code == 200:
                    live = r.json()
                time.sleep(3)
            except:
                pass
            for c in candidates:
                cid = c['cid']
                live_price = live.get(cid, {}).get("usd", c['hist_close'])
                if live_price is None: continue
                trend7 = c['trend7_hist']
                if live_price <= c['fibo']['618']:
                    trend24 = c['coin'].get('price_change_percentage_24h',0)
                    dispatch(TELEGRAM_CHAT_ID, c['coin']['name'], c['sym'], c['coin'].get('market_cap_rank','N/A'),
                             c['ath'], c['vol_mult'], c['rsi'], live_price, c['fibo'], c['cid'],
                             trend24, c['vol'], trend7, c['atr'], c['ema7'], c['ema21'])
                    save_cd(cid)
        if is_scanning:
            if ADMIN_CHAT_ID:
                try:
                    bot.send_message(ADMIN_CHAT_ID, "⏳ Nova7 cycle selesai. Cooling 6 jam.")
                except:
                    pass
            time.sleep(21600)

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
        last_week = _week_key(time.time() - 86400*7)
        report = journal.get_weekly_report_text(week_key=last_week)
        try:
            bot.send_message(ADMIN_CHAT_ID, report, parse_mode="HTML")
            print("Weekly report sent")
        except Exception as e:
            print(f"Gagal hantar report: {e}")
        time.sleep(86400*7)

# ==========================================
# 10. TELEGRAM COMMANDS
# ==========================================
@bot.message_handler(commands=['start','help'])
def start(msg):
    bot.reply_to(msg, "⚡ Nova7 aktif. /scan /stop /ca /report")

@bot.message_handler(commands=['scan'])
def scan_cmd(msg):
    global is_scanning
    is_scanning = True
    bot.reply_to(msg, "✅ Scanning diaktifkan")

@bot.message_handler(commands=['stop'])
def stop_cmd(msg):
    global is_scanning
    is_scanning = False
    bot.reply_to(msg, "🛑 Scanning dihentikan")

@bot.message_handler(commands=['ca'])
def ca_cmd(msg):
    args = msg.text.split()
    if len(args)<2:
        bot.reply_to(msg, "Usage: /ca <id or address>")
        return
    query = args[1].lower()
    bot.reply_to(msg, f"🔍 Analysing {html.escape(query)}...")
    headers = {"x-cg-demo-api-key": CG_API_KEY} if CG_API_KEY else {}
    try:
        if query.startswith("0x"):
            search = requests.get(f"{BASE_URL}/search?query={query}", headers=headers, timeout=15).json()
            if not search.get("coins"):
                bot.reply_to(msg, "❌ Not found")
                return
            cid = search["coins"][0]["id"]
        else:
            cid = query
        market = requests.get(f"{BASE_URL}/coins/markets?vs_currency=usd&ids={cid}", headers=headers, timeout=15).json()[0]
        name = market['name']
        sym = market['symbol'].upper()
        rank = market.get('market_cap_rank','N/A')
        ath = market.get('ath_change_percentage',0)
        price_live = market['current_price']
        trend24 = market.get('price_change_percentage_24h',0)
        vol24 = market.get('total_volume',0)
        hist = requests.get(f"{BASE_URL}/coins/{cid}/market_chart", params={"vs_currency":"usd","days":"7","interval":"hourly"}, headers=headers, timeout=15).json()
        prices = [p[1] for p in hist['prices']]
        volumes = [v[1] for v in hist.get('total_volumes',[])]
        rsi_val = rsi(prices,14) if len(prices)>=30 else 0
        fib = fibo(prices) if len(prices)>=30 else {'100':price_live,'618':price_live,'786':price_live,'0':price_live}
        ema7 = ema(prices,7)
        ema21 = ema(prices,21)
        atr_val = atr(prices,7)
        trend7 = ((prices[-1]-prices[-8])/prices[-8])*100 if len(prices)>=8 else 0
        avg_vol7 = np.mean(volumes[-8:-1]) if len(volumes)>=8 else vol24
        vol_mult = vol24/avg_vol7 if avg_vol7>0 else 1
        dispatch(TELEGRAM_CHAT_ID, name, sym, rank, ath, vol_mult, rsi_val, price_live, fib, cid, trend24, vol24, trend7, atr_val, ema7, ema21, ca=query if query.startswith("0x") else None)
        bot.reply_to(msg, "✅ Analisis selesai")
    except Exception as e:
        bot.reply_to(msg, f"❌ Error: {str(e)[:100]}")
        logger.error(f"CA error: {e}")

@bot.message_handler(commands=['report'])
def cmd_report(message):
    if str(message.chat.id) != ADMIN_CHAT_ID:
        bot.reply_to(message, "Akses ditolak. Command ini untuk admin sahaja.")
        return
    week_key = _week_key(time.time())
    report = journal.get_weekly_report_text(week_key=week_key)
    bot.reply_to(message, report, parse_mode="HTML")

# ==========================================
# 11. MAIN
# ==========================================
def graceful(*args):
    if ADMIN_CHAT_ID:
        try:
            bot.send_message(ADMIN_CHAT_ID, "🔴 Nova7 offline")
        except:
            pass
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, graceful)
    signal.signal(signal.SIGINT, graceful)
    RENDER_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if RENDER_URL:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=f"{RENDER_URL}/webhook")
        print(f"Webhook set: {RENDER_URL}/webhook")
    if ADMIN_CHAT_ID:
        try:
            bot.send_message(ADMIN_CHAT_ID, "🟢 Nova7 active")
        except:
            pass
    threading.Thread(target=tracker, daemon=True).start()
    threading.Thread(target=scanner, daemon=True).start()
    threading.Thread(target=schedule_weekly_report, daemon=True).start()
    journal.register_commands(bot, ADMIN_CHAT_ID)
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))