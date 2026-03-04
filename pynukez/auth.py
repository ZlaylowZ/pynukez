"""
Authentication utilities for Nukez SDK.

Extracted from nukez GenericAgentTools - proven patterns.
Critical implementation details preserved exactly as tested.

CRITICAL: The signed envelope structure MUST match server expectations exactly.
Any deviation will cause authentication failures.
"""

import hashlib
import json
import os
import time
import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Union

try:
    from nacl.signing import SigningKey
    HAS_NACL = True
except ImportError:
    HAS_NACL = False
    SigningKey = None

try:
    import base58
    HAS_BASE58 = True
except ImportError:
    HAS_BASE58 = False
    base58 = None

from .errors import NukezError


@dataclass
class SignedEnvelope:
    """Result from build_signed_envelope()."""
    headers: Dict[str, str]  # X-Nukez-Envelope, X-Nukez-Signature
    canonical_body: Optional[str]
    locker_id: str
    
    # Include what agent needs to know
    usage: str = "Add headers to your HTTP request"


class Keypair:
    """Ed25519 keypair management for Solana-compatible signing."""
    
    def __init__(self, keypair_path: Union[str, Path]):
        """
        Load keypair from Solana CLI format JSON file.
        
        Args:
            keypair_path: Path to keypair JSON file (e.g., ~/.config/solana/id.json)
            
        Raises:
            ImportError: If pynacl or base58 not installed
            NukezError: If keypair file not found or invalid
        """
        if not HAS_NACL:
            raise ImportError(
                "PyNaCl required for signing. "
                "Install with: pip install pynacl"
            )
        if not HAS_BASE58:
            raise ImportError(
                "base58 required for Solana address encoding. "
                "Install with: pip install base58"
            )
        
        self.keypair_path = Path(keypair_path).expanduser()
        
        if not self.keypair_path.exists():
            raise NukezError(
                f"Keypair file not found: {self.keypair_path}. "
                f"Create one with: solana-keygen new --outfile {self.keypair_path}"
            )
        
        # Load Solana keypair format (JSON array of 64 bytes)
        try:
            with open(self.keypair_path, 'r') as f:
                data = json.load(f)
            
            if isinstance(data, list) and len(data) >= 32:
                # Take first 32 bytes as seed (standard Solana format)
                seed = bytes(data[:32])
                self.signing_key = SigningKey(seed)
            else:
                raise ValueError("Invalid keypair format - expected JSON array of 64 bytes")
                
        except json.JSONDecodeError as e:
            raise NukezError(f"Invalid JSON in keypair file {self.keypair_path}: {e}")
        except Exception as e:
            raise NukezError(f"Failed to load keypair from {self.keypair_path}: {e}")
    
    @property
    def pubkey_b58(self) -> str:
        """Get public key as Base58 string (Solana address format)."""
        return base58.b58encode(self.signing_key.verify_key.encode()).decode()
    
    def sign_message(self, message: bytes) -> str:
        """
        Sign message bytes and return Base58-encoded signature.
        
        Args:
            message: Bytes to sign
            
        Returns:
            Base58-encoded signature string
        """
        signature = self.signing_key.sign(message).signature
        return base58.b58encode(signature).decode()


def compute_locker_id(receipt_id: str) -> str:
    """
    Compute locker_id from receipt_id using the canonical pattern.
    
    This is the EXACT formula used by the Nukez backend.
    DO NOT MODIFY without matching server-side changes.
    
    Args:
        receipt_id: Receipt ID from confirm_storage()
        
    Returns:
        Locker ID string in format "locker_" + 12 hex chars
        
    Example:
        >>> compute_locker_id("receipt_abc123")
        'locker_7f3d2a1b9c8e'
        
    Note:
        This is deterministic - same receipt_id always produces same locker_id.
        The mapping is ONE-WAY (cannot reverse locker_id back to receipt_id).
    """
    return "locker_" + hashlib.sha256(receipt_id.encode()).hexdigest()[:12]


