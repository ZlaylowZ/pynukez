"""
Tests for operator delegation feature.

Covers:
- OperatorResult type construction
- Operator error classes and inheritance
- add_operator() / remove_operator() methods (sync + async)
- confirm_storage() with explicit operator_pubkey
- Receipt.authorized_operator field
- Error code mapping in handle_error_response()
"""

import json
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from pynukez.types import OperatorResult, Receipt
from pynukez.errors import (
    NukezError,
    OperatorError,
    InvalidOperatorPubkeyError,
    OperatorIsOwnerError,
    OperatorNotAuthorizedError,
    OwnerOnlyError,
    OperatorNotFoundError,
    OperatorConflictError,
)
from pynukez._http import handle_error_response


# ---------------------------------------------------------------------------
# Mock HTTP response (same pattern as test_http_client.py)
# ---------------------------------------------------------------------------

class MockResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None, url="https://api.nukez.xyz/test"):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.headers = headers or {}
        self.url = url
        if json_data is not None and not text:
            self.content = json.dumps(json_data).encode()
        else:
            self.content = text.encode() if isinstance(text, str) else text

    def json(self):
        if self._json is not None:
            return self._json
        return json.loads(self.content) if self.content else {}


# ---------------------------------------------------------------------------
# OperatorResult type
# ---------------------------------------------------------------------------

class TestOperatorResult:
    def test_construction(self):
        result = OperatorResult(ok=True, operator_ids=["key1", "key2"])
        assert result.ok is True
        assert result.operator_ids == ["key1", "key2"]

    def test_empty_operator_ids(self):
        result = OperatorResult(ok=True, operator_ids=[])
        assert result.operator_ids == []


# ---------------------------------------------------------------------------
# Receipt.authorized_operator
# ---------------------------------------------------------------------------

class TestReceiptAuthorizedOperator:
    def test_default_none(self):
        r = Receipt(id="r1", units=1, payer_pubkey="pk1", network="devnet")
        assert r.authorized_operator is None

    def test_set_at_construction(self):
        r = Receipt(
            id="r1", units=1, payer_pubkey="pk1", network="devnet",
            authorized_operator="op_key_123",
        )
        assert r.authorized_operator == "op_key_123"


# ---------------------------------------------------------------------------
# Operator error classes
# ---------------------------------------------------------------------------

class TestOperatorErrors:
    def test_base_operator_error(self):
        err = OperatorError("test msg", error_code="TEST_CODE", pubkey="pk1", locker_id="lk1")
        assert isinstance(err, NukezError)
        assert err.error_code == "TEST_CODE"
        assert err.pubkey == "pk1"
        assert err.locker_id == "lk1"
        assert str(err) == "test msg"

    def test_invalid_operator_pubkey(self):
        err = InvalidOperatorPubkeyError(pubkey="bad_key")
        assert isinstance(err, OperatorError)
        assert err.error_code == "INVALID_OPERATOR_PUBKEY"
        assert err.pubkey == "bad_key"
        assert "bad_key" in str(err)

    def test_operator_is_owner(self):
        err = OperatorIsOwnerError(pubkey="owner_key")
        assert isinstance(err, OperatorError)
        assert err.error_code == "OPERATOR_IS_OWNER"
        assert "owner_key" in str(err)

    def test_operator_is_payer(self):
        err = OperatorIsOwnerError(pubkey="pk", error_code="OPERATOR_IS_PAYER")
        assert err.error_code == "OPERATOR_IS_PAYER"

    def test_operator_not_authorized(self):
        err = OperatorNotAuthorizedError(pubkey="op1")
        assert isinstance(err, OperatorError)
        assert err.error_code == "NOT_AUTHORIZED_OPERATOR"
        assert "add_operator" in str(err)

    def test_owner_only(self):
        err = OwnerOnlyError(locker_id="lk1")
        assert isinstance(err, OperatorError)
        assert err.error_code == "OWNER_ONLY"
        assert err.locker_id == "lk1"

    def test_operator_not_found(self):
        err = OperatorNotFoundError(pubkey="missing_key")
        assert isinstance(err, OperatorError)
        assert err.error_code == "OPERATOR_NOT_FOUND"
        assert "missing_key" in str(err)

    def test_operator_already_exists(self):
        err = OperatorConflictError(error_code="OPERATOR_ALREADY_EXISTS", pubkey="dup_key")
        assert isinstance(err, OperatorError)
        assert err.error_code == "OPERATOR_ALREADY_EXISTS"
        assert "already exists" in str(err)

    def test_max_operators_reached(self):
        err = OperatorConflictError(error_code="MAX_OPERATORS_REACHED")
        assert err.error_code == "MAX_OPERATORS_REACHED"
        assert "Maximum" in str(err)

    def test_catch_all_operator_error(self):
        """All operator errors are catchable via OperatorError."""
        errors = [
            InvalidOperatorPubkeyError(),
            OperatorIsOwnerError(),
            OperatorNotAuthorizedError(),
            OwnerOnlyError(),
            OperatorNotFoundError(),
            OperatorConflictError(error_code="OPERATOR_ALREADY_EXISTS"),
        ]
        for err in errors:
            assert isinstance(err, OperatorError)
            assert isinstance(err, NukezError)


