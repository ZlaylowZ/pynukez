# tests/test_hardening.py
"""
Batch 4B: SDK hardening tests — input sanitization and URL validation.
"""
import pytest
from pynukez.hardening import sanitize_upload_data, validate_signed_url


class TestSanitizeUploadData:
    """Input sanitization tests."""

    def test_plain_text_passthrough(self):
        """Plain text data passes through unchanged."""
        data, fix = sanitize_upload_data("Hello world")
        assert data == "Hello world"
        assert fix is None

    def test_json_wrapper_unwrapped(self):
        """JSON wrapper objects are unwrapped."""
        import json
        wrapper = json.dumps({"content": "inner value"})
        data, fix = sanitize_upload_data(wrapper)
        assert data == "inner value"
        assert fix is not None and "unwrapped" in fix

    def test_markdown_fencing_stripped(self):
        """Markdown code fences are stripped."""
        fenced = "```\nsome code\n```"
        data, fix = sanitize_upload_data(fenced)
        assert "```" not in data
        assert fix is not None and "markdown" in fix

    def test_empty_string(self):
        """Empty string passes through."""
        data, fix = sanitize_upload_data("")
        assert data == ""
        assert fix is None


class TestValidateSignedUrl:
    """URL validation tests."""

    def test_valid_nukez_url(self):
        """Nukez short URL passes validation."""
        result = validate_signed_url("https://api.nukez.xyz/f/abc123token")
        assert result is None  # None = valid

    def test_invalid_url_detected(self):
        """Malformed URL returns error message."""
        result = validate_signed_url("not-a-url")
        assert result is not None  # Non-None = error
        assert isinstance(result, str)
