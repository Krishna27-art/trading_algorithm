from datetime import datetime

from backend.backtest.strategy_backtester import StrategyBacktester
from backend.config.settings import settings
from backend.database.db import DatabaseManager
from backend.database.db import OrderDirection, TradeRecord


def _trade(trade_id: str, is_paper: bool) -> TradeRecord:
    return TradeRecord(
        trade_id=trade_id,
        symbol="SBIN",
        direction=OrderDirection.BUY,
        entry_time=datetime(2026, 10, 1, 10, 0),
        entry_price=1000.0,
        quantity=1,
        initial_stop=990.0,
        initial_target=1020.0,
        is_paper=is_paper,
    )


def test_live_trade_query_excludes_backtest_rows(tmp_path):
    db = DatabaseManager(tmp_path / "trading_system.db")

    db.record_trade_entry(_trade("BT_2026-10-01_TEST", is_paper=True))
    db.record_trade_entry(_trade("LIVE_2026-10-01_TEST", is_paper=False))

    live_trades = db.get_live_trades()

    assert len(live_trades) == 1
    assert live_trades[0]["trade_id"] == "LIVE_2026-10-01_TEST"


def test_open_live_trade_query_excludes_backtest_rows(tmp_path):
    db = DatabaseManager(tmp_path / "trading_system.db")

    db.record_trade_entry(_trade("BT_OPEN", is_paper=True))
    db.record_trade_entry(_trade("LIVE_OPEN", is_paper=False))

    open_live = db.get_open_live_trades()

    assert len(open_live) == 1
    assert open_live[0]["trade_id"] == "LIVE_OPEN"


def test_strategy_backtester_persistence_is_opt_in():
    from backend.strategy.orb_strategy import IntradayORBStrategy

    backtester = StrategyBacktester(
        strategy_factory=lambda: IntradayORBStrategy(
            settings.instruments[0],
            settings.strategy,
        ),
        instrument=settings.instruments[0],
        app_settings=settings,
    )

    assert backtester.persist_trades is False
