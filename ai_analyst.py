"""
BVNL 2AI  —  AI 1 : MARKET ANALYST (Gold & Forex Multi-Asset)
============================================================
The brain that reads the market. News plus charts.

It reads M1 through D1, scores the BVNL confluence factors, and produces
one structured opinion: BUY, SELL or WAIT, with entry, SL, TP and reasons.
Optimized for Gold (XAUUSD / GOLD) volatility and Forex.

What it CANNOT do — and this is enforced by there being no code for it here:
    - it cannot place an order
    - it cannot see the account balance
    - it cannot override a news block

It hands its opinion to AI 2 and has no further say.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import config
import indicators as ind
import news as news_mod

log = logging.getLogger("bvnl.analyst")

BUY, SELL, WAIT = "BUY", "SELL", "WAIT"


class MarketAnalyst:
    """AI 1. Read-only on the market, blind to the account."""

    def __init__(self, client, llm=None):
        self.client = client
        self.llm = llm

    @staticmethod
    def _is_gold(symbol: str) -> bool:
        s = (symbol or "").upper()
        return "XAU" in s or "GOLD" in s

    # -- main entry --------------------------------------------------------

    def analyse(self, symbol: str) -> dict:
        frames, snaps = {}, {}
        for tf in config.TIMEFRAMES:
            try:
                df = self.client.candles(symbol, tf, config.CANDLES)
                frames[tf] = df
                snaps[tf] = ind.snapshot(df)
            except Exception as e:
                log.warning("%s %s: %s", symbol, tf, e)

        entry_tf = getattr(config, "ENTRY_TF", "M15")
        trend_tf = getattr(config, "TREND_TF", "H4")

        if entry_tf not in snaps or trend_tf not in snaps:
            return self._wait(
                symbol,
                "MISSING DATA",
                f"Missing required timeframe data (need {entry_tf} and {trend_tf})",
                snaps=snaps,
            )

        entry_snap = snaps[entry_tf]
        trend_snap = snaps[trend_tf]

        # 1. News & sentiment checks
        news_state = news_mod.check(symbol) if hasattr(news_mod, "check") else {"status": getattr(news_mod, "AVAILABLE", "AVAILABLE")}
        sent = news_mod.sentiment(symbol) if hasattr(news_mod, "sentiment") else {}

        if news_state.get("status") == getattr(news_mod, "BLOCKED", "BLOCKED"):
            return self._wait(
                symbol,
                "NEWS BLOCKED",
                news_state.get("reason", "Trading blocked due to high impact news"),
                snaps=snaps,
                news=news_state,
                sentiment=sent,
            )

        # 2. Determine market direction
        direction = self._direction(entry_snap, trend_snap, snaps, symbol)
        if direction == WAIT:
            return self._wait(
                symbol,
                "WAIT",
                "Market flat across timeframes or timeframes in conflict",
                snaps=snaps,
                news=news_state,
                sentiment=sent,
            )

        # 3. Build trade setup (Entry, SL, TP)
        setup = self._build_setup(symbol, direction, entry_snap)
        if not setup:
            return self._wait(
                symbol,
                "NO SETUP",
                "Invalid setup geometry or risk:reward below minimum",
                snaps=snaps,
                news=news_state,
                sentiment=sent,
            )

        # 4. Strategy checks
        strategies = ind.strategies(frames) if hasattr(ind, "strategies") else {}

        # 5. Score confluence
        scores, reasons = self._score(
            direction, entry_snap, trend_snap, snaps, setup, news_state, sent, strategies, symbol
        )
        confidence = round(sum(scores.values()), 1)
        passes_threshold = confidence >= config.MIN_CONFIDENCE

        result = {
            "ai": "AI1_MARKET_ANALYST",
            "time": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "asset_type": "COMMODITY_GOLD" if self._is_gold(symbol) else "FOREX",
            "direction": direction,
            "raw_direction": direction,
            "confidence": confidence,
            "threshold": config.MIN_CONFIDENCE,
            "passes_threshold": passes_threshold,
            "entry": setup["entry"],
            "sl": setup["sl"],
            "tp": setup["tp"],
            "rr": setup["rr"],
            "sl_distance": setup["sl_distance"],
            "scores": scores,
            "reasons": reasons,
            "strategies": strategies,
            "news": news_state,
            "sentiment": sent,
            "session": self._session(),
            "snapshot": {tf: self._trim(s) for tf, s in snaps.items()},
        }

        if confidence < config.MIN_CONFIDENCE:
            result["reasons"].insert(
                0, f"BELOW THRESHOLD: {confidence}% < {config.MIN_CONFIDENCE}% — no trade"
            )

        if self.llm and getattr(self.llm, "enabled", False) and result["passes_threshold"]:
            try:
                result["commentary"] = self.llm.explain_signal(result)
            except Exception as e:
                log.warning("LLM commentary failed: %s", e)

        return result

    # -- direction ---------------------------------------------------------

    def _direction(self, entry_snap, trend_snap, snaps, symbol: str = "") -> str:
        """
        Direction comes from the higher timeframe. The entry timeframe may
        only agree or stand down — it never overrules H4.
        """
        t = trend_snap
        trend_bias = WAIT
        if t["ema_stack"] == "UP" or (t["structure"]["trend"] == "UP" and t["above_ema200"]):
            trend_bias = BUY
        elif t["ema_stack"] == "DOWN" or (t["structure"]["trend"] == "DOWN" and not t["above_ema200"]):
            trend_bias = SELL

        e = entry_snap
        entry_bias = WAIT
        bull = sum([
            e["ema"]["5"] > e["ema"]["20"],
            e["rsi"] > 50,
            e["macd_hist"] > 0,
            e["structure"]["trend"] == "UP" or e["structure"]["bos"] == "UP",
            e["plus_di"] > e["minus_di"],
        ])
        bear = sum([
            e["ema"]["5"] < e["ema"]["20"],
            e["rsi"] < 50,
            e["macd_hist"] < 0,
            e["structure"]["trend"] == "DOWN" or e["structure"]["bos"] == "DOWN",
            e["minus_di"] > e["plus_di"],
        ])
        if bull >= 3 and bull > bear:
            entry_bias = BUY
        elif bear >= 3 and bear > bull:
            entry_bias = SELL

        # Standard Multi-Timeframe Alignment:
        if trend_bias in (BUY, SELL) and entry_bias == trend_bias:
            return trend_bias

        # Scalper Logic: If scalping mode is enabled, prioritize entry momentum
        # when trend is flat, but never trade directly against strong trend bias.
        is_scalping = getattr(config, "SCALPING", False) or getattr(config, "SCALPING_MODE", False)
        if is_scalping:
            if trend_bias == WAIT and entry_bias in (BUY, SELL):
                return entry_bias
            if trend_bias in (BUY, SELL) and entry_bias in (BUY, SELL) and entry_bias == trend_bias:
                return trend_bias

        return WAIT

    # -- setup levels ------------------------------------------------------

    def _build_setup(self, symbol: str, direction: str, snap: dict):
        atr = snap.get("atr", 0.0)
        if atr <= 0:
            return None

        info = self.client.symbol_info(symbol)
        is_gold = self._is_gold(symbol)

        # Handle both dict and MT5 SymbolInfo object safely
        if info is not None:
            digits = getattr(info, "digits", None) if not isinstance(info, dict) else info.get("digits")
            ask = getattr(info, "ask", None) if not isinstance(info, dict) else info.get("ask")
            bid = getattr(info, "bid", None) if not isinstance(info, dict) else info.get("bid")
        else:
            digits, ask, bid = None, None, None

        if digits is None:
            digits = 2 if is_gold else 5

        entry = (ask if direction == BUY else bid) or snap.get("price", 0.0)
        if not entry or entry <= 0:
            return None

        # Multipliers: Gold needs slightly wider stop buffer for market noise / spreads
        sl_mult = getattr(config, "ATR_SL_MULT", 1.5)
        tp_mult = getattr(config, "ATR_TP_MULT", 3.0)

        sl_dist = atr * sl_mult
        tp_dist = atr * tp_mult

        # Structural stops: Gold wicks are larger, so give 0.25 ATR breathing buffer
        buffer_mult = 0.25 if is_gold else 0.15
        st = snap.get("structure", {})
        if direction == BUY and st.get("last_swing_low"):
            struct_dist = entry - st["last_swing_low"] + (atr * buffer_mult)
            if (atr * 0.8) <= struct_dist <= (atr * 3.5):
                sl_dist = struct_dist
        elif direction == SELL and st.get("last_swing_high"):
            struct_dist = st["last_swing_high"] - entry + (atr * buffer_mult)
            if (atr * 0.8) <= struct_dist <= (atr * 3.5):
                sl_dist = struct_dist

        # TP must clear the house minimum R:R
        min_rr = config.RISK.get("min_rr", 1.5) if hasattr(config, "RISK") and isinstance(config.RISK, dict) else 1.5
        tp_dist = max(tp_dist, sl_dist * min_rr)

        if direction == BUY:
            sl, tp = entry - sl_dist, entry + tp_dist
        else:
            sl, tp = entry + sl_dist, entry - tp_dist

        if sl_dist <= 0:
            return None

        return {
            "entry": round(entry, digits),
            "sl": round(sl, digits),
            "tp": round(tp, digits),
            "sl_distance": round(sl_dist, digits),
            "rr": round(tp_dist / sl_dist, 2),
        }

    # -- confluence scoring ------------------------------------------------

    def _score(self, direction, e, t, snaps, setup, news_state, sent, strategies, symbol: str = ""):
        """BVNL weights. Each factor returns 0..1, multiplied by its weight."""
        w = config.WEIGHTS
        scores, reasons = {}, []
        want_up = direction == BUY
        is_gold = self._is_gold(symbol)

        # --- TREND 20%
        f = 0.0
        if t["ema_stack"] == ("UP" if want_up else "DOWN"):
            f += 0.5
            reasons.append(f"{config.TREND_TF} EMA stack aligned {t['ema_stack']}")
        if t["structure"]["trend"] == ("UP" if want_up else "DOWN"):
            f += 0.3
            reasons.append(f"{config.TREND_TF} structure {t['structure']['trend']}")
        if t["adx"] > 25:
            f += 0.2
            reasons.append(f"{config.TREND_TF} ADX {t['adx']:.0f} — trend has strong conviction")
        elif t["adx"] < 18:
            reasons.append(f"{config.TREND_TF} ADX {t['adx']:.0f} — weak, choppy")
        scores["trend"] = round(min(f, 1.0) * w["trend"], 2)

        # --- RSI 15%  (multi-timeframe agreement & Gold momentum calibration)
        agree, checked = 0, 0
        for tf in ("M15", "H1", "H4"):
            if tf not in snaps:
                continue
            checked += 1
            r = snaps[tf]["rsi"]
            if want_up and 50 <= r <= 72:
                agree += 1
            elif (not want_up) and 28 <= r <= 50:
                agree += 1
        f = agree / checked if checked else 0.0
        er = e["rsi"]

        # Gold can run with high RSI during institutional trend surges
        max_overbought = 80 if is_gold else 75
        min_oversold = 20 if is_gold else 25

        if want_up and er > max_overbought:
            f *= 0.5
            reasons.append(f"RSI {er:.0f} overbought — chasing peak")
        elif (not want_up) and er < min_oversold:
            f *= 0.5
            reasons.append(f"RSI {er:.0f} oversold — chasing bottom")
        else:
            reasons.append(f"RSI {er:.0f} on {config.ENTRY_TF}, {agree}/{checked} timeframes aligned")
        scores["rsi"] = round(f * w["rsi"], 2)

        # --- MACD 15%
        f = 0.0
        if (e["macd_hist"] > 0) == want_up:
            f += 0.6
            expanding = abs(e["macd_hist"]) > abs(e.get("macd_hist_prev", 0.0))
            if expanding:
                f += 0.4
                reasons.append("MACD histogram expanding with the trade")
            else:
                reasons.append("MACD on side but momentum fading")
        else:
            reasons.append("MACD against the trade")
        scores["macd"] = round(min(f, 1.0) * w["macd"], 2)

        # --- VOLUME 15%
        vr = e["volume_ratio"]
        if vr >= 1.5:
            f, note = 1.0, f"volume {vr:.1f}x its 20-bar average — high market participation"
        elif vr >= 1.0:
            f, note = 0.65, f"volume {vr:.1f}x average"
        elif vr >= 0.7:
            f, note = 0.3, f"volume {vr:.1f}x average — thin"
        else:
            f, note = 0.0, f"volume {vr:.1f}x average — low participation"
        reasons.append(note)
        scores["volume"] = round(f * w["volume"], 2)

        # --- CANDLES 10%
        pats = e.get("patterns", {})
        want = "BULLISH" if want_up else "BEARISH"
        against = "BEARISH" if want_up else "BULLISH"
        if any(v == want for v in pats.values()):
            f = 1.0
            reasons.append("candle pattern: " +
                           ", ".join(k for k, v in pats.items() if v == want))
        elif any(v == against for v in pats.values()):
            f = 0.0
            reasons.append("candle pattern against the trade: " +
                           ", ".join(k for k, v in pats.items() if v == against))
        else:
            f = 0.4
        scores["candles"] = round(f * w["candles"], 2)

        # --- STRUCTURE 10%
        st = e["structure"]
        f = 0.0
        if st["trend"] == ("UP" if want_up else "DOWN"):
            f += 0.5
        if st["bos"] == ("UP" if want_up else "DOWN"):
            f += 0.3
            reasons.append(f"break of structure {st['bos']} on {config.ENTRY_TF}")
        if st.get("choch"):
            f = max(0.0, f - 0.3)
            reasons.append("change of character detected — trend may be turning")
        if want_up and st.get("support") and abs(e["price"] - st["support"]) < e["atr"]:
            f += 0.2
            reasons.append("price sitting on support")
        if (not want_up) and st.get("resistance") and abs(e["price"] - st["resistance"]) < e["atr"]:
            f += 0.2
            reasons.append("price sitting under resistance")
        scores["structure"] = round(min(f, 1.0) * w["structure"], 2)

        # --- RISK:REWARD 10%
        rr = setup["rr"]
        min_rr = config.RISK.get("min_rr", 1.5) if hasattr(config, "RISK") and isinstance(config.RISK, dict) else 1.5
        if rr >= 3.0:
            f = 1.0
        elif rr >= 2.0:
            f = 0.85
        elif rr >= min_rr:
            f = 0.6
        else:
            f = 0.0
        reasons.append(f"risk:reward 1:{rr}")
        scores["rr"] = round(f * w["rr"], 2)

        # --- NEWS AND SESSION 5%
        f = 0.0
        if news_state.get("status") == getattr(news_mod, "AVAILABLE", "AVAILABLE"):
            f += 0.6
        elif news_state.get("status") in (getattr(news_mod, "STALE", "STALE"), getattr(news_mod, "UNKNOWN", "UNKNOWN")):
            f += 0.1
            reasons.append(f"news status {news_state.get('status')} — treat with caution")
        else:
            reasons.append(f"NEWS HIGH RISK: {news_state.get('reason', 'Blocked')}")

        cur_session = self._session()
        allowed_sessions = getattr(config, "ALLOWED_SESSIONS", [])
        if not allowed_sessions or cur_session in allowed_sessions:
            f += 0.4
        else:
            reasons.append(f"outside preferred sessions (now: {cur_session})")

        # Special session check for Gold:
        if is_gold and cur_session == "ASIAN":
            reasons.append("Gold Asian session — lower liquidity, watch for spread expansion")

        bias = sent.get("bias")
        if bias == "HIGH_RISK":
            f = 0.0
            reasons.append("news sentiment flags high risk")
        elif bias in ("BULLISH", "BEARISH"):
            if (bias == "BULLISH") != want_up:
                f *= 0.5
                reasons.append(f"news sentiment {bias} — against this trade")
            else:
                reasons.append(f"news sentiment {bias} — with this trade")
        scores["news"] = round(min(f, 1.0) * w["news"], 2)

        # --- strategy agreement note
        agreeing = [k for k, v in strategies.items() if v == direction]
        if agreeing:
            reasons.append("strategies agreeing: " + ", ".join(agreeing))
        else:
            reasons.append("no named strategy agrees with this direction")

        return scores, reasons

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _session():
        h = datetime.now(timezone.utc).hour
        sessions = getattr(config, "SESSIONS", {"LONDON": (7, 16), "NY": (12, 21), "ASIAN": (0, 8)})
        active = [name for name, (s, e) in sessions.items() if s <= h < e]
        if "LONDON" in active and "NY" in active:
            return "LONDON_NY_OVERLAP"
        return active[0] if active else "OFF_SESSION"

    @staticmethod
    def _trim(snap):
        """Keep the journal readable — drop the bulky bits."""
        return {k: snap[k] for k in
                ("price", "rsi", "adx", "atr", "ema_stack", "volume_ratio",
                 "macd_hist", "bb_position") if k in snap}

    def _wait(self, symbol, headline, detail, snaps=None, news=None, sentiment=None):
        return {
            "ai": "AI1_MARKET_ANALYST",
            "time": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol, "direction": WAIT, "raw_direction": WAIT,
            "confidence": 0.0, "threshold": config.MIN_CONFIDENCE,
            "passes_threshold": False,
            "entry": None, "sl": None, "tp": None, "rr": None,
            "scores": {}, "reasons": [headline, detail],
            "strategies": {}, "news": news or {"status": getattr(news_mod, "UNKNOWN", "UNKNOWN")},
            "sentiment": sentiment or {},
            "session": self._session(),
            "snapshot": {tf: self._trim(s) for tf, s in (snaps or {}).items()},
        }