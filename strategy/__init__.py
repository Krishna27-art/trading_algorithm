from .base_strategy import BaseStrategy, SignalAction, StrategySignal
from .orb_strategy import IntradayORBStrategy
from .cpr_strategy import CPRRegimeBreakoutStrategy
from .dual_ema_strategy import BufferedDualEMAStrategy

__all__ = [
    # intraday, single-instrument (ORB, CPR, Dual-EMA)
    "BaseStrategy",
    "IntradayORBStrategy",
    "CPRRegimeBreakoutStrategy",
    "BufferedDualEMAStrategy",
    "SignalAction",
    "StrategySignal",
]

# NSE-RM-100, NSE-VRP-INDEX, and APEX-AIVEM (portfolio_base, residual_momentum,
# vrp_index, apex_engine) are research/experimental and outside this repo's
# intraday ORB/CPR/Dual-EMA scope. They're no longer imported here — this used
# to mean `import strategy` (or anything under it, including prediction_service)
# ALWAYS pulled in apex_engine.py, which imports `polars`, a package not even
# listed in requirements.txt. Anyone who wants those strategies can still
# import them directly, e.g. `from strategy.apex_engine import ApexAivemEngine`.
