import os, time, json, requests, threading, traceback, schedule
from telebot import TeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

# ================================================================
# 1. KONFIGURASI
# ================================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
VIP_CHANNEL_ID     = os.environ.get("VIP_CHANNEL_ID")
ADMIN_ID           = os.environ.get("ADMIN_ID")
CG_API_KEY         = os.environ.get("CG_API_KEY")

bot = TeleBot(TELEGRAM_BOT_TOKEN)
START_TIME = time.time()

def alert_admin(text):
    try:
        bot.send_message(ADMIN_ID, f"🚨 <b>SYSTEM</b>\n<pre>{text[:800]}</pre>", parse_mode="HTML")
    except:
        pass

# ================================================================
# 2. PERSISTENCE — semua data kekal merentas restart
# ================================================================
SENT_POOL_FILE  = "sent_pool.json"
TRADE_LOG_FILE  = "trade_log.json"
CONFIG_FILE     = "config.json"
WARM_POOL_FILE  = "warm_pool.json"

DEFAULT_CONFIG = {
    "mc_min":            1_000_000,    # $1M
    "mc_max":          500_000_000,    # $500M
    "liq_min":           150_000,      # $150K
    "vol_mc_ratio_min":      0.05,     # 5%
    "change_24h_min":        5.0,      # +5%
    "change_5m_min":         0.5,      # +0.5%
    "atr_sl_mult":           1.5,      # SL = ATR × 1.5
    "score_pass":            4,        # lulus jika skor >= 4/5
    "score_watchlist":       3,        # watchlist jika skor >= 3/5
}

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return (default.copy() if isinstance(default, dict) else {})

def save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[WARN] save_json {path}: {e}")

SENT_POOL = load_json(SENT_POOL_FILE, {})
TRADE_LOG = load_json(TRADE_LOG_FILE, {})
CONFIG    = load_json(CONFIG_FILE, DEFAULT_CONFIG)
WARM_POOL = load_json(WARM_POOL_FILE, {})

# Merge kunci yang mungkin tidak ada dalam fail lama
for k, v in DEFAULT_CONFIG.items():
    if k not in CONFIG:
        CONFIG[k] = v

IS_SCANNING   = True
ACTIVE_TRADES = {ca: t for ca, t in TRADE_LOG.items() if not t.get('closed')}

CORE_NARRATIVES = [
    'artificial-intelligence', 'depin', 'real-world-assets-rwa',
    'layer-1', 'meme', 'layer-2', 'zero-knowledge-proofs',
    'solana-ecosystem', 'base-ecosystem', 'bitcoin-ecosystem',
]

# ================================================================
# 3. HELPER
# ================================================================
def fmt(val):
    """Format harga kriptografi dengan ketepatan sesuai."""
    if val == 0: return "0.00"
    if abs(val) < 0.000001: return f"{val:.10f}"
    if abs(val) < 0.001:    return f"{val:.8f}"
    if abs(val) < 1.0:      return f"{val:.6f}"
    if abs(val) < 1000:     return f"{val:.4f}"
    return f"{val:,.2f}"

# ================================================================
# 4. API FETCHERS
# ================================================================
def check_binance_listing(symbol):
    try:
        r = requests.get(
            f"https://api.binance.com/api/v3/ticker/price?symbol={symbol.upper()}USDT",
            timeout=3
        )
        return r.status_code == 200
    except:
        return False

def get_trending_categories():
    try:
        h = {"x-cg-demo-api-key": CG_API_KEY}
        cats = requests.get(
            "https://api.coingecko.com/api/v3/coins/categories",
            headers=h, timeout=10
        ).json()
        return [c['id'] for c in sorted(
            cats, key=lambda x: x.get('market_cap_change_24h', 0) or 0, reverse=True
        )[:3]]
    except:
        return []

def get_coins_in_category(cat_id, per_page=50):
    try:
        h = {"x-cg-demo-api-key": CG_API_KEY}
        url = (f"https://api.coingecko.com/api/v3/coins/markets"
               f"?vs_currency=usd&category={cat_id}"
               f"&order=market_cap_desc&per_page={per_page}&page=1")
        r = requests.get(url, headers=h, timeout=10).json()
        return r if isinstance(r, list) else []
    except:
        return []

def get_dex(query, search_type="symbol"):
    """Ambil data DEX terkini dari DexScreener."""
    try:
        if search_type == "symbol":
            url = f"https://api.dexscreener.com/latest/dex/search?q={query}"
        else:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{query}"

        res = requests.get(url, timeout=10).json()
        if not res.get('pairs'):
            return None

        pairs = res['pairs']
        if search_type == "symbol":
            pairs = [p for p in pairs
                     if p.get('baseToken', {}).get('symbol', '').upper() == query.upper()]
        if not pairs:
            return None

        # Ambil pair dengan kecairan tertinggi
        pair = sorted(
            pairs,
            key=lambda x: float(x.get('liquidity', {}).get('usd', 0) or 0),
            reverse=True
        )[0]

        created_at = pair.get('pairCreatedAt', 0)
        age_days   = (time.time() * 1000 - created_at) / 86_400_000 if created_at else 0

        return {
            'name':         pair.get('baseToken', {}).get('name', 'Unknown'),
            'symbol':       pair.get('baseToken', {}).get('symbol', '?'),
            'contract':     pair.get('baseToken', {}).get('address', ''),
            'price_usd':    float(pair.get('priceUsd', 0) or 0),
            'fdv':          float(pair.get('fdv', 0) or 0),
            'volume_24h':   float(pair.get('volume', {}).get('h24', 0) or 0),
            'change_24h':   float(pair.get('priceChange', {}).get('h24', 0) or 0),
            'change_5m':    float(pair.get('priceChange', {}).get('m5', 0) or 0),
            'liquidity':    float(pair.get('liquidity', {}).get('usd', 0) or 0),
            'network':      pair.get('chainId', 'unknown').upper(),
            'chain_raw':    pair.get('chainId', 'unknown').lower(),
            'pair_address': pair.get('pairAddress', ''),
            'age_display':  f"{int(age_days)}d" if age_days >= 1 else f"{int(age_days*24)}h",
        }
    except:
        return None

