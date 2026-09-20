#!/usr/bin/env python3
"""
BVNL REAL-TIME FOREX ANALYZER - METATRADER 5 (MT5) / EXNESS LIVE BRIDGE
-------------------------------------------------------------------------
This script connects directly to your active MetaTrader 5 (MT5) terminal on
Windows or VPS, detects your broker's actual symbol names (e.g., XAUUSDm on Exness),
and streams real tick & candle data via WebSocket directly to your BVNL Web Analyzer.

Requirements:
    pip install MetaTrader5 websockets requests

Usage:
    python mt5_bridge.py --server-url wss://<YOUR_APP_URL>/ws/mt5-bridge
    Or local testing:
    python mt5_bridge.py --server-url ws://localhost:3000/ws/mt5-bridge
"""

import sys
import time
import json
import argparse
import asyncio
from datetime import datetime, timezone

try:
    import MetaTrader5 as mt5
except ImportError:
    print("[ERROR] MetaTrader5 package is not installed.")
    print("Run: pip install MetaTrader5 websockets")
    mt5 = None

try:
    import websockets
except ImportError:
    print("[ERROR] websockets package is not installed.")
    print("Run: pip install websockets")
    websockets = None

# Target watchlist of symbols requested by BVNL Analyzer
TARGET_CANONICAL_SYMBOLS = [
    "XAUUSD",
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "USDCHF",
    "USDCAD",
    "AUDUSD",
    "NZDUSD",
    "EURJPY",
    "GBPJPY",
    "EURGBP",
    "NAS100",  # May be USTEC, USTECm, NAS100m, NAS100
    "US30",    # May be US30, US30m, DJ30, WALLSTREET
    "BTCUSD"   # If available on Exness/MT5
]

# Common variations/suffixes used by brokers like Exness, IC Markets, etc.
SYMBOL_ALIASES = {
    "XAUUSD": ["XAUUSD", "XAUUSDm", "XAUUSDc", "XAUUSDk", "GOLD", "GOLDm", "XAUUSD.a"],
    "EURUSD": ["EURUSD", "EURUSDm", "EURUSDc", "EURUSDk", "EURUSD.a"],
    "GBPUSD": ["GBPUSD", "GBPUSDm", "GBPUSDc", "GBPUSDk", "GBPUSD.a"],
    "USDJPY": ["USDJPY", "USDJPYm", "USDJPYc", "USDJPYk", "USDJPY.a"],
    "USDCHF": ["USDCHF", "USDCHFm", "USDCHFc", "USDCHFk", "USDCHF.a"],
    "USDCAD": ["USDCAD", "USDCADm", "USDCADc", "USDCADk", "USDCAD.a"],
    "AUDUSD": ["AUDUSD", "AUDUSDm", "AUDUSDc", "AUDUSDk", "AUDUSD.a"],
    "NZDUSD": ["NZDUSD", "NZDUSDm", "NZDUSDc", "NZDUSDk", "NZDUSD.a"],
    "EURJPY": ["EURJPY", "EURJPYm", "EURJPYc", "EURJPYk", "EURJPY.a"],
    "GBPJPY": ["GBPJPY", "GBPJPYm", "GBPJPYc", "GBPJPYk", "GBPJPY.a"],
    "EURGBP": ["EURGBP", "EURGBPm", "EURGBPc", "EURGBPk", "EURGBP.a"],
    "NAS100": ["NAS100", "NAS100m", "USTEC", "USTECm", "US100", "US100m", "NQ", "NDX"],
    "US30": ["US30", "US30m", "DJ30", "DJ30m", "WALLSTREET", "WS30"],
    "BTCUSD": ["BTCUSD", "BTCUSDm", "BTCUSDc", "BTCUSDk", "BTC/USD"]
}

TIMEFRAME_MAP = {}
if mt5:
    TIMEFRAME_MAP = {
        "M1": mt5.TIMEFRAME_M1,
        "M2": mt5.TIMEFRAME_M2,
        "M3": mt5.TIMEFRAME_M3,
        "M4": mt5.TIMEFRAME_M4,
        "M5": mt5.TIMEFRAME_M5,
        "M6": mt5.TIMEFRAME_M6,
        "M10": mt5.TIMEFRAME_M10,
        "M12": mt5.TIMEFRAME_M12,
        "M15": mt5.TIMEFRAME_M15,
        "M20": mt5.TIMEFRAME_M20,
        "M30": mt5.TIMEFRAME_M30,
        "H1": mt5.TIMEFRAME_H1,
        "H2": mt5.TIMEFRAME_H2,
        "H3": mt5.TIMEFRAME_H3,
        "H4": mt5.TIMEFRAME_H4,
        "H6": mt5.TIMEFRAME_H6,
        "H8": mt5.TIMEFRAME_H8,
        "H12": mt5.TIMEFRAME_H12,
        "D1": mt5.TIMEFRAME_D1,
        "W1": mt5.TIMEFRAME_W1,
        "MN1": mt5.TIMEFRAME_MN1,
    }