# ---------------------------------------------------------------------------
# Error code mapping in handle_error_response()
# ---------------------------------------------------------------------------

class TestErrorMapping:
    def test_400_invalid_operator_pubkey(self):
        resp = MockResponse(400, json_data={"error_code": "INVALID_OPERATOR_PUBKEY", "pubkey": "bad"})
        with pytest.raises(InvalidOperatorPubkeyError) as exc_info:
            handle_error_response(resp)
        assert exc_info.value.pubkey == "bad"

    def test_400_operator_is_owner(self):
        resp = MockResponse(400, json_data={"error_code": "OPERATOR_IS_OWNER", "pubkey": "pk"})
        with pytest.raises(OperatorIsOwnerError):
            handle_error_response(resp)

    def test_400_operator_is_payer(self):
        resp = MockResponse(400, json_data={"error_code": "OPERATOR_IS_PAYER", "pubkey": "pk"})
        with pytest.raises(OperatorIsOwnerError) as exc_info:
            handle_error_response(resp)
        assert exc_info.value.error_code == "OPERATOR_IS_PAYER"

    def test_403_not_authorized_operator(self):
        resp = MockResponse(403, json_data={"error_code": "NOT_AUTHORIZED_OPERATOR", "pubkey": "op1"})
        with pytest.raises(OperatorNotAuthorizedError):
            handle_error_response(resp)

    def test_403_owner_only(self):
        resp = MockResponse(403, json_data={"error_code": "OWNER_ONLY", "locker_id": "lk1"})
        with pytest.raises(OwnerOnlyError) as exc_info:
            handle_error_response(resp)
        assert exc_info.value.locker_id == "lk1"

    def test_404_operator_not_found(self):
        resp = MockResponse(404, json_data={"error_code": "OPERATOR_NOT_FOUND", "pubkey": "pk"})
        with pytest.raises(OperatorNotFoundError):
            handle_error_response(resp)

    def test_409_operator_already_exists(self):
        resp = MockResponse(409, json_data={"error_code": "OPERATOR_ALREADY_EXISTS", "pubkey": "pk"})
        with pytest.raises(OperatorConflictError) as exc_info:
            handle_error_response(resp)
        assert exc_info.value.error_code == "OPERATOR_ALREADY_EXISTS"

    def test_409_max_operators_reached(self):
        resp = MockResponse(409, json_data={"error_code": "MAX_OPERATORS_REACHED"})
        with pytest.raises(OperatorConflictError) as exc_info:
            handle_error_response(resp)
        assert exc_info.value.error_code == "MAX_OPERATORS_REACHED"

    def test_403_generic_still_works(self):
        """Non-operator 403 still raises AuthenticationError."""
        from pynukez.errors import AuthenticationError
        resp = MockResponse(403, json_data={"message": "Auth failed"})
        with pytest.raises(AuthenticationError):
            handle_error_response(resp)


