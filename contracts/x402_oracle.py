# x402_oracle.py
# =============================================================================
# x402 Paywalled-Data Oracle — GenLayer Intelligent Contract (skeleton)
# =============================================================================
#
# PROBLEM THIS SOLVES
# -------------------
# An Intelligent Oracle on GenLayer can read the open web, but premium data
# usually sits behind a paywall guarded by a *private API key*. A transparent
# blockchain is a terrible place to store that key — every validator (and the
# whole world) would see it. GenLayer's own writing calls this out as a hard
# limitation for oracles.
#
# THIS CONTRACT'S APPROACH
# ------------------------
# Don't store a key at all. Instead, pay-per-request using the x402 protocol
# (HTTP 402 "Payment Required", Coinbase spec). When the oracle fetches a
# premium URL, the server answers `402` with payment instructions; the oracle
# signs a stablecoin micro-payment (settled on Base, an EVM L2), retries with
# proof of payment, and receives the content. Then it runs an LLM extraction
# and validators reach consensus via an equivalence principle. Final state is
# settled on GenLayer.
#
#   contract.request_data() ──> queued query
#   contract.resolve()      ──> [web.render -> 402 -> pay -> render -> LLM]
#                               under gl.eq_principle consensus
#
# IMPORTANT
# ---------
# This is an ILLUSTRATIVE skeleton, not a deployed contract. GenLayer's exact
# Python/GenVM surface evolves; where a call is representative rather than
# guaranteed, it is marked `# ASSUMPTION:` and the payment-signing step is
# stubbed with a clear `# TODO:`.
# =============================================================================

from __future__ import annotations

# GenLayer GenVM SDK. The `gl` namespace exposes:
#   gl.public.write / gl.public.view  -> method decorators
#   gl.nondet.web.render(...)         -> non-deterministic web fetch
#   gl.nondet.exec_prompt(...)        -> non-deterministic LLM call
#   gl.eq_principle.*                 -> consensus / equivalence principles
#   gl.message                        -> tx context (sender, value, ...)
#   gl.storage helpers / typed fields -> persistent contract state
from genlayer import *  # noqa: F401,F403  (GenLayer convention)

import json
import typing


# -----------------------------------------------------------------------------
# Configuration constants
# -----------------------------------------------------------------------------

# Payment is denominated in a stablecoin on Base (EVM L2). USDC has 6 decimals,
# so 1 USDC == 1_000_000 atomic units. The oracle will NEVER pay more than the
# per-query ceiling supplied by the requester, capped again by this global max.
GLOBAL_MAX_PAYMENT_ATOMIC: typing.Final[int] = 1_000_000  # 1.00 USDC hard cap

# The chain id used for the payment rail (Base mainnet = 8453; Base Sepolia =
# 84532). Settlement of contract STATE is on GenLayer; only the micro-payment
# rides Base. Kept here purely for reference / event tagging.
PAYMENT_CHAIN_ID: typing.Final[int] = 8453  # Base mainnet

# Status values stored per query.
STATUS_PENDING: typing.Final[str] = "PENDING"
STATUS_RESOLVED: typing.Final[str] = "RESOLVED"
STATUS_REJECTED: typing.Final[str] = "REJECTED"


# -----------------------------------------------------------------------------
# Plain data records (stored in contract state)
# -----------------------------------------------------------------------------

@allow_storage  # ASSUMPTION: marks a dataclass as storage-serializable
@dataclass
class Query:
    """A single oracle request and its lifecycle state."""
    id: u256
    requester: Address
    url: str
    prompt: str
    max_payment_atomic: u256  # requester's ceiling, <= GLOBAL_MAX_PAYMENT_ATOMIC
    status: str               # one of STATUS_*
    extracted: str            # LLM-extracted answer (JSON string), "" until done
    paid_atomic: u256         # actual amount paid (0 if free / unresolved)
    payment_tx_ref: str       # Base settlement reference (illustrative)


# -----------------------------------------------------------------------------
# The Intelligent Contract
# -----------------------------------------------------------------------------

