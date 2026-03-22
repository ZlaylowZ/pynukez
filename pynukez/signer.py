"""
Generic signer interface for Nukez envelope authentication.

Decouples envelope signing from key type. The Signer protocol allows
build_signed_envelope to accept either Ed25519 (Keypair) or secp256k1
(EVMSigner) keys without branching on scheme.

Why "Signer" not "SigningKey": auth.py imports nacl.signing.SigningKey.
Using the same name would shadow it. Also, EVM addresses are not public
keys — "identity" is the correct property name.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Protocol, runtime_checkable

_EVM_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


@runtime_checkable
class Signer(Protocol):
    """Generic signer for Nukez envelope authentication.

    Implementations:
      - Keypair (Ed25519, from Solana JSON keypair files)
      - EVMSigner (secp256k1, from EVM private key files)
    """

    @property
    def identity(self) -> str:
        """Canonical public identifier.

        Base58 for Ed25519, lowercase 0x-hex for EVM.
        """
        ...

    @property
    def sig_alg(self) -> str:
        """Signature algorithm: 'ed25519' or 'secp256k1'."""
        ...

    def sign(self, message: bytes) -> str:
        """Sign raw bytes. Returns transport-ready encoded signature.

        Ed25519 -> base58-encoded signature string.
        secp256k1 -> 0x-prefixed hex-encoded signature string.

        The caller places this directly into the X-Nukez-Signature header.

        IMPORTANT: This method MUST be synchronous.
        ``build_signed_envelope`` calls it in a sync context.
        Consumers with async signing backends should bridge to sync
        before injecting a Signer.
        """
        ...


class EVMSigner:
    """secp256k1 signer using EIP-191 personal_sign.

    Loads from the same JSON format as EVM payment keys:
        {"address": "0x...", "private_key": "0x...", ...}

    Optional dependency: eth_account (gated behind pip install pynukez[evm]).
    """

    def __init__(self, private_key: str, address: str = ""):
        """
        Create an EVM signer from a private key.

        Args:
            private_key: Hex-encoded private key (0x-prefixed or bare).
            address: Optional EVM address for verification. If provided,
                     must match the address derived from private_key.

        Raises:
            ImportError: If eth_account is not installed.
            ValueError: If provided address doesn't match derived address.
        """
        try:
            from eth_account import Account
            from eth_account.messages import encode_defunct
        except ImportError:
            raise ImportError(
                "eth_account required for EVM signing. "
                "Install with: pip install pynukez[evm]"
            )
        self._Account = Account
        self._encode_defunct = encode_defunct

        # Derive address from private key
        acct = Account.from_key(private_key)
        derived = acct.address.lower()
        if address:
            provided = address.lower()
            if not provided.startswith("0x"):
                provided = f"0x{provided}"
            if provided != derived:
                raise ValueError(
                    f"Provided address {address} does not match "
                    f"derived address {derived}"
                )
        self._private_key = private_key
        self._address = derived  # ALWAYS lowercase

    @property
    def identity(self) -> str:
        """Lowercase 0x-prefixed EVM address."""
        return self._address

    @property
    def sig_alg(self) -> str:
        return "secp256k1"

    def sign(self, message: bytes) -> str:
        """EIP-191 personal_sign. Returns 0x-prefixed hex signature."""
        msg = self._encode_defunct(primitive=message)
        signed = self._Account.sign_message(msg, private_key=self._private_key)
        result = signed.signature.hex()
        # Defensive: ensure 0x prefix. eth_account >= 0.10 returns "0x..."
        # from HexBytes.hex(), but guard against unexpected versions.
        # Without 0x, the gateway's format inference silently misroutes.
        if not result.startswith("0x"):
            result = f"0x{result}"
        return result

    @classmethod
    def from_file(cls, path: str) -> "EVMSigner":
        """Load from JSON key file.

        Expected format (same as ~/.keys/monad_key.json):
            {"address": "0x...", "private_key": "0x...", ...}
        """
        from .errors import NukezError

        p = Path(path).expanduser()
        if not p.exists():
            raise NukezError(f"EVM key file not found: {p}")

        with open(p) as f:
            data = json.load(f)

        private_key = data.get("private_key") or data.get("privateKey") or ""
        address = data.get("address", "")

        if not private_key:
            raise ValueError(f"No private_key found in {p}")

        return cls(private_key=private_key, address=address)