# ---------------------------------------------------------------------------
# Sync client: add_operator / remove_operator
# ---------------------------------------------------------------------------

class TestAddOperator:
    def test_happy_path(self, sync_client):
        sync_client.http.post.return_value = {
            "ok": True,
            "operator_ids": ["op_key_1", "op_key_2"],
        }
        result = sync_client.add_operator("receipt_123", "op_key_2")
        assert isinstance(result, OperatorResult)
        assert result.ok is True
        assert "op_key_2" in result.operator_ids
        sync_client.http.post.assert_called_once()
        call_args = sync_client.http.post.call_args
        assert "/operators" in call_args[0][0]

    def test_envelope_has_correct_ops(self, sync_client):
        sync_client.http.post.return_value = {"ok": True, "operator_ids": []}
        with patch("pynukez.client.build_signed_envelope") as mock_env:
            mock_env.return_value = MagicMock(
                headers={"X-Nukez-Envelope": "e", "X-Nukez-Signature": "s"},
                canonical_body='{"pubkey":"op1"}',
            )
            sync_client.add_operator("receipt_123", "op1")
            mock_env.assert_called_once()
            _, kwargs = mock_env.call_args
            assert kwargs["ops"] == ["locker:admin"]
            assert kwargs["method"] == "POST"
            assert kwargs["body"] == {"pubkey": "op1"}


class TestRemoveOperator:
    def test_happy_path(self, sync_client):
        sync_client.http.delete.return_value = {
            "ok": True,
            "operator_ids": [],
        }
        result = sync_client.remove_operator("receipt_123", "op_key_1")
        assert isinstance(result, OperatorResult)
        assert result.ok is True
        assert result.operator_ids == []
        sync_client.http.delete.assert_called_once()
        call_args = sync_client.http.delete.call_args
        assert "op_key_1" in call_args[0][0]

    def test_envelope_has_correct_method(self, sync_client):
        sync_client.http.delete.return_value = {"ok": True, "operator_ids": []}
        with patch("pynukez.client.build_signed_envelope") as mock_env:
            mock_env.return_value = MagicMock(
                headers={"X-Nukez-Envelope": "e", "X-Nukez-Signature": "s"},
                canonical_body=None,
            )
            sync_client.remove_operator("receipt_123", "op1")
            _, kwargs = mock_env.call_args
            assert kwargs["method"] == "DELETE"
            assert kwargs["ops"] == ["locker:admin"]
            assert "op1" in kwargs["path"]


# ---------------------------------------------------------------------------
# Sync client: confirm_storage with operator_pubkey
# ---------------------------------------------------------------------------

class TestConfirmStorageOperator:
    def _mock_confirm_response(self, client, authorized_operator=None):
        """Set up a mock raw HTTP response for confirm_storage."""
        receipt_data = {
            "receipt_id": "r_123",
            "receipt": {
                "units": 1,
                "payer_pubkey": "payer_pk",
                "network": "devnet",
                "provider": "gcs",
                "authorized_operator": authorized_operator,
            },
        }
        import httpx as _httpx
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = receipt_data
        return mock_resp

    @patch("pynukez.client._httpx.post")
    def test_explicit_operator_pubkey(self, mock_post, sync_client):
        mock_post.return_value = self._mock_confirm_response(sync_client, "op_explicit")
        receipt = sync_client.confirm_storage("pr_1", "solana_sig_123", operator_pubkey="op_explicit")
        payload = mock_post.call_args[1]["json"]
        assert payload["operator_pubkey"] == "op_explicit"

    @patch("pynukez.client._httpx.post")
    def test_auto_evm_operator(self, mock_post, sync_client):
        mock_post.return_value = self._mock_confirm_response(sync_client)
        receipt = sync_client.confirm_storage("pr_1", "0xabc123")
        payload = mock_post.call_args[1]["json"]
        assert payload["operator_pubkey"] == "FakePublicKey123456789"

    @patch("pynukez.client._httpx.post")
    def test_explicit_overrides_evm_auto(self, mock_post, sync_client):
        mock_post.return_value = self._mock_confirm_response(sync_client, "op_manual")
        receipt = sync_client.confirm_storage("pr_1", "0xabc123", operator_pubkey="op_manual")
        payload = mock_post.call_args[1]["json"]
        assert payload["operator_pubkey"] == "op_manual"

    @patch("pynukez.client._httpx.post")
    def test_no_operator_for_solana(self, mock_post, sync_client):
        mock_post.return_value = self._mock_confirm_response(sync_client)
        receipt = sync_client.confirm_storage("pr_1", "solana_base58_sig")
        payload = mock_post.call_args[1]["json"]
        assert "operator_pubkey" not in payload

    @patch("pynukez.client._httpx.post")
    def test_authorized_operator_in_receipt(self, mock_post, sync_client):
        mock_post.return_value = self._mock_confirm_response(sync_client, "op_key_99")
        receipt = sync_client.confirm_storage("pr_1", "solana_sig")
        assert receipt.authorized_operator == "op_key_99"


