"""Portfolio allocator with sector caps (max 2/sector, max 4 total) and deterministic tie-breaker."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CandidateSignal:
    """Candidate signal competing for portfolio allocation."""

    ticker: str
    sector: str
    strategy_name: str
    signal_close: float
    priority_rank: float  # Higher value = higher priority


def select_portfolio_candidates(
    candidates: list[CandidateSignal],
    active_positions_by_sector: dict[str, int],
    current_open_positions: int,
    max_total_positions: int = 4,
    max_per_sector: int = 2,
    available_cash_nok: float | None = None,
    position_notional_nok: float | None = None,
) -> list[CandidateSignal]:
    """Deterministically rank and filter candidate signals against portfolio and sector limits."""
    available_slots = max(0, max_total_positions - current_open_positions)
    if available_slots == 0 or not candidates:
        return []

    sector_counts = dict(active_positions_by_sector)
    ordered = sorted(candidates, key=lambda c: (-c.priority_rank, c.ticker))

    selected: list[CandidateSignal] = []
    seen_tickers: set[str] = set()
    remaining_cash = available_cash_nok
    position_notional = position_notional_nok or 25_000.0

    for cand in ordered:
        if len(selected) >= available_slots:
            break
        if cand.ticker in seen_tickers:
            continue
        curr_sec = sector_counts.get(cand.sector, 0)
        if curr_sec >= max_per_sector:
            continue
        if remaining_cash is not None:
            notional = position_notional
            if remaining_cash < notional:
                continue
            remaining_cash -= notional

        selected.append(cand)
        seen_tickers.add(cand.ticker)
        sector_counts[cand.sector] = curr_sec + 1

    return selected
