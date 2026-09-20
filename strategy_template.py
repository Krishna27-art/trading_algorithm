"""
Semi-Automated Trading Strategy Template
Combines technical indicator calculation (EMA, RSI, Supertrend) with semi-automated trade execution.
"""

import logging
import time
from datetime import datetime, timedelta
import pandas as pd
import ta

from kite_client import KiteApp

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("strategy")


def fetch_and_prepare_data(app: KiteApp, instrument_token: int, days: int = 5, interval: str = "5minute") -> pd.DataFrame:
    """Fetches historical candles and loads them into a pandas DataFrame."""
    to_date = datetime.now()
    from_date = to_date - timedelta(days=days)

    candles = app.get_historical_candles(
        instrument_token=instrument_token,
        from_date=from_date.strftime("%Y-%m-%d %H:%M:%S"),
        to_date=to_date.strftime("%Y-%m-%d %H:%M:%S"),
        interval=interval,
    )

    if not candles:
        logger.warning("No candle data returned.")
        return pd.DataFrame()

    df = pd.DataFrame(candles)
    df["date"] = pd.to_datetime(df["date"])
    df.set_index("date", inplace=True)
    return df


def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Calculates Exponential Moving Averages (EMA) and RSI."""
    if df.empty or len(df) < 20:
        return df

    # EMA 9 & EMA 21
    df["ema_9"] = ta.trend.ema_indicator(df["close"], window=9)
    df["ema_21"] = ta.trend.ema_indicator(df["close"], window=21)

    # RSI (14)
    df["rsi_14"] = ta.momentum.rsi(df["close"], window=14)

    return df


def evaluate_signals(df: pd.DataFrame) -> str:
    """
    Evaluates trading logic on the latest candle.
    Returns: 'BUY', 'SELL', or 'HOLD'
    """
    if df.empty or len(df) < 2:
        return "HOLD"

    current = df.iloc[-1]
    prev = df.iloc[-2]

    # Bullish EMA crossover & RSI filter
    if prev["ema_9"] <= prev["ema_21"] and current["ema_9"] > current["ema_21"] and current["rsi_14"] > 50:
        return "BUY"

    # Bearish EMA crossover & RSI filter
    if prev["ema_9"] >= prev["ema_21"] and current["ema_9"] < current["ema_21"] and current["rsi_14"] < 50:
        return "SELL"

    return "HOLD"


def run_strategy():
    app = KiteApp()

    if not app.is_connected():
        logger.error("Could not connect to Zerodha Kite. Run 'python auth.py' first.")
        return

    profile = app.get_profile()
    logger.info(f"Connected to Kite as: {profile.get('user_name', 'Trader')} ({profile.get('user_id')})")

    # Example: Trading INFY on NSE (Example Instrument Token: 408065)
    TRADING_SYMBOL = "INFY"
    EXCHANGE = "NSE"
    INSTRUMENT_TOKEN = 408065  # Replace with target instrument token
    QUANTITY = 1
    PRODUCT = "MIS"  # Intraday

    logger.info(f"Starting semi-automated strategy loop for {EXCHANGE}:{TRADING_SYMBOL}...")

    try:
        # Example Single Run (in production, wrap in schedule or interval loop)
        df = fetch_and_prepare_data(app, instrument_token=INSTRUMENT_TOKEN, days=3, interval="5minute")
        if df.empty:
            logger.warning("Could not fetch data for strategy evaluation.")
            return

        df = calculate_indicators(df)
        signal = evaluate_signals(df)

        latest_close = df["close"].iloc[-1]
        latest_rsi = df["rsi_14"].iloc[-1]
        logger.info(f"Latest Close: {latest_close:.2f} | RSI: {latest_rsi:.2f} | Signal: {signal}")

        if signal == "BUY":
            logger.info("BUY signal generated. Requesting order confirmation...")
            app.place_order_semi_auto(
                variety="regular",
                exchange=EXCHANGE,
                tradingsymbol=TRADING_SYMBOL,
                transaction_type="BUY",
                quantity=QUANTITY,
                product=PRODUCT,
                order_type="MARKET",
            )
        elif signal == "SELL":
            logger.info("SELL signal generated. Requesting order confirmation...")
            app.place_order_semi_auto(
                variety="regular",
                exchange=EXCHANGE,
                tradingsymbol=TRADING_SYMBOL,
                transaction_type="SELL",
                quantity=QUANTITY,
                product=PRODUCT,
                order_type="MARKET",
            )
        else:
            logger.info("No actionable signal. Holding position.")

    except KeyboardInterrupt:
        logger.info("Strategy stopped by user.")


if __name__ == "__main__":
    run_strategy()
