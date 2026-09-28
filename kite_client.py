"""
Zerodha Kite Connect Client Wrapper.
Delegates to the module-level session functions in broker/kite_adapter.py.
Provides high-level helper functions for market data used by the CLI (run_algo.py).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from kiteconnect import KiteConnect

from broker.kite_adapter import (
    get_active_kite_with_diagnostics,
    get_saved_session,
)
from config.settings import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("kite_client")


class KiteApp:
    """
    Thin CLI wrapper around the shared Kite session managed by broker/kite_adapter.py.
    The actual session (session_token.json, in-memory cache, validation) is owned
    entirely by that module — this class only delegates to it.
    """

    def __init__(self):
        self._kite: Optional[KiteConnect] = None
        self._err: Optional[str] = None
        # Validate the session once at construction time so callers can check
        # is_connected() cheaply without re-hitting the profile endpoint.
        self._kite, self._err = get_active_kite_with_diagnostics(force_validate=False)

    @property
    def kite(self) -> Optional[KiteConnect]:
        return self._kite

    @property
    def api_key(self) -> Optional[str]:
        session = get_saved_session()
        return session.get("api_key") if session else None

    def is_connected(self) -> bool:
        """Returns True if a validated Kite session exists."""
        if self._kite is not None:
            return True
        # Re-check in case the session was created after construction.
        self._kite, self._err = get_active_kite_with_diagnostics(force_validate=False)
        return self._kite is not None

    def get_profile(self) -> Optional[Dict[str, Any]]:
        if not self._kite:
            return None
        try:
            return self._kite.profile()
        except Exception as e:
            logger.error(f"Error fetching profile: {e}")
            return None

    def get_margins(self) -> Optional[Dict[str, Any]]:
        if not self._kite:
            return None
        try:
            return self._kite.margins()
        except Exception as e:
            logger.error(f"Error fetching margins: {e}")
            return None

    def get_ltp(self, instruments: List[str]) -> Dict[str, Any]:
        """
        Fetch Last Traded Price (LTP) for given instruments.
        Example instruments: ['NSE:INFY', 'NSE:RELIANCE', 'NFO:NIFTY24SEP25000CE']
        """
        if not self._kite:
            return {}
        try:
            return self._kite.ltp(instruments)
        except Exception as e:
            logger.error(f"Error fetching LTP for {instruments}: {e}")
            return {}

    def get_historical_candles(
        self,
        instrument_token: int,
        from_date: str,
        to_date: str,
        interval: str = "5minute",
        continuous: bool = False,
        oi: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Fetch historical candle data.
        Interval options: 'minute', '3minute', '5minute', '10minute', '15minute',
                          '30minute', '60minute', 'day'
        """
        if not self._kite:
            return []
        try:
            return self._kite.historical_data(
                instrument_token=instrument_token,
                from_date=from_date,
                to_date=to_date,
                interval=interval,
                continuous=continuous,
                oi=oi,
            )
        except Exception as e:
            logger.error(f"Error fetching historical data: {e}")
            return []
