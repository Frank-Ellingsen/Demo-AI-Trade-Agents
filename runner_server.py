"""Execution Runner API Server for OBX AI Trading Desk (`runner_server.py`).

Local backend server enabling execution of all 4 operational steps directly from `index.html`:
1. `python -m pytest -v` (Enhetstester på tvers av alle moduler)
2. `python run_daily_scan.py` (Daglig EOD-skanning: Agent 1 -> 2 -> 3 -> 4)
3. `python portfolio_monitor.py && python setup_drift_views.py` (Agent 5 portefølje & drift-views)
4. `python -m streamlit run app.py --server.port=8501 --server.headless=true` (Streamlit Kontrolltårn)
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import mimetypes
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from scheduler_daemon import SchedulerEngine

PORT = int(os.environ.get("RUNNER_PORT", "8000"))
BASE_DIR = Path(__file__).resolve().parent


def _reload_env() -> None:
    """Reload .env variables in live mode so edits to .env take effect without restart."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(BASE_DIR / ".env", override=True)
    except ImportError:
        pass


_reload_env()
JOBS: dict[str, dict[str, Any]] = {}
SCHEDULER = SchedulerEngine()
PUBLIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/ui/index.html": "ui/index.html",
    "/ui/dashboard_data.json": "ui/dashboard_data.json",
}


def _is_loopback_origin(headers: Any) -> bool:
    origin = headers.get("Origin")
    if not origin:
        return True
    parsed = urlparse(origin)
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        return False

    host = headers.get("Host", "")
    if host:
        host_name = host.rsplit(":", 1)[0].strip("[]")
        if host_name not in ("127.0.0.1", "localhost", "::1"):
            return False
    return True


def _authorize_mutating_request(headers: Any) -> bool:
    """Require an explicit runner secret when RUNNER_SECRET is configured."""
    secret = os.environ.get("RUNNER_SECRET", "").strip()
    if not secret:
        return True
    return str(headers.get("X-Runner-Secret", "")).strip() == secret

COMMANDS: dict[str, dict[str, Any]] = {
    "step1_tests": {
        "name": "Enhetstester (pytest -v)",
        "cmd": [sys.executable, "-m", "pytest", "-v"],
    },
    "step2_eod_scan": {
        "name": "Daglig EOD-skanning (Agent 1 -> 2 -> 3 -> 4)",
        "cmd": [sys.executable, "run_daily_scan.py"],
    },
    "step3_portfolio_drift": {
        "name": "Porteføljehelsesjekk & Drift-views (Agent 5)",
        "cmd": [sys.executable, "portfolio_monitor.py"],
        "cmd2": [sys.executable, "setup_drift_views.py"],
    },
    "step4_streamlit_app": {
        "name": "Start Streamlit Kontrolltårnet (Fane 4)",
        "cmd": [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            "app.py",
            "--server.port=8501",
            "--server.headless=true",
        ],
        "is_daemon": True,
    },
    "step5_us_backtest": {
        "name": "US Multi-Universe Backtest (S&P 500, OTC, Covered Calls & AI Regime Switcher)",
        "cmd": [sys.executable, "backtest_us_multi_universe.py"],
    },
    "step6_nordic_backtest": {
        "name": "Norden (OMXN40) & Norge (OBX 15) Multi-Univers Backtest",
        "cmd": [sys.executable, "backtest_nordic_multi_universe.py"],
    },
    "step7_live_bridge": {
        "name": "Live Market Bridge (Massive Level 1 BBO & Alpaca WebSocket)",
        "cmd": [sys.executable, "live_market_bridge.py"],
    },
    "step8_dynamic_recommender": {
        "name": "Dynamisk Alpaca Porteføljerådgiver ($100k USD, Profil 2 Balansert)",
        "cmd": [sys.executable, "dynamic_recommender.py", "--profile", "2"],
    },
    "step8_recommender_p1": {
        "name": "Dynamisk Alpaca Porteføljerådgiver ($100k USD, Profil 1 Konservativ)",
        "cmd": [sys.executable, "dynamic_recommender.py", "--profile", "1"],
    },
    "step8_recommender_p3": {
        "name": "Dynamisk Alpaca Porteføljerådgiver ($100k USD, Profil 3 Aggressiv)",
        "cmd": [sys.executable, "dynamic_recommender.py", "--profile", "3"],
    },
    "step8_recommender_exec": {
        "name": "Send Anbefalt Portefølje til Alpaca Paper Trading (--execute)",
        "cmd": [sys.executable, "dynamic_recommender.py", "--profile", "2", "--execute"],
    },
    "step9_telegram_hitl_send": {
        "name": "Send 6-Felts Telegram Beslutningskort (#1042 NVDA) til Mobil",
        "cmd": [sys.executable, "telegram_hitl_trader.py", "--scenario", "nvda", "--send-only"],
    },
    "step9_scenario_1": {
        "name": "Brukertest Scenario 1: Standard Trendutbrudd (MSFT)",
        "cmd": [sys.executable, "telegram_hitl_trader.py", "--scenario", "1", "--send-only"],
    },
    "step9_scenario_2": {
        "name": "Brukertest Scenario 2: Kvartalstall-Fellen / Earnings Trap (PLTR)",
        "cmd": [sys.executable, "telegram_hitl_trader.py", "--scenario", "2", "--send-only"],
    },
    "step9_scenario_3": {
        "name": "Brukertest Scenario 3: Slippage-Sperre / Kursvern (XOM $120 -> Ask $122.50)",
        "cmd": [sys.executable, "telegram_hitl_trader.py", "--scenario", "3", "--send-only"],
    },
    "step9_scenario_4": {
        "name": "Brukertest Scenario 4: Sektortak & Korrelasjonsvern 2/2 Energi (COP)",
        "cmd": [sys.executable, "telegram_hitl_trader.py", "--scenario", "4", "--send-only"],
    },
    "step9_scenario_all": {
        "name": "Send Alle 4 UI/UX Brukertest-Scenarioer til Telegram Mobil",
        "cmd": [sys.executable, "telegram_hitl_trader.py", "--scenario", "all", "--send-only"],
    },
    "step9_telegram_hitl_listen": {
        "name": "Lytt etter Telegram Mobil-Godkjenning -> Kursvern & Alpaca OCO (60s)",
        "cmd": [sys.executable, "telegram_hitl_trader.py", "--listen-only", "--timeout", "60", "--once"],
    },
    "step10_strategy_advisor": {
        "name": "AI Strategi- & Risikorådgiver (Regime & Porteføljeanalyse)",
        "cmd": [sys.executable, "telegram_strategy_advisor_bot.py", "--query", "regime"],
    },
    "step10_advisor_send_telegram": {
        "name": "Send AI Strategi- & Regimerapport til Telegram Mobil",
        "cmd": [sys.executable, "telegram_strategy_advisor_bot.py", "--send-query", "regime"],
    },
    "ping": {
        "name": "Rask helsesjekk av subprosess-runner",
        "cmd": [sys.executable, "-c", "print('RUNNER_PING_OK')"],
    },
}


