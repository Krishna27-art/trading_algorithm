from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
import json
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from backend.data.time_utils import MarketCalendar, now_ist_naive
from backend.monitoring.logger import logger


@dataclass
class VolumeProfileFacts:
    symbol: str = ""
    instrument_token: int = 0
    session_date: Optional[date] = None
    status: str = "UNAVAILABLE"
    source: str = "KITE_WEBSOCKET_OBSERVED"
    profile_method: str = "OBSERVED_TICK_INCREMENTAL"
    tick_size: float = 0.05
    total_observed_volume: int = 0
    poc: Optional[float] = None
    vah: Optional[float] = None
    val: Optional[float] = None
    developing_poc: Optional[float] = None
    developing_vah: Optional[float] = None
    developing_val: Optional[float] = None
    poc_direction: str = "UNAVAILABLE"
    current_price: Optional[float] = None
    distance_to_poc: Optional[float] = None
    distance_to_vah: Optional[float] = None
    distance_to_val: Optional[float] = None
    distance_pct_poc: Optional[float] = None
    distance_in_ticks_poc: Optional[int] = None
    position_to_value: str = "UNAVAILABLE"
    position_to_poc: str = "UNAVAILABLE"
    is_vp_allowed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "instrument_token": self.instrument_token,
            "session_date": self.session_date.isoformat() if self.session_date else None,
            "status": self.status,
            "source": self.source,
            "profile_method": self.profile_method,
            "tick_size": self.tick_size,
            "total_observed_volume": self.total_observed_volume,
            "poc": self.poc,
            "vah": self.vah,
            "val": self.val,
            "developing_poc": self.developing_poc,
            "developing_vah": self.developing_vah,
            "developing_val": self.developing_val,
            "poc_direction": self.poc_direction,
            "current_price": self.current_price,
            "distance_to_poc": self.distance_to_poc,
            "distance_to_vah": self.distance_to_vah,
            "distance_to_val": self.distance_to_val,
            "distance_pct_poc": self.distance_pct_poc,
            "distance_in_ticks_poc": self.distance_in_ticks_poc,
            "position_to_value": self.position_to_value,
            "position_to_poc": self.position_to_poc,
            "is_vp_allowed": self.is_vp_allowed,
        }