# ── FIX #1: Security — GoPlus sebenar untuk semua EVM chain ──────
GOPLUS_CHAIN_IDS = {
    'bsc':        '56',
    'base':       '8453',
    'ethereum':   '1',
    'eth':        '1',
    'polygon':    '137',
    'arbitrum':   '42161',
    'avalanche':  '43114',
}

def verify_security(network, contract):
    """
    Semak keselamatan token:
    - Solana: RugCheck.xyz (skor < 500 = selamat)
    - EVM: GoPlus API sebenar (semak honeypot, tax, mintable)
    BUKAN hardcoded — setiap token disemak secara live.
    """
    net = network.lower()
    try:
        if net in ('solana', 'sol'):
            r = requests.get(
                f"https://api.rugcheck.xyz/v1/tokens/{contract}/report",
                timeout=5
            ).json()
            score = r.get('score', 9999)
            if score < 300:  return True,  f"✅ SELAMAT (RugCheck: {score})"
            if score < 700:  return True,  f"⚠️ SEDERHANA (RugCheck: {score})"
            return False, f"🚨 BAHAYA (RugCheck: {score})"

        chain_id = GOPLUS_CHAIN_IDS.get(net)
        if not chain_id:
            return True, f"⚠️ Rantai '{net}' tidak disokong audit"

        r = requests.get(
            f"https://api.gopluslabs.io/api/v1/token_security/{chain_id}"
            f"?contract_addresses={contract}",
            timeout=6
        ).json()

        result = (r.get('result') or {}).get(contract.lower(), {})
        if not result:
            return True, "⚠️ Tiada rekod GoPlus"

        if result.get('is_honeypot') == '1':
            return False, "🚨 HONEYPOT!"

        flags = []
        if result.get('is_mintable') == '1':
            flags.append("Mintable")
        if result.get('is_proxy') == '1':
            flags.append("Proxy")
        buy_tax  = float(result.get('buy_tax',  0) or 0)
        sell_tax = float(result.get('sell_tax', 0) or 0)
        if buy_tax  > 10: flags.append(f"Buy Tax {buy_tax:.0f}%")
        if sell_tax > 10: flags.append(f"Sell Tax {sell_tax:.0f}%")

        if flags:
            return False, f"⚠️ RISIKO: {', '.join(flags)}"
        return True, "✅ SELAMAT (GoPlus)"

    except Exception as e:
        return True, f"⚠️ Audit gagal: {str(e)[:40]}"

# ── FIX #2: Teknikal — H4 candle (bukan daily) ──────────────────
def get_technicals_h4(network, pair_address):
    """
    RSI(14) Wilder + ATR(14) Wilder + Fibonacci
    menggunakan H4 candles dari GeckoTerminal.
    H4 relevan untuk token intraday micro-cap.
    Returns: rsi_label, fibo_label, atr, swing_high, swing_low
    """
    try:
        if not pair_address:
            return "N/A", "N/A", 0, 0, 0

        net_map = {
            'solana':'solana','base':'base','bsc':'bsc',
            'ethereum':'eth','eth':'eth','ton':'ton','sui':'sui',
        }
        gt_net = net_map.get(network.lower(), network.lower())

        # H4 = hour aggregate 4, 60 candles = ~10 hari
        url = (f"https://api.geckoterminal.com/api/v2/networks/{gt_net}"
               f"/pools/{pair_address}/ohlcv/hour?aggregate=4&limit=60")
        res     = requests.get(url, timeout=6).json()
        ohlcv   = res.get('data', {}).get('attributes', {}).get('ohlcv_list', [])

        if len(ohlcv) < 14:
            return "Koin Baru (<14 H4)", "Data Terhad", 0, 0, 0

        candles = list(reversed(ohlcv))          # oldest → newest
        closes  = [float(c[4]) for c in candles]
        highs   = [float(c[2]) for c in candles]
        lows    = [float(c[3]) for c in candles]

        # ── RSI(14) Wilder ──────────────────────────────────────
        gains, losses = [], []
        for i in range(1, len(closes)):
            d = closes[i] - closes[i-1]
            gains.append(max(d, 0.0))
            losses.append(max(-d, 0.0))

        avg_g = sum(gains[:14])  / 14
        avg_l = sum(losses[:14]) / 14
        for i in range(14, len(gains)):
            avg_g = (avg_g * 13 + gains[i])  / 14
            avg_l = (avg_l * 13 + losses[i]) / 14

        rsi     = 100.0 if avg_l == 0 else 100 - (100 / (1 + avg_g / avg_l))
        rsi_lbl = (f"{rsi:.1f} 🔴 Overbought" if rsi >= 70
                   else f"{rsi:.1f} 🟢 Oversold"  if rsi <= 35
                   else f"{rsi:.1f} ⚪ Neutral")

        # ── ATR(14) Wilder ──────────────────────────────────────
        trs = [
            max(highs[i]-lows[i],
                abs(highs[i]-closes[i-1]),
                abs(lows[i] -closes[i-1]))
            for i in range(1, len(closes))
        ]
        atr = sum(trs[:14]) / 14
        for t in trs[14:]:
            atr = (atr * 13 + t) / 14

        # ── Fibonacci dari 20 candle terakhir ───────────────────
        n          = min(20, len(candles))
        swing_high = max(highs[-n:])
        swing_low  = min(lows[-n:])
        price      = closes[-1]
        rng        = swing_high - swing_low

        if rng > 0:
            f236 = swing_high - 0.236 * rng
            f382 = swing_high - 0.382 * rng
            f500 = swing_high - 0.500 * rng
            f618 = swing_high - 0.618 * rng
            f786 = swing_high - 0.786 * rng

            def near(a, b, tol=0.04):
                return abs(a-b)/max(b, 1e-12) <= tol

            if price >= swing_high:         fibo_lbl = f"Breakout ATH (${fmt(swing_high)})"
            elif near(price, f618):         fibo_lbl = f"Golden Pocket 0.618 (${fmt(f618)})"
            elif near(price, f786):         fibo_lbl = f"Zon 0.786 (${fmt(f786)})"
            elif near(price, f382):         fibo_lbl = f"Zon 0.382 (${fmt(f382)})"
            elif near(price, f500):         fibo_lbl = f"Zon 0.50 (${fmt(f500)})"
            elif price <= swing_low:        fibo_lbl = f"Lantai Support (${fmt(swing_low)})"
            else: fibo_lbl = f"S: ${fmt(swing_low)} | R: ${fmt(swing_high)}"
        else:
            fibo_lbl = "Data Tidak Mencukupi"

        return rsi_lbl, fibo_lbl, atr, swing_high, swing_low

    except:
        return "N/A", "N/A", 0, 0, 0

