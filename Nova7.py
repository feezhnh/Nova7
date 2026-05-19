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

# ==========================================
# 1. KONFIGURASI & KESELAMATAN (ENV) [LOCKED]
# ==========================================
CG_API_KEY = os.environ.get("CG_API_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")
BASE_URL = "https://api.coingecko.com/api/v3"  # <--- PASTIKAN BARIS INI WUJUD!

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode="HTML")
is_scanning = True 

 

# ==========================================
# 2. DUMMY WEB SERVER (RENDER KEEP-ALIVE) [LOCKED]
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "Engine Nova7 Aktif & Stabil 🐋"

# ==========================================
# 2.5 TELEGRAM GOD MODE (SYSTEM LOGGING)
# ==========================================
def admin_log(context, error):
    if not ADMIN_CHAT_ID: return
    try:
        msg = f"☢️ <b>[NOVA7 ERROR] {context}</b>\n<code>{str(error)}</code>"
        bot.send_message(ADMIN_CHAT_ID, msg, parse_mode="HTML")
    except:
        pass

# ==========================================
# 3. INDIKATOR TEKNIKAL & MATEMATIK [LOCKED]
# ==========================================
def calculate_rsi(prices, period=14):
    if len(prices) < period + 1: return 50
    deltas = np.diff(prices)
    seed = deltas[:period]
    up = seed[seed >= 0].sum() / period
    down = -seed[seed < 0].sum() / period
    if down == 0: return 100
    rs = up / down
    rsi = np.zeros_like(prices)
    rsi[:period] = 100. - 100. / (1. + rs)

    for i in range(period, len(prices)):
        delta = deltas[i - 1]
        up_val = delta if delta > 0 else 0.
        down_val = -delta if delta < 0 else 0.
        up = (up * (period - 1) + up_val) / period
        down = (down * (period - 1) + down_val) / period
        if down == 0: rsi[i] = 100
        else:
            rs = up / down
            rsi[i] = 100. - 100. / (1. + rs)
    return rsi[-1]

def calculate_fibonacci_levels(prices):
    high_p, low_p = max(prices), min(prices)
    diff = high_p - low_p
    return {
        "Fibo_100": high_p, "Fibo_618": high_p - (0.618 * diff),
        "Fibo_786": high_p - (0.786 * diff), "Fibo_0": low_p
    }

