"""Agent 3: AI Risk Manager & 'Devil's Advocate' (Hybrid Local Ollama / Gemini Flash).

Evaluates quantitative signals against 48-72h NewsWeb notices and the earnings calendar,
classifies rejections into ('NONE', 'EARNINGS_SOON', 'COMPANY_SHOCK', 'MACRO_NOISE'),
and logs ALL evaluations (both GO and NO-GO) into DuckDB `signal_evaluations` for Shadow Tracking.
"""

from __future__ import annotations

import json
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from config.settings import settings

VALID_REJECTION_CATEGORIES = {"NONE", "EARNINGS_SOON", "COMPANY_SHOCK", "MACRO_NOISE"}


@dataclass(frozen=True)
class RiskDecisionCard:
    """Structured decision card returned by Agent 3 (AI Risk Officer)."""

    eval_id: str
    ticker: str
    strategy_name: str
    approve_recommendation: bool
    rejection_category: str  # 'NONE', 'EARNINGS_SOON', 'COMPANY_SHOCK', 'MACRO_NOISE'
    confidence: float
    risk_score: str          # 'LOW', 'MEDIUM', 'HIGH'
    earnings_blackout_flag: bool
    bull_case: str
    devils_advocate: str
    llm_provider: str


RISK_OFFICER_SYSTEM_PROMPT = """Du er Agent 3 (AI Risk Officer & Djevelens Advokat) på et kvantitativt OBX-meglerbord.
Din jobb er IKKE å regne på tekniske indikatorer eller filtrere på generell makrostøy (markedet er allerede filtrert av OBX > SMA200 og VIX < 25).
Du skal kun lete etter selskapsspesifikk halerisiko i børsmeldinger (NewsWeb) og regnskapskalender:
1. Earnings Guardrail (EARNINGS_SOON): Er kvartalsrapport under 3 handelsdager unna? Hvis ja, sett earnings_blackout_flag=true, rejection_category="EARNINGS_SOON" og approve_recommendation=false.
2. Selskapsspesifikke sjokk (COMPANY_SHOCK): Sanksjoner, kontraktskanselleringer, rettstvister, resultatvarsler (profit warnings) eller emisjoner.
3. Unngå å avslå handler kun pga. generell makrostøy (MACRO_NOISE).
Svar KUN med gyldig JSON med nøklene:
{
  "approve_recommendation": bool,
  "rejection_category": "NONE" | "EARNINGS_SOON" | "COMPANY_SHOCK" | "MACRO_NOISE",
  "confidence": float,
  "risk_score": "LOW" | "MEDIUM" | "HIGH",
  "earnings_blackout_flag": bool,
  "bull_case": "kort konkret oppsummering",
  "devils_advocate": "hva kan gå galt de neste 1-20 dagene"
}"""


def evaluate_signal_with_risk_officer(
    ticker: str,
    strategy_name: str,
    horizon_days: int,
    signal_close_price: float,
    atr_stop_loss: float,
    news_context_72h: str,
    days_until_earnings: int | None,
    duckdb_path: Path | None = None,
    signal_date: date | None = None,
) -> RiskDecisionCard:
    """Run deterministic Earnings Guardrail first, then query local Ollama (or fallback) and log to DuckDB."""
    eval_id = f"eval_{uuid.uuid4().hex[:12]}"

    # Deterministic hard guardrail: never allow LLM hallucination to bypass < 3d earnings blackout
    if days_until_earnings is not None and 0 <= days_until_earnings < 3:
        card = RiskDecisionCard(
            eval_id=eval_id,
            ticker=ticker,
            strategy_name=strategy_name,
            approve_recommendation=False,
            rejection_category="EARNINGS_SOON",
            confidence=1.0,
            risk_score="HIGH",
            earnings_blackout_flag=True,
            bull_case="Teknisk signal utløst.",
            devils_advocate=f"Kvartalstall om {days_until_earnings} dager (< 3 handelsdager blackout-grense).",
            llm_provider="deterministic_guardrail",
        )
        log_evaluation_to_duckdb(
            card=card,
            signal_date=signal_date or date.today(),
            horizon_days=horizon_days,
            signal_close_price=signal_close_price,
            atr_stop_loss=atr_stop_loss,
            db_path=duckdb_path,
        )
        return card

    payload_dict = _call_llm_json(
        ticker=ticker,
        strategy_name=strategy_name,
        news_context=news_context_72h,
        days_until_earnings=days_until_earnings,
    )

    earnings_flag = bool(payload_dict.get("earnings_blackout_flag", False))
    approve = bool(payload_dict.get("approve_recommendation", True)) and not earnings_flag

    raw_cat = str(payload_dict.get("rejection_category", "NONE")).upper()
    if earnings_flag:
        rejection_cat = "EARNINGS_SOON"
    elif approve:
        rejection_cat = "NONE"
    else:
        rejection_cat = raw_cat if raw_cat in VALID_REJECTION_CATEGORIES - {"NONE"} else "COMPANY_SHOCK"

    risk_score = str(payload_dict.get("risk_score", "MEDIUM")).upper()
    if risk_score not in {"LOW", "MEDIUM", "HIGH"}:
        risk_score = "MEDIUM"

    card = RiskDecisionCard(
        eval_id=eval_id,
        ticker=ticker,
        strategy_name=strategy_name,
        approve_recommendation=approve,
        rejection_category=rejection_cat,
        confidence=float(payload_dict.get("confidence", 0.75)),
        risk_score=risk_score,
        earnings_blackout_flag=earnings_flag,
        bull_case=str(payload_dict.get("bull_case", "Teknisk setup bekreftet uten negative selskapshendelser.")),
        devils_advocate=str(payload_dict.get("devils_advocate", "Generell markeds- og likviditetsrisiko.")),
        llm_provider=str(payload_dict.get("provider", settings.llm_provider)),
    )

    log_evaluation_to_duckdb(
        card=card,
        signal_date=signal_date or date.today(),
        horizon_days=horizon_days,
        signal_close_price=signal_close_price,
        atr_stop_loss=atr_stop_loss,
        db_path=duckdb_path,
    )
    return card


