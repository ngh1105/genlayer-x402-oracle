# v0.2.16
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

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
#   contract.request_data()     ──> queued query (PENDING)
#   contract.resolve_authorize()──> probe 402 + bind reqs (AUTHORIZED)
#   [off-chain relayer settles ONE Base payment, submit_payment_proof()]
#   contract.resolve_fetch()    ──> [web.get -> 200 -> LLM] under consensus
#                                   (RESOLVED)
#
# IMPORTANT
# ---------
# Real X-PAYMENT signing lives OFF-CHAIN by design: GenVM has no primitive to
# hold/sign with a secp256k1 key, and value-moving effects must happen once
# post-consensus (not per validator). So the contract binds + validates the
# payment and exposes it via get_payment_intent(); an off-chain relayer signs
# one Base AA-session-key payment (spend cap enforced on-chain) and reports it
# via submit_payment_proof(). Where a GenVM call shape is representative rather
# than guaranteed it is marked `# ASSUMPTION:`.
# =============================================================================

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
from dataclasses import dataclass


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

# Status values stored per query. The lifecycle is a strict state machine:
#
#   PENDING ──resolve_authorize()──▶ AUTHORIZED ──resolve_fetch()──▶ RESOLVED
#       │                               │
#       └──────────(reject)─────────────┴────────────▶ REJECTED
#
# AUTHORIZED means: validators reached consensus on the 402 payment
# requirements, the contract bound them to this queryId and clamped them to the
# ceiling/cap, and the (off-chain) relayer is now invited to settle EXACTLY this
# query at most once. Only after a settlement proof is recorded can the paid
# content be fetched in resolve_fetch().
STATUS_PENDING: typing.Final[str] = "PENDING"
STATUS_AUTHORIZED: typing.Final[str] = "AUTHORIZED"
STATUS_RESOLVED: typing.Final[str] = "RESOLVED"
STATUS_REJECTED: typing.Final[str] = "REJECTED"

# Asset / network we are willing to settle on. The 402 requirements must match
# these exactly or the query is rejected (anti-redirect to a foreign chain/asset).
EXPECTED_ASSET: typing.Final[str] = "USDC"


# -----------------------------------------------------------------------------
# Plain data records (stored in contract state)
# -----------------------------------------------------------------------------

