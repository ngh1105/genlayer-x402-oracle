/**
 * x402 Paywalled-Data Oracle — minimal genlayer-js client stub.
 *
 * This is an ILLUSTRATIVE scaffold. It does not install or run anything.
 * It shows the shape of a client that:
 *   1. connects to a GenLayer endpoint,
 *   2. submits a query to the deployed Intelligent Contract,
 *   3. waits for validator consensus,
 *   4. reads the resolved result.
 *
 * The contract autonomously handles x402 micro-payments server-side
 * (inside the GenVM), so the frontend never touches a payment key.
 *
 * NOTE: API names below follow the public genlayer-js surface circa 2025.
 * Treat any unfamiliar symbol as a documented assumption (see README).
 */

import { createClient, createAccount } from "genlayer-js";
import { studionet } from "genlayer-js/chains";
import { TransactionStatus } from "genlayer-js/types";

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

/** Address of the deployed x402_oracle Intelligent Contract. */
const CONTRACT_ADDRESS = (process.env.ORACLE_CONTRACT_ADDRESS ??
  "0xYourDeployedOracleContractAddress") as `0x${string}`;

/**
 * A query the oracle can resolve. The `url` must be on the contract's
 * on-chain domain whitelist or the contract will reject it.
 */
interface OracleQuery {
  /** Premium/paywalled resource to fetch (x402-enabled). */
  url: string;
  /** Natural-language extraction instruction for the LLM step. */
  prompt: string;
  /** Hard ceiling (in atomic units, e.g. USDC 6dp) the oracle may pay. */
  maxPaymentAtomic: bigint;
}

// ---------------------------------------------------------------------------
// Client bootstrap
// ---------------------------------------------------------------------------

/**
 * Build a genlayer-js client. In a real app the account would be a browser
 * wallet (e.g. MetaMask) or an injected provider; here we generate an
 * ephemeral account for read/write demonstration.
 */
function makeClient() {
  const account = createAccount(); // ephemeral; replace with wallet provider
  const client = createClient({
    chain: studionet, // or `localnet` / a custom RPC config object
    account,
  });
  return { client, account };
}

// ---------------------------------------------------------------------------
// Write path: submit a query (a state-changing transaction)
// ---------------------------------------------------------------------------

/**
 * Submit a new oracle query. Returns the transaction hash. The contract's
 * `request_data` method enqueues the query into its registry; resolution
 * happens in a later `resolve` transaction (or same-tx, design-dependent).
 */
async function submitQuery(
  client: ReturnType<typeof makeClient>["client"],
  query: OracleQuery,
): Promise<string> {
  const txHash = await client.writeContract({
    address: CONTRACT_ADDRESS,
    functionName: "request_data",
    args: [query.url, query.prompt, query.maxPaymentAtomic],
    value: 0n,
  });

  // Block until validators append + finalize the transaction.
  await client.waitForTransactionReceipt({
    hash: txHash,
    status: TransactionStatus.FINALIZED,
  });

  return txHash;
}

// ---------------------------------------------------------------------------
// Trigger resolution (paid fetch + LLM + consensus happen here)
// ---------------------------------------------------------------------------

/**
 * Ask the oracle to resolve a previously-submitted query by id. This is the
 * transaction where the GenVM performs the x402 fetch, the LLM extraction,
 * and validators run the equivalence principle to reach consensus.
 */
async function resolveQuery(
  client: ReturnType<typeof makeClient>["client"],
  queryId: bigint,
): Promise<string> {
  const txHash = await client.writeContract({
    address: CONTRACT_ADDRESS,
    functionName: "resolve",
    args: [queryId],
    value: 0n,
  });
  await client.waitForTransactionReceipt({
    hash: txHash,
    status: TransactionStatus.FINALIZED,
  });
  return txHash;
}

// ---------------------------------------------------------------------------
// Read path: fetch a resolved result (a free, non-state-changing call)
// ---------------------------------------------------------------------------

interface OracleResult {
  status: "PENDING" | "RESOLVED" | "REJECTED";
  url: string;
  extracted: string;
  paidAtomic: string; // serialized bigint
  paymentTxRef: string; // Base settlement reference (illustrative)
}

/** Read the current state of a query from contract storage (no gas). */
async function readResult(
  client: ReturnType<typeof makeClient>["client"],
  queryId: bigint,
): Promise<OracleResult> {
  const result = (await client.readContract({
    address: CONTRACT_ADDRESS,
    functionName: "get_result",
    args: [queryId],
  })) as unknown as OracleResult;
  return result;
}

// ---------------------------------------------------------------------------
// Demo flow
// ---------------------------------------------------------------------------

async function main() {
  const { client, account } = makeClient();
  console.log("Using account:", account.address);

  const query: OracleQuery = {
    url: "https://api.premium-data.example/v1/markets/eth-usd/close",
    prompt:
      "Extract today's official ETH/USD closing price as a number. " +
      "Return JSON: { \"price\": <number>, \"asOf\": <ISO8601> }.",
    maxPaymentAtomic: 50_000n, // 0.05 USDC @ 6 decimals
  };

  console.log("Submitting query...");
  const submitTx = await submitQuery(client, query);
  console.log("Submitted in tx:", submitTx);

  // In this stub we assume queryId 0 for the first query. A real client would
  // read the emitted event / return value to learn the assigned id.
  const queryId = 0n;

  console.log("Triggering paid resolution...");
  const resolveTx = await resolveQuery(client, queryId);
  console.log("Resolved in tx:", resolveTx);

  const result = await readResult(client, queryId);
  console.log("Oracle result:", result);
}

// Only run when executed directly (not when imported).
main().catch((err) => {
  console.error("client error:", err);
  process.exitCode = 1;
});

export { makeClient, submitQuery, resolveQuery, readResult };
export type { OracleQuery, OracleResult };