# ================================================================
# 5. FILTER ENGINE — FIX #3: skor bermula dari 0, bukan 2
# ================================================================
def run_filter(dex):
    """
    Skor 0–5. Veto keras untuk MC dan kecairan.
    Bermula dari 0 — token mesti buktikan kelayakan.
    """
    cfg = CONFIG

    # ── Veto keras ──────────────────────────────────────────────
    if not (cfg['mc_min'] <= dex['fdv'] <= cfg['mc_max']):
        return False, f"VETO MC: ${dex['fdv']/1e6:.2f}M luar julat", False
    if dex['liquidity'] < cfg['liq_min']:
        return False, f"VETO Kecairan: ${dex['liquidity']/1e3:.0f}K terlalu rendah", False

    score, fails = 0, []

    # Kriteria 1: Nisbah Vol/MC (aktiviti kecairan)
    vol_mc = dex['volume_24h'] / max(dex['fdv'], 1)
    if vol_mc >= cfg['vol_mc_ratio_min']:
        score += 1
    else:
        fails.append(f"Vol/MC {vol_mc*100:.1f}%<{cfg['vol_mc_ratio_min']*100:.0f}%")

    # Kriteria 2: Momentum 24H
    if dex['change_24h'] >= cfg['change_24h_min']:
        score += 1
    else:
        fails.append(f"24H {dex['change_24h']:.1f}%<{cfg['change_24h_min']:.0f}%")

    # Kriteria 3: Isyarat intraday 5M
    if dex['change_5m'] >= cfg['change_5m_min']:
        score += 1
    else:
        fails.append(f"5M {dex['change_5m']:.2f}%")

    # Kriteria 4: Isipadu minimum bermakna
    if dex['volume_24h'] >= cfg['liq_min']:
        score += 1
    else:
        fails.append(f"Vol24H ${dex['volume_24h']/1e3:.0f}K rendah")

    # Kriteria 5: Kecairan kukuh (2× minimum)
    if dex['liquidity'] >= cfg['liq_min'] * 2:
        score += 1
    else:
        fails.append("Kecairan sederhana")

    reason = " | ".join(fails) if fails else "BERSIH 🎯"

    if score >= cfg['score_pass']:
        return True,  f"Skor {score}/5: {reason}", False
    if score >= cfg['score_watchlist']:
        return False, f"Skor {score}/5 (Watchlist): {reason}", True
    return False, f"Skor {score}/5 (Tolak): {reason}", False