@allow_storage  # ASSUMPTION: marks a dataclass as storage-serializable
@dataclass
class Query:
    """A single oracle request and its full lifecycle state.

    The fields after `status` form the *payment authorization* that
    `resolve_authorize()` binds (post-consensus) and the relayer settles. They
    are empty/zero until a query reaches AUTHORIZED.
    """
    id: u256
    requester: Address
    url: str
    prompt: str
    max_payment_atomic: u256  # requester's ceiling, <= GLOBAL_MAX_PAYMENT_ATOMIC
    status: str               # one of STATUS_*
    extracted: str            # LLM-extracted answer (JSON string), "" until done
    paid_atomic: u256         # actual amount authorized/paid (0 until AUTHORIZED)
    payment_tx_ref: str       # Base settlement reference (set by relayer proof)

    # ---- payment authorization (bound once, post-consensus, in authorize) --
    pay_to: str               # merchant address from the 402 (anti-redirect)
    asset: str                # settlement asset, must == EXPECTED_ASSET
    chain_id: u256            # settlement chain, must == PAYMENT_CHAIN_ID
    server_nonce: str         # x402 server-supplied nonce (relayer signs it)
    expiry: u256              # x402 authorization expiry (unix seconds)
    idem_key: str             # H(queryId|contract|chainId): exactly-once key
    has_proof: bool           # True once relayer recorded a settlement proof


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
    # The off-chain relayer allowed to submit Base settlement proofs. It holds
    # the Base AA session key OFF-CHAIN; the contract never sees the key.
    relayer: Address
    # ASSUMPTION: TreeMap / DynArray are GenLayer's storage-native collections.
    whitelist: TreeMap[str, bool]
    next_id: u256
    queries: TreeMap[u256, Query]

    # -------------------------------------------------------------------------
    # Constructor
    # -------------------------------------------------------------------------
    def __init__(self, initial_whitelist: list[str]):
        """Deploy with an initial set of trusted premium hostnames.

        The relayer defaults to the deployer and can be reassigned via
        `set_relayer`. The deployer is the owner (whitelist + relayer admin).
        """
        self.owner = gl.message.sender_address
        self.relayer = gl.message.sender_address
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
            pay_to="",
            asset="",
            chain_id=u256(0),
            server_nonce="",
            expiry=u256(0),
            idem_key="",
            has_proof=False,
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
            # authorization view (populated once a query reaches AUTHORIZED)
            "payTo": q.pay_to,
            "asset": q.asset,
            "chainId": str(int(q.chain_id)),
            "idemKey": q.idem_key,
            "hasProof": q.has_proof,
        }

    # -------------------------------------------------------------------------
    # Authorization helper (free view): expose exactly what the OFF-CHAIN
    # relayer needs to settle a single query. The relayer reads this, fetches
    # its own fresh 402 from the merchant, signs with the Base AA session key
    # (whose spend cap + payTo allowlist are enforced ON-CHAIN on Base), settles
    # USDC, then calls submit_payment_proof() with the settlement reference.
    # No private key is ever exposed to or stored by the contract.
    # -------------------------------------------------------------------------
    @gl.public.view
    def get_payment_intent(self, query_id: int) -> dict:
        """Canonical, query-bound payment intent for the relayer to honor.

        Only meaningful once the query is AUTHORIZED. The `idemKey` is the
        exactly-once settlement key (bound to this queryId): the relayer MUST
        settle at most once per idemKey, and the Base session key enforces the
        ceiling on-chain as the authoritative backstop.
        """
        q = self._must_get(query_id)
        if q.status != STATUS_AUTHORIZED:
            raise Exception(f"query {query_id} is not AUTHORIZED (is {q.status})")
        return _payment_intent_dict(q)

    # =========================================================================
    # Step 2 (PHASE A): authorize payment for a PENDING query.
    #
    # WHY TWO PHASES? `resolve_authorize` and `resolve_fetch` each open a
    # non-deterministic block, but a GenVM non-det block runs on EVERY
    # validator. Any value-moving side effect MUST therefore happen exactly
    # once, in the DETERMINISTIC context AFTER the block (on finality), never
    # inside it. The original single `resolve()` signed + paid INSIDE the
    # per-validator closure, which would multiply the spend by the validator
    # count (the central hazard flagged in ARCHITECTURE.md §3.3).
    #
    # So we split the work:
    #   PHASE A  authorize  : probe the URL (non-det), reach consensus on the
    #                          402 payment requirements, then bind + store them
    #                          ONCE (deterministic). No money moves here.
    #   off-chain relayer    : reads get_payment_intent(), settles ONE USDC
    #                          micro-payment on Base using a session key whose
    #                          spend cap + payTo allowlist are enforced
    #                          ON-CHAIN, then calls submit_payment_proof().
    #   PHASE B  fetch       : fetch the now-entitled content (non-det) and
    #                          reach consensus on the EXTRACTED JSON, not on any
    #                          signature. Store the result ONCE (deterministic).
    # =========================================================================
    @gl.public.write
    def resolve_authorize(self, query_id: int) -> None:
        """Probe a PENDING query and bind its payment authorization.

        Non-deterministic part: GET the URL and, if it answers 402, parse the
        payment requirements. Validators reach consensus on those requirements.
        Deterministic part (after the block, once): validate the requirements
        against the per-query ceiling, the global cap, the expected chain and
        asset, bind an idempotency key to this queryId, and move the query to
        AUTHORIZED. A free (200) resource needs no payment and is marked ready.
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

        # --- NON-DETERMINISTIC: probe only, produce VALUES (no money) --------
        # The closure returns a JSON description of the paywall. Validators
        # converge on it via a non-comparative principle (servers may vary
        # whitespace/ordering, so we judge semantic equivalence).
        def _probe() -> str:
            return _probe_402(url)

        probe_json = gl.eq_principle.prompt_non_comparative(
            _probe,
            task=(
                "Two validators each probed a premium URL for its x402 payment "
                "requirements. Decide if the requirements they report are "
                "equivalent."
            ),
            criteria=(
                "`free` must match. When not free, payTo/asset/chainId must "
                "match exactly (case-insensitive) and amount must match within "
                "0.5%. Ignore key ordering and whitespace."
            ),
        )

        # --- DETERMINISTIC (after consensus, runs once): validate + bind -----
        probe = json.loads(probe_json)
        if probe.get("free"):
            # Nothing to pay. Mark AUTHORIZED with a zero-cost proof so the
            # fetch phase can proceed without a relayer round-trip.
            q.pay_to = ""
            q.asset = ""
            q.chain_id = u256(0)
            q.server_nonce = ""
            q.expiry = u256(0)
            q.paid_atomic = u256(0)
            q.idem_key = _idem_key(int(q.id), 0, "")
            q.has_proof = True
            q.status = STATUS_AUTHORIZED
            self.queries[q.id] = q
            return

        amount = int(probe["amount"])
        chain_id = int(probe["chain_id"])
        asset = str(probe["asset"]).upper()
        pay_to = str(probe["pay_to"])
        ceiling = int(q.max_payment_atomic)

        if not pay_to:
            raise Exception("402 missing payTo address")
        if amount <= 0:
            raise Exception("402 amount must be > 0")
        if amount > ceiling:
            # ceiling was already clamped to GLOBAL_MAX at request time.
            raise Exception(f"402 demands {amount} > ceiling {ceiling}")
        if chain_id != PAYMENT_CHAIN_ID:
            raise Exception(f"402 chain {chain_id} != expected {PAYMENT_CHAIN_ID}")
        if asset != EXPECTED_ASSET:
            raise Exception(f"402 asset {asset} != expected {EXPECTED_ASSET}")

        q.pay_to = pay_to
        q.asset = asset
        q.chain_id = u256(chain_id)
        q.server_nonce = str(probe.get("nonce", ""))
        q.expiry = u256(int(probe.get("expiry", 0)))
        # The amount we AUTHORIZE (and the relayer may settle up to). The Base
        # session key enforces this same ceiling on-chain as the backstop.
        q.paid_atomic = u256(amount)
        q.idem_key = _idem_key(int(q.id), chain_id, q.server_nonce)
        q.has_proof = False
        q.status = STATUS_AUTHORIZED
        self.queries[q.id] = q

    # -------------------------------------------------------------------------
    # Relayer callback: record the Base settlement proof for ONE query.
    # -------------------------------------------------------------------------
    @gl.public.write
    def submit_payment_proof(self, query_id: int, settlement_ref: str) -> None:
        """Record the off-chain relayer's Base settlement reference.

        Access-controlled to the configured relayer. Idempotent by construction:
        only an AUTHORIZED query WITHOUT a proof can accept one, so a duplicate
        or replayed call is rejected. Combined with the on-chain spend cap of
        the Base session key, a query can move funds at most once.
        """
        if gl.message.sender_address != self.relayer:
            raise Exception("relayer only")
        q = self._must_get(query_id)
        if q.status != STATUS_AUTHORIZED:
            raise Exception(f"query {query_id} is not AUTHORIZED (is {q.status})")
        if q.has_proof:
            raise Exception(f"query {query_id} already has a payment proof")
        if not settlement_ref:
            raise Exception("settlement_ref must be non-empty")
        q.payment_tx_ref = settlement_ref
        q.has_proof = True
        self.queries[q.id] = q

    # =========================================================================
    # Step 3 (PHASE B): fetch the entitled content and store consensus result.
    # =========================================================================
    @gl.public.write
    def resolve_fetch(self, query_id: int) -> None:
        """Fetch the (now paid-for) content and persist the consensus answer.

        Requires the query to be AUTHORIZED and to carry a payment proof (a
        free resource gets its proof in `resolve_authorize`). The non-det block
        fetches the entitled content and runs LLM extraction; validators reach
        consensus on the EXTRACTED JSON. The signature/payment is never the
        consensus object.
        """
        q = self._must_get(query_id)
        if q.status != STATUS_AUTHORIZED:
            raise Exception(f"query {query_id} is not AUTHORIZED (is {q.status})")
        if not q.has_proof:
            raise Exception(f"query {query_id} has no payment proof yet")

        url = q.url
        prompt = q.prompt
        proof = q.payment_tx_ref

        def _fetch_and_extract() -> str:
            content = _entitled_fetch(url, proof)
            extraction = gl.nondet.exec_prompt(
                f"{prompt}\n\n"
                f"Source content follows between <DATA> tags. Respond with "
                f"ONLY the JSON described above, no commentary.\n"
                f"<DATA>\n{content}\n</DATA>"
            )
            return json.dumps({"extracted": _coerce_json(extraction)}, sort_keys=True)

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
                "timestamps, key ordering, and whitespace."
            ),
        )

        # Persist the consensus result (deterministic, once).
        parsed = json.loads(result_json)
        q.extracted = json.dumps(parsed["extracted"], sort_keys=True)
        q.status = STATUS_RESOLVED
        self.queries[q.id] = q

    # -------------------------------------------------------------------------
    # Admin: relayer management
    # -------------------------------------------------------------------------
    @gl.public.write
    def set_relayer(self, relayer: str) -> None:
        """Set the address allowed to submit payment proofs. Owner only."""
        self._only_owner()
        self.relayer = relayer

    @gl.public.view
    def get_relayer(self) -> str:
        return self.relayer

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
# x402 phase helpers (module-level). The probe/fetch helpers are
# NON-DETERMINISTIC — only call them inside an equivalence-principle block.
# The intent/idempotency helpers are PURE + deterministic.
# -----------------------------------------------------------------------------

def _probe_402(url: str) -> str:
    """Probe a premium URL and report its x402 payment requirements as JSON.

    NON-DETERMINISTIC (uses gl.nondet.web.get). Produces VALUES only — it never
    signs or moves money. Validators reach consensus on its output, then the
    deterministic part of `resolve_authorize` binds the requirements.

    Returns a JSON string:
      free resource  -> {"free": true}
      paywalled      -> {"free": false, "amount", "chain_id", "asset",
                          "pay_to", "nonce", "expiry"}
    """
    # ASSUMPTION: gl.nondet.web.get(url) returns an object/dict exposing status,
    # body, headers. The adapter helpers below tolerate both shapes. (The old
    # scaffold used a guessed `render(..., return_status=, headers=)` signature;
    # the verified GenVM surface is `gl.nondet.web.get`.)
    resp = gl.nondet.web.get(url)
    status = _status_of(resp)

    if status == 200:
        return json.dumps({"free": True}, sort_keys=True)
    if status != 402:
        raise Exception(f"unexpected status {status} probing premium url")

    reqs = _parse_402(resp)
    return json.dumps(
        {
            "free": False,
            "amount": int(reqs["max_amount_required_atomic"]),
            "chain_id": int(reqs.get("chain_id", PAYMENT_CHAIN_ID)),
            "asset": str(reqs.get("asset", EXPECTED_ASSET)),
            "pay_to": str(reqs["pay_to"]),
            "nonce": str(reqs.get("nonce", "")),
            "expiry": int(reqs.get("expiry", 0) or 0),
        },
        sort_keys=True,
    )


def _entitled_fetch(url: str, settlement_ref: str) -> str:
    """Fetch content the contract is already entitled to (payment settled).

    NON-DETERMINISTIC. By this point the relayer has settled the micro-payment
    on Base (or the resource was free). We present the settlement reference via
    the standard x402 `X-PAYMENT-RESPONSE` header so the merchant serves the
    paid content. We never sign anything here.
    """
    headers = {"X-PAYMENT-RESPONSE": settlement_ref} if settlement_ref else {}
    # ASSUMPTION: gl.nondet.web.get supports a headers kwarg. If the verified
    # GenVM surface omits it, the merchant must key entitlement off the settled
    # on-chain payment instead; the relayer boundary remains unchanged.
    resp = gl.nondet.web.get(url, headers=headers) if headers else gl.nondet.web.get(url)
    status = _status_of(resp)
    if status != 200:
        raise Exception(f"entitled fetch did not return content (status {status})")
    return _body_of(resp)


def _idem_key(query_id: int, chain_id: int, server_nonce: str) -> str:
    """Deterministic exactly-once settlement key bound to a single query.

    PURE. Binds the GenLayer queryId to the settlement chain and the server's
    x402 nonce. The relayer MUST settle at most once per idem_key, and the Base
    session key enforces the spend cap on-chain as the authoritative backstop.
    """
    return f"q{int(query_id)}:c{int(chain_id)}:{server_nonce}"


def _payment_intent_dict(q) -> dict:
    """Canonical, query-bound payment intent the relayer must honor (PURE).

    Binds payTo, amount, asset, chainId, nonce, expiry, and the idempotency
    key. The relayer signs a Base AA-session-key payment matching exactly these
    fields; the on-chain spend cap rejects anything above `amountAtomic`.
    """
    return {
        "queryId": str(int(q.id)),
        "payTo": q.pay_to,
        "amountAtomic": str(int(q.paid_atomic)),
        "asset": q.asset,
        "chainId": str(int(q.chain_id)),
        "nonce": q.server_nonce,
        "expiry": str(int(q.expiry)),
        "idemKey": q.idem_key,
    }


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