# ==========================================
# 4. PEMETAAN KATEGORI & TESIS BM (ULTRA-VIP BLOOMBERG TONE)
# ==========================================
# GUIDE: Tajuk kuantitatif dirombak untuk menunjukkan peningkatan efisiensi (Laju/Murah/Stabil) berbanding Big Cap.
# Tajuk Katalis disuntik dengan elemen On-Chain realistik (Whales/TVL sejak minggu lepas/Integrasi) untuk mencipta FOMO.
def get_category_insight(categories):
    cat_str = ", ".join(categories).lower() if categories else ""
    
    if "layer 1" in cat_str or "smart contract" in cat_str:
        return (
            "Layer 1", 
            "Aset ini menawarkan throughput pemprosesan blok yang jauh lebih laju dan kos transaksi mikro berbanding gergasi L1 Big Cap yang mahal. Seni bina rangkaian yang lebih stabil ini memintas kekangan latency lama, memberikan kecekapan operasi maksimum pada pecahan kos.", 
            "Metrik on-chain mencatatkan lonjakan TVL masif sejak minggu lepas didorong oleh akumulasi agresif oleh dompet jerung (Whales). Integrasi ekosistem korporat peringkat akhir sedang berlaku, mencetuskan supply shock sebelum pendedahan awam."
        )
    elif "defi" in cat_str or "decentralized finance" in cat_str:
        return (
            "DeFi", 
            "Protokol ini merekayasa semula kecairan dengan kelajuan settlement ultra-pantas dan struktur yuran jauh lebih murah berbanding dApps legasi yang mahal. Kestabilan kolam kecairan menghapuskan risiko slippage yang sering membebani modal besar.", 
            "Jejakan on-chain mendedahkan dompet institusi sedang memindahkan modal ke sini sejak minggu lepas. Aktiviti paus (Whale buying) bergerak selari dengan spekulasi integrasi TradFi, memampatkan bekalan terapung di pasaran."
        )
    elif "gaming" in cat_str or "play to earn" in cat_str:
        return (
            "GameFi/Web3", 
            "Menampilkan enjin pemprosesan aset on-chain yang berkali ganda lebih pantas dan kos minting hampir percuma berbanding ekosistem gaming premium yang mahal. Rangkaian menawarkan kestabilan tinggi tanpa mengorbankan aspek keamanan gred industri.", 
            "Aliran data on-chain mendedahkan pegangan whales meningkat drastik sejak minggu lepas untuk menyerap floating supply. Integrasi bersama studio gergasi Web2 sedang berjalan secara rahsia, membina momentum sebelum ledakan viral."
        )
    elif "meme" in cat_str:
        return (
            "Meme", 
            "Menyediakan rantaian pengedaran kecairan yang lebih pantas dengan kos gas terendah, mengecilkan halangan modal berbanding token meme Big Cap yang mahal. Kestabilan smart contract memastikan keselamatan dana maksimum untuk dagangan volum tinggi.", 
            "Metrik harian memaparkan pengumpulan tanpa henti oleh jerung (Smart money accumulation) sejak minggu lepas. Semburan volum on-chain berserta desas-desus integrasi utiliti rahsia sedang mematangkan struktur pasaran di sebalik tabir."
        )
    elif "artificial intelligence" in cat_str or "ai" in cat_str:
        return (
            "AI", 
            "Rangkaian ini membolehkan pengiraan terdesentralisasi yang jauh lebih pantas dan kos latensi ultra-rendah berbanding platform AI monopoli yang mahal. Infrastruktur stabil ini direka khusus untuk pemprosesan data berprestasi tinggi pada kos mikro.", 
            "Aliran volum on-chain membuktikan aktiviti whales mencedok pasaran melonjak drastik sejak minggu lepas. Integrasi model kecerdasan buatan terbaru sedang berada di fasa final, bersiap sedia memanfaatkan tailwinds teknologi AI global."
        )
    elif "layer 2" in cat_str or "rollup" in cat_str:
        return (
            "Layer 2", 
            "Teknologi rollups memastikan kelajuan transaksi yang berlipat ganda dan kos gas pecahan sen berbanding mainnet asal yang mahal. Memberikan kestabilan operasi maksimum tanpa gangguan rintangan rangkaian walaupun semasa volum sesak.", 
            "Sejak minggu lepas, address aktif on-chain meroket ke paras tertinggi hasil daripada pengumpulan sistematik oleh whales. Integrasi jambatan (cross-chain bridge) baru sedang bersedia untuk mencetuskan letupan kecairan berskala besar."
        )
    elif "real world assets" in cat_str or "rwa" in cat_str:
        return (
            "RWA", 
            "Tokenisasi aset fizikal dengan kelajuan audit on-chain yang telus dan kos pematuhan jauh lebih murah berbanding platform legasi TradFi yang mahal. Kestabilan sistem jaminan memastikan pemindahan nilai berlaku tanpa sebarang geseran.", 
            "Kemasukan TVL institusi secara agresif dikesan pada data on-chain sejak minggu lepas melalui dominasi dompet paus. Integrasi portfolio aset nyata gred tertinggi sedang mencetuskan ketidakseimbangan bekalan sebelum disedari pasaran runcit."
        )
    else:
        cat_name = categories[0] if categories else "Altcoin"
        return (
            cat_name, 
            "Menampilkan kecekapan pemprosesan data yang lebih pantas, stabil, dan kos pelaksanaan mikro berbanding purata koin Big Cap yang mahal. Peningkatan struktur teknikal ini meletakkan aset pada kedudukan kelebihan pasaran yang ketara.", 
            "Analisis data on-chain mendedahkan akumulasi berskala besar oleh dompet paus bermula minggu lepas. Integrasi pembuat pasaran (Market Maker) sedang memampatkan bekalan sebelum letupan volatiliti berlaku."
        ) 
     