# ================================================================
# 6. SIGNAL — FIX #4+#5: SL dari ATR, TP dari Fibonacci
# ================================================================
def send_signal(coin, dex, narrative, verdict="ALPHA ACTIVE", target=None):
    """
    SL  = entry - (ATR × multiplier)       [FIX: bukan ATR×0.40 rekaan]
    TP1 = swing_high                        [FIX: rintangan pasaran sebenar]
    TP2 = swing_low + range × 1.618         [FIX: Fib extension 1.618]
    TP3 = swing_low + range × 2.618         [FIX: Fib extension 2.618]
    SECURITY = GoPlus/RugCheck sebenar      [FIX: bukan hardcoded]
    RSI = H4 candle                         [FIX: bukan daily]
    """
    if target is None:
        target = VIP_CHANNEL_ID

    is_safe, sec_label = verify_security(dex['network'], dex['contract'])
    if not is_safe:
        alert_admin(f"🚨 Security GAGAL: {coin['symbol']} — {sec_label}")
        return

    rsi_lbl, fibo_lbl, atr, swing_high, swing_low = get_technicals_h4(
        dex['network'], dex['pair_address']
    )

    entry    = dex['price_usd']
    atr_mult = CONFIG.get('atr_sl_mult', 1.5)

    # ── SL berdasarkan ATR × multiplier ─────────────────────────
    if atr > 0:
        sl = entry - (atr * atr_mult)
    else:
        sl = entry * 0.85           # fallback -15% jika tiada ATR
    sl = max(sl, entry * 0.60)      # floor: tidak lebih dari -40%
    sl = max(sl, 0.0)

    risk = entry - sl if entry > sl else entry * 0.15

    # ── TP berdasarkan Fibonacci extension ──────────────────────
    if swing_high > 0 and swing_low > 0 and swing_high > swing_low:
        rng = swing_high - swing_low
        tp1 = swing_high                  # rintangan semasa
        tp2 = swing_low + rng * 1.618     # Fib extension 1.618
        tp3 = swing_low + rng * 2.618     # Fib extension 2.618
    else:
        tp1 = entry * 1.20
        tp2 = entry * 1.50
        tp3 = entry * 2.00

    # Pastikan TP lebih tinggi dari entry
    tp1 = max(tp1, entry * 1.10)
    tp2 = max(tp2, entry * 1.30)
    tp3 = max(tp3, entry * 1.80)

    rr1 = (tp1 - entry) / risk if risk > 0 else 0
    rr2 = (tp2 - entry) / risk if risk > 0 else 0
    rr3 = (tp3 - entry) / risk if risk > 0 else 0

    sym  = dex['symbol'].upper()
    t24  = dex['change_24h']
    m5   = dex['change_5m']
    turn = dex['volume_24h'] / max(dex['liquidity'], 1)

    markup = InlineKeyboardMarkup()
    if check_binance_listing(sym):
        markup.row(InlineKeyboardButton(
            "🟧 BINANCE", url=f"https://www.binance.com/en/trade/{sym}_USDT"
        ))
    elif dex['chain_raw'] in ('solana', 'sol'):
        markup.row(InlineKeyboardButton(
            "🔫 BONKBOT", url=f"https://t.me/bonkbot_bot?start={dex['contract']}"
        ))
    else:
        markup.row(InlineKeyboardButton(
            "🦄 MAESTRO", url=f"https://t.me/maestro?start={dex['contract']}"
        ))
    markup.row(
        InlineKeyboardButton(
            "📊 DexScreener",
            url=f"https://dexscreener.com/{dex['chain_raw']}/{dex['contract']}"
        ),
        InlineKeyboardButton(
            "🔍 GoPlus",
            url=f"https://gopluslabs.io/token-security/{dex['chain_raw']}/{dex['contract']}"
        ),
    )

    msg = (
        f"⚡ <b>QUANT INSIGHT — {narrative.upper()}</b>\n\n"
        f"┌ <b>ASET</b>\n"
        f"├ Token : {dex['name']} (<code>${sym}</code>)\n"
        f"└ CA    : <code>{dex['contract']}</code>\n\n"
        f"┌ <b>METRIK PASARAN (LIVE)</b>\n"
        f"├ FDV      : <code>${dex['fdv']/1e6:.2f}M</code>\n"
        f"├ Vol 24H  : <code>${dex['volume_24h']/1e3:.0f}K</code>\n"
        f"├ Turnover : <code>{turn:.1f}x Vol/Liq</code>\n"
        f"└ Umur     : <code>{dex['age_display']}</code>\n\n"
        f"┌ <b>STRUKTUR TEKNIKAL (H4)</b>\n"
        f"├ Trend 24H: <code>{f'+{t24:.2f}' if t24>=0 else f'{t24:.2f}'}%</code>\n"
        f"├ 5M Sniper: <code>{f'+{m5:.2f}' if m5>=0 else f'{m5:.2f}'}%</code>\n"
        f"├ RSI (H4) : <code>{rsi_lbl}</code>\n"
        f"├ ATR (H4) : <code>${fmt(atr)}</code>\n"
        f"└ Fibonacci : <code>{fibo_lbl}</code>\n\n"
        f"🎯 <b>TRADE SETUP</b>\n"
        f"• ENTRY : <code>${fmt(entry)}</code>\n"
        f"• SL    : <code>${fmt(sl)}</code>  [{(entry-sl)/entry*100:.1f}%]  ATR×{atr_mult}\n"
        f"• TP1   : <code>${fmt(tp1)}</code>  [+{(tp1-entry)/entry*100:.1f}%]  RR 1:{rr1:.1f}  Fib S.High\n"
        f"• TP2   : <code>${fmt(tp2)}</code>  [+{(tp2-entry)/entry*100:.1f}%]  RR 1:{rr2:.1f}  Fib 1.618\n"
        f"• TP3   : <code>${fmt(tp3)}</code>  [+{(tp3-entry)/entry*100:.1f}%]  RR 1:{rr3:.1f}  Fib 2.618\n\n"
        f"🛡️ <b>KESELAMATAN</b>\n"
        f"• Network : {dex['network']}\n"
        f"• Audit   : <b>{sec_label}</b>\n\n"
        f"🦅 <b>VERDICT: {verdict}</b>"
    )

    try:
        sent = bot.send_message(
            target, msg, parse_mode="HTML",
            reply_markup=markup, disable_web_page_preview=True
        )
        TRADE_LOG[dex['contract']] = {
            'symbol':   sym,        'name':      dex['name'],
            'entry':    entry,      'sl':        sl,
            'tp1':      tp1,        'tp2':       tp2,         'tp3':       tp3,
            'sent_at':  int(time.time()),
            'narrative':narrative,  'network':   dex['network'],
            'rsi':      rsi_lbl,    'atr':       atr,
            'atr_mult': atr_mult,
            'tp1_hit':  False,      'tp2_hit':   False,        'tp3_hit':   False,
            'sl_hit':   False,      'closed':    False,
            'msg_id':   sent.message_id,
            'rr1':      round(rr1,2), 'rr2': round(rr2,2), 'rr3': round(rr3,2),
        }
        ACTIVE_TRADES[dex['contract']] = TRADE_LOG[dex['contract']]
        save_json(TRADE_LOG_FILE, TRADE_LOG)

    except Exception as e:
        alert_admin(f"Gagal hantar signal {sym}: {e}")

