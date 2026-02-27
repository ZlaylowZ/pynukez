"""
Internal HTTP client for Nukez SDK.

Handles:
- Request execution with timeouts
- Error response parsing
- Conversion to agent-friendly exceptions

FIXED: Better handling of 402 responses and debug logging for payment field extraction.
"""

import requests
from typing import Dict, Any, Optional

from .errors import (
    NukezError,
    PaymentRequiredError,
    TransactionNotFoundError,
    AuthenticationError,
    NukezFileNotFoundError,
    URLExpiredError,
    RateLimitError,
)


class HTTPClient:
    """
    Internal HTTP client with Nukez-specific error handling.

    Converts HTTP error responses into appropriate Nukez exceptions
    with actionable error messages for agents.
    """
    
    def __init__(self, base_url: str, timeout: int = 30):
        """
        Initialize HTTP client.
        
        Args:
            base_url: Nukez API base URL
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.session = requests.Session()
        
        # Standard headers
        self.session.headers.update({
            "User-Agent": "nukez-sdk/3.0.0",
            "Accept": "application/json",
            "Content-Type": "application/json"
        })
        
    def _parse_json_response(self, response: requests.Response, method: str, path: str) -> Dict[str, Any]:
        """
        Safely parse JSON response body with clear error messages.
        
        Args:
            response: HTTP response object
            method: HTTP method (for error messages)
            path: Request path (for error messages)
            
        Returns:
            Parsed JSON as dict, or empty dict if no content
            
        Raises:
            NukezError: If JSON parsing fails
        """
        if not response.content:
            return {}
    
        try:
            return response.json()
        
        except ValueError as e:
            
            # JSON decode error - provide clear message
            content_preview = response.content[:200].decode('utf-8', errors='replace')
            
            raise NukezError(
                f"Invalid JSON response from {method} {path}: {e}. "
                f"Response content: {content_preview}..."
            )

    def _parse_error_response(self, response: requests.Response) -> dict:
        """
        Safely parse error response body.
        
        FIXED: Better error handling and logging for debugging.
        """
        try:
            if response.content:
                # Attempt JSON parsing
                data = response.json()
                return data if isinstance(data, dict) else {}
        except (ValueError, TypeError) as e:
            # Log parsing failure for debugging
            import sys
            print(f"[nukez] WARNING: Failed to parse response JSON: {e}", file=sys.stderr)
            print(f"[nukez] Response content: {response.content[:500]}", file=sys.stderr)
        return {}
    
    def _handle_error_response(self, response: requests.Response):
        """
        Convert HTTP error responses to appropriate exceptions.
        
        Maps HTTP status codes to Nukez exception types with
        all relevant details extracted from the response.
        
        FIXED: Better extraction of 402 payment fields with fallback paths.
        """
        error_details = self._parse_error_response(response)
        
        # 402 Payment Required - contains payment instructions (multi-chain)
        if response.status_code == 402:
            # Extract payment fields - try multiple possible locations
            # Primary: top-level fields (as seen in curl tests)
            pay_req_id = error_details.get("pay_req_id", "")
            pay_to_address = error_details.get("pay_to_address", "")
            amount_sol = error_details.get("amount_sol", 0)
            amount_lamports = error_details.get("amount_lamports", 0)
            network = error_details.get("network", "devnet")

            # EVM / multi-chain fields (Phase 2)
            pay_asset = error_details.get("pay_asset", "SOL")
            amount = str(error_details.get("amount", ""))        # human-readable
            amount_raw = int(error_details.get("amount_raw", 0) or 0)
            token_address = str(error_details.get("token_address", ""))
            token_decimals = int(error_details.get("token_decimals", 0) or 0)

            # Fallback: check nested 'price' object
            if not pay_req_id or not pay_to_address:
                price = error_details.get("price", {})
                if not amount_sol and price:
                    amount_sol = price.get("amount_sol", 0)
                if not amount_lamports and price:
                    amount_lamports = price.get("amount_lamports", 0)

            # Fallback: check 'details' wrapper (some API versions)
            if not pay_req_id:
                details = error_details.get("details", {})
                pay_req_id = details.get("pay_req_id", pay_req_id)
                pay_to_address = details.get("pay_to_address", pay_to_address)
                amount_sol = details.get("amount_sol", amount_sol)

            # Calculate lamports from SOL if not provided (Solana only)
            if amount_sol and not amount_lamports:
                amount_lamports = int(float(amount_sol) * 1_000_000_000)

            # Debug logging if fields are missing
            if not pay_req_id or not pay_to_address:
                import sys
                print(f"[nukez] WARNING: 402 response missing expected fields", file=sys.stderr)
                print(f"[nukez] Response keys: {list(error_details.keys())}", file=sys.stderr)
                print(f"[nukez] pay_req_id={pay_req_id!r}, pay_to_address={pay_to_address!r}", file=sys.stderr)

            raise PaymentRequiredError(
                pay_req_id=str(pay_req_id) if pay_req_id else "",
                pay_to_address=str(pay_to_address) if pay_to_address else "",
                amount_sol=float(amount_sol) if amount_sol else 0.0,
                amount_lamports=int(amount_lamports) if amount_lamports else 0,
                network=str(network) if network else "devnet",
                pay_asset=str(pay_asset) if pay_asset else "SOL",
                amount=amount,
                amount_raw=amount_raw,
                token_address=token_address,
                token_decimals=token_decimals,
            )
        
        # 401/403 Authentication errors
        if response.status_code in (401, 403):
            message = error_details.get("message", "Authentication failed")
            missing_headers = error_details.get("missing_headers", [])
            
            # Check if it's an expired URL
            if "expired" in message.lower() or "url" in message.lower():
                raise URLExpiredError(operation="access")
            
            raise AuthenticationError(message=message, missing_headers=missing_headers)
        
        # 404 Not Found
        if response.status_code == 404:
            error_code = error_details.get("error_code", "").lower()
            message = error_details.get("message", "")
            
            # Check if it's a file not found
            if "file" in error_code or "file" in message.lower():
                filename = error_details.get("filename", "unknown")
                locker_id = error_details.get("locker_id", "")
                raise NukezFileNotFoundError(filename=filename, locker_id=locker_id)
            
            raise NukezError(
                f"Resource not found: {error_details.get('message', response.url)}",
                details=error_details
            )
        
        # 409 Conflict - often transaction propagation issues
        if response.status_code == 409:
            tx_sig = error_details.get("tx_sig", "")
            if tx_sig:
                raise TransactionNotFoundError(tx_sig=tx_sig)
            
            raise NukezError(
                f"Conflict: {error_details.get('message', 'Resource conflict')}",
                details=error_details
            )
        
        # 429 Rate Limited
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 60))
            raise RateLimitError(retry_after=retry_after)
        
        # 5xx Server Errors
        if response.status_code >= 500:
            error = NukezError(
                f"Server error ({response.status_code}). "
                "This may be temporary - try again in a few seconds.",
                details=error_details
            )
            error.retryable = True
            raise error
        
        # Generic error for other status codes
        message = error_details.get("message", f"HTTP {response.status_code}")
        raise NukezError(message, details=error_details)
    
    def get(
        self, 
        path: str, 
        params: dict = None, 
        headers: dict = None
    ) -> Dict[str, Any]:
        """
        Execute GET request.
        
        Args:
            path: API path (e.g., "/v1/price")
            params: Query parameters
            headers: Additional headers
            
        Returns:
            Parsed JSON response
            
        Raises:
            NukezError: On request failure
        """
        url = f"{self.base_url}{path}"
        
        try:
            response = self.session.get(
                url,
                params=params,
                headers=headers or {},
                timeout=self.timeout
            )
        except requests.Timeout:
            raise NukezError(f"Request timed out after {self.timeout}s: GET {path}")
        
        except requests.RequestException as e:
            raise NukezError(f"Request failed: GET {path}: {e}")
        
        if response.status_code >= 400:
            self._handle_error_response(response)
        
        return self._parse_json_response(response, "GET", path)
    
    def post(
        self, 
        path: str, 
        json: dict = None, 
        headers: dict = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Execute POST request.
        
        Args:
            path: API path
            json: Request body (will be JSON-encoded)
            headers: Additional headers
            **kwargs: Additional requests kwargs
            
        Returns:
            Parsed JSON response
        """
        url = f"{self.base_url}{path}"
        
        try:
            response = self.session.post(
                url,
                json=json,
                headers=headers or {},
                timeout=self.timeout,
                **kwargs
            )
        
        except requests.Timeout:
            raise NukezError(f"Request timed out after {self.timeout}s: POST {path}")
        
        except requests.RequestException as e:
            raise NukezError(f"Request failed: GET {path}: {e}")
        
        if response.status_code >= 400:
            self._handle_error_response(response)
        
        return self._parse_json_response(response, "POST", path)
    
    def delete(
        self, 
        path: str, 
        headers: dict = None
    ) -> Dict[str, Any]:
        """
        Execute DELETE request.
        
        Args:
            path: API path
            headers: Additional headers
            
        Returns:
            Parsed JSON response
        """
        url = f"{self.base_url}{path}"
        
        try:
            response = self.session.delete(
                url,
                headers=headers or {},
                timeout=self.timeout
            )
        
        except requests.Timeout:
            raise NukezError(f"Request timed out after {self.timeout}s: DELETE {path}")
        
        except requests.RequestException as e:
            raise NukezError(f"Request failed: GET {path}: {e}")
        
        if response.status_code >= 400:
            self._handle_error_response(response)
        
        return self._parse_json_response(response, "DELETE", path)
    
    def put(
        self, 
        path: str, 
        json: dict = None,
        data: bytes = None,
        headers: dict = None
    ) -> Dict[str, Any]:
        """
        Execute PUT request.
        
        Args:
            path: API path
            json: Request body as dict (will be JSON-encoded)
            data: Raw request body as bytes
            headers: Additional headers
            
        Returns:
            Parsed JSON response
        """
        url = f"{self.base_url}{path}"
        
        try:
            response = self.session.put(
                url,
                json=json,
                data=data,
                headers=headers or {},
                timeout=self.timeout
            )
        
        except requests.Timeout:
            raise NukezError(f"Request timed out after {self.timeout}s: PUT {path}")
        
        except requests.RequestException as e:
            raise NukezError(f"Request failed: GET {path}: {e}")
        
        if response.status_code >= 400:
            self._handle_error_response(response)
        
        return self._parse_json_response(response, "PUT", path)