def _fetch_alpaca_portfolio_safe() -> dict[str, Any]:
    """Fetch live Alpaca Paper account, open positions, and recent orders with safe fallback."""
    try:
        _reload_env()
        from alpaca_execution import AlpacaClient

        return AlpacaClient().get_portfolio_snapshot()
    except Exception as exc:
        return {
            "connected": False,
            "error": str(exc),
            "endpoint": "https://paper-api.alpaca.markets/v2",
            "equity": 100000.0,
            "last_equity": 100000.0,
            "cash": 100000.0,
            "buying_power": 100000.0,
            "long_market_value": 0.0,
            "today_pnl": 0.0,
            "today_pnl_pct": 0.0,
            "unrealized_pl": 0.0,
            "realized_pl": 0.0,
            "positions": [],
            "recent_orders": [],
        }


def _is_port_open(port: int, host: str = "127.0.0.1") -> bool:
    """Check if a local TCP port (e.g. 8501 for Streamlit) is already listening."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def run_job_process(job_id: str, step_key: str) -> None:
    """Run the requested step in a background thread and stream UTF-8 logs into `JOBS[job_id]`."""
    job = JOBS[job_id]
    job["status"] = "running"
    step_info = COMMANDS.get(step_key)

    if not step_info:
        job["status"] = "failed"
        job["logs"].append(f"[ERROR] Ukjent steg: {step_key}\n")
        return

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    # Special handling for Step 4 (Streamlit long-running server on port 8501)
    if step_info.get("is_daemon"):
        cmd_list = step_info["cmd"]
        cmd_str = " ".join(cmd_list)
        job["logs"].append(f"\n>>> EKSEKVERER: {cmd_str}\n")
        job["logs"].append("=" * 60 + "\n")

        if _is_port_open(8501):
            job["logs"].append("[OK] Streamlit kjører allerede på http://localhost:8501\n")
            job["status"] = "completed"
            job["logs"].append("\n>>> FULLFØRT MED SUKSESS! <<<\n")
            return

        try:
            proc = subprocess.Popen(
                cmd_list,
                cwd=str(BASE_DIR),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            # Wait up to 6 seconds for port 8501 to come online while capturing startup output
            deadline = time.time() + 6.0
            while time.time() < deadline:
                if _is_port_open(8501):
                    break
                if proc.poll() is not None:
                    break
                time.sleep(0.25)

            if _is_port_open(8501) or proc.poll() is None:
                job["logs"].append("Streamlit Kontrolltårn startet i bakgrunnen på http://localhost:8501\n")
                job["status"] = "completed"
                job["logs"].append("\n>>> FULLFØRT MED SUKSESS! <<<\n")
            else:
                out_rem = proc.stdout.read() if proc.stdout else ""
                if out_rem:
                    job["logs"].append(out_rem)
                job["status"] = "failed"
                job["logs"].append(f"\n[FEIL] Streamlit avsluttet med kode {proc.returncode}\n")
        except Exception as e:
            job["status"] = "failed"
            job["logs"].append(f"\n[UNNTAK] Kunne ikke starte Streamlit: {e}\n")
        return

    commands_to_run = [step_info["cmd"]]
    if "cmd2" in step_info:
        commands_to_run.append(step_info["cmd2"])

    try:
        for c in commands_to_run:
            cmd_str = " ".join(c)
            job["logs"].append(f"\n>>> EKSEKVERER: {cmd_str}\n")
            job["logs"].append("=" * 60 + "\n")

            proc = subprocess.Popen(
                c,
                cwd=str(BASE_DIR),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )

            if proc.stdout is not None:
                for line in iter(proc.stdout.readline, ""):
                    if line:
                        job["logs"].append(line)
                proc.stdout.close()

            ret_code = proc.wait()
            if ret_code != 0:
                job["status"] = "failed"
                job["logs"].append(f"\n[FEIL] Prosess avsluttet med feilkode {ret_code}\n")
                return

        job["status"] = "completed"
        job["logs"].append("\n>>> FULLFØRT MED SUKSESS! <<<\n")
    except Exception as e:
        job["status"] = "failed"
        job["logs"].append(f"\n[UNNTAK] Systemfeil under kjøring: {e}\n")


class CommandRunnerHandler(SimpleHTTPRequestHandler):
    """HTTP handler serving static files (`index.html`) and `/api/run/*` + `/api/status/*`."""

    def log_message(self, format: str, *args: Any) -> None:
        """Silence noisy per-poll HTTP log lines during test & UI polling."""
        pass

    def end_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin and _is_loopback_origin(self.headers):
            self.send_header("Access-Control-Allow-Origin", origin)
        elif not origin:
            self.send_header("Access-Control-Allow-Origin", f"http://127.0.0.1:{PORT}")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Requested-With")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        if not _is_loopback_origin(self.headers):
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        public_file = PUBLIC_FILES.get(parsed.path)
        if not public_file:
            self.send_response(404)
            self.end_headers()
            return

        file_path = (BASE_DIR / public_file).resolve()
        if not file_path.is_file():
            self.send_response(404)
            self.end_headers()
            return

        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()

    def do_POST(self) -> None:
        if not _is_loopback_origin(self.headers):
            self.send_response(403)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Forbidden"}).encode("utf-8"))
            return

        if not _authorize_mutating_request(self.headers):
            self.send_response(403)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Runner secret required"}).encode("utf-8"))
            return

        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/run/"):
            step_key = parsed.path.replace("/api/run/", "").strip("/")
            if step_key not in COMMANDS:
                self.send_response(404)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Unknown step"}).encode("utf-8"))
                return
            job_id = f"job_{int(time.time() * 1000)}_{len(JOBS) + 1}"
            JOBS[job_id] = {
                "id": job_id,
                "step": step_key,
                "status": "queued",
                "logs": [],
            }

            t = threading.Thread(target=run_job_process, args=(job_id, step_key), daemon=True)
            t.start()

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "job_id": job_id}).encode("utf-8"))
        elif parsed.path == "/api/hitl/action":
            content_len = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
            payload = json.loads(raw_body)
            act = payload.get("action", "")
            order_id = payload.get("order_id", "")
            from src.db.sqlite_store import init_sqlite_db
            from src.hitl.order_queue import enqueue_order_proposal, update_order_status

            init_sqlite_db()
            if act in ("APPROVED", "REJECTED") and order_id:
                ok = update_order_status(order_id, act)
                res_obj = {"success": ok, "order_id": order_id, "status": act}
            elif act == "DEMO_ENQUEUE":
                queued = enqueue_order_proposal(
                    ticker=payload.get("ticker", "EQNR.OL"),
                    action=payload.get("side", "BUY"),
                    shares=int(payload.get("shares", 88)),
                    signal_close_price=float(payload.get("limit_price", 284.15)),
                    limit_price=float(payload.get("limit_price", 284.10)),
                    stop_loss_price=float(payload.get("stop_loss_price", 271.50)),
                    strategy_name=payload.get("strategy_name", "rsi_mean_reversion"),
                    bull_case=payload.get("bull_case", "RSI(2) < 10 pullback i opptrend over SMA(200)."),
                    devils_advocate=payload.get("devils_advocate", "Kortsiktig Brent-volatilitet må overvåkes."),
                )
                res_obj = {"success": True, "queued": queued}
            elif act == "SEND_TELEGRAM_SIGNAL":
                from telegram_hitl_trader import TelegramHITLTrader

                hitl = TelegramHITLTrader()
                trade_id = f"trd_{int(time.time())}"
                msg_id = hitl.send_trade_signal(
                    trade_id=trade_id,
                    symbol=str(payload.get("symbol", "NVDA")).upper(),
                    qty=int(payload.get("qty", 15)),
                    side=str(payload.get("side", "buy")).lower(),
                    limit_price=float(payload.get("limit_price", 145.00)),
                    take_profit_price=float(payload.get("take_profit_price", 175.00)),
                    stop_loss_price=float(payload.get("stop_loss_price", 133.00)),
                    reasoning=str(
                        payload.get(
                            "reasoning",
                            "Breakout over 20-dagers konsolidering. Volum 1.45x 20-dagers snitt. RSI=61.",
                        )
                    ),
                )
                res_obj = {"success": bool(msg_id), "trade_id": trade_id, "message_id": msg_id}
            elif act == "SEND_UX_SCENARIO":
                from telegram_hitl_trader import TelegramHITLTrader

                hitl = TelegramHITLTrader()
                sc_key = str(payload.get("scenario", "1"))
                sc_res = hitl.send_test_scenario(sc_key)
                res_obj = {"success": bool(sc_res.get("message_id")), **sc_res}
            else:
                res_obj = {"success": False, "error": "Unsupported HITL action"}

            out_bytes = json.dumps(res_obj, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(out_bytes)))
            self.end_headers()
            self.wfile.write(out_bytes)
        elif parsed.path == "/api/recommender/run":
            content_len = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
            payload = json.loads(raw_body)
            profile_key = str(payload.get("profile", "2"))
            if profile_key not in ("1", "2", "3"):
                profile_key = "2"
            capital_val = payload.get("capital")
            execute_live = bool(payload.get("execute", False))

            cmd = [sys.executable, "dynamic_recommender.py", "--profile", profile_key]
            if capital_val is not None:
                cmd.extend(["--capital", str(float(capital_val))])
            if execute_live:
                cmd.append("--execute")

            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            proc = subprocess.run(
                cmd,
                cwd=str(BASE_DIR),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            cli_out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
            res_obj = {
                "success": proc.returncode == 0,
                "returncode": proc.returncode,
                "cmd": " ".join(cmd),
                "output": cli_out,
                "alpaca_portfolio": _fetch_alpaca_portfolio_safe() if execute_live else None,
            }
            out_bytes = json.dumps(res_obj, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(out_bytes)))
            self.end_headers()
            self.wfile.write(out_bytes)
        elif parsed.path == "/api/advisor/query":
            content_len = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
            payload = json.loads(raw_body)
            query_text = str(payload.get("query", "regime"))
            send_to_telegram = bool(payload.get("send_to_telegram", False))

            from telegram_strategy_advisor_bot import StrategyAdvisor, TelegramBotListener

            answer_text = StrategyAdvisor.answer_query(query_text)
            tg_sent = False
            tg_error = ""
            if send_to_telegram:
                try:
                    bot = TelegramBotListener()
                    tg_res = bot.send_text(answer_text)
                    tg_sent = bool(tg_res.get("ok"))
                    if not tg_sent:
                        tg_error = str(tg_res)
                except Exception as exc:
                    tg_error = str(exc)

            res_obj = {
                "success": True,
                "query": query_text,
                "answer": answer_text,
                "telegram_sent": tg_sent,
                "telegram_error": tg_error,
            }
            out_bytes = json.dumps(res_obj, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(out_bytes)))
            self.end_headers()
            self.wfile.write(out_bytes)
        elif parsed.path == "/api/scheduler/toggle":
            content_len = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
            payload = json.loads(raw_body)
            job_id = str(payload.get("id", ""))
            enabled = bool(payload.get("enabled", False))
            res_obj = SCHEDULER.toggle_job(job_id, enabled)
            out_bytes = json.dumps(res_obj, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(out_bytes)))
            self.end_headers()
            self.wfile.write(out_bytes)
        elif parsed.path == "/api/scheduler/trigger":
            content_len = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
            payload = json.loads(raw_body)
            job_id = str(payload.get("id", ""))
            res_obj = SCHEDULER.run_job_now(job_id, async_mode=True)
            out_bytes = json.dumps(res_obj, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(out_bytes)))
            self.end_headers()
            self.wfile.write(out_bytes)
        elif parsed.path == "/api/alpaca/order":
            content_len = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
            payload = json.loads(raw_body)
            try:
                import re
                from alpaca_execution import AlpacaClient

                symbol = str(payload.get("symbol", "")).upper().strip()
                qty = int(payload.get("qty", 0))
                side = str(payload.get("side", "buy")).lower().strip()
                order_type = str(payload.get("order_type", "limit")).lower().strip()
                lim = None if order_type == "market" else float(payload.get("limit_price"))
                raw_tp = payload.get("take_profit_price", payload.get("take_profit"))
                raw_sl = payload.get("stop_loss_price", payload.get("stop_loss"))
                tp = float(raw_tp) if raw_tp else None
                sl = float(raw_sl) if raw_sl else None

                if not symbol or not re.fullmatch(r"[A-Z]{1,6}([.-][A-Z]{1,3})?", symbol):
                    raise ValueError(f"Ugyldig aksjesymbol ({symbol}), vennligst sjekk tickeren.")
                if qty <= 0:
                    raise ValueError("Antall aksjer må være større enn 0.")
                if lim is not None and lim <= 0:
                    raise ValueError("Limit-kurs må være større enn 0.")
                if side == "buy" and lim is not None and sl is not None and sl >= lim:
                    raise ValueError("Stop-Loss må ligge under kjøpskurs for et kjøp.")
                if side == "buy" and lim is not None and tp is not None and tp <= lim:
                    raise ValueError("Take-Profit må ligge over kjøpskurs for et kjøp.")

                client = AlpacaClient()
                ord_res = client.place_bracket_order(
                    symbol=symbol,
                    qty=qty,
                    side=side,
                    limit_price=lim,
                    take_profit_price=tp,
                    stop_loss_price=sl,
                )
                res_obj = {
                    "success": True,
                    "order": ord_res,
                    "alpaca_portfolio": _fetch_alpaca_portfolio_safe(),
                }
                status_code = 200
            except Exception as exc:
                res_obj = {"success": False, "error": str(exc)}
                status_code = 400

            out_bytes = json.dumps(res_obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(out_bytes)))
            self.end_headers()
            self.wfile.write(out_bytes)
        elif parsed.path == "/api/strategy/propose-telegram":
            content_len = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
            payload = json.loads(raw_body)
            strategy_key = str(payload.get("strategy_key", "liquid_sp_momentum")).strip()
            total_amount = float(payload.get("total_amount_usd", 50000.0))

            strategy_to_profile = {
                "liquid_sp_momentum": ("2", "1. Liquid S&P Momentum"),
                "conservative_core": ("1", "2. Konservativ Index Core (ETF)"),
                "aggressive_ai_tech": ("3", "3. Aggressiv AI & Tech Momentum"),
                "ai_regime_switcher": ("2", "4. AI Regime Switcher (VIX-styrt)"),
            }
            profile_key, strategy_label = strategy_to_profile.get(
                strategy_key, ("2", "1. Liquid S&P Momentum")
            )

            alp_snap = _fetch_alpaca_portfolio_safe()
            avail_cash = float(alp_snap.get("cash", 0.0))

            if total_amount < 1000.0:
                res_obj = {"success": False, "error": "Minimumsbeløp for strategiordre er $1,000 USD."}
                status_code = 400
            elif total_amount > avail_cash:
                res_obj = {
                    "success": False,
                    "error": (
                        f"Ikke nok kontanter (Cash-sperre): Du ba om ${total_amount:,.2f}, "
                        f"men tilgjengelig cash på Alpaca Paper er ${avail_cash:,.2f}."
                    ),
                    "available_cash": avail_cash,
                }
                status_code = 400
            else:
                try:
                    from dynamic_recommender import DynamicRecommender
                    from telegram_hitl_trader import TelegramHITLTrader

                    rec = DynamicRecommender().generate_recommendation(profile_key, custom_capital=total_amount)
                    hitl = TelegramHITLTrader()
                    sent_items = []
                    base_ts = int(time.time())
                    for idx, p in enumerate(rec["proposals"]):
                        tid = f"trd_{strategy_key[:4]}_{base_ts}_{idx}"
                        mid = hitl.send_trade_signal(
                            trade_id=tid,
                            symbol=p["ticker"],
                            qty=int(p["qty"]),
                            side="buy",
                            limit_price=float(p["price"]),
                            take_profit_price=float(p["tp_price"]),
                            stop_loss_price=float(p["sl_price"]),
                            reasoning=(
                                f"Strategi: {strategy_label} (Totalt budsjett: ${total_amount:,.0f} USD). "
                                f"ATR(14)=${p['atr']:.2f}, Vekt={p['weight_pct']:.1f}%."
                            ),
                            strategy_label=strategy_label,
                            portfolio_equity=total_amount,
                            cash_reserve_pct=float(rec["cash_buffer_pct"]),
                        )
                        sent_items.append({
                            "trade_id": tid,
                            "message_id": mid,
                            "symbol": p["ticker"],
                            "qty": p["qty"],
                            "limit_price": p["price"],
                            "sl_price": p["sl_price"],
                            "tp_price": p["tp_price"],
                        })

                    res_obj = {
                        "success": True,
                        "strategy_label": strategy_label,
                        "total_amount_usd": total_amount,
                        "available_cash_before": avail_cash,
                        "allocated_total": rec["allocated_total"],
                        "remaining_cash_after": round(avail_cash - rec["allocated_total"], 2),
                        "sent_count": len(sent_items),
                        "sent_items": sent_items,
                    }
                    status_code = 200
                except Exception as exc:
                    res_obj = {"success": False, "error": f"Serverfeil ved strategiforslag: {exc}"}
                    status_code = 500

            out_bytes = json.dumps(res_obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(out_bytes)))
            self.end_headers()
            self.wfile.write(out_bytes)
        elif parsed.path == "/api/failover/trigger":
            content_len = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
            payload = json.loads(raw_body)
            _reload_env()
            from failover_approver import get_smtp_status, list_pending_approvals, send_backup_email

            symbol = str(payload.get("symbol", "NVDA")).strip().upper()
            qty = int(payload.get("qty", 10))
            limit_price = float(payload.get("limit_price", 145.0))
            sl = float(payload.get("stop_loss", round(limit_price * 0.94, 2)))
            tp = float(payload.get("take_profit", round(limit_price * 1.12, 2)))
            reason = str(payload.get("reason", "Failover-godkjenning utløst fra AI Trading Desk"))
            trade_id = f"fo_{symbol.lower()}_{int(time.time())}"

            email_sent = send_backup_email(trade_id, symbol, qty, limit_price, sl, tp, reason)
            res_obj = {
                "success": True,
                "trade_id": trade_id,
                "email_sent": email_sent,
                "smtp": get_smtp_status(),
                "pending": list_pending_approvals(),
            }
            out_bytes = json.dumps(res_obj, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(out_bytes)))
            self.end_headers()
            self.wfile.write(out_bytes)
        elif parsed.path == "/api/tax-fx/evaluate":
            content_len = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
            payload = json.loads(raw_body)
            try:
                from src.portfolio.tax_fee_fx_engine import evaluate_trade_hurdle

                res_obj = evaluate_trade_hurdle(
                    ticker=str(payload.get("ticker", "NVDA")),
                    trade_value=float(payload.get("trade_value", 10000.0)),
                    expected_alpha_pct=float(payload.get("expected_alpha_pct", 1.2)),
                    broker_key=payload.get("broker_key") or None,
                    spread_pct=float(payload.get("spread_pct", 0.05)),
                    slippage_pct=float(payload.get("slippage_pct", 0.05)),
                    alpha_buffer_pct=float(payload.get("alpha_buffer_pct", 0.20)),
                    holding_days=int(payload.get("holding_days", 10)),
                    fx_mode=str(payload.get("fx_mode", "auto")),
                    is_adr=bool(payload.get("is_adr", False)),
                )
                res_obj["success"] = True
                status_code = 200
            except Exception as exc:
                res_obj = {"success": False, "error": str(exc)}
                status_code = 400

            out_bytes = json.dumps(res_obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(out_bytes)))
            self.end_headers()
            self.wfile.write(out_bytes)
        elif parsed.path == "/api/settings":
            content_len = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
            payload = json.loads(raw_body)
            try:
                allowed_plain = {
                    "ALPACA_ENDPOINT",
                    "ALPACA_INITIAL_EQUITY",
                    "TELEGRAM_CHAT_ID",
                    "SMTP_SERVER",
                    "SMTP_PORT",
                    "SMTP_USER",
                    "ALERT_EMAIL_TO",
                    "WEBHOOK_BASE_URL",
                    "LLM_PROVIDER",
                    "OLLAMA_HOST",
                    "OLLAMA_MODEL",
                    "GEMINI_MODEL",
                }
                allowed_secrets = {
                    "ALPACA_API_KEY",
                    "ALPACA_SECRET_KEY",
                    "TELEGRAM_BOT_TOKEN",
                    "SMTP_PASS",
                    "GEMINI_API_KEY",
                    "MASSIVE_API_KEY",
                }
                env_path = BASE_DIR / ".env"
                existing_lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
                updates: dict[str, str] = {}

                for key in allowed_plain:
                    if key in payload:
                        val = str(payload.get(key) or "").strip()
                        # Ignore unchanged masked demo placeholders (e.g. "fr••••@hotmail.com" or "88xxxx25")
                        if "••••" in val or "xxxx" in val.lower():
                            continue
                        if key == "ALPACA_ENDPOINT" and val:
                            ep_check = urlparse(val)
                            if ep_check.scheme != "https" or (ep_check.hostname or "").lower() != "paper-api.alpaca.markets":
                                raise ValueError("SIKKERHETSSPERRE: Kun https://paper-api.alpaca.markets/v2 er tillatt.")
                        if val:
                            updates[key] = val

                for key in allowed_secrets:
                    if key in payload:
                        val = str(payload.get(key) or "").strip()
                        if "••••" in val or "xxxx" in val.lower():
                            continue
                        if val:
                            updates[key] = val

                # Keep legacy lowercase alpaca aliases synchronized if present
                if "ALPACA_API_KEY" in updates:
                    updates["alpaca_key"] = updates["ALPACA_API_KEY"]
                if "ALPACA_SECRET_KEY" in updates:
                    updates["alpaca_secret"] = updates["ALPACA_SECRET_KEY"]
                if "ALPACA_ENDPOINT" in updates:
                    updates["alpaca_endpoint"] = updates["ALPACA_ENDPOINT"]

                updated_keys: set[str] = set()
                new_lines: list[str] = []
                for line in existing_lines:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#") and "=" in stripped:
                        k = stripped.split("=", 1)[0].strip()
                        if k in updates:
                            new_lines.append(f"{k}={updates[k]}")
                            updated_keys.add(k)
                            continue
                    new_lines.append(line)

                for k, v in updates.items():
                    if k not in updated_keys:
                        new_lines.append(f"{k}={v}")

                env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                _reload_env()
                alp_snap = _fetch_alpaca_portfolio_safe()
                res_obj = {
                    "success": True,
                    "updated_count": len(updates),
                    "alpaca_connected": bool(alp_snap.get("connected")),
                    "alpaca_cash": alp_snap.get("cash", 0.0),
                    "alpaca_error": alp_snap.get("error", ""),
                }
                status_code = 200
            except Exception as exc:
                res_obj = {"success": False, "error": str(exc)}
                status_code = 400

            out_bytes = json.dumps(res_obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(out_bytes)))
            self.end_headers()
            self.wfile.write(out_bytes)
        else:
            out_bytes = json.dumps({"success": False, "error": f"Ukjent endepunkt: {parsed.path}"}).encode("utf-8")
            self.send_response(404)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(out_bytes)))
            self.end_headers()
            self.wfile.write(out_bytes)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/api/settings":
            _reload_env()

            def _mask(val: str) -> str:
                s = (val or "").strip()
                if not s:
                    return ""
                return (s[:2] + "xxxx••••") if len(s) >= 6 else "xxxx••••"

            def _mask_email(val: str) -> str:
                s = (val or "").strip()
                if not s:
                    return ""
                if "@" in s:
                    local, dom = s.split("@", 1)
                    prefix = local[:2] if len(local) >= 2 else "x"
                    return f"{prefix}xxxx••••@{dom}"
                return _mask(s)

            def _mask_id(val: str) -> str:
                s = (val or "").strip()
                if not s:
                    return ""
                return f"{s[:2]}xxxx{s[-2:]}" if len(s) >= 5 else "xxxx"

            alp_key = os.getenv("ALPACA_API_KEY") or os.getenv("alpaca_key") or ""
            alp_sec = os.getenv("ALPACA_SECRET_KEY") or os.getenv("alpaca_secret") or ""
            alp_ep = os.getenv("ALPACA_ENDPOINT") or os.getenv("alpaca_endpoint") or "https://paper-api.alpaca.markets/v2"
            tg_tok = os.getenv("TELEGRAM_BOT_TOKEN") or ""
            smtp_pw = os.getenv("SMTP_PASS") or ""
            gem_key = os.getenv("GEMINI_API_KEY") or ""
            mas_key = os.getenv("MASSIVE_API_KEY") or ""

            res_obj = {
                "plain": {
                    "ALPACA_ENDPOINT": alp_ep,
                    "ALPACA_INITIAL_EQUITY": os.getenv("ALPACA_INITIAL_EQUITY", "100000"),
                    "TELEGRAM_CHAT_ID": _mask_id(os.getenv("TELEGRAM_CHAT_ID", "")),
                    "SMTP_SERVER": os.getenv("SMTP_SERVER", "smtp.gmail.com"),
                    "SMTP_PORT": os.getenv("SMTP_PORT", "587"),
                    "SMTP_USER": _mask_email(os.getenv("SMTP_USER", "")),
                    "ALERT_EMAIL_TO": _mask_email(os.getenv("ALERT_EMAIL_TO", os.getenv("SMTP_USER", ""))),
                    "WEBHOOK_BASE_URL": os.getenv("WEBHOOK_BASE_URL", "http://localhost:8000"),
                    "LLM_PROVIDER": os.getenv("LLM_PROVIDER", "ollama"),
                    "OLLAMA_HOST": os.getenv("OLLAMA_HOST", "http://localhost:11434"),
                    "OLLAMA_MODEL": os.getenv("OLLAMA_MODEL", "qwen2.5:14b"),
                    "GEMINI_MODEL": os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
                },
                "secrets": {
                    "ALPACA_API_KEY": {"set": bool(alp_key), "hint": _mask(alp_key)},
                    "ALPACA_SECRET_KEY": {"set": bool(alp_sec), "hint": _mask(alp_sec)},
                    "TELEGRAM_BOT_TOKEN": {"set": bool(tg_tok), "hint": _mask(tg_tok)},
                    "SMTP_PASS": {"set": bool(smtp_pw), "hint": _mask(smtp_pw)},
                    "GEMINI_API_KEY": {"set": bool(gem_key), "hint": _mask(gem_key)},
                    "MASSIVE_API_KEY": {"set": bool(mas_key), "hint": _mask(mas_key)},
                },
            }
            out_bytes = json.dumps(res_obj, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(out_bytes)))
            self.end_headers()
            self.wfile.write(out_bytes)
            return

        # Failover webhook endpoints (/approve & /reject) from failover_approver.py
        if parsed.path in ("/approve", "/reject"):
            from failover_approver import process_approval_action

            params = parse_qs(parsed.query)
            trade_id = params.get("trade_id", [None])[0]
            token = params.get("token", [None])[0]
            action = "approve" if parsed.path == "/approve" else "reject"
            status_code, html_body = process_approval_action(trade_id, token, action)
            full_html = (
                "<!doctype html><html lang='no'><body style='font-family:sans-serif;text-align:center;"
                f"padding:40px;background:#0b0f19;color:#edf3f5;'>{html_body}"
                "<p style='margin-top:24px;'><a href='/' style='color:#63c7d6;'>← Tilbake til AI Trading Desk</a></p>"
                "</body></html>"
            ).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(full_html)))
            self.end_headers()
            self.wfile.write(full_html)
            return

        if parsed.path == "/api/failover/pending":
            _reload_env()
            from failover_approver import get_smtp_status, list_pending_approvals

            out_bytes = json.dumps(
                {"smtp": get_smtp_status(), "pending": list_pending_approvals()},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(out_bytes)))
            self.end_headers()
            self.wfile.write(out_bytes)
            return

        if parsed.path == "/api/markets/live-ticker":
            now_ts = time.time()
            cache = getattr(self.server, "_live_ticker_cache", None)
            if cache and (now_ts - cache.get("ts", 0)) < 25:
                res_obj = cache["data"]
            else:
                specs = [
                    ("OSLO", "OSEBX.OL", "", ""),
                    ("OLJE", "BZ=F", "$ ", ""),
                    ("NASDAQ", "^IXIC", "", ""),
                    ("S&P 500", "^GSPC", "", ""),
                    ("USD", "NOK=X", "", " kr"),
                ]
                items = []
                hdr = {"User-Agent": "Mozilla/5.0"}
                try:
                    import requests

                    for lbl, sym, pref, suff in specs:
                        try:
                            r = requests.get(
                                f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                                params={"range": "5d", "interval": "1d"},
                                headers=hdr,
                                timeout=5,
                            )
                            res0 = r.json()["chart"]["result"][0]
                            meta = res0.get("meta", {})
                            p = float(meta.get("regularMarketPrice") or 0.0)
                            closes = [c for c in (res0.get("indicators", {}).get("quote", [{}])[0].get("close") or []) if c is not None]
                            pc = float(closes[-2]) if len(closes) >= 2 else float(meta.get("chartPreviousClose") or p or 1.0)
                            chg_pct = ((p - pc) / pc) * 100.0 if pc > 0 else 0.0
                            items.append({
                                "label": lbl,
                                "symbol": sym,
                                "price": round(p, 2),
                                "change_pct": round(chg_pct, 2),
                                "prefix": pref,
                                "suffix": suff,
                            })
                        except Exception:
                            pass
                except Exception:
                    pass

                if not items:
                    items = [
                        {"label": "OSLO", "symbol": "OSEBX.OL", "price": 2068.75, "change_pct": -0.25, "prefix": "", "suffix": ""},
                        {"label": "OLJE", "symbol": "BZ=F", "price": 92.45, "change_pct": 1.19, "prefix": "$ ", "suffix": ""},
                        {"label": "NASDAQ", "symbol": "^IXIC", "price": 24166.51, "change_pct": -1.08, "prefix": "", "suffix": ""},
                        {"label": "S&P 500", "symbol": "^GSPC", "price": 7235.44, "change_pct": -1.20, "prefix": "", "suffix": ""},
                        {"label": "USD", "symbol": "NOK=X", "price": 9.76, "change_pct": 0.66, "prefix": "", "suffix": " kr"},
                    ]

                res_obj = {"updated_at": time.strftime("%H:%M:%S"), "items": items}
                setattr(self.server, "_live_ticker_cache", {"ts": now_ts, "data": res_obj})

            out_bytes = json.dumps(res_obj, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(out_bytes)))
            self.end_headers()
            self.wfile.write(out_bytes)
            return

        if parsed.path == "/api/markets/sp500-movers":
            _reload_env()
            sp500_leaders = [
                "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AVGO", "JPM", "V",
                "MA", "UNH", "XOM", "CVX", "LLY", "JNJ", "PG", "HD", "CAT", "BAC",
                "WMT", "KO", "PEP", "COST", "CRM", "ORCL", "AMD", "NFLX", "INTC", "PLTR",
            ]
            movers_rows = []
            source = "fallback"
            try:
                import requests
                from alpaca_execution import AlpacaClient

                client = AlpacaClient()
                r = requests.get(
                    "https://data.alpaca.markets/v2/stocks/snapshots",
                    params={"symbols": ",".join(sp500_leaders), "feed": "iex"},
                    headers=client.headers,
                    timeout=8,
                )
                if r.status_code == 200:
                    snaps = r.json()
                    for sym, s in snaps.items():
                        lt = (s.get("latestTrade") or {}).get("p")
                        pc = (s.get("prevDailyBar") or {}).get("c")
                        if lt and pc and float(pc) > 0:
                            lt_f, pc_f = float(lt), float(pc)
                            chg_pct = ((lt_f - pc_f) / pc_f) * 100.0
                            movers_rows.append({
                                "ticker": sym,
                                "price": round(lt_f, 2),
                                "prev_close": round(pc_f, 2),
                                "change_pct": round(chg_pct, 2),
                            })
                    if movers_rows:
                        source = "live_alpaca"
            except Exception:
                pass

            if not movers_rows:
                movers_rows = [
                    {"ticker": "NVDA", "price": 145.00, "prev_close": 142.00, "change_pct": 2.11},
                    {"ticker": "AAPL", "price": 229.10, "prev_close": 227.20, "change_pct": 0.84},
                    {"ticker": "MSFT", "price": 426.80, "prev_close": 422.05, "change_pct": 1.13},
                    {"ticker": "META", "price": 565.00, "prev_close": 559.10, "change_pct": 1.06},
                    {"ticker": "AMZN", "price": 205.00, "prev_close": 203.20, "change_pct": 0.89},
                    {"ticker": "TSLA", "price": 340.00, "prev_close": 347.50, "change_pct": -2.16},
                    {"ticker": "PLTR", "price": 61.20, "prev_close": 62.10, "change_pct": -1.45},
                    {"ticker": "AMD", "price": 148.00, "prev_close": 150.20, "change_pct": -1.46},
                    {"ticker": "XOM", "price": 120.00, "prev_close": 121.10, "change_pct": -0.91},
                    {"ticker": "JPM", "price": 240.00, "prev_close": 241.50, "change_pct": -0.62},
                ]

            movers_rows.sort(key=lambda x: x["change_pct"], reverse=True)
            res_obj = {
                "source": source,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "top_up": movers_rows[:5],
                "top_down": list(reversed(movers_rows[-5:])),
            }
            out_bytes = json.dumps(res_obj, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(out_bytes)))
            self.end_headers()
            self.wfile.write(out_bytes)
            return

        if parsed.path == "/api/universes/omxn40":
            import yaml

            uni_file = BASE_DIR / "config" / "universe.yaml"
            uni_data = yaml.safe_load(uni_file.read_text(encoding="utf-8")) if uni_file.exists() else {}
            omxn40 = uni_data.get("omxn40_stocks", [])
            out_bytes = json.dumps(
                {"index": uni_data.get("omxn40_index", "^OMXN40"), "count": len(omxn40), "stocks": omxn40},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(out_bytes)))
            self.end_headers()
            self.wfile.write(out_bytes)
            return

        if parsed.path == "/api/rulebook":
            rb_file = BASE_DIR / "docs" / "trading_agent_rulebook.md"
            if not rb_file.exists():
                rb_file = BASE_DIR / "trading_agent_rulebook.md"
            md_text = rb_file.read_text(encoding="utf-8") if rb_file.exists() else "Rulebook ikke funnet."
            out_bytes = json.dumps({"filename": rb_file.name, "markdown": md_text}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(out_bytes)))
            self.end_headers()
            self.wfile.write(out_bytes)
            return

        dashboard_routes = {
            "/meglerbord": "Multi-Agent_Meglerbord_—_Kontrolltårn___Porteføljeo.html",
            "/cockpit": "Multi-Agent_Meglerbord_—_Kontrolltårn___Porteføljeo.html",
            "/kontroltarn": "OBX_15_AI_Trader_-_Kontrolltårn___Porteføljeoversikt.html",
            "/kontrolltarn": "OBX_15_AI_Trader_-_Kontrolltårn___Porteføljeoversikt.html",
            "/aurora": "OBX_15_AI_Trader_-_Kontrolltårn___Porteføljeoversikt.html",
            "/strategi-radgiver": "AI_Trading_Dashboard_med_Strategi-Rådgiver.html",
            "/advisor": "AI_Trading_Dashboard_med_Strategi-Rådgiver.html",
            "/bloomberg": "Bloomberg-stil_Indeks-_og_Porteføljedashbord.html",
            "/nordquant": "Bloomberg-stil_Indeks-_og_Porteføljedashbord.html",
            "/terminal": "AI_Trading_Terminal___Porteføljedashbord.html",
            "/nordicquant": "AI_Trading_Terminal___Porteføljedashbord.html",
            "/streamlined": "AI_Trading_Terminal_-_Streamlined___Clean.html",
            "/clean": "AI_Trading_Terminal_-_Streamlined___Clean.html",
            "/nordquant-ai": "Bygging_av_AI-tradingagenter_i_Python.html",
            "/bygging": "Bygging_av_AI-tradingagenter_i_Python.html",
        }
        if parsed.path in dashboard_routes:
            fname = dashboard_routes[parsed.path]
            html_file = BASE_DIR / "ui" / "dashboards" / fname
            if not html_file.exists():
                html_file = BASE_DIR / fname
            if html_file.exists():
                data = html_file.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

        if parsed.path in ("/guide", "/dummies", "/readme", "/brukertest", "/brukertest-nybegynner", "/rulebook", "/tax-fx") or parsed.path.endswith(".md"):
            md_map = {
                "/guide": BASE_DIR / "docs" / "USER_GUIDE.md",
                "/dummies": BASE_DIR / "docs" / "ai-trading-strategi-for-dummies.md",
                "/readme": BASE_DIR / "README.md",
                "/brukertest": BASE_DIR / "docs" / "ui_ux_bruker_test.md",
                "/brukertest-nybegynner": BASE_DIR / "docs" / "bruker-test-nybegynner.md",
                "/rulebook": BASE_DIR / "docs" / "trading_agent_rulebook.md",
                "/tax-fx": BASE_DIR / "docs" / "anbefalte_opt_tax_fee_fx_struktur.md",
            }
            md_file = md_map.get(parsed.path) or (BASE_DIR / "docs" / parsed.path.lstrip("/"))
            if not md_file.exists():
                md_file = BASE_DIR / parsed.path.lstrip("/")
            if md_file.exists() and md_file.is_file():
                raw_md = md_file.read_text(encoding="utf-8")
                escaped_md = (
                    raw_md.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                )
                html_doc = f"""<!doctype html>
