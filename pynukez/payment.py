"""
Solana payment execution for Nukez SDK.

Adapted from proven autonomous_agent/payment.py implementation.
"""

from __future__ import annotations

import json
import time
import os
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Dict, Optional, Union

from .errors import NukezError

LAMPORTS_PER_SOL = 1_000_000_000

# Check for Solana libraries
try:
    from solana.rpc.api import Client
    from solana.rpc.commitment import Confirmed
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.system_program import TransferParams, transfer
    from solders.transaction import VersionedTransaction
    from solders.signature import Signature
    HAS_SOLANA = True
except ImportError:
    HAS_SOLANA = False
    Keypair = None
    Pubkey = None


def lamports_from_amount_sol(amount_sol) -> int:
    """Convert SOL to lamports with proper rounding."""
    d = Decimal(str(amount_sol)).quantize(Decimal("0.000000001"), rounding=ROUND_HALF_UP)
    return int((d * Decimal(LAMPORTS_PER_SOL)).to_integral_value(rounding=ROUND_HALF_UP))


def _default_rpc_for_network(network: str) -> str:
    """Get RPC URL for network.

    Respects `RPC_URL` (preferred) or `SOLANA_RPC_URL` if set.
    """
    env_rpc = os.getenv("RPC_URL") or os.getenv("SOLANA_RPC_URL")
    if env_rpc:
        return env_rpc.strip()

    n = (network or "").lower().strip()
    if n in {"solana-testnet", "testnet"}:
        return "https://api.testnet.solana.com"
    if n in {"solana-mainnet", "solana-mainnet-beta", "mainnet", "mainnet-beta"}:
        return "https://api.mainnet-beta.solana.com"
    # Default to devnet
    return "https://api.devnet.solana.com"

def load_solana_keypair_from_file(keypair_path: Union[str, Path]) -> Keypair:
    """Load a Solana CLI-style keypair JSON file."""
    if not HAS_SOLANA:
        raise ImportError(
            "Solana libraries required. Install with: pip install pynukez[solana]"
        )
    
    kp = Path(keypair_path).expanduser()
    if not kp.exists():
        raise NukezError(f"Keypair file not found: {kp}")
    
    raw = json.loads(kp.read_text())
    if not isinstance(raw, list) or not raw:
        raise NukezError(f"Keypair file is not a JSON int array: {kp}")

    b = bytes(int(x) & 0xFF for x in raw)

    if len(b) == 64:
        return Keypair.from_bytes(b)
    if len(b) == 32:
        return Keypair.from_seed(b)

    raise NukezError(f"Unsupported keypair byte length {len(b)} in {kp}")


def _extract_blockhash(resp: Any) -> Union[str, Hash]:
    """Extract blockhash from solana-py response."""
    if isinstance(resp, dict):
        try:
            return resp["result"]["value"]["blockhash"]
        except Exception:
            pass

    value = getattr(resp, "value", None)
    if value is not None:
        bh = getattr(value, "blockhash", None)
        if bh is not None:
            return bh

    raise NukezError(f"Could not extract blockhash from response: {resp!r}")


def _extract_signature(send_resp: Any) -> str:
    """Extract signature from send_transaction response."""
    if isinstance(send_resp, dict):
        sig = send_resp.get("result")
        if isinstance(sig, str) and sig:
            return sig
        err = send_resp.get("error")
        if err:
            raise NukezError(f"RPC send error: {err}")
        raise NukezError(f"Unexpected send response: {send_resp}")
    
    def _sig_to_str(x: Any) -> Optional[str]:
        if x is None:
            return None
        if isinstance(x, str) and x:
            return x
        try:
            s = str(x)
            if s and "Signature" not in s and len(s) > 40:
                return s
            if hasattr(x, "__class__") and x.__class__.__name__ == "Signature":
                return str(x)
        except Exception:
            return None
        return None
    
    for attr in ("value", "result", "signature"):
        v = getattr(send_resp, attr, None)
        out = _sig_to_str(v)
        if out:
            return out
        if v is not None:
            for inner_attr in ("value", "result", "signature"):
                vv = getattr(v, inner_attr, None)
                out2 = _sig_to_str(vv)
                if out2:
                    return out2
    
    try:
        s = str(send_resp)
        if s and "Signature" not in s and len(s) > 40:
            return s
    except Exception:
        pass
    
    raise NukezError(f"Could not extract signature from send response: {send_resp}")


