"""Automated Scheduler & Event-Trigger Daemon for OBX 15 & US Trading Desk.

Manages:
1. Time-based Schedules (Europe/Oslo):
   - obx_preopen (08:55 Mon-Fri): Expire stale HITL orders & pre-market readiness check
   - us_preopen (15:15 Mon-Fri): Massive BBO & Alpaca Stream pre-market bridge check
   - obx_eod_scan (16:40 Mon-Fri): Daily EOD Scan (Agent 1 -> 2 -> 3 -> 4)
   - sunday_audit (20:00 Sun): Weekly Agent 5 Portfolio & 60d Drift Audit
2. Event-driven Triggers:
   - newsweb_rss_watch (every 5 min): Checks NewsWeb RSS for OBX 15 corporate announcements
   - hitl_expiry_watch (every 1 min): Enforces 14h auto-expiry in SQLite WAL
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
OSLO_TZ = ZoneInfo("Europe/Oslo")
logger = logging.getLogger("SchedulerDaemon")

DEFAULT_JOBS: dict[str, dict[str, Any]] = {
    "obx_preopen": {
        "id": "obx_preopen",
        "kind": "schedule",
        "name": "OBX Før-Åpning & 14t Utløpssjekk",
        "cron_label": "Man–Fre 08:55 CET",
        "weekdays": [0, 1, 2, 3, 4],
        "hour": 8,
        "minute": 55,
        "enabled": False,
        "cmd": [sys.executable, "-c", "from src.hitl.order_queue import expire_stale_orders; print('Expired:', expire_stale_orders())"],
        "description": "Rydder utløpte HITL-ordrer før Oslo Børs åpner kl. 09:00.",
    },
    "us_preopen": {
        "id": "us_preopen",
        "kind": "schedule",
        "name": "US S&P 500 Live Bridge Sjekk",
        "cron_label": "Man–Fre 15:15 CET",
        "weekdays": [0, 1, 2, 3, 4],
        "hour": 15,
        "minute": 15,
        "enabled": False,
        "cmd": [sys.executable, "live_market_bridge.py"],
        "description": "Tester Massive Level 1 BBO & Alpaca WebSocket 15 min før US-åpning.",
    },
    "obx_eod_scan": {
        "id": "obx_eod_scan",
        "kind": "schedule",
        "name": "Daglig EOD-Skanning (Agent 1–4)",
        "cron_label": "Man–Fre 16:40 CET",
        "weekdays": [0, 1, 2, 3, 4],
        "hour": 16,
        "minute": 40,
        "enabled": False,
        "cmd": [sys.executable, "run_daily_scan.py"],
        "description": "Kjører OBX 15 kvantskanning, NewsWeb-kontekst, AI-risiko og sender HITL til Telegram.",
    },
    "sunday_audit": {
        "id": "sunday_audit",
        "kind": "schedule",
        "name": "Søndagsrevisjon & Konseptdrift (Agent 5)",
        "cron_label": "Søndag 20:00 CET",
        "weekdays": [6],
        "hour": 20,
        "minute": 0,
        "enabled": False,
        "cmd": [sys.executable, "portfolio_monitor.py"],
        "cmd2": [sys.executable, "setup_drift_views.py"],
        "description": "Oppdaterer rullende 60d Sortino, faktorregresjon og Shadow Tracking i DuckDB.",
    },
    "newsweb_rss_watch": {
        "id": "newsweb_rss_watch",
        "kind": "trigger",
        "name": "Event: NewsWeb RSS Overvåkning",
        "cron_label": "Hvert 5. minutt (hvis aktiv)",
        "interval_seconds": 300,
        "enabled": False,
        "cmd": [sys.executable, "-c", "from src.data_ingestion.newsweb_rss import fetch_newsweb_rss; items = fetch_newsweb_rss(); print(f'NewsWeb RSS OK: {len(items)} meldinger')"],
        "description": "Poller NewsWeb RSS kontinuerlig for nye OBX 15 børsmeldinger.",
    },
    "hitl_expiry_watch": {
        "id": "hitl_expiry_watch",
        "kind": "trigger",
        "name": "Event: SQLite WAL 14t Auto-Expiry",
        "cron_label": "Hvert minutt (hvis aktiv)",
        "interval_seconds": 60,
        "enabled": True,
        "cmd": [sys.executable, "-c", "from src.hitl.order_queue import expire_stale_orders; print('HITL expiry check:', expire_stale_orders())"],
        "description": "Passer automatisk på at ubesvarte HITL-ordrer utløper etter 14 timer.",
    },
    "daily_telegram_status": {
        "id": "daily_telegram_status",
        "kind": "schedule",
        "name": "Daglig Porteføljestatus på Telegram",
        "cron_label": "Man–Fre 22:15 CET",
        "weekdays": [0, 1, 2, 3, 4],
        "hour": 22,
        "minute": 15,
        "enabled": True,
        "cmd": [sys.executable, "telegram_portfolio_notifier.py", "--daily"],
        "description": "Sender daglig Alpaca Paper porteføljerapport (Equity, Cash, Realised/Urealisert P&L og posisjoner) til Telegram.",
    },
    "portfolio_change_notify": {
        "id": "portfolio_change_notify",
        "kind": "trigger",
        "name": "Event: Telegram-varsel ved Porteføljeendring",
        "cron_label": "Hvert 2. minutt",
        "interval_seconds": 120,
        "enabled": True,
        "cmd": [sys.executable, "telegram_portfolio_notifier.py", "--check-change"],
        "description": "Overvåker Alpaca Paper posisjoner og ordrer og sender umiddelbart Telegram-varsel ved fylling eller endring.",
    },
}


class SchedulerEngine:
    """Thread-safe local scheduler & event trigger manager backed by JSON state."""

    def __init__(self, state_path: Path | None = None) -> None:
        self.state_path = state_path or (BASE_DIR / "data" / "scheduler_state.json")
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.jobs: dict[str, dict[str, Any]] = {}
        self._load_state()

    def _load_state(self) -> None:
        saved: dict[str, Any] = {}
        if self.state_path.exists():
            try:
                saved = json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception:
                saved = {}

        for jid, template in DEFAULT_JOBS.items():
            entry = dict(template)
            prev = saved.get(jid, {})
            if "enabled" in prev:
                entry["enabled"] = bool(prev["enabled"])
            entry["last_run"] = prev.get("last_run")
            entry["last_status"] = prev.get("last_status", "idle")
            entry["last_output"] = prev.get("last_output", "")
            entry["last_run_epoch"] = float(prev.get("last_run_epoch") or 0.0)
            entry["last_run_date_hm"] = prev.get("last_run_date_hm", "")
            self.jobs[jid] = entry

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        serializable = {
            jid: {
                "enabled": j["enabled"],
                "last_run": j.get("last_run"),
                "last_status": j.get("last_status"),
                "last_output": (j.get("last_output") or "")[-1200:],
                "last_run_epoch": j.get("last_run_epoch", 0.0),
                "last_run_date_hm": j.get("last_run_date_hm", ""),
            }
            for jid, j in self.jobs.items()
        }
        self.state_path.write_text(json.dumps(serializable, indent=2, ensure_ascii=False), encoding="utf-8")

    def get_status(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "id": j["id"],
                    "kind": j["kind"],
                    "name": j["name"],
                    "cron_label": j["cron_label"],
                    "enabled": j["enabled"],
                    "description": j["description"],
                    "last_run": j.get("last_run") or "Aldri kjørt",
                    "last_status": j.get("last_status") or "idle",
                    "last_output": (j.get("last_output") or "").strip(),
                }
                for j in self.jobs.values()
            ]

    def toggle_job(self, job_id: str, enabled: bool) -> dict[str, Any]:
        with self._lock:
            if job_id not in self.jobs:
                raise KeyError(f"Ukjent jobb-ID: {job_id}")
            self.jobs[job_id]["enabled"] = bool(enabled)
            self._save_state()
            return {"id": job_id, "enabled": self.jobs[job_id]["enabled"]}

    def run_job_now(self, job_id: str, async_mode: bool = True) -> dict[str, Any]:
        with self._lock:
            if job_id not in self.jobs:
                raise KeyError(f"Ukjent jobb-ID: {job_id}")
            self.jobs[job_id]["last_status"] = "running"

        if async_mode:
            t = threading.Thread(target=self._execute_job, args=(job_id,), daemon=True)
            t.start()
            return {"started": True, "id": job_id}
        return self._execute_job(job_id)

    def _execute_job(self, job_id: str) -> dict[str, Any]:
        job = self.jobs[job_id]
        cmds = [job["cmd"]]
        if "cmd2" in job:
            cmds.append(job["cmd2"])

        outputs: list[str] = []
        ok = True
        for cmd in cmds:
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(BASE_DIR),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=120,
                )
                out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
                outputs.append(out.strip())
                if proc.returncode != 0:
                    ok = False
                    break
            except Exception as exc:
                ok = False
                outputs.append(f"[FEIL] {exc}")
                break

        now_oslo = datetime.now(OSLO_TZ)
        with self._lock:
            job["last_run"] = now_oslo.strftime("%Y-%m-%d %H:%M:%S CET")
            job["last_run_epoch"] = time.time()
            job["last_run_date_hm"] = now_oslo.strftime("%Y-%m-%d %H:%M")
            job["last_status"] = "completed" if ok else "failed"
            job["last_output"] = "\n".join(o for o in outputs if o)
            self._save_state()
        return {"id": job_id, "status": job["last_status"], "output": job["last_output"]}

    def tick(self, now: datetime | None = None, runner_fn: Callable[[str], Any] | None = None) -> list[str]:
        """Evaluate all enabled schedules and interval triggers once; return IDs triggered."""
        now_oslo = now or datetime.now(OSLO_TZ)
        now_hm = now_oslo.strftime("%Y-%m-%d %H:%M")
        now_epoch = time.time()
        triggered: list[str] = []

        for jid, job in self.jobs.items():
            if not job.get("enabled"):
                continue
            if job.get("last_status") == "running":
                continue

            should_run = False
            if job["kind"] == "schedule":
                if (
                    now_oslo.weekday() in job["weekdays"]
                    and now_oslo.hour == job["hour"]
                    and now_oslo.minute == job["minute"]
                    and job.get("last_run_date_hm") != now_hm
                ):
                    should_run = True
            elif job["kind"] == "trigger":
                interval = float(job.get("interval_seconds") or 60)
                if (now_epoch - float(job.get("last_run_epoch") or 0.0)) >= interval:
                    should_run = True

            if should_run:
                triggered.append(jid)
                if runner_fn is not None:
                    runner_fn(jid)
                else:
                    self.run_job_now(jid, async_mode=True)

        return triggered

    def start_background_loop(self, poll_seconds: int = 15) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()

        def _loop() -> None:
            logger.info("SchedulerEngine bakgrunnsløkke startet (Europe/Oslo).")
            while not self._stop_event.is_set():
                try:
                    self.tick()
                except Exception as exc:
                    logger.error(f"Feil i SchedulerEngine.tick(): {exc}")
                self._stop_event.wait(poll_seconds)

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