class Mt5Bridge:
    def __init__(self, server_url, token="bvnl_live_secret", account_login=None, account_password=None, account_server=None):
        self.server_url = server_url
        self.token = token
        self.account_login = account_login
        self.account_password = account_password
        self.account_server = account_server
        self.mapped_symbols = {}  # Canonical -> Actual MT5 symbol
        self.reverse_mapping = {}  # Actual MT5 symbol -> Canonical
        self.is_connected_to_mt5 = False

    def initialize_mt5(self):
        if not mt5:
            print("[ERROR] MetaTrader5 python library not found.")
            return False

        print("\n=======================================================")
        print("  BVNL REAL-TIME FOREX ANALYZER - MT5 BRIDGE")
        print("=======================================================")

        # Initialize connection to terminal
        if self.account_login and self.account_password and self.account_server:
            initialized = mt5.initialize(
                login=int(self.account_login),
                password=self.account_password,
                server=self.account_server
            )
        else:
            initialized = mt5.initialize()

        if not initialized:
            error_code = mt5.last_error()
            print(f"[ERROR] MT5 initialization failed. Error code: {error_code}")
            print("Tip: Make sure MetaTrader 5 terminal is open and logged into your Exness account.")
            self.is_connected_to_mt5 = False
            return False

        self.is_connected_to_mt5 = True
        version = mt5.version()
        terminal_info = mt5.terminal_info()
        account_info = mt5.account_info()

        print(f"[OK] MT5 Terminal Connected! Version: {version}")
        if terminal_info:
            print(f"[INFO] Terminal: {terminal_info.name} | Connected: {terminal_info.connected}")
        if account_info:
            print(f"[INFO] Broker: {account_info.company}")
            print(f"[INFO] Server: {account_info.server}")
            print(f"[INFO] Account: {account_info.login} ({account_info.currency})")
            print(f"[INFO] Balance: {account_info.balance:.2f} | Equity: {account_info.equity:.2f} | Free Margin: {account_info.margin_free:.2f}")

        # Map actual symbols in the terminal
        self.detect_symbols()
        return True

    def detect_symbols(self):
        print("\n[DETECTING BROKER SYMBOLS]")
        all_symbols = mt5.symbols_get()
        if not all_symbols:
            print("[WARNING] No symbols returned by mt5.symbols_get(). Make sure Market Watch contains symbols.")
            return

        available_symbol_names = {s.name for s in all_symbols}

        for canonical, candidates in SYMBOL_ALIASES.items():
            matched = None
            # Check exact candidates first
            for candidate in candidates:
                if candidate in available_symbol_names:
                    matched = candidate
                    break
            # Fallback search with prefix or substring
            if not matched:
                for s_name in available_symbol_names:
                    if s_name.upper().startswith(canonical):
                        matched = s_name
                        break

            if matched:
                # Enable symbol in market watch
                mt5.symbol_select(matched, True)
                self.mapped_symbols[canonical] = matched
                self.reverse_mapping[matched] = canonical
                print(f"  ✓ {canonical:<8} -> {matched:<12} (Mapped successfully)")
            else:
                print(f"  ✗ {canonical:<8} -> Not found on broker terminal")

        print(f"[INFO] Total Mapped Instruments: {len(self.mapped_symbols)}/{len(TARGET_CANONICAL_SYMBOLS)}\n")

    def get_account_summary(self):
        if not self.is_connected_to_mt5:
            return None
        acc = mt5.account_info()
        if not acc:
            return None
        return {
            "login": acc.login,
            "broker": acc.company,
            "server": acc.server,
            "currency": acc.currency,
            "balance": acc.balance,
            "equity": acc.equity,
            "margin": acc.margin,
            "freeMargin": acc.margin_free,
            "marginLevel": acc.margin_level if acc.margin > 0 else 0,
            "profit": acc.profit,
            "leverage": acc.leverage
        }

    def fetch_all_ticks(self):
        ticks = []
        now_ts = int(time.time() * 1000)
        for canonical, actual in self.mapped_symbols.items():
            tick = mt5.symbol_info_tick(actual)
            info = mt5.symbol_info(actual)
            if tick:
                # Calculate spread in points or pips
                point = info.point if info else 0.00001
                spread_points = int(round((tick.ask - tick.bid) / point)) if point > 0 else int(tick.ask - tick.bid)
                
                ticks.append({
                    "symbol": canonical,
                    "actualSymbol": actual,
                    "bid": tick.bid,
                    "ask": tick.ask,
                    "spread": spread_points,
                    "last": tick.last if tick.last > 0 else (tick.bid + tick.ask) / 2.0,
                    "volume": tick.volume,
                    "timestamp": tick.time_msc if hasattr(tick, 'time_msc') and tick.time_msc else (tick.time * 1000 if tick.time else now_ts),
                    "digits": info.digits if info else 5,
                    "point": point
                })
        return ticks

    def fetch_candles(self, canonical_symbol, timeframe_str="M15", count=200):
        actual = self.mapped_symbols.get(canonical_symbol)
        if not actual:
            return []

        tf = TIMEFRAME_MAP.get(timeframe_str.upper())
        if tf is None:
            tf = mt5.TIMEFRAME_M15

        rates = mt5.copy_rates_from_pos(actual, tf, 0, count)
        if rates is None or len(rates) == 0:
            return []

        candles = []
        for r in rates:
            candles.append({
                "time": int(r['time'] * 1000),
                "open": float(r['open']),
                "high": float(r['high']),
                "low": float(r['low']),
                "close": float(r['close']),
                "tick_volume": int(r['tick_volume']),
                "spread": int(r['spread']) if 'spread' in r.dtype.names else 0,
                "real_volume": int(r['real_volume']) if 'real_volume' in r.dtype.names else 0
            })
        return candles

    def run_http_sync(self):
        """HTTP-based sync loop: Works with any cloud proxy without WebSocket upgrade restrictions."""
        import urllib.request
        base_url = self.server_url.replace("ws://", "http://").replace("wss://", "https://").replace("/ws/mt5-bridge", "").rstrip("/")
        handshake_url = f"{base_url}/api/bridge/handshake"
        push_url = f"{base_url}/api/bridge/push"

        print(f"[BRIDGE] Initializing HTTP streaming mode to: {base_url}")
        
        while not self.is_connected_to_mt5:
            if not self.initialize_mt5():
                print("[BRIDGE] Retrying MT5 connection in 5 seconds...")
                time.sleep(5)

        # Send Handshake
        handshake_payload = json.dumps({
            "token": self.token,
            "account": self.get_account_summary(),
            "mappedSymbols": self.mapped_symbols
        }).encode('utf-8')

        try:
            req = urllib.request.Request(handshake_url, data=handshake_payload, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                print(f"[OK] Handshake successful with BVNL Analyzer! HTTP Streaming active.\n")
        except Exception as e:
            print(f"[WARN] Initial handshake failed: {e}. Will retry during push...")

        tick_count = 0
        last_report = time.time()

        while True:
            try:
                ticks = self.fetch_all_ticks()
                acc = self.get_account_summary()
                payload = json.dumps({
                    "timestamp": int(time.time() * 1000),
                    "token": self.token,
                    "ticks": ticks,
                    "account": acc
                }).encode('utf-8')

                req = urllib.request.Request(push_url, data=payload, headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    pass

                tick_count += len(ticks)
                if time.time() - last_report > 10:
                    print(f"[LIVE STREAMING] Pushed {tick_count} live ticks across {len(ticks)} symbols. Status: ONLINE ✓")
                    tick_count = 0
                    last_report = time.time()

            except Exception as e:
                print(f"[WARN] Push error: {e}. Retrying in 2s...")
                time.sleep(2)

            time.sleep(0.5)

    async def run_bridge(self):
        while True:
            if not self.is_connected_to_mt5:
                if not self.initialize_mt5():
                    print("[BRIDGE] Retrying MT5 connection in 5 seconds...")
                    await asyncio.sleep(5)
                    continue

            print(f"[BRIDGE] Connecting to BVNL Analyzer WebSocket: {self.server_url} ...")
            try:
                # websockets 14+ / 17+ uses additional_headers, older versions used extra_headers
                connect_kwargs = {
                    "ping_interval": 20,
                    "ping_timeout": 20,
                }
                # Inspect or try compatible header passing
                try:
                    import inspect
                    sig = inspect.signature(websockets.connect)
                    if "additional_headers" in sig.parameters:
                        connect_kwargs["additional_headers"] = {"X-Bridge-Token": self.token}
                    elif "extra_headers" in sig.parameters:
                        connect_kwargs["extra_headers"] = {"X-Bridge-Token": self.token}
                except Exception:
                    pass

                async with websockets.connect(self.server_url, **connect_kwargs) as ws:
                    print(f"[OK] Connected to BVNL Analyzer! Live streaming active.\n")

                    # Send handshake message with account & mapped symbols
                    handshake = {
                        "type": "BRIDGE_HANDSHAKE",
                        "timestamp": int(time.time() * 1000),
                        "token": self.token,
                        "account": self.get_account_summary(),
                        "mappedSymbols": self.mapped_symbols
                    }
                    await ws.send(json.dumps(handshake))

                    # Start streaming loop and incoming request listener
                    async def tick_streamer():
                        tick_counter = 0
                        last_report = time.time()
                        while True:
                            ticks = self.fetch_all_ticks()
                            acc = self.get_account_summary()
                            payload = {
                                "type": "TICK_UPDATE",
                                "timestamp": int(time.time() * 1000),
                                "ticks": ticks,
                                "account": acc
                            }
                            await ws.send(json.dumps(payload))
                            tick_counter += len(ticks)
                            
                            # Log heartbeat every 10 seconds
                            if time.time() - last_report > 10:
                                print(f"[LIVE STREAMING] Sent {tick_counter} tick updates across {len(ticks)} symbols. Status: OK")
                                tick_counter = 0
                                last_report = time.time()
                            
                            await asyncio.sleep(0.3)  # 300ms stream rate

                    async def message_listener():
                        async for msg in ws:
                            try:
                                data = json.loads(msg)
                                req_type = data.get("type")
                                req_id = data.get("requestId")

                                if req_type == "GET_CANDLES":
                                    symbol = data.get("symbol", "XAUUSD")
                                    timeframe = data.get("timeframe", "M15")
                                    count = data.get("count", 250)
                                    candles = self.fetch_candles(symbol, timeframe, count)
                                    resp = {
                                        "type": "CANDLES_RESPONSE",
                                        "requestId": req_id,
                                        "symbol": symbol,
                                        "timeframe": timeframe,
                                        "candles": candles,
                                        "timestamp": int(time.time() * 1000)
                                    }
                                    await ws.send(json.dumps(resp))
                                    print(f"[QUERY] Served {len(candles)} {timeframe} candles for {symbol}")
                                elif req_type == "PING":
                                    await ws.send(json.dumps({"type": "PONG", "timestamp": int(time.time() * 1000)}))
                            except Exception as e:
                                print(f"[ERROR] Handling message: {e}")

                    # Run streamer and listener concurrently
                    await asyncio.gather(tick_streamer(), message_listener())

            except websockets.exceptions.ConnectionClosed as e:
                print(f"[WARN] Connection to BVNL Web Analyzer closed: {e}. Reconnecting in 3s...")
            except Exception as e:
                print(f"[WARN] Bridge WebSocket error: {e}. Reconnecting in 3s...")

            await asyncio.sleep(3)


def main():
    parser = argparse.ArgumentParser(description="BVNL MT5 Real-Time Live Bridge")
    parser.add_argument("--server-url", default="ws://localhost:3000/ws/mt5-bridge", help="BVNL Web Analyzer WebSocket URL (e.g. wss://<app-id>.run.app/ws/mt5-bridge)")
    parser.add_argument("--token", default="bvnl_live_secret", help="Authentication token for bridge")
    parser.add_argument("--login", default=None, help="MT5 account login number")
    parser.add_argument("--password", default=None, help="MT5 account password")
    parser.add_argument("--server", default=None, help="MT5 broker server name (e.g. Exness-Real19)")
    parser.add_argument("--mode", default="ws", choices=["ws", "http"], help="Connection mode: 'ws' (WebSocket) or 'http' (HTTP Sync)")

    args = parser.parse_args()

    bridge = Mt5Bridge(
        server_url=args.server_url,
        token=args.token,
        account_login=args.login,
        account_password=args.password,
        account_server=args.server
    )

    try:
        if args.mode == "http" or args.server_url.startswith("http://") or args.server_url.startswith("https://"):
            bridge.run_http_sync()
        else:
            asyncio.run(bridge.run_bridge())
    except KeyboardInterrupt:
        print("\n[INFO] MT5 Bridge stopped by user.")
        if mt5:
            mt5.shutdown()
        sys.exit(0)


if __name__ == "__main__":
    main()