def _wait_for_confirmation(
    rpc: Client,
    sig: str,
    timeout_seconds: float = 30.0,
    poll_interval: float = 1.0,
) -> bool:
    """Poll for transaction confirmation."""
    start = time.time()
    sig_obj = Signature.from_string(sig)
    
    while time.time() - start < timeout_seconds:
        try:
            resp = rpc.get_signature_statuses([sig_obj])
            
            value = None
            if hasattr(resp, 'value') and resp.value:
                value = resp.value[0]
            elif isinstance(resp, dict):
                try:
                    value = resp.get('result', {}).get('value', [None])[0]
                except (TypeError, IndexError, KeyError):
                    pass
            
            if value is not None:
                err = getattr(value, 'err', None)
                if err is not None:
                    raise NukezError(f"Transaction failed on-chain: {err}")
                
                confirmation_status = getattr(value, 'confirmation_status', None)
                if confirmation_status is not None:
                    status_str = str(confirmation_status).lower()
                    if 'confirmed' in status_str or 'finalized' in status_str:
                        return True
                
                confirmations = getattr(value, 'confirmations', None)
                if confirmations is not None and confirmations > 0:
                    return True
                    
        except NukezError:
            raise
        except Exception:
            pass
        
        time.sleep(poll_interval)
    
    raise NukezError(
        f"Transaction confirmation timeout after {timeout_seconds}s. "
        f"Signature: {sig}. Check Solana explorer."
    )


@dataclass(frozen=True)
class SolanaTransferResult:
    """Result from a Solana transfer."""
    signature: str
    rpc_url: str
    to_address: str
    lamports: int


class SolanaPayment:
    """Solana payment handler for Nukez x402 protocol."""

    def __init__(self, keypair_path: Union[str, Path], network: str = "devnet", rpc_url: str = None):
        if not HAS_SOLANA:
            raise ImportError(
                "Solana libraries required. Install with: pip install pynukez[solana]"
            )
        self.keypair_path = Path(keypair_path).expanduser()
        self.network = network
        self._keypair = None
        self.rpc_url = rpc_url

    def _kp(self) -> Keypair:
        if self._keypair is None:
            self._keypair = load_solana_keypair_from_file(self.keypair_path)
        return self._keypair

    @property
    def pubkey(self) -> str:
        return str(self._kp().pubkey())

    def get_balance(self) -> float:
        """Get wallet balance in SOL."""
        rpc = Client(self.rpc_url)
        response = rpc.get_balance(self._kp().pubkey(), commitment=Confirmed)
        return response.value / LAMPORTS_PER_SOL

    def transfer_sol(
        self,
        to_address: str,
        amount_sol: float,
        confirm: bool = True,
        confirm_timeout: float = 30.0
    ) -> str:
        """
        Transfer SOL to an address.
        
        Returns transaction signature.
        """
        rpc = Client(self.rpc_url)
        lamports = lamports_from_amount_sol(amount_sol)
        payer = self._kp()

        # Get blockhash
        bh_resp = rpc.get_latest_blockhash()
        bh_val = _extract_blockhash(bh_resp)
        blockhash = bh_val if isinstance(bh_val, Hash) else Hash.from_string(bh_val)

        # Build transfer instruction
        ix = transfer(TransferParams(
            from_pubkey=payer.pubkey(),
            to_pubkey=Pubkey.from_string(to_address),
            lamports=lamports
        ))

        # Build versioned transaction
        msg = MessageV0.try_compile(
            payer=payer.pubkey(),
            instructions=[ix],
            address_lookup_table_accounts=[],
            recent_blockhash=blockhash,
        )
        tx = VersionedTransaction(msg, [payer])

        # Send
        try:
            send_resp = rpc.send_transaction(tx)
        except Exception:
            send_resp = rpc.send_raw_transaction(bytes(tx))

        sig = _extract_signature(send_resp)

        # Confirm if requested
        if confirm:
            _wait_for_confirmation(rpc, sig, confirm_timeout)

        return sig
