"""
Regression tests for bind_receipt + hardened _require_signer / _is_delegating.

These tests codify the spec for cross-session / cold-cache workflows that was
missing prior to the client.py:283 / _require_signer fix. They pin:

  1. bind_receipt(Receipt(...)) primes both owner_identity and sig_alg.
  2. bind_receipt(receipt_id=..., owner_identity="0x...") infers secp256k1.
  3. bind_receipt(receipt_id=..., owner_identity="<base58>") infers ed25519.
  4. Dual-key client + cold cache + admin op → ReceiptStateNotBoundError.
  5. Single-key client + cold cache + admin op → works (regression guard).
  6. bind_receipt(receipt_id=...) alone → raises (nothing to prime).
  7. Conflicting re-bind (different owner_identity) → raises.
"""
import pytest
from unittest.mock import MagicMock, patch

from pynukez.client import Nukez
from pynukez.auth import _ReceiptState, infer_sig_alg
from pynukez.errors import NukezError, ReceiptStateNotBoundError
from pynukez.types import Receipt


REAL_ED25519 = "BhBeSkwKyqysZstzkqdf4qAcYfS9r27wEMmouvSVfp1U"
REAL_EVM = "0xc12e3657ce2ede7fae1d6f5a83b386f6a630fd18"


class TestInferSigAlg:
    """The pure format-based inference helper mirrors gateway identity.py."""

    def test_infer_evm_address(self):
        assert infer_sig_alg(REAL_EVM) == "secp256k1"

    def test_infer_base58_ed25519(self):
        assert infer_sig_alg(REAL_ED25519) == "ed25519"

    def test_infer_empty(self):
        assert infer_sig_alg("") is None

    def test_infer_junk(self):
        assert infer_sig_alg("not-a-key") is None

    def test_infer_0x_prefix_anchored_first(self):
        """0x-prefix check runs before base58, so adversarial non-hex 0x strings
        are still rejected (infer returns None) rather than mis-classified."""
        # Valid 42-char 0x-prefixed but with uppercase mix — matches regex.
        assert infer_sig_alg("0x" + "A" * 40) == "secp256k1"
        # 0x-prefixed with non-hex chars — regex rejects, then base58 also fails.
        assert infer_sig_alg("0xZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ") is None


class TestBindReceiptHappyPath:
    """Test 1: bind_receipt with a Receipt dataclass primes both fields."""

    @patch("pynukez.client.Keypair")
    def test_bind_with_receipt_dataclass(self, mock_kp):
        mock_kp.return_value.identity = REAL_ED25519
        mock_kp.return_value.sig_alg = "ed25519"
        client = Nukez(keypair_path="~/.config/solana/id.json")

        r = Receipt(
            id="rcpt_abc",
            units=1,
            payer_pubkey=REAL_EVM,
            network="eip155:143",
            sig_alg="secp256k1",
        )
        client.bind_receipt(r)
        state = client._receipt_state["rcpt_abc"]
        assert state.owner_identity == REAL_EVM
        assert state.sig_alg == "secp256k1"


class TestBindReceiptInference:
    """Tests 2 & 3: inference from owner_identity format when sig_alg omitted."""

    @patch("pynukez.client.Keypair")
    def test_bind_infers_secp256k1_from_0x(self, mock_kp):
        mock_kp.return_value.identity = REAL_ED25519
        client = Nukez(keypair_path="~/.config/solana/id.json")
        client.bind_receipt(receipt_id="rcpt_evm", owner_identity=REAL_EVM)
        assert client._receipt_state["rcpt_evm"].sig_alg == "secp256k1"

    @patch("pynukez.client.Keypair")
    def test_bind_infers_ed25519_from_base58(self, mock_kp):
        mock_kp.return_value.identity = REAL_ED25519
        client = Nukez(keypair_path="~/.config/solana/id.json")
        client.bind_receipt(receipt_id="rcpt_svm", owner_identity=REAL_ED25519)
        assert client._receipt_state["rcpt_svm"].sig_alg == "ed25519"


