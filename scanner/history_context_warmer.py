"""
Background daily historical-context warmer.

The live scanner must NEVER block on 700 historical Kite API requests.

This module refreshes the daily scanner context cache outside the request
path. The scanner then reads the cached daily context synchronously.
"""

from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Optional, Set

from broker.kite_adapter import get_active_kite
from config.settings import settings
from data.historical_loader import HistoricalDataLoader
from data.market_calendar import MarketCalendar
from data.time_utils import today_ist
from monitoring.logger import logger


class DailyHistoryContextWarmer:
    """
    Background refresher for the 700-stock daily context cache.

    The expensive 700 Historical API requests run here, never inside
    /api/research/live.
    """

    def __init__(
        self,
        refresh_interval_seconds: int = 60,
    ):
        self.refresh_interval_seconds = (
            refresh_interval_seconds
        )

        self._thread: Optional[
            threading.Thread
        ] = None

        self._stop_event = (
            threading.Event()
        )

        self._refresh_lock = threading.Lock()

        self._last_target_date: Optional[
            date
        ] = None

        # Symbols that have actually succeeded for a given target date.
        # Only this date's entry is kept at a time (pruned on completion
        # or when the target date changes), so a symbol that failed
        # remains eligible for retry on the next refresh pass instead of
        # being silently treated as done.
        self._succeeded_symbols_by_date: dict[
            date,
            Set[str],
        ] = {}

    @staticmethod
    def _latest_completed_trading_day(
        today: date,
    ) -> date:

        candidate = (
            today - timedelta(days=1)
        )

        while not MarketCalendar.is_trading_day(
            candidate
        ):
            candidate -= timedelta(days=1)

        return candidate

    def start(self) -> None:
        """
        Start the daemon thread exactly once.
        """

        if (
            self._thread is not None
            and self._thread.is_alive()
        ):
            return

        self._thread = threading.Thread(
            target=self._run,
            name="daily-history-context-warmer",
            daemon=True,
        )

        self._thread.start()

        logger.info(
            "Daily history context warmer started."
        )

    def stop(self) -> None:
        self._stop_event.set()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._maybe_refresh()

            except Exception as exc:
                logger.exception(
                    "Daily history warmer failed: %s",
                    exc,
                )

            self._stop_event.wait(
                self.refresh_interval_seconds
            )

    def _maybe_refresh(self) -> None:
        kite = get_active_kite()

        if kite is None:
            return

        today = today_ist()

        if not MarketCalendar.is_trading_day(
            today
        ):
            return

        target_date = (
            self._latest_completed_trading_day(
                today
            )
        )

        if (
            self._last_target_date
            == target_date
        ):
            return

        self.refresh(
            kite_client=kite,
            target_date=target_date,
        )

    def refresh(
        self,
        kite_client: Any,
        target_date: date,
    ) -> None:
        """
        Refresh all 700 daily-context caches.

        This may take ~100+ seconds. That is intentional because this
        method runs outside the live HTTP request path.
        """

        if not self._refresh_lock.acquire(
            blocking=False
        ):
            logger.info(
                "Daily history refresh already running."
            )
            return

        try:
            from scanner.stock_ranker import (
                StockUniverseScanner,
            )

            scanner = StockUniverseScanner()

            token_map = scanner.resolve_tokens(
                kite_client=kite_client,
                force_refresh=False,
            )

            symbols = [
                record.symbol
                for record
                in scanner.universe.all_stocks
            ]

            required_symbols: Set[str] = set(symbols)

            # Drop any bookkeeping left over from a different target
            # date, then only (re)attempt symbols that have not yet
            # actually succeeded for THIS target date.
            if (
                self._succeeded_symbols_by_date
                and target_date not in self._succeeded_symbols_by_date
            ):
                self._succeeded_symbols_by_date = {}

            already_succeeded = self._succeeded_symbols_by_date.get(
                target_date,
                set(),
            )

            symbols_to_fetch = [
                symbol
                for symbol in symbols
                if symbol not in already_succeeded
            ]

            history_start = (
                target_date
                - timedelta(days=40)
            )

            success_count = 0
            failed_count = 0
            newly_succeeded: Set[str] = set()

            logger.info(
                "Starting daily history context refresh "
                "for %d/%d stocks (retrying previously failed/unattempted "
                "symbols only): %s -> %s",
                len(symbols_to_fetch),
                len(symbols),
                history_start,
                target_date,
            )

            for symbol in symbols_to_fetch:
                if self._stop_event.is_set():
                    break

                token = token_map.get(
                    symbol
                )

                if token is None:
                    failed_count += 1

                    logger.warning(
                        "No token for %s; "
                        "daily context not refreshed.",
                        symbol,
                    )

                    continue

                cache_path = (
                    settings.base_dir
                    / "data"
                    / "cache"
                    / f"{symbol}_daily_context.csv"
                )

                try:
                    HistoricalDataLoader.fetch_real_data(
                        kite_client=kite_client,
                        instrument_token=token,
                        start_date=history_start,
                        end_date=target_date,
                        interval="day",
                        cache_path=cache_path,
                        force_refresh=False,
                    )

                    success_count += 1
                    newly_succeeded.add(symbol)

                except Exception as exc:
                    failed_count += 1

                    logger.warning(
                        "Daily context refresh failed "
                        "for %s: %s",
                        symbol,
                        exc,
                    )

            succeeded_so_far = (
                already_succeeded | newly_succeeded
            )
            self._succeeded_symbols_by_date = {
                target_date: succeeded_so_far
            }

            # Only mark this target date complete once every required
            # symbol in the universe has actually succeeded. Symbols
            # that failed (or were never attempted because the run was
            # stopped) remain outside `succeeded_so_far` and are
            # therefore retried on the next refresh pass instead of
            # being silently treated as done.
            is_fully_refreshed = (
                not self._stop_event.is_set()
                and required_symbols.issubset(succeeded_so_far)
            )

            if is_fully_refreshed:
                self._last_target_date = target_date

            logger.info(
                "Daily history context refresh pass complete: "
                "newly_succeeded=%d failed=%d total_succeeded=%d/%d "
                "target=%s complete=%s",
                success_count,
                failed_count,
                len(succeeded_so_far),
                len(required_symbols),
                target_date,
                is_fully_refreshed,
            )

        finally:
            self._refresh_lock.release()


daily_history_context_warmer = (
    DailyHistoryContextWarmer()
)


def start_daily_history_warmer() -> None:
    daily_history_context_warmer.start()