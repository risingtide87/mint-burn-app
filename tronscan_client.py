"""TronScan client for TRC-20 USDT mint, redeem, and treasury activity."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import time
from typing import Any

import requests


API_URL = "https://apilist.tronscanapi.com/api"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
USDT_TREASURY = "TKHuVq1oKVruCGLvqVexFs6dawKv6fQgFs"
TRON_ZERO_ADDRESS = "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"
USDT_DECIMALS = 6


class TronScanError(RuntimeError):
    pass


class TronScanClient:
    def __init__(
        self,
        api_key: str,
        timeout: int = 30,
        min_request_interval: float = 1.0,
        max_retries: int = 5,
    ) -> None:
        if not api_key:
            raise ValueError("A TronScan API key is required.")
        self.api_key = api_key
        self.timeout = timeout
        self.min_request_interval = min_request_interval
        self.max_retries = max_retries
        self._last_request_started = 0.0
        self.session = requests.Session()

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            elapsed = time.monotonic() - self._last_request_started
            if elapsed < self.min_request_interval:
                time.sleep(self.min_request_interval - elapsed)
            self._last_request_started = time.monotonic()

            response = self.session.get(
                f"{API_URL}/{path}",
                params=params,
                headers={"TRON-PRO-API-KEY": self.api_key},
                timeout=self.timeout,
            )
            if response.status_code == 429 and attempt < self.max_retries:
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else 2**attempt
                except ValueError:
                    delay = 2**attempt
                time.sleep(min(max(delay, 1.0), 30.0))
                continue
            if response.status_code == 429:
                raise TronScanError(
                    "TronScan rate limit remained active after retries. "
                    "Wait a few minutes and use Refresh now."
                )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and payload.get("code") not in (None, 0, 200):
                raise TronScanError(payload.get("message", "Unknown TronScan error"))
            return payload
        raise TronScanError("TronScan rate limit remained active after retries")

    def token_details(self) -> tuple[Decimal, datetime]:
        payload = self._get("token_trc20", contract=USDT_CONTRACT, limit=1)
        tokens = payload.get("trc20_tokens", [])
        if not tokens:
            raise TronScanError("No TRC-20 USDT token details returned")
        token = tokens[0]
        supply = Decimal(token["total_supply_with_decimals"]) / Decimal(10**USDT_DECIMALS)
        launched_at = datetime.fromtimestamp(int(token["date_created"]), tz=timezone.utc)
        return supply, launched_at

    def treasury_balance(self) -> Decimal:
        payload = self._get(
            "account/tokens",
            address=USDT_TREASURY,
            token=USDT_CONTRACT,
            show=1,
            hidden=1,
            start=0,
            limit=20,
        )
        for token in payload.get("data", []):
            if token.get("tokenId") == USDT_CONTRACT:
                decimals = int(token.get("tokenDecimal", USDT_DECIMALS))
                return Decimal(token["balance"]) / Decimal(10**decimals)
        return Decimal(0)

    def transfers(
        self,
        event_type: str,
        participant: str,
        start_ms: int,
        end_ms: int,
        max_records: int = 10_000,
    ) -> tuple[list[dict[str, Any]], bool]:
        if event_type not in {"Mint", "Burn"}:
            raise ValueError("event_type must be Mint or Burn")
        address_filter = "fromAddress" if event_type == "Mint" else "toAddress"
        rows: list[dict[str, Any]] = []
        page_size = 50
        for start in range(0, max_records, page_size):
            payload = self._get(
                "token_trc20/transfers",
                contract_address=USDT_CONTRACT,
                **{address_filter: participant},
                start_timestamp=start_ms,
                end_timestamp=end_ms,
                confirm=0,
                start=start,
                limit=page_size,
            )
            batch = payload.get("token_transfers", [])
            rows.extend(batch)
            if len(batch) < page_size:
                return rows, False
        return rows, True


def parse_tron_transfer(row: dict[str, Any], event_type: str, log_index: int) -> dict[str, Any]:
    decimals = int(row.get("tokenInfo", {}).get("tokenDecimal", USDT_DECIMALS))
    amount = Decimal(row["quant"]) / Decimal(10**decimals)
    return {
        "timestamp": datetime.fromtimestamp(int(row["block_ts"]) / 1000, tz=timezone.utc),
        "token": "USDT",
        "type": event_type,
        "amount": float(amount),
        "from": row["from_address"],
        "to": row["to_address"],
        "tx_hash": row["transaction_id"],
        "block": int(row["block"]),
        "log_index": int(row.get("event_index", log_index)),
    }