class SingleStockVolumeProfile:
    def __init__(
        self,
        symbol: str,
        instrument_token: int,
        session_date: date,
        tick_size: float = 0.05,
        value_area_pct: float = 0.70,
    ):
        self.symbol = str(symbol).strip().upper()
        self.instrument_token = int(instrument_token)
        self.session_date = session_date
        self.tick_size = float(tick_size) if tick_size > 0 else 0.05
        self.value_area_pct = float(value_area_pct)
        self.profile: Dict[float, int] = {}
        self.total_observed_volume: int = 0
        self.last_cumulative_volume: Optional[int] = None
        self.first_tick_time: Optional[datetime] = None
        self.last_trade_time: Optional[datetime] = None
        self.status: str = "INITIALIZING"
        self.gap_detected: bool = False
        self.gap_count: int = 0
        self.poc_history: List[Tuple[datetime, float]] = []
        self._cached_poc: Optional[float] = None
        self._cached_vah: Optional[float] = None
        self._cached_val: Optional[float] = None
        self._dirty: bool = False

    def round_to_tick(self, price: float) -> float:
        ticks = round(price / self.tick_size)
        return round(ticks * self.tick_size, 4)

    def process_tick(
        self,
        price: float,
        cumulative_volume: Optional[int],
        timestamp: datetime,
    ) -> bool:
        if not math.isfinite(price) or price <= 0:
            return False
        if isinstance(cumulative_volume, bool) or cumulative_volume is None:
            return False
        try:
            cum_vol = int(cumulative_volume)
        except (TypeError, ValueError):
            return False
        if cum_vol < 0:
            return False

        if self.first_tick_time is None:
            self.first_tick_time = timestamp
        if self.last_cumulative_volume is None:
            self.last_cumulative_volume = cum_vol
            self.last_trade_time = timestamp
            if not self.gap_detected:
                self.status = "OBSERVING"
            return True

        if cum_vol < self.last_cumulative_volume:
            logger.warning(
                "[%s] Cumulative volume decrease %s -> %s. Re-baselining.",
                self.symbol,
                self.last_cumulative_volume,
                cum_vol,
            )
            self.last_cumulative_volume = cum_vol
            self.last_trade_time = timestamp
            return True

        delta = cum_vol - self.last_cumulative_volume
        self.last_cumulative_volume = cum_vol
        self.last_trade_time = timestamp

        if delta <= 0:
            return True

        bucket = self.round_to_tick(price)
        self.profile[bucket] = self.profile.get(bucket, 0) + delta
        self.total_observed_volume += delta
        self._dirty = True

        if not self.gap_detected:
            self.status = "OBSERVING"

        return True

    def handle_connection_gap(self) -> None:
        self.gap_detected = True
        self.gap_count += 1
        self.status = "DEGRADED"
        self.last_cumulative_volume = None

    def calculate_poc(self) -> Tuple[Optional[float], int]:
        if not self.profile:
            return None, 0
        best_price = None
        max_vol = -1
        for p, v in self.profile.items():
            if v > max_vol or (v == max_vol and (best_price is None or p > best_price)):
                max_vol = v
                best_price = p
        return best_price, max_vol

    def calculate_value_area(self) -> Tuple[Optional[float], Optional[float]]:
        if not self.profile or self.total_observed_volume <= 0:
            return None, None
        poc_price, _ = self.calculate_poc()
        if poc_price is None:
            return None, None

        sorted_prices = sorted(self.profile.keys())
        poc_idx = sorted_prices.index(poc_price)

        target_vol = self.total_observed_volume * self.value_area_pct
        accumulated_vol = self.profile[poc_price]

        up_idx = poc_idx + 1
        down_idx = poc_idx - 1

        while accumulated_vol < target_vol and (up_idx < len(sorted_prices) or down_idx >= 0):
            has_up = up_idx < len(sorted_prices)
            has_down = down_idx >= 0

            if has_up and has_down:
                up_vol = self.profile[sorted_prices[up_idx]]
                down_vol = self.profile[sorted_prices[down_idx]]
                if up_vol > down_vol:
                    accumulated_vol += up_vol
                    up_idx += 1
                elif down_vol > up_vol:
                    accumulated_vol += down_vol
                    down_idx -= 1
                else:
                    accumulated_vol += up_vol + down_vol
                    up_idx += 1
                    down_idx -= 1
            elif has_up:
                accumulated_vol += self.profile[sorted_prices[up_idx]]
                up_idx += 1
            else:
                accumulated_vol += self.profile[sorted_prices[down_idx]]
                down_idx -= 1

        val = sorted_prices[max(down_idx + 1, 0)]
        vah = sorted_prices[min(up_idx - 1, len(sorted_prices) - 1)]

        if val > poc_price:
            val = poc_price
        if vah < poc_price:
            vah = poc_price

        return val, vah

    def get_developing_levels(self) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        if self._dirty or self._cached_poc is None:
            poc, _ = self.calculate_poc()
            val, vah = self.calculate_value_area()
            if poc is not None and (not self.poc_history or self.poc_history[-1][1] != poc):
                ts = self.last_trade_time or now_ist_naive()
                self.poc_history.append((ts, poc))
            self._cached_poc = poc
            self._cached_val = val
            self._cached_vah = vah
            self._dirty = False
        return self._cached_poc, self._cached_val, self._cached_vah

    def get_poc_direction(self) -> str:
        if len(self.poc_history) < 2:
            return "STABLE"
        first_poc = self.poc_history[0][1]
        latest_poc = self.poc_history[-1][1]
        if latest_poc > first_poc:
            return "RISING"
        elif latest_poc < first_poc:
            return "FALLING"
        return "STABLE"

    def to_snapshot_dict(self) -> Dict[str, Any]:
        poc, val, vah = self.get_developing_levels()
        levels = [
            {"price": p, "volume": v}
            for p, v in sorted(self.profile.items())
        ]
        is_complete = (
            not self.gap_detected
            and self.total_observed_volume > 0
            and self.first_tick_time is not None
            and self.first_tick_time.time() <= time(9, 16)
            and self.last_trade_time is not None
            and self.last_trade_time.time() >= time(15, 29)
        )
        return {
            "symbol": self.symbol,
            "instrument_token": self.instrument_token,
            "exchange": "NSE",
            "trading_date": self.session_date.isoformat(),
            "first_tick_time": self.first_tick_time.isoformat() if self.first_tick_time else None,
            "last_trade_time": self.last_trade_time.isoformat() if self.last_trade_time else None,
            "tick_size": self.tick_size,
            "status": "COMPLETE" if is_complete else self.status,
            "source": "KITE_WEBSOCKET_OBSERVED",
            "profile_method": "OBSERVED_TICK_INCREMENTAL",
            "total_observed_volume": self.total_observed_volume,
            "poc": poc,
            "vah": vah,
            "val": val,
            "gap_detected": self.gap_detected,
            "gap_count": self.gap_count,
            "levels": levels,
        }


