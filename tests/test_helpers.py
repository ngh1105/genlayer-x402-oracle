# tests/test_helpers.py — unit tests for x402_oracle pure helpers.
#
# These tests import the contract through the genlayer stub installed in
# conftest.py, then exercise ONLY the deterministic, network-free helpers.
# Consensus / web.render / payment signing are out of scope here.

import json

import pytest

import x402_oracle as oracle


# ---------------------------------------------------------------------------
# host normalization / extraction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Example.COM", "example.com"),
        ("  example.com  ", "example.com"),
        ("https://example.com/path?q=1", "example.com"),
        ("http://API.Premium-Data.example/v1/x", "api.premium-data.example"),
        ("example.com/a/b/c", "example.com"),
    ],
)
def test_normalize_host(raw, expected):
    assert oracle._normalize_host(raw) == expected


def test_host_of_matches_normalize_host():
    url = "https://API.Example.com/markets/eth"
    assert oracle._host_of(url) == "api.example.com"


# ---------------------------------------------------------------------------
# JSON coercion: valid json -> parsed; junk -> trimmed raw string
# ---------------------------------------------------------------------------

def test_coerce_json_parses_object():
    assert oracle._coerce_json('{"price": 42}') == {"price": 42}


def test_coerce_json_parses_scalar():
    assert oracle._coerce_json("123") == 123


def test_coerce_json_falls_back_to_trimmed_string():
    assert oracle._coerce_json("  not json  ") == "not json"


# ---------------------------------------------------------------------------
# response adapters: tolerate both attribute-style and dict-style responses
# ---------------------------------------------------------------------------

class _AttrResp:
    def __init__(self, status, text, headers):
        self.status = status
        self.text = text
        self.headers = headers


def test_status_of_attr_and_dict():
    assert oracle._status_of(_AttrResp(200, "ok", {})) == 200
    assert oracle._status_of({"status": 402, "body": "", "headers": {}}) == 402


def test_body_of_attr_and_dict():
    assert oracle._body_of(_AttrResp(200, "hello", {})) == "hello"
    assert oracle._body_of({"status": 200, "body": "world", "headers": {}}) == "world"


def test_headers_of_defaults_to_empty_dict():
    assert oracle._headers_of({"status": 200, "body": ""}) == {}
    assert oracle._headers_of(_AttrResp(200, "", {"a": "b"})) == {"a": "b"}


# ---------------------------------------------------------------------------
# 402 parsing: pick first acceptable payment option; raise if none
# ---------------------------------------------------------------------------

def _resp_402(accepts):
    return {"status": 402, "body": json.dumps({"accepts": accepts}), "headers": {}}


def test_parse_402_picks_first_option():
    req = oracle._parse_402(
        _resp_402(
            [
                {
                    "scheme": "exact",
                    "chainId": 8453,
                    "payTo": "0xMerchant",
                    "asset": "USDC",
                    "maxAmountRequired": 50_000,
                    "nonce": "n1",
                    "expiry": 111,
                    "resource": "/v1/x",
                }
            ]
        )
    )
    assert req["pay_to"] == "0xMerchant"
    assert req["max_amount_required_atomic"] == 50_000
    assert req["chain_id"] == 8453
    assert req["scheme"] == "exact"


def test_parse_402_raises_without_options():
    with pytest.raises(Exception):
        oracle._parse_402(_resp_402([]))


def test_payment_ref_reads_either_header_casing():
    assert (
        oracle._payment_ref_of({"headers": {"X-PAYMENT-RESPONSE": "ref-1"}}) == "ref-1"
    )
    assert (
        oracle._payment_ref_of({"headers": {"x-payment-response": "ref-2"}}) == "ref-2"
    )


# ---------------------------------------------------------------------------
# payment signing is now an off-chain relayer boundary (no in-contract stub):
# the deterministic payment-intent builder must bind the replay-sensitive
# fields so the relayer signs exactly the authorized payment.
# ---------------------------------------------------------------------------

def test_idem_key_binds_query_chain_and_nonce():
    k = oracle._idem_key(7, 8453, "n-abc")
    assert k == "q7:c8453:n-abc"
    # different query / chain / nonce -> different key (exactly-once binding)
    assert oracle._idem_key(8, 8453, "n-abc") != k
    assert oracle._idem_key(7, 1, "n-abc") != k
    assert oracle._idem_key(7, 8453, "n-xyz") != k
