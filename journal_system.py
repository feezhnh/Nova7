"""
====================================================
  JOURNAL SYSTEM — Nova7 / Alpha8 / Crypthon
  Simpan setiap signal, semak mingguan setiap Ahad
====================================================

CARA GUNA:
  from journal_system import JournalSystem
  journal = JournalSystem(system_name="Nova7")

  # Bila signal keluar — panggil log_signal()
  journal.log_signal(symbol, entry_price, sl, tp1, tp2, tp3, coin_id, grade)

  # Bila TP/SL kena (dari trade tracker) — panggil update_outcome()
  journal.update_outcome(symbol, coin_id, outcome, exit_price)

  # Register handler Telegram untuk /journal dan /weekly
  journal.register_commands(bot, ADMIN_CHAT_ID)
"""

import json
import os
import time
import threading
import html
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────
#  KONFIGURASI
# ──────────────────────────────────────────────────
JOURNAL_FILE   = "signal_journal.json"   # satu fail kongsi semua sistem
JOURNAL_LOCK   = threading.Lock()

OUTCOME_LABELS = {
    "TP1_HIT":   "✅ TP1",
    "TP2_HIT":   "🔥 TP2",
    "TP3_HIT":   "👑 TP3 MAX",
    "STOP_LOSS": "🛑 Stop Loss",
    "EXPIRED":   "⏳ Tamat Tempoh",
    "PENDING":   "⏳ Belum Selesai",
}

# ──────────────────────────────────────────────────
#  HELPER — baca / tulis JSON selamat
# ──────────────────────────────────────────────────
def _read_journal() -> dict:
    with JOURNAL_LOCK:
        if not os.path.exists(JOURNAL_FILE):
            return {}
        try:
            with open(JOURNAL_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}

def _write_journal(data: dict):
    with JOURNAL_LOCK:
        try:
            with open(JOURNAL_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"[JOURNAL] Gagal simpan: {e}")