class X402Oracle(gl.Contract):
    """
    A paywalled-data oracle that pays per request via x402 instead of holding
    an API key on-chain.

    State
    -----
    owner            : admin who manages the domain whitelist
    whitelist        : set of trusted premium hostnames the oracle may pay
    next_id          : monotonic counter for query ids
    queries          : id -> Query record
    """

    # ---- Persistent storage fields (typed; GenVM serializes these) ----------
    owner: Address
    # ASSUMPTION: TreeMap / DynArray are GenLayer's storage-native collections.
    whitelist: TreeMap[str, bool]
    next_id: u256
    queries: TreeMap[u256, Query]

    # -------------------------------------------------------------------------
    # Constructor
    # -------------------------------------------------------------------------
    def __init__(self, initial_whitelist: list[str]):
        """Deploy with an initial set of trusted premium hostnames."""
        self.owner = gl.message.sender_address
        self.next_id = u256(0)
        for host in initial_whitelist:
            self.whitelist[_normalize_host(host)] = True

    # -------------------------------------------------------------------------
    # Admin: domain whitelist management
    # -------------------------------------------------------------------------
    @gl.public.write
    def add_domain(self, host: str) -> None:
        """Whitelist a premium hostname. Owner only."""
        self._only_owner()
        self.whitelist[_normalize_host(host)] = True

    @gl.public.write
    def remove_domain(self, host: str) -> None:
        """Remove a hostname from the whitelist. Owner only."""
        self._only_owner()
        h = _normalize_host(host)
        if h in self.whitelist:
            del self.whitelist[h]

    @gl.public.view
    def is_whitelisted(self, host: str) -> bool:
        """True if `host` is a trusted premium source."""
        return self.whitelist.get(_normalize_host(host), False)

    # -------------------------------------------------------------------------
    # Step 1: submit a query (cheap, deterministic, state-changing)
    # -------------------------------------------------------------------------
    @gl.public.write
    def request_data(self, url: str, prompt: str, max_payment_atomic: int) -> int:
        """
        Register a new oracle query and return its id.

        This method is fully DETERMINISTIC: it does no web/LLM work. It only
        validates inputs and persists a PENDING record. The expensive,
        non-deterministic resolution happens in `resolve()`.
        """
        host = _host_of(url)
        if not self.whitelist.get(host, False):
            # Reject unknown sources up front: we only pay trusted paywalls.
            raise Exception(f"domain not whitelisted: {host}")

        ceiling = min(int(max_payment_atomic), GLOBAL_MAX_PAYMENT_ATOMIC)
        if ceiling <= 0:
            raise Exception("max_payment_atomic must be > 0")

        qid = self.next_id
        self.queries[qid] = Query(
            id=qid,
            requester=gl.message.sender_address,
            url=url,
            prompt=prompt,
            max_payment_atomic=u256(ceiling),
            status=STATUS_PENDING,
            extracted="",
            paid_atomic=u256(0),
            payment_tx_ref="",
        )
        self.next_id = u256(int(qid) + 1)
        return int(qid)

    # -------------------------------------------------------------------------
    # Read path: fetch a stored result (free, deterministic view)
    # -------------------------------------------------------------------------
    @gl.public.view
    def get_result(self, query_id: int) -> dict:
        """Return the current state of a query as a plain dict."""
        q = self._must_get(query_id)
        return {
            "status": q.status,
            "url": q.url,
            "extracted": q.extracted,
            "paidAtomic": str(int(q.paid_atomic)),
            "paymentTxRef": q.payment_tx_ref,
        }

    # -------------------------------------------------------------------------
    # Step 2: resolve a query (EXPENSIVE, NON-DETERMINISTIC, under consensus)
    # -------------------------------------------------------------------------
    @gl.public.write
    def resolve(self, query_id: int) -> None:
        """
        Perform the paid fetch + LLM extraction for a PENDING query and store
        the consensus result.

        Everything that is non-deterministic (web fetch, micro-payment, LLM)
        runs inside `gl.eq_principle.*`. Each validator independently executes
        the leader's logic and the equivalence principle decides whether their
        outputs agree closely enough to accept.
        """
        q = self._must_get(query_id)
        if q.status != STATUS_PENDING:
            raise Exception(f"query {query_id} is not PENDING (is {q.status})")

        host = _host_of(q.url)
        if not self.whitelist.get(host, False):
            q.status = STATUS_REJECTED
            self.queries[q.id] = q
            return

        url = q.url
        prompt = q.prompt
        ceiling = int(q.max_payment_atomic)

        # ---------------------------------------------------------------------
        # Non-deterministic block. The closure does:
        #   (a) x402-aware fetch (may sign a micro-payment),
        #   (b) LLM extraction of the requested fields.
        # Its return value is what validators compare under the eq principle.
        # ---------------------------------------------------------------------
        def _fetch_and_extract() -> str:
            fetched = _x402_fetch(url, ceiling)
            content = fetched["content"]
            # LLM extraction step. Ask the model to return STRICT JSON so the
            # equivalence principle compares structured values, not prose.
            extraction = gl.nondet.exec_prompt(
                f"{prompt}\n\n"
                f"Source content follows between <DATA> tags. Respond with "
                f"ONLY the JSON described above, no commentary.\n"
                f"<DATA>\n{content}\n</DATA>"
            )
            # Pack the extraction together with payment metadata so the stored
            # result carries an audit trail. We keep it as a JSON string.
            return json.dumps({
                "extracted": _coerce_json(extraction),
                "paid_atomic": fetched["paid_atomic"],
                "payment_tx_ref": fetched["payment_tx_ref"],
            }, sort_keys=True)

        # ---------------------------------------------------------------------
        # Equivalence principle choice (see README for full rationale):
        # We use a NON-COMPARATIVE / prompt-based principle. The fetched
        # paywalled content can vary slightly per validator (timestamps,
        # whitespace, field ordering), so byte-equality would never pass.
        # Instead we let an LLM judge whether two validators' extracted JSON
        # answers are SEMANTICALLY EQUIVALENT within tolerance.
        # ---------------------------------------------------------------------
        result_json = gl.eq_principle.prompt_non_comparative(
            _fetch_and_extract,
            task=(
                "Two validators each fetched a premium data source and "
                "extracted an answer as JSON. Decide if the extracted answers "
                "are equivalent."
            ),
            criteria=(
                "Numeric fields must match within 0.5%. String/enum fields "
                "must match exactly (case-insensitive). Ignore differences in "
                "timestamps, key ordering, and whitespace. The payment amount "
                "must not exceed the agreed ceiling."
            ),
        )

        # Persist the consensus result.
        parsed = json.loads(result_json)
        q.extracted = json.dumps(parsed["extracted"], sort_keys=True)
        q.paid_atomic = u256(int(parsed["paid_atomic"]))
        q.payment_tx_ref = str(parsed["payment_tx_ref"])
        q.status = STATUS_RESOLVED
        self.queries[q.id] = q

    # -------------------------------------------------------------------------
    # Internal helpers (deterministic guards)
    # -------------------------------------------------------------------------
    def _only_owner(self) -> None:
        if gl.message.sender_address != self.owner:
            raise Exception("owner only")

    def _must_get(self, query_id: int) -> Query:
        qid = u256(int(query_id))
        if qid not in self.queries:
            raise Exception(f"unknown query id: {query_id}")
        return self.queries[qid]


