"""Agent 2: NewsWeb & Financial RSS Scraper and 48-72h Context Formatter for Agent 3.

Parses RSS/Atom XML feeds using Python's standard library (`xml.etree.ElementTree` and `urllib`),
matches issuer names or tickers against the OBX 15 universe, stores messages in DuckDB
`newsweb_messages`, and formats the 48-72h context digest for the AI Risk Manager.
"""

from __future__ import annotations

import hashlib
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import duckdb

from config.settings import settings
from src.data_ingestion.ohlcv_loader import load_universe_tickers

DEFAULT_FEEDS = [
    ("NewsWeb_OsloBors", "https://newsweb.oslobors.no/rss"),
    ("E24_Bors", "https://e24.no/rss2/?seksjon=boers-og-finans"),
]


@dataclass(frozen=True)
class NewsMessage:
    """Normalized company or macro news notice."""

    message_id: str
    ticker: str
    published_at: datetime
    category: str
    title: str
    summary: str
    url: str


def match_obx15_ticker(text: str, stocks: list[dict] | None = None) -> str | None:
    """Match a headline or summary to one of the 15 OBX tickers by ticker root or company name."""
    universe = stocks or load_universe_tickers()
    upper_text = text.upper()

    # Custom keyword aliases for Norwegian company names in NewsWeb / E24 headlines
    aliases: dict[str, list[str]] = {
        "EQNR.OL": ["EQUINOR", "EQNR"],
        "AKRBP.OL": ["AKER BP", "AKRBP"],
        "SUBC.OL": ["SUBSEA 7", "SUBSEA7", "SUBC"],
        "FRO.OL": ["FRONTLINE", "FRO "],
        "DNB.OL": ["DNB BANK", "DNB "],
        "STB.OL": ["STOREBRAND", "STB "],
        "GJF.OL": ["GJENSIDIGE", "GJF", "GJFG"],
        "NHY.OL": ["NORSK HYDRO", "HYDRO", "NHY"],
        "YAR.OL": ["YARA", "YAR "],
        "KOG.OL": ["KONGSBERG GRUPPEN", "KONGSBERG", "KOG"],
        "MOWI.OL": ["MOWI"],
        "SALM.OL": ["SALMAR", "SALM"],
        "TEL.OL": ["TELENOR", "TEL "],
        "ORK.OL": ["ORKLA", "ORK "],
        "NOD.OL": ["NORDIC SEMICONDUCTOR", "NOD "],
    }

    for stock in universe:
        ticker = stock["ticker"]
        keywords = aliases.get(ticker, [ticker.replace(".OL", ""), stock["name"].upper()])
        for kw in keywords:
            if kw in upper_text:
                return ticker
    return None


def parse_rss_xml(xml_text: str, source_category: str = "NewsWeb") -> list[NewsMessage]:
    """Parse standard RSS 2.0 or Atom XML string and extract notices matching OBX 15 stocks."""
    stocks = load_universe_tickers()
    root = ET.fromstring(xml_text)
    messages: list[NewsMessage] = []

    # Handle standard RSS <item> elements
    items = root.findall(".//item")
    for item in items:
        title = (item.findtext("title") or "").strip()
        description = (item.findtext("description") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date_raw = (item.findtext("pubDate") or "").strip()
        category = (item.findtext("category") or source_category).strip()

        matched_ticker = match_obx15_ticker(f"{title} {description}", stocks)
        if not matched_ticker:
            continue

        pub_dt = _parse_timestamp(pub_date_raw)
        raw_id = f"{matched_ticker}:{title}:{pub_dt.isoformat()}"
        msg_id = f"nw_{hashlib.sha256(raw_id.encode('utf-8')).hexdigest()[:16]}"

        messages.append(
            NewsMessage(
                message_id=msg_id,
                ticker=matched_ticker,
                published_at=pub_dt,
                category=category,
                title=title,
                summary=description[:600],
                url=link,
            )
        )

    return messages


def _parse_timestamp(raw_str: str) -> datetime:
    """Parse RFC-2822 or ISO-8601 date string into a timezone-aware UTC datetime."""
    if not raw_str:
        return datetime.now(timezone.utc)
    try:
        dt = parsedate_to_datetime(raw_str)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(raw_str.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def save_news_messages_to_duckdb(
    messages: list[NewsMessage],
    db_path: Path | None = None,
) -> int:
    """Persist scraped NewsMessage items into DuckDB `newsweb_messages`."""
    if not messages:
        return 0
    target_db = db_path or settings.duckdb_path
    con = duckdb.connect(str(target_db))
    try:
        rows = [
            (
                m.message_id,
                m.ticker,
                m.published_at.replace(tzinfo=None),
                m.category,
                m.title,
                m.summary,
                m.url,
            )
            for m in messages
        ]
        con.executemany(
            """
            INSERT OR REPLACE INTO newsweb_messages (
                message_id, ticker, published_at, category, title, summary, url
            ) VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            rows,
        )
        return len(rows)
    finally:
        con.close()


def poll_live_rss_feeds(db_path: Path | None = None, timeout: float = 10.0) -> int:
    """Poll configured NewsWeb / financial RSS feeds and store matched OBX 15 notices in DuckDB."""
    collected: list[NewsMessage] = []
    for source_name, url in DEFAULT_FEEDS:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 AntigravityOBX/0.1"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                xml_data = resp.read().decode("utf-8", errors="replace")
                collected.extend(parse_rss_xml(xml_data, source_category=source_name))
        except Exception:
            continue

    return save_news_messages_to_duckdb(collected, db_path=db_path)


def build_72h_news_digest_for_ticker(
    ticker: str,
    lookback_hours: int = 72,
    reference_time: datetime | None = None,
    db_path: Path | None = None,
) -> str:
    """Format the 48-72h NewsWeb/financial news digest for a given OBX ticker for Agent 3."""
    target_db = db_path or settings.duckdb_path
    if not target_db.exists():
        return f"Ingen registrerte børsmeldinger for {ticker} siste {lookback_hours} timer."

    ref_utc = (reference_time or datetime.now(timezone.utc)).replace(tzinfo=None)
    cutoff = ref_utc - timedelta(hours=lookback_hours)

    con = duckdb.connect(str(target_db), read_only=True)
    try:
        rows = con.execute(
            """
            SELECT published_at, category, title, summary
            FROM newsweb_messages
            WHERE ticker = ?
              AND published_at >= ?
            ORDER BY published_at DESC
            LIMIT 15;
            """,
            (ticker, cutoff),
        ).fetchall()
    finally:
        con.close()

    if not rows:
        return f"Ingen børsmeldinger eller nyhetsvarsler for {ticker} siste {lookback_hours} timer."

    lines = [f"Børsmeldinger og nyheter for {ticker} (siste {lookback_hours} timer):"]
    for pub_at, cat, title, summary in rows:
        ts_str = pub_at.strftime("%Y-%m-%d %H:%M") if hasattr(pub_at, "strftime") else str(pub_at)
        clean_summary = f" — {summary}" if summary else ""
        lines.append(f"- [{ts_str}] ({cat}) {title}{clean_summary}")
    return "\n".join(lines)