def _call_ollama_json(
    ticker: str,
    strategy_name: str,
    news_context: str,
    days_until_earnings: int | None,
) -> dict[str, Any]:
    """Call local Ollama /api/generate endpoint with format='json'."""
    user_prompt = (
        f"Ticker: {ticker}\n"
        f"Strategi: {strategy_name}\n"
        f"Dager til neste kvartalsrapport: {days_until_earnings if days_until_earnings is not None else 'Ukjent (>3)'}\n"
        f"NewsWeb / Nyheter siste 72 timer:\n{news_context or 'Ingen børsmeldinger siste 72 timer.'}"
    )
    body = json.dumps(
        {
            "model": settings.ollama_model,
            "system": RISK_OFFICER_SYSTEM_PROMPT,
            "prompt": user_prompt,
            "format": "json",
            "stream": False,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        url=f"{settings.ollama_host.rstrip('/')}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
            parsed = json.loads(raw.get("response", "{}"))
            parsed["provider"] = f"ollama:{settings.ollama_model}"
            return parsed
    except Exception:
        return {}


def _call_gemini_json(
    ticker: str,
    strategy_name: str,
    news_context: str,
    days_until_earnings: int | None,
) -> dict[str, Any]:
    """Call Gemini REST generateContent as an optional cloud fallback."""
    if not settings.gemini_api_key:
        return {}

    user_prompt = (
        f"Ticker: {ticker}\nStrategi: {strategy_name}\n"
        f"Dager til neste kvartalsrapport: {days_until_earnings if days_until_earnings is not None else 'Ukjent (>3)'}\n"
        f"NewsWeb / Nyheter siste 72 timer:\n{news_context or 'Ingen børsmeldinger siste 72 timer.'}\n"
        "Svar KUN med et JSON-objekt som følger Agent 3-systeminstruksen."
    )
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.gemini_model}:generateContent?key={settings.gemini_api_key}"
    )
    body = json.dumps(
        {
            "systemInstruction": {"parts": [{"text": RISK_OFFICER_SYSTEM_PROMPT}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        text = raw["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
        parsed["provider"] = f"gemini:{settings.gemini_model}"
        return parsed
    except Exception:
        return {}


def _call_llm_json(
    ticker: str,
    strategy_name: str,
    news_context: str,
    days_until_earnings: int | None,
) -> dict[str, Any]:
    """Try configured providers in strict sequence; return fail-closed NO-GO if all fail."""
    ollama = _call_ollama_json(
        ticker=ticker,
        strategy_name=strategy_name,
        news_context=news_context,
        days_until_earnings=days_until_earnings,
    )
    if ollama:
        return ollama

    gemini = _call_gemini_json(
        ticker=ticker,
        strategy_name=strategy_name,
        news_context=news_context,
        days_until_earnings=days_until_earnings,
    )
    if gemini:
        return gemini

    return {
        "approve_recommendation": False,
        "rejection_category": "COMPANY_SHOCK",
        "confidence": 1.0,
        "risk_score": "HIGH",
        "earnings_blackout_flag": False,
        "bull_case": "Teknisk signal eksisterer, men risikovurdering er utilgjengelig.",
        "devils_advocate": "LLM-tjenester er offline eller returnerte ugyldig JSON; sikker fail-closed avslag.",
        "provider": "offline_fail_closed",
    }


def log_evaluation_to_duckdb(
    card: RiskDecisionCard,
    signal_date: date,
    horizon_days: int,
    signal_close_price: float,
    atr_stop_loss: float,
    db_path: Path | None = None,
) -> None:
    """Persist Agent 3 evaluation to DuckDB `signal_evaluations` for Agent 5 Shadow Tracking."""
    target = db_path or settings.duckdb_path
    if not target.exists():
        return
    con = duckdb.connect(str(target))
    try:
        con.execute("ALTER TABLE signal_evaluations ADD COLUMN IF NOT EXISTS rejection_category VARCHAR DEFAULT 'NONE';")
        con.execute(
            """
            INSERT OR REPLACE INTO signal_evaluations (
                eval_id, signal_date, ticker, strategy_name, horizon_days,
                signal_close_price, atr_stop_loss, llm_provider,
                approve_recommendation, rejection_category, confidence, risk_score,
                earnings_blackout_flag, bull_case, devils_advocate
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                card.eval_id,
                signal_date,
                card.ticker,
                card.strategy_name,
                horizon_days,
                signal_close_price,
                atr_stop_loss,
                card.llm_provider,
                card.approve_recommendation,
                card.rejection_category,
                card.confidence,
                card.risk_score,
                card.earnings_blackout_flag,
                card.bull_case,
                card.devils_advocate,
            ),
        )
    finally:
        con.close()