class TestDualKeyColdCache:
    """Test 4: dual-key client + cold cache on admin op → ReceiptStateNotBoundError."""

    @patch("pynukez.client.Keypair")
    def test_add_operator_cold_cache_raises(self, mock_kp):
        mock_kp.return_value.identity = REAL_ED25519
        mock_kp.return_value.sig_alg = "ed25519"
        client = Nukez(keypair_path="~/.config/solana/id.json")
        # Stub dual-key by attaching an EVM signer after construction.
        client._evm_signer = MagicMock()
        client._evm_signer.identity = REAL_EVM

        with pytest.raises(ReceiptStateNotBoundError) as exc:
            client._require_signer("add_operator", "cold_receipt_xyz")
        assert exc.value.receipt_id == "cold_receipt_xyz"
        assert exc.value.operation == "add_operator"
        # Error message guides the user to bind_receipt.
        assert "bind_receipt" in str(exc.value)

    @patch("pynukez.client.Keypair")
    def test_is_delegating_dual_key_cold_raises(self, mock_kp):
        mock_kp.return_value.identity = REAL_ED25519
        client = Nukez(keypair_path="~/.config/solana/id.json")
        client._evm_signer = MagicMock()
        client._evm_signer.identity = REAL_EVM
        with pytest.raises(ReceiptStateNotBoundError):
            client._is_delegating("cold_receipt_xyz")


class TestSingleKeyColdCacheUnchanged:
    """Test 5: single-key client with cold cache — regression guard for legacy behavior."""

    @patch("pynukez.client.Keypair")
    def test_single_key_require_signer_cold(self, mock_kp):
        mock_kp.return_value.identity = REAL_ED25519
        mock_kp.return_value.sig_alg = "ed25519"
        client = Nukez(keypair_path="~/.config/solana/id.json")
        assert client._evm_signer is None
        # Cold receipt → no raise, returns default signer (unchanged behavior).
        signer = client._require_signer("add_operator", "cold_receipt")
        assert signer is client._signer

    @patch("pynukez.client.Keypair")
    def test_single_key_is_delegating_cold_returns_true(self, mock_kp):
        mock_kp.return_value.identity = REAL_ED25519
        client = Nukez(keypair_path="~/.config/solana/id.json")
        assert client._evm_signer is None
        # Legacy "default True" fallback preserved for single-key clients.
        assert client._is_delegating("cold_receipt") is True


class TestBindReceiptRequiresContent:
    """Test 6: bind_receipt(receipt_id=...) alone must raise — nothing to prime."""

    @patch("pynukez.client.Keypair")
    def test_bind_receipt_id_only_raises(self, mock_kp):
        mock_kp.return_value.identity = REAL_ED25519
        client = Nukez(keypair_path="~/.config/solana/id.json")
        with pytest.raises(NukezError, match="requires owner_identity or sig_alg"):
            client.bind_receipt(receipt_id="empty_rcpt")

    @patch("pynukez.client.Keypair")
    def test_bind_no_args_raises(self, mock_kp):
        mock_kp.return_value.identity = REAL_ED25519
        client = Nukez(keypair_path="~/.config/solana/id.json")
        with pytest.raises(NukezError, match="requires receipt_id"):
            client.bind_receipt()

    @patch("pynukez.client.Keypair")
    def test_bind_uninferable_owner_without_sig_alg_raises(self, mock_kp):
        """owner_identity that neither infers to a known sig_alg nor has one supplied → raise."""
        mock_kp.return_value.identity = REAL_ED25519
        client = Nukez(keypair_path="~/.config/solana/id.json")
        with pytest.raises(NukezError, match="cannot determine sig_alg"):
            client.bind_receipt(receipt_id="rcpt", owner_identity="not-a-real-key")


