# IMPL_NOTES.md — x402 payment signing implementation (crash-safe checkpoint)

> Working notes for wiring real, key-safe x402 payment signing into
> `contracts/x402_oracle.py`. Decided architecture: **Base account-abstraction
> (ERC-4337) session key, spend cap enforced ON-CHAIN on Base**. Signing key
> NEVER in contract storage or source. Updated continuously as work proceeds.

## 0. Status log

- [t0] Read x402_oracle.py, README, ARCHITECTURE, DEPLOYMENTS, conftest,
  tests/test_helpers.py, git log. Wrote this checkpoint. Starting research.

## (a) Current GenVM API surface — confirmed from local code/docs

Runner header in contract (line 1-2):
```
# v0.2.16
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
```
GenVM v0.2.16. Deployed + verified on studionet (see DEPLOYMENTS.md), contract
`0x556E27b5F6c87B30E40dCD94E5706F1d48fa75b0`.

Confirmed-used `gl` surface (from contract source — marked ASSUMPTION where the
contract author was unsure):
- `from genlayer import *` — module-level wildcard import (GenLayer convention).
- `gl.Contract` — base class for the Intelligent Contract.
- `gl.public.write` / `gl.public.view` — method decorators.
- `gl.message.sender_address` — tx sender (NOTE: conftest stub exposes
  `gl.message.sender_account`, a MISMATCH; tests don't construct the contract so
  it's never hit. Need to verify the real attribute name against the SDK).
- `gl.nondet.web.render(url, mode=, return_status=, headers=)` — ASSUMPTION on
  the kwargs (`return_status`, `headers`).
- `gl.nondet.exec_prompt(text)` — non-deterministic LLM call.
- `gl.eq_principle.prompt_non_comparative(fn, task=, criteria=)` — consensus.
  Also referenced: `strict_eq`, `prompt_comparative`.
- Storage types: `TreeMap[K,V]`, `DynArray`, `Address`, sized ints `u256` etc.,
  `@allow_storage` + `@dataclass` for storage records (all ASSUMPTION-marked).

>>> ALL of the above need verification against the real py-genlayer SDK docs.
>>> See section (research) below for what is actually confirmed.

## (b) THE CORE CONSTRAINT — N-payments problem

`resolve()` builds closure `_fetch_and_extract()` and passes it to
`gl.eq_principle.prompt_non_comparative(...)`. In GenLayer, that closure runs
**independently on every validator** (leader + validators re-execute / appeal
flow). The closure currently calls `_x402_fetch()` which calls
`_sign_x402_payment()` and submits the paid request. Therefore, as written:

  N validators  ->  N signatures  ->  potentially N on-chain payments.

This is the central hazard flagged in ARCHITECTURE.md §3.3 ("naive every-
validator-pays"). The fix must guarantee the **payment is authorized/settled
EXACTLY ONCE**, while validators still reach **consensus on the returned
content**, NOT on the signature.

### Design answer (to implement + document in code)

Split the non-deterministic work into TWO phases:

1. **Deterministic authorization (once, on-chain, before non-det):** the
   contract derives a canonical, query-bound payment intent: `(payTo, amount,
   asset, chainId, nonce, expiry)` where `nonce` is **bound to the GenLayer
   queryId** (idempotency key). This is computed deterministically and stored.
2. **Off-chain signer/relayer = explicit interface boundary:** the actual
   ERC-4337 UserOperation against the Base smart account session key is signed
   and submitted by an **off-chain relayer**, NOT inside the GenVM closure. The
   session key on Base enforces the **spend cap + payTo allowlist on-chain**, so
   even if the relayer is called more than once for the same queryId, the Base
   account / paymaster rejects the duplicate (idempotent settlement keyed by the
   query nonce). The relayer returns only the resulting `X-PAYMENT` proof /
   settlement reference.
3. **Consensus on content, not signature:** inside `eq_principle`, every
   validator fetches the *already-unlocked* content (or fetches with the shared
   settlement proof) and runs LLM extraction. Validators compare the extracted
   JSON. The signature/payment itself is never the consensus object.

Exactly-once is enforced at TWO layers (defense in depth):
- **Off-chain:** relayer dedupes by `queryId`/nonce.
- **On-chain (authoritative):** Base smart-account session key has a hard spend
  cap and the EIP-3009/UserOp nonce is bound to the queryId, so a replay or a
  second validator's attempt cannot move funds twice.

### Open question driving the implementation shape
Does GenVM let a contract make an **outbound EVM signed tx** (native signing /
secret primitive) at all? If YES, a designated-leader or threshold-sign model
could live closer to the chain. If NO, the off-chain relayer boundary is
mandatory (and is the honest, documented design). Resolving this in research
below decides whether AA-session-key is feasible as specified.

## (c) Planned flow (to be refined after research)

```
request_data(url, prompt, cap)            # deterministic, stores PENDING (unchanged)
  -> queryId

resolve(queryId)                          # the change surface
  1. deterministic guards (whitelist, status, ceiling)  [on-chain, once]
  2. build query-bound PaymentIntent(payTo?, ceiling, asset, chainId,
     nonce=H(queryId|contract|chainId), expiry)         [deterministic]
  3. eq_principle.prompt_non_comparative(_fetch_and_extract):
       per validator:
         a. web.render(url) -> 402 + reqs
         b. validate reqs against PaymentIntent (amount<=ceiling, chainId,
            payTo allowlisted, nonce/expiry bound)
         c. X-PAYMENT = settle_via_relayer(intent)   # OFF-CHAIN BOUNDARY,
            idempotent by queryId; on-chain cap on Base is authoritative
         d. web.render(url, headers={X-PAYMENT}) -> 200 + content
         e. exec_prompt(prompt + <DATA>content</DATA>) -> JSON
         f. return {extracted, paid_atomic, payment_tx_ref}
       eq principle judges semantic equivalence of extracted JSON
  4. persist consensus result (status RESOLVED)
```

The signer/relayer call (3c) is the documented interface boundary. The
on-chain spend-cap + field-binding logic and the contract-side validation
(amount<=ceiling, chainId==Base, payTo allowlist, nonce==H(queryId), expiry)
must be REAL and complete in the contract; only the actual private-key signing
lives behind the relayer interface.

## (research) GenLayer capability findings — VERIFIED (with citations)

Sources (GenLayer official docs, fetched 2026-06-01):
- [EVM] https://docs.genlayer.com/developers/intelligent-contracts/features/interacting-with-evm-contracts
- [ND]  https://docs.genlayer.com/developers/intelligent-contracts/features/non-determinism
- [HOME] https://docs.genlayer.com  (confirms GenLayer explicitly targets Coinbase x402 for agentic payments)

### CONFIRMED real GenVM surface (v0.2.16)
- Non-det entry: `gl.vm.run_nondet_unsafe(leader_fn, validator_fn)` runs a
  leader_fn + validator_fn pair. Equivalence-principle helpers
  (`strict_eq`, `comparative`, `non_comparative`) are "convenience shortcuts"
  over this. [ND]
- Inside nondet ONLY: `gl.nondet.web.get(url)`, `gl.nondet.exec_prompt(prompt)`.
  These CANNOT run in regular contract code. [ND]
  >>> NOTE: real call is `gl.nondet.web.get(...)`, NOT `gl.nondet.web.render(...)`
  >>> as the current scaffold assumes. The scaffold render()/return_status/
  >>> headers kwargs are UNVERIFIED guesses.
- EVM interop: `@gl.evm.contract_interface` class with `View`/`Write` inner
  classes. Read via `Iface(addr).view().method()`. Write via
  `Iface(addr).emit().method(args)`. Balance via `Iface(addr).balance`. [EVM]

### THE EXACTLY-ONCE PRIMITIVE (this solves the core constraint)
[ND] states explicitly that the following MUST be OUTSIDE nondet blocks, in the
deterministic context AFTER the block returns:
  - Storage writes (`self.x = ...`)
  - Contract calls (`gl.get_contract_at()`)
  - **Message emission** (incl. EVM `.emit()`)
Reason quoted: "Storage must only change based on consensus-agreed values."
[EVM] adds: "Messages to EVM contract can be emitted only on finality."

=> An outbound EVM write / message is emitted EXACTLY ONCE, post-consensus, on
   finality, NOT once per validator. The per-validator nondet block only
   PRODUCES VALUES (fetched content, parsed 402, LLM extraction) that are then
   reconciled by the equivalence principle; the money-moving side effect is a
   single deterministic emission afterward.

### The current scaffold BUG (root cause of N-payments)
`resolve()` calls `_x402_fetch()` (which calls `_sign_x402_payment()` and POSTs
the paid request) INSIDE the closure passed to
`gl.eq_principle.prompt_non_comparative`. That closure is the per-validator
nondet body => N validators each sign + pay. This violates the [ND] rule that
the value-moving effect must be a single post-consensus emission.

### Can the contract itself sign an X-PAYMENT (secp256k1/EIP-712)?
NO native "sign arbitrary payload with a contract-held key" primitive found in
the docs. GenLayer money primitives are: native value transfers, and EVM
`.emit()` writes that settle on finality. There is no documented way to hold a
secp256k1 key in the contract and emit an x402 `X-PAYMENT` HTTP header signature
from inside GenVM. (Searched: features/* incl. balances, value-transfers,
messages, special-methods, interacting-with-evm-contracts.)

=> IMPLICATION FOR THE DECIDED ARCHITECTURE (Base AA session key, on-chain cap):
   The AA-session-key approach is FEASIBLE but the actual secp256k1 signing of
   the x402 `X-PAYMENT` header cannot live inside GenVM. It MUST be an off-chain
   signer/relayer (the documented interface boundary, exactly as the task
   anticipated). What CAN and MUST be real on-chain:
     (1) contract-side validation that binds payTo/amount/asset/chainId/nonce/
         expiry and clamps to the spend cap (deterministic, in-contract);
     (2) a single post-consensus AUTHORIZATION via the contract status
         state-machine (PENDING -> AUTHORIZED -> RESOLVED), so the relayer is
         invited to settle a given queryId AT MOST ONCE;
     (3) the Base AA session key enforces the spend cap + payTo allowlist
         ON-CHAIN, making a duplicate/replayed settlement impossible to overpay.
   This is honest: GenVM guarantees single-authorization; Base enforces the cap.

### Ordering problem -> TWO-PHASE resolve
x402 needs: fetch -> 402 -> PAY -> refetch-with-proof -> 200 content. Payment
must be exactly-once & post-consensus, but the content fetch needs payment to
have already happened. These can't both sit in one nondet block. Correct shape:
  Phase A `resolve_authorize(queryId)`:
     - nondet: web.get(url) -> 402 -> parse requirements (consensus on
       payTo/amount/asset/chainId/nonce/expiry)
     - deterministic AFTER block: validate vs ceiling+cap+allowlist, bind nonce
       to queryId, store PaymentAuthorization, status PENDING->AUTHORIZED.
  Phase B `resolve_fetch(queryId)`:
     - requires status AUTHORIZED + a recorded settlement reference (supplied by
       relayer via `submit_payment_proof`, or re-fetch now that entitled)
     - nondet: web.get(url[, proof]) -> 200 content -> exec_prompt extract
       (consensus on extracted JSON)
     - deterministic AFTER block: store extracted + status -> RESOLVED.

This keeps every money side effect single + post-consensus, and consensus is on
CONTENT, never on a signature. Implemented accordingly.