class VolumeProfileEngine:
    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        tick_sizes: Optional[Dict[str, float]] = None,
    ):
        base = Path(__file__).resolve().parent.parent
        self.cache_dir = cache_dir or (base / "data" / "cache" / "volume_profile")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.tick_sizes: Dict[str, float] = tick_sizes or {}
        self.profiles: Dict[str, SingleStockVolumeProfile] = {}
        self.previous_profiles: Dict[str, VolumeProfileFacts] = {}
        self.current_session_date: Optional[date] = None

    def get_tick_size(self, symbol: str) -> float:
        return self.tick_sizes.get(symbol.strip().upper(), 0.05)

    def register_instrument(
        self,
        symbol: str,
        instrument_token: int,
        session_date: date,
        tick_size: Optional[float] = None,
    ) -> SingleStockVolumeProfile:
        sym = symbol.strip().upper()
        ts = tick_size if tick_size and tick_size > 0 else self.get_tick_size(sym)
        self.current_session_date = session_date
        profile = SingleStockVolumeProfile(
            symbol=sym,
            instrument_token=instrument_token,
            session_date=session_date,
            tick_size=ts,
        )
        self.profiles[sym] = profile
        return profile

    def initialize_universe(
        self,
        token_to_symbol: Dict[int, str],
        reference_date: Optional[date] = None,
    ) -> None:
        today = reference_date or now_ist_naive().date()
        self.current_session_date = today
        for token, symbol in token_to_symbol.items():
            sym = str(symbol).strip().upper()
            self.register_instrument(
                symbol=sym,
                instrument_token=int(token),
                session_date=today,
            )
            self.load_previous_session_profile(
                symbol=sym,
                instrument_token=int(token),
                reference_date=today,
            )

    def handle_connection_gap(self) -> None:
        for profile in self.profiles.values():
            profile.handle_connection_gap()

    def process_tick(
        self,
        symbol: str,
        instrument_token: int,
        price: float,
        cumulative_volume: Optional[int],
        timestamp: datetime,
    ) -> bool:
        sym = symbol.strip().upper()
        tick_date = timestamp.date()
        profile = self.profiles.get(sym)
        if profile is not None and profile.session_date != tick_date:
            if profile.total_observed_volume > 0:
                self.save_session_profiles()
            profile = None
        if profile is None:
            ts = self.get_tick_size(sym)
            profile = self.register_instrument(
                symbol=sym,
                instrument_token=instrument_token,
                session_date=tick_date,
                tick_size=ts,
            )
        return profile.process_tick(
            price=price,
            cumulative_volume=cumulative_volume,
            timestamp=timestamp,
        )

    def process_ticks(self, ticks: List[Dict[str, Any]], token_to_symbol: Dict[int, str]) -> int:
        processed = 0
        for tick in ticks:
            if not isinstance(tick, dict):
                continue
            raw_token = tick.get("instrument_token")
            try:
                token = int(raw_token)
            except (TypeError, ValueError):
                continue
            symbol = token_to_symbol.get(token)
            if not symbol:
                continue

            raw_price = tick.get("last_price")
            if raw_price is None:
                continue
            try:
                price = float(raw_price)
            except (TypeError, ValueError):
                continue

            cum_vol = tick.get("volume_traded")
            ts = tick.get("exchange_timestamp") or tick.get("timestamp")
            if not isinstance(ts, datetime):
                continue

            if self.process_tick(
                symbol=symbol,
                instrument_token=token,
                price=price,
                cumulative_volume=cum_vol,
                timestamp=ts,
            ):
                processed += 1
        return processed

    def save_session_profiles(self) -> None:
        for sym, prof in self.profiles.items():
            if prof.total_observed_volume <= 0:
                continue
            filename = f"{sym}_{prof.session_date.isoformat()}.json"
            target = self.cache_dir / filename
            tmp = self.cache_dir / f"{filename}.tmp"
            data = prof.to_snapshot_dict()
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, target)
            except Exception as exc:
                logger.error("Failed to save volume profile for %s: %s", sym, exc)

    def load_previous_session_profile(
        self,
        symbol: str,
        instrument_token: int,
        reference_date: Optional[date] = None,
    ) -> VolumeProfileFacts:
        sym = symbol.strip().upper()
        ref_date = reference_date or now_ist_naive().date()
        try:
            prev_session = MarketCalendar.previous_trading_session(ref_date)
        except Exception:
            return VolumeProfileFacts(
                symbol=sym,
                instrument_token=instrument_token,
                status="UNAVAILABLE",
                is_vp_allowed=False,
            )

        filepath = self.cache_dir / f"{sym}_{prev_session.isoformat()}.json"
        if not filepath.exists():
            return VolumeProfileFacts(
                symbol=sym,
                instrument_token=instrument_token,
                session_date=prev_session,
                status="UNAVAILABLE",
                is_vp_allowed=False,
            )

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            logger.warning("Corrupt volume profile file for %s: %s", sym, exc)
            return VolumeProfileFacts(
                symbol=sym,
                instrument_token=instrument_token,
                session_date=prev_session,
                status="INVALID",
                is_vp_allowed=False,
            )

        if not isinstance(data, dict):
            return VolumeProfileFacts(
                symbol=sym,
                instrument_token=instrument_token,
                session_date=prev_session,
                status="INVALID",
                is_vp_allowed=False,
            )

        if str(data.get("symbol", "")).upper() != sym:
            return VolumeProfileFacts(
                symbol=sym,
                instrument_token=instrument_token,
                session_date=prev_session,
                status="INVALID",
                is_vp_allowed=False,
            )

        if int(data.get("instrument_token", 0)) != int(instrument_token):
            return VolumeProfileFacts(
                symbol=sym,
                instrument_token=instrument_token,
                session_date=prev_session,
                status="INVALID",
                is_vp_allowed=False,
            )

        if data.get("trading_date") != prev_session.isoformat():
            return VolumeProfileFacts(
                symbol=sym,
                instrument_token=instrument_token,
                session_date=prev_session,
                status="STALE",
                is_vp_allowed=False,
            )

        poc = data.get("poc")
        vah = data.get("vah")
        val = data.get("val")

        if poc is None or vah is None or val is None:
            return VolumeProfileFacts(
                symbol=sym,
                instrument_token=instrument_token,
                session_date=prev_session,
                status="INVALID",
                is_vp_allowed=False,
            )

        try:
            f_poc = float(poc)
            f_vah = float(vah)
            f_val = float(val)
        except (TypeError, ValueError):
            return VolumeProfileFacts(
                symbol=sym,
                instrument_token=instrument_token,
                session_date=prev_session,
                status="INVALID",
                is_vp_allowed=False,
            )

        if not (math.isfinite(f_poc) and math.isfinite(f_vah) and math.isfinite(f_val)):
            return VolumeProfileFacts(
                symbol=sym,
                instrument_token=instrument_token,
                session_date=prev_session,
                status="INVALID",
                is_vp_allowed=False,
            )

        if not (f_val <= f_poc <= f_vah):
            return VolumeProfileFacts(
                symbol=sym,
                instrument_token=instrument_token,
                session_date=prev_session,
                status="INVALID",
                is_vp_allowed=False,
            )

        total_vol = int(data.get("total_observed_volume", 0))
        tick_sz = float(data.get("tick_size", self.get_tick_size(sym)))
        st = str(data.get("status", "UNAVAILABLE"))
        gap = bool(data.get("gap_detected", False))
        is_allowed = (st == "COMPLETE" and not gap)

        facts = VolumeProfileFacts(
            symbol=sym,
            instrument_token=instrument_token,
            session_date=prev_session,
            status=st,
            source=data.get("source", "KITE_WEBSOCKET_OBSERVED"),
            profile_method=data.get("profile_method", "OBSERVED_TICK_INCREMENTAL"),
            tick_size=tick_sz,
            total_observed_volume=total_vol,
            poc=f_poc,
            vah=f_vah,
            val=f_val,
            is_vp_allowed=is_allowed,
        )
        self.previous_profiles[sym] = facts
        return facts

    def get_facts(
        self,
        symbol: str,
        current_price: Optional[float] = None,
        reference_date: Optional[date] = None,
    ) -> VolumeProfileFacts:
        sym = symbol.strip().upper()
        profile = self.profiles.get(sym)

        token = profile.instrument_token if profile else 0
        prev_facts = self.previous_profiles.get(sym)
        if prev_facts is None and token > 0:
            prev_facts = self.load_previous_session_profile(
                symbol=sym,
                instrument_token=token,
                reference_date=reference_date,
            )

        dev_poc = None
        dev_val = None
        dev_vah = None
        poc_dir = "UNAVAILABLE"
        today_vol = 0
        today_status = "UNAVAILABLE"
        tick_sz = self.get_tick_size(sym)

        if profile is not None:
            dev_poc, dev_val, dev_vah = profile.get_developing_levels()
            poc_dir = profile.get_poc_direction()
            today_vol = profile.total_observed_volume
            today_status = profile.status
            tick_sz = profile.tick_size

        prior_poc = prev_facts.poc if prev_facts else None
        prior_vah = prev_facts.vah if prev_facts else None
        prior_val = prev_facts.val if prev_facts else None

        active_poc = prior_poc if prior_poc is not None else dev_poc
        active_vah = prior_vah if prior_vah is not None else dev_vah
        active_val = prior_val if prior_val is not None else dev_val

        pos_val = "UNAVAILABLE"
        pos_poc = "UNAVAILABLE"
        dist_poc = None
        dist_vah = None
        dist_val = None
        dist_pct = None
        dist_ticks = None

        if current_price is not None and math.isfinite(current_price) and current_price > 0:
            if active_vah is not None and active_val is not None:
                if current_price > active_vah:
                    pos_val = "ABOVE_VALUE"
                elif current_price < active_val:
                    pos_val = "BELOW_VALUE"
                else:
                    pos_val = "INSIDE_VALUE"

            if active_poc is not None:
                half_tick = tick_sz / 2.0
                if abs(current_price - active_poc) <= half_tick:
                    pos_poc = "AT_POC"
                elif current_price > active_poc:
                    pos_poc = "ABOVE_POC"
                else:
                    pos_poc = "BELOW_POC"

                dist_poc = round(current_price - active_poc, 4)
                dist_pct = round((dist_poc / active_poc) * 100.0, 4)
                dist_ticks = int(round(dist_poc / tick_sz)) if tick_sz > 0 else 0

            if active_vah is not None:
                dist_vah = round(current_price - active_vah, 4)
            if active_val is not None:
                dist_val = round(current_price - active_val, 4)

        overall_status = today_status if today_status in ("OBSERVING", "DEGRADED") else (
            prev_facts.status if prev_facts else "UNAVAILABLE"
        )
        is_allowed = (prev_facts.is_vp_allowed if prev_facts else False) or (
            today_status == "OBSERVING" and dev_poc is not None
        )

        return VolumeProfileFacts(
            symbol=sym,
            instrument_token=token,
            session_date=self.current_session_date or (prev_facts.session_date if prev_facts else None),
            status=overall_status,
            source="KITE_WEBSOCKET_OBSERVED",
            profile_method="OBSERVED_TICK_INCREMENTAL",
            tick_size=tick_sz,
            total_observed_volume=today_vol,
            poc=prior_poc,
            vah=prior_vah,
            val=prior_val,
            developing_poc=dev_poc,
            developing_vah=dev_vah,
            developing_val=dev_val,
            poc_direction=poc_dir,
            current_price=current_price,
            distance_to_poc=dist_poc,
            distance_to_vah=dist_vah,
            distance_to_val=dist_val,
            distance_pct_poc=dist_pct,
            distance_in_ticks_poc=dist_ticks,
            position_to_value=pos_val,
            position_to_poc=pos_poc,
            is_vp_allowed=is_allowed,
        )
