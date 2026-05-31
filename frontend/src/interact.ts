/**
 * interact.ts — end-to-end smoke against the DEPLOYED x402 Paywalled-Data
 * Oracle on studionet. Exercises the DETERMINISTIC paths that do not depend on
 * the (intentionally stubbed) micro-payment signer:
 *
 *   - is_whitelisted(host)           (view)
 *   - request_data(url, prompt, max) (write -> PENDING query)
 *   - get_result(query_id)           (view)
 *
 * resolve() is NOT called here: it runs the x402 fetch whose payment-signing
 * step is a documented stub pending a key-custody decision (TSS / Base AA
 * session key / TEE). See README "Roadmap".
 *
 * USAGE:
 *   GENLAYER_PRIVATE_KEY=*** GENLAYER_NETWORK=studionet \
 *   ORACLE_ADDRESS=0x... npx tsx src/interact.ts
 */

import { createClient, createAccount } from "genlayer-js";
import { studionet, localnet } from "genlayer-js/chains";
import { TransactionStatus, type Address, type Hash } from "genlayer-js/types";

const CHAIN = process.env.GENLAYER_NETWORK === "localnet" ? localnet : studionet;

/** Raw RPC endpoint (for reading a tx's execution_result). */
const RPC_URL =
  process.env.RPC_URL ??
  (CHAIN === localnet
    ? "http://127.0.0.1:4000/api"
    : "https://studio.genlayer.com/api");

/** Deployed oracle on studionet (override via ORACLE_ADDRESS). */
const ORACLE_ADDRESS = (process.env.ORACLE_ADDRESS ??
  "0x0000000000000000000000000000000000000000") as Address;

/** A whitelisted host from the deploy-time whitelist. */
const WHITELISTED_URL =
  process.env.URL ?? "https://api.premium-data.example/v1/price";
/** A host that is NOT in the whitelist (request_data must reject it). */
const UNTRUSTED_URL = "https://random-unknown-source.example/data";

function requireKey(): `0x${string}` {
  const k = process.env.GENLAYER_PRIVATE_KEY;
  if (!k || !/^0x[0-9a-fA-F]{64}$/.test(k)) {
    throw new Error("Set GENLAYER_PRIVATE_KEY (0x + 64 hex).");
  }
  return k as `0x${string}`;
}

function host(u: string): string {
  return new URL(u).host;
}

function parse(raw: unknown): unknown {
  if (typeof raw !== "string") return raw;
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

/**
 * Read a transaction's GenVM execution_result. On GenLayer a reverting write
 * still reaches ACCEPTED consensus (validators agree on the ERROR outcome), so
 * the JS writeContract call does NOT throw. Negative-path assertions must check
 * execution_result instead of catching a JS exception.
 */
async function execResultOf(txHash: string): Promise<string> {
  const res = await fetch(RPC_URL, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      jsonrpc: "2.0",
      method: "eth_getTransactionByHash",
      params: [txHash],
      id: 1,
    }),
  });
  const j = (await res.json()) as {
    result?: { consensus_data?: { leader_receipt?: unknown } };
  };
  let lr = j.result?.consensus_data?.leader_receipt as
    | { execution_result?: string }
    | Array<{ execution_result?: string }>
    | undefined;
  if (Array.isArray(lr)) lr = lr[0];
  return String(lr?.execution_result ?? "UNKNOWN");
}

async function main(): Promise<void> {
  if (ORACLE_ADDRESS === "0x0000000000000000000000000000000000000000") {
    throw new Error("Set ORACLE_ADDRESS to the deployed x402 oracle address.");
  }

  const account = createAccount(requireKey());
  const client = createClient({ chain: CHAIN, account });

  console.log("x402 oracle — studionet smoke");
  console.log("  contract:", ORACLE_ADDRESS);
  console.log("  caller:  ", account.address);

  // --- view: is_whitelisted (trusted vs untrusted) ------------------------
  const trustedHost = host(WHITELISTED_URL);
  const untrustedHost = host(UNTRUSTED_URL);
  const wl = await client.readContract({
    address: ORACLE_ADDRESS,
    functionName: "is_whitelisted",
    args: [trustedHost],
  });
  const nwl = await client.readContract({
    address: ORACLE_ADDRESS,
    functionName: "is_whitelisted",
    args: [untrustedHost],
  });
  console.log(`\n→ is_whitelisted(${trustedHost})   =>`, wl);
  console.log(`→ is_whitelisted(${untrustedHost}) =>`, nwl);

  // --- negative path: request_data on an untrusted host must be rejected ---
  console.log("\n→ request_data on UNTRUSTED host (expect contract error) ...");
  const badTx = (await client.writeContract({
    address: ORACLE_ADDRESS,
    functionName: "request_data",
    args: [UNTRUSTED_URL, "extract price as JSON", 1_000_000],
    value: 0n,
  })) as Hash;
  await client.waitForTransactionReceipt({
    hash: badTx,
    status: TransactionStatus.ACCEPTED,
  });
  const badExec = await execResultOf(badTx);
  console.log(
    badExec === "ERROR"
      ? "  ✅ contract rejected untrusted host (execution_result=ERROR)"
      : `  ⚠ unexpected execution_result=${badExec}`,
  );

  // --- happy path: request_data on a whitelisted host ---------------------
  console.log("\n→ request_data on WHITELISTED host ...");
  const tx = (await client.writeContract({
    address: ORACLE_ADDRESS,
    functionName: "request_data",
    args: [WHITELISTED_URL, "extract the spot price as JSON {price:number}", 5_000_000],
    value: 0n,
  })) as Hash;
  console.log("  tx:", tx);
  await client.waitForTransactionReceipt({ hash: tx, status: TransactionStatus.ACCEPTED });
  console.log("  accepted ✅ (query persisted as PENDING)");

  // --- view: get_result for the freshly created query (id 0 on fresh deploy)
  const qid = Number(process.env.QUERY_ID ?? "0");
  console.log(`\n→ get_result(${qid})`);
  const result = await client.readContract({
    address: ORACLE_ADDRESS,
    functionName: "get_result",
    args: [qid],
  });
  console.log("  result:", JSON.stringify(parse(result), null, 2));

  console.log(
    "\nDone. Note: resolve() is not exercised — its payment-signing step is a" +
      " documented stub (see README Roadmap).",
  );
}

main().catch((err) => {
  console.error("interact failed:", err);
  process.exitCode = 1;
});
