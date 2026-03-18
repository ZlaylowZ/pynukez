"""
EVM payment execution for Nukez SDK (Monad, Ethereum, etc.).

Parallel to payment.py (Solana). Lazy-imported by client.py only when
evm_transfer() is called.

Transfer pattern: direct transfer() to treasury address (single tx).
The gateway verifies via:
  - Native tokens (MON/ETH): tx.value check
  - ERC-20 tokens (USDC/USDT/WETH): Transfer event log parsing

Token registry below is a HINT for offline/fallback use only.
The 402 response from request_storage() is the source of truth for
contract addresses and amounts at runtime. A mismatch between this
registry and the 402 response is non-fatal.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Union

from .errors import NukezError

log = logging.getLogger("pynukez.evm_payment")

# Check for web3 libraries
try:
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware
    from eth_account import Account
    HAS_WEB3 = True
except ImportError:
    HAS_WEB3 = False
    Web3 = None
    Account = None

# ---------------------------------------------------------------------------
# Token registry — mirrors gateway/app/core/evm_tokens.py
#
# THIS REGISTRY IS A HINT, NOT THE SOURCE OF TRUTH.
# The 402 response provides correct addresses at runtime.
# A mismatch is non-fatal — the 402 response always wins.
# ---------------------------------------------------------------------------

EVM_TOKENS: Dict[int, Dict[str, Dict[str, Any]]] = {
    143: {  # monad-mainnet
        "MON":  {"address": "0x0000000000000000000000000000000000000000", "decimals": 18, "is_native": True},
        "USDC": {"address": "0x754704Bc059F8C67012fEd69BC8A327a5aafb603", "decimals": 6},
        "USDT": {"address": "0xe7cd86e13AC4309349F30B3435a9d337750fC82D", "decimals": 6},
        "WETH": {"address": "0xEE8c0E9f1BFFb4Eb878d8f15f368A02a35481242", "decimals": 18},
    },
    10143: {  # monad-testnet
        "MON":  {"address": "0x0000000000000000000000000000000000000000", "decimals": 18, "is_native": True},
        "USDC": {"address": "0x534b2f3A21130d7a60830c2Df862319e593943A3", "decimals": 6},
        "USDT": {"address": "0x88b8E2161DEDC77EF4ab7585569D2415a1C1055D", "decimals": 6},
        "WETH": {"address": "0xB5a30b0FDc5EA94A52fDc42e3E9760Cb8449Fb37", "decimals": 18},
    },
}

NETWORK_TO_CHAIN_ID: Dict[str, int] = {
    "monad-mainnet": 143,
    "monad-testnet": 10143,
}

# Default RPC endpoints from env vars (matching gateway config pattern)
_DEFAULT_RPC: Dict[str, str] = {
    "monad-mainnet": "MONAD_MAINNET_RPC_PRIMARY",
    "monad-testnet": "MONAD_TESTNET_RPC_PRIMARY",
}

_DEFAULT_RPC_FALLBACK: Dict[str, str] = {
    "monad-mainnet": "MONAD_MAINNET_RPC_FALLBACK",
    "monad-testnet": "MONAD_TESTNET_RPC_FALLBACK",
}

# ERC-20 minimal ABI for transfer()
ERC20_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
]


def _default_rpc_for_network(network: str) -> str:
    """
    Get RPC URL for an EVM network.

    Checks env vars matching gateway config:
      MONAD_MAINNET_RPC_PRIMARY, MONAD_TESTNET_RPC_PRIMARY, etc.
    """
    env_key = _DEFAULT_RPC.get(network)
    if env_key:
        url = os.getenv(env_key, "").strip()
        if url:
            return url

    # Fallback env var
    fb_key = _DEFAULT_RPC_FALLBACK.get(network)
    if fb_key:
        url = os.getenv(fb_key, "").strip()
        if url:
            return url

    raise NukezError(
        f"No RPC URL configured for network '{network}'. "
        f"Set environment variable {env_key or network.upper().replace('-', '_') + '_RPC_PRIMARY'} "
        f"or pass rpc_url to the constructor."
    )


def load_evm_private_key(key_path: Union[str, Path]) -> str:
    """
    Load an EVM private key from file.

    Supports:
      - Hex string (with or without 0x prefix)
      - Raw 32 bytes
      - JSON keystore (not password-protected)

    Returns 0x-prefixed hex private key string.

    Note: This differs from Solana keypair format — do not reuse
    payment.py key loading logic.
    """
    if not HAS_WEB3:
        raise ImportError(
            "Web3 libraries required. Install with: pip install pynukez[evm]"
        )

    kp = Path(key_path).expanduser()
    if not kp.exists():
        raise NukezError(f"EVM private key file not found: {kp}")

    raw = kp.read_text().strip()

    # Hex string (with or without 0x prefix)
    clean = raw.strip()
    if clean.startswith("0x") or clean.startswith("0X"):
        clean = clean[2:]

    # Try as hex string
    try:
        key_bytes = bytes.fromhex(clean)
        if len(key_bytes) == 32:
            return "0x" + clean.lower()
    except ValueError:
        pass

    # Try as JSON keystore (no password)
    try:
        keystore = json.loads(raw)
        if isinstance(keystore, dict) and "crypto" in keystore:
            # This is a keystore file — needs password
            raise NukezError(
                f"Password-protected keystore detected at {kp}. "
                f"Please export the private key as a hex string instead."
            )
        # Maybe it's a JSON with a "private_key" field
        if isinstance(keystore, dict) and "private_key" in keystore:
            pk = keystore["private_key"]
            if pk.startswith("0x"):
                return pk.lower()
            return "0x" + pk.lower()
    except (json.JSONDecodeError, TypeError):
        pass

    # Try as raw bytes
    raw_bytes = kp.read_bytes().strip()
    if len(raw_bytes) == 32:
        return "0x" + raw_bytes.hex()

    raise NukezError(
        f"Could not parse EVM private key from {kp}. "
        f"Expected: hex string (64 chars), 0x-prefixed hex, or raw 32 bytes."
    )


class EVMPayment:
    """
    EVM payment handler for Nukez (Monad, Ethereum, etc.).

    Parallel to SolanaPayment. Handles native token and ERC-20 transfers.

    Usage:
        evm = EVMPayment(private_key_path="~/.nukez/evm_key.hex", network="monad-testnet")
        tx_hash = evm.transfer(to_address="0x...", amount_raw=1000000, pay_asset="USDC",
                               token_address="0x...")
    """

    def __init__(
        self,
        private_key_path: Union[str, Path],
        network: str = "monad-testnet",
        rpc_url: Optional[str] = None,
    ):
        if not HAS_WEB3:
            raise ImportError(
                "Web3 libraries required for EVM payments. "
                "Install with: pip install pynukez[evm]"
            )

        self.network = network
        self._chain_id = NETWORK_TO_CHAIN_ID.get(network)
        if not self._chain_id:
            log.warning(
                "Unknown network '%s' — chain_id not in registry. "
                "Transfer will still work if RPC is valid.", network
            )

        # Load private key
        self._private_key = load_evm_private_key(private_key_path)
        self._account = Account.from_key(self._private_key)

        # Connect to RPC
        self._rpc_url = rpc_url or _default_rpc_for_network(network)
        self._w3 = Web3(Web3.HTTPProvider(self._rpc_url))

        # Monad and other PoA chains need the extraData middleware
        try:
            self._w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        except Exception:
            # Some web3 versions handle this differently
            pass

        if not self._w3.is_connected():
            raise NukezError(
                f"Cannot connect to EVM RPC at {self._rpc_url}. "
                f"Check your RPC URL and network settings."
            )

        # Nonce lock for concurrent transfers
        self._nonce_lock = threading.Lock()

    @property
    def address(self) -> str:
        """Checksummed wallet address."""
        return self._account.address

    def get_balance(self, token_address: Optional[str] = None) -> float:
        """
        Get wallet balance.

        Args:
            token_address: ERC-20 contract address. None for native token.

        Returns:
            Balance in human-readable units.
        """
        if token_address and token_address != "0x" + "0" * 40:
            # ERC-20 balance
            contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(token_address),
                abi=ERC20_ABI,
            )
            raw_balance = contract.functions.balanceOf(self.address).call()
            # Look up decimals from registry or default to 18
            decimals = 18
            if self._chain_id:
                tokens = EVM_TOKENS.get(self._chain_id, {})
                for _sym, cfg in tokens.items():
                    if cfg.get("address", "").lower() == token_address.lower():
                        decimals = cfg["decimals"]
                        break
            return raw_balance / (10 ** decimals)
        else:
            # Native balance
            raw_balance = self._w3.eth.get_balance(self.address)
            return raw_balance / (10 ** 18)

    def transfer_native(
        self,
        to_address: str,
        amount_wei: int,
        confirm: bool = True,
        confirm_timeout: float = 30.0,
    ) -> str:
        """
        Transfer native token (MON, ETH) to an address.

        Args:
            to_address: Destination 0x address
            amount_wei: Amount in wei (atomic units)
            confirm: Wait for transaction confirmation
            confirm_timeout: Max seconds to wait for confirmation

        Returns:
            0x-prefixed transaction hash
        """
        with self._nonce_lock:
            nonce = self._w3.eth.get_transaction_count(self.address, "pending")

            tx = {
                "nonce": nonce,
                "to": Web3.to_checksum_address(to_address),
                "value": amount_wei,
                "chainId": self._chain_id or self._w3.eth.chain_id,
            }

            # Use EIP-1559 gas pricing
            try:
                base_fee = self._w3.eth.get_block("latest").get("baseFeePerGas", 0)
                tx["maxFeePerGas"] = base_fee * 2 + self._w3.to_wei(2, "gwei")
                tx["maxPriorityFeePerGas"] = self._w3.to_wei(2, "gwei")
            except Exception:
                # Fallback to legacy gas pricing
                tx["gasPrice"] = self._w3.eth.gas_price

            tx["gas"] = self._w3.eth.estimate_gas(tx)

            signed = self._w3.eth.account.sign_transaction(tx, self._private_key)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)

        tx_hash_hex = tx_hash.hex() if isinstance(tx_hash, bytes) else str(tx_hash)
        if not tx_hash_hex.startswith("0x"):
            tx_hash_hex = "0x" + tx_hash_hex

        if confirm:
            self._wait_for_confirmation(tx_hash_hex, confirm_timeout)

        return tx_hash_hex

    def transfer_erc20(
        self,
        token_address: str,
        to_address: str,
        amount_raw: int,
        confirm: bool = True,
        confirm_timeout: float = 30.0,
    ) -> str:
        """
        Transfer ERC-20 tokens via direct transfer() call.

        The gateway verifies by parsing Transfer event logs from the
        transaction receipt. Pattern: direct transfer(), NOT approve+transferFrom.

        Args:
            token_address: ERC-20 contract address
            to_address: Destination 0x address (treasury)
            amount_raw: Amount in token's atomic units
            confirm: Wait for transaction confirmation
            confirm_timeout: Max seconds to wait for confirmation

        Returns:
            0x-prefixed transaction hash
        """
        contract = self._w3.eth.contract(
            address=Web3.to_checksum_address(token_address),
            abi=ERC20_ABI,
        )

        with self._nonce_lock:
            nonce = self._w3.eth.get_transaction_count(self.address, "pending")

            tx = contract.functions.transfer(
                Web3.to_checksum_address(to_address),
                amount_raw,
            ).build_transaction({
                "from": self.address,
                "nonce": nonce,
                "chainId": self._chain_id or self._w3.eth.chain_id,
            })

            # Use EIP-1559 gas pricing
            try:
                base_fee = self._w3.eth.get_block("latest").get("baseFeePerGas", 0)
                tx["maxFeePerGas"] = base_fee * 2 + self._w3.to_wei(2, "gwei")
                tx["maxPriorityFeePerGas"] = self._w3.to_wei(2, "gwei")
            except Exception:
                # Fallback to legacy gas pricing
                if "gasPrice" not in tx:
                    tx["gasPrice"] = self._w3.eth.gas_price

            signed = self._w3.eth.account.sign_transaction(tx, self._private_key)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)

        tx_hash_hex = tx_hash.hex() if isinstance(tx_hash, bytes) else str(tx_hash)
        if not tx_hash_hex.startswith("0x"):
            tx_hash_hex = "0x" + tx_hash_hex

        if confirm:
            self._wait_for_confirmation(tx_hash_hex, confirm_timeout)

        return tx_hash_hex

    def transfer(
        self,
        to_address: str,
        amount_raw: int,
        pay_asset: str = "MON",
        token_address: Optional[str] = None,
    ) -> str:
        """
        Unified transfer: dispatches to native or ERC-20 based on asset.

        This is the primary transfer method — auto-dispatches based on
        whether the token is native or an ERC-20.

        Args:
            to_address: Destination 0x address (from request.pay_to_address)
            amount_raw: Atomic units (from request.amount_raw)
            pay_asset: Token symbol (from request.pay_asset)
            token_address: ERC-20 contract address (from request.token_address,
                          None for native tokens). If None and pay_asset is not
                          native, attempts lookup from built-in registry.

        Returns:
            0x-prefixed transaction hash for confirm_storage()
        """
        is_native = self._is_native_token(pay_asset, token_address)

        if is_native:
            log.info(
                "Native transfer: %s %s → %s on %s",
                amount_raw, pay_asset, to_address[:10], self.network,
            )
            return self.transfer_native(to_address, amount_raw)
        else:
            # Resolve token address if not provided
            if not token_address:
                token_address = self._resolve_token_address(pay_asset)
            log.info(
                "ERC-20 transfer: %s %s (contract %s) → %s on %s",
                amount_raw, pay_asset, token_address[:10], to_address[:10], self.network,
            )
            return self.transfer_erc20(token_address, to_address, amount_raw)

    def _is_native_token(self, pay_asset: str, token_address: Optional[str]) -> bool:
        """Determine if a token is native (MON/ETH) or ERC-20."""
        # Explicit zero-address means native
        if token_address == "0x" + "0" * 40:
            return True
        # No token_address + known native symbol
        if not token_address and pay_asset.upper() in ("MON", "ETH"):
            return True
        # Check registry
        if self._chain_id and not token_address:
            tokens = EVM_TOKENS.get(self._chain_id, {})
            cfg = tokens.get(pay_asset.upper(), {})
            if cfg.get("is_native"):
                return True
        return not token_address  # If no address, assume native

    def _resolve_token_address(self, pay_asset: str) -> str:
        """
        Look up token contract address from built-in registry.

        This is a fallback — the 402 response should provide
        token_address directly. The registry is a hint only.
        """
        if not self._chain_id:
            raise NukezError(
                f"Cannot resolve token address for '{pay_asset}' — "
                f"unknown chain_id for network '{self.network}'. "
                f"Pass token_address explicitly."
            )

        tokens = EVM_TOKENS.get(self._chain_id, {})
        cfg = tokens.get(pay_asset.upper())
        if not cfg:
            raise NukezError(
                f"Token '{pay_asset}' not found in registry for chain {self._chain_id}. "
                f"Available: {', '.join(tokens.keys())}. "
                f"Pass token_address explicitly from the 402 response."
            )

        addr = cfg["address"]
        if addr == "0x" + "0" * 40:
            raise NukezError(
                f"'{pay_asset}' is a native token — use transfer_native(), "
                f"not transfer_erc20()."
            )

        return addr

    def _wait_for_confirmation(
        self,
        tx_hash: str,
        timeout: float = 30.0,
        poll_interval: float = 0.5,
    ) -> None:
        """
        Wait for transaction to be mined and confirmed.

        Monad has ~400ms blocks and ~800ms finality, so we poll
        aggressively with 500ms intervals (default 1 confirmation).
        """
        start = time.time()
        while time.time() - start < timeout:
            try:
                receipt = self._w3.eth.get_transaction_receipt(tx_hash)
                if receipt is not None:
                    status = receipt.get("status", 0)
                    if status == 1:
                        return  # Success
                    elif status == 0:
                        raise NukezError(
                            f"EVM transaction reverted: {tx_hash}. "
                            f"Check the transaction on a block explorer."
                        )
            except NukezError:
                raise
            except Exception:
                pass  # Receipt not available yet
            time.sleep(poll_interval)

        raise NukezError(
            f"EVM transaction confirmation timeout after {timeout}s. "
            f"Hash: {tx_hash}. Check a block explorer for status."
        )
