"""
Order Executor — Simultaneous dual-leg order placement for Kalshi + Polymarket.

Kalshi:    FIX protocol over TLS (simplefix library)
           Host: fix.elections.kalshi.com  Port: 8228 (no-retransmit)
           Auth: RSA-PSS signature of Logon prehash (same key as REST/WS)

Polymarket: CLOB REST API (py-clob-client library)
            Auth: Ethereum private key (EOA, signature_type=0)

Usage:
    kalshi_fix = KalshiFixClient(key_id, private_key_path)
    kalshi_fix.connect()   # blocks until Logon ack
    poly = PolyOrderClient(eth_private_key)
    executor = OrderExecutor(kalshi_fix, poly)
    executor.execute(opp)  # fire-and-forget
"""

import base64
import csv
import json as _json
import os
import socket
import ssl
import threading
import time
import uuid

import requests
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

try:
    import simplefix
except ImportError:
    simplefix = None  # KalshiFixClient unavailable without: pip install simplefix
import pytz
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ── Constants ────────────────────────────────────────────────────────────────

FIX_HOST       = "fix.elections.kalshi.com"
FIX_PORT       = 8228          # Order Entry, no retransmission
FIX_TARGET     = "KalshiNR"   # TargetCompID for port 8228
FIX_HEARTBEAT  = 30            # seconds

EST_TZ = pytz.timezone("US/Eastern")

DATA_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "live_market_data"
)


# ── ArbitrageOpportunity ─────────────────────────────────────────────────────

@dataclass
class ArbitrageOpportunity:
    leg: str            # "A" or "B"
    kalshi_ticker: str
    kalshi_side: str    # "yes" (BUY_YES) or "no" (SELL_NO)
    kalshi_price: float # ask price to pay in dollars (0.01 – 0.99)
    poly_token_id: str  # token_id of the outcome to buy on Polymarket
    poly_price: float   # ask price to pay in dollars
    contracts: int      # already capped by MAX_CONTRACTS_CAP
    expected_profit: float


# ── KalshiFixClient ──────────────────────────────────────────────────────────

