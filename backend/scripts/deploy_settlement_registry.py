#!/usr/bin/env python3
"""Deploy ``contracts/SettlementRegistry.sol`` to Metal L2 testnet.

Raw JSON-RPC + local signing (``eth-account``), the same path
``MetalL2SettlementChain`` uses — no web3.py, no Hardhat.

Prerequisites (see ``backend/docs/metal.md`` for the full runbook):

1. Compile the contract once and keep the bytecode file, e.g.::

       solc --bin contracts/SettlementRegistry.sol -o build/
       # -> build/SettlementRegistry.bin

2. Export a faucet-funded testnet wallet and the RPC endpoint::

       export METAL_RPC_URL="https://<metal-l2-testnet-rpc>"
       export METAL_PRIVATE_KEY="0x<testnet wallet private key>"
       export METAL_EXPLORER_URL="https://<explorer>"   # optional

3. Run::

       uv run python scripts/deploy_settlement_registry.py --bytecode ../../build/SettlementRegistry.bin

The script fails closed: with no RPC URL / private key / bytecode it
prints what is missing and exits non-zero. On success it prints the
deployed address — put it in ``METAL_REGISTRY_ADDRESS``.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import httpx

DEPLOY_GAS_LIMIT = 1_500_000  # creation of the small registry contract


class _Rpc:
    def __init__(self, url: str) -> None:
        self.url = url
        self._id = 0

    def __call__(self, method: str, params: list):
        self._id += 1
        with httpx.Client(timeout=20.0) as client:
            response = client.post(self.url, json={
                "jsonrpc": "2.0", "id": self._id, "method": method, "params": params})
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            raise SystemExit(f"{method} failed: {data['error']}")
        return data["result"]


def _load_bytecode(path: Path | None) -> bytes | None:
    candidates = [path] if path else []
    env_path = os.environ.get("METAL_REGISTRY_BYTECODE")
    if env_path:
        candidates.append(Path(env_path))
    repo_root = Path(__file__).resolve().parents[2]
    candidates += [repo_root / "build" / "SettlementRegistry.bin",
                   repo_root / "contracts" / "SettlementRegistry.bin"]
    for candidate in candidates:
        if candidate and candidate.is_file():
            text = candidate.read_text().strip()
            return bytes.fromhex(text[2:] if text.startswith("0x") else text)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bytecode", type=Path, default=None,
                        help="Path to the compiled SettlementRegistry bytecode (.bin from solc).")
    parser.add_argument("--wait-seconds", type=int, default=120,
                        help="How long to wait for the deployment receipt.")
    args = parser.parse_args()

    rpc_url = os.environ.get("METAL_RPC_URL")
    private_key = os.environ.get("METAL_PRIVATE_KEY")
    bytecode = _load_bytecode(args.bytecode)
    missing = [name for name, value in (("METAL_RPC_URL", rpc_url),
                                        ("METAL_PRIVATE_KEY", private_key),
                                        ("compiled SettlementRegistry bytecode", bytecode)) if not value]
    if missing:
        print(f"Cannot deploy — missing: {', '.join(missing)}. Failing closed; nothing was sent.",
              file=sys.stderr)
        return 2

    from eth_account import Account

    rpc = _Rpc(rpc_url)
    account = Account.from_key(private_key)
    tx: dict = {
        "value": 0,
        "gas": DEPLOY_GAS_LIMIT,
        "nonce": int(rpc("eth_getTransactionCount", [account.address, "pending"]), 16),
        "chainId": int(rpc("eth_chainId", []), 16),
        "data": bytecode,
    }
    try:
        priority = int(rpc("eth_maxPriorityFeePerGas", []), 16)
        base_fee = int(rpc("eth_getBlockByNumber", ["latest", False])["baseFeePerGas"], 16)
        tx.update({"type": 2, "maxPriorityFeePerGas": priority, "maxFeePerGas": 2 * base_fee + priority})
    except Exception:
        tx.update({"gasPrice": int(rpc("eth_gasPrice", []), 16)})

    signed = account.sign_transaction(tx)
    tx_hash = rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])
    print(f"Deployment transaction sent: {tx_hash}")
    explorer = os.environ.get("METAL_EXPLORER_URL", "").rstrip("/")
    if explorer:
        print(f"Explorer: {explorer}/tx/{tx_hash}")

    deadline = time.time() + args.wait_seconds
    while time.time() < deadline:
        receipt = rpc("eth_getTransactionReceipt", [tx_hash])
        if receipt:
            if receipt.get("status") not in ("0x1", 1):
                print(f"Deployment transaction reverted: {tx_hash}", file=sys.stderr)
                return 1
            address = receipt["contractAddress"]
            print(f"SettlementRegistry deployed at: {address}")
            print(f"Next: export METAL_REGISTRY_ADDRESS={address}")
            return 0
        time.sleep(3)
    print(f"Timed out waiting for the deployment receipt ({tx_hash}). Check the explorer link above.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
