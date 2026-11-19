"""Project-wide configuration loader using standard library dataclasses and env vars."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Load local .env values without overriding process environment variables."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    """Immutable application settings loaded from environment variables."""

    # Paths
    base_dir: Path = BASE_DIR
    sqlite_db_path: Path = BASE_DIR / os.getenv("SQLITE_DB_PATH", "data/hitl_orders.db")
    duckdb_path: Path = BASE_DIR / os.getenv("DUCKDB_PATH", "data/obx_analytics.duckdb")
    raw_news_dir: Path = BASE_DIR / "data" / "raw_news"

    # Portfolio & Risk Baseline
    initial_capital_nok: float = 100_000.0
    max_concurrent_positions: int = 4
    max_positions_per_sector: int = 2
    max_cost_drag_pct_of_gross: float = 0.20  # 20% Cost Drag ceiling
    hitl_expiry_hours: int = 14               # 14h auto-expiry before 09:00 CET open

    # Hybrid LLM Configuration (Local Ollama first, Gemini fallback)
    llm_provider: str = os.getenv("LLM_PROVIDER", "ollama").lower()
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen2.5:14b")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    execution_mode: str = os.getenv("EXECUTION_MODE", "paper").lower()
    live_execution_ack: str = os.getenv("LIVE_EXECUTION_ACK", "")
    runner_secret: str = os.getenv("RUNNER_SECRET", "")

    # Telegram HITL Configuration
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Saxo OpenAPI v2
    saxo_env: str = os.getenv("SAXO_ENV", "sim")
    saxo_app_key: str = os.getenv("SAXO_APP_KEY", "")
    saxo_account_key: str = os.getenv("SAXO_ACCOUNT_KEY", "")

    # Alpaca Markets API v2 (S&P 500 Paper / Live Execution)
    alpaca_endpoint: str = (
        os.getenv("ALPACA_ENDPOINT")
        or os.getenv("alpaca_endpoint")
        or "https://paper-api.alpaca.markets/v2"
    )
    alpaca_api_key: str = (
        os.getenv("ALPACA_API_KEY")
        or os.getenv("alpaca_key")
        or os.getenv("APCA_API_KEY_ID")
        or ""
    )
    alpaca_secret_key: str = (
        os.getenv("ALPACA_SECRET_KEY")
        or os.getenv("alpaca_secret")
        or os.getenv("APCA_API_SECRET_KEY")
        or ""
    )


settings = Settings()