# ================================================================
# 7. TRADE MONITOR
# ================================================================
def monitor_active_trades():
    global ACTIVE_TRADES, TRADE_LOG
    if not ACTIVE_TRADES:
        return

    to_close = []
    for ca, trade in list(ACTIVE_TRADES.items()):
        try:
            dex = get_dex(ca, "ca")
            if not dex:
                continue
            cp  = dex['price_usd']
            sym = trade['symbol']
            mid = trade.get('msg_id')

            def notify(text, ca=ca):
                kw = {'parse_mode': 'HTML'}
                if mid: kw['reply_to_message_id'] = mid
                bot.send_message(VIP_CHANNEL_ID, text, **kw)

            if cp >= trade['tp1'] and not trade['tp1_hit']:
                trade['tp1_hit'] = True
                notify(f"✅ <b>{sym}</b> TP1!\nAlih SL → BE: <code>${fmt(trade['entry'])}</code>")
            if cp >= trade['tp2'] and not trade['tp2_hit']:
                trade['tp2_hit'] = True
                notify(f"🚀 <b>{sym}</b> TP2!\nTrail SL → TP1: <code>${fmt(trade['tp1'])}</code>")
            if cp >= trade['tp3'] and not trade['tp3_hit']:
                trade['tp3_hit'] = True
                notify(f"🏆 <b>{sym}</b> TP3 MOONSHOT!\nTutup di <code>${fmt(cp)}</code>")
                trade['closed'] = True
                to_close.append(ca)
            elif cp <= trade['sl'] and not trade['sl_hit']:
                trade['sl_hit'] = True
                notify(f"❌ <b>{sym}</b> SL HIT.\nTrade ditutup: <code>${fmt(cp)}</code>")
                trade['closed'] = True
                to_close.append(ca)

            TRADE_LOG[ca] = trade
        except:
            pass

    for ca in to_close:
        ACTIVE_TRADES.pop(ca, None)
    if to_close:
        save_json(TRADE_LOG_FILE, TRADE_LOG)

# ================================================================
# 8. SCANNER
# ================================================================
def clean_pools():
    global SENT_POOL
    now     = time.time()
    expired = [k for k, v in SENT_POOL.items() if now - v > 3600]
    for k in expired:
        del SENT_POOL[k]
    if expired:
        save_json(SENT_POOL_FILE, SENT_POOL)

def process_warm_pool():
    global WARM_POOL, SENT_POOL
    to_remove = []
    for sym, ts in list(WARM_POOL.items()):
        if time.time() - ts > 3600:
            to_remove.append(sym)
            continue
        dex = get_dex(sym)
        if not dex:
            continue
        passed, reason, is_warm = run_filter(dex)
        if passed:
            if sym in SENT_POOL and time.time() - SENT_POOL[sym] < 3600:
                to_remove.append(sym)
                continue
            send_signal(
                {'name': dex['name'], 'symbol': sym, 'contract': dex['contract']},
                dex, "🔥 WATCHLIST BREAKOUT", verdict="WATCHLIST SNIPER 🎯"
            )
            SENT_POOL[sym] = time.time()
            save_json(SENT_POOL_FILE, SENT_POOL)
            to_remove.append(sym)
        elif not is_warm:
            to_remove.append(sym)
    for sym in to_remove:
        WARM_POOL.pop(sym, None)
    save_json(WARM_POOL_FILE, WARM_POOL)

def run_scan(categories, max_coins=20, label="ENJIN"):
    global WARM_POOL, SENT_POOL
    clean_pools()
    for cat in categories:
        print(f"[{label}] Sektor: {cat}")
        coins = get_coins_in_category(cat, per_page=50)
        for coin in coins[:max_coins]:
            sym = coin['symbol'].upper()
            if sym in WARM_POOL:
                continue
            if sym in SENT_POOL and time.time() - SENT_POOL[sym] < 3600:
                continue
            dex = get_dex(sym)
            if not dex:
                continue
            passed, reason, is_warm = run_filter(dex)
            if passed:
                send_signal(
                    {'name': dex['name'], 'symbol': sym, 'contract': dex['contract']},
                    dex, f"{label} | {cat}", verdict=f"{label} 🎯"
                )
                SENT_POOL[sym] = time.time()
                save_json(SENT_POOL_FILE, SENT_POOL)
            elif is_warm:
                WARM_POOL[sym] = time.time()
        time.sleep(5)
    save_json(WARM_POOL_FILE, WARM_POOL)

def main_scan():
    if not IS_SCANNING:
        return
    try:
        process_warm_pool()
        run_scan(CORE_NARRATIVES, 20, "ENJIN 1")
        trending = get_trending_categories()
        if trending:
            run_scan(trending, 10, "ENJIN 2")
    except Exception as e:
        alert_admin(f"CRASH SCAN:\n{traceback.format_exc()[:400]}")

