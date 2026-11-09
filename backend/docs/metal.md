# Metal L2 testnet anchoring — runbook

PaySilo anchors a hash of every settled run's reconciliation receipt.
Two implementations share one interface (`app/partners/metal.py`):

- `MockSettlementChain` — offline, deterministic, hash-linked. **The
  default.** Receipts are labelled `chain: "mock"` everywhere.
- `MetalL2SettlementChain` — anchors the receipt hash in the
  `SettlementRegistry` contract on Metal L2 testnet over raw JSON-RPC
  (no web3.py; signing via `eth-account`). It **fails closed**: if
  `METAL_RPC_URL`, `METAL_PRIVATE_KEY` or `METAL_REGISTRY_ADDRESS` is
  missing, every call raises `MetalNotConfiguredError`. It never falls
  back to the mock chain and never labels a mock anchor as on-chain.

**Only hashes go on-chain.** The contract stores `receiptHash (bytes32)`
plus the attestor and timestamp, and the transaction calldata is exactly
`anchor(bytes32)` with that hash. No amounts, names, IBANs or any other
payroll data are sent (covered by
`tests/test_metal.py::test_attest_sends_only_the_receipt_hash_no_pii`).

## Deploy the registry (Sriram, with your own testnet wallet)

Blocked on keys by design: no wallet or RPC credentials exist on the
build machine, so the live deployment happens from your machine.

1. **Get a testnet wallet and funds.** Create a fresh wallet for testnet
   only, note its private key, and fund it from the Metal L2 testnet
   faucet. Never reuse a mainnet wallet or key.
2. **Compile the contract** (needs `solc` ≥ 0.8.20, e.g.
   `npm i -g solc` or a Docker `ethereum/solc` image):

   ```bash
   solc --bin contracts/SettlementRegistry.sol -o build/
   # produces build/SettlementRegistry.bin
   ```

3. **Configure the environment:**

   ```bash
   export METAL_RPC_URL="https://<metal-l2-testnet-rpc>"
   export METAL_PRIVATE_KEY="0x<testnet wallet private key>"
   export METAL_EXPLORER_URL="https://<testnet explorer>"   # optional, for receipt links
   ```

4. **Deploy** (same raw JSON-RPC + local-signing path as the backend):

   ```bash
   cd backend
   uv run python scripts/deploy_settlement_registry.py --bytecode ../build/SettlementRegistry.bin
   ```

   The script fails closed if the RPC URL, key or bytecode is missing,
   then prints the deployment transaction and, once mined, the contract
   address.

5. **Wire the backend:** add the printed address to `.env`:

   ```bash
   METAL_REGISTRY_ADDRESS=0x<deployed address>
   ```

   Restart the backend. From then on, settled runs anchor on Metal L2:
   the receipt row in `settlement_receipts` carries `chain: "metal-l2"`
   and the real `tx_hash`, and the run panel shows an explorer link when
   `METAL_EXPLORER_URL` is set. Remove any of the three `METAL_*` values
   and the backend returns to the mock chain — visibly labelled `mock`.

## Verifying an anchor

- In the run panel: the receipt shows the chain, index, receipt hash and
  (for L2) a transaction link.
- On-chain: call `SettlementRegistry.isAnchored(receiptHash)` or
  `getAnchor(receiptHash)` (returns attestor + timestamp), or open the
  transaction in the explorer — its calldata is the receipt hash and
  nothing else.