class TestBindReceiptConflict:
    """Test 7: conflicting re-bind raises; idempotent re-bind succeeds."""

    @patch("pynukez.client.Keypair")
    def test_conflict_different_owner_raises(self, mock_kp):
        mock_kp.return_value.identity = REAL_ED25519
        client = Nukez(keypair_path="~/.config/solana/id.json")
        client.bind_receipt(receipt_id="rcpt", owner_identity=REAL_EVM)
        other_evm = "0x" + "d" * 40
        with pytest.raises(NukezError, match="already bound to owner"):
            client.bind_receipt(receipt_id="rcpt", owner_identity=other_evm)

    @patch("pynukez.client.Keypair")
    def test_conflict_different_sig_alg_raises(self, mock_kp):
        mock_kp.return_value.identity = REAL_ED25519
        client = Nukez(keypair_path="~/.config/solana/id.json")
        client.bind_receipt(receipt_id="rcpt", owner_identity=REAL_EVM)
        # Same receipt, same owner (sort of — we override sig_alg only)
        with pytest.raises(NukezError, match="already bound with sig_alg"):
            client.bind_receipt(
                receipt_id="rcpt",
                owner_identity=REAL_EVM,
                sig_alg="ed25519",
            )

    @patch("pynukez.client.Keypair")
    def test_idempotent_rebind_same_values(self, mock_kp):
        mock_kp.return_value.identity = REAL_ED25519
        client = Nukez(keypair_path="~/.config/solana/id.json")
        client.bind_receipt(receipt_id="rcpt", owner_identity=REAL_EVM)
        # Rebinding with identical values must not raise.
        client.bind_receipt(receipt_id="rcpt", owner_identity=REAL_EVM)
        client.bind_receipt(
            receipt_id="rcpt",
            owner_identity=REAL_EVM,
            sig_alg="secp256k1",
        )
        assert client._receipt_state["rcpt"].owner_identity == REAL_EVM
        assert client._receipt_state["rcpt"].sig_alg == "secp256k1"


class TestBindReceiptNotebookScenario:
    """End-to-end regression for the EVM_SVM_Owners.ipynb cold-cache bug.

    Original failure mode: dual-key client + EVM-paid receipt + kernel restart
    (or client constructor re-run) → _require_signer silently picks the
    Ed25519 signer → gateway rejects with "sig_alg 'ed25519' incompatible with
    EVM network". After bind_receipt, the dual-key client picks the EVM signer
    on EVM receipts and the Ed25519 signer on SOL receipts automatically.
    """

    @patch("pynukez.client.Keypair")
    def test_cold_dual_key_recovers_via_bind_receipt(self, mock_kp):
        mock_kp.return_value.identity = REAL_ED25519
        mock_kp.return_value.sig_alg = "ed25519"
        client = Nukez(keypair_path="~/.config/solana/id.json")
        # Attach EVM signer to simulate dual-key post-construction.
        client._evm_signer = MagicMock()
        client._evm_signer.identity = REAL_EVM

        # Cold call — should raise with the exact error we added.
        with pytest.raises(ReceiptStateNotBoundError):
            client._require_signer("remove_operator", "notebook_rcpt")

        # User loads/reconstructs a Receipt and binds it.
        r = Receipt(
            id="notebook_rcpt",
            units=1,
            payer_pubkey=REAL_EVM,
            network="eip155:143",
            sig_alg="secp256k1",
        )
        client.bind_receipt(r)

        # Now the EVM signer is selected for the admin op.
        picked = client._require_signer("remove_operator", "notebook_rcpt")
        assert picked is client._evm_signer

    @patch("pynukez.client.Keypair")
    def test_dual_key_svm_receipt_picks_ed25519(self, mock_kp):
        mock_kp.return_value.identity = REAL_ED25519
        mock_kp.return_value.sig_alg = "ed25519"
        client = Nukez(keypair_path="~/.config/solana/id.json")
        client._evm_signer = MagicMock()
        client._evm_signer.identity = REAL_EVM

        r = Receipt(
            id="svm_rcpt",
            units=1,
            payer_pubkey=REAL_ED25519,
            network="solana:mainnet",
            sig_alg="ed25519",
        )
        client.bind_receipt(r)
        picked = client._require_signer("upload", "svm_rcpt")
        assert picked is client._signer