# ================================================================
# 9. JOURNAL — auto setiap Ahad 21:00 + paksa manual
# ================================================================
def generate_journal(label="Mingguan", days=7):
    now    = time.time()
    cutoff = now - days * 86400
    trades = [t for t in TRADE_LOG.values() if t.get('sent_at', 0) >= cutoff]

    if not trades:
        return f"📓 <b>JOURNAL {label.upper()}</b>\n\nTiada signal dalam {days} hari lepas."

    total   = len(trades)
    tp1_n   = sum(1 for t in trades if t.get('tp1_hit'))
    tp2_n   = sum(1 for t in trades if t.get('tp2_hit'))
    tp3_n   = sum(1 for t in trades if t.get('tp3_hit'))
    sl_n    = sum(1 for t in trades if t.get('sl_hit'))
    open_n  = sum(1 for t in trades if not t.get('closed'))
    wr      = tp1_n / total * 100 if total else 0

    nets = {}
    for t in trades:
        n = t.get('network', '?')
        nets[n] = nets.get(n, 0) + 1
    net_str = " | ".join(f"{k}:{v}" for k, v in sorted(nets.items(), key=lambda x: -x[1]))

    best  = max(trades, key=lambda t: t.get('tp3_hit',0)*3+t.get('tp2_hit',0)*2+t.get('tp1_hit',0), default=None)
    losers = [t for t in trades if t.get('sl_hit')]

    lines = [
        f"📓 <b>JOURNAL {label.upper()}</b>",
        f"📅 {datetime.fromtimestamp(cutoff).strftime('%d/%m')} – {datetime.now().strftime('%d/%m/%Y %H:%M')}",
        "",
        "<b>📊 RINGKASAN</b>",
        f"├ Total Signal : <code>{total}</code>",
        f"├ TP1 Secured  : <code>{tp1_n}  ({wr:.0f}%)</code>",
        f"├ TP2 Secured  : <code>{tp2_n}  ({tp2_n/total*100:.0f}%)</code>",
        f"├ TP3 Moonshot : <code>{tp3_n}  ({tp3_n/total*100:.0f}%)</code>",
        f"├ SL Hit       : <code>{sl_n}   ({sl_n/total*100:.0f}%)</code>",
        f"└ Masih Buka   : <code>{open_n}</code>",
        "",
        f"<b>🌐 RANGKAIAN</b>",
        f"<code>{net_str}</code>",
    ]

    if best:
        tier = ("TP3 🏆" if best.get('tp3_hit')
                else "TP2 🚀" if best.get('tp2_hit')
                else "TP1 ✅" if best.get('tp1_hit')
                else "Open ⏳")
        lines += ["", f"<b>⭐ TERBAIK:</b> {best['symbol']} → {tier}"]

    if losers:
        syms = ", ".join(t['symbol'] for t in losers[:5])
        lines += [f"<b>❌ SL HIT:</b> {syms}"]

    lines += [
        "",
        "<b>📈 PRESTASI</b>",
        f"TP1: {wr:.0f}%  TP2: {tp2_n/total*100:.0f}%  TP3: {tp3_n/total*100:.0f}%  SL: {sl_n/total*100:.0f}%",
    ]
    return "\n".join(lines)

def send_weekly_journal():
    report = generate_journal("Mingguan", days=7)
    try:
        bot.send_message(ADMIN_ID,       report, parse_mode="HTML")
        bot.send_message(VIP_CHANNEL_ID, report, parse_mode="HTML")
    except Exception as e:
        alert_admin(f"Gagal hantar journal: {e}")

# ================================================================
# 10. TELEGRAM COMMANDS
# ================================================================
_force_cache = {}

@bot.message_handler(commands=['start', 'menu'])
def cmd_start(msg):
    cid = str(msg.chat.id)
    if cid != str(ADMIN_ID): return
    cfg = CONFIG
    uptime_m = int((time.time() - START_TIME) / 60)

    text = (
        f"🤖 <b>ALPHA SIGNAL BOT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Uptime     : <code>{uptime_m}m</code>\n"
        f"🔍 Watchlist  : <code>{len(WARM_POOL)} token</code>\n"
        f"📊 Trade Aktif: <code>{len(ACTIVE_TRADES)}</code>\n"
        f"🔧 Scan       : <code>{'✅ AKTIF' if IS_SCANNING else '⛔ STOP'}</code>\n\n"
        f"<b>⚙️ Filter Semasa:</b>\n"
        f"MC    : ${cfg['mc_min']/1e6:.0f}M – ${cfg['mc_max']/1e6:.0f}M\n"
        f"Liq   : ${cfg['liq_min']/1e3:.0f}K\n"
        f"Vol/MC: {cfg['vol_mc_ratio_min']*100:.0f}%  |  24H: +{cfg['change_24h_min']:.0f}%\n"
        f"ATR×  : {cfg['atr_sl_mult']}  |  Skor Lulus: {cfg['score_pass']}/5\n"
    )

    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📊 Status",      callback_data="status"),
        InlineKeyboardButton("📓 Journal",     callback_data="journal"),
        InlineKeyboardButton("🔧 Edit Filter", callback_data="edit_filter"),
        InlineKeyboardButton("🎯 Paksa Pair",  callback_data="help_pair"),
        InlineKeyboardButton("▶️ Mula Scan",   callback_data="scan_on"),
        InlineKeyboardButton("⏸ Henti Scan",  callback_data="scan_off"),
    )
    bot.send_message(cid, text, parse_mode="HTML", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith('force_signal:'))
def cb_force_signal(call):
    if str(call.message.chat.id) != str(ADMIN_ID): return
    bot.answer_callback_query(call.id)
    parts = call.data.split(':', 2)
    ca    = parts[1]
    if ca in _force_cache:
        cached = _force_cache.pop(ca)
        def _do():
            send_signal(
                {'name': cached['name'], 'symbol': cached['symbol'], 'contract': ca},
                cached['dex'], "🎯 FORCE PAIR", verdict="MANUAL ENTRY 🎯"
            )
        threading.Thread(target=_do).start()
        bot.send_message(
            call.message.chat.id,
            f"📡 Signal <b>{cached['symbol']}</b> dihantar!", parse_mode="HTML"
        )
    else:
        bot.send_message(call.message.chat.id, "⚠️ Cache tamat. Cuba /pair semula.")


@bot.callback_query_handler(func=lambda c: c.data == 'cancel_force')
def cb_cancel(call):
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "❌ Dibatalkan.")