# ---------------------------------------------------------------------------
# Async client: add_operator / remove_operator
# ---------------------------------------------------------------------------

class TestAsyncAddOperator:
    async def test_happy_path(self, async_client):
        async_client.http.post.return_value = {
            "ok": True,
            "operator_ids": ["op1"],
        }
        result = await async_client.add_operator("receipt_123", "op1")
        assert isinstance(result, OperatorResult)
        assert result.ok is True
        assert result.operator_ids == ["op1"]


class TestAsyncRemoveOperator:
    async def test_happy_path(self, async_client):
        async_client.http.delete.return_value = {
            "ok": True,
            "operator_ids": [],
        }
        result = await async_client.remove_operator("receipt_123", "op1")
        assert isinstance(result, OperatorResult)
        assert result.ok is True
        assert result.operator_ids == []


# ===========================================================================
# Deeper delegation tests — edge cases, path construction, error propagation
# ===========================================================================

class TestAddOperatorPathConstruction:
    """Verify the URL path includes the correct locker_id derived from receipt_id."""

    def test_locker_id_in_path(self, sync_client):
        from pynukez.auth import compute_locker_id
        receipt_id = "rcpt_unique_abc"
        expected_locker = compute_locker_id(receipt_id)

        sync_client.http.post.return_value = {"ok": True, "operator_ids": ["op1"]}
        sync_client.add_operator(receipt_id, "op1")

        call_args = sync_client.http.post.call_args
        path = call_args[0][0]
        assert expected_locker in path
        assert path == f"/v1/lockers/{expected_locker}/operators"

    def test_different_receipt_ids_produce_different_paths(self, sync_client):
        from pynukez.auth import compute_locker_id
        sync_client.http.post.return_value = {"ok": True, "operator_ids": []}

        sync_client.add_operator("receipt_A", "op1")
        path_a = sync_client.http.post.call_args[0][0]

        sync_client.add_operator("receipt_B", "op1")
        path_b = sync_client.http.post.call_args[0][0]

        assert path_a != path_b


class TestRemoveOperatorPathConstruction:
    """Verify remove URL includes operator_pubkey in path segment."""

    def test_operator_pubkey_in_path(self, sync_client):
        from pynukez.auth import compute_locker_id
        receipt_id = "rcpt_del_test"
        expected_locker = compute_locker_id(receipt_id)

        sync_client.http.delete.return_value = {"ok": True, "operator_ids": []}
        sync_client.remove_operator(receipt_id, "DeadBeefOperator123")

        call_args = sync_client.http.delete.call_args
        path = call_args[0][0]
        assert path == f"/v1/lockers/{expected_locker}/operators/DeadBeefOperator123"


class TestAddOperatorRequestBody:
    """Verify the request body is correctly encoded."""

    def test_canonical_body_sent(self, sync_client):
        sync_client.http.post.return_value = {"ok": True, "operator_ids": ["op_xyz"]}
        sync_client.add_operator("rcpt_1", "op_xyz")

        call_kwargs = sync_client.http.post.call_args[1]
        data = call_kwargs.get("data")
        assert data is not None
        parsed = json.loads(data)
        assert parsed["pubkey"] == "op_xyz"


