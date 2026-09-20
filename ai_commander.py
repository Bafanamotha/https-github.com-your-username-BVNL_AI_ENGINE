"""
BVNL 2AI  —  AI 2 : TRADE COMMANDER
===================================
The gatekeeper. The only module in the system that can send an order.

It takes the Analyst's opinion and checks it against the account, the
exposure, the spread, the day's damage and the news. Then it returns one
of five verdicts:

    EXECUTE_BUY
    EXECUTE_SELL
    WAIT                    nothing wrong, just no signal
    REJECT_TRADE            there is a signal, but a rule says no
    HIGH_RISK_DO_NOT_TRADE  conditions are dangerous, stand down entirely

Every check is plain Python. No LLM has a vote here. The LLM may write a
sentence explaining the verdict afterwards, and that is all.

Design rule: the Commander can always say no to the Analyst. The Analyst
can never say no to the Commander, because it is never asked.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import config
import news as news_mod

log = logging.getLogger("bvnl.commander")

EXECUTE_BUY = "EXECUTE_BUY"
EXECUTE_SELL = "EXECUTE_SELL"
WAIT = "WAIT"
REJECT_TRADE = "REJECT_TRADE"
HIGH_RISK = "HIGH_RISK_DO_NOT_TRADE"


class TradeCommander:
    def __init__(self, client, journal=None, llm=None):
        self.client = client
        self.journal = journal
        self.llm = llm

    # =======================================================================
    #  DECIDE
    # =======================================================================

    def decide(self, signal: dict) -> dict:
        symbol = signal["symbol"]
        acct = self.client.account()
        balance = acct["balance"]
        equity = acct["equity"]

        checks = []          # every check, pass or fail, for the journal

        def ok(name, detail=""):
            checks.append({"check": name, "pass": True, "detail": detail})

        def fail(name, detail):
            checks.append({"check": name, "pass": False, "detail": detail})
            return detail

        # -------------------------------------------------------------------
        # 0. Account sanity — before anything else
        # -------------------------------------------------------------------
        if balance <= 0:
            return self._verdict(HIGH_RISK, signal, checks,
                                 [fail("balance", f"balance is {balance}")], acct)

        if not config.DRY_RUN and not config.ALLOW_LIVE_ACCOUNT and not self.client.is_demo():
            return self._verdict(
                HIGH_RISK, signal, checks,
                [fail("account_type",
                      "This is a LIVE account and BVNL_ALLOW_LIVE is not set. "
                      "Refusing to trade. Demo first — your own rule.")], acct)
        ok("account_type", "demo" if self.client.is_demo() else "live (explicitly allowed)")

        # -------------------------------------------------------------------
        # 1. Hard stops that apply even with no signal
        # -------------------------------------------------------------------
        blockers = []

        dd = (equity - balance) / balance
        day = self.day_stats()

        if day["pl_pct"] <= config.RISK["daily_loss_limit"]:
            blockers.append(fail(
                "daily_loss_limit",
                f"day P/L {day['pl_pct']:.2%} has hit the "
                f"{config.RISK['daily_loss_limit']:.0%} limit — trading is done for today"))
        else:
            ok("daily_loss_limit",
               f"day P/L {day['pl_pct']:.2%}, room to "
               f"{config.RISK['daily_loss_limit']:.0%}")

        if dd <= config.RISK["max_drawdown"]:
            blockers.append(fail(
                "max_drawdown",
                f"equity is {dd:.2%} below balance, past the "
                f"{config.RISK['max_drawdown']:.0%} ceiling"))
        else:
            ok("max_drawdown", f"floating {dd:.2%}")

        if day["consecutive_losses"] >= config.RISK["max_consecutive_losses"]:
            blockers.append(fail(
                "consecutive_losses",
                f"{day['consecutive_losses']} losses in a row — stop and review, "
                "do not trade through it"))
        else:
            ok("consecutive_losses", str(day["consecutive_losses"]))

        if day["trades"] >= config.RISK["max_trades_per_day"]:
            blockers.append(fail(
                "max_trades_per_day",
                f"{day['trades']} trades today, limit is "
                f"{config.RISK['max_trades_per_day']}"))
        else:
            ok("max_trades_per_day", f"{day['trades']}/{config.RISK['max_trades_per_day']}")

        if blockers:
            return self._verdict(HIGH_RISK, signal, checks, blockers, acct)

        # -------------------------------------------------------------------
        # 2. News — independent of whatever the Analyst thought
        # -------------------------------------------------------------------
        news_state = news_mod.news_check(symbol)
        if news_state["blocked"]:
            fail("news", news_state["reason"])
            return self._verdict(HIGH_RISK, signal, checks,
                                 [news_state["reason"]], acct, news=news_state)
        ok("news", f"{news_state['status']} — {news_state['reason']}")

        # -------------------------------------------------------------------
        # 3. Is there even a signal?
        # -------------------------------------------------------------------
        if signal.get("direction") not in ("BUY", "SELL"):
            ok("signal", "analyst says WAIT")
            return self._verdict(WAIT, signal, checks,
                                 ["No trade signal from the Analyst."], acct,
                                 news=news_state)

        if not signal.get("passes_threshold"):
            reason = (f"confidence {signal.get('confidence')}% is below the "
                      f"{config.MIN_CONFIDENCE}% BVNL threshold")
            fail("confidence", reason)
            return self._verdict(WAIT, signal, checks, [reason], acct, news=news_state)
        ok("confidence", f"{signal['confidence']}% >= {config.MIN_CONFIDENCE}%")

        direction = signal["direction"]
        rejects = []

        # -------------------------------------------------------------------
        # 4. The setup itself
        # -------------------------------------------------------------------
        entry, sl, tp = signal.get("entry"), signal.get("sl"), signal.get("tp")

        if config.RISK["require_stop_loss"] and not sl:
            rejects.append(fail("stop_loss", "no stop loss — never permitted"))
        else:
            ok("stop_loss", str(sl))

        if sl:
            if direction == "BUY" and sl >= entry:
                rejects.append(fail("sl_side", "BUY stop loss is not below entry"))
            elif direction == "SELL" and sl <= entry:
                rejects.append(fail("sl_side", "SELL stop loss is not above entry"))
            else:
                ok("sl_side")

        rr = signal.get("rr") or 0
        if rr < config.RISK["min_rr"]:
            rejects.append(fail("min_rr",
                                f"R:R 1:{rr} is below the house minimum 1:{config.RISK['min_rr']}"))
        else:
            ok("min_rr", f"1:{rr}")

        # SL distance vs ATR — catches a stop that is absurdly tight or wide
        atr = signal.get("atr") or 0
        sl_dist = signal.get("sl_distance") or (abs(entry - sl) if (entry and sl) else 0)
        if atr > 0 and sl_dist > 0:
            mult = sl_dist / atr
            if mult < config.RISK["min_sl_atr_mult"]:
                rejects.append(fail("sl_atr",
                                    f"stop is {mult:.2f} x ATR — too tight, noise will take it"))
            elif mult > config.RISK["max_sl_atr_mult"]:
                rejects.append(fail("sl_atr",
                                    f"stop is {mult:.2f} x ATR — too wide for the edge"))
            else:
                ok("sl_atr", f"{mult:.2f} x ATR")

        # -------------------------------------------------------------------
        # 5. Live market conditions
        # -------------------------------------------------------------------
        try:
            info = self.client.symbol_info(symbol)
        except Exception as e:
            rejects.append(fail("symbol_info", str(e)))
            return self._verdict(REJECT_TRADE, signal, checks, rejects, acct, news=news_state)

        if info["spread"] > config.RISK["max_spread_points"]:
            rejects.append(fail("spread",
                                f"spread {info['spread']} points is above the "
                                f"{config.RISK['max_spread_points']} limit"))
        else:
            ok("spread", f"{info['spread']} points")

        # Has price already run away from the Analyst's entry?
        live = info["ask"] if direction == "BUY" else info["bid"]
        if entry and atr > 0 and abs(live - entry) > atr * 0.5:
            rejects.append(fail("slippage",
                                f"price moved {abs(live - entry):.5f} from the signal entry "
                                f"({entry}) — the setup has gone"))
        else:
            ok("slippage")

        # -------------------------------------------------------------------
        # 6. Exposure and sizing
        # -------------------------------------------------------------------
        positions = self.client.positions()
        if len(positions) >= config.RISK["max_open_positions"]:
            rejects.append(fail("max_positions",
                                f"{len(positions)} positions open, limit "
                                f"{config.RISK['max_open_positions']}"))
        else:
            ok("max_positions", f"{len(positions)}/{config.RISK['max_open_positions']}")

        same = [p for p in positions if p["symbol"] == symbol]
        if same:
            opposite = [p for p in same if p["side"] != direction]
            if opposite:
                rejects.append(fail("hedge",
                                    f"already {len(opposite)} opposite position(s) on {symbol} "
                                    "— this would hedge, not trade"))
            else:
                rejects.append(fail("stacking",
                                    f"already {len(same)} position(s) on {symbol} in the same "
                                    "direction — no pyramiding in this system"))
        else:
            ok("existing_exposure", "flat on this symbol")

        risk_pct = min(config.RISK["risk_per_trade"], config.RISK["risk_per_trade_max"])
        lot, money = self.size_position(symbol, entry, sl, balance, risk_pct, info)

        if lot <= 0:
            rejects.append(fail("lot_size", "computed lot size is zero — stop too wide "
                                            "for this balance at the configured risk"))
        else:
            ok("lot_size", f"{lot} lots risking ${money:.2f} ({risk_pct:.1%})")

        open_risk = self.open_risk_fraction(balance)
        projected = open_risk + (money / balance if balance else 1)
        if projected > config.RISK["max_total_open_risk"]:
            rejects.append(fail("total_open_risk",
                                f"total open risk would be {projected:.1%}, ceiling is "
                                f"{config.RISK['max_total_open_risk']:.0%}"))
        else:
            ok("total_open_risk", f"{open_risk:.1%} now, {projected:.1%} after")

        # Margin sanity
        if acct["free_margin"] < balance * 0.2:
            rejects.append(fail("free_margin",
                                f"free margin ${acct['free_margin']:.2f} is under 20% of balance"))
        else:
            ok("free_margin", f"${acct['free_margin']:.2f}")

        if rejects:
            return self._verdict(REJECT_TRADE, signal, checks, rejects, acct,
                                 news=news_state, lot=lot, money=money)

        # -------------------------------------------------------------------
        # 7. Approved
        # -------------------------------------------------------------------
        verdict = EXECUTE_BUY if direction == "BUY" else EXECUTE_SELL
        return self._verdict(verdict, signal, checks,
                             [f"All {len(checks)} checks passed."], acct,
                             news=news_state, lot=lot, money=money)

    # =======================================================================
    #  EXECUTE
    # =======================================================================

    def execute(self, decision: dict) -> dict:
        """Place the order. Only ever called with an EXECUTE_* verdict."""
        if decision["verdict"] not in (EXECUTE_BUY, EXECUTE_SELL):
            return {"ok": False, "sent": False, "note": "verdict is not an execute"}

        s = decision["signal"]
        side = "BUY" if decision["verdict"] == EXECUTE_BUY else "SELL"

        if config.DRY_RUN:
            log.info("DRY RUN — would send %s %s %s lots SL %s TP %s",
                     side, s["symbol"], decision["lot"], s["sl"], s["tp"])
            return {"ok": True, "sent": False, "dry_run": True,
                    "note": "DRY_RUN is on. Nothing was sent to MT5.",
                    "would_send": {"symbol": s["symbol"], "side": side,
                                   "lot": decision["lot"], "sl": s["sl"], "tp": s["tp"]}}

        try:
            res = self.client.send_market_order(
                symbol=s["symbol"], side=side, volume=decision["lot"],
                sl=s["sl"], tp=s["tp"], magic=config.MAGIC,
                comment=f"BVNL2AI {s['confidence']:.0f}%")
            log.info("order result: %s", res)
            return {"sent": True, **res}
        except Exception as e:
            log.error("order failed: %s", e)
            return {"ok": False, "sent": False, "error": str(e)}

    # =======================================================================
    #  MATHS
    # =======================================================================

    @staticmethod
    def value_per_price_unit(info: dict) -> float:
        """What one full unit of price movement is worth, per 1.00 lot."""
        ts, tv = info.get("tick_size"), info.get("tick_value")
        if ts and tv:
            return tv / ts
        return info.get("contract_size", 100000.0)

    def size_position(self, symbol, entry, sl, balance, risk_pct, info=None):
        """
        Lot size so that entry -> SL costs exactly risk_pct of balance.
        Rounded DOWN to the broker's step, so the risk is never exceeded.
        """
        if entry is None or sl is None:
            return 0.0, 0.0
        info = info or self.client.symbol_info(symbol)
        dist = abs(entry - sl)
        if dist <= 0:
            return 0.0, 0.0

        money = balance * risk_pct
        per_unit = self.value_per_price_unit(info)
        raw = money / (dist * per_unit)

        step = info.get("volume_step", 0.01) or 0.01
        lot = int(raw / step) * step          # round DOWN, never up
        lot = round(lot, 8)

        vmin, vmax = info.get("volume_min", 0.01), info.get("volume_max", 100.0)
        if lot < vmin:
            # The smallest lot the broker allows would risk more than configured.
            min_risk = vmin * dist * per_unit
            if min_risk > balance * config.RISK["risk_per_trade_max"]:
                return 0.0, 0.0               # refuse rather than over-risk
            lot = vmin
        lot = min(lot, vmax)

        actual = lot * dist * per_unit
        return round(lot, 2), actual

    def open_risk_fraction(self, balance) -> float:
        """Money at risk across open positions, as a fraction of balance."""
        if not balance:
            return 1.0
        total = 0.0
        for p in self.client.positions():
            if not p.get("sl"):
                total += balance * config.RISK["risk_per_trade_max"]
                continue
            try:
                info = self.client.symbol_info(p["symbol"])
            except Exception:
                total += balance * config.RISK["risk_per_trade"]
                continue
            dist = abs(p["price_open"] - p["sl"])
            total += dist * self.value_per_price_unit(info) * p["volume"]
        return total / balance

    def day_stats(self) -> dict:
        """Today's realised P/L, trade count and losing streak."""
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0,
                                                   microsecond=0)
        realised, trades, results = 0.0, 0, []
        try:
            for d in self.client.deals_since(start):
                entry_type = d.get("entry", 1)
                if entry_type == 0:            # DEAL_ENTRY_IN — an opening
                    trades += 1
                    continue
                pl = (d.get("profit", 0.0) + d.get("commission", 0.0)
                      + d.get("swap", 0.0))
                realised += pl
                results.append(pl)
        except Exception as e:
            log.warning("deal history unavailable: %s", e)

        streak = 0
        for pl in reversed(results):
            if pl < 0:
                streak += 1
            else:
                break

        floating = sum(p.get("profit", 0.0) for p in self.client.positions())
        balance = self.client.account()["balance"]
        base = balance - realised or balance          # rough start-of-day balance

        return {"realised": round(realised, 2), "floating": round(floating, 2),
                "trades": trades, "consecutive_losses": streak,
                "pl_pct": ((realised + floating) / base) if base else 0.0}

    # =======================================================================
    #  RESULT SHAPE
    # =======================================================================

    def _verdict(self, verdict, signal, checks, reasons, acct,
                 news=None, lot=0.0, money=0.0):
        out = {
            "ai": "AI2_TRADE_COMMANDER",
            "time": datetime.now(timezone.utc).isoformat(),
            "symbol": signal.get("symbol"),
            "verdict": verdict,
            "reasons": [r for r in reasons if r],
            "checks": checks,
            "checks_passed": sum(1 for c in checks if c["pass"]),
            "checks_failed": sum(1 for c in checks if not c["pass"]),
            "lot": round(lot, 2),
            "money_at_risk": round(money, 2),
            "risk_pct": round(money / acct["balance"] * 100, 3) if acct["balance"] else 0,
            "account": {"balance": acct["balance"], "equity": acct["equity"],
                        "free_margin": acct["free_margin"]},
            "news_status": (news or {}).get("status", "UNKNOWN"),
            "dry_run": config.DRY_RUN,
            "signal": signal,
        }
        if self.llm and self.llm.enabled and verdict in (REJECT_TRADE, HIGH_RISK):
            try:
                out["commentary"] = self.llm.ask(
                    "A trading risk gate rejected a trade. Reasons:\n"
                    + "\n".join(f"- {r}" for r in out["reasons"])
                    + "\n\nIn two sentences, tell the trader plainly what to fix "
                      "or whether to simply stand down today.", max_tokens=180).strip()
            except Exception:
                pass
        return out