# ==========================================
# 5. PENJANA INLINE KEYBOARD (NOVA - THE PERFECT DEGEN)
# ==========================================
def generate_inline_keyboard(coin_id, symbol, coin_name, contract_address=None):
    markup = InlineKeyboardMarkup(row_width=2) 
    headers = {"x-cg-demo-api-key": CG_API_KEY} if CG_API_KEY else {}
    
    url = f"{BASE_URL}/coins/{coin_id}?localization=false&tickers=true&market_data=false&community_data=false&developer_data=false"
    categories = []
    chain_name = "Native Chain" 
    asset_platform_id = ""
    
    try:
        res = requests.get(url, headers=headers)
        data = res.json()
        categories = data.get("categories", [])
        
        asset_platform_id = data.get("asset_platform_id", "")
        if asset_platform_id: chain_name = asset_platform_id.replace("-", " ").title()

        if not contract_address:
            platforms = data.get("platforms", {})
            if platforms: contract_address = list(platforms.values())[0]

        # 💡 BARIS 1: ENJIN HYPE & ALIRAN WANG (CASHTAG LIVE & DEXSCREENER)
        # (CryptoPanic DIMATIKAN SEPENUHNYA UNTUK NOVA)
        cashtag_url = f"https://twitter.com/search?q=%24{symbol}&f=live"
        dex_url = f"https://dexscreener.com/search?q={contract_address}" if contract_address else f"https://dexscreener.com/search?q={symbol}"
        markup.row(InlineKeyboardButton("🐦 Cashtag Live", url=cashtag_url), InlineKeyboardButton("📊 DexScreener", url=dex_url))
        
        # 💡 BARIS 2: DEX SNIPER (KEUTAMAAN PERTAMA UNTUK EKSEKUSI PANTAS)
        if contract_address:
            platform_id_lower = asset_platform_id.lower() if asset_platform_id else ""
            if "solana" in platform_id_lower:
                markup.row(InlineKeyboardButton("🤖 Fast Snipe on BonkBot", url=f"https://t.me/bonkbot_bot?start=ref_krypton_{contract_address}"))
            else:
                markup.row(InlineKeyboardButton("🦅 Fast Snipe on Maestro", url=f"https://t.me/MaestroSniperBot?start={contract_address}-krypton"))

        # 💡 BARIS 3: CEX SOKONGAN (HANYA BITGET/GATE JIKA ADA)
        tickers = data.get("tickers", [])
        bitget_url = gate_url = None
        for t in tickers:
            market_name = t["market"]["name"].lower()
            target_coin = t.get("target", "").upper()
            
            # Paksaan Deep-Link
            if "USDT" in target_coin or t.get("target") == "USDT":
                if "bitget" in market_name: bitget_url = f"https://www.bitget.com/spot/{symbol.upper()}USDT"
                elif "gate" in market_name: gate_url = f"https://www.gate.io/trade/{symbol.upper()}_USDT"
        
        if bitget_url: markup.row(InlineKeyboardButton("🟦 Trade on Bitget", url=bitget_url))
        elif gate_url: markup.row(InlineKeyboardButton("🟥 Trade on Gate.io", url=gate_url))
         
    except Exception as e: 
        admin_log(f"Ralat Keyboard / UI ({symbol})", e)
        print(f"[ERROR LOG] Ralat keyboard: {e}")

    return markup, categories, contract_address, chain_name