class TestOperatorResultFromPartialResponse:
    """Server may omit fields — verify defaults."""

    def test_missing_ok_defaults_true(self, sync_client):
        sync_client.http.post.return_value = {"operator_ids": ["op1"]}
        result = sync_client.add_operator("rcpt_1", "op1")
        assert result.ok is True

    def test_missing_operator_ids_defaults_empty(self, sync_client):
        sync_client.http.post.return_value = {"ok": True}
        result = sync_client.add_operator("rcpt_1", "op1")
        assert result.operator_ids == []

    def test_empty_response(self, sync_client):
        sync_client.http.post.return_value = {}
        result = sync_client.add_operator("rcpt_1", "op1")
        assert result.ok is True
        assert result.operator_ids == []


class TestAddOperatorMultiple:
    """Verify accumulation of operator_ids across sequential adds."""

    def test_ids_grow(self, sync_client):
        sync_client.http.post.return_value = {"ok": True, "operator_ids": ["op1"]}
        r1 = sync_client.add_operator("rcpt_1", "op1")
        assert len(r1.operator_ids) == 1

        sync_client.http.post.return_value = {"ok": True, "operator_ids": ["op1", "op2"]}
        r2 = sync_client.add_operator("rcpt_1", "op2")
        assert len(r2.operator_ids) == 2
        assert "op2" in r2.operator_ids


class TestRemoveOperatorHeaders:
    """Verify signed envelope headers are forwarded on DELETE."""

    def test_headers_passed(self, sync_client):
        sync_client.http.delete.return_value = {"ok": True, "operator_ids": []}
        with patch("pynukez.client.build_signed_envelope") as mock_env:
            mock_env.return_value = MagicMock(
                headers={"X-Nukez-Envelope": "env_val", "X-Nukez-Signature": "sig_val"},
                canonical_body=None,
            )
            sync_client.remove_operator("rcpt_1", "op1")
            call_kwargs = sync_client.http.delete.call_args[1]
            assert call_kwargs["headers"]["X-Nukez-Envelope"] == "env_val"
            assert call_kwargs["headers"]["X-Nukez-Signature"] == "sig_val"


class TestOperatorRequiresKeypair:
    """add_operator / remove_operator must fail without signing key."""

    def test_add_no_keypair(self, mock_keypair):
        from pynukez import Nukez
        from pynukez.errors import NukezError
        client = Nukez(keypair_path="~/.config/solana/id.json")
        client.keypair = None
        client._signer = None  # simulate no signing key
        with pytest.raises(NukezError, match="requires a signing key"):
            client.add_operator("rcpt_1", "op1")

    def test_remove_no_keypair(self, mock_keypair):
        from pynukez import Nukez
        from pynukez.errors import NukezError
        client = Nukez(keypair_path="~/.config/solana/id.json")
        client.keypair = None
        client._signer = None
        with pytest.raises(NukezError, match="requires a signing key"):
            client.remove_operator("rcpt_1", "op1")


class TestErrorMappingEdgeCases:
    """Edge cases in operator error code mapping."""

    def test_400_unknown_error_code_falls_through(self):
        """400 with an unknown error_code should NOT raise OperatorError."""
        resp = MockResponse(400, json_data={"error_code": "SOMETHING_ELSE", "message": "bad"})
        with pytest.raises(NukezError):
            handle_error_response(resp)
        # Verify it's NOT an OperatorError subclass
        try:
            handle_error_response(MockResponse(400, json_data={"error_code": "SOMETHING_ELSE"}))
        except OperatorError:
            pytest.fail("Unknown error_code should not raise OperatorError")
        except NukezError:
            pass

    def test_403_unknown_error_code_falls_through(self):
        """403 with unknown error_code raises AuthenticationError, not OperatorError."""
        from pynukez.errors import AuthenticationError
        resp = MockResponse(403, json_data={"error_code": "UNKNOWN_CODE"})
        with pytest.raises(AuthenticationError):
            handle_error_response(resp)

    def test_409_unknown_error_code_falls_through(self):
        """409 with unknown error_code should not raise OperatorConflictError."""
        resp = MockResponse(409, json_data={"error_code": "SOME_OTHER_CONFLICT"})
        try:
            handle_error_response(resp)
        except OperatorConflictError:
            pytest.fail("Unknown 409 error_code should not raise OperatorConflictError")
        except NukezError:
            pass

    def test_locker_id_propagated(self):
        """Error response locker_id is propagated to the exception."""
        resp = MockResponse(403, json_data={
            "error_code": "OWNER_ONLY",
            "locker_id": "locker_abc123def456",
        })
        with pytest.raises(OwnerOnlyError) as exc_info:
            handle_error_response(resp)
        assert exc_info.value.locker_id == "locker_abc123def456"

    def test_pubkey_propagated_on_not_found(self):
        resp = MockResponse(404, json_data={
            "error_code": "OPERATOR_NOT_FOUND",
            "pubkey": "MissingPubkey123",
        })
        with pytest.raises(OperatorNotFoundError) as exc_info:
            handle_error_response(resp)
        assert exc_info.value.pubkey == "MissingPubkey123"


