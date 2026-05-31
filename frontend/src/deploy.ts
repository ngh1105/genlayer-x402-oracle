/**
 * deploy.ts — deploy the x402 Paywalled-Data Oracle Intelligent Contract.
 *
 * Reads the contract source from ../contracts/x402_oracle.py, deploys it to a
 * GenLayer network, waits for finalization, and prints the contract address.
 *
 * USAGE (after `npm install`):
 *   GENLAYER_PRIVATE_KEY=0x... \
 *   GENLAYER_NETWORK=studionet \
 *   X402_WHITELIST=api.premium-data.example,data.example \
 *   npx tsx src/deploy.ts
 *
 * SECURITY: the deployer key is read from the environment and never written to
 * disk or committed. Use a throwaway/testnet key. Do not paste mainnet keys.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { createClient, createAccount } from "genlayer-js";
import {
  localnet,
  studionet,
  testnetAsimov,
  testnetBradbury,
} from "genlayer-js/chains";
import { TransactionStatus, type Hash } from "genlayer-js/types";

// ---------------------------------------------------------------------------
// Network selection
// ---------------------------------------------------------------------------

const NETWORKS = {
  localnet,
  studionet,
  testnetAsimov,
  testnetBradbury,
} as const;

type NetworkName = keyof typeof NETWORKS;

function pickNetwork(): (typeof NETWORKS)[NetworkName] {
  const name = (process.env.GENLAYER_NETWORK ?? "studionet") as NetworkName;
  const chain = NETWORKS[name];
  if (!chain) {
    throw new Error(
      `Unknown GENLAYER_NETWORK="${name}". ` +
        `Valid: ${Object.keys(NETWORKS).join(", ")}`,
    );
  }
  return chain;
}

// ---------------------------------------------------------------------------
// Inputs
// ---------------------------------------------------------------------------

/** Validate + narrow the deployer private key to the 0x-hex shape genlayer-js wants. */
function requirePrivateKey(): `0x${string}` {
  const key = process.env.GENLAYER_PRIVATE_KEY;
  if (!key || !/^0x[0-9a-fA-F]{64}$/.test(key)) {
    throw new Error(
      "Set GENLAYER_PRIVATE_KEY to a 0x-prefixed 32-byte hex key (testnet only).",
    );
  }
  return key as `0x${string}`;
}

/** Constructor arg: initial trusted premium hostnames (comma-separated env). */
function initialWhitelist(): string[] {
  const raw = process.env.X402_WHITELIST ?? "";
  return raw
    .split(",")
    .map((h) => h.trim().toLowerCase())
    .filter((h) => h.length > 0);
}

/** Read the contract source relative to this script (../contracts/...). */
function readContractCode(): string {
  const here = dirname(fileURLToPath(import.meta.url));
  const path = resolve(here, "..", "..", "contracts", "x402_oracle.py");
  return readFileSync(path, "utf8");
}

// ---------------------------------------------------------------------------
// Deploy
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  const chain = pickNetwork();
  const account = createAccount(requirePrivateKey());
  const whitelist = initialWhitelist();
  const code = readContractCode();

  console.log("Deploying x402 Paywalled-Data Oracle");
  console.log("  network:   ", process.env.GENLAYER_NETWORK ?? "studionet");
  console.log("  deployer:  ", account.address);
  console.log("  whitelist: ", whitelist.length ? whitelist : "(empty)");

  const client = createClient({ chain, account });

  const txHash = await client.deployContract({
    code,
    // Constructor: X402Oracle.__init__(self, initial_whitelist: list[str])
    args: [whitelist],
  });
  console.log("  deploy tx: ", txHash);

  const receipt = await client.waitForTransactionReceipt({
    hash: txHash as Hash,
    status: TransactionStatus.FINALIZED,
  });

  // GenLayer deploy receipts expose the new contract under `recipient`
  // (the created contract account). Fall back to other shapes defensively.
  const r = receipt as {
    contractAddress?: string;
    recipient?: string;
    data?: { contract_address?: string };
  };
  const address = r.recipient ?? r.contractAddress ?? r.data?.contract_address;
  if (!address) {
    console.log("\n⚠ Finalized but address field not found. Raw receipt:");
    console.log(JSON.stringify(receipt, null, 2));
    throw new Error("Deployment finalized but no contract address was returned.");
  }

  console.log("\n✅ Deployed x402_oracle at:", address);
  console.log("   Export it for the client:");
  console.log(`   ORACLE_CONTRACT_ADDRESS=${address}`);
}

main().catch((err) => {
  console.error("deploy failed:", err);
  process.exitCode = 1;
});