@bot.callback_query_handler(func=lambda c: True)
def cb_main(call):
    global IS_SCANNING
    cid = str(call.message.chat.id)
    if cid != str(ADMIN_ID): return
    bot.answer_callback_query(call.id)
    d = call.data

    if d == "status":
        cfg = CONFIG
        text = (
            f"📊 <b>STATUS</b>\n\n"
            f"Scan  : {'🟢 AKTIF' if IS_SCANNING else '🔴 STOP'}\n"
            f"WL    : {len(WARM_POOL)}  |  Trade: {len(ACTIVE_TRADES)}\n\n"
            f"<b>Config:</b>\n"
            f"MC    : ${cfg['mc_min']/1e6:.0f}M–${cfg['mc_max']/1e6:.0f}M\n"
            f"Liq   : ${cfg['liq_min']/1e3:.0f}K\n"
            f"Vol/MC: {cfg['vol_mc_ratio_min']*100:.0f}%\n"
            f"24H   : +{cfg['change_24h_min']:.0f}%\n"
            f"ATR×  : {cfg['atr_sl_mult']}\n"
            f"Pass  : {cfg['score_pass']}/5\n"
        )
        bot.send_message(cid, text, parse_mode="HTML")

    elif d == "journal":
        bot.send_message(cid, generate_journal("Terkini", days=7), parse_mode="HTML")

    elif d == "edit_filter":
        text = (
            f"🔧 <b>EDIT FILTER</b>\n\n"
            f"<code>/setmc [min_M] [max_M]</code>\n"
            f"  Contoh: /setmc 1 500\n\n"
            f"<code>/setliq [nilai_K]</code>\n"
            f"  Contoh: /setliq 150\n\n"
            f"<code>/setvol [%]</code>\n"
            f"  Contoh: /setvol 5\n\n"
            f"<code>/set24h [%]</code>\n"
            f"  Contoh: /set24h 5\n\n"
            f"<code>/setatr [x]</code>\n"
            f"  Contoh: /setatr 1.5\n\n"
            f"<code>/setpass [1-5]</code>\n"
            f"  Contoh: /setpass 4"
        )
        bot.send_message(cid, text, parse_mode="HTML")

    elif d == "help_pair":
        bot.send_message(
            cid,
            "🎯 <b>PAKSA PAIR</b>\n\n"
            "Taip: <code>/pair [SYMBOL atau CA]</code>\n\n"
            "Contoh:\n"
            "<code>/pair PEPE</code>\n"
            "<code>/pair 0x1234abc...</code>",
            parse_mode="HTML"
        )

    elif d == "scan_on":
        IS_SCANNING = True
        bot.send_message(cid, "▶️ Scan DIAKTIFKAN.")
        threading.Thread(target=main_scan).start()

    elif d == "scan_off":
        IS_SCANNING = False
        bot.send_message(cid, "⏸ Scan DIBERHENTIKAN.")


# ── Filter editing commands ──────────────────────────────────────
@bot.message_handler(commands=['setmc'])
def cmd_setmc(msg):
    if str(msg.chat.id) != str(ADMIN_ID): return
    try:
        p = msg.text.split()
        CONFIG['mc_min'] = float(p[1]) * 1e6
        CONFIG['mc_max'] = float(p[2]) * 1e6
        save_json(CONFIG_FILE, CONFIG)
        bot.reply_to(msg, f"✅ MC: ${CONFIG['mc_min']/1e6:.1f}M – ${CONFIG['mc_max']/1e6:.1f}M")
    except:
        bot.reply_to(msg, "❌ Format: /setmc [min_M] [max_M]  →  /setmc 1 500")

@bot.message_handler(commands=['setliq'])
def cmd_setliq(msg):
    if str(msg.chat.id) != str(ADMIN_ID): return
    try:
        val = float(msg.text.split()[1]) * 1000
        CONFIG['liq_min'] = val
        save_json(CONFIG_FILE, CONFIG)
        bot.reply_to(msg, f"✅ Liq min: ${val/1e3:.0f}K")
    except:
        bot.reply_to(msg, "❌ Format: /setliq [nilai_K]  →  /setliq 150")

@bot.message_handler(commands=['setvol'])
def cmd_setvol(msg):
    if str(msg.chat.id) != str(ADMIN_ID): return
    try:
        val = float(msg.text.split()[1]) / 100
        CONFIG['vol_mc_ratio_min'] = val
        save_json(CONFIG_FILE, CONFIG)
        bot.reply_to(msg, f"✅ Vol/MC min: {val*100:.1f}%")
    except:
        bot.reply_to(msg, "❌ Format: /setvol [%]  →  /setvol 5")

@bot.message_handler(commands=['set24h'])
def cmd_set24h(msg):
    if str(msg.chat.id) != str(ADMIN_ID): return
    try:
        val = float(msg.text.split()[1])
        CONFIG['change_24h_min'] = val
        save_json(CONFIG_FILE, CONFIG)
        bot.reply_to(msg, f"✅ 24H min: +{val:.1f}%")
    except:
        bot.reply_to(msg, "❌ Format: /set24h [%]  →  /set24h 5")

@bot.message_handler(commands=['setatr'])
def cmd_setatr(msg):
    if str(msg.chat.id) != str(ADMIN_ID): return
    try:
        val = float(msg.text.split()[1])
        CONFIG['atr_sl_mult'] = val
        save_json(CONFIG_FILE, CONFIG)
        bot.reply_to(msg, f"✅ ATR SL multiplier: {val}x")
    except:
        bot.reply_to(msg, "❌ Format: /setatr [x]  →  /setatr 1.5")

@bot.message_handler(commands=['setpass'])
def cmd_setpass(msg):
    if str(msg.chat.id) != str(ADMIN_ID): return
    try:
        val = int(msg.text.split()[1])
        if not 1 <= val <= 5: raise ValueError
        CONFIG['score_pass'] = val
        save_json(CONFIG_FILE, CONFIG)
        bot.reply_to(msg, f"✅ Skor minimum lulus: {val}/5")
    except:
        bot.reply_to(msg, "❌ Format: /setpass [1-5]  →  /setpass 4")


