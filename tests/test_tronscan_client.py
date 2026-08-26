from decimal import Decimal
from unittest.mock import Mock, patch

from tronscan_client import TronScanClient, USDT_CONTRACT, parse_tron_transfer


def response(payload):
    result = Mock()
    result.status_code = 200
    result.raise_for_status.return_value = None
    result.json.return_value = payload
    return result


def test_tronscan_uses_api_key_header_and_transfer_filters():
    client = TronScanClient("tron-key", min_request_interval=0)
    client.session.get = Mock(return_value=response({"token_transfers": []}))
    rows, capped = client.transfers("Mint", "TZero", 1000, 2000)

    assert rows == [] and not capped
    call = client.session.get.call_args.kwargs
    assert call["headers"]["TRON-PRO-API-KEY"] == "tron-key"
    assert call["params"]["fromAddress"] == "TZero"
    assert call["params"]["contract_address"] == USDT_CONTRACT


def test_parse_tron_transfer_applies_decimals():
    row = {
        "quant": "1500000",
        "block_ts": 1_700_000_000_000,
        "block": 123,
        "from_address": "TFrom",
        "to_address": "TTo",
        "transaction_id": "abc",
        "tokenInfo": {"tokenDecimal": 6},
    }
    parsed = parse_tron_transfer(row, "Mint", 4)
    assert Decimal(str(parsed["amount"])) == Decimal("1.5")
    assert parsed["log_index"] == 4


def test_tron_treasury_balance_applies_decimals():
    client = TronScanClient("tron-key", min_request_interval=0)
    client.session.get = Mock(
        return_value=response(
            {
                "code": 200,
                "data": [
                    {
                        "tokenId": USDT_CONTRACT,
                        "balance": "2500000",
                        "tokenDecimal": 6,
                    }
                ],
            }
        )
    )
    assert client.treasury_balance() == Decimal("2.5")


def test_tronscan_rate_limit_honors_retry_after():
    limited = response({"message": "rate limited"})
    limited.status_code = 429
    limited.headers = {"Retry-After": "2"}
    success = response({"token_transfers": []})
    success.headers = {}
    client = TronScanClient("tron-key", min_request_interval=0)
    client.session.get = Mock(side_effect=[limited, success])

    with patch("tronscan_client.time.sleep") as sleep:
        payload = client._get("token_trc20/transfers")
    assert payload == {"token_transfers": []}
    sleep.assert_called_once_with(2.0)
