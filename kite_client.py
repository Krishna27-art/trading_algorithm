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

    def place_order_semi_auto(
        self,
        variety: str,
        exchange: str,
        tradingsymbol: str,
        transaction_type: str,
        quantity: int,
        product: str,
        order_type: str,
        price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        tag: Optional[str] = "algo",
    ) -> Optional[str]:
        """
        Places order with optional manual confirmation safeguard (semi-automated).
        """
        client, _ = self.adapter.get_active_client()
        if not client:
            logger.error("Kite is not connected. Order aborted.")
            return None

        order_summary = (
            f"\n---------------- ORDER REQUEST ----------------\n"
            f" Symbol:       {exchange}:{tradingsymbol}\n"
            f" Action:       {transaction_type}\n"
            f" Quantity:     {quantity}\n"
            f" Product:      {product}\n"
            f" Order Type:   {order_type}\n"
            f" Limit Price:  {price}\n"
            f" Trigger:      {trigger_price}\n"
            f" Tag:          {tag}\n"
            f"-----------------------------------------------"
        )
        print(order_summary)

        if settings.semi_automated_confirmation:
            confirm = input("Confirm and place this order? (y/N): ").strip().lower()
            if confirm not in ("y", "yes"):
                logger.info("Order cancelled by user.")
                return None

        try:
            order_id = client.place_order(
                variety=variety,
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                transaction_type=transaction_type,
                quantity=quantity,
                product=product,
                order_type=order_type,
                price=price,
                trigger_price=trigger_price,
                tag=tag,
            )
            logger.info(f"Order placed successfully! Order ID: {order_id}")
            return order_id
        except Exception as e:
            logger.error(f"Order placement failed: {e}")
            return None