class TestOperatorErrorAttributes:
    """Verify error attributes are accessible and correctly typed."""

    def test_operator_error_retryable_is_false(self):
        err = OperatorError("msg", error_code="TEST")
        assert err.retryable is False

    def test_error_details_dict(self):
        err = InvalidOperatorPubkeyError(pubkey="bad", locker_id="lk1")
        assert err.details["error_code"] == "INVALID_OPERATOR_PUBKEY"
        assert err.details["pubkey"] == "bad"
        assert err.details["locker_id"] == "lk1"

    def test_conflict_error_message_differs_by_code(self):
        e1 = OperatorConflictError(error_code="OPERATOR_ALREADY_EXISTS", pubkey="pk")
        e2 = OperatorConflictError(error_code="MAX_OPERATORS_REACHED")
        assert str(e1) != str(e2)
        assert "already exists" in str(e1)
        assert "Maximum" in str(e2)

    def test_operator_is_payer_vs_owner_messages(self):
        e1 = OperatorIsOwnerError(pubkey="pk", error_code="OPERATOR_IS_OWNER")
        e2 = OperatorIsOwnerError(pubkey="pk", error_code="OPERATOR_IS_PAYER")
        # Same message template but different error_code
        assert e1.error_code != e2.error_code
        assert type(e1) is type(e2)

    def test_operator_not_authorized_message_includes_add_operator(self):
        err = OperatorNotAuthorizedError(pubkey="some_key")
        assert "add_operator" in str(err)
        assert "some_key" in str(err)


class TestAsyncAddOperatorEnvelope:
    """Async add_operator envelope validation."""

    async def test_envelope_ops_and_method(self, async_client):
        async_client.http.post.return_value = {"ok": True, "operator_ids": ["op1"]}
        with patch("pynukez._async_client.build_signed_envelope") as mock_env:
            mock_env.return_value = MagicMock(
                headers={"X-Nukez-Envelope": "e", "X-Nukez-Signature": "s"},
                canonical_body='{"pubkey":"op1"}',
            )
            await async_client.add_operator("rcpt_1", "op1")
            _, kwargs = mock_env.call_args
            assert kwargs["method"] == "POST"
            assert kwargs["ops"] == ["locker:admin"]
            assert kwargs["body"] == {"pubkey": "op1"}


class TestAsyncRemoveOperatorEnvelope:
    """Async remove_operator envelope validation."""

    async def test_envelope_ops_and_method(self, async_client):
        async_client.http.delete.return_value = {"ok": True, "operator_ids": []}
        with patch("pynukez._async_client.build_signed_envelope") as mock_env:
            mock_env.return_value = MagicMock(
                headers={"X-Nukez-Envelope": "e", "X-Nukez-Signature": "s"},
                canonical_body=None,
            )
            await async_client.remove_operator("rcpt_1", "op1")
            _, kwargs = mock_env.call_args
            assert kwargs["method"] == "DELETE"
            assert kwargs["ops"] == ["locker:admin"]
            assert "op1" in kwargs["path"]
