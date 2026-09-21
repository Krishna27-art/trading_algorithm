"""
Zerodha Kite Connect Client Wrapper
Provides high-level helper functions for market data, order placement, and risk safeguards.
"""

import json
import logging
from typing import Any, Dict, List, Optional
from kiteconnect import KiteConnect

from config.settings import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("kite_client")


class KiteApp:
    def __init__(self):
        self.api_key = settings.kite_api_key
        self.api_secret = settings.kite_api_secret
        self.kite: Optional[KiteConnect] = None
        self._initialize_session()

    def _get_access_token(self) -> Optional[str]:
        # Priority 1: session_token.json
        if settings.token_file.exists():
            try:
                with open(settings.token_file, "r") as f:
                    data = json.load(f)
                    token = data.get("access_token")
                    if token:
                        return token
            except Exception as e:
                logger.warning(f"Could not read session_token.json: {e}")

        # Priority 2: .env file
        if settings.kite_access_token:
            return settings.kite_access_token

        return None

    def _initialize_session(self):
        if not self.api_key or self.api_key == "your_api_key_here":
            logger.error("API Key not configured in .env file.")
            return

        token = self._get_access_token()
        if not token:
            logger.warning("No access token found. Please run 'python auth.py' to generate one.")
            return

        self.kite = KiteConnect(api_key=self.api_key)
        self.kite.set_access_token(token)

    def is_connected(self) -> bool:
        if not self.kite:
            return False
        try:
            profile = self.kite.profile()
            return bool(profile and "user_id" in profile)
        except Exception as e:
            logger.error(f"Kite session validation failed: {e}")
            return False

    def get_profile(self) -> Optional[Dict[str, Any]]:
        try:
            return self.kite.profile() if self.kite else None
        except Exception as e:
            logger.error(f"Error fetching profile: {e}")
            return None

    def get_margins(self) -> Optional[Dict[str, Any]]:
        try:
            return self.kite.margins() if self.kite else None
        except Exception as e:
            logger.error(f"Error fetching margins: {e}")
            return None

    def get_ltp(self, instruments: List[str]) -> Dict[str, Any]:
        """
        Fetch Last Traded Price (LTP) for given instruments.
        Example instruments: ['NSE:INFY', 'NSE:RELIANCE', 'NFO:NIFTY24SEP25000CE']
        """
        try:
            if not self.kite:
                return {}
            return self.kite.ltp(instruments)
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
        try:
            if not self.kite:
                return []
            return self.kite.historical_data(
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
        if not self.kite:
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
            order_id = self.kite.place_order(
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
