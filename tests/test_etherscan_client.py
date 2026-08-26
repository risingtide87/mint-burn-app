from decimal import Decimal
from unittest.mock import Mock, patch

from etherscan_client import EtherscanClient, TOKENS, hex_int, parse_log, topic_address
from market_data import coingecko_price_usd


def padded(address: str) -> str:
    return "0x" + "0" * 24 + address.removeprefix("0x").lower()


def test_topic_address_extracts_last_twenty_bytes():
    address = "0x1234567890abcdef1234567890abcdef12345678"
    assert topic_address(padded(address)) == address


def test_empty_hex_payload_is_zero():
    assert hex_int("0x") == 0
    assert hex_int(None) == 0


def test_parse_log_applies_token_decimals():
    recipient = "0x1234567890abcdef1234567890abcdef12345678"
    log = {
        "data": hex(1_500_000),
        "timeStamp": hex(1_700_000_000),
        "topics": ["transfer", "0x" + "0" * 64, padded(recipient)],
        "transactionHash": "0xabc",
        "blockNumber": hex(123),
        "logIndex": hex(4),
    }
    result = parse_log(log, TOKENS["USDT"], "Mint")
    assert Decimal(str(result["amount"])) == Decimal("1.5")
    assert result["to"] == recipient
    assert result["block"] == 123


def test_rate_limit_response_is_retried():
    limited = Mock()
    limited.status_code = 200
    limited.raise_for_status.return_value = None
    limited.json.return_value = {
        "status": "0",
        "message": "NOTOK",
        "result": "Max calls per sec rate limit reached (3/sec)",
    }
    success = Mock()
    success.status_code = 200
    success.raise_for_status.return_value = None
    success.json.return_value = {"status": "1", "message": "OK", "result": "123"}

    client = EtherscanClient("test-key", min_request_interval=0)
    client.session.get = Mock(side_effect=[limited, success])
    with patch("etherscan_client.time.sleep") as sleep:
        assert client._get(module="block") == "123"
    assert client.session.get.call_count == 2
    sleep.assert_called_once_with(1)


def test_historical_total_supply_parses_json_rpc_result():
    response = Mock()
    response.status_code = 200
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": hex(2_500_000),
    }
    client = EtherscanClient("test-key", min_request_interval=0)
    client.session.get = Mock(return_value=response)

    assert client.total_supply_at(TOKENS["USDT"], 123) == Decimal("2.5")
    params = client.session.get.call_args.kwargs["params"]
    assert params["data"] == "0x18160ddd"
    assert params["tag"] == hex(123)


def test_contract_creation_returns_block_and_utc_timestamp():
    response = Mock()
    response.status_code = 200
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "status": "1",
        "message": "OK",
        "result": [{"blockNumber": "42", "timestamp": "1700000000"}],
    }
    client = EtherscanClient("test-key", min_request_interval=0)
    client.session.get = Mock(return_value=response)

    block, launched_at = client.contract_creation(TOKENS["USAT"])
    assert block == 42
    assert launched_at.tzinfo is not None


def test_historical_token_balance_encodes_holder():
    response = Mock()
    response.status_code = 200
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": hex(1_250_000),
    }
    client = EtherscanClient("test-key", min_request_interval=0)
    client.session.get = Mock(return_value=response)
    holder = "0x5754284f345afc66a98fbb0a0afe71e0f007b949"

    assert client.token_balance_at(TOKENS["USDT"], holder, 123) == Decimal("1.25")
    params = client.session.get.call_args.kwargs["params"]
    assert params["data"].startswith("0x70a08231")
    assert params["data"].endswith(holder.removeprefix("0x"))


def test_coingecko_keyless_token_price():
    response = Mock()
    response.status_code = 200
    response.raise_for_status.return_value = None
    response.json.return_value = {TOKENS["XAUT"].address.lower(): {"usd": 2650.25}}

    with patch("market_data.requests.get", return_value=response) as get:
        assert coingecko_price_usd(TOKENS["XAUT"]) == 2650.25
    assert "x-cg-pro-api-key" not in get.call_args.kwargs["headers"]
