# tests/test_state_machine.py — exercises the two-phase resolve flow.
#
# These tests construct the real X402Oracle through the genlayer stub
# (conftest.py) and drive its lifecycle:
#
#   request_data -> resolve_authorize -> submit_payment_proof -> resolve_fetch
#
# The stub's `gl.nondet.web.get` runs the contract's non-deterministic closures
# for real (conftest's eq_principle CALLS the leader closure), so we control the
# merchant's HTTP behavior by swapping `gl.nondet.web.get` / `exec_prompt` per
# test. Money/signing is an off-chain relayer boundary, so nothing here moves
# funds; we only assert the on-chain state machine + binding/validation logic.

import json

import pytest

import genlayer
import x402_oracle as oracle

gl = genlayer.gl

OWNER = "0xOwner"
RELAYER = "0xRelayer"
OTHER = "0xMallory"
HOST = "api.example.com"
URL = f"https://{HOST}/v1/price"


# ---------------------------------------------------------------------------
# helpers to script the merchant's responses
# ---------------------------------------------------------------------------

def _resp(status, body="", headers=None):
    return {"status": status, "body": body, "headers": headers or {}}


def _402_body(pay_to="0xMerchant", amount=50_000, chain=8453, asset="USDC",
              nonce="n1", expiry=2_000_000_000):
    return json.dumps({"accepts": [{
        "scheme": "exact",
        "chainId": chain,
        "payTo": pay_to,
        "asset": asset,
        "maxAmountRequired": amount,
        "nonce": nonce,
        "expiry": expiry,
        "resource": "/v1/price",
    }]})


def _set_web(fn):
    gl.nondet.web.get = fn


def _set_prompt(fn):
    gl.nondet.exec_prompt = fn


@pytest.fixture(autouse=True)
def _reset_env():
    """Each test starts as OWNER with clean web/prompt stubs."""
    gl.message.sender_address = OWNER
    _set_web(lambda *a, **k: _resp(200))
    _set_prompt(lambda *a, **k: "")
    yield


def _fresh_contract(whitelist=(HOST,)):
    gl.message.sender_address = OWNER
    c = oracle.X402Oracle(list(whitelist))
    c.set_relayer(RELAYER)
    return c


# ---------------------------------------------------------------------------
# request_data
# ---------------------------------------------------------------------------

def test_request_data_rejects_non_whitelisted_host():
    c = _fresh_contract()
    with pytest.raises(Exception):
        c.request_data("https://evil.example/x", "p", 1000)


def test_request_data_clamps_to_global_max():
    c = _fresh_contract()
    qid = c.request_data(URL, "p", 10**12)  # huge ceiling
    # ceiling clamped to GLOBAL_MAX_PAYMENT_ATOMIC at registration
    assert int(c.queries[qid].max_payment_atomic) == oracle.GLOBAL_MAX_PAYMENT_ATOMIC
    assert c.get_result(qid)["status"] == oracle.STATUS_PENDING


# ---------------------------------------------------------------------------
# PHASE A: resolve_authorize — paywalled happy path + validation
# ---------------------------------------------------------------------------

def test_authorize_paywalled_binds_intent():
    c = _fresh_contract()
    qid = c.request_data(URL, "extract price", 60_000)
    _set_web(lambda *a, **k: _resp(402, _402_body(amount=50_000)))
    c.resolve_authorize(qid)

    q = c.queries[qid]
    assert q.status == oracle.STATUS_AUTHORIZED
    assert q.pay_to == "0xMerchant"
    assert int(q.paid_atomic) == 50_000
    assert int(q.chain_id) == oracle.PAYMENT_CHAIN_ID
    assert q.asset == oracle.EXPECTED_ASSET
    assert q.has_proof is False

    intent = c.get_payment_intent(qid)
    assert intent["payTo"] == "0xMerchant"
    assert intent["amountAtomic"] == "50000"
    assert intent["idemKey"] == oracle._idem_key(qid, oracle.PAYMENT_CHAIN_ID, "n1")


def test_authorize_rejects_over_ceiling():
    c = _fresh_contract()
    qid = c.request_data(URL, "p", 40_000)  # ceiling below demand
    _set_web(lambda *a, **k: _resp(402, _402_body(amount=50_000)))
    with pytest.raises(Exception):
        c.resolve_authorize(qid)


def test_authorize_rejects_wrong_chain():
    c = _fresh_contract()
    qid = c.request_data(URL, "p", 60_000)
    _set_web(lambda *a, **k: _resp(402, _402_body(chain=1)))  # Ethereum, not Base
    with pytest.raises(Exception):
        c.resolve_authorize(qid)