# ==========================================
# 6. ENJIN SIGNAL TELEGRAM (CLINICAL EXECUTION UI)
# ==========================================
def dispatch_signal(chat_id, coin_name, symbol, rank, ath_change, vol_multiplier, rsi, current_price, fibo, coin_id, trend_24h, vol_24h, trend_7d, passed_ca=None):
    if not TELEGRAM_TOKEN or not chat_id: return

    markup, categories, final_ca, chain_name = generate_inline_keyboard(coin_id, symbol, coin_name, contract_address=passed_ca)
    cat_name, _, _ = get_category_insight(categories) # Abaikan teks karangan
    
    safe_coin = html.escape(coin_name)
    safe_sym = html.escape(symbol)
    safe_chain = html.escape(chain_name) if chain_name else "Native Chain"
    
    diff = fibo['Fibo_100'] - fibo['Fibo_0']
    sl = fibo['Fibo_0'] * 0.95  
    tp1 = fibo['Fibo_0'] + (diff * 0.382) 
    tp2 = fibo['Fibo_0'] + (diff * 0.618) 
    tp3 = fibo['Fibo_100'] 

    vol_str = f"${vol_24h:,.0f}" if vol_24h else "N/A"
    ca_display = f"<code>{final_ca}</code>" if final_ca else "<i>No CA Found</i>"

    if rsi < 30: rsi_status = "<b>STRONG OVERSOLD</b>"
    elif rsi < 40: rsi_status = "<b>OVERSOLD</b>"
    else: rsi_status = "<b>NEUTRAL</b>"

    price_to_100 = abs(current_price - fibo['Fibo_100'])
    price_to_618 = abs(current_price - fibo['Fibo_618'])
    price_to_786 = abs(current_price - fibo['Fibo_786'])
    price_to_0 = abs(current_price - fibo['Fibo_0'])
    closest_diff = min(price_to_100, price_to_618, price_to_786, price_to_0)

    if current_price > fibo['Fibo_100']: fibo_result = "Above Peak (1.000)"
    elif current_price < fibo['Fibo_0']: fibo_result = "Below Floor (0.000)"
    elif closest_diff == price_to_100: fibo_result = "Retesting Peak (1.000)"
    elif closest_diff == price_to_618: fibo_result = "Golden Pocket (0.618)"
    elif closest_diff == price_to_786: fibo_result = "Deep Value (0.786)"
    elif closest_diff == price_to_0: fibo_result = "Absolute Bottom (0.000)"

    # POSITION SIZING BERDASARKAN KELAS ASET
    rank_int = int(rank) if str(rank).isdigit() else 999
    if "Layer 1" in cat_name or "DeFi" in cat_name or rank_int <= 50:
        risk_tier = "Tier-1 (High Conviction - Max 5% Modal)"
    elif "Meme" in cat_name or rank_int >= 200:
        risk_tier = "Tier-3 (Tactical/Spekulatif - Max 1.5% Modal)"
    else:
        risk_tier = "Tier-2 (Standard Risk - Max 3% Modal)"

        # MESEJ SUPER PADAT, ELIT DAN MISTERI
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
        f"🔥 <b>RSI (14D):</b> {rsi:.2f} ({rsi_status})\n"
        f"📊 <b>Fibo (D1):</b> {fibo_result}\n"
        "........................................................\n"
        "🛠️ <b>ALGO TRADE SETUP (Chart: D1)</b>\n"
        f"🔸 <b>Entry Zone:</b> <code>${current_price:.6f}</code> - <code>${fibo['Fibo_786']:.6f}</code>\n"
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
        bot.send_message(chat_id, msg, reply_markup=markup, disable_web_page_preview=True)
    except Exception as e:
        admin_log(f"Gagal hantar signal {symbol}", e)
        print(f"[ERROR LOG] Mesej Telegram gagal dihantar: {e}")

