# tests/test_auth.py
"""
Batch 4B: SDK auth tests — compute_locker_id, signed envelopes.
"""
import hashlib
import os
import json
import tempfile

import pytest
from pynukez.auth import compute_locker_id, Keypair, build_signed_envelope


class TestComputeLockerId:
    """Deterministic locker ID computation."""

    def test_deterministic(self):
        """Same receipt_id always produces same locker_id."""
        lid1 = compute_locker_id("test_receipt")
        lid2 = compute_locker_id("test_receipt")
        assert lid1 == lid2

    def test_prefix(self):
        """Locker ID starts with 'locker_'."""
        lid = compute_locker_id("test_receipt")
        assert lid.startswith("locker_")

    def test_different_receipts_different_lockers(self):
        """Different receipt_ids produce different locker_ids."""
        lid1 = compute_locker_id("receipt_a")
        lid2 = compute_locker_id("receipt_b")
        assert lid1 != lid2

    def test_hash_length(self):
        """Locker ID hash portion is 12 hex chars."""
        lid = compute_locker_id("test")
        hash_part = lid[len("locker_"):]
        assert len(hash_part) == 12
        # Verify it's valid hex
        int(hash_part, 16)


class TestKeypair:
    """Keypair loading and signing."""

    @pytest.fixture
    def keypair_path(self):
        """Create a test keypair file (Solana format: JSON array of 64 bytes)."""
        path = os.path.expanduser("~/.config/solana/id.json")
        if os.path.exists(path):
            return path
        pytest.skip("No Solana keypair available at ~/.config/solana/id.json")

    def test_load_keypair(self, keypair_path):
        """Can load keypair from file."""
        kp = Keypair(keypair_path)
        assert kp.pubkey_b58 is not None
        assert len(kp.pubkey_b58) > 20  # Base58 pubkey is ~44 chars

    def test_sign_message(self, keypair_path):
        """Can sign a message and get a signature."""
        kp = Keypair(keypair_path)
        sig = kp.sign_message(b"test message")
        assert isinstance(sig, str)
        assert len(sig) > 40  # Base58 signature is ~88 chars


class TestBuildSignedEnvelope:
    """Signed envelope construction."""

    @pytest.fixture
    def keypair(self):
        path = os.path.expanduser("~/.config/solana/id.json")
        if os.path.exists(path):
            return Keypair(path)
        pytest.skip("No Solana keypair available")

    def test_get_envelope_has_headers(self, keypair):
        """GET envelope includes required headers."""
        env = build_signed_envelope(
            keypair=keypair,
            receipt_id="test_rid",
            method="GET",
            path="/v1/lockers/locker_test/files",
            ops=["locker:list"],
        )
        assert "X-Nukez-Envelope" in env.headers
        assert "X-Nukez-Signature" in env.headers

    def test_post_envelope_with_body(self, keypair):
        """POST envelope includes canonical body hash."""
        env = build_signed_envelope(
            keypair=keypair,
            receipt_id="test_rid",
            method="POST",
            path="/v1/files/confirm",
            ops=["file:confirm"],
            body={"filename": "test.txt"},
        )
        assert env.canonical_body is not None
