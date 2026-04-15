# tests/test_signer.py
"""
Phase 2A: Signer protocol + EVMSigner + Keypair retrofit tests.
"""
import json
import os
import tempfile

import pytest

from pynukez.signer import Signer, EVMSigner


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Test EVM private key (DO NOT use in production — this is a well-known test key)
TEST_EVM_PRIVATE_KEY = "0x4c0883a69102937d6231471b5dbb6204fe512961708279f9d92f2e20d8c563b6"
# Derived address for the above key (lowercase)
TEST_EVM_ADDRESS = None  # computed in fixture


@pytest.fixture
def evm_signer():
    """Create an EVMSigner from a test private key.

    eth_account is a core pynukez dependency, so this should always succeed
    on a correctly installed package. The try/except remains as a safety net
    for broken environments where eth_account was somehow uninstalled
    independently of pynukez.
    """
    try:
        return EVMSigner(private_key=TEST_EVM_PRIVATE_KEY)
    except ImportError:
        pytest.skip("eth_account not importable — try reinstalling pynukez")


@pytest.fixture
def evm_key_file(evm_signer):
    """Create a temporary EVM key JSON file."""
    data = {
        "address": evm_signer.identity,
        "private_key": TEST_EVM_PRIVATE_KEY,
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        path = f.name
    yield path
    os.unlink(path)


@pytest.fixture
def keypair():
    """Load a Solana keypair if available."""
    path = os.path.expanduser("~/.config/solana/id.json")
    if not os.path.exists(path):
        pytest.skip("No Solana keypair at ~/.config/solana/id.json")
    from pynukez.auth import Keypair
    return Keypair(path)


# ---------------------------------------------------------------------------
# EVMSigner tests
# ---------------------------------------------------------------------------

class TestEVMSigner:
    """EVMSigner construction and signing."""

    def test_identity_is_lowercase_0x(self, evm_signer):
        """identity returns lowercase 0x-prefixed address."""
        assert evm_signer.identity.startswith("0x")
        assert evm_signer.identity == evm_signer.identity.lower()
        assert len(evm_signer.identity) == 42

    def test_sig_alg(self, evm_signer):
        assert evm_signer.sig_alg == "secp256k1"

    def test_sign_returns_0x_hex(self, evm_signer):
        """sign() returns 0x-prefixed hex string."""
        sig = evm_signer.sign(b"hello world")
        assert isinstance(sig, str)
        assert sig.startswith("0x")
        # EIP-191 personal_sign produces 65-byte signature -> 130 hex chars + "0x" = 132
        assert len(sig) == 132

    def test_sign_recoverable(self, evm_signer):
        """Signature is recoverable to the correct address."""
        from eth_account import Account
        from eth_account.messages import encode_defunct

        message = b"test message for recovery"
        sig = evm_signer.sign(message)

        msg_obj = encode_defunct(primitive=message)
        recovered = Account.recover_message(msg_obj, signature=sig)
        assert recovered.lower() == evm_signer.identity

    def test_mismatched_address_raises(self):
        """ValueError if provided address doesn't match derived."""
        try:
            from eth_account import Account
        except ImportError:
            pytest.skip("eth_account not installed")

        with pytest.raises(ValueError, match="does not match"):
            EVMSigner(
                private_key=TEST_EVM_PRIVATE_KEY,
                address="0x0000000000000000000000000000000000000000",
            )

    def test_from_file(self, evm_key_file, evm_signer):
        """from_file loads correctly from JSON."""
        loaded = EVMSigner.from_file(evm_key_file)
        assert loaded.identity == evm_signer.identity
        assert loaded.sig_alg == "secp256k1"

    def test_from_file_not_found(self):
        """from_file raises NukezError for missing file."""
        from pynukez.errors import NukezError
        with pytest.raises(NukezError, match="not found"):
            EVMSigner.from_file("/nonexistent/path.json")

    def test_from_file_no_private_key(self):
        """from_file raises ValueError if no private_key in JSON."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"address": "0x1234"}, f)
            path = f.name
        try:
            with pytest.raises(ValueError, match="No private_key"):
                EVMSigner.from_file(path)
        finally:
            os.unlink(path)

    def test_isinstance_signer(self, evm_signer):
        """EVMSigner satisfies the Signer protocol."""
        assert isinstance(evm_signer, Signer)


# ---------------------------------------------------------------------------
# Keypair Signer protocol compliance
# ---------------------------------------------------------------------------

class TestKeypairSignerProtocol:
    """Keypair satisfies the Signer protocol after retrofit."""

    def test_identity_equals_pubkey_b58(self, keypair):
        assert keypair.identity == keypair.pubkey_b58

    def test_sig_alg(self, keypair):
        assert keypair.sig_alg == "ed25519"

    def test_sign_equals_sign_message(self, keypair):
        msg = b"test message"
        assert keypair.sign(msg) == keypair.sign_message(msg)

    def test_isinstance_signer(self, keypair):
        """Keypair satisfies the Signer protocol."""
        assert isinstance(keypair, Signer)

    def test_pubkey_b58_still_works(self, keypair):
        """pubkey_b58 is not broken by the retrofit."""
        assert len(keypair.pubkey_b58) > 20
        assert isinstance(keypair.pubkey_b58, str)

    def test_sign_message_still_works(self, keypair):
        """sign_message is not broken by the retrofit."""
        sig = keypair.sign_message(b"test")
        assert isinstance(sig, str)
        assert len(sig) > 20
