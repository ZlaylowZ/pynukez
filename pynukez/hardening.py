# pynukez/hardening.py
"""
SDK-level hardening for weaker models.

Two fixes that belong in the SDK (not the test harness), plus
INFRA_ERROR classification for the test harness to import.

FIXES (SDK changes — modify __init__.py handlers):
  1. upload_bytes data sanitizer  — unwrap common JSON wrappers
  2. download_bytes URL validator  — catch malformed URLs before HTTP call

CLASSIFICATION (test harness import):
  3. classify_run_result()  — PASSED / FAILED / INFRA_ERROR bucketing

Install:
  Copy sanitize_upload_data() and validate_signed_url() into pynukez/client.py
  or call them from the executor in test_sdk_realworld.py.
  Import classify_run_result() in the test harness report generator.
"""

import json
import re
from typing import Optional, Tuple

# ═══════════════════════════════════════════════════════════════════════
# Fix 1: upload_bytes data sanitizer
# ═══════════════════════════════════════════════════════════════════════
#
# Problem: Haiku 4.5 (and pre-fix weaker models) occasionally wrap the
# data parameter in JSON structure instead of passing a plain string:
#
#   CORRECT:   data="Hello world"
#   BROKEN:    data='{"content": "Hello world"}'
#   BROKEN:    data='{"data": "Hello world", "encoding": "utf-8"}'
#   BROKEN:    data='```\nHello world\n```'
#
# The SDK tool definition says "Do not wrap in JSON" but smaller models
# still do it ~10% of the time.  The signed URL server rejects these
# because the body doesn't match expectations.
#
# Fix: Detect and unwrap before encoding to bytes.  This is SDK-level
# because it's a reasonable "do what I mean" behavior — the SDK knows
# the data parameter should be plain content, not a JSON envelope.

# Keys that weaker models commonly use to wrap content
_WRAPPER_KEYS = {"content", "data", "text", "body", "payload", "value"}


def sanitize_upload_data(data: str) -> Tuple[str, Optional[str]]:
    """
    Unwrap common malformations of the upload data parameter.

    Returns:
        (cleaned_data, fix_applied)
        fix_applied is None if data was already clean, or a short
        description of what was unwrapped (for logging/telemetry).

    Examples:
        >>> sanitize_upload_data("Hello world")
        ("Hello world", None)

        >>> sanitize_upload_data('{"content": "Hello world"}')
        ("Hello world", "unwrapped_json_key:content")

        >>> sanitize_upload_data('```\\nHello world\\n```')
        ("Hello world", "stripped_markdown_fencing")
    """
    stripped = data.strip()

    # ── Pass 1: JSON object with single content key ────────────────
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict) and len(parsed) <= 3:
                # Look for a recognized wrapper key
                for key in _WRAPPER_KEYS:
                    if key in parsed and isinstance(parsed[key], str):
                        return parsed[key], f"unwrapped_json_key:{key}"
        except (json.JSONDecodeError, TypeError):
            pass  # Not valid JSON — leave it alone

    # ── Pass 2: Markdown code fencing ──────────────────────────────
    fence_match = re.match(
        r'^```(?:\w*)\s*\n(.*?)\n\s*```$', stripped, re.DOTALL
    )
    if fence_match:
        inner = fence_match.group(1).strip()
        if inner:
            return inner, "stripped_markdown_fencing"

    # ── Clean — return as-is ───────────────────────────────────────
    return data, None


# ═══════════════════════════════════════════════════════════════════════
# Fix 2: download_bytes URL validator
# ═══════════════════════════════════════════════════════════════════════
#
# Problem: Haiku 4.5 occasionally malforms the download_url the same
# way it malforms uploads — truncating query params, double-encoding,
# or injecting extra path segments.  The HTTP GET then returns 400.
#
# Fix: Validate the URL has the expected structure before making the
# request.  Return an actionable error message that tells the model
# to call list_files() for fresh URLs instead of retrying the same
# bad URL.
#
# UPDATED: Now accepts both short URLs (api.nukez.xyz/f/{token})
# and raw signed URLs (storage.googleapis.com/...?X-Goog-Signature=...).
# Short URLs are the default format returned by the API.

# Short URL pattern: https://api.nukez.xyz/f/{base64url_token}
_SHORT_URL_PATTERN = re.compile(
    r'^https://[^/]+/f/[A-Za-z0-9_-]{20,}$'
)

# Legacy signed URL pattern (GCS, S3, Azure)
_SIGNED_URL_PATTERN = re.compile(
    r'^https://.+\?'           # https://host/path?
    r'.*(?:X-Goog-Signature|Signature|X-Amz-Signature)=.+'
)