# ==========================================
# 6.5 ENJIN TRACKER FOMO (REAL-TIME AUTO REPLY)
# ==========================================
def run_trade_tracker_loop():
    import json
    while True:
        time.sleep(300) # Semak setiap 5 minit untuk memelihara kuota API
        if not TELEGRAM_CHAT_ID or not os.path.exists("active_trades.json"): continue
        
        try:
            with open("active_trades.json", "r") as f: trades = json.load(f)
        except: continue
        
        # Tapis hanya koin yang belum mati (belum hit SL atau TP3)
        active_items = {k: v for k, v in trades.items() if v["status"] not in ["COMPLETED", "STOP_LOSS"]}
        if not active_items: continue
        
        # Himpun semua ID koin untuk buat panggilan pukal (Batch Call)
        coin_ids = list(set([v["coin_id"] for v in active_items.values()]))
        ids_str = ",".join(coin_ids)
        
        headers = {"x-cg-demo-api-key": CG_API_KEY} if CG_API_KEY else {}
        try:
            res = requests.get(f"{BASE_URL}/simple/price?ids={ids_str}&vs_currencies=usd", headers=headers)
            if res.status_code != 200: continue
            current_prices = res.json()
        except: continue
        
        updated = False
        for msg_id, trade in active_items.items():
            c_id = trade["coin_id"]
            if c_id not in current_prices: continue
            
            price_now = current_prices[c_id]["usd"]
            status = trade["status"]
            sym = trade["symbol"]
            
            reply_text = ""
            new_status = status
            
            # 1. SEMAK STOPlOSS (KEUTAMAAN PERLINDUNGAN)
            if price_now <= trade["sl"]:
                reply_text = f"🛑 <b>{sym} — STOP LOSS HIT</b>\nProteksi modal diaktifkan pada harga <code>${price_now:.6f}</code>. Sila keluar dari pasaran."
                new_status = "STOP_LOSS"
            
            # 2. SEMAK TP3 (MAKSIMUM FOMO)
            elif price_now >= trade["tp3"] and status != "TP2_HIT":
                reply_text = f"👑 <b>{sym} — TP3 MAX TARGET HIT!</b>\nMoonshot selesai sempurna di harga <code>${price_now:.6f}</code>! 100% sasaran hancur ditewaskan. 🎉🚀"
                new_status = "COMPLETED"
                
            # 3. SEMAK TP2
            elif price_now >= trade["tp2"] and status not in ["TP2_HIT", "COMPLETED"]:
                reply_text = f"🔥 <b>{sym} — TARGET TP2 ACHIEVED!</b>\nGolden Pocket ditembus pada harga <code>${price_now:.6f}</code>. Poketkan 50% profit, biarkan baki berjalan 'Risk-Free'!"
                new_status = "TP2_HIT"
                
            # 4. SEMAK TP1
            elif price_now >= trade["tp1"] and status == "TRACKING":
                reply_text = f"✅ <b>{sym} — TARGET TP1 SECURED!</b>\nLantunan pertama disahkan pada harga <code>${price_now:.6f}</code>. Alihkan Stop Loss kau ke harga Entry (Break-Even) SEKARANG! ⚡"
                new_status = "TP1_HIT"
                
            if reply_text:
                try:
                    # Melakukan arahan REPLY tepat pada mesej asal di Telegram channel
                    bot.send_message(TELEGRAM_CHAT_ID, reply_text, reply_to_message_id=int(msg_id), parse_mode="HTML")
                    trades[msg_id]["status"] = new_status
                    updated = True
                except Exception as e:
                    print(f"[ERROR LOG] Gagal hantar reply tracker: {e}")
                    
        if updated:
            with open("active_trades.json", "w") as f: json.dump(trades, f, indent=4)