def test_authorize_rejects_wrong_asset():
    c = _fresh_contract()
    qid = c.request_data(URL, "p", 60_000)
    _set_web(lambda *a, **k: _resp(402, _402_body(asset="DAI")))
    with pytest.raises(Exception):
        c.resolve_authorize(qid)


def test_authorize_free_resource_is_ready_without_relayer():
    c = _fresh_contract()
    qid = c.request_data(URL, "p", 60_000)
    _set_web(lambda *a, **k: _resp(200))  # no paywall
    c.resolve_authorize(qid)
    q = c.queries[qid]
    assert q.status == oracle.STATUS_AUTHORIZED
    assert q.has_proof is True
    assert int(q.paid_atomic) == 0


# ---------------------------------------------------------------------------
# relayer callback: submit_payment_proof
# ---------------------------------------------------------------------------

def _authorize_paywalled(c, ceiling=60_000):
    qid = c.request_data(URL, "extract price", ceiling)
    _set_web(lambda *a, **k: _resp(402, _402_body(amount=50_000)))
    c.resolve_authorize(qid)
    return qid


def test_submit_proof_requires_relayer():
    c = _fresh_contract()
    qid = _authorize_paywalled(c)
    gl.message.sender_address = OTHER
    with pytest.raises(Exception):
        c.submit_payment_proof(qid, "base-tx-0x1")


def test_submit_proof_happy_then_double_rejected():
    c = _fresh_contract()
    qid = _authorize_paywalled(c)
    gl.message.sender_address = RELAYER
    c.submit_payment_proof(qid, "base-tx-0x1")
    assert c.queries[qid].has_proof is True
    assert c.queries[qid].payment_tx_ref == "base-tx-0x1"
    # second proof for the same query is rejected (exactly-once)
    with pytest.raises(Exception):
        c.submit_payment_proof(qid, "base-tx-0x2")


def test_submit_proof_rejects_empty_ref():
    c = _fresh_contract()
    qid = _authorize_paywalled(c)
    gl.message.sender_address = RELAYER
    with pytest.raises(Exception):
        c.submit_payment_proof(qid, "")


def test_submit_proof_requires_authorized():
    c = _fresh_contract()
    qid = c.request_data(URL, "p", 60_000)  # still PENDING
    gl.message.sender_address = RELAYER
    with pytest.raises(Exception):
        c.submit_payment_proof(qid, "base-tx-0x1")


# ---------------------------------------------------------------------------
# PHASE B: resolve_fetch
# ---------------------------------------------------------------------------

def test_fetch_before_authorize_rejected():
    c = _fresh_contract()
    qid = c.request_data(URL, "p", 60_000)  # PENDING
    with pytest.raises(Exception):
        c.resolve_fetch(qid)


def test_fetch_before_proof_rejected():
    c = _fresh_contract()
    qid = _authorize_paywalled(c)  # AUTHORIZED but no proof
    with pytest.raises(Exception):
        c.resolve_fetch(qid)


def test_full_paywalled_flow_resolves():
    c = _fresh_contract()
    qid = _authorize_paywalled(c)
    gl.message.sender_address = RELAYER
    c.submit_payment_proof(qid, "base-tx-0x1")

    # entitled fetch returns content; LLM extracts strict JSON
    _set_web(lambda *a, **k: _resp(200, "BTC price is 64000"))
    _set_prompt(lambda *a, **k: '{"price": 64000}')
    gl.message.sender_address = OWNER
    c.resolve_fetch(qid)

    res = c.get_result(qid)
    assert res["status"] == oracle.STATUS_RESOLVED
    assert json.loads(res["extracted"]) == {"price": 64000}


def test_free_resource_full_flow():
    c = _fresh_contract()
    qid = c.request_data(URL, "p", 60_000)
    _set_web(lambda *a, **k: _resp(200))   # free at authorize
    c.resolve_authorize(qid)
    _set_web(lambda *a, **k: _resp(200, "hello world"))
    _set_prompt(lambda *a, **k: '{"ok": true}')
    c.resolve_fetch(qid)
    assert c.get_result(qid)["status"] == oracle.STATUS_RESOLVED


# ---------------------------------------------------------------------------
# whitelist removal -> authorize marks REJECTED (no raise)
# ---------------------------------------------------------------------------

def test_authorize_rejects_dewhitelisted_host():
    c = _fresh_contract()
    qid = c.request_data(URL, "p", 60_000)
    c.remove_domain(HOST)
    c.resolve_authorize(qid)
    assert c.queries[qid].status == oracle.STATUS_REJECTED
