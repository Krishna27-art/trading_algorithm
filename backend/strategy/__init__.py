from .base_strategy import BaseStrategy, SignalAction, StrategySignal
from .intraday_base import IntradaySignalStrategy
from .gap_fade_strategy import GapFadeStrategy
from .rsi_ema_strategy import RsiEmaStrategy
from .orb_sip_strategy import OrbSipStrategy

# The only strategies in the system. Key -> class.
STRATEGIES = {
    GapFadeStrategy.key: GapFadeStrategy,
    RsiEmaStrategy.key: RsiEmaStrategy,
    OrbSipStrategy.key: OrbSipStrategy,
}

STRATEGY_LABELS = {key: cls.display_name for key, cls in STRATEGIES.items()}

__all__ = [
    "BaseStrategy",
    "SignalAction",
    "StrategySignal",
    "IntradaySignalStrategy",
    "GapFadeStrategy",
    "RsiEmaStrategy",
    "OrbSipStrategy",
    "STRATEGIES",
    "STRATEGY_LABELS",
]