# ── Force Pair ───────────────────────────────────────────────────
@bot.message_handler(commands=['pair'])
def cmd_pair(msg):
    if str(msg.chat.id) != str(ADMIN_ID): return
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(msg, "❌ Format: /pair [SYMBOL atau CA]")
        return
    query = parts[1].strip()
    bot.reply_to(msg, f"🔍 Menyemak <code>{query}</code>...", parse_mode="HTML")

    def do_lookup():
        stype = "ca" if len(query) > 20 else "symbol"
        dex   = get_dex(query, stype)
        if not dex:
            bot.send_message(msg.chat.id, f"❌ Tiada data DEX untuk <code>{query}</code>", parse_mode="HTML")
            return

        is_safe, sec_lbl = verify_security(dex['network'], dex['contract'])
        passed, reason, _ = run_filter(dex)
        rsi_lbl, fibo_lbl, atr, sh, sl_fib = get_technicals_h4(dex['network'], dex['pair_address'])

        status = (
            f"🔍 <b>RESULT: {dex['symbol'].upper()}</b>\n\n"
            f"MC       : ${dex['fdv']/1e6:.2f}M\n"
            f"Liq      : ${dex['liquidity']/1e3:.0f}K\n"
            f"Vol 24H  : ${dex['volume_24h']/1e3:.0f}K\n"
            f"24H      : {dex['change_24h']:+.2f}%\n"
            f"5M       : {dex['change_5m']:+.2f}%\n"
            f"RSI (H4) : {rsi_lbl}\n"
            f"Fibo     : {fibo_lbl}\n"
            f"ATR (H4) : ${fmt(atr)}\n"
            f"Security : {sec_lbl}\n\n"
            f"Filter   : <b>{reason}</b>\n\n"
            f"Nak paksa signal walaupun gagal filter?"
        )

        kb = InlineKeyboardMarkup()
        kb.row(
            InlineKeyboardButton(
                "✅ PAKSA SIGNAL",
                callback_data=f"force_signal:{dex['contract']}:{dex['symbol']}"
            ),
            InlineKeyboardButton("❌ Batal", callback_data="cancel_force"),
        )

        _force_cache[dex['contract']] = {
            'dex':    dex,
            'symbol': dex['symbol'].upper(),
            'name':   dex['name'],
        }
        bot.send_message(msg.chat.id, status, parse_mode="HTML", reply_markup=kb)

    threading.Thread(target=do_lookup).start()


# ── Journal Manual ───────────────────────────────────────────────
@bot.message_handler(commands=['journal'])
def cmd_journal(msg):
    if str(msg.chat.id) != str(ADMIN_ID): return
    parts = msg.text.split()
    days  = int(parts[1]) if len(parts) > 1 else 7
    bot.reply_to(msg, generate_journal(f"Manual ({days}d)", days=days), parse_mode="HTML")


# ── Lain-lain ────────────────────────────────────────────────────
@bot.message_handler(commands=['scan'])
def cmd_scan(msg):
    if str(msg.chat.id) != str(ADMIN_ID): return
    bot.reply_to(msg, "⚙️ Kitaran scan dipaksa...")
    threading.Thread(target=main_scan).start()

@bot.message_handler(commands=['status'])
def cmd_status(msg):
    if str(msg.chat.id) != str(ADMIN_ID): return
    cfg = CONFIG
    bot.reply_to(msg, (
        f"📊 <b>STATUS</b>\n"
        f"Scan : {'🟢' if IS_SCANNING else '🔴'}\n"
        f"WL   : {len(WARM_POOL)}  Trade: {len(ACTIVE_TRADES)}\n\n"
        f"MC  : ${cfg['mc_min']/1e6:.0f}M–${cfg['mc_max']/1e6:.0f}M\n"
        f"Liq : ${cfg['liq_min']/1e3:.0f}K\n"
        f"ATR×: {cfg['atr_sl_mult']}  Pass:{cfg['score_pass']}/5"
    ), parse_mode="HTML")

@bot.message_handler(commands=['help'])
def cmd_help(msg):
    if str(msg.chat.id) != str(ADMIN_ID): return
    bot.reply_to(msg, (
        "📖 <b>SENARAI ARAHAN</b>\n\n"
        "/start    — Menu utama\n"
        "/status   — Status semasa\n"
        "/scan     — Paksa kitaran scan\n"
        "/pair [X] — Semak & paksa signal token\n"
        "/journal [hari] — Jana laporan\n\n"
        "<b>Edit Filter:</b>\n"
        "/setmc [min] [max]  — MC (dalam M)\n"
        "/setliq [K]         — Kecairan min\n"
        "/setvol [%]         — Vol/MC min\n"
        "/set24h [%]         — Perubahan 24H min\n"
        "/setatr [x]         — Pengganda ATR SL\n"
        "/setpass [1-5]      — Skor minimum lulus"
    ), parse_mode="HTML")

# ================================================================
# 11. SCHEDULER & MAIN
# ================================================================
def run_scheduler():
    schedule.every(10).minutes.do(
        lambda: threading.Thread(target=main_scan).start()
    )
    schedule.every(3).minutes.do(
        lambda: threading.Thread(target=monitor_active_trades).start()
    )
    # Auto journal setiap Ahad 21:00
    schedule.every().sunday.at("21:00").do(
        lambda: threading.Thread(target=send_weekly_journal).start()
    )
    while True:
        try:
            schedule.run_pending()
        except:
            pass
        time.sleep(1)

class RenderHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ALPHA BOT ACTIVE")
    def do_HEAD(self):
        self.send_response(200)
        self.send_header('Content-Length', '0')
        self.end_headers()
    def log_message(self, *args):
        pass

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(
        target=lambda: HTTPServer(('0.0.0.0', port), RenderHandler).serve_forever(),
        daemon=True
    ).start()
    threading.Thread(target=run_scheduler, daemon=True).start()
    alert_admin(
        "ALPHA BOT (FIXED v2) DEPLOYED\n"
        f"Config: MC ${CONFIG['mc_min']/1e6:.0f}M-${CONFIG['mc_max']/1e6:.0f}M | "
        f"Liq ${CONFIG['liq_min']/1e3:.0f}K | ATR×{CONFIG['atr_sl_mult']}\n"
        "/start untuk menu"
    )
    threading.Thread(target=main_scan).start()
    bot.infinity_polling(timeout=20, long_polling_timeout=20)
