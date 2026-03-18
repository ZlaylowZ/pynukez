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


class TestBuildSignedEnvelopeGeneralized:
    """Phase 2B: generalized build_signed_envelope with Signer protocol."""

    @pytest.fixture
    def keypair(self):
        path = os.path.expanduser("~/.config/solana/id.json")
        if os.path.exists(path):
            return Keypair(path)
        pytest.skip("No Solana keypair available")

    @pytest.fixture
    def evm_signer(self):
        try:
            from pynukez.signer import EVMSigner
            return EVMSigner(
                private_key="0x4c0883a69102937d6231471b5dbb6204fe512961708279f9d92f2e20d8c563b6"
            )
        except ImportError:
            pytest.skip("eth_account not installed")

    def _decode_envelope(self, env):
        """Decode envelope JSON from headers."""
        import base64 as b64
        raw = env.headers["X-Nukez-Envelope"]
        # Restore padding
        raw += "=" * (-len(raw) % 4)
        return json.loads(b64.urlsafe_b64decode(raw))

    def test_ed25519_sig_alg_in_envelope(self, keypair):
        """Ed25519 envelope includes sig_alg: ed25519."""
        env = build_signed_envelope(
            signer=keypair,
            receipt_id="test_rid",
            method="GET",
            path="/v1/lockers/locker_test/files",
            ops=["locker:list"],
        )
        decoded = self._decode_envelope(env)
        assert decoded["sig_alg"] == "ed25519"

    def test_evm_sig_alg_in_envelope(self, evm_signer):
        """EVM envelope includes sig_alg: secp256k1."""
        env = build_signed_envelope(
            signer=evm_signer,
            receipt_id="test_rid",
            method="GET",
            path="/v1/lockers/locker_test/files",
            ops=["locker:list"],
        )
        decoded = self._decode_envelope(env)
        assert decoded["sig_alg"] == "secp256k1"

    def test_evm_signature_is_0x_hex(self, evm_signer):
        """EVM envelope signature is 0x-prefixed hex."""
        env = build_signed_envelope(
            signer=evm_signer,
            receipt_id="test_rid",
            method="GET",
            path="/v1/lockers/locker_test/files",
            ops=["locker:list"],
        )
        sig = env.headers["X-Nukez-Signature"]
        assert sig.startswith("0x")
        assert len(sig) == 132

    def test_ed25519_signature_is_base58(self, keypair):
        """Ed25519 envelope signature is base58 (regression)."""
        env = build_signed_envelope(
            signer=keypair,
            receipt_id="test_rid",
            method="GET",
            path="/v1/lockers/locker_test/files",
            ops=["locker:list"],
        )
        sig = env.headers["X-Nukez-Signature"]
        assert not sig.startswith("0x")
        assert len(sig) > 40

    def test_deprecated_keypair_kwarg(self, keypair):
        """keypair= kwarg still works (backward compat)."""
        env = build_signed_envelope(
            keypair=keypair,
            receipt_id="test_rid",
            method="GET",
            path="/v1/lockers/locker_test/files",
            ops=["locker:list"],
        )
        assert "X-Nukez-Envelope" in env.headers

    def test_no_signer_raises(self):
        """No signer or keypair raises NukezError."""
        from pynukez.errors import NukezError
        with pytest.raises(NukezError, match="requires a signer"):
            build_signed_envelope(
                receipt_id="test_rid",
                method="GET",
                path="/test",
                ops=["locker:list"],
            )

    def test_delegating_false_no_signer_field(self, keypair):
        """delegating=False omits signer from envelope."""
        env = build_signed_envelope(
            signer=keypair,
            receipt_id="test_rid",
            method="GET",
            path="/v1/lockers/locker_test/files",
            ops=["locker:list"],
            delegating=False,
        )
        decoded = self._decode_envelope(env)
        assert "signer" not in decoded

    def test_delegating_true_includes_signer_field(self, keypair):
        """delegating=True includes signer identity in envelope."""
        env = build_signed_envelope(
            signer=keypair,
            receipt_id="test_rid",
            method="GET",
            path="/v1/lockers/locker_test/files",
            ops=["locker:list"],
            delegating=True,
        )
        decoded = self._decode_envelope(env)
        assert decoded["signer"] == keypair.identity

    def test_sig_alg_always_present(self, keypair):
        """sig_alg is always present in envelope."""
        env = build_signed_envelope(
            signer=keypair,
            receipt_id="test_rid",
            method="GET",
            path="/v1/lockers/locker_test/files",
            ops=["locker:list"],
        )
        decoded = self._decode_envelope(env)
        assert "sig_alg" in decoded