class KalshiFixClient:
    """
    Persistent FIX session with Kalshi for order entry.

    - Connects once at startup (TLS, port 8228)
    - Handles Logon / Heartbeat / sequence numbers
    - Background listener thread handles inbound ExecutionReports
    - CancelOrdersOnDisconnect=Y: open orders auto-cancel if socket drops

    NOTE: SenderCompID must be your FIX API key (UUID format). This may be
    the same as your REST/WS key or a separately provisioned FIX key —
    verify in your Kalshi account settings.
    """

    def __init__(self, key_id: str, private_key_path: str,
                 host: str = FIX_HOST, port: int = FIX_PORT,
                 target_comp_id: str = FIX_TARGET):
        self.sender_comp_id = key_id
        self.target_comp_id = target_comp_id
        self.host = host
        self.port = port

        with open(private_key_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(f.read(), password=None)

        self._sock: Optional[ssl.SSLSocket] = None
        self._seq = 1
        self._seq_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._running = False
        self._connected = threading.Event()

        os.makedirs(DATA_DIR, exist_ok=True)
        session_ts = datetime.now(EST_TZ).strftime("%Y%m%d_%H%M%S")
        fills_path = os.path.join(DATA_DIR, f"kalshi_fills_{session_ts}.csv")
        self._fills_file = open(fills_path, "w", newline="")
        self._fills_writer = csv.writer(self._fills_file)
        self._fills_writer.writerow([
            "timestamp_ms", "clord_id", "order_id", "exec_type", "ord_status",
            "last_qty", "cum_qty", "avg_px", "leaves_qty", "text",
        ])
        self._fills_file.flush()
        print(f"[FIX] Fills log: {fills_path}")

    def connect(self):
        """
        Establish TLS connection and send Logon. Blocks until Logon ack
        received (up to 10 seconds), then returns.
        """
        tls_ctx = ssl.create_default_context()
        raw = socket.create_connection((self.host, self.port), timeout=10)
        self._sock = tls_ctx.wrap_socket(raw, server_hostname=self.host)
        self._running = True

        threading.Thread(target=self._listen_loop, daemon=True, name="fix-listener").start()
        threading.Thread(target=self._heartbeat_loop, daemon=True, name="fix-heartbeat").start()

        self._send_logon()

        if not self._connected.wait(timeout=10):
            raise ConnectionError("Kalshi FIX: no Logon ack within 10s — check key_id and private key")

        print(f"[FIX] Connected to {self.host}:{self.port} (SenderCompID={self.sender_comp_id})")

    def place_order(self, ticker: str, side: str, count: int, price: float) -> str:
        """
        Send a NewOrderSingle (IOC limit order).

        side:  "yes" → FIX Side=1 (BUY_YES)
               "no"  → FIX Side=2 (SELL_NO, i.e. buy NO at `price` dollars)
        price: what you are willing to pay in dollars (0.01 – 0.99);
               converted internally to integer cents.

        Returns clord_id (UUID string) for tracking ExecutionReports.
        Raises if the socket is closed.
        """
        price_cents = round(price * 100)
        clord_id = str(uuid.uuid4())
        fix_side = "1" if side == "yes" else "2"

        msg = self._make_header("D")    # NewOrderSingle
        msg.append_pair(11, clord_id)   # ClOrdID
        msg.append_pair(38, str(count)) # OrderQty
        msg.append_pair(40, "2")        # OrdType=Limit
        msg.append_pair(44, str(price_cents))  # Price in cents (integer)
        msg.append_pair(54, fix_side)   # Side
        msg.append_pair(55, ticker)     # Symbol
        msg.append_pair(59, "3")        # TimeInForce=IOC

        self._send_msg(msg)
        print(f"[FIX] → NewOrderSingle: {ticker} {side} x{count} @ {price_cents}¢  "
              f"clord_id={clord_id[:8]}…")
        return clord_id

    def disconnect(self):
        """Send Logout and close socket."""
        self._running = False
        if self._sock:
            try:
                self._send_logout()
                time.sleep(0.2)
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
        self._fills_file.close()

    # ── private ───────────────────────────────────────────────────────────────

    def _next_seq(self) -> int:
        with self._seq_lock:
            s = self._seq
            self._seq += 1
        return s

    @staticmethod
    def _utc_now() -> str:
        """FIX SendingTime format: YYYYMMDD-HH:MM:SS.mmm (UTC)."""
        return datetime.now(timezone.utc).strftime("%Y%m%d-%H:%M:%S.%f")[:23]

    def _make_header(self, msg_type: str):
        """Build a FixMessage with standard header fields pre-filled."""
        msg = simplefix.FixMessage()
        msg.append_pair(8, "FIXT.1.1")
        msg.append_pair(35, msg_type)
        msg.append_pair(49, self.sender_comp_id)
        msg.append_pair(56, self.target_comp_id)
        msg.append_pair(34, str(self._next_seq()))
        msg.append_pair(52, self._utc_now())
        return msg

    def _sign_logon(self, sending_time: str, seq: int) -> str:
        """
        RSA-PSS sign the Kalshi FIX Logon prehash string.

        PreHashString = SendingTime SOH MsgType SOH MsgSeqNum SOH SenderCompID SOH TargetCompID
        (SOH = \x01)
        """
        soh = "\x01"
        pre_hash = soh.join([
            sending_time,
            "A",            # MsgType for Logon
            str(seq),
            self.sender_comp_id,
            self.target_comp_id,
        ]).encode("utf-8")

        sig = self._private_key.sign(
            pre_hash,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("utf-8")

    def _send_logon(self):
        """
        Build and send FIX Logon (35=A).
        The signing uses the SAME SendingTime and MsgSeqNum as the header.
        """
        seq = self._next_seq()
        sending_time = self._utc_now()
        raw_data = self._sign_logon(sending_time, seq)

        msg = simplefix.FixMessage()
        msg.append_pair(8, "FIXT.1.1")
        msg.append_pair(35, "A")                    # Logon
        msg.append_pair(49, self.sender_comp_id)
        msg.append_pair(56, self.target_comp_id)
        msg.append_pair(34, str(seq))
        msg.append_pair(52, sending_time)           # Must match prehash exactly
        msg.append_pair(98, "0")                    # EncryptMethod=None
        msg.append_pair(108, str(FIX_HEARTBEAT))    # HeartBtInt
        msg.append_pair(95, str(len(raw_data)))     # RawDataLength (must precede 96)
        msg.append_pair(96, raw_data)               # RawData = base64(RSA-PSS sig)
        msg.append_pair(141, "Y")                   # ResetSeqNumFlag
        msg.append_pair(1137, "9")                  # DefaultApplVerID=FIX50SP2
        msg.append_pair(8013, "Y")                  # CancelOrdersOnDisconnect

        self._send_msg(msg)
        print(f"[FIX] Logon sent (seq={seq})…")

    def _send_heartbeat(self, test_req_id: Optional[str] = None):
        msg = self._make_header("0")    # Heartbeat
        if test_req_id:
            msg.append_pair(112, test_req_id)
        self._send_msg(msg)

    def _send_logout(self):
        msg = self._make_header("5")    # Logout
        self._send_msg(msg)

    def _send_msg(self, msg):
        self._send_raw(msg.encode())

    def _send_raw(self, data: bytes):
        with self._send_lock:
            self._sock.sendall(data)

    # ── listener ──────────────────────────────────────────────────────────────

    def _listen_loop(self):
        parser = simplefix.FixParser()
        while self._running:
            try:
                chunk = self._sock.recv(4096)
                if not chunk:
                    print("[FIX] Server closed connection.")
                    break
                parser.append_buffer(chunk)
                while True:
                    fix_msg = parser.get_message()
                    if fix_msg is None:
                        break
                    self._handle_inbound(fix_msg)
            except ssl.SSLError as e:
                if self._running:
                    print(f"[FIX] SSL error: {e}")
                break
            except Exception as e:
                if self._running:
                    print(f"[FIX] Listener error: {e}")
                break

    def _heartbeat_loop(self):
        while self._running:
            time.sleep(FIX_HEARTBEAT)
            if self._running and self._connected.is_set():
                try:
                    self._send_heartbeat()
                except Exception as e:
                    print(f"[FIX] Heartbeat error: {e}")

    def _handle_inbound(self, msg):
        def _s(tag) -> str:
            v = msg.get(tag)
            return v.decode() if isinstance(v, bytes) else (v or "")

        msg_type = _s(35)

        if msg_type == "A":         # Logon ack
            print("[FIX] Logon acknowledged by server.")
            self._connected.set()

        elif msg_type in ("0", "1"):  # Heartbeat / TestRequest
            test_req_id = _s(112)
            # TestRequest requires a Heartbeat response with the same TestReqID
            self._send_heartbeat(test_req_id or None)

        elif msg_type == "8":       # ExecutionReport
            self._log_fill(msg, _s)

        elif msg_type == "3":       # Session-level Reject
            print(f"[FIX] Session Reject: {_s(58)}")

        elif msg_type == "j":       # BusinessMessageReject
            print(f"[FIX] Business Reject: {_s(58)}")

        elif msg_type == "5":       # Logout
            print(f"[FIX] Logout from server: {_s(58)}")
            self._running = False

        else:
            pass  # Ignore other admin messages

    def _log_fill(self, msg, _s):
        exec_type = _s(150)
        clord_id  = _s(11)
        cum_qty   = _s(14)
        avg_px    = _s(6)
        leaves    = _s(151)
        text      = _s(58)

        row = [
            int(time.time() * 1000),
            clord_id, _s(37), exec_type, _s(39),
            _s(32), cum_qty, avg_px, leaves, text,
        ]
        self._fills_writer.writerow(row)
        self._fills_file.flush()

        if exec_type == "F":    # Trade / fill
            print(f"[FIX] ✓ FILL: clord={clord_id[:8]}… cum={cum_qty} avg={avg_px}¢")
        elif exec_type == "8":  # Rejected
            print(f"[FIX] ✗ REJECTED: clord={clord_id[:8]}… reason={text}")
        elif exec_type == "4":  # Canceled (IOC expired)
            if cum_qty and cum_qty != "0":
                print(f"[FIX] ~ PARTIAL+IOC-CANCEL: clord={clord_id[:8]}… filled={cum_qty} canceled={leaves}")
            else:
                print(f"[FIX] ~ IOC UNFILLED: clord={clord_id[:8]}…")
        elif exec_type == "A":  # PendingNew
            pass  # expected acknowledgment, not worth printing
        else:
            print(f"[FIX] ExecReport exec_type={exec_type} clord={clord_id[:8]}… status={_s(39)}")


# ── KalshiRestClient ─────────────────────────────────────────────────────────

class KalshiRestClient:
    """
    Drop-in replacement for KalshiFixClient using the Kalshi REST API.

    Same public interface: connect(), place_order(), disconnect().
    Uses the same RSA-PSS auth as the WebSocket client (live_kalshi.py).
    No persistent connection — each call is a standalone HTTPS request.

    connect() verifies auth via GET /portfolio/balance.
    place_order() sends an IOC limit order via POST /portfolio/orders.
    """

    _BASE_DOMAIN = "https://api.elections.kalshi.com"
    _BASE_PATH   = "/trade-api/v2"

    def __init__(self, key_id: str, private_key_path: str):
        self.key_id = key_id
        with open(private_key_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(f.read(), password=None)

        os.makedirs(DATA_DIR, exist_ok=True)
        session_ts = datetime.now(EST_TZ).strftime("%Y%m%d_%H%M%S")
        orders_path = os.path.join(DATA_DIR, f"kalshi_orders_{session_ts}.csv")
        self._orders_file = open(orders_path, "w", newline="")
        self._orders_writer = csv.writer(self._orders_file)
        self._orders_writer.writerow([
            "timestamp_ms", "order_id", "ticker", "side", "count",
            "price_cents", "status", "filled_count", "remaining_count",
        ])
        self._orders_file.flush()
        print(f"[REST] Orders log: {orders_path}")

    def connect(self):
        """Verify auth via GET /portfolio/balance. Raises on failure."""
        resp = self._get("/portfolio/balance")
        balance = resp.get("balance", 0)
        print(f"[REST] Auth verified. Kalshi balance: ${balance / 100:.2f}")

    def place_order(self, ticker: str, side: str, count: int, price: float) -> str:
        """
        Place an IOC limit order via REST.

        side:  "yes" → buy YES contracts
               "no"  → buy NO contracts
        price: what you are willing to pay in dollars (0.01 – 0.99);
               converted internally to integer cents.

        Returns order_id string (used as clord_id by OrderExecutor).
        Raises on HTTP error.
        """
        price_cents = round(price * 100)
        price_field = "yes_price" if side == "yes" else "no_price"
        body = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "count": count,
            price_field: price_cents,       # yes_price or no_price (1–99 int)
            "time_in_force": "immediate_or_cancel",
        }
        resp = self._post("/portfolio/orders", body)
        order = resp.get("order", {})
        order_id = order.get("order_id", "")
        status = order.get("status", "?")
        filled = order.get("filled_count", 0)
        remaining = order.get("remaining_count", 0)

        self._orders_writer.writerow([
            int(time.time() * 1000), order_id, ticker, side, count,
            price_cents, status, filled, remaining,
        ])
        self._orders_file.flush()

        print(f"[REST] → Order: {ticker} {side} x{count} @ {price_cents}¢  "
              f"order_id={order_id[:8] if order_id else '?'}…  "
              f"status={status}  filled={filled}")
        return order_id

    def disconnect(self):
        self._orders_file.close()

    # ── private ───────────────────────────────────────────────────────────────

    def _auth_headers(self, method: str, path: str) -> dict:
        ts_ms = str(int(time.time() * 1000))
        pre_hash = f"{ts_ms}{method}{path}".encode("utf-8")
        sig = self._private_key.sign(
            pre_hash,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "Content-Type": "application/json",
        }

    def _get(self, endpoint: str) -> dict:
        full_path = self._BASE_PATH + endpoint
        resp = requests.get(
            self._BASE_DOMAIN + full_path,
            headers=self._auth_headers("GET", full_path),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, endpoint: str, body: dict) -> dict:
        full_path = self._BASE_PATH + endpoint
        resp = requests.post(
            self._BASE_DOMAIN + full_path,
            headers=self._auth_headers("POST", full_path),
            data=_json.dumps(body),
            timeout=10,
        )
        if not resp.ok:
            raise requests.HTTPError(
                f"{resp.status_code} {resp.reason}: {resp.text}", response=resp
            )
        return resp.json()


# ── PolyOrderClient ───────────────────────────────────────────────────────────

class PolyOrderClient:
    """
    Polymarket CLOB order placement via py-clob-client.

    py-clob-client is imported lazily inside __init__ so that the rest of
    order_executor.py can be imported (for ArbitrageOpportunity) without
    py-clob-client installed, as long as PolyOrderClient is never instantiated.

    Requires: pip install py-clob-client
    Requires: an EOA Ethereum private key for a funded Polymarket account.
    """

    def __init__(self, private_key: str, chain_id: int = 137):
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY
        except ImportError:
            raise ImportError(
                "py-clob-client is required for Polymarket order placement. "
                "Install with: pip install py-clob-client"
            )

        self._OrderArgs = OrderArgs
        self._OrderType = OrderType
        self._BUY = BUY

        self._client = ClobClient(
            "https://clob.polymarket.com",
            key=private_key,
            chain_id=chain_id,
            signature_type=0,   # EOA wallet
        )
        # Derive L2 API credentials from the private key (one-time call)
        self._client.set_api_creds(self._client.create_or_derive_api_creds())
        print("[Poly] CLOB client initialized.")

    def place_order(self, token_id: str, price: float, size: int) -> dict:
        """
        Place a GTC limit BUY order on Polymarket.

        token_id: outcome token ID (from market_config.json)
        price:    dollars (0.01 – 0.99), the ask price to cross
        size:     integer number of contracts

        Returns {"ok": True, "order_id": "..."} or {"ok": False, "error": "..."}.
        """
        try:
            order_args = self._OrderArgs(
                token_id=token_id,
                price=price,
                size=float(size),
                side=self._BUY,
            )
            signed = self._client.create_order(order_args)
            resp = self._client.post_order(signed, self._OrderType.GTC)
            order_id = resp.get("orderID") or resp.get("order_id")
            return {"ok": True, "order_id": order_id}
        except Exception as e:
            return {"ok": False, "error": str(e)}


# ── OrderExecutor ─────────────────────────────────────────────────────────────

class OrderExecutor:
    """
    Coordinates simultaneous dual-leg order placement.

    execute() is fire-and-forget: it spawns a daemon thread and returns
    immediately so the arbitrage detection loop is never blocked.

    The inner _run() fires both platform orders in parallel, waits up to
    8 seconds, then logs and prints the result.
    """

    def __init__(self, kalshi_fix: KalshiFixClient, poly: PolyOrderClient):
        self._kalshi = kalshi_fix
        self._poly = poly

        os.makedirs(DATA_DIR, exist_ok=True)
        session_ts = datetime.now(EST_TZ).strftime("%Y%m%d_%H%M%S")
        exec_path = os.path.join(DATA_DIR, f"execution_log_{session_ts}.csv")
        self._exec_file = open(exec_path, "w", newline="")
        self._exec_writer = csv.writer(self._exec_file)
        self._exec_writer.writerow([
            "timestamp_ms", "timestamp_est", "leg", "contracts", "expected_profit",
            "kalshi_ticker", "kalshi_side", "kalshi_price", "kalshi_clord_id", "kalshi_send_ok",
            "poly_token_id", "poly_price", "poly_ok", "poly_order_id", "poly_error",
        ])
        self._exec_file.flush()
        print(f"[Exec] Execution log: {exec_path}")

    def execute(self, opp: ArbitrageOpportunity) -> None:
        """Spawn a daemon thread to place both legs. Returns immediately."""
        threading.Thread(
            target=self._run, args=(opp,), daemon=True, name="executor"
        ).start()

    def _run(self, opp: ArbitrageOpportunity):
        results: dict = {}

        def place_kalshi():
            try:
                clord_id = self._kalshi.place_order(
                    opp.kalshi_ticker, opp.kalshi_side,
                    opp.contracts, opp.kalshi_price,
                )
                results["kalshi_clord_id"] = clord_id
                results["kalshi_ok"] = True
            except Exception as e:
                results["kalshi_clord_id"] = ""
                results["kalshi_ok"] = False
                results["kalshi_error"] = str(e)
                print(f"[Exec] Kalshi send error: {e}")

        def place_poly():
            result = self._poly.place_order(
                opp.poly_token_id, opp.poly_price, opp.contracts,
            )
            results["poly"] = result

        t_k = threading.Thread(target=place_kalshi)
        t_p = threading.Thread(target=place_poly)
        t_k.start()
        t_p.start()
        t_k.join(timeout=8)
        t_p.join(timeout=8)

        poly = results.get("poly", {})
        k_ok = results.get("kalshi_ok", False)
        p_ok = poly.get("ok", False)

        ts_ms = int(time.time() * 1000)
        ts_est = datetime.now(EST_TZ).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

        self._exec_writer.writerow([
            ts_ms, ts_est, opp.leg, opp.contracts, opp.expected_profit,
            opp.kalshi_ticker, opp.kalshi_side, opp.kalshi_price,
            results.get("kalshi_clord_id", ""), k_ok,
            opp.poly_token_id, opp.poly_price,
            p_ok, poly.get("order_id", ""), poly.get("error", ""),
        ])
        self._exec_file.flush()

        if k_ok and p_ok:
            print(
                f"[EXECUTION OK] Leg {opp.leg}: {opp.contracts} contracts  "
                f"Kalshi clord={results.get('kalshi_clord_id', '')[:8]}…  "
                f"Poly order={poly.get('order_id', '')}"
            )
        else:
            failures = []
            if not k_ok:
                failures.append(f"Kalshi FAILED: {results.get('kalshi_error', 'send error')}")
            if not p_ok:
                failures.append(f"Poly FAILED: {poly.get('error', 'unknown')}")
            print(f"\n{'!' * 60}")
            print(f"[LEG MISMATCH] Leg {opp.leg}: {' | '.join(failures)}")
            print(f"  Check execution_log CSV for details. Manual intervention may be needed.")
            print(f"{'!' * 60}\n")

    def close(self):
        self._exec_file.close()
