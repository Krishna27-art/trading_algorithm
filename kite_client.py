"""
Zerodha Kite Connect Client Wrapper (Delegates to unified KiteBrokerAdapter).
Provides high-level helper functions for market data, order placement, and risk safeguards.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from kiteconnect import KiteConnect

from broker.kite_adapter import KiteBrokerAdapter
from config.settings import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("kite_client")


class KiteApp:
    def __init__(self):
        self.adapter = KiteBrokerAdapter.get_instance()
        self.api_key = self.adapter.api_key
        self.api_secret = self.adapter.api_secret

    @property
    def kite(self) -> Optional[KiteConnect]:
        return self.adapter.kite

    def is_connected(self) -> bool:
        client, _ = self.adapter.get_active_client(force_validate=True)
        return client is not None

    def get_profile(self) -> Optional[Dict[str, Any]]:
        return self.adapter.get_user_profile()

    def get_margins(self) -> Optional[Dict[str, Any]]:
        return self.adapter.get_account_margins()

    def get_ltp(self, instruments: List[str]) -> Dict[str, Any]:
        """
        Fetch Last Traded Price (LTP) for given instruments.
        Example instruments: ['NSE:INFY', 'NSE:RELIANCE', 'NFO:NIFTY24SEP25000CE']
        """
        client, _ = self.adapter.get_active_client()
        if not client:
            return {}
        try:
            return client.ltp(instruments)
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
        Interval options: 'minute', '3minute', '5minute', '10minute', '15minute', '30minute', '60minute', 'day'
        """
        client, _ = self.adapter.get_active_client()
        if not client:
            return []
        try:
            return client.historical_data(
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


