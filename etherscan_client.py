"""Small Etherscan V2 client for ERC-20 mint and burn events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import time
from typing import Any

import requests


API_URL = "https://api.etherscan.io/v2/api"
CHAIN_ID = "1"
TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)
ZERO_ADDRESS = "0x" + "0" * 40


class EtherscanError(RuntimeError):
    """Raised when Etherscan returns an error or an unexpected response."""


def hex_int(value: Any) -> int:
    """Parse an Ethereum hex quantity, treating an empty `0x` payload as zero."""
    if value in (None, "", "0x", "0X"):
        return 0
    return int(value, 16)


@dataclass(frozen=True)
class Token:
    symbol: str
    name: str
    address: str
    decimals: int


TOKENS: dict[str, Token] = {
    "USDT": Token("USDT", "Tether USD", "0xdAC17F958D2ee523a2206206994597C13D831ec7", 6),
    "USAT": Token("USAT", "USA₮", "0x07041776f5007ACa2A54844F50503a18A72A8b68", 6),
    "XAUT": Token("XAUT", "Tether Gold", "0x68749665FF8D2d112Fa859AA293F07A622782F38", 6),
    "PAXG": Token("PAXG", "PAX Gold", "0x45804880De22913dAFE09f4980848ECE6EcbAf78", 18),
}


class EtherscanClient:
    def __init__(
        self,
        api_key: str,
        timeout: int = 30,
        min_request_interval: float = 0.4,
        max_retries: int = 4,
    ) -> None:
        if not api_key:
            raise ValueError("An Etherscan API key is required.")
        self.api_key = api_key
        self.timeout = timeout
        self.min_request_interval = min_request_interval
        self.max_retries = max_retries
        self._last_request_started = 0.0
        self.session = requests.Session()

    def _get(self, **params: Any) -> Any:
        for attempt in range(self.max_retries + 1):
            elapsed = time.monotonic() - self._last_request_started
            if elapsed < self.min_request_interval:
                time.sleep(self.min_request_interval - elapsed)
            self._last_request_started = time.monotonic()

            response = self.session.get(
                API_URL,
                params={"chainid": CHAIN_ID, "apikey": self.api_key, **params},
                timeout=self.timeout,
            )
            try:
                response.raise_for_status()
            except requests.HTTPError:
                if response.status_code == 429 and attempt < self.max_retries:
                    time.sleep(2**attempt)
                    continue
                raise

            payload = response.json()
            if payload.get("status") == "1":
                return payload.get("result")
            # Proxy endpoints use JSON-RPC responses rather than the usual
            # Etherscan status/message envelope.
            if payload.get("jsonrpc") and "result" in payload and not payload.get("error"):
                return payload["result"]

            result = payload.get("result")
            message = payload.get("message", "Unknown Etherscan error")
            error_text = f"{message}: {result}"
            if (
                ("rate limit" in error_text.lower() or "max calls" in error_text.lower())
                and attempt < self.max_retries
            ):
                time.sleep(2**attempt)
                continue

            # Empty log searches are reported with status=0, not as an API failure.
            if isinstance(result, list) and not result:
                return []
            if isinstance(result, str) and "No records found" in result:
                return []
            raise EtherscanError(error_text)

        raise EtherscanError("Etherscan rate limit remained active after retries.")

    def block_at(self, timestamp: int, closest: str) -> int:
        result = self._get(
            module="block",
            action="getblocknobytime",
            timestamp=str(timestamp),
            closest=closest,
        )
        return int(result)

    def total_supply_at(self, token: Token, block_number: int) -> Decimal:
        """Read ERC-20 totalSupply() at a specific historical block."""
        result = self._get(
            module="proxy",
            action="eth_call",
            to=token.address,
            data="0x18160ddd",
            tag=hex(block_number),
        )
        if not isinstance(result, str) or not result.startswith("0x"):
            raise EtherscanError(f"Unexpected totalSupply response for {token.symbol}: {result}")
        raw_supply = hex_int(result)
        return Decimal(raw_supply) / (Decimal(10) ** token.decimals)

    def token_balance_at(self, token: Token, holder: str, block_number: int) -> Decimal:
        """Read ERC-20 balanceOf(holder) at a specific historical block."""
        encoded_holder = holder.lower().removeprefix("0x").rjust(64, "0")
        result = self._get(
            module="proxy",
            action="eth_call",
            to=token.address,
            data="0x70a08231" + encoded_holder,
            tag=hex(block_number),
        )
        if not isinstance(result, str) or not result.startswith("0x"):
            raise EtherscanError(f"Unexpected balanceOf response for {token.symbol}: {result}")
        raw_balance = hex_int(result)
        return Decimal(raw_balance) / (Decimal(10) ** token.decimals)

    def token_balance_latest(self, token: Token, holder: str) -> Decimal:
        """Read the latest ERC-20 balance through Etherscan's tokenbalance endpoint."""
        result = self._get(
            module="account",
            action="tokenbalance",
            contractaddress=token.address,
            address=holder,
            tag="latest",
        )
        return Decimal(result) / (Decimal(10) ** token.decimals)

    def contract_creation(self, token: Token) -> tuple[int, datetime]:
        result = self._get(
            module="contract",
            action="getcontractcreation",
            contractaddresses=token.address,
        )
        if not isinstance(result, list) or not result:
            raise EtherscanError(f"No contract creation data returned for {token.symbol}")
        creation = result[0]
        return (
            int(creation["blockNumber"]),
            datetime.fromtimestamp(int(creation["timestamp"]), tz=timezone.utc),
        )

    def event_logs(
        self,
        token: Token,
        event_type: str,
        from_block: int,
        to_block: int,
        max_pages: int = 10,
        page_size: int = 1000,
        participant_address: str = ZERO_ADDRESS,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return matching Transfer logs and whether the result hit the safety cap."""
        if event_type not in {"Mint", "Burn"}:
            raise ValueError("event_type must be Mint or Burn")

        topic_position = "topic1" if event_type == "Mint" else "topic2"
        operator = "topic0_1_opr" if event_type == "Mint" else "topic0_2_opr"
        participant_topic = "0x" + participant_address.lower().removeprefix("0x").rjust(64, "0")
        logs: list[dict[str, Any]] = []
        capped = False
        for page in range(1, max_pages + 1):
            batch = self._get(
                module="logs",
                action="getLogs",
                fromBlock=str(from_block),
                toBlock=str(to_block),
                address=token.address,
                topic0=TRANSFER_TOPIC,
                **{topic_position: participant_topic, operator: "and"},
                page=str(page),
                offset=str(page_size),
            )
            logs.extend(batch)
            if len(batch) < page_size:
                break
        else:
            capped = True
        return logs, capped


def topic_address(topic: str) -> str:
    return "0x" + topic[-40:]


def parse_log(log: dict[str, Any], token: Token, event_type: str) -> dict[str, Any]:
    """Convert one raw Transfer log into a dashboard-friendly record."""
    raw_amount = hex_int(log.get("data"))
    amount = Decimal(raw_amount) / (Decimal(10) ** token.decimals)
    timestamp = hex_int(log["timeStamp"])
    topics = log["topics"]
    return {
        "timestamp": datetime.fromtimestamp(timestamp, tz=timezone.utc),
        "token": token.symbol,
        "type": event_type,
        "amount": float(amount),
        "from": topic_address(topics[1]),
        "to": topic_address(topics[2]),
        "tx_hash": log["transactionHash"],
        "block": hex_int(log["blockNumber"]),
        "log_index": hex_int(log.get("logIndex")),
    }