def build_signed_envelope(
    keypair: Keypair,
    receipt_id: str,
    method: str,
    path: str,
    ops: List[str],
    body: Optional[Union[Dict, str]] = None,
    ttl_seconds: int = 300
) -> SignedEnvelope:
    """
    Build signed envelope for Nukez API authentication.
    
    CRITICAL: This implementation MUST match the server's expectations exactly.
    The envelope structure, field names, and canonicalization are all verified
    server-side. Any deviation causes authentication failure.
    
    Extracted from nukez GenericAgentTools - proven with 70-98% success
    across 8+ AI models in autonomous testing.
    
    Args:
        keypair: Keypair for signing
        receipt_id: Receipt ID from confirm_storage()
        method: HTTP method (GET, POST, PUT, DELETE)
        path: API path (e.g., "/v1/lockers/{id}/files")
        ops: Required operations (e.g., ["locker:write"])
        body: Request body dict for POST/PUT (will be canonicalized)
        ttl_seconds: Envelope validity duration (default 5 minutes)
        
    Returns:
        SignedEnvelope with headers and canonical body
        
    Raises:
        NukezError: If body missing for POST/PUT or invalid JSON
        
    Critical implementation notes (DO NOT CHANGE):
        1. Envelope MUST include "v": 1 version field
        2. Field name is "receipt_id" not "sub"
        3. Field name is "body_sha256" not "body_hash"
        4. Nonce uses os.urandom(16).hex() for cryptographic randomness
        5. JSON canonicalization: separators=(',', ':'), sort_keys=True
        6. Signature is Base58-encoded (not Base64)
        7. Envelope is Base64URL-encoded with padding stripped
    """
    # Compute locker_id from receipt_id
    locker_id = compute_locker_id(receipt_id)
    
    # Handle body - CRITICAL for signature verification
    canonical_body = None
    body_sha256 = None
    
    if body is not None:
        # Support both dict and string inputs (fallback tolerance for agents)
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                raise NukezError(
                    "build_signed_envelope: 'body' must be valid JSON if provided as string. "
                    f"Received: {body[:100]}..."
                )
        
        # Canonical JSON - EXACT pattern from proven nukez implementation
        # This MUST match: json.dumps(body, separators=(',', ':'), sort_keys=True)
        canonical_body = json.dumps(body, separators=(',', ':'), sort_keys=True)
        body_sha256 = hashlib.sha256(canonical_body.encode('utf-8')).hexdigest()
    
    # POST/PUT MUST have body for signature verification
    if method.upper() in ('POST', 'PUT') and canonical_body is None:
        raise NukezError(
            f"build_signed_envelope: 'body' parameter is REQUIRED for {method} requests. "
            "The server verifies the signature covers the request body. "
            "Pass body={} for empty body if needed."
        )
    
    # For GET/DELETE, compute hash of empty string
    if method.upper() in ('GET', 'DELETE'):
        body_sha256 = hashlib.sha256(b'').hexdigest()
    
    # Current timestamp
    now = int(time.time())
    
    # Build envelope - EXACT structure from proven implementation
    # WARNING: Field names and structure are verified server-side
    envelope = {
        "v": 1,                              # Version field - REQUIRED
        "locker_id": locker_id,
        "receipt_id": receipt_id,            # NOT "sub"
        "nonce": os.urandom(16).hex(),       # Cryptographic randomness
        "iat": now,                          # Issued at
        "exp": now + ttl_seconds,            # Expiration
        "ops": ops,                          # Required operations
        "method": method.upper(),
        "path": path,
        "body_sha256": body_sha256,          # NOT "body_hash"
        "signer": keypair.pubkey_b58,        # Operator delegation (ADR-3)
    }
    
    # Canonicalize envelope for signing - MUST use exact same pattern
    envelope_json = json.dumps(envelope, separators=(',', ':'), sort_keys=True)
    
    # Sign the canonical envelope bytes
    signature = keypair.sign_message(envelope_json.encode('utf-8'))
    
    # Encode envelope for header - Base64URL without padding
    envelope_b64 = base64.urlsafe_b64encode(
        envelope_json.encode('utf-8')
    ).decode().rstrip('=')
    
    # Build headers
    headers = {
        "X-Nukez-Envelope": envelope_b64,
        "X-Nukez-Signature": signature  # Base58-encoded
    }
    
    return SignedEnvelope(
        headers=headers,
        canonical_body=canonical_body,
        locker_id=locker_id
    )
