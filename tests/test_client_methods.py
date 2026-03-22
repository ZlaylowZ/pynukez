# tests/test_client_methods.py
"""
Batch 4D: SDK client method tests — verifies method signatures and public API surface.
"""
import pytest
from unittest.mock import MagicMock, patch, call
from pynukez.client import Nukez
from pynukez.errors import NukezError


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


class TestDownloadBytesRetry:
    """Verify download_bytes retries on 404 for content-addressed propagation."""

    @patch("pynukez.client.Keypair")
    def test_download_succeeds_first_try(self, mock_kp):
        """No retries when download succeeds immediately."""
        client = Nukez(keypair_path="~/.config/solana/id.json")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"hello world"
        mock_resp.raise_for_status = MagicMock()

        client._raw_client = MagicMock()
        client._raw_client.get = MagicMock(return_value=mock_resp)
        result = client.download_bytes("https://api.nukez.xyz/f/tok123")

        assert result == b"hello world"
        assert client._raw_client.get.call_count == 1

    @patch("pynukez.client.time.sleep")
    @patch("pynukez.client.Keypair")
    def test_download_retries_on_404_then_succeeds(self, mock_kp, mock_sleep):
        """Download retries on 404 and succeeds on second attempt."""
        client = Nukez(keypair_path="~/.config/solana/id.json")

        resp_404 = MagicMock()
        resp_404.status_code = 404

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.content = b"propagated data"
        resp_200.raise_for_status = MagicMock()

        client._raw_client = MagicMock()
        client._raw_client.get = MagicMock(side_effect=[resp_404, resp_200])
        result = client.download_bytes("https://api.nukez.xyz/f/tok123")

        assert result == b"propagated data"
        assert client._raw_client.get.call_count == 2
        mock_sleep.assert_called_once_with(2.0)

    @patch("pynukez.client.time.sleep")
    @patch("pynukez.client.Keypair")
    def test_download_exhausts_retries_raises(self, mock_kp, mock_sleep):
        """Download raises NukezError after all retries exhausted on 404."""
        from httpx import HTTPStatusError
        from unittest.mock import MagicMock as _MagicMock

        client = Nukez(keypair_path="~/.config/solana/id.json")

        mock_err_resp = MagicMock()
        mock_err_resp.status_code = 404
        last_err = HTTPStatusError(message="error", request=_MagicMock(), response=mock_err_resp)

        resp_404_ok = MagicMock()
        resp_404_ok.status_code = 404

        resp_404_final = MagicMock()
        resp_404_final.status_code = 404
        resp_404_final.raise_for_status.side_effect = last_err

        client._raw_client = MagicMock()
        client._raw_client.get = MagicMock(
            side_effect=[resp_404_ok, resp_404_ok, resp_404_ok, resp_404_final]
        )
        with pytest.raises(NukezError, match="404"):
            client.download_bytes("https://api.nukez.xyz/f/tok123")

        # 3 retries → sleeps at 2s, 4s, 8s
        assert mock_sleep.call_count == 3
        mock_sleep.assert_any_call(2.0)
        mock_sleep.assert_any_call(4.0)
        mock_sleep.assert_any_call(8.0)

    @patch("pynukez.client.Keypair")
    def test_download_no_retry_on_403(self, mock_kp):
        """403 (expired URL) does not trigger retry — raises immediately."""
        from httpx import HTTPStatusError
        from unittest.mock import MagicMock as _MagicMock

        client = Nukez(keypair_path="~/.config/solana/id.json")

        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.raise_for_status.side_effect = HTTPStatusError(message="error", request=_MagicMock(), response=mock_resp)

        client._raw_client = MagicMock()
        client._raw_client.get = MagicMock(return_value=mock_resp)
        with pytest.raises(NukezError, match="expired or malformed"):
            client.download_bytes("https://api.nukez.xyz/f/tok123")

        assert client._raw_client.get.call_count == 1

    @patch("pynukez.client.time.sleep")
    @patch("pynukez.client.Keypair")
    def test_download_retry_disabled_with_zero(self, mock_kp, mock_sleep):
        """max_retries=0 disables retry — single attempt only."""
        from httpx import HTTPStatusError
        from unittest.mock import MagicMock as _MagicMock

        client = Nukez(keypair_path="~/.config/solana/id.json")

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.raise_for_status.side_effect = HTTPStatusError(message="error", request=_MagicMock(), response=mock_resp)

        client._raw_client = MagicMock()
        client._raw_client.get = MagicMock(return_value=mock_resp)
        with pytest.raises(NukezError, match="404"):
            client.download_bytes(
                "https://api.nukez.xyz/f/tok123", max_retries=0
            )

        assert client._raw_client.get.call_count == 1
        mock_sleep.assert_not_called()

    @patch("pynukez.client.time.sleep")
    @patch("pynukez.client.Keypair")
    def test_download_exponential_backoff_timing(self, mock_kp, mock_sleep):
        """Verify exponential backoff doubles each retry: 2s, 4s, 8s."""
        client = Nukez(keypair_path="~/.config/solana/id.json")

        resp_404 = MagicMock()
        resp_404.status_code = 404

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.content = b"ok"
        resp_200.raise_for_status = MagicMock()

        client._raw_client = MagicMock()
        client._raw_client.get = MagicMock(
            side_effect=[resp_404, resp_404, resp_404, resp_200]
        )
        result = client.download_bytes("https://api.nukez.xyz/f/tok123")

        assert result == b"ok"
        assert mock_sleep.call_args_list == [call(2.0), call(4.0), call(8.0)]

    @patch("pynukez.client.time.sleep")
    @patch("pynukez.client.Keypair")
    def test_download_propagation_error_parsed_from_gateway(self, mock_kp, mock_sleep):
        """Gateway CONTENT_PROPAGATION_PENDING response is parsed into NukezError details."""
        from httpx import HTTPStatusError
        from unittest.mock import MagicMock as _MagicMock

        client = Nukez(keypair_path="~/.config/solana/id.json")

        gateway_body = {
            "error_code": "CONTENT_PROPAGATION_PENDING",
            "message": "File upload confirmed but data is not yet available.",
            "details": {
                "retryable": True,
                "provider": "arweave",
                "suggested_delay": 15,
                "filename": "test.txt",
            },
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.json.return_value = gateway_body
        mock_resp.raise_for_status.side_effect = HTTPStatusError(message="error", request=_MagicMock(), response=mock_resp)

        resp_404 = MagicMock()
        resp_404.status_code = 404

        client._raw_client = MagicMock()
        client._raw_client.get = MagicMock(
            side_effect=[resp_404, resp_404, resp_404, mock_resp]
        )
        with pytest.raises(NukezError) as exc_info:
            client.download_bytes("https://api.nukez.xyz/f/tok123")

        err = exc_info.value
        assert "arweave" in err.message
        assert "propagating" in err.message
        assert err.details.get("error_code") == "CONTENT_PROPAGATION_PENDING"
        assert err.details.get("provider") == "arweave"
        assert err.details.get("suggested_delay") == 15
        assert err.details.get("retryable") is True

    @patch("pynukez.client.time.sleep")
    @patch("pynukez.client.Keypair")
    def test_download_generic_404_when_no_json_body(self, mock_kp, mock_sleep):
        """Non-JSON 404 response falls back to generic propagation message."""
        from httpx import HTTPStatusError
        from unittest.mock import MagicMock as _MagicMock

        client = Nukez(keypair_path="~/.config/solana/id.json")

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.json.side_effect = ValueError("No JSON")
        mock_resp.raise_for_status.side_effect = HTTPStatusError(message="error", request=_MagicMock(), response=mock_resp)

        client._raw_client = MagicMock()
        client._raw_client.get = MagicMock(return_value=mock_resp)
        with pytest.raises(NukezError) as exc_info:
            client.download_bytes(
                "https://storage.googleapis.com/signed-url", max_retries=0
            )

        err = exc_info.value
        assert "404" in err.message
        assert "confirm_file" in err.message
        assert err.details.get("retryable") is True
        # Should NOT have propagation-specific fields
        assert "error_code" not in err.details


class TestUploadBytesContentType:
    """Verify upload_bytes Content-Type header behavior after requests→httpx migration."""

    @patch("pynukez.client.Keypair")
    def test_default_content_type_is_octet_stream(self, mock_kp):
        """upload_bytes defaults to application/octet-stream, matching create_file's default.

        GCS signed URLs include content-type in X-Goog-SignedHeaders.
        The Content-Type must match what create_file signed into the URL.
        create_file defaults to application/octet-stream, so upload_bytes must too.
        """
        client = Nukez(keypair_path="~/.config/solana/id.json")
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        client._raw_client = MagicMock()
        client._raw_client.put = MagicMock(return_value=mock_resp)

        client.upload_bytes("https://api.nukez.xyz/f/token", b"Hello!")

        _, kwargs = client._raw_client.put.call_args
        assert kwargs["headers"]["Content-Type"] == "application/octet-stream"

    @patch("pynukez.client.Keypair")
    def test_content_type_header_sent_when_specified(self, mock_kp):
        """upload_bytes sends Content-Type when caller provides it."""
        client = Nukez(keypair_path="~/.config/solana/id.json")
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        client._raw_client = MagicMock()
        client._raw_client.put = MagicMock(return_value=mock_resp)

        client.upload_bytes(
            "https://api.nukez.xyz/f/token", b"Hello!", content_type="text/plain"
        )

        _, kwargs = client._raw_client.put.call_args
        assert kwargs["headers"]["Content-Type"] == "text/plain"

    @patch("pynukez.client.Keypair")
    def test_upload_bytes_uses_raw_client(self, mock_kp):
        """upload_bytes uses self._raw_client (persistent, follow_redirects=True)."""
        client = Nukez(keypair_path="~/.config/solana/id.json")
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        client._raw_client = MagicMock()
        client._raw_client.put = MagicMock(return_value=mock_resp)

        client.upload_bytes("https://api.nukez.xyz/f/token", b"data")

        client._raw_client.put.assert_called_once()


class TestDelegatingFlag:
    """Verify _is_delegating and delegating= threading to build_signed_envelope."""

    @patch("pynukez.client.Keypair")
    def test_is_delegating_returns_true_when_signer_differs(self, mock_kp):
        """Operator signer (different identity) → _is_delegating returns True."""
        client = Nukez(keypair_path="~/.config/solana/id.json")
        # Simulate: signer is an operator, owner is someone else
        mock_signer = MagicMock()
        mock_signer.identity = "operator_pubkey_abc"
        client._signer = mock_signer
        client._owner_cache["receipt_123"] = "owner_pubkey_xyz"

        assert client._is_delegating("receipt_123") is True

    @patch("pynukez.client.Keypair")
    def test_is_delegating_returns_false_when_signer_is_owner(self, mock_kp):
        """Owner signer (same identity) → _is_delegating returns False."""
        client = Nukez(keypair_path="~/.config/solana/id.json")
        mock_signer = MagicMock()
        mock_signer.identity = "owner_pubkey_xyz"
        client._signer = mock_signer
        client._owner_cache["receipt_123"] = "owner_pubkey_xyz"

        assert client._is_delegating("receipt_123") is False

    @patch("pynukez.client.Keypair")
    def test_is_delegating_defaults_true_when_owner_unknown(self, mock_kp):
        """Unknown owner (operator client that never provisioned) → True (safe default)."""
        client = Nukez(keypair_path="~/.config/solana/id.json")
        mock_signer = MagicMock()
        mock_signer.identity = "operator_pubkey_abc"
        client._signer = mock_signer
        # No entry in _owner_cache

        assert client._is_delegating("unknown_receipt") is True

    @patch("pynukez.client.build_signed_envelope")
    @patch("pynukez.client.Keypair")
    def test_data_plane_passes_delegating_true_for_operator(self, mock_kp_cls, mock_build):
        """Data-plane method (list_files) passes delegating=True when signer != owner."""
        mock_build.return_value = MagicMock(
            headers={"X-Nukez-Envelope": "e", "X-Nukez-Signature": "s"},
        )
        client = Nukez(keypair_path="~/.config/solana/id.json")
        client.http = MagicMock()
        client.http.get.return_value = {"files": []}

        # Set up operator scenario: signer != owner
        mock_signer = MagicMock()
        mock_signer.identity = "operator_pubkey_abc"
        client._signer = mock_signer
        client._owner_cache["receipt_123"] = "owner_pubkey_xyz"

        client.list_files("receipt_123")

        # Verify build_signed_envelope was called with delegating=True
        assert mock_build.call_args.kwargs["delegating"] is True

    @patch("pynukez.client.build_signed_envelope")
    @patch("pynukez.client.Keypair")
    def test_data_plane_passes_delegating_false_for_owner(self, mock_kp_cls, mock_build):
        """Data-plane method (list_files) passes delegating=False when signer == owner."""
        mock_build.return_value = MagicMock(
            headers={"X-Nukez-Envelope": "e", "X-Nukez-Signature": "s"},
        )
        client = Nukez(keypair_path="~/.config/solana/id.json")
        client.http = MagicMock()
        client.http.get.return_value = {"files": []}

        # Set up owner scenario: signer == owner
        mock_signer = MagicMock()
        mock_signer.identity = "owner_pubkey_xyz"
        client._signer = mock_signer
        client._owner_cache["receipt_123"] = "owner_pubkey_xyz"

        client.list_files("receipt_123")

        # Verify build_signed_envelope was called with delegating=False
        assert mock_build.call_args.kwargs["delegating"] is False

    @patch("pynukez.client.build_signed_envelope")
    @patch("pynukez.client.Keypair")
    def test_admin_op_does_not_pass_delegating(self, mock_kp_cls, mock_build):
        """Admin method (add_operator) does NOT pass delegating — owner-only."""
        mock_build.return_value = MagicMock(
            headers={"X-Nukez-Envelope": "e", "X-Nukez-Signature": "s"},
        )
        client = Nukez(keypair_path="~/.config/solana/id.json")
        client.http = MagicMock()
        client.http.post.return_value = {"ok": True, "operator_ids": []}

        client.add_operator("receipt_123", "some_operator_pubkey")

        # Admin ops should NOT have delegating in kwargs (defaults to False)
        assert "delegating" not in mock_build.call_args.kwargs


class TestKeypairDualInit:
    """Keypair is still initialized when signing_key is injected."""

    @patch("pynukez.client.Keypair")
    def test_keypair_initialized_with_signing_key(self, mock_kp):
        mock_signer = MagicMock()
        mock_signer.identity = "external-signer-id"
        mock_signer.sig_alg = "ed25519"
        client = Nukez(
            keypair_path="~/.config/solana/id.json",
            signing_key=mock_signer,
        )
        assert client._signer is mock_signer
        mock_kp.assert_called_once_with("~/.config/solana/id.json")
        assert client.keypair is mock_kp.return_value

    @patch("pynukez.client.Keypair")
    def test_keypair_none_without_keypair_path(self, mock_kp):
        mock_signer = MagicMock()
        mock_signer.identity = "external-signer-id"
        mock_signer.sig_alg = "ed25519"
        client = Nukez(signing_key=mock_signer)
        assert client._signer is mock_signer
        assert client.keypair is None


class TestSetOwner:
    """set_owner() pre-seeds the owner cache."""

    @patch("pynukez.client.Keypair")
    def test_set_owner_uses_signer_identity(self, mock_kp):
        mock_kp.return_value.identity = "owner-pubkey"
        mock_kp.return_value.sig_alg = "ed25519"
        mock_kp.return_value.sign.return_value = "sig"
        client = Nukez(keypair_path="~/.config/solana/id.json")
        client.set_owner("receipt-123")
        assert client._owner_cache["receipt-123"] == "owner-pubkey"
        assert client._is_delegating("receipt-123") is False

    @patch("pynukez.client.Keypair")
    def test_set_owner_explicit_identity(self, mock_kp):
        mock_kp.return_value.identity = "different-pubkey"
        mock_kp.return_value.sig_alg = "ed25519"
        client = Nukez(keypair_path="~/.config/solana/id.json")
        client.set_owner("receipt-123", identity="explicit-owner")
        assert client._owner_cache["receipt-123"] == "explicit-owner"

    def test_set_owner_no_signer_raises(self):
        client = Nukez()
        with pytest.raises(NukezError, match="requires either an explicit identity"):
            client.set_owner("receipt-123")

    def test_set_owner_no_signer_with_explicit_identity(self):
        client = Nukez()
        client.set_owner("receipt-123", identity="some-owner")
        assert client._owner_cache["receipt-123"] == "some-owner"