# -----------------------------------------------------------------------------
# x402-aware fetch (module-level, NON-DETERMINISTIC — only call inside an
# equivalence-principle block).
# -----------------------------------------------------------------------------

def _x402_fetch(url: str, ceiling_atomic: int) -> dict:
    """
    Fetch a premium URL using the x402 "Payment Required" flow.

    Flow (Coinbase x402 spec):
      1. GET url. If server returns 200, we're done (free / already entitled).
      2. If server returns HTTP 402, parse the payment requirements it returns
         (the `accepts` array: scheme, network, payTo address, asset, amount,
         resource, nonce, expiry...).
      3. Validate the demanded amount <= our ceiling and the network/asset are
         what we expect (USDC on Base). Reject fake/over-priced 402s.
      4. Sign an EIP-3009 `transferWithAuthorization` (or the scheme the server
         advertises) authorizing the micro-payment to `payTo` on Base.
      5. Re-request with the `X-PAYMENT` header carrying the signed payload.
      6. Server verifies + settles on Base, returns 200 + content and an
         `X-PAYMENT-RESPONSE` header with the settlement reference.

    Returns: { content: str, paid_atomic: int, payment_tx_ref: str }
    """
    # --- attempt 1: unpaid request ------------------------------------------
    resp = gl.nondet.web.render(url, mode="text", return_status=True)  # ASSUMPTION
    status = _status_of(resp)

    if status == 200:
        return {
            "content": _body_of(resp),
            "paid_atomic": 0,
            "payment_tx_ref": "",
        }

    if status != 402:
        raise Exception(f"unexpected status {status} fetching premium url")

    # --- parse + validate the 402 payment requirements ----------------------
    requirements = _parse_402(resp)
    amount = int(requirements["max_amount_required_atomic"])
    if amount > ceiling_atomic:
        # Defends against an overpayment / price-gouging server.
        raise Exception(f"402 demands {amount} > ceiling {ceiling_atomic}")
    if int(requirements.get("chain_id", PAYMENT_CHAIN_ID)) != PAYMENT_CHAIN_ID:
        raise Exception("402 payment network mismatch (expected Base)")

    # --- sign the micro-payment ---------------------------------------------
    # TODO(payment): THIS is the only place a key is used, and it lives OFF the
    # GenLayer chain. Production options:
    #   (a) GenLayer-native signing primitive that derives a payment key from
    #       validator-held secret shares (no single validator sees the key); or
    #   (b) an account-abstraction / session-key delegated to this contract on
    #       Base, with the spend cap == ceiling enforced on-chain by Base; or
    #   (c) a trusted co-processor that returns only the signed X-PAYMENT blob.
    # The signed payload MUST bind: payTo, amount, asset, network, nonce, and
    # expiry from `requirements` so it cannot be replayed or redirected.
    x_payment_header = _sign_x402_payment(requirements, amount)  # TODO: implement

    # --- attempt 2: paid request --------------------------------------------
    paid_resp = gl.nondet.web.render(  # ASSUMPTION: headers kwarg supported
        url,
        mode="text",
        return_status=True,
        headers={"X-PAYMENT": x_payment_header},
    )
    paid_status = _status_of(paid_resp)
    if paid_status != 200:
        raise Exception(f"payment did not unlock content (status {paid_status})")

    return {
        "content": _body_of(paid_resp),
        "paid_atomic": amount,
        "payment_tx_ref": _payment_ref_of(paid_resp),
    }