def _week_key(ts: float) -> str:
    """Pulangkan string 'YYYY-WNN' berdasarkan timestamp (ISO week)."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return f"{dt.isocalendar()[0]}-W{dt.isocalendar()[1]:02d}"

def _current_week_key() -> str:
    return _week_key(time.time())

def _readable_dt(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d %b %Y %H:%M UTC")


# ══════════════════════════════════════════════════
#  KELAS UTAMA
# ══════════════════════════════════════════════════
class JournalSystem:
    def __init__(self, system_name: str = "Nova7"):
        self.name = system_name  # "Nova7" / "Alpha8" / "Crypthon"
        logger.info(f"[JOURNAL] {self.name} — Journal System aktif.")

    # ──────────────────────────────────────────────
    #  1. LOG SIGNAL BARU
    # ──────────────────────────────────────────────
    def log_signal(
        self,
        symbol: str,
        coin_id: str,
        entry_price: float,
        sl: float,
        tp1: float,
        tp2: float,
        tp3: float,
        grade: str = "N/A",
        coin_name: str = "",
        risk_tier: str = "",
        msg_id: int = 0,
    ) -> str:
        """
        Simpan satu signal ke journal.
        Pulangkan journal_id (string unik).
        """
        journal = _read_journal()
        week    = _current_week_key()

        journal_id = f"{self.name}_{symbol}_{int(time.time())}"

        entry = {
            "journal_id":   journal_id,
            "system":       self.name,
            "week":         week,
            "symbol":       symbol,
            "coin_name":    coin_name or symbol,
            "coin_id":      coin_id,
            "entry_price":  entry_price,
            "sl":           sl,
            "tp1":          tp1,
            "tp2":          tp2,
            "tp3":          tp3,
            "grade":        grade,
            "risk_tier":    risk_tier,
            "outcome":      "PENDING",
            "exit_price":   None,
            "pnl_pct":      None,
            "signal_time":  time.time(),
            "close_time":   None,
            "msg_id":       msg_id,
        }

        journal[journal_id] = entry
        _write_journal(journal)
        logger.info(f"[JOURNAL] Signal dicatat: {journal_id}")
        return journal_id

    # ──────────────────────────────────────────────
    #  2. KEMASKINI OUTCOME (TP1/TP2/TP3/SL)
    # ──────────────────────────────────────────────
    def update_outcome(
        self,
        coin_id: str,
        outcome: str,          # "TP1_HIT" / "TP2_HIT" / "TP3_HIT" / "STOP_LOSS"
        exit_price: float,
        system_name: str = "",
    ):
        """
        Dikemaskini bila tracker detect TP atau SL.
        Cari semua entri PENDING untuk coin_id + sistem ini.
        """
        target_system = system_name or self.name
        journal = _read_journal()
        updated = 0

        for jid, entry in journal.items():
            if (
                entry.get("coin_id") == coin_id
                and entry.get("system") == target_system
                and entry.get("outcome") == "PENDING"
            ):
                entry_price = entry.get("entry_price", exit_price)
                pnl_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price else 0

                journal[jid]["outcome"]    = outcome
                journal[jid]["exit_price"] = exit_price
                journal[jid]["pnl_pct"]    = round(pnl_pct, 2)
                journal[jid]["close_time"] = time.time()
                updated += 1

        if updated:
            _write_journal(journal)
            logger.info(f"[JOURNAL] {updated} entri dikemaskini → {outcome} untuk {coin_id}")

    # ──────────────────────────────────────────────
    #  3. LAPORAN MINGGUAN (AHAD MALAM)
    # ──────────────────────────────────────────────
    def get_weekly_report(self, week_key: str = "", system_filter: str = "") -> str:
        """
        Pulangkan string laporan mingguan berformat HTML (Telegram).
        week_key  : cth "2025-W22" — kosong = minggu semasa
        system_filter : kosong = semua sistem
        """
        week   = week_key or _current_week_key()
        target = system_filter or self.name
        journal = _read_journal()

        entries = [
            e for e in journal.values()
            if e.get("week") == week and (target == "ALL" or e.get("system") == target)
        ]

        if not entries:
            return (
                f"📒 <b>Laporan Mingguan {week}</b>\n"
                f"Sistem: <b>{target}</b>\n\n"
                f"Tiada signal direkodkan minggu ini."
            )

        total   = len(entries)
        wins    = [e for e in entries if e["outcome"] in ("TP1_HIT", "TP2_HIT", "TP3_HIT")]
        losses  = [e for e in entries if e["outcome"] == "STOP_LOSS"]
        pending = [e for e in entries if e["outcome"] == "PENDING"]
        expired = [e for e in entries if e["outcome"] == "EXPIRED"]

        win_rate = (len(wins) / (total - len(pending))) * 100 if (total - len(pending)) > 0 else 0

        # Purata PnL untuk yang dah close
        closed_pnl = [e["pnl_pct"] for e in entries if e.get("pnl_pct") is not None]
        avg_pnl    = round(sum(closed_pnl) / len(closed_pnl), 2) if closed_pnl else 0.0

        # Grade breakdown
        grade_count: dict = {}
        for e in entries:
            g = e.get("grade", "N/A")
            grade_count[g] = grade_count.get(g, 0) + 1

        # Emoji keputusan mingguan
        if win_rate >= 65:
            verdict = "🟢 MINGGU HIJAU — Prestasi Cemerlang!"
        elif win_rate >= 45:
            verdict = "🟡 MINGGU NEUTRAL — Boleh Diperbaiki"
        else:
            verdict = "🔴 MINGGU MERAH — Semak Semula Strategi"

        # ─── Bina mesej ───
        lines = [
            f"📒 <b>LAPORAN MINGGUAN {week}</b>",
            f"⚙️ Sistem: <b>{target}</b>",
            "────────────────────────",
            f"📊 Total Signal  : <b>{total}</b>",
            f"✅ Win (TP hit)  : <b>{len(wins)}</b>",
            f"🛑 Loss (SL hit) : <b>{len(losses)}</b>",
            f"⏳ Pending       : <b>{len(pending)}</b>",
            f"⌛ Expired       : <b>{len(expired)}</b>",
            f"🎯 Win Rate      : <b>{win_rate:.1f}%</b>",
            f"📈 Avg PnL       : <b>{avg_pnl:+.2f}%</b>",
            "────────────────────────",
            f"<b>{verdict}</b>",
            "────────────────────────",
        ]

        # Grade breakdown
        if grade_count:
            lines.append("📋 <b>Grade Breakdown:</b>")
            for g, c in sorted(grade_count.items()):
                lines.append(f"  {g}: {c} signal")
            lines.append("────────────────────────")

        # Senarai signal (max 15 supaya tak overflow)
        lines.append("🗂 <b>Rekod Signal:</b>")
        for e in entries[:15]:
            outcome_label = OUTCOME_LABELS.get(e["outcome"], e["outcome"])
            pnl_str = f"{e['pnl_pct']:+.2f}%" if e.get("pnl_pct") is not None else "—"
            sym = html.escape(e.get("symbol", "?"))
            lines.append(f"  • <b>{sym}</b> | {outcome_label} | PnL: {pnl_str}")

        if len(entries) > 15:
            lines.append(f"  … dan {len(entries)-15} lagi signal")

        lines.append("────────────────────────")
        lines.append("📌 Guna /journal untuk lihat semua rekod terbaru.")

        return "\n".join(lines)

    # ──────────────────────────────────────────────
    #  4. LAPORAN SEMUA SISTEM (ALL)
    # ──────────────────────────────────────────────
    def get_all_systems_report(self, week_key: str = "") -> str:
        """Pulangkan laporan gabungan Nova7 + Alpha8 + Crypthon."""
        week   = week_key or _current_week_key()
        journal = _read_journal()

        entries = [e for e in journal.values() if e.get("week") == week]
        if not entries:
            return f"📒 Tiada signal direkodkan untuk {week}."

        systems = list({e["system"] for e in entries})
        parts   = [f"📊 <b>LAPORAN GABUNGAN {week}</b>\n"]
        for sys in sorted(systems):
            sub = [e for e in entries if e["system"] == sys]
            wins = sum(1 for e in sub if e["outcome"] in ("TP1_HIT","TP2_HIT","TP3_HIT"))
            losses = sum(1 for e in sub if e["outcome"] == "STOP_LOSS")
            pending = sum(1 for e in sub if e["outcome"] == "PENDING")
            closed = wins + losses
            wr = (wins/closed*100) if closed else 0
            parts.append(
                f"⚙️ <b>{sys}</b>: {len(sub)} signal | ✅{wins} 🛑{losses} ⏳{pending} | WR {wr:.0f}%"
            )

        parts.append("\nGuna /weekly &lt;sistem&gt; untuk laporan penuh.")
        return "\n".join(parts)

    # ──────────────────────────────────────────────
    #  5. RINGKASAN TERBARU (untuk /journal command)
    # ──────────────────────────────────────────────
    def get_recent_signals(self, limit: int = 10, system_filter: str = "") -> str:
        target  = system_filter or self.name
        journal = _read_journal()

        entries = [
            e for e in journal.values()
            if target == "ALL" or e.get("system") == target
        ]
        entries.sort(key=lambda x: x.get("signal_time", 0), reverse=True)
        entries = entries[:limit]

        if not entries:
            return f"📒 Tiada rekod journal untuk <b>{target}</b>."

        lines = [f"📒 <b>JOURNAL TERBARU — {target}</b>", ""]
        for e in entries:
            outcome_label = OUTCOME_LABELS.get(e["outcome"], e["outcome"])
            pnl_str = f"{e['pnl_pct']:+.2f}%" if e.get("pnl_pct") is not None else "—"
            dt_str  = _readable_dt(e["signal_time"])
            sym     = html.escape(e.get("symbol","?"))
            grade   = html.escape(e.get("grade","—"))
            lines.append(
                f"🪙 <b>{sym}</b> [{e['system']}]\n"
                f"   ⏰ {dt_str}\n"
                f"   📊 {grade}\n"
                f"   {outcome_label} | PnL: {pnl_str}\n"
            )

        return "\n".join(lines)

    # ──────────────────────────────────────────────
    #  6. AUTO WEEKLY REPORT (thread — setiap Ahad 8pm UTC)
    # ──────────────────────────────────────────────
    def start_weekly_scheduler(self, bot, chat_id: str):
        """
        Spawn satu thread yang hantar laporan mingguan
        setiap malam Ahad (Isnin 00:00 UTC = akhir Ahad).
        """
        def _scheduler():
            logger.info("[JOURNAL] Weekly scheduler aktif.")
            while True:
                now = datetime.now(tz=timezone.utc)
                # weekday(): Monday=0 … Sunday=6
                # Target: Ahad 20:00 UTC
                seconds_until = _seconds_until_sunday_night(now)
                logger.info(f"[JOURNAL] Laporan mingguan dalam {seconds_until/3600:.1f} jam.")
                time.sleep(seconds_until)

                week_key = _week_key(time.time() - 3600)  # ambil minggu yang baru lepas
                report   = self.get_weekly_report(week_key)
                try:
                    bot.send_message(chat_id, report, parse_mode="HTML")
                    logger.info(f"[JOURNAL] Laporan mingguan {week_key} dihantar.")
                except Exception as e:
                    logger.error(f"[JOURNAL] Gagal hantar laporan: {e}")

                time.sleep(7200)  # buffer 2 jam supaya takde repeat

        t = threading.Thread(target=_scheduler, daemon=True)
        t.start()

    # ──────────────────────────────────────────────
    #  7. REGISTER TELEGRAM COMMANDS
    # ──────────────────────────────────────────────
    def register_commands(self, bot, admin_chat_id: str):
        """
        Daftarkan command Telegram:
          /journal         — 10 signal terbaru (sistem ini)
          /journal all     — semua sistem
          /weekly          — laporan minggu semasa (sistem ini)
          /weekly all      — laporan gabungan semua sistem
          /weekly YYYY-WNN — laporan minggu tertentu
        """

        @bot.message_handler(commands=["journal"])
        def cmd_journal(message):
            parts = message.text.strip().split()
            sys_filter = parts[1].upper() if len(parts) > 1 else self.name
            if sys_filter == "ALL":
                text = self.get_recent_signals(limit=10, system_filter="ALL")
            else:
                text = self.get_recent_signals(limit=10, system_filter=sys_filter)
            try:
                bot.reply_to(message, text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"[JOURNAL CMD] Ralat: {e}")

        @bot.message_handler(commands=["weekly"])
        def cmd_weekly(message):
            parts = message.text.strip().split()
            arg   = parts[1] if len(parts) > 1 else ""

            if arg.upper() == "ALL":
                text = self.get_all_systems_report()
            elif arg.startswith("20") and "-W" in arg:
                # /weekly 2025-W22
                text = self.get_weekly_report(week_key=arg)
            elif arg.upper() in ("NOVA7", "ALPHA8", "CRYPTHON"):
                text = self.get_weekly_report(system_filter=arg.capitalize())
            else:
                text = self.get_weekly_report()

            try:
                bot.reply_to(message, text, parse_mode="HTML")
            except Exception as e:
                logger.error(f"[WEEKLY CMD] Ralat: {e}")

        logger.info(f"[JOURNAL] Commands /journal & /weekly didaftarkan untuk {self.name}.")
        self.start_weekly_scheduler(bot, admin_chat_id)


# ──────────────────────────────────────────────────
#  UTILITY — kira masa sehingga Ahad 20:00 UTC
# ──────────────────────────────────────────────────
def _seconds_until_sunday_night(now: datetime) -> float:
    TARGET_WEEKDAY = 6   # Ahad
    TARGET_HOUR    = 20  # 8pm UTC

    days_ahead = (TARGET_WEEKDAY - now.weekday()) % 7
    if days_ahead == 0 and now.hour >= TARGET_HOUR:
        days_ahead = 7  # dah lepas — tunggu minggu depan

    target = now.replace(hour=TARGET_HOUR, minute=0, second=0, microsecond=0)
    target = target.replace(day=now.day + days_ahead)  # simple shift
    # guna timedelta untuk elak overflow hari
    from datetime import timedelta
    target = (now + timedelta(days=days_ahead)).replace(
        hour=TARGET_HOUR, minute=0, second=0, microsecond=0
    )
    return max((target - now).total_seconds(), 60)