<html lang="no">
<head>
  <meta charset="utf-8">
  <title>{md_file.name} — OBX 15 Dokumentasjon</title>
  <style>
    body {{
      margin: 0; padding: 2.5rem clamp(1rem, 5vw, 4rem);
      background: #0a0e17; color: #f1f5f9;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      line-height: 1.65;
    }}
    .wrap {{ max-width: 920px; margin: 0 auto; background: #121826; border: 1px solid #27354f; border-radius: 8px; padding: 2rem 2.5rem; }}
    .topbar {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #27354f; padding-bottom: 1rem; margin-bottom: 1.5rem; }}
    a.back {{ color: #38bdf8; text-decoration: none; font-weight: 600; font-size: 0.9rem; }}
    pre.md-content {{
      white-space: pre-wrap; word-wrap: break-word;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 0.9rem; color: #e2e8f0; margin: 0;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <strong>📘 {md_file.name}</strong>
      <a class="back" href="http://localhost:8000/index.html">← Tilbake til Mission Control</a>
    </div>
    <pre class="md-content">{escaped_md}</pre>
  </div>
</body>
</html>"""
                data = html_doc.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

        if parsed.path == "/api/scheduler/status":
            sched_data = {"jobs": SCHEDULER.get_status()}
            out_bytes = json.dumps(sched_data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(out_bytes)))
            self.end_headers()
            self.wfile.write(out_bytes)
            return

        if parsed.path == "/api/alpaca/portfolio":
            alp_data = _fetch_alpaca_portfolio_safe()
            out_bytes = json.dumps(alp_data, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(out_bytes)))
            self.end_headers()
            self.wfile.write(out_bytes)
            return

        if parsed.path == "/api/suite":
            from backtest_nordic_multi_universe import run_nordic_backtest
            from backtest_us_multi_universe import calculate_metrics as calculate_us_metrics, run_us_backtest
            from dynamic_recommender import DynamicRecommender
            from intuitive_backtest_engine import generate_intuitive_comparison
            from live_market_bridge import (
                ALPACA_WS_DATA_URL,
                ALPACA_WS_UPDATES_URL,
                MASSIVE_BASE_URL,
                AlpacaStreamer,
                MassiveClient,
            )
            from src.db.sqlite_store import get_sqlite_conn, init_sqlite_db

            df_us = run_us_backtest(initial_capital=100_000.0, days=252, seed=42)
            df_us_metrics = calculate_us_metrics(df_us, initial_capital=100_000.0)
            df_nordic, df_nordic_metrics = run_nordic_backtest(initial_capital_nok=100_000.0, trading_days=252)
            df_intuitive = generate_intuitive_comparison()

            rec_engine = DynamicRecommender()
            profiles_data = {
                k: rec_engine.generate_recommendation(k, custom_capital=100_000.0)
                for k in ("1", "2", "3")
            }

            massive = MassiveClient()
            bbo_quotes = [
                massive.get_last_quote(sym, allow_fallback=True)
                for sym in ("NVDA", "SPY", "QQQ", "AAPL", "MSFT", "TSLA")
            ]
            news_feed = massive.get_latest_news(limit=5, allow_fallback=True)
            streamer = AlpacaStreamer()

            init_sqlite_db()
            with get_sqlite_conn() as conn:
                hitl_rows = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM pending_orders ORDER BY created_at DESC LIMIT 25;"
                    ).fetchall()
                ]

            # Sample 42 points (every 6th trading day) for crisp SVG curves in index.html
            step_idx = list(range(0, len(df_us), 6))
            if step_idx[-1] != len(df_us) - 1:
                step_idx.append(len(df_us) - 1)

            suite_payload = {
                "us_backtest": {
                    "metrics": df_us_metrics.to_dict("records"),
                    "days": [int(df_us["Dag"].iloc[i]) for i in step_idx],
                    "series": {
                        col: [round(float(df_us[col].iloc[i]), 2) for i in step_idx]
                        for col in (
                            "S&P 500 Benchmark",
                            "1. Liquid S&P Momentum",
                            "2. OTC Micro-Caps (Netto)",
                            "3. Systematic Covered Calls",
                            "4. AI Regime Switcher (VIX)",
                        )
                    },
                },
                "nordic_backtest": {
                    "metrics": df_nordic_metrics.to_dict("records"),
                    "days": [int(df_nordic["Handelsdag"].iloc[i]) for i in step_idx],
                    "series": {
                        col: [round(float(df_nordic[col].iloc[i]), 2) for i in step_idx]
                        for col in (
                            "1. OBX Momentum (m/ Brent-filter)",
                            "2. OMXN40 Trend (m/ Nordic FX-drag)",
                            "3. First North Small-Caps (Netto)",
                            "4. Dekkede Opsjoner (Covered Calls)",
                        )
                    },
                },
                "intuitive_6case": df_intuitive.to_dict("records"),
                "recommender_profiles": profiles_data,
                "live_bridge": {
                    "massive_base_url": MASSIVE_BASE_URL,
                    "alpaca_ws_data_url": ALPACA_WS_DATA_URL,
                    "alpaca_ws_updates_url": ALPACA_WS_UPDATES_URL,
                    "massive_key_set": bool(massive.api_key),
                    "alpaca_key_set": bool(streamer.key),
                    "quotes": bbo_quotes,
                    "news": news_feed,
                },
                "hitl_orders": hitl_rows,
                "alpaca_portfolio": _fetch_alpaca_portfolio_safe(),
            }
            data = json.dumps(suite_payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path == "/api/dashboard":
            json_file = BASE_DIR / "ui" / "dashboard_data.json"
            if not json_file.exists():
                from scripts.export_ui_data import build_dashboard_payload

                payload = build_dashboard_payload()
                json_file.parent.mkdir(parents=True, exist_ok=True)
                json_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            data = json_file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if parsed.path == "/api/status/health_check":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                json.dumps({"ok": True, "service": "OBX Execution Runner", "port": PORT}).encode("utf-8")
            )
            return

        if parsed.path.startswith("/api/status/"):
            job_id = parsed.path.replace("/api/status/", "").strip("/")
            job = JOBS.get(job_id)
            if job:
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {
                            "id": job["id"],
                            "status": job["status"],
                            "logs": "".join(job["logs"]),
                        },
                        ensure_ascii=False,
                    ).encode("utf-8")
                )
            else:
                self.send_response(404)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Job not found"}).encode("utf-8"))
        else:
            public_file = PUBLIC_FILES.get(parsed.path)
            if not public_file:
                self.send_response(404)
                self.end_headers()
                return

            file_path = (BASE_DIR / public_file).resolve()
            if not file_path.is_file():
                self.send_response(404)
                self.end_headers()
                return

            content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
            data = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)


def create_runner_server(host: str = "127.0.0.1", port: int = PORT) -> ThreadingHTTPServer:
    """Create a `ThreadingHTTPServer` instance bound to `(host, port)`."""
    return ThreadingHTTPServer((host, port), CommandRunnerHandler)


if __name__ == "__main__":
    SCHEDULER.start_background_loop()
    print(f"Starter OBX Execution Server på http://localhost:{PORT}")
    print("Åpne http://localhost:8000/index.html i nettleseren for direkte skriptkjøring.")
    server = create_runner_server("127.0.0.1", PORT)
    server.serve_forever()
