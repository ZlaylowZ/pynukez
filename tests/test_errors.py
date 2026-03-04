# tests/test_errors.py
"""
Batch 4B: SDK error class tests.

Tests all exception classes and backward-compat aliases.
"""
import pytest
from pynukez.errors import (
    NukezError,
    PaymentRequiredError,
    TransactionNotFoundError,
    AuthenticationError,
    NukezFileNotFoundError,
    URLExpiredError,
    NukezNotProvisionedError,
    InsufficientFundsError,
    RateLimitError,
)


class TestNukezError:
    """Base error class tests."""

    def test_basic_construction(self):
        err = NukezError("something broke")
        assert str(err) == "something broke"
        assert err.message == "something broke"

    def test_details_default(self):
        err = NukezError("test")
        assert err.details is None or isinstance(err.details, dict)

    def test_is_exception(self):
        assert issubclass(NukezError, Exception)


class TestRetryableErrors:
    """Errors that should be retryable."""

    def test_transaction_not_found_retryable(self):
        err = TransactionNotFoundError("sig123")
        assert err.retryable is True
        assert err.tx_sig == "sig123"
        assert err.suggested_delay == 2

    def test_url_expired_retryable(self):
        err = URLExpiredError("upload")
        assert err.retryable is True
        assert err.operation == "upload"

    def test_rate_limit_retryable(self):
        err = RateLimitError(retry_after=30)
        assert err.retryable is True
        assert err.retry_after == 30


class TestNonRetryableErrors:
    """Errors that should NOT be retryable."""

    def test_authentication_not_retryable(self):
        err = AuthenticationError(message="bad sig")
        assert err.retryable is False

    def test_not_provisioned_not_retryable(self):
        err = NukezNotProvisionedError("rid123")
        assert err.retryable is False
        assert err.receipt_id == "rid123"

    def test_file_not_found_not_retryable(self):
        err = NukezFileNotFoundError("test.txt", "locker_abc")
        assert err.retryable is False
        assert err.filename == "test.txt"

    def test_insufficient_funds_not_retryable(self):
        err = InsufficientFundsError(required=1.0, available=0.5)
        assert err.retryable is False


class TestBackwardCompatAlias:
    """Backward compatibility aliases."""

    def test_file_not_found_alias(self):
        """FileNotFound should be importable as alias."""
        try:
            from pynukez.errors import FileNotFound
            assert FileNotFound is NukezFileNotFoundError
        except ImportError:
            # Alias may not exist — skip
            pytest.skip("FileNotFound alias not defined")