def _sign_x402_payment(requirements: dict, amount: int) -> str:
    """
    STUB. Produce the signed `X-PAYMENT` header value for the x402 retry.

    TODO(payment): implement real signing using one of the strategies noted in
    `_x402_fetch`. Must NOT embed a plaintext private key in contract storage
    or source. Returns a base64 JSON payload per the x402 spec.
    """
    raise NotImplementedError(
        "x402 payment signing is intentionally stubbed in this scaffold"
    )


# -----------------------------------------------------------------------------
# Small parsing / coercion helpers (deterministic, pure).
# -----------------------------------------------------------------------------

def _normalize_host(host: str) -> str:
    """Lowercase + strip a hostname (no scheme, no path)."""
    h = host.strip().lower()
    if "://" in h:
        h = h.split("://", 1)[1]
    return h.split("/", 1)[0]


def _host_of(url: str) -> str:
    """Extract a normalized hostname from a full URL."""
    return _normalize_host(url)


def _coerce_json(text: str):
    """Best-effort parse of an LLM response into a JSON value."""
    try:
        return json.loads(text)
    except Exception:
        # Fall back to returning the raw string so resolution never hard-fails
        # on a model that added stray prose. The eq principle still compares.
        return text.strip()


# --- thin adapters over whatever gl.nondet.web.render returns ----------------
# ASSUMPTION: render() returns an object/dict exposing status, body, headers.
# Centralized here so the shape assumption lives in one place.

def _status_of(resp) -> int:
    return int(getattr(resp, "status", None) or resp["status"])


def _body_of(resp) -> str:
    return getattr(resp, "text", None) or resp.get("body", "")


def _headers_of(resp) -> dict:
    return getattr(resp, "headers", None) or resp.get("headers", {}) or {}


def _parse_402(resp) -> dict:
    """
    Turn a 402 response into a normalized payment-requirements dict.
    The x402 spec returns a JSON body with an `accepts` array; we take the
    first acceptable option.
    """
    body = _body_of(resp)
    data = json.loads(body) if body else {}
    accepts = data.get("accepts") or []
    if not accepts:
        raise Exception("402 response had no payment options")
    opt = accepts[0]
    return {
        "scheme": opt.get("scheme", "exact"),
        "chain_id": opt.get("chainId", PAYMENT_CHAIN_ID),
        "pay_to": opt["payTo"],
        "asset": opt.get("asset", "USDC"),
        "max_amount_required_atomic": opt["maxAmountRequired"],
        "nonce": opt.get("nonce", ""),
        "expiry": opt.get("expiry", 0),
        "resource": opt.get("resource", ""),
    }


def _payment_ref_of(resp) -> str:
    """Pull the Base settlement reference from the X-PAYMENT-RESPONSE header."""
    headers = _headers_of(resp)
    return str(headers.get("X-PAYMENT-RESPONSE", "") or headers.get("x-payment-response", ""))
