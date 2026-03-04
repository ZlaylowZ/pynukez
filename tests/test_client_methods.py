# tests/test_client_methods.py
"""
Batch 4D: SDK client method tests — verifies method signatures and public API surface.
"""
import pytest
from unittest.mock import MagicMock, patch
from pynukez.client import Nukez


class TestClientInit:
    """Client initialization tests."""

    @patch("pynukez.client.Keypair")
    def test_default_base_url(self, mock_kp):
        """Client defaults to nukez API URL."""
        client = Nukez(keypair_path="~/.config/solana/id.json")
        assert "nukez" in client.http.base_url.lower()

    @patch("pynukez.client.Keypair")
    def test_custom_base_url(self, mock_kp):
        """Custom base URL is respected."""
        client = Nukez(
            keypair_path="~/.config/solana/id.json",
            base_url="https://custom.example.com",
        )
        assert client.http.base_url == "https://custom.example.com"

    @patch("pynukez.client.Keypair")
    def test_default_network(self, mock_kp):
        """Default network is devnet."""
        client = Nukez(keypair_path="~/.config/solana/id.json")
        assert client.network == "devnet"


class TestClientPublicMethods:
    """Verify the public API surface exists."""

    @patch("pynukez.client.Keypair")
    def test_has_storage_flow_methods(self, mock_kp):
        """Client has get_price, request_storage, confirm_storage."""
        client = Nukez(keypair_path="~/.config/solana/id.json")
        for method in ("get_price", "request_storage", "confirm_storage"):
            assert hasattr(client, method), f"Missing method: {method}"

    @patch("pynukez.client.Keypair")
    def test_has_file_methods(self, mock_kp):
        """Client has create_file, upload_bytes, list_files, delete_file, confirm_file."""
        client = Nukez(keypair_path="~/.config/solana/id.json")
        for method in (
            "create_file",
            "upload_bytes",
            "list_files",
            "delete_file",
            "confirm_file",
        ):
            assert hasattr(client, method), f"Missing method: {method}"

    @patch("pynukez.client.Keypair")
    def test_has_provision_locker(self, mock_kp):
        """Client has provision_locker method."""
        client = Nukez(keypair_path="~/.config/solana/id.json")
        assert hasattr(client, "provision_locker")

    @patch("pynukez.client.Keypair")
    def test_has_verify_methods(self, mock_kp):
        """Client has verify_storage and get_merkle_proof."""
        client = Nukez(keypair_path="~/.config/solana/id.json")
        for method in ("verify_storage", "get_merkle_proof"):
            assert hasattr(client, method), f"Missing method: {method}"

    @patch("pynukez.client.Keypair")
    def test_has_batch_methods(self, mock_kp):
        """Client has batch operations."""
        client = Nukez(keypair_path="~/.config/solana/id.json")
        for method in ("confirm_files", "create_files_batch"):
            assert hasattr(client, method), f"Missing method: {method}"

    @patch("pynukez.client.Keypair")
    def test_has_download_methods(self, mock_kp):
        """Client has download_bytes and download_files."""
        client = Nukez(keypair_path="~/.config/solana/id.json")
        for method in ("download_bytes", "download_files"):
            assert hasattr(client, method), f"Missing method: {method}"


class TestClientFileInfoExpansion:
    """Verify list_files returns expanded FileInfo objects (Phase 2 Step 2.1)."""

    @patch("pynukez.client.build_signed_envelope")
    @patch("pynukez.client.Keypair")
    def test_list_files_returns_fileinfo_with_size(self, mock_kp_cls, mock_env):
        """list_files result includes size_bytes, content_hash, provider_ref."""
        from pynukez.types import FileInfo

        mock_env.return_value = MagicMock(
            headers={"X-Nukez-Envelope": "e", "X-Nukez-Signature": "s"}
        )
        client = Nukez(keypair_path="~/.config/solana/id.json")
        client.http = MagicMock()
        client.http.get.return_value = {
            "files": [
                {
                    "filename": "test.txt",
                    "size_bytes": 42,
                    "content_hash": "sha256:abc",
                    "provider_ref": "ref_123",
                    "content_type": "text/plain",
                }
            ]
        }

        files = client.list_files("test_receipt")

        assert len(files) == 1
        f = files[0]
        assert isinstance(f, FileInfo)
        assert f.size_bytes == 42
        assert f.content_hash == "sha256:abc"
        assert f.provider_ref == "ref_123"

    @patch("pynukez.client.build_signed_envelope")
    @patch("pynukez.client.Keypair")
    def test_list_files_defaults_missing_fields(self, mock_kp_cls, mock_env):
        """list_files handles missing optional fields gracefully."""
        from pynukez.types import FileInfo

        mock_env.return_value = MagicMock(
            headers={"X-Nukez-Envelope": "e", "X-Nukez-Signature": "s"}
        )
        client = Nukez(keypair_path="~/.config/solana/id.json")
        client.http = MagicMock()
        client.http.get.return_value = {
            "files": [{"filename": "bare.txt"}]
        }

        files = client.list_files("test_receipt")

        assert len(files) == 1
        f = files[0]
        assert f.size_bytes == 0
        assert f.content_hash is None
        assert f.provider_ref is None
