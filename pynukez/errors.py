"""
Agent-friendly error classes for Nukez SDK.

Every error includes actionable guidance for autonomous agents.
Error messages tell agents exactly how to fix problems.

Design Philosophy:
- Errors are part of the API, not just failure modes
- Each error tells the agent what to do next
- The `retryable` flag indicates if retry might help
- Details dict provides structured error information
"""


class NukezError(Exception):
    """
    Base exception for Nukez SDK operations.

    All Nukez exceptions inherit from this class.

    Attributes:
        message: Human-readable error message
        details: Structured error information dict
        retryable: Whether retrying might succeed
    """
    
    def __init__(self, message: str, details: dict = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.retryable = False
    
    def __str__(self):
        return self.message


class PaymentRequiredError(NukezError):
    """
    HTTP 402 Payment Required - contains payment instructions.

    This is NOT an error in the traditional sense - it's the expected response
    from request_storage(). The SDK converts this to a StorageRequest object.

    Multi-chain support (Phase 2):
      - Solana: amount_sol / amount_lamports populated, pay_asset="SOL"
      - EVM/Monad: amount / amount_raw / token_address / token_decimals populated

    If you see this error directly, use the payment fields to execute the transfer:

        try:
            client.http.post("/v1/storage/request", ...)
        except PaymentRequiredError as e:
            # e.pay_to_address - where to send payment
            # e.amount_sol - how much SOL to send (Solana)
            # e.amount - human-readable amount (EVM)
            # e.pay_req_id - save for confirm_storage()

    Attributes:
        pay_req_id: Payment request ID (save for confirm_storage)
        pay_to_address: Address to send payment (Solana pubkey or 0x EVM address)
        amount_sol: Amount in SOL (Solana payments)
        amount_lamports: Amount in lamports (Solana payments)
        network: Payment network identifier
        pay_asset: Token symbol ("SOL", "USDC", "USDT", "MON", "WETH")
        amount: Human-readable amount string (EVM payments)
        amount_raw: Atomic units as integer (EVM payments)
        token_address: ERC-20 contract address (EVM payments)
        token_decimals: Token decimal places (EVM payments)
    """

    def __init__(
        self,
        pay_req_id: str,
        pay_to_address: str,
        amount_sol: float,
        amount_lamports: int,
        network: str,
        # EVM / multi-chain fields (Phase 2) — all optional for backward compat
        pay_asset: str = "SOL",
        amount: str = "",
        amount_raw: int = 0,
        token_address: str = "",
        token_decimals: int = 0,
    ):
        is_evm = any(tag in (network or "") for tag in ("monad", "ethereum", "evm", "arbitrum"))
        if is_evm and amount:
            message = (
                f"Payment required: {amount} {pay_asset} to {pay_to_address} on {network}. "
                f"Use pay_req_id '{pay_req_id}' in confirm_storage() after payment."
            )
        else:
            message = (
                f"Payment required: {amount_sol} SOL to {pay_to_address}. "
                f"Use pay_req_id '{pay_req_id}' in confirm_storage() after payment."
            )
        details = {
            "pay_req_id": pay_req_id,
            "pay_to_address": pay_to_address,
            "amount_sol": amount_sol,
            "amount_lamports": amount_lamports,
            "network": network,
            "pay_asset": pay_asset,
        }
        if amount:
            details["amount"] = amount
        if amount_raw:
            details["amount_raw"] = amount_raw
        if token_address:
            details["token_address"] = token_address
        if token_decimals:
            details["token_decimals"] = token_decimals
        super().__init__(message, details=details)
        self.pay_req_id = pay_req_id
        self.pay_to_address = pay_to_address
        self.amount_sol = amount_sol
        self.amount_lamports = amount_lamports
        self.network = network
        self.pay_asset = pay_asset
        self.amount = amount
        self.amount_raw = amount_raw
        self.token_address = token_address
        self.token_decimals = token_decimals
        self.retryable = False  # Need to pay first


class TransactionNotFoundError(NukezError):
    """
    Solana transaction not yet visible on-chain.
    
    The payment was sent but hasn't propagated to all RPC nodes yet.
    This is a transient condition - wait and retry.
    
    Recovery:
        1. Wait for suggested_delay seconds
        2. Retry confirm_storage() with the same parameters
        3. If still failing after 60 seconds, check transaction on Solana explorer
    
    Attributes:
        tx_sig: Transaction signature that wasn't found
        suggested_delay: Recommended wait time in seconds
    """
    
    def __init__(self, tx_sig: str, suggested_delay: int = 2):
        message = (
            f"Transaction {tx_sig[:16]}... not found on chain (may still be propagating). "
            f"Wait {suggested_delay} seconds and retry confirm_storage()."
        )
        super().__init__(
            message,
            details={"tx_sig": tx_sig, "suggested_delay": suggested_delay}
        )
        self.tx_sig = tx_sig
        self.suggested_delay = suggested_delay
        self.retryable = True


class AuthenticationError(NukezError):
    """
    Authentication failed - signed envelope rejected by server.
    
    Common causes:
    - Wrong keypair (must match the one used for payment)
    - Envelope expired (valid for 5 minutes after creation)
    - Invalid signature or envelope format
    - Mismatched receipt_id
    
    Recovery:
        1. Verify you're using the same keypair used for payment
        2. Try generating a fresh envelope (they expire after 5 minutes)
        3. Check that receipt_id matches a valid receipt
    
    Attributes:
        missing_headers: List of required headers that were missing (if any)
    """
    
    def __init__(self, message: str = None, missing_headers: list = None):
        default_message = (
            "Authentication failed. Verify you're using the correct keypair "
            "and that the signed envelope hasn't expired."
        )
        super().__init__(
            message or default_message,
            details={"missing_headers": missing_headers or []}
        )
        self.missing_headers = missing_headers or []
        self.retryable = False


class NukezFileNotFoundError(NukezError):
    """
    File doesn't exist in locker.
    
    The requested file was not found. This could mean:
    - File was never created
    - File was deleted
    - Filename is misspelled
    
    Recovery:
        1. Use list_files(receipt_id) to see what files exist
        2. Use create_file(receipt_id, filename) to create new files
        3. Check filename spelling matches exactly
    
    Attributes:
        filename: The file that wasn't found
        locker_id: The locker that was searched (if available)
    """
    
    def __init__(self, filename: str, locker_id: str = ""):
        message = (
            f"File not found: '{filename}'. "
            "Use list_files() to see existing files, or create_file() to create new ones."
        )
        super().__init__(
            message,
            details={"filename": filename, "locker_id": locker_id}
        )
        self.filename = filename
        self.locker_id = locker_id
        self.retryable = False


FileNotFound = NukezFileNotFoundError


class URLExpiredError(NukezError):
    """
    Signed URL has expired.
    
    Upload/download URLs have a TTL (default 30 minutes).
    After expiration, the URL returns HTTP 403 Forbidden.
    
    Recovery:
        1. Call get_file_urls(receipt_id, filename) to get fresh URLs
        2. Use the new URLs immediately
        3. Consider requesting longer TTL if operations take time
    
    Attributes:
        operation: What operation was attempted ("upload" or "download")
    """
    
    def __init__(self, operation: str = "upload"):
        message = (
            f"Signed {operation} URL has expired. "
            f"Call get_file_urls() to get a fresh {operation} URL."
        )
        super().__init__(
            message,
            details={"operation": operation}
        )
        self.operation = operation
        self.retryable = True  # Get fresh URL and retry


class NukezNotProvisionedError(NukezError):
    """
    Storage locker has not been provisioned yet.
    
    File operations require a provisioned locker. The receipt exists
    but provision_locker() hasn't been called yet.
    
    Recovery:
        1. Call provision_locker(receipt_id) to create the locker
        2. Then proceed with file operations
    
    Attributes:
        receipt_id: The receipt that needs provisioning
    """
    
    def __init__(self, receipt_id: str):
        message = (
            f"Locker not provisioned for receipt '{receipt_id}'. "
            "Call provision_locker(receipt_id) first."
        )
        super().__init__(
            message,
            details={"receipt_id": receipt_id}
        )
        self.receipt_id = receipt_id
        self.retryable = False


class InsufficientFundsError(NukezError):
    """
    Wallet doesn't have enough SOL for the operation.
    
    The Solana wallet balance is too low to complete the payment
    including transaction fees.
    
    Recovery:
        1. Check balance with get_wallet_info()
        2. Add SOL to your wallet
        3. On devnet, use: solana airdrop 2 --url devnet
    
    Attributes:
        required_sol: Amount of SOL needed
        available_sol: Current wallet balance
        network: Solana network
    """
    
    def __init__(self, required_sol: float, available_sol: float, network: str = "devnet"):
        if network == "devnet":
            recovery = "Get devnet SOL with: solana airdrop 2 --url devnet"
        else:
            recovery = "Transfer SOL to your wallet"
        
        message = (
            f"Insufficient funds: need {required_sol} SOL, have {available_sol} SOL. "
            f"{recovery}"
        )
        super().__init__(
            message,
            details={
                "required_sol": required_sol,
                "available_sol": available_sol,
                "network": network
            }
        )
        self.required_sol = required_sol
        self.available_sol = available_sol
        self.network = network
        self.retryable = False


class RateLimitError(NukezError):
    """
    API rate limit exceeded.
    
    Too many requests in a short time period.
    
    Recovery:
        1. Wait for retry_after seconds
        2. Implement exponential backoff for subsequent requests
    
    Attributes:
        retry_after: Seconds to wait before retrying
    """
    
    def __init__(self, retry_after: int = 60):
        message = (
            f"Rate limit exceeded. Wait {retry_after} seconds before retrying."
        )
        super().__init__(
            message,
            details={"retry_after": retry_after}
        )
        self.retry_after = retry_after
        self.retryable = True