# ==========================================
# 7. ENJIN PENGIMBASAN (MACRO DEFENSE, RSI BYPASS & COOLDOWN)
# ==========================================
def run_scanner_loop():
    global is_scanning
    
    KILL_LIST = {"btc", "eth", "usdt", "usdc", "fdusd", "dai", "wbtc", "steth", "weeth", "weth", "tusd", "usde"}
    SIGNAL_COOLDOWN = {}  # 💡 MEMORY CACHE: Menyimpan log masa isyarat dihantar {coin_id: timestamp}

    while True:
        if not is_scanning:
            time.sleep(10)
            continue
            
        print("[STATUS LOG] Kitaran makro bermula. Menganalisis Cuaca Makro (BTC & Global)...")
        headers = {"x-cg-demo-api-key": CG_API_KEY} if CG_API_KEY else {}
        
        try:
            btc_res = requests.get(f"{BASE_URL}/coins/markets?vs_currency=usd&ids=bitcoin", headers=headers).json()
            btc_trend_24h = btc_res[0].get('price_change_percentage_24h', 0)
            
            if btc_trend_24h < -4.0:
                print(f"[DEFENSE MODE] BTC berdarah ({btc_trend_24h:.2f}%). Menghentikan imbasan Altcoin.")
                if ADMIN_CHAT_ID:
                    try: bot.send_message(ADMIN_CHAT_ID, f"⚠️ <b>[DEFENSE MODE AKTIF]</b> Makro BTC sedang mengalami pendarahan berisiko tinggi (<code>{btc_trend_24h:.2f}%</code>). KRYPTON V1 membekukan operasi isyarat Altcoin untuk memelihara modal. Siklus ditunda 6 Jam.", parse_mode="HTML")
                    except: pass
                time.sleep(21600) 
                continue

            global_res = requests.get(f"{BASE_URL}/global", headers=headers).json()
            btc_dominance = global_res['data']['market_cap_percentage']['btc']
            
            rsi_limit = 32 if (btc_dominance > 50.0 and btc_trend_24h < 0) else 40
            print(f"[GLOBAL PULSE] BTC.D: {btc_dominance:.2f}% | Had ketat RSI dikunci pada: {rsi_limit}")
            
        except Exception as e:
            admin_log("Ralat API Cuaca Makro (CoinGecko)", e)
            print(f"[ERROR LOG] Ralat Cuaca Makro: {e}")
            time.sleep(60)
            continue

        top_coins = []
        for page in range(5, 7): 
            if not is_scanning: break
            url = f"{BASE_URL}/coins/markets"
            params = {"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": page, "sparkline": "false"}
            try:
                response = requests.get(url, params=params, headers=headers)
                if response.status_code == 200: top_coins.extend(response.json())
                time.sleep(2)
            except: time.sleep(2)
                
        for coin in top_coins:
            if not is_scanning: break
            try:
                coin_id = coin['id']
                
                # 💡 PROTOKOL COOLDOWN: Sekat koin jika sudah dihantar dalam tempoh 24 jam (86400 saat)
                current_time = time.time()
                if coin_id in SIGNAL_COOLDOWN:
                    if current_time - SIGNAL_COOLDOWN[coin_id] < 86400:
                        continue  # Abaikan dan lompat ke koin seterusnya
                
                symbol_lower = coin['symbol'].lower()
                if symbol_lower in KILL_LIST: continue
                
                symbol = coin['symbol'].upper()
                ath_change = coin.get('ath_change_percentage')
                current_vol = coin.get('total_volume')
                
                if ath_change is None or ath_change > -50: continue
                if current_vol is None or current_vol < 500000: continue

                hist_url = f"{BASE_URL}/coins/{coin_id}/market_chart"
                hist_res = requests.get(hist_url, params={"vs_currency": "usd", "days": "30", "interval": "daily"}, headers=headers)
                
                if hist_res.status_code != 200:
                    time.sleep(2)
                    continue
                    
                data = hist_res.json()
                prices = [p[1] for p in data['prices']]
                volumes = [v[1] for v in data['total_volumes']]
                
                if len(prices) < 30: continue
                    
                avg_vol_7d = np.mean(volumes[-8:-1])
                if avg_vol_7d == 0: continue
                
                vol_mult = current_vol / avg_vol_7d
                if vol_mult < 1.5: continue
                    
                rsi_14 = calculate_rsi(prices, period=14)
                
                if vol_mult >= 2.0:
                    if rsi_14 > 50: continue 
                else:
                    if rsi_14 >= rsi_limit: continue
                    
                fibo = calculate_fibonacci_levels(prices)
                current_price = prices[-1]

                trend_7d = 0.0
                if len(prices) >= 8:
                    trend_7d = ((current_price - prices[-8]) / prices[-8]) * 100
                
                if current_price <= fibo["Fibo_618"]:
                    trend_24 = coin.get('price_change_percentage_24h', 0)
                    dispatch_signal(TELEGRAM_CHAT_ID, coin['name'], symbol, coin.get('market_cap_rank', 'N/A'), ath_change, vol_mult, rsi_14, current_price, fibo, coin_id, trend_24, current_vol, trend_7d)
                    
                    # 💡 REKOD LOG: Cop masa isyarat dihantar untuk sekatan kitaran seterusnya
                    SIGNAL_COOLDOWN[coin_id] = current_time
                    
                time.sleep(2)
            except: time.sleep(2)
                
        if is_scanning:
            if ADMIN_CHAT_ID:
                try: bot.send_message(ADMIN_CHAT_ID, "⏳ <b>[STANDBY]</b>Scanning makro Nova7 selesai. Engine Cooling (6Hrs).", parse_mode="HTML")
                except: pass
            time.sleep(21600)

# # ==========================================
# 8. TELEGRAM COMMAND HANDLERS
# ==========================================
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    bot.reply_to(message, "⚡ <b>Nova7 DiaktifkaF!</b>\nArahan tersedia: <code>/ca</code>, <code>/scan</code>, <code>/stop</code>", parse_mode="HTML")

@bot.message_handler(commands=['scan'])
def start_scan_cmd(message):
    global is_scanning
    is_scanning = True
    bot.reply_to(message, "✅ <b>Engine Nova7 Diaktifkan.</b> Bot sedang merempuh pasaran.", parse_mode="HTML")

