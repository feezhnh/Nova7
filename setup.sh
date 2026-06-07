#!/bin/bash
# Nova7 v8 - OOM Optimized Setup Script

echo "🚀 Nova7 v8 Setup untuk Render Free Tier"
echo "========================================"

# 1. Install dependencies
echo ""
echo "📦 Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# 2. Database initialization test
echo ""
echo "📁 Testing database initialization..."
python -c "
import sqlite3
from Nova7_VER1_OPTIMIZED_OOM_FIX import init_db, get_tuning
try:
    init_db()
    t = get_tuning()
    print(f'✅ Database initialized. Tuning params: {len(t)} items')
    print(f'   - Mode: {t.get(\"mode\")}')
    print(f'   - BO RVol: {t.get(\"bo_rvol\")}')
except Exception as e:
    print(f'❌ Database error: {e}')
    exit(1)
"

# 3. Memory test
echo ""
echo "💾 Testing memory usage..."
python -c "
import psutil
import os

process = psutil.Process(os.getpid())
initial = process.memory_info().rss / 1024 / 1024

print(f'Initial memory: {initial:.1f}MB')

# Test import
from Nova7_VER1_OPTIMIZED_OOM_FIX import (
    bot, get_btc_macro_state, generate_trading_journal, 
    supa_fetch_all_paginated, memory_mgr
)

after_import = process.memory_info().rss / 1024 / 1024
print(f'After import: {after_import:.1f}MB (+{after_import-initial:.1f}MB)')

# Test BTC macro
try:
    state = get_btc_macro_state(force_refresh=False)
    if state:
        print(f'✅ BTC data: {state[\"btc_price\"]:,.2f} USD')
except Exception as e:
    print(f'⚠️  BTC fetch error: {e}')

# Force GC
import gc
gc.collect()
after_gc = process.memory_info().rss / 1024 / 1024
print(f'After GC: {after_gc:.1f}MB')
print(f'✅ Memory test passed!')
" || exit 1

# 4. Render environment check
echo ""
echo "🌐 Checking Render environment..."
if [ -z \"\$TELEGRAM_BOT_TOKEN\" ]; then
    echo \"⚠️  TELEGRAM_BOT_TOKEN not set\"
else
    echo \"✅ TELEGRAM_BOT_TOKEN is set\"
fi

if [ -z \"\$TELEGRAM_CHAT_ID\" ]; then
    echo \"⚠️  TELEGRAM_CHAT_ID not set\"
else
    echo \"✅ TELEGRAM_CHAT_ID is set\"
fi

if [ -z \"\$RENDER_EXTERNAL_URL\" ]; then
    echo \"⚠️  RENDER_EXTERNAL_URL not set (local mode will be used)\"
else
    echo \"✅ RENDER_EXTERNAL_URL is set: \$RENDER_EXTERNAL_URL\"
fi

# 5. Summary
echo ""
echo "✅ Setup Complete!"
echo ""
echo "📝 Next steps:"
echo "  1. Set environment variables in Render Dashboard:"
echo "     - TELEGRAM_BOT_TOKEN"
echo "     - TELEGRAM_CHAT_ID"
echo "     - ADMIN_CHAT_ID (optional)"
echo "     - SUPABASE_URL (optional)"
echo "     - SUPABASE_KEY (optional)"
echo ""
echo "  2. Set Render start command:"
echo "     python Nova7_VER1_OPTIMIZED_OOM_FIX.py"
echo ""
echo "  3. Monitor logs for '✅ Nova7 v8 OPTIMIZED' message"
echo ""
echo "  4. For memory monitoring, check for '⚠️ Memory high' warnings"
echo ""
echo "  5. Telegram commands:"
echo "     /status  - Check active trades"
echo "     /macro   - Check BTC macro"
echo "     /pending - Check pending signals"
echo "     /journal - Generate trading journal"
echo ""
