"""
Failover Approval Engine: Email & In-App Webhook (`failover_approver.py`)
=========================================================================
Sender e-postvarsel med sikre godkjenningslenker og støtter
web-endepunkt (`/approve`, `/reject`) for manuell godkjenning dersom Telegram feiler.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("FailoverApprover")

PENDING_APPROVALS: Dict[str, Dict[str, Any]] = {}


def get_smtp_status() -> Dict[str, Any]:
    """Return non-sensitive SMTP failover configuration status."""
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").strip()
    return {
        "configured": bool(smtp_user and smtp_pass),
        "smtp_server": os.getenv("SMTP_SERVER", "smtp.gmail.com"),
        "smtp_port": int(os.getenv("SMTP_PORT", "587")),
        "alert_email_to": os.getenv("ALERT_EMAIL_TO", smtp_user) or "Ikke satt",
        "webhook_base_url": os.getenv("WEBHOOK_BASE_URL", "http://localhost:8000"),
    }


def list_pending_approvals() -> List[Dict[str, Any]]:
    """Return pending/processed failover approvals (without exposing raw secret tokens)."""
    items: List[Dict[str, Any]] = []
    for tid, data in PENDING_APPROVALS.items():
        items.append(
            {
                "trade_id": tid,
                "symbol": data.get("symbol"),
                "qty": data.get("qty"),
                "limit": data.get("limit"),
                "sl": data.get("sl"),
                "tp": data.get("tp"),
                "reason": data.get("reason", ""),
                "status": data.get("status", "PENDING"),
                "created_at": data.get("created_at"),
                "approve_url": data.get("approve_url", ""),
                "reject_url": data.get("reject_url", ""),
            }
        )
    return items


def process_approval_action(trade_id: Optional[str], token: Optional[str], action: str) -> tuple[int, str]:
    """Validate token and approve/reject a failover trade item."""
    if not trade_id or trade_id not in PENDING_APPROVALS:
        return 400, "<h1>Ugyldig eller utløpt forespørsel</h1><p>Fant ingen ventende failover-ordre med denne ID-en.</p>"

    trade = PENDING_APPROVALS[trade_id]
    if not token or trade.get("token") != token:
        return 403, "<h1>Ugyldig sikkerhetstoken</h1><p>Godkjenningslenken kunne ikke verifiseres.</p>"

    if action == "approve":
        trade["status"] = "APPROVED"
        return (
            200,
            f"<h1>✅ Ordre Godkjent!</h1><p><b>{trade['symbol']}</b> ({trade['qty']} stk @ ${trade['limit']:.2f}) er godkjent via failover.</p>",
        )
    if action == "reject":
        trade["status"] = "REJECTED"
        return (
            200,
            f"<h1>❌ Ordre Avvist!</h1><p><b>{trade['symbol']}</b> ({trade['qty']} stk) er kansellert.</p>",
        )
    return 400, "<h1>Ukjent handling</h1>"


class ApprovalWebhookHandler(BaseHTTPRequestHandler):
    """Lokal HTTP-server for å motta godkjenning via e-postlenker eller nettleser."""

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        trade_id = params.get("trade_id", [None])[0]
        token = params.get("token", [None])[0]

        if parsed.path == "/approve":
            status, html_body = process_approval_action(trade_id, token, "approve")
            self._respond_html(html_body, status=status)
            return
        if parsed.path == "/reject":
            status, html_body = process_approval_action(trade_id, token, "reject")
            self._respond_html(html_body, status=status)
            return

        self._respond_html("<h1>Ugyldig eller utløpt forespørsel</h1>", status=400)

    def _respond_html(self, content: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        html = (
            "<html><body style='font-family:sans-serif;text-align:center;padding:40px;"
            f"background:#0b0f19;color:#fff;'>{content}</body></html>"
        )
        self.wfile.write(html.encode("utf-8"))


def send_backup_email(
    trade_id: str,
    symbol: str,
    qty: int,
    limit_price: float,
    sl: float,
    tp: float,
    reason: str,
) -> bool:
    """Registrerer failover-godkjenning i web-køen og sender e-postvarsel hvis SMTP er konfigurert."""
    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").strip()
    alert_email_to = os.getenv("ALERT_EMAIL_TO", smtp_user).strip() or smtp_user

    token = secrets.token_urlsafe(16)
    base_url = os.getenv("WEBHOOK_BASE_URL", "http://localhost:8000").rstrip("/")
    approve_url = f"{base_url}/approve?trade_id={trade_id}&token={token}"
    reject_url = f"{base_url}/reject?trade_id={trade_id}&token={token}"

    PENDING_APPROVALS[trade_id] = {
        "symbol": symbol,
        "qty": int(qty),
        "limit": float(limit_price),
        "sl": float(sl),
        "tp": float(tp),
        "reason": reason,
        "token": token,
        "status": "PENDING",
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "approve_url": approve_url,
        "reject_url": reject_url,
    }

    if not smtp_user or not smtp_pass:
        logger.warning("SMTP-konfigurasjon mangler i .env. Ordren er lagt i tertiær Web Failover-kø.")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[RESERVEVARSEL] Godkjenn ordre: {symbol} ({qty} stk @ ${limit_price:.2f})"
    msg["From"] = smtp_user
    msg["To"] = alert_email_to

    sl_pct = ((limit_price - sl) / limit_price) * 100.0 if limit_price else 0.0
    tp_pct = ((tp - limit_price) / limit_price) * 100.0 if limit_price else 0.0

    html = f"""
    <html>
      <body style="font-family: Arial, sans-serif; background-color: #0b0f19; color: #ffffff; padding: 25px;">
        <div style="max-width: 600px; margin: auto; background: #111827; border: 1px solid #1f2937; border-radius: 8px; padding: 20px;">
          <h2 style="color: #10b981; margin-top:0;">Failover Ordregodkjenning</h2>
          <p>Telegram-varsling feilet eller oppnådde timeout. Vennligst ta stilling til ordren under:</p>
          <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px; color: #e5e7eb;">
            <tr><td style="padding: 8px; border-bottom: 1px solid #374151;"><b>Ticker:</b></td><td style="padding: 8px; border-bottom: 1px solid #374151;">{symbol}</td></tr>
            <tr><td style="padding: 8px; border-bottom: 1px solid #374151;"><b>Volum:</b></td><td style="padding: 8px; border-bottom: 1px solid #374151;">{qty} aksjer (~${qty * limit_price:,.2f})</td></tr>
            <tr><td style="padding: 8px; border-bottom: 1px solid #374151;"><b>Limit Kurs:</b></td><td style="padding: 8px; border-bottom: 1px solid #374151;">${limit_price:.2f}</td></tr>
            <tr><td style="padding: 8px; border-bottom: 1px solid #374151;"><b>Stop-Loss:</b></td><td style="padding: 8px; border-bottom: 1px solid #374151; color: #ef4444;">${sl:.2f} (-{sl_pct:.1f}%)</td></tr>
            <tr><td style="padding: 8px; border-bottom: 1px solid #374151;"><b>Take-Profit:</b></td><td style="padding: 8px; border-bottom: 1px solid #374151; color: #10b981;">${tp:.2f} (+{tp_pct:.1f}%)</td></tr>
          </table>
          <p style="font-size: 13px; color: #9ca3af; margin-bottom: 25px;"><b>Begrunnelse:</b> {reason}</p>
          <div style="text-align: center;">
            <a href="{approve_url}" style="background-color: #10b981; color: white; padding: 12px 25px; text-decoration: none; border-radius: 6px; font-weight: bold; margin-right: 15px; display: inline-block;">GODKJENN ORDRE</a>
            <a href="{reject_url}" style="background-color: #ef4444; color: white; padding: 12px 25px; text-decoration: none; border-radius: 6px; font-weight: bold; display: inline-block;">AVVIS</a>
          </div>
        </div>
      </body>
    </html>
    """
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(smtp_server, smtp_port, timeout=10) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, alert_email_to, msg.as_string())
        logger.info("Reserve-epost levert til %s", alert_email_to)
        return True
    except Exception as exc:
        logger.error("Kunne ikke sende failover e-post: %s", exc)
        return False
