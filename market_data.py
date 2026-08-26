"""Keyless public market-data helpers."""

from __future__ import annotations

import time

import requests

from etherscan_client import Token


COINGECKO_TOKEN_PRICE_URL = "https://api.coingecko.com/api/v3/simple/token_price/ethereum"


class MarketDataError(RuntimeError):
    pass


def coingecko_price_usd(token: Token, timeout: int = 20, max_retries: int = 3) -> float:
    """Fetch a current Ethereum token price from CoinGecko's keyless API."""
    for attempt in range(max_retries + 1):
        response = requests.get(
            COINGECKO_TOKEN_PRICE_URL,
            params={"contract_addresses": token.address.lower(), "vs_currencies": "usd"},
            headers={"accept": "application/json"},
            timeout=timeout,
        )
        if response.status_code == 429 and attempt < max_retries:
            time.sleep(2**attempt)
            continue
        response.raise_for_status()
        payload = response.json()
        quote = payload.get(token.address.lower(), {})
        price = quote.get("usd")
        if price is None or float(price) <= 0:
            raise MarketDataError(f"CoinGecko returned no USD price for {token.symbol}")
        return float(price)
    raise MarketDataError("CoinGecko rate limit remained active after retries")
