# Deployments

Live **studionet** deployments. studionet charges no gas; deploys are signed by
an ephemeral key, so **each `npm run deploy` mints a NEW contract address**.
Update this file whenever you redeploy.

## x402 Paywalled-Data Oracle

| Field | Value |
| --- | --- |
| Network | `studionet` (`https://studio.genlayer.com/api`) |
| Contract | `0x556E27b5F6c87B30E40dCD94E5706F1d48fa75b0` |
| Deployer | `0xeD5ba8f2C1Ce875bf71E98bD6f6c25b243eb3AEa` |
| Constructor | `initial_whitelist = ["api.premium-data.example", "data.example"]` |
| Runner | `py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6` (GenVM v0.2.16) |
| Status | deploy SUCCESS; deterministic paths verified end-to-end |

### Verified flows

- `is_whitelisted(host)` -> `true` for whitelisted, `false` otherwise
- `request_data(url, prompt, max)` on a **whitelisted** host -> persists a
  PENDING query (ACCEPTED)
- `request_data` on an **untrusted** host -> contract rejects
  (`execution_result=ERROR`)
- `get_result(id)` -> reads the stored query state

### NOT exercised

- `resolve(query_id)` — runs the x402 fetch whose micro-payment signing step
  (`_sign_x402_payment`) is an intentional `NotImplementedError` stub pending a
  key-custody decision (validator TSS / Base AA session key with spend cap /
  TEE). See README "Roadmap".

> Note: on GenLayer a reverting write still reaches `ACCEPTED` consensus
> (validators agree on the ERROR outcome), so negative-path checks must inspect
> the tx `execution_result`, not catch a JS exception.