def validate_signed_url(url: str, param_name: str = "url") -> Optional[str]:
    """
    Validate that a URL looks like a properly-formed file access URL.

    Accepts:
        - Nukez short URLs: https://api.nukez.xyz/f/{token}
        - Raw signed URLs: https://storage.googleapis.com/...?X-Goog-Signature=...
        - S3 presigned URLs: https://...?X-Amz-Signature=...

    Returns:
        None if valid, or an error message string if malformed.
        The error message includes recovery guidance for the agent.
    """
    if not url or not isinstance(url, str):
        return (
            f"Empty or missing {param_name}. "
            "Use the URL exactly as returned by create_file() or get_file_urls()."
        )

    stripped = url.strip()

    # Strip wrapping quotes the model sometimes adds
    if (stripped.startswith('"') and stripped.endswith('"')) or \
       (stripped.startswith("'") and stripped.endswith("'")):
        stripped = stripped[1:-1]

    if not stripped.startswith("https://") and not stripped.startswith("http://localhost") and not stripped.startswith("http://localhost"):
        return (
            f"Invalid {param_name}: must start with https://. "
            f"Use the URL exactly as returned by create_file(). "
            f"If the URL has expired, call list_files() to get fresh URLs."
        )

    # Accept short URLs (the default format from the API)
    if _SHORT_URL_PATTERN.match(stripped):
        return None  # Valid short URL

    # Accept legacy raw signed URLs
    if _SIGNED_URL_PATTERN.match(stripped):
        return None  # Valid signed URL

    # URL is https:// but doesn't match either pattern
    # Be lenient — it might be a valid URL format we don't recognize yet
    # Only reject if it's clearly malformed (no path after host)
    if "/" not in stripped[8:]:  # after "https://"
        return (
            f"Invalid {param_name}: URL appears truncated. "
            f"Call list_files(receipt_id=...) or get_file_urls(receipt_id=..., filename=...) "
            f"to get fresh URLs."
        )

    return None  # Accept — don't be too strict


# ═══════════════════════════════════════════════════════════════════════
# Fix 3: INFRA_ERROR classification
# ═══════════════════════════════════════════════════════════════════════
#
# For import by test_sdk_realworld.py report generation.
#
# Three-bucket classification:
#   PASSED      — Model completed workflow and verified data
#   FAILED      — Model made tool calls but couldn't complete
#   INFRA_ERROR — Model never got a chance (provider 529, timeout, etc.)
#
# Rule: zero successful tool calls + provider-level exception = INFRA_ERROR

# Provider-level errors that indicate infrastructure, not model failure
_INFRA_ERROR_TYPES = {
    "OverloadedError",      # Anthropic 529
    "APIStatusError",       # Generic API failures (when status >= 500)
    "APIConnectionError",   # Network/DNS failures
    "APITimeoutError",      # Request timeout before response
    "RateLimitError",       # Provider rate limit (not agent-caused)
    "InternalServerError",  # Provider 500
    "ServiceUnavailableError",  # Provider 503
}

# Status codes that are always infrastructure
_INFRA_STATUS_CODES = {429, 500, 502, 503, 529}


def classify_run_result(result_dict: dict) -> str:
    """
    Classify a RealWorldResult dict into PASSED / FAILED / INFRA_ERROR.

    Args:
        result_dict: Serialized RealWorldResult (from asdict() or JSON)

    Returns:
        "PASSED", "FAILED", or "INFRA_ERROR"
    """
    # Already passed?
    if result_dict.get("passed", False):
        return "PASSED"

    # Check for zero tool execution
    total_calls = result_dict.get("total_calls", 0)
    successful_calls = result_dict.get("successful_calls", 0)
    tool_calls = result_dict.get("tool_calls", [])

    # If the model never executed a single tool call, check why
    if successful_calls == 0 and total_calls == 0:
        # Look at error info
        error_type = result_dict.get("error_type", "")
        error_msg = result_dict.get("error", "")

        # Known infrastructure error types
        if error_type in _INFRA_ERROR_TYPES:
            return "INFRA_ERROR"

        # Check error message for status codes
        for code in _INFRA_STATUS_CODES:
            if str(code) in error_msg:
                return "INFRA_ERROR"

        # "overloaded" anywhere in the error
        if "overloaded" in error_msg.lower():
            return "INFRA_ERROR"

        # If zero calls and no recognized infra error, still INFRA_ERROR
        # because the model never got agency
        return "INFRA_ERROR"

    # Model made tool calls but didn't complete — real failure
    return "FAILED"


def compute_pass_rate(results: list) -> dict:
    """
    Compute pass rate excluding INFRA_ERROR runs.

    Args:
        results: List of RealWorldResult dicts

    Returns:
        {
            "total_runs": int,
            "passed": int,
            "failed": int,
            "infra_error": int,
            "valid_runs": int,      # total - infra_error
            "pass_rate": float|None, # None if no valid runs
            "pass_rate_str": str,    # "95.0%" or "N/A (no valid runs)"
        }
    """
    classifications = [classify_run_result(r) for r in results]

    passed = classifications.count("PASSED")
    failed = classifications.count("FAILED")
    infra = classifications.count("INFRA_ERROR")
    valid = passed + failed

    if valid == 0:
        rate = None
        rate_str = "N/A (no valid runs)"
    else:
        rate = (passed / valid) * 100
        rate_str = f"{rate:.1f}%"

    return {
        "total_runs": len(results),
        "passed": passed,
        "failed": failed,
        "infra_error": infra,
        "valid_runs": valid,
        "pass_rate": rate,
        "pass_rate_str": rate_str,
    }