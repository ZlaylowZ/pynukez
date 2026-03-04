# tests/test_types.py
"""
Batch 4B: SDK types tests — validates data model correctness.

Tests all dataclasses, including expanded FileInfo fields (Phase 2).
"""
import pytest
from pynukez.types import (
    StorageRequest, Receipt, FileUrls, VerificationResult,
    PriceInfo, PaymentOption, TransferResult, NukezManifest,
    FileInfo, ViewerLink, FileViewerInfo, ViewerFileList,
    UploadResult, DeleteResult, WalletInfo, ConfirmResult,
    BatchConfirmResult, AttestResult, BatchUploadResult,
    DownloadedFile, BatchDownloadResult, DiscoveryDoc,
    ProviderInfo, ViewerContainer,
)


class TestStorageRequest:
    """StorageRequest dataclass tests."""

    def test_basic_construction(self):
        sr = StorageRequest(
            pay_req_id="pr_1",
            pay_to_address="addr1",
            amount_sol=0.001,
            amount_lamports=1000000,
            network="solana-devnet",
            units=1,
        )
        assert sr.pay_req_id == "pr_1"
        assert sr.amount_sol == 0.001
        assert sr.units == 1

    def test_is_evm_solana(self):
        """Solana network should NOT be EVM."""
        sr = StorageRequest(
            pay_req_id="pr_1", pay_to_address="addr1",
            amount_sol=0.001, amount_lamports=1000000,
            network="solana-devnet", units=1,
        )
        assert sr.is_evm is False

    def test_is_evm_monad(self):
        """Monad network should be EVM."""
        sr = StorageRequest(
            pay_req_id="pr_1", pay_to_address="addr1",
            amount_sol=0.0, amount_lamports=0,
            network="monad-testnet", units=1,
        )
        assert sr.is_evm is True

    def test_next_step_default(self):
        """Default next_step guides agent."""
        sr = StorageRequest(
            pay_req_id="pr_1", pay_to_address="addr1",
            amount_sol=0.001, amount_lamports=1000000,
            network="solana-devnet", units=1,
        )
        assert "solana_transfer" in sr.next_step or "confirm_storage" in sr.next_step


class TestFileInfo:
    """FileInfo dataclass tests — includes Phase 2 expanded fields."""

    def test_basic_construction(self):
        fi = FileInfo(filename="test.txt", content_type="text/plain")
        assert fi.filename == "test.txt"
        assert fi.content_type == "text/plain"

    def test_size_bytes_default(self):
        """size_bytes defaults to 0."""
        fi = FileInfo(filename="test.txt", content_type="text/plain")
        assert fi.size_bytes == 0

    def test_content_hash_default(self):
        """content_hash defaults to None."""
        fi = FileInfo(filename="test.txt", content_type="text/plain")
        assert fi.content_hash is None

    def test_provider_ref_default(self):
        """provider_ref defaults to None."""
        fi = FileInfo(filename="test.txt", content_type="text/plain")
        assert fi.provider_ref is None

    def test_expanded_fields_populated(self):
        """All three new fields can be populated."""
        fi = FileInfo(
            filename="test.txt",
            content_type="text/plain",
            size_bytes=1024,
            content_hash="sha256:abc123",
            provider_ref="ar_txid_123",
        )
        assert fi.size_bytes == 1024
        assert fi.content_hash == "sha256:abc123"
        assert fi.provider_ref == "ar_txid_123"

    def test_zero_size_bytes(self):
        """size_bytes=0 is valid (not None/missing)."""
        fi = FileInfo(filename="empty.txt", content_type="text/plain", size_bytes=0)
        assert fi.size_bytes == 0
        assert fi.size_bytes is not None


class TestVerificationResult:
    """VerificationResult dataclass tests."""

    def test_verified_true(self):
        vr = VerificationResult(
            receipt_id="r1", verified=True, result_hash="abc",
        )
        assert vr.verified is True

    def test_verified_false(self):
        vr = VerificationResult(
            receipt_id="r1", verified=False, result_hash="abc",
        )
        assert vr.verified is False


class TestConfirmResult:
    """ConfirmResult dataclass tests."""

    def test_fields(self):
        cr = ConfirmResult(
            filename="test.txt",
            content_hash="sha256:abc",
            size_bytes=100,
            confirmed=True,
        )
        assert cr.filename == "test.txt"
        assert cr.size_bytes == 100
        assert cr.confirmed is True


class TestDataclassCount:
    """Verify total dataclass count matches expectations."""

    def test_dataclass_count(self):
        """Should have at least 17 public dataclasses."""
        import inspect
        import dataclasses
        import pynukez.types as types_module

        dataclass_names = [
            name for name, obj in inspect.getmembers(types_module)
            if inspect.isclass(obj) and dataclasses.is_dataclass(obj)
        ]
        assert len(dataclass_names) >= 17, f"Found {len(dataclass_names)}: {dataclass_names}"