@bot.message_handler(commands=['stop'])
def stop_scan_cmd(message):
    global is_scanning
    is_scanning = False
    bot.reply_to(message, "🛑 <b>Enjin Dihentikan Sementara.</b>", parse_mode="HTML")

@bot.message_handler(commands=['ca'])
def manual_ca_check(message):
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "⚠️ <b>Sila masukkan Contract Address atau ID.</b>", parse_mode="HTML")
        return
        
    query = args[1].lower()
    bot.reply_to(message, f"🔍 <i>Menganalisis {query}...</i>", parse_mode="HTML")
    headers = {"x-cg-demo-api-key": CG_API_KEY} if CG_API_KEY else {}
    
    try:
        passed_address = query if query.startswith("0x") else None
        search_res = requests.get(f"{BASE_URL}/search?query={query}", headers=headers).json()
        if not search_res.get("coins"):
            bot.reply_to(message, "❌ <b>Aset tidak dijumpai.</b>", parse_mode="HTML")
            return
            
        coin_id = search_res["coins"][0]["id"]
        market_res = requests.get(f"{BASE_URL}/coins/markets?vs_currency=usd&ids={coin_id}", headers=headers).json()[0]
        
        coin_name = market_res['name']
        symbol = market_res['symbol'].upper()
        rank = market_res.get('market_cap_rank', 'N/A')
        ath_change = market_res.get('ath_change_percentage', 0)
        current_price = market_res['current_price']
        trend_24 = market_res.get('price_change_percentage_24h', 0)
        vol_24 = market_res.get('total_volume', 0)
        
        hist_url = f"{BASE_URL}/coins/{coin_id}/market_chart"
        hist_data = requests.get(hist_url, params={"vs_currency": "usd", "days": "30", "interval": "daily"}, headers=headers).json()
        prices = [p[1] for p in hist_data['prices']]
        
        rsi_14 = calculate_rsi(prices, 14) if len(prices) >= 30 else 0.0
        fibo = calculate_fibonacci_levels(prices) if len(prices) >= 30 else {"Fibo_100": current_price, "Fibo_786": current_price, "Fibo_618": current_price, "Fibo_0": current_price}
        
        # MATEMATIK BARU: 1W (7D) Drop
        trend_7d = 0.0
        if len(prices) >= 8:
            trend_7d = ((current_price - prices[-8]) / prices[-8]) * 100

        dispatch_signal(TELEGRAM_CHAT_ID, coin_name, symbol, rank, ath_change, 1.0, rsi_14, current_price, fibo, coin_id, trend_24, vol_24, trend_7d, passed_ca=passed_address)
        bot.reply_to(message, "✅ <b>Analisis Selesai!</b>", parse_mode="HTML")
        
    except Exception as e:
        bot.reply_to(message, f"❌ <b>Ralat Teknikal:</b> Gagal memproses data pasaran.", parse_mode="HTML")

# ==========================================
# 9. SISTEM KAWALAN UTAMA
# ==========================================
def graceful_shutdown(*args):
    # OFFLINE MESEJ KE ADMIN SAHAJA
    if TELEGRAM_TOKEN and ADMIN_CHAT_ID:
        try: bot.send_message(ADMIN_CHAT_ID, "🔴 <b>[OFFLINE] NOVA7 DISCONNECTED.</b> Render shutting down.", parse_mode="HTML")
        except: pass
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, graceful_shutdown)
    signal.signal(signal.SIGINT, graceful_shutdown)
    
    # BOOT UP MESEJ KE ADMIN SAHAJA
    if TELEGRAM_TOKEN and ADMIN_CHAT_ID:
        try: bot.send_message(ADMIN_CHAT_ID, "🟢 <b>HELLO, NOVA7  NOW ACTIVE.</b>\nLink to Render established.", parse_mode="HTML")
        except: pass

    # 💡 HIDUPKAN ENJIN AUTOMATIK DI SINI
    threading.Thread(target=run_trade_tracker_loop, daemon=True).start()
    threading.Thread(target=run_scanner_loop, daemon=True).start()
    threading.Thread(target=bot.infinity_polling, daemon=True).start()
    
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))

