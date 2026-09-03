from __future__ import annotations

import os
import requests

HOST = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
DATA_API = os.getenv("DATA_API_HOST", "https://data-api.polymarket.com")
GEOBLOCK_URL = os.getenv("GEOBLOCK_URL", "https://polymarket.com/api/geoblock")


class LiveCLOB:
    """Fail-closed adapter around Polymarket CLOB V2."""

    def __init__(self):
        try:
            from py_clob_client_v2 import (
                ApiCreds, AssetType, BalanceAllowanceParams, ClobClient,
                OrderArgs, OrderType, PartialCreateOrderOptions, Side,
            )
        except Exception as exc:
            raise RuntimeError("py_clob_client_v2==1.1.0 is required for LIVE_TRADING") from exc

        self.AssetType = AssetType
        self.BalanceAllowanceParams = BalanceAllowanceParams
        self.OrderArgs = OrderArgs
        self.OrderType = OrderType
        self.PartialCreateOrderOptions = PartialCreateOrderOptions
        self.Side = Side

        key = os.getenv("PRIVATE_KEY")
        sig_raw = os.getenv("SIGNATURE_TYPE", "")
        funder = os.getenv("FUNDER_ADDRESS")
        api_key = os.getenv("CLOB_API_KEY") or os.getenv("POLY_API_KEY")
        secret = os.getenv("CLOB_SECRET") or os.getenv("POLY_API_SECRET")
        phrase = os.getenv("CLOB_PASS_PHRASE") or os.getenv("POLY_API_PASSPHRASE")
        if not key:
            raise RuntimeError("PRIVATE_KEY is required for LIVE_TRADING")
        if not sig_raw or not funder:
            raise RuntimeError("SIGNATURE_TYPE and FUNDER_ADDRESS are required for LIVE_TRADING")
        try:
            sig = int(sig_raw)
        except ValueError as exc:
            raise RuntimeError("SIGNATURE_TYPE must be 0, 1, 2, or 3") from exc
        if sig not in (0, 1, 2, 3):
            raise RuntimeError("SIGNATURE_TYPE must be 0, 1, 2, or 3")
        if not (api_key and secret and phrase):
            raise RuntimeError("CLOB API credentials are required for LIVE_TRADING")

        creds = ApiCreds(api_key=api_key, api_secret=secret, api_passphrase=phrase)
        self.client = ClobClient(
            host=HOST,
            chain_id=137,
            key=key,
            creds=creds,
            signature_type=sig,
            funder=funder,
            use_server_time=True,
            retry_on_error=False,
        )
        self.signature_type = sig
        self.funder = funder
        self._heartbeat_id = ""

    @staticmethod
    def _usd_amount(value):
        """Normalize CLOB collateral amounts returned in micro-units or USD."""
        try:
            x = float(value)
        except (TypeError, ValueError):
            return 0.0
        # CLOB collateral/allowance values are normally integer micro-USDC.
        # Tiny test doubles and already-normalized values are kept as USD.
        return x / 1_000_000.0 if abs(x) >= 100_000.0 else x

    def preflight(self):
        health = self.client.get_ok()
        if not health:
            raise RuntimeError(f"CLOB health check failed: {health!r}")
        try:
            geo = requests.get(GEOBLOCK_URL, timeout=10).json()
        except Exception as exc:
            raise RuntimeError(f"geoblock check failed: {exc}") from exc
        if bool(geo.get("blocked")):
            raise RuntimeError(f"Polymarket geoblock: country={geo.get('country')} region={geo.get('region')}")

        signer = self.client.get_address()
        if not signer:
            raise RuntimeError("CLOB signer address unavailable")

        try:
            info = self.client.get_balance_allowance(
                self.BalanceAllowanceParams(asset_type=self.AssetType.COLLATERAL)
            )
            balance_raw = info.get("balance", 0) if isinstance(info, dict) else 0
            balance = self._usd_amount(balance_raw)
            allowance_raw = info.get("allowance") if isinstance(info, dict) else None
            if allowance_raw is None and isinstance(info, dict):
                allowances = info.get("allowances")
                if isinstance(allowances, dict):
                    allowance_raw = max(allowances.values(), default=0)
                else:
                    allowance_raw = allowances
            allowance = self._usd_amount(allowance_raw or 0)
        except Exception as exc:
            raise RuntimeError(f"balance/allowance check failed: {exc}") from exc

        min_balance = float(os.getenv("LIVE_PREFLIGHT_MIN_BALANCE", "100"))
        if balance + 1e-9 < min_balance:
            raise RuntimeError(f"insufficient CLOB collateral balance: ${balance:.6f}; need at least ${min_balance:.2f}")
        if allowance + 1e-9 < min_balance:
            raise RuntimeError(f"insufficient CLOB collateral allowance: ${allowance:.6f}; need at least ${min_balance:.2f}")

        # A live canary must start from a clean position state. The Data API
        # exposes current positions by user/profile address; for this setup the
        # funder/deposit wallet is the account whose positions must be empty.
        try:
            pos_resp = requests.get(
                f"{DATA_API}/positions",
                params={"user": self.funder, "sizeThreshold": 0.01, "limit": 500},
                timeout=10,
            )
            pos_resp.raise_for_status()
            positions = pos_resp.json()
        except Exception as exc:
            raise RuntimeError(f"position preflight failed: {exc}") from exc
        if isinstance(positions, list) and positions:
            active = [p for p in positions if float(p.get("size", 0) or 0) > 0.01]
            if active:
                raise RuntimeError(
                    "LIVE PREFLIGHT HALT: funder wallet already has open positions; "
                    "reconcile/close them before starting the $100 deployment"
                )

        return {
            "health": health, "blocked": False, "country": geo.get("country"),
            "region": geo.get("region"), "signer": signer, "funder": self.funder,
            "signature_type": self.signature_type, "balance": balance, "allowance": allowance,
        }

    def market_info(self, condition):
        info = self.client.get_clob_market_info(condition)
        if not isinstance(info, dict):
            raise RuntimeError(f"invalid CLOB market info for {condition}")
        if info.get("mos") is None or info.get("mts") is None:
            raise RuntimeError(f"CLOB market info missing minimum order/tick size for {condition}")
        return info

    def tick_size(self, token_id):
        return float(self.client.get_tick_size(str(token_id)))

    def minimum_order(self, token_id, condition, price):
        """Return the exchange-derived minimum dollar cost at this price."""
        info = self.market_info(condition)
        min_shares = float(info["mos"])
        p = float(price)
        if not 0.0 < p < 1.0:
            raise ValueError("invalid price")
        return p * min_shares, min_shares

    def order_options(self, token_id, condition):
        info = self.market_info(condition)
        tick = str(self.client.get_tick_size(str(token_id)))
        neg = bool(self.client.get_neg_risk(str(token_id)))
        return self.PartialCreateOrderOptions(tick_size=tick, neg_risk=neg), float(info["mos"]), tick

    def adaptive_buy(self, token_id, limit_price, shares, condition):
        """Submit a marketable FAK BUY capped at limit_price.

        FAK is the CLOB execution primitive used by the adaptive executor: it
        takes available liquidity up to the caller's price ceiling and cancels
        the remainder instead of resting a stale quote.
        """
        price = float(limit_price)
        size = float(shares)
        if not 0 < price < 1:
            raise ValueError("invalid adaptive limit price")
        if size <= 0:
            raise ValueError("invalid adaptive share size")
        options, min_size, _ = self.order_options(token_id, condition)
        if size + 1e-9 < min_size:
            raise ValueError(f"order size {size:.8f} is below market minimum {min_size:.8f}")
        max_order = float(os.getenv("MAX_SINGLE_ORDER", "5"))
        if price * size > max_order + 1e-9:
            size = max_order / price
        if size + 1e-9 < min_size:
            raise ValueError("adaptive order cannot satisfy market minimum within MAX_SINGLE_ORDER")
        response = self.client.create_and_post_order(
            order_args=self.OrderArgs(token_id=str(token_id), price=price, size=size, side=self.Side.BUY),
            options=options,
            order_type=self.OrderType.FAK,
            post_only=False,
        )
        if not isinstance(response, dict):
            raise RuntimeError(f"invalid CLOB adaptive response: {response!r}")
        if response.get("success") is False or response.get("errorMsg"):
            raise RuntimeError(f"CLOB rejected adaptive FAK: {response.get('errorMsg') or response!r}")
        order_id = response.get("orderID") or response.get("orderId") or response.get("id")
        if not order_id:
            raise RuntimeError(f"CLOB adaptive call returned no order id: {response!r}")
        return response

    def post_only_buy(self, token_id, price, notional, condition):
        price = float(price)
        notional = float(notional)
        if not 0 < price < 1:
            raise ValueError("invalid price")
        max_order = float(os.getenv("MAX_SINGLE_ORDER", "5"))
        if notional <= 0 or notional > max_order + 1e-9 or notional > 15.0 + 1e-9:
            raise ValueError("invalid live notional")
        options, min_size, _ = self.order_options(token_id, condition)
        size = notional / price
        if size + 1e-9 < min_size:
            raise ValueError(f"order size {size:.8f} is below market minimum {min_size:.8f}")
        args = self.OrderArgs(token_id=str(token_id), price=price, size=size, side=self.Side.BUY)
        response = self.client.create_and_post_order(
            order_args=args,
            options=options,
            order_type=self.OrderType.GTC,
            post_only=True,
        )
        if not isinstance(response, dict):
            raise RuntimeError(f"invalid CLOB order response: {response!r}")
        if response.get("success") is False or response.get("errorMsg"):
            raise RuntimeError(f"CLOB rejected order: {response.get('errorMsg') or response!r}")
        order_id = response.get("orderID") or response.get("orderId") or response.get("id")
        if not order_id:
            raise RuntimeError(f"CLOB accepted call but returned no order id: {response!r}")
        return response

    def get_open_orders(self):
        return self.client.get_open_orders(only_first_page=False)

    def get_order(self, order_id):
        return self.client.get_order(str(order_id))

    def get_trades(self):
        return self.client.get_trades(only_first_page=False)

    def reconcile_orders(self, order_ids):
        states = {}
        for oid in order_ids:
            states[str(oid)] = self.get_order(str(oid))
        return states

    def cancel_market_orders(self, condition):
        try:
            from py_clob_client_v2 import OrderMarketCancelParams
        except ImportError:
            from py_clob_client_v2.clob_types import OrderMarketCancelParams
        return self.client.cancel_market_orders(
            OrderMarketCancelParams(market=str(condition))
        )

    def cancel_all(self):
        return self.client.cancel_all()

    def heartbeat(self, heartbeat_id=""):
        if heartbeat_id:
            self._heartbeat_id = str(heartbeat_id)
        response = self.client.post_heartbeat(getattr(self, "_heartbeat_id", ""))
        if not isinstance(response, dict):
            raise RuntimeError(f"invalid heartbeat response: {response!r}")
        next_id = (
            response.get("heartbeat_id")
            or response.get("heartbeatId")
            or response.get("id")
        )
        if not next_id:
            raise RuntimeError(f"heartbeat response missing next id: {response!r}")
        self._heartbeat_id = str(next_id)
        return response
