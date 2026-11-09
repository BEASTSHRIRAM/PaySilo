// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title SettlementRegistry — Metal L2 anchor for PaySilo reconciliation receipts.
/// @notice Stores ONLY receipt hashes. No amounts, names, account numbers or
///         any other payload data ever goes on-chain: PaySilo hashes the
///         reconciliation receipt off-chain (SHA-256) and anchors the
///         32-byte hash here, so anyone can later prove the receipt existed
///         at a point in time without learning anything about the payroll.
contract SettlementRegistry {
    struct Anchor {
        address attestor;
        uint256 timestamp;
    }

    /// receiptHash => anchor record (attestor is address(0) when unset).
    mapping(bytes32 => Anchor) private _anchors;

    /// Emitted for every anchored receipt hash.
    event ReceiptAnchored(bytes32 indexed receiptHash, address indexed attestor, uint256 timestamp);

    /// @notice Anchor a receipt hash. Re-anchoring the same hash is a no-op
    ///         revert: the first attestation is the proof of existence.
    /// @param receiptHash SHA-256 hash of the off-chain reconciliation receipt.
    function anchor(bytes32 receiptHash) external {
        require(receiptHash != bytes32(0), "SettlementRegistry: zero hash");
        require(_anchors[receiptHash].attestor == address(0), "SettlementRegistry: already anchored");
        _anchors[receiptHash] = Anchor({attestor: msg.sender, timestamp: block.timestamp});
        emit ReceiptAnchored(receiptHash, msg.sender, block.timestamp);
    }

    /// @notice Who anchored a receipt hash, and when. Zero values when unknown.
    function getAnchor(bytes32 receiptHash) external view returns (address attestor, uint256 timestamp) {
        Anchor memory record = _anchors[receiptHash];
        return (record.attestor, record.timestamp);
    }

    /// @notice True when a receipt hash has been anchored.
    function isAnchored(bytes32 receiptHash) external view returns (bool) {
        return _anchors[receiptHash].attestor != address(0);
    }
}
