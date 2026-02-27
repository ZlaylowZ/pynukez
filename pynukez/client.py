#pynukez/pynukez/client.py
"""
Nukez client - agent-native storage operations.

Every method is a tool that maps to one HTTP operation.
Explicit inputs/outputs - agents know exactly what to pass and what they get back.

FIXED: confirm_storage now has proper retry logic for tx_not_found,
matching the working nukez implementation.
"""

import base64
import binascii
import hashlib
import json
import mimetypes
import os
import threading
import time
import uuid
import requests as raw_requests
from pathlib import Path
from typing import Dict, List, Optional, Union, Any, Callable
from dataclasses import dataclass
from urllib.parse import urlencode
from .types import (
    StorageRequest,
    TransferResult,
    Receipt,
    NukezManifest,
    FileUrls,
    FileInfo,
    UploadResult,
    DeleteResult,
    VerificationResult,
    WalletInfo,
    PriceInfo,
    ConfirmResult,
    BatchConfirmResult,
    AttestResult,
    BatchUploadResult,
    DownloadedFile,
    BatchDownloadResult,
    ViewerLink,
    FileViewerInfo,
    ViewerFileList,
    ViewerContainer,
)

from .auth import Keypair, build_signed_envelope, compute_locker_id
from .errors import NukezError, PaymentRequiredError, TransactionNotFoundError
from .hardening import sanitize_upload_data, validate_signed_url
from ._http import HTTPClient

# Lazy import for optional Solana support
SolanaPayment = None

VIEWER_RENDERER_CONTRACT_NAME = "nukez.mcp.viewer_link"
VIEWER_RENDERER_CONTRACT_VERSION = "1.0"
VIEWER_RENDERER_VARIANT = "nukez-neon"
VIEWER_CONTAINER_CONTRACT_NAME = "nukez.viewer_container"
VIEWER_CONTAINER_CONTRACT_VERSION = "1.0.0"
UPLOAD_STRING_MAX_BYTES = int(os.getenv("PYNUKEZ_UPLOAD_STRING_MAX_BYTES", "262144"))
SANDBOX_INGEST_DEFAULT_PART_BYTES = int(os.getenv("PYNUKEZ_SANDBOX_INGEST_PART_BYTES", "196608"))
SANDBOX_INGEST_MAX_PART_BYTES = 512 * 1024
SANDBOX_INGEST_MIN_PART_BYTES = 4 * 1024
SANDBOX_INGEST_EXECUTION_MODE = os.getenv("PYNUKEZ_SANDBOX_EXECUTION_MODE", "sandbox")

_SANDBOX_PATH_BLOCKED_MARKERS = (
    "file arg rewrite paths are required",
    "proxied mounts are present",
    "proxied mount",
    "path rewrite",
    "sandbox_path_unavailable",
    "/mnt/data",
    "/mnt/user-data/uploads",
)

def _get_solana_payment():
    """Lazy import SolanaPayment to avoid requiring solana libs for non-payment ops."""
    global SolanaPayment
    if SolanaPayment is None:
        try:
            from .payment import SolanaPayment as _SolanaPayment
            SolanaPayment = _SolanaPayment
        except ImportError:
            raise ImportError(
                "Solana libraries required for payment operations. "
                "Install with: pip install pynukez[solana]"
            )
    return SolanaPayment

class Nukez:
    """
    Agent-native Nukez client.
    
    Each method is a self-contained tool operation designed for LLM function calling.
    Methods use explicit parameters - agents always know what to pass.
    
    Basic Usage:
        client = Nukez(keypair_path="~/.config/solana/id.json")
        
        # Payment flow (explicit steps)
        request = client.request_storage(units=1)
        transfer = client.solana_transfer(request.pay_to_address, request.amount_sol)
        receipt = client.confirm_storage(request.pay_req_id, transfer.signature)
        
        # File operations (require receipt_id)
        manifest = client.provision_locker(receipt.id)
        urls = client.create_file(receipt.id, "data.txt")
        client.upload_bytes(urls.upload_url, b"Hello!")
        data = client.download_bytes(urls.download_url)
    
    Note:
        Most file operations require receipt_id (not locker_id) because the 
        signed envelope authentication uses receipt_id. Agents should track
        the receipt_id returned from confirm_storage().
    """
    
    def __init__(
        self,
        keypair_path: Optional[Union[str, Path]] = None,
        base_url: str = "https://api.nukez.xyz",
        network: str = "devnet",
        rpc_url: Optional[str] = "https://api.devnet.solana.com",
        timeout: int = None
    ):
        """
        Initialize Nukez client.
        
        Args:
            keypair_path: Path to Solana keypair file (required for payments and signed requests)
            base_url: Nukez API base URL
            network: Solana network ("devnet" or "mainnet-beta")
            
        Example:
            client = Nukez(keypair_path="~/.config/solana/id.json", network="devnet")
        """
        
        self.base_url = base_url.rstrip('/')
        self.network = network
        self.rpc_url = rpc_url
        self.timeout = timeout or 120
        self.http = HTTPClient(base_url, timeout=self.timeout)
        
        # Optional keypair for signing operations
        self.keypair: Optional[Keypair] = None
        if keypair_path:
            self.keypair = Keypair(keypair_path)
        
        # Lazy-initialized payment handler
        self._payment = None
        self._keypair_path = keypair_path
        self._upload_jobs: Dict[str, Dict[str, Any]] = {}
        self._upload_jobs_lock = threading.Lock()

    def _require_keypair(self, operation: str) -> Keypair:
        """Ensure keypair is available, with helpful error message."""
        if not self.keypair:
            raise NukezError(
                f"{operation} requires keypair_path. "
                f"Initialize Nukez(keypair_path='~/.config/solana/id.json')"
            )
        return self.keypair
    
    # =========================================================================
    # DISCOVERY & PRICING (No auth required)
    # =========================================================================
    
    def get_price(self, units: int = 1) -> PriceInfo:
        """
        Get current storage pricing.
        
        Args:
            units: Number of storage units
            
        Returns:
            PriceInfo with pricing details
        """
        response = self.http.get("/v1/price", params={"units": units})
        
        return PriceInfo(
            units=units,
            unit_price_usd=response.get("unit_price_usd", 0.0),
            total_usd=response.get("total_usd", 0.0),
            amount_sol=response.get("amount_sol", 0.0),
            amount_lamports=response.get("amount_lamports", 0),
            network=response.get("network", self.network)
        )
    
    # =========================================================================
    # PAYMENT FLOW (3 explicit steps)
    # =========================================================================
    
    def request_storage(
        self,
        units: int = 1,
        provider: str = None,
        pay_network: str = None,
        pay_asset: str = None,
    ) -> StorageRequest:
        """
        Step 1: Start the x402 payment flow to purchase storage.

        Args:
            units: Number of storage units to purchase
            provider: Optional storage backend ("gcs", "mongodb"). Default: server
                      default (gcs). MongoDB is for document/RAG workloads (16MB limit).
            pay_network: Payment chain. Default: Solana devnet.
                         Examples: "solana-devnet", "monad-testnet", "monad-mainnet"
            pay_asset: Token to pay with. Default: "SOL".
                       Examples: "SOL", "USDC", "USDT", "MON", "WETH"

        Returns:
            StorageRequest with payment instructions:
            - pay_req_id: Save this for confirm_storage()
            - pay_to_address: Address to send payment (Solana pubkey or 0x address)
            - amount_sol / amount_lamports: Populated for Solana payments
            - amount / amount_raw / token_address: Populated for EVM payments
            - pay_asset: Token symbol ("SOL", "USDC", etc.)
            - network: Payment network identifier
            - next_step: Instructions for what to do next

        Note:
            This endpoint returns HTTP 402 Payment Required - this is expected behavior,
            not an error. The response contains the payment instructions.
        """
        try:
            body = {"units": units}
            if provider:
                body["provider"] = provider
            if pay_network:
                body["pay_network"] = pay_network
            if pay_asset:
                body["pay_asset"] = pay_asset
            self.http.post("/v1/storage/request", json=body)
            # If we get here, something unexpected happened
            raise NukezError(
                "Expected HTTP 402 Payment Required, got success. "
                "This indicates an API change - please report this issue."
            )
        except PaymentRequiredError as e:
            # HTTP 402 is expected - contains payment instructions
            request = StorageRequest(
                pay_req_id=e.pay_req_id,
                pay_to_address=e.pay_to_address,
                amount_sol=e.amount_sol,
                amount_lamports=e.amount_lamports,
                network=e.network,
                units=units,
                provider=provider or "gcs",
                pay_asset=e.pay_asset,
                amount=e.amount or None,
                amount_raw=e.amount_raw or None,
                token_address=e.token_address or None,
                token_decimals=e.token_decimals or None,
            )
            return request
    
    def solana_transfer(
        self, 
        to_address: str, 
        amount_sol: Union[str, float]
    ) -> TransferResult:
        """
        Step 2: Execute Solana SOL transfer.
        
        Args:
            to_address: Destination Solana address (from request.pay_to_address)
            amount_sol: Amount in SOL (from request.amount_sol)
            
        Returns:
            TransferResult with:
            - signature: Transaction signature (use in confirm_storage)
            - to_address: Destination address
            - amount_sol: Amount transferred
            - network: Solana network
        """
        self._require_keypair("solana_transfer")
        
        # Lazy initialize payment handler
        if self._payment is None:
            PaymentClass = _get_solana_payment()
            self._payment = PaymentClass(
                keypair_path=str(self.keypair.keypair_path),
                network=self.network,
                rpc_url=self.rpc_url,
            )
        
        signature = self._payment.transfer_sol(
            to_address=to_address,
            amount_sol=float(amount_sol)
        )
        
        result = TransferResult(
            signature=signature,
            to_address=to_address,
            amount_sol=float(amount_sol),
            network=self.network
        )

        return result
    
    def confirm_storage(
        self, 
        pay_req_id: str, 
        tx_sig: str,
        max_retries: int = 5,
        initial_delay: float = 2.0
    ) -> Receipt:
        """
        Step 3: Confirm payment and receive storage receipt.
        
        FIXED: Now includes retry logic for transaction propagation delays,
        matching the working nukez implementation.
        
        Args:
            pay_req_id: Payment request ID from request_storage()
            tx_sig: Transaction signature from solana_transfer()
            max_retries: Maximum retry attempts for tx_not_found (default: 5)
            initial_delay: Initial delay in seconds, doubles each retry (default: 2.0)
            
        Returns:
            Receipt with:
            - id: Receipt ID (SAVE THIS - needed for all file operations)
            - units: Storage units purchased
            - payer_pubkey: Your wallet address
            - network: Solana network
            - locker_id: Derived locker ID (convenience property)
            
        Next step:
            Call provision_locker(receipt_id=receipt.id) to create storage space
            
        Note:
            Transaction may take 10-30 seconds to confirm on-chain.
            This method automatically retries on tx_not_found errors.
        """
        url = f"{self.base_url}/v1/storage/confirm"
        payload = {"pay_req_id": pay_req_id}
        headers = {
            "Content-Type": "application/json",
            "X402-TX": tx_sig
        }
        
        last_error: Optional[Exception] = None
        
        for attempt in range(max_retries):
            try:
                # Make raw request - don't use self.http which raises PaymentRequiredError
                resp = raw_requests.post(
                    url, 
                    json=payload, 
                    headers=headers, 
                    timeout=self.timeout
                )
                
                # Success!
                if resp.status_code == 200:
                    data = resp.json()
                    receipt = Receipt(
                        id=data["receipt_id"],
                        units=data.get("units", 1),
                        payer_pubkey=data.get("payer_pubkey", ""),
                        network=data.get("network", self.network),
                        created_at=data.get("created_at")
                    )
                    return receipt
                
                # 402 from confirm endpoint - check if it's tx_not_found
                if resp.status_code == 402:
                    try:
                        body = resp.json()
                    except:
                        body = {"raw": resp.text}
                    
                    # Check for tx_not_found in various response formats
                    # Format 1: details.verify.err
                    verify_info = (body.get("details") or {}).get("verify") or {}
                    err = verify_info.get("err", "")
                    
                    # Format 2: error_code at top level
                    error_code = body.get("error_code", "")
                    
                    # Format 3: message contains tx_not_found
                    message = body.get("message", "")
                    
                    is_tx_not_found = (
                        err == "tx_not_found" or
                        error_code == "TX_NOT_FOUND" or
                        "tx_not_found" in message.lower() or
                        "transaction" in message.lower() and "not found" in message.lower()
                    )
                    
                    if is_tx_not_found and attempt < max_retries - 1:
                        delay = initial_delay * (2 ** attempt)
                        print(f"[pynukez] Transaction not found, retrying in {delay}s (attempt {attempt + 1}/{max_retries})")
                        time.sleep(delay)
                        continue
                    
                    # Not tx_not_found or out of retries
                    last_error = TransactionNotFoundError(
                        tx_sig=tx_sig,
                        suggested_delay=int(initial_delay * (2 ** attempt))
                    ) if is_tx_not_found else NukezError(
                        f"Payment confirmation failed: {body.get('message', resp.text)}",
                        details=body
                    )
                    raise last_error
                
                # Other error status
                resp.raise_for_status()
                
            except raw_requests.RequestException as e:
                last_error = NukezError(f"Request failed: {e}")
                if attempt < max_retries - 1:
                    time.sleep(initial_delay * (2 ** attempt))
                    continue
                raise last_error
        
        # Exhausted retries
        if last_error:
            raise last_error
        raise NukezError(f"confirm_storage failed after {max_retries} attempts")

    # =========================================================================
    # LOCKER OPERATIONS (Require receipt_id and signed envelope)
    # =========================================================================
    
    def provision_locker(
        self, 
        receipt_id: str, 
        tags: Optional[List[str]] = None
    ) -> NukezManifest:
        """
        Create storage locker namespace for files.
        
        Args:
            receipt_id: Receipt ID from confirm_storage()
            tags: Optional tags for the locker
            
        Returns:
            NukezManifest with locker details
        """
        keypair = self._require_keypair("provision_locker")
        
        locker_id = compute_locker_id(receipt_id)
        body = {"receipt_id": receipt_id, "tags": tags or []}
        
        envelope = build_signed_envelope(
            keypair=keypair,
            receipt_id=receipt_id,
            method="POST",
            path="/v1/storage/signed_provision",
            ops=["locker:provision"],
            body=body
        )
        
        response = self.http.post(
            "/v1/storage/signed_provision",
            json=body,
            headers=envelope.headers
        )
        
        space = response.get("space", response)
        
        manifest = NukezManifest(
            locker_id=space.get("locker_id", locker_id),
            receipt_id=receipt_id,
            bucket=space.get("bucket", ""),
            path_prefix=space.get("path_prefix", ""),
            tags=space.get("tags", tags or []),
            cap_token=space.get("cap_token"),
            cap_expires_in_sec=space.get("cap_expires_in_sec"),
            created_at=space.get("created_at")
        )

        return manifest
    
    def create_file(
        self, 
        receipt_id: str, 
        filename: str,
        content_type: str = "application/octet-stream",
        ttl_min: int = 30
    ) -> FileUrls:
        """
        Create a new file and get upload/download URLs.
        
        Args:
            receipt_id: Receipt ID from confirm_storage()
            filename: Name for the file
            content_type: MIME type (default: application/octet-stream)
            ttl_min: URL expiration time in minutes (default: 30)
            
        Returns:
            FileUrls with:
            - filename: The file name
            - upload_url: PUT data here
            - download_url: GET data from here
            - content_type: MIME type
            - expires_in_sec: URL validity duration
        """
        keypair = self._require_keypair("create_file")
        locker_id = compute_locker_id(receipt_id)
        
        body = {
            "filename": filename,
            "content_type": content_type,
            "ttl_min": ttl_min
        }
        
        envelope = build_signed_envelope(
            keypair=keypair,
            receipt_id=receipt_id,
            method="POST",
            path=f"/v1/lockers/{locker_id}/files",
            ops=["locker:write"],
            body=body
        )
        
        response = self.http.post(
            f"/v1/lockers/{locker_id}/files",
            json=body,
            headers=envelope.headers
        )
        
        urls = FileUrls(
            filename=response.get("filename", filename),
            upload_url=response["upload_url"],
            download_url=response["download_url"],
            content_type=response.get("content_type", content_type),
            expires_in_sec=response.get("urls_expire_in_sec", ttl_min * 60)
        )
        return urls

    @staticmethod
    def _infer_content_type(filename: str, explicit: Optional[str] = None) -> str:
        """Infer MIME type from filename when explicit value is not provided."""
        if explicit:
            return explicit
        guessed = mimetypes.guess_type(filename)[0]
        return guessed or "application/octet-stream"

    def create_files_batch(
        self,
        receipt_id: str,
        files: List[Dict[str, Any]],
        ttl_min: int = 30,
    ) -> Dict[str, Any]:
        """
        Create multiple file entries and mint upload/download URLs in one call.

        This maps to POST /v1/lockers/{locker_id}/files/batch and is the
        low-level primitive used by path-based bulk upload methods.
        """
        keypair = self._require_keypair("create_files_batch")
        locker_id = compute_locker_id(receipt_id)

        if not files:
            raise NukezError("create_files_batch requires at least one file")

        normalized_files: List[Dict[str, Any]] = []
        for file_spec in files:
            filename = (file_spec.get("filename") or "").strip()
            if not filename:
                raise NukezError("create_files_batch file spec missing filename")

            row: Dict[str, Any] = {
                "filename": filename,
                "content_type": self._infer_content_type(
                    filename,
                    file_spec.get("content_type"),
                ),
            }
            expected_hash = (file_spec.get("expected_hash") or "").strip()
            if expected_hash:
                row["expected_hash"] = expected_hash
            normalized_files.append(row)

        body = {
            "files": normalized_files,
            "ttl_min": ttl_min,
        }

        envelope = build_signed_envelope(
            keypair=keypair,
            receipt_id=receipt_id,
            method="POST",
            path=f"/v1/lockers/{locker_id}/files/batch",
            ops=["locker:write"],
            body=body,
        )

        return self.http.post(
            f"/v1/lockers/{locker_id}/files/batch",
            json=body,
            headers=envelope.headers,
        )

    def _normalize_path_sources(
        self,
        sources: List[Union[str, Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        """Normalize file path upload specs into a canonical internal format."""
        if not sources:
            raise NukezError("No file sources provided")

        normalized: List[Dict[str, Any]] = []
        for idx, raw in enumerate(sources):
            if isinstance(raw, str):
                spec: Dict[str, Any] = {"filepath": raw}
            elif isinstance(raw, dict):
                spec = dict(raw)
            else:
                raise NukezError(
                    f"Invalid source at index {idx}: expected str or dict, got {type(raw).__name__}"
                )

            filepath = str(spec.get("filepath") or spec.get("path") or "").strip()
            if not filepath:
                raise NukezError(f"Source {idx} missing filepath")

            p = Path(filepath).expanduser()
            if not p.exists():
                p_text = str(p)
                if "/mnt/data" in p_text or "/mnt/user-data/uploads" in p_text:
                    raise NukezError(
                        f"SANDBOX_PATH_UNAVAILABLE: {p_text}",
                        details={
                            "filepath": p_text,
                            "recovery_hint": (
                                "Path upload is blocked in this sandbox runtime. "
                                "Use sandbox_create_ingest_job -> sandbox_append_ingest_part -> sandbox_complete_ingest_job."
                            ),
                            "next_best_method": (
                                "sandbox_create_ingest_job -> sandbox_append_ingest_part -> sandbox_complete_ingest_job"
                            ),
                        },
                    )
                raise NukezError(f"File not found: {p}")
            if not p.is_file():
                raise NukezError(f"Expected file path, got non-file: {p}")

            filename = str(spec.get("filename") or p.name).strip()
            if not filename:
                raise NukezError(f"Source {idx} produced empty filename")

            normalized.append(
                {
                    "filepath": str(p),
                    "filename": filename,
                    "content_type": self._infer_content_type(
                        filename,
                        spec.get("content_type"),
                    ),
                    "size_bytes": p.stat().st_size,
                    "expected_hash": spec.get("expected_hash"),
                }
            )

        return normalized

    def upload_file_path(
        self,
        receipt_id: str,
        filepath: str,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        ttl_min: int = 30,
        confirm: bool = True,
    ) -> Dict[str, Any]:
        """
        Upload one local file by path without passing file bytes through LLM context.
        """
        source: Dict[str, Any] = {"filepath": filepath}
        if filename:
            source["filename"] = filename
        if content_type:
            source["content_type"] = content_type

        result = self.bulk_upload_paths(
            receipt_id=receipt_id,
            sources=[source],
            workers=1,
            ttl_min=ttl_min,
            confirm=confirm,
            auto_attest=False,
        )
        if result["failed"] > 0:
            err = result.get("errors") or [{"error": "upload failed"}]
            first_error = err[0]
            first_message = str(first_error.get("error", "upload failed"))
            if self._is_sandbox_path_unavailable_error(
                NukezError(first_message, details=first_error)
            ):
                raise NukezError(
                    f"SANDBOX_PATH_UNAVAILABLE: {first_message}",
                    details={
                        "recovery_hint": (
                            "Path upload is blocked in this sandbox runtime. "
                            "Use sandbox_create_ingest_job -> sandbox_append_ingest_part -> sandbox_complete_ingest_job."
                        ),
                        "next_best_method": (
                            "sandbox_create_ingest_job -> sandbox_append_ingest_part -> sandbox_complete_ingest_job"
                        ),
                    },
                )
            raise NukezError(f"upload_file_path failed: {first_message}")
        return result["files"][0] if result.get("files") else result

    def bulk_upload_paths(
        self,
        receipt_id: str,
        sources: List[Union[str, Dict[str, Any]]],
        workers: int = 6,
        ttl_min: int = 30,
        confirm: bool = True,
        auto_attest: bool = False,
        attest_sync: bool = False,
        on_progress: Optional[Callable[[str, bool, int, int], None]] = None,
    ) -> Dict[str, Any]:
        """
        Upload multiple local files by path with optional confirm + attestation.

        This is context-safe for MCP/LLM flows because only file paths are passed
        through tool calls; file bytes are read directly from disk in the SDK.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        normalized = self._normalize_path_sources(sources)
        t0 = time.time()

        create_specs = [
            {
                "filename": s["filename"],
                "content_type": s["content_type"],
                "expected_hash": s.get("expected_hash"),
            }
            for s in normalized
        ]
        create_response = self.create_files_batch(
            receipt_id=receipt_id,
            files=create_specs,
            ttl_min=ttl_min,
        )

        url_by_filename = {
            row.get("filename"): row
            for row in (create_response.get("files") or [])
            if row.get("filename")
        }

        total = len(normalized)
        file_results: List[Dict[str, Any]] = []
        errors: List[Dict[str, Any]] = []

        def _upload_one(spec: Dict[str, Any]) -> Dict[str, Any]:
            filename = spec["filename"]
            entry = url_by_filename.get(filename)
            if not entry:
                return {
                    "filename": filename,
                    "filepath": spec["filepath"],
                    "content_type": spec["content_type"],
                    "size_bytes": 0,
                    "success": False,
                    "error": "Missing upload URL for file from batch create response",
                }

            try:
                body = Path(spec["filepath"]).read_bytes()
                self.upload_bytes(
                    entry["upload_url"],
                    body,
                    content_type=spec["content_type"],
                )
                return {
                    "filename": filename,
                    "filepath": spec["filepath"],
                    "content_type": spec["content_type"],
                    "size_bytes": len(body),
                    "success": True,
                    "error": "",
                }
            except Exception as e:
                return {
                    "filename": filename,
                    "filepath": spec["filepath"],
                    "content_type": spec["content_type"],
                    "size_bytes": 0,
                    "success": False,
                    "error": str(e),
                }

        worker_count = max(1, int(workers or 1))
        uploaded_names: List[str] = []

        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = [pool.submit(_upload_one, spec) for spec in normalized]
            for idx, fut in enumerate(as_completed(futures), 1):
                row = fut.result()
                file_results.append(row)
                if row["success"]:
                    uploaded_names.append(row["filename"])
                else:
                    errors.append(
                        {
                            "filename": row["filename"],
                            "filepath": row["filepath"],
                            "error": row["error"],
                        }
                    )
                if on_progress:
                    try:
                        on_progress(row["filename"], row["success"], idx, total)
                    except Exception:
                        pass

        confirmed_map: Dict[str, ConfirmResult] = {}
        if confirm and uploaded_names:
            confirm_result = self.confirm_files(receipt_id, uploaded_names)
            confirmed_map = {
                c.filename: c
                for c in confirm_result.results
                if c.confirmed
            }

        confirmed_count = 0
        for row in file_results:
            if row["success"] and row["filename"] in confirmed_map:
                c = confirmed_map[row["filename"]]
                row["content_hash"] = c.content_hash
                row["confirmed"] = True
                confirmed_count += 1
            elif row["success"]:
                row["content_hash"] = ""
                row["confirmed"] = False
            else:
                row["content_hash"] = ""
                row["confirmed"] = False

        attestation: Optional[Dict[str, Any]] = None
        if auto_attest and uploaded_names:
            try:
                att = self.attest(receipt_id, sync=attest_sync)
                attestation = {
                    "status": att.status,
                    "merkle_root": att.merkle_root,
                    "file_count": att.file_count,
                    "att_code": att.att_code,
                    "push_ok": att.push_ok,
                    "tx_signature": att.tx_signature,
                }
            except Exception as e:
                attestation = {
                    "status": "failed",
                    "error": str(e),
                }

        elapsed = round(time.time() - t0, 2)
        uploaded_count = len([r for r in file_results if r["success"]])

        return {
            "receipt_id": receipt_id,
            "uploaded": uploaded_count,
            "failed": len(file_results) - uploaded_count,
            "total": total,
            "confirmed": confirmed_count,
            "elapsed_sec": elapsed,
            "files": file_results,
            "errors": errors,
            "attestation": attestation,
        }

    def upload_directory(
        self,
        receipt_id: str,
        source_dir: str,
        pattern: str = "*",
        recursive: bool = False,
        exclude_pattern: Optional[str] = None,
        preserve_structure: bool = False,
        workers: int = 6,
        ttl_min: int = 30,
        confirm: bool = True,
        auto_attest: bool = False,
        attest_sync: bool = False,
    ) -> Dict[str, Any]:
        """
        Upload files from a directory using glob pattern filtering.
        """
        root = Path(source_dir).expanduser()
        if not root.exists():
            raise NukezError(f"Directory not found: {root}")
        if not root.is_dir():
            raise NukezError(f"Expected directory path, got: {root}")

        paths = root.rglob(pattern) if recursive else root.glob(pattern)
        sources: List[Dict[str, Any]] = []

        for p in paths:
            if not p.is_file():
                continue
            rel = str(p.relative_to(root))
            if exclude_pattern and (p.match(exclude_pattern) or Path(rel).match(exclude_pattern)):
                continue
            filename = rel if preserve_structure else p.name
            sources.append({"filepath": str(p), "filename": filename})

        if not sources:
            raise NukezError(
                f"No files matched pattern '{pattern}' in directory: {root}"
            )

        return self.bulk_upload_paths(
            receipt_id=receipt_id,
            sources=sources,
            workers=workers,
            ttl_min=ttl_min,
            confirm=confirm,
            auto_attest=auto_attest,
            attest_sync=attest_sync,
        )

    def _set_upload_job_state(self, job_id: str, **updates: Any) -> None:
        with self._upload_jobs_lock:
            job = self._upload_jobs.get(job_id)
            if not job:
                return
            job.update(updates)
            job["updated_at"] = int(time.time())

    def start_bulk_upload_job(
        self,
        receipt_id: str,
        sources: List[Union[str, Dict[str, Any]]],
        workers: int = 6,
        ttl_min: int = 30,
        confirm: bool = True,
        auto_attest: bool = False,
        attest_sync: bool = False,
    ) -> Dict[str, Any]:
        """
        Start a non-blocking background bulk upload job.

        Returns immediately with job_id. Poll with get_upload_job().
        """
        normalized = self._normalize_path_sources(sources)
        total = len(normalized)
        now = int(time.time())
        job_id = f"up_{uuid.uuid4().hex[:12]}"

        with self._upload_jobs_lock:
            self._upload_jobs[job_id] = {
                "job_id": job_id,
                "status": "queued",
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "finished_at": None,
                "receipt_id": receipt_id,
                "total": total,
                "uploaded": 0,
                "failed": 0,
                "confirmed": 0,
                "result": None,
                "error": None,
            }

        progress = {"uploaded": 0, "failed": 0}
        progress_lock = threading.Lock()

        def _progress_cb(_filename: str, success: bool, _index: int, _total: int) -> None:
            with progress_lock:
                if success:
                    progress["uploaded"] += 1
                else:
                    progress["failed"] += 1
                self._set_upload_job_state(
                    job_id,
                    status="running",
                    uploaded=progress["uploaded"],
                    failed=progress["failed"],
                )

        def _run_job() -> None:
            self._set_upload_job_state(job_id, status="running", started_at=int(time.time()))
            try:
                result = self.bulk_upload_paths(
                    receipt_id=receipt_id,
                    sources=normalized,
                    workers=workers,
                    ttl_min=ttl_min,
                    confirm=confirm,
                    auto_attest=auto_attest,
                    attest_sync=attest_sync,
                    on_progress=_progress_cb,
                )
                terminal = "complete" if result.get("failed", 0) == 0 else "partial"
                self._set_upload_job_state(
                    job_id,
                    status=terminal,
                    finished_at=int(time.time()),
                    uploaded=result.get("uploaded", 0),
                    failed=result.get("failed", 0),
                    confirmed=result.get("confirmed", 0),
                    result=result,
                )
            except Exception as e:
                self._set_upload_job_state(
                    job_id,
                    status="failed",
                    finished_at=int(time.time()),
                    error=str(e),
                )

        t = threading.Thread(
            target=_run_job,
            name=f"pynukez-upload-{job_id}",
            daemon=True,
        )
        t.start()

        return {
            "job_id": job_id,
            "status": "queued",
            "created_at": now,
            "total": total,
            "receipt_id": receipt_id,
        }

    def get_upload_job(self, job_id: str) -> Dict[str, Any]:
        """
        Return the current state of a previously started upload job.
        """
        with self._upload_jobs_lock:
            job = self._upload_jobs.get(job_id)
            if not job:
                raise NukezError(f"Upload job not found: {job_id}")
            return dict(job)

    def list_upload_jobs(self, limit: int = 20) -> Dict[str, Any]:
        """
        List recent upload jobs tracked in this SDK process.
        """
        with self._upload_jobs_lock:
            jobs = list(self._upload_jobs.values())
        jobs.sort(key=lambda j: j.get("created_at", 0), reverse=True)
        if limit > 0:
            jobs = jobs[:limit]
        return {
            "count": len(jobs),
            "jobs": [dict(j) for j in jobs],
        }

    @staticmethod
    def _normalize_expected_sha256(value: Optional[str]) -> Optional[str]:
        raw = (value or "").strip().lower()
        if not raw:
            return None
        if raw.startswith("sha256:"):
            raw = raw[7:]
        if len(raw) != 64 or any(c not in "0123456789abcdef" for c in raw):
            raise NukezError(
                "expected_sha256 must be 64 hex chars (optionally prefixed with sha256:)"
            )
        return f"sha256:{raw}"

    def _is_sandbox_path_unavailable_error(self, exc: Exception) -> bool:
        message = str(exc).lower()
        details_text = ""
        details = getattr(exc, "details", None)
        if details:
            try:
                details_text = json.dumps(details, sort_keys=True).lower()
            except Exception:
                details_text = str(details).lower()
        return any(marker in message or marker in details_text for marker in _SANDBOX_PATH_BLOCKED_MARKERS)

    def sandbox_create_ingest_job(
        self,
        receipt_id: str,
        files: List[Dict[str, Any]],
        execution_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create a sandbox-ingest job.

        This maps directly to:
            POST /v1/lockers/{locker_id}/ingest/jobs
        """
        keypair = self._require_keypair("sandbox_create_ingest_job")
        locker_id = compute_locker_id(receipt_id)

        if not files:
            raise NukezError("sandbox_create_ingest_job requires at least one file")

        normalized_files: List[Dict[str, Any]] = []
        for idx, spec in enumerate(files):
            filename = (spec.get("filename") or "").strip()
            if not filename:
                raise NukezError(f"sandbox_create_ingest_job file {idx} missing filename")

            row: Dict[str, Any] = {
                "filename": filename,
                "content_type": self._infer_content_type(
                    filename,
                    spec.get("content_type"),
                ),
            }

            if spec.get("expected_size_bytes") is not None:
                try:
                    row["expected_size_bytes"] = int(spec["expected_size_bytes"])
                except Exception as exc:
                    raise NukezError(
                        f"expected_size_bytes for '{filename}' must be an integer"
                    ) from exc

            normalized_sha = self._normalize_expected_sha256(
                spec.get("expected_sha256") or spec.get("expected_hash")
            )
            if normalized_sha:
                row["expected_sha256"] = normalized_sha

            normalized_files.append(row)

        body = {
            "receipt_id": receipt_id,
            "files": normalized_files,
            "execution_mode": (execution_mode or SANDBOX_INGEST_EXECUTION_MODE).strip().lower(),
        }
        path = f"/v1/lockers/{locker_id}/ingest/jobs"

        envelope = build_signed_envelope(
            keypair=keypair,
            receipt_id=receipt_id,
            method="POST",
            path=path,
            ops=["locker:write"],
            body=body,
        )

        return self.http.post(path, json=body, headers=envelope.headers)

    def sandbox_append_ingest_part(
        self,
        receipt_id: str,
        job_id: str,
        file_id: str,
        part_no: int,
        payload_b64: str,
        *,
        is_last: bool = False,
    ) -> Dict[str, Any]:
        """
        Append one base64 chunk into a sandbox-ingest job file.

        Maps to:
            POST /v1/ingest/jobs/{job_id}/files/{file_id}/parts
        """
        keypair = self._require_keypair("sandbox_append_ingest_part")

        raw_payload = (payload_b64 or "").strip()
        if not raw_payload:
            raise NukezError("sandbox_append_ingest_part requires non-empty payload_b64")
        if raw_payload.lower() in {"none", "null", "undefined"}:
            raise NukezError("payload_b64 sentinel values are rejected")

        try:
            base64.b64decode(raw_payload, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise NukezError("payload_b64 must be valid base64") from exc

        body = {
            "part_no": int(part_no),
            "payload_b64": raw_payload,
            "is_last": bool(is_last),
        }
        path = f"/v1/ingest/jobs/{job_id}/files/{file_id}/parts"

        envelope = build_signed_envelope(
            keypair=keypair,
            receipt_id=receipt_id,
            method="POST",
            path=path,
            ops=["locker:write"],
            body=body,
        )
        return self.http.post(path, json=body, headers=envelope.headers)

    def sandbox_complete_ingest_job(
        self,
        receipt_id: str,
        job_id: str,
        *,
        file_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Finalize a sandbox-ingest job.

        Maps to:
            POST /v1/ingest/jobs/{job_id}/complete
        """
        keypair = self._require_keypair("sandbox_complete_ingest_job")

        body: Dict[str, Any] = {}
        if file_ids:
            body["file_ids"] = list(file_ids)

        path = f"/v1/ingest/jobs/{job_id}/complete"
        envelope = build_signed_envelope(
            keypair=keypair,
            receipt_id=receipt_id,
            method="POST",
            path=path,
            ops=["locker:write"],
            body=body,
        )
        return self.http.post(path, json=body, headers=envelope.headers)

    def sandbox_upload_bytes(
        self,
        receipt_id: str,
        filename: str,
        data: bytes,
        *,
        content_type: Optional[str] = None,
        expected_sha256: Optional[str] = None,
        part_size_bytes: int = SANDBOX_INGEST_DEFAULT_PART_BYTES,
        execution_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Upload bytes through the sandbox-ingest job flow (chunked base64 parts).

        This is intended for sandboxed runtimes where local path uploads are blocked.
        """
        if not isinstance(data, (bytes, bytearray)):
            raise NukezError("sandbox_upload_bytes expects bytes")
        data_bytes = bytes(data)
        if not data_bytes:
            raise NukezError("sandbox_upload_bytes requires non-empty data")

        try:
            part_size = int(part_size_bytes)
        except Exception as exc:
            raise NukezError("part_size_bytes must be an integer") from exc
        part_size = max(SANDBOX_INGEST_MIN_PART_BYTES, min(part_size, SANDBOX_INGEST_MAX_PART_BYTES))

        normalized_sha = self._normalize_expected_sha256(expected_sha256)
        if not normalized_sha:
            normalized_sha = f"sha256:{hashlib.sha256(data_bytes).hexdigest()}"

        created = self.sandbox_create_ingest_job(
            receipt_id=receipt_id,
            files=[
                {
                    "filename": filename,
                    "content_type": self._infer_content_type(filename, content_type),
                    "expected_size_bytes": len(data_bytes),
                    "expected_sha256": normalized_sha,
                }
            ],
            execution_mode=execution_mode,
        )

        job_id = str(created.get("job_id") or "")
        files = created.get("files") or []
        file_id = str((files[0] or {}).get("file_id") or "") if files else ""
        if not job_id or not file_id:
            raise NukezError(
                "sandbox_create_ingest_job response missing job_id or file_id",
                details={"response": created},
            )

        for idx, offset in enumerate(range(0, len(data_bytes), part_size)):
            chunk = data_bytes[offset : offset + part_size]
            payload_b64 = base64.b64encode(chunk).decode("ascii")
            is_last = offset + part_size >= len(data_bytes)
            self.sandbox_append_ingest_part(
                receipt_id=receipt_id,
                job_id=job_id,
                file_id=file_id,
                part_no=idx,
                payload_b64=payload_b64,
                is_last=is_last,
            )

        terminal = self.sandbox_complete_ingest_job(
            receipt_id=receipt_id,
            job_id=job_id,
            file_ids=[file_id],
        )

        result = terminal.get("result") or {}
        completed = result.get("completed") or []
        errors = result.get("errors") or []
        if completed:
            return {
                "job_id": job_id,
                "status": terminal.get("status"),
                "file": completed[0],
                "result": result,
            }
        if errors:
            raise NukezError(
                "sandbox_upload_bytes completed with errors",
                details={"job_id": job_id, "errors": errors},
            )
        raise NukezError(
            "sandbox_upload_bytes did not return any completed files",
            details={"job_id": job_id, "terminal": terminal},
        )

    def sandbox_upload_base64(
        self,
        receipt_id: str,
        filename: str,
        payload_b64: str,
        *,
        content_type: Optional[str] = None,
        expected_sha256: Optional[str] = None,
        part_size_bytes: int = SANDBOX_INGEST_DEFAULT_PART_BYTES,
        execution_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Decode base64 and upload via sandbox-ingest chunk flow.
        """
        raw = (payload_b64 or "").strip()
        if not raw:
            raise NukezError("sandbox_upload_base64 requires non-empty payload_b64")
        if raw.lower() in {"none", "null", "undefined"}:
            raise NukezError("payload_b64 sentinel values are rejected")
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise NukezError("payload_b64 must be valid base64") from exc

        return self.sandbox_upload_bytes(
            receipt_id=receipt_id,
            filename=filename,
            data=data,
            content_type=content_type,
            expected_sha256=expected_sha256,
            part_size_bytes=part_size_bytes,
            execution_mode=execution_mode,
        )

    def sandbox_upload_file_path(
        self,
        receipt_id: str,
        filepath: str,
        *,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        expected_sha256: Optional[str] = None,
        part_size_bytes: int = SANDBOX_INGEST_DEFAULT_PART_BYTES,
        execution_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Read a local file and upload it through sandbox-ingest chunk flow.
        """
        p = Path(filepath).expanduser()
        if not p.exists():
            raise NukezError(f"File not found: {p}")
        if not p.is_file():
            raise NukezError(f"Expected file path, got non-file: {p}")
        remote_name = (filename or p.name).strip()
        if not remote_name:
            raise NukezError("sandbox_upload_file_path produced an empty filename")

        return self.sandbox_upload_bytes(
            receipt_id=receipt_id,
            filename=remote_name,
            data=p.read_bytes(),
            content_type=content_type or self._infer_content_type(remote_name),
            expected_sha256=expected_sha256,
            part_size_bytes=part_size_bytes,
            execution_mode=execution_mode,
        )
    
    def upload_bytes(self, upload_url: str, data: bytes, content_type: str = None) -> UploadResult:
        """
        Upload data to signed URL.
        
        Args:
            upload_url: URL from create_file() or get_file_urls()
            data: Bytes to upload
            content_type: Optional content type override
            
        Returns:
            UploadResult with upload confirmation
            
        Note:
            For agent/tool-calling use, prefer upload_string() which accepts
            a string and handles common formatting issues automatically.
        """
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        
        response = raw_requests.put(upload_url, data=data, headers=headers, timeout=60)
        response.raise_for_status()
        
        return UploadResult(
            upload_url=upload_url,
            size_bytes=len(data),
            content_type=content_type,
            uploaded_at=int(time.time())
        )
    
    def upload_string(
        self,
        upload_url: str,
        data: str,
        content_type: str = None
    ) -> UploadResult:
        """
        Upload string data to signed URL (agent-native interface).
        
        Accepts a string, sanitizes common agent formatting issues
        (JSON wrappers, markdown fencing), encodes to bytes, and uploads.
        
        This is the recommended method for LLM tool-calling integrations
        where agents pass data as strings.

        Guardrail: this method rejects oversized payloads with
        PAYLOAD_TOO_LARGE_FOR_CONTEXT and a recovery hint to use
        upload_file_path/bulk_upload_paths/upload_directory/start_bulk_upload_job.
        
        Args:
            upload_url: Signed upload URL from create_file() or get_file_urls()
            data: Content string to store. Sent as raw bytes in the HTTP PUT body.
                  Common agent malformations are auto-corrected:
                  - JSON wrappers like {"content": "..."} are unwrapped
                  - Markdown code fencing is stripped
            content_type: Optional content type override
            
        Returns:
            UploadResult with upload confirmation and size_bytes
            
        Raises:
            NukezError: If upload_url is malformed or upload fails.
                Error message includes recovery steps.
        """
        # Validate URL before making the request
        url_err = validate_signed_url(upload_url, "upload_url")
        if url_err:
            raise NukezError(url_err)
        
        # Guardrail: prevent huge data-in-context payloads from blowing up LLM/MCP flows
        raw_bytes = len(data.encode("utf-8"))
        if raw_bytes > UPLOAD_STRING_MAX_BYTES:
            raise NukezError(
                "PAYLOAD_TOO_LARGE_FOR_CONTEXT: upload_string payload exceeds context-safe limit. "
                "Use upload_file_path, bulk_upload_paths, upload_directory, or start_bulk_upload_job."
            )

        # Sanitize common agent formatting issues
        cleaned, fix_applied = sanitize_upload_data(data)

        cleaned_bytes = len(cleaned.encode("utf-8"))
        if cleaned_bytes > UPLOAD_STRING_MAX_BYTES:
            raise NukezError(
                "PAYLOAD_TOO_LARGE_FOR_CONTEXT: cleaned upload payload exceeds context-safe limit. "
                "Use upload_file_path, bulk_upload_paths, upload_directory, or start_bulk_upload_job."
            )

        return self.upload_bytes(upload_url, cleaned.encode("utf-8"), content_type)
    
    def download_bytes(self, download_url: str) -> bytes:
        """
        Download data from signed URL.
        
        Args:
            download_url: URL from create_file() or get_file_urls()
            
        Returns:
            Downloaded bytes
            
        Raises:
            NukezError: If URL is malformed or download fails.
                Error message includes recovery steps.
        """
        # Validate URL before making the request
        url_err = validate_signed_url(download_url, "download_url")
        if url_err:
            raise NukezError(url_err)
        
        try:
            response = raw_requests.get(download_url, timeout=60)
            response.raise_for_status()
        except raw_requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status in (400, 403):
                raise NukezError(
                    f"Download failed (HTTP {status}). The signed URL may be expired or malformed. "
                    f"Call get_file_urls(receipt_id=..., filename=...) or list_files(receipt_id=...) "
                    f"to get fresh download URLs."
                ) from e
            raise
        return response.content
    
    def list_files(self, receipt_id: str) -> List[FileInfo]:
        """
        List all files in locker.
        
        Args:
            receipt_id: Receipt ID from confirm_storage()
            
        Returns:
            List of FileInfo objects
        """
        keypair = self._require_keypair("list_files")
        locker_id = compute_locker_id(receipt_id)
        
        envelope = build_signed_envelope(
            keypair=keypair,
            receipt_id=receipt_id,
            method="GET",
            path=f"/v1/lockers/{locker_id}/files",
            ops=["locker:list"]
        )
        
        response = self.http.get(
            f"/v1/lockers/{locker_id}/files",
            headers=envelope.headers
        )
        
        files = response.get("files", [])
        return [
            FileInfo(
                filename=f["filename"],
                content_type=f.get("content_type", "application/octet-stream"),
                created_at=f.get("created_at"),
                updated_at=f.get("updated_at"),
                object_key=f.get("object_key")
            )
            for f in files
        ]
    
    def get_file_urls(
        self, 
        receipt_id: str, 
        filename: str, 
        ttl_min: int = 30
    ) -> FileUrls:
        """
        Get fresh upload/download URLs for existing file.
        
        Args:
            receipt_id: Receipt ID from confirm_storage()
            filename: File name from list_files()
            ttl_min: URL expiration time in minutes
            
        Returns:
            FileUrls with refreshed signed URLs
        """
        keypair = self._require_keypair("get_file_urls")
        locker_id = compute_locker_id(receipt_id)
        
        envelope = build_signed_envelope(
            keypair=keypair,
            receipt_id=receipt_id,
            method="GET",
            path=f"/v1/lockers/{locker_id}/files/{filename}",
            ops=["locker:read"]
        )
        
        response = self.http.get(
            f"/v1/lockers/{locker_id}/files/{filename}",
            headers=envelope.headers
        )
        
        return FileUrls(
            filename=response["filename"],
            upload_url=response["upload_url"],
            download_url=response["download_url"],
            content_type=response.get("content_type", "application/octet-stream"),
            expires_in_sec=response.get("expires_in_sec", ttl_min * 60)
        )

    # =========================================================================
    # VIEWER PORTAL HANDOFF (Agent -> Human)
    # =========================================================================

    @staticmethod
    def _normalize_viewer_base_url(viewer_base_url: str) -> str:
        """Normalize viewer host for URL construction."""
        base = (viewer_base_url or "https://nukez.xyz").strip()
        if not base:
            base = "https://nukez.xyz"
        return base.rstrip("/")

    @staticmethod
    def _viewer_button_ui(
        label: str,
        url: str,
        variant: str = VIEWER_RENDERER_VARIANT,
    ) -> Dict[str, str]:
        """UI metadata for MCP/tool renderers."""
        return {
            "kind": "button",
            "label": label,
            "href": url,
            "variant": variant,
            "target": "_blank",
        }

    @staticmethod
    def _viewer_renderer_contract() -> Dict[str, str]:
        """Stable renderer contract descriptor for MCP/frontends."""
        return {
            "name": VIEWER_RENDERER_CONTRACT_NAME,
            "version": VIEWER_RENDERER_CONTRACT_VERSION,
        }

    @staticmethod
    def _viewer_container_contract() -> Dict[str, str]:
        """Stable container contract descriptor for generic viewer payloads."""
        return {
            "name": VIEWER_CONTAINER_CONTRACT_NAME,
            "version": VIEWER_CONTAINER_CONTRACT_VERSION,
        }

    def get_viewer_container_url(
        self,
        viewer_base_url: str = "https://nukez.xyz",
        request_type: str = "container",
        receipt_id: Optional[str] = None,
        locker_id: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> str:
        """
        Build a generic viewer container URL.

        This URL is intentionally render-agnostic. The agent can hand it to a
        human as an empty container first, then later provide richer payloads.
        """
        base = self._normalize_viewer_base_url(viewer_base_url)
        resolved_locker_id = locker_id or (compute_locker_id(receipt_id) if receipt_id else None)

        params: Dict[str, str] = {}
        if request_type:
            params["request_type"] = request_type
        if receipt_id:
            params["receipt_id"] = receipt_id
        if resolved_locker_id:
            params["locker_id"] = resolved_locker_id
        if filename:
            params["filename"] = filename

        if not params:
            return f"{base}/viewer"
        return f"{base}/viewer?{urlencode(params)}"

    def get_viewer_container_handoff(
        self,
        viewer_base_url: str = "https://nukez.xyz",
        request_type: str = "container",
        view_kind: str = "custom",
        receipt_id: Optional[str] = None,
        locker_id: Optional[str] = None,
        filename: Optional[str] = None,
        blocks: Optional[List[Dict[str, Any]]] = None,
        renderables: Optional[List[Dict[str, Any]]] = None,
        embed_payload_in_url: bool = True,
        button_label: str = "Open Nukez Viewer",
    ) -> ViewerContainer:
        """
        Build a generic viewer-container handoff payload for MCP renderers.
        """
        resolved_locker_id = locker_id or (compute_locker_id(receipt_id) if receipt_id else None)
        base_viewer_url = self.get_viewer_container_url(
            viewer_base_url=viewer_base_url,
            request_type=request_type,
            receipt_id=receipt_id,
            locker_id=resolved_locker_id,
            filename=filename,
        )

        normalized_blocks = blocks or []
        normalized_renderables = renderables or []
        state = "ready" if (normalized_blocks or normalized_renderables) else "empty"
        errors: List[Dict[str, Any]] = []
        viewer_url = base_viewer_url

        if (normalized_blocks or normalized_renderables) and embed_payload_in_url:
            payload_obj = {
                "contract": VIEWER_CONTAINER_CONTRACT_NAME,
                "version": VIEWER_CONTAINER_CONTRACT_VERSION,
                "request_type": request_type,
                "view_kind": view_kind,
                "input": {
                    "receipt_id": receipt_id,
                    "locker_id": resolved_locker_id,
                    "filename": filename,
                },
                "result": {
                    "kind": "container",
                    "state": state,
                    "view_kind": view_kind,
                    "blocks": normalized_blocks,
                    "renderables": normalized_renderables,
                },
            }
            payload_param = urlencode(
                {"payload": json.dumps(payload_obj, separators=(",", ":"))}
            )
            candidate = f"{base_viewer_url}{'&' if '?' in base_viewer_url else '?'}{payload_param}"
            if len(candidate) <= 7800:
                viewer_url = candidate
            else:
                errors.append(
                    {
                        "code": "PAYLOAD_TOO_LARGE",
                        "message": "Renderable payload exceeded safe URL length; returning unembedded viewer URL.",
                    }
                )

        return ViewerContainer(
            contract=VIEWER_CONTAINER_CONTRACT_NAME,
            version=VIEWER_CONTAINER_CONTRACT_VERSION,
            request_type=request_type,
            viewer_url=viewer_url,
            input={
                "receipt_id": receipt_id,
                "locker_id": resolved_locker_id,
                "filename": filename,
            },
            result={
                "kind": "container",
                "state": state,
                "view_kind": view_kind,
                "viewer_url": base_viewer_url,
                "blocks": normalized_blocks,
                "renderables": normalized_renderables,
            },
            render_hints={
                "variant": VIEWER_RENDERER_VARIANT,
                "layout": "container",
                "primary_action_label": button_label,
                "target": "_blank",
            },
            auth_state={
                "mode": "keypair_signature",
            },
            errors=errors,
            meta={
                "generated_at": int(time.time()),
                "sdk_contract": self._viewer_container_contract(),
                "payload_embedded_in_url": viewer_url != base_viewer_url,
            },
            ui=self._viewer_button_ui(button_label, viewer_url),
        )

    @staticmethod
    def make_text_renderable(
        content: str,
        title: str = "Text",
        description: str = "",
        content_type: str = "text/plain",
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build a text renderable object for viewer_container payloads."""
        payload: Dict[str, Any] = {
            "type": "text",
            "title": title,
            "content": content,
            "content_type": content_type,
        }
        if description:
            payload["description"] = description
        if meta:
            payload["meta"] = meta
        return payload

    @staticmethod
    def make_json_renderable(
        data: Any,
        title: str = "JSON",
        description: str = "",
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build a JSON renderable object for viewer_container payloads."""
        payload: Dict[str, Any] = {
            "type": "json",
            "title": title,
            "data": data,
            "content_type": "application/json",
        }
        if description:
            payload["description"] = description
        if meta:
            payload["meta"] = meta
        return payload

    @staticmethod
    def make_pdf_renderable(
        url: str,
        title: str = "PDF",
        description: str = "",
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build a PDF renderable object for viewer_container payloads."""
        payload: Dict[str, Any] = {
            "type": "pdf",
            "title": title,
            "url": url,
            "content_type": "application/pdf",
        }
        if description:
            payload["description"] = description
        if meta:
            payload["meta"] = meta
        return payload

    @staticmethod
    def make_image_renderable(
        url: str,
        title: str = "Image",
        description: str = "",
        alt: str = "",
        content_type: str = "image/*",
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build an image renderable object for viewer_container payloads."""
        payload: Dict[str, Any] = {
            "type": "image",
            "title": title,
            "url": url,
            "content_type": content_type,
        }
        if description:
            payload["description"] = description
        if alt:
            payload["alt"] = alt
        if meta:
            payload["meta"] = meta
        return payload

    @staticmethod
    def make_binary_renderable(
        hex_preview: str = "",
        title: str = "Binary",
        description: str = "",
        size_bytes: Optional[int] = None,
        content_type: str = "application/octet-stream",
        base64_data: str = "",
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build a binary renderable object for viewer_container payloads."""
        payload: Dict[str, Any] = {
            "type": "binary",
            "title": title,
            "content_type": content_type,
        }
        if description:
            payload["description"] = description
        if hex_preview:
            payload["hex_preview"] = hex_preview
        if base64_data:
            payload["base64"] = base64_data
        if size_bytes is not None:
            payload["size_bytes"] = size_bytes
        if meta:
            payload["meta"] = meta
        return payload

    @staticmethod
    def make_header_block(
        title: str,
        subtitle: str = "",
        description: str = "",
        badge: str = "",
    ) -> Dict[str, Any]:
        """Build a generic header block for viewer_container blocks."""
        block: Dict[str, Any] = {"type": "header", "title": title}
        if subtitle:
            block["subtitle"] = subtitle
        if description:
            block["description"] = description
        if badge:
            block["badge"] = badge
        return block

    @staticmethod
    def make_stats_block(items: List[Dict[str, Any]], title: str = "Stats") -> Dict[str, Any]:
        """Build a stats block."""
        return {"type": "stats", "title": title, "items": items}

    @staticmethod
    def make_links_block(items: List[Dict[str, Any]], title: str = "Links") -> Dict[str, Any]:
        """Build a links block."""
        return {"type": "links", "title": title, "items": items}

    @staticmethod
    def make_table_block(
        columns: List[Dict[str, Any]],
        rows: List[Dict[str, Any]],
        title: str = "Table",
    ) -> Dict[str, Any]:
        """Build a table block."""
        return {
            "type": "table",
            "title": title,
            "columns": columns,
            "rows": rows,
        }

    @staticmethod
    def make_kv_block(items: List[Dict[str, Any]], title: str = "Details") -> Dict[str, Any]:
        """Build a key-value block."""
        return {"type": "kv", "title": title, "items": items}

    @staticmethod
    def make_status_block(status: str, label: str = "Status", detail: str = "") -> Dict[str, Any]:
        """Build a status block."""
        block: Dict[str, Any] = {"type": "status", "status": status, "label": label}
        if detail:
            block["detail"] = detail
        return block

    @staticmethod
    def make_proofs_block(items: List[Dict[str, Any]], title: str = "Proofs") -> Dict[str, Any]:
        """Build a proofs block."""
        return {"type": "proofs", "title": title, "items": items}

    @staticmethod
    def make_json_block(data: Any, title: str = "Raw JSON") -> Dict[str, Any]:
        """Build a JSON block."""
        return {"type": "json", "title": title, "data": data}

    @staticmethod
    def make_file_meta_block(
        filename: str,
        content_type: str = "",
        size_bytes: Optional[int] = None,
        updated_at: Optional[str] = None,
        sha256: str = "",
        extra: Optional[Dict[str, Any]] = None,
        title: str = "File Metadata",
    ) -> Dict[str, Any]:
        """Build a file metadata block."""
        items: List[Dict[str, Any]] = [{"key": "Filename", "value": filename}]
        if content_type:
            items.append({"key": "Content-Type", "value": content_type})
        if size_bytes is not None:
            items.append({"key": "Size Bytes", "value": size_bytes})
        if updated_at:
            items.append({"key": "Updated At", "value": updated_at})
        if sha256:
            items.append({"key": "SHA-256", "value": sha256})
        if extra:
            for key, value in extra.items():
                items.append({"key": str(key), "value": value})
        return {"type": "file_meta", "title": title, "items": items}

    @staticmethod
    def make_file_preview_block(
        filename: str,
        content_type: str = "",
        url: str = "",
        text_content: str = "",
        json_data: Any = None,
        hex_preview: str = "",
        base64_data: str = "",
        size_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Build a file preview block.

        The frontend resolves rendering mode from filename/content_type and
        available fields so callers do not manage mime-specific rendering logic.
        """
        block: Dict[str, Any] = {
            "type": "file_preview",
            "filename": filename,
        }
        if content_type:
            block["content_type"] = content_type
        if url:
            block["url"] = url
        if text_content:
            block["text_content"] = text_content
        if json_data is not None:
            block["data"] = json_data
        if hex_preview:
            block["hex_preview"] = hex_preview
        if base64_data:
            block["base64"] = base64_data
        if size_bytes is not None:
            block["size_bytes"] = size_bytes
        return block

    def get_locker_view_container(
        self,
        receipt_id: str,
        viewer_base_url: str = "https://nukez.xyz",
        include_download_urls: bool = False,
        ttl_min: int = 30,
        embed_payload_in_url: bool = True,
        button_label: str = "Open Locker Viewer",
    ) -> ViewerContainer:
        """
        Build a locker view payload:
        table + stats + links.
        """
        bundle = self.list_files_with_viewer_urls(
            receipt_id=receipt_id,
            viewer_base_url=viewer_base_url,
            include_download_urls=include_download_urls,
            ttl_min=ttl_min,
        )
        rows: List[Dict[str, Any]] = []
        for f in bundle.files:
            row: Dict[str, Any] = {
                "filename": f.filename,
                "content_type": f.content_type,
                "updated_at": f.updated_at or f.created_at or "",
                "viewer_url": f.viewer_url,
            }
            if f.download_url:
                row["download_url"] = f.download_url
            rows.append(row)

        blocks: List[Dict[str, Any]] = [
            self.make_header_block(
                title="Locker Contents",
                subtitle=bundle.locker_id,
                description="Canonical manifest view of files stored through Nukez protocol flows.",
            ),
            self.make_stats_block(
                [
                    {"label": "Locker ID", "value": bundle.locker_id},
                    {"label": "Receipt ID", "value": bundle.receipt_id},
                    {"label": "File Count", "value": len(bundle.files)},
                ],
                title="Locker Stats",
            ),
            self.make_links_block(
                [{"label": "Open Owner Portal", "href": bundle.owner_viewer_url}],
                title="Locker Links",
            ),
            self.make_table_block(
                columns=[
                    {"key": "filename", "label": "Filename"},
                    {"key": "content_type", "label": "Type"},
                    {"key": "updated_at", "label": "Updated"},
                    {"key": "viewer_url", "label": "Viewer"},
                    {"key": "download_url", "label": "Download"},
                ],
                rows=rows,
                title="Files",
            ),
        ]

        return self.get_viewer_container_handoff(
            viewer_base_url=viewer_base_url,
            request_type="locker_view",
            view_kind="locker",
            receipt_id=receipt_id,
            locker_id=bundle.locker_id,
            blocks=blocks,
            embed_payload_in_url=embed_payload_in_url,
            button_label=button_label,
        )

    def get_attestation_view_container(
        self,
        receipt_id: str,
        viewer_base_url: str = "https://nukez.xyz",
        embed_payload_in_url: bool = True,
        button_label: str = "Open Attestation Viewer",
    ) -> ViewerContainer:
        """
        Build an attestation view payload:
        kv + status + proofs + json.
        """
        verification = self.verify_storage(receipt_id)
        locker_id = verification.locker_id or compute_locker_id(receipt_id)
        verified_status = "verified" if verification.verified else "unverified"
        status_detail = (
            "Attestation root is present."
            if verification.attested
            else "No attestation root found in verification response."
        )

        kv_items = [
            {"key": "Receipt ID", "value": verification.receipt_id},
            {"key": "Locker ID", "value": locker_id},
            {"key": "Verified", "value": verification.verified},
            {"key": "Attested", "value": verification.attested},
            {"key": "File Count", "value": verification.file_count},
            {"key": "Result Hash", "value": verification.result_hash},
            {"key": "Verified At", "value": verification.verified_at or "-"},
        ]

        proofs: List[Dict[str, Any]] = []
        if verification.att_code:
            proofs.append({"label": "Attestation Code", "value": verification.att_code})
        if verification.merkle_root:
            proofs.append({"label": "Merkle Root", "value": verification.merkle_root})
        if verification.manifest_signature:
            proofs.append({"label": "Manifest Signature", "value": verification.manifest_signature})
        if verification.verify_url:
            proofs.append({"label": "Verify URL", "href": verification.verify_url})

        raw_json = {
            "receipt_id": verification.receipt_id,
            "locker_id": locker_id,
            "verified": verification.verified,
            "attested": verification.attested,
            "result_hash": verification.result_hash,
            "att_code": verification.att_code,
            "verified_at": verification.verified_at,
            "merkle_root": verification.merkle_root,
            "manifest_signature": verification.manifest_signature,
            "file_count": verification.file_count,
            "files": verification.files or [],
            "verify_url": verification.verify_url,
        }

        blocks: List[Dict[str, Any]] = [
            self.make_header_block(
                title="Attestation View",
                subtitle=locker_id,
                description="Verification state and cryptographic proof data.",
            ),
            self.make_status_block(
                status=verified_status,
                label="Verification Status",
                detail=status_detail,
            ),
            self.make_kv_block(kv_items, title="Attestation Summary"),
        ]
        if proofs:
            blocks.append(self.make_proofs_block(proofs, title="Proof Material"))
        blocks.append(self.make_json_block(raw_json, title="Raw Verification JSON"))

        return self.get_viewer_container_handoff(
            viewer_base_url=viewer_base_url,
            request_type="attestation_view",
            view_kind="attestation",
            receipt_id=receipt_id,
            locker_id=locker_id,
            blocks=blocks,
            embed_payload_in_url=embed_payload_in_url,
            button_label=button_label,
        )

    def get_file_view_container(
        self,
        receipt_id: str,
        filename: str,
        viewer_base_url: str = "https://nukez.xyz",
        ttl_min: int = 30,
        include_download_url: bool = True,
        embed_payload_in_url: bool = True,
        button_label: str = "Open File Viewer",
    ) -> ViewerContainer:
        """
        Build a file view payload:
        file_meta + file_preview.
        """
        locker_id = compute_locker_id(receipt_id)
        owner_link = self.get_owner_viewer_url(receipt_id=receipt_id, viewer_base_url=viewer_base_url)
        file_link = self.get_file_viewer_url(
            receipt_id=receipt_id,
            filename=filename,
            viewer_base_url=viewer_base_url,
            ttl_min=ttl_min,
            include_download_url=include_download_url,
        )

        content_type = "application/octet-stream"
        updated_at: Optional[str] = None
        for f in self.list_files(receipt_id=receipt_id):
            if f.filename == filename:
                content_type = f.content_type or content_type
                updated_at = f.updated_at or f.created_at
                break

        links = [
            {"label": "Back to Locker", "href": owner_link.url},
            {"label": "File Route", "href": file_link.url},
        ]
        if file_link.download_url:
            links.append({"label": "Raw Download", "href": file_link.download_url})

        blocks: List[Dict[str, Any]] = [
            self.make_header_block(
                title="File View",
                subtitle=filename,
                description="Single-file viewer with automatic preview mode selection.",
            ),
            self.make_file_meta_block(
                filename=filename,
                content_type=content_type,
                updated_at=updated_at,
                extra={
                    "Locker ID": locker_id,
                    "Receipt ID": receipt_id,
                },
            ),
            self.make_links_block(links, title="File Links"),
            self.make_file_preview_block(
                filename=filename,
                content_type=content_type,
                url=file_link.download_url or "",
            ),
        ]

        return self.get_viewer_container_handoff(
            viewer_base_url=viewer_base_url,
            request_type="file_view",
            view_kind="file",
            receipt_id=receipt_id,
            locker_id=locker_id,
            filename=filename,
            blocks=blocks,
            embed_payload_in_url=embed_payload_in_url,
            button_label=button_label,
        )

    def get_owner_viewer_url(
        self,
        receipt_id: str,
        viewer_base_url: str = "https://nukez.xyz"
    ) -> ViewerLink:
        """
        Build a stable owner portal URL for a locker.

        This is the default agent handoff link for human-in-the-loop review.
        No network call is required.
        """
        locker_id = compute_locker_id(receipt_id)
        base = self._normalize_viewer_base_url(viewer_base_url)
        query = urlencode({
            "locker_id": locker_id,
            "receipt_id": receipt_id,
        })
        return ViewerLink(
            url=f"{base}/owner?{query}",
            kind="owner",
            locker_id=locker_id,
            receipt_id=receipt_id,
            includes_download_url=False,
        )

    def get_file_viewer_url(
        self,
        receipt_id: str,
        filename: str,
        viewer_base_url: str = "https://nukez.xyz",
        ttl_min: int = 30,
        include_download_url: bool = True,
    ) -> ViewerLink:
        """
        Build a file-scoped viewer URL with a stable download URL.

        Uses the receipt-based file proxy endpoint (/v1/r/{receipt_id}/f/{filename})
        which never expires, instead of time-limited short URL tokens.
        No network call is needed — the URL is computed locally.
        """
        locker_id = compute_locker_id(receipt_id)
        base = self._normalize_viewer_base_url(viewer_base_url)

        # Stable download URL: receipt-based proxy (never expires)
        api_base = self.base_url.rstrip("/")
        download_url = f"{api_base}/v1/r/{receipt_id}/f/{filename}"

        params: Dict[str, str] = {
            "locker_id": locker_id,
            "receipt_id": receipt_id,
            "filename": filename,
            "download_url": download_url,
        }

        return ViewerLink(
            url=f"{base}/view?{urlencode(params)}",
            kind="file",
            locker_id=locker_id,
            receipt_id=receipt_id,
            filename=filename,
            download_url=download_url,
            expires_in_sec=None,  # Never expires
            includes_download_url=True,
        )

    def list_files_with_viewer_urls(
        self,
        receipt_id: str,
        viewer_base_url: str = "https://nukez.xyz",
        include_download_urls: bool = False,
        ttl_min: int = 30,
    ) -> ViewerFileList:
        """
        List locker files and enrich each with a human viewer URL.

        Default behavior avoids minting per-file signed URLs
        (include_download_urls=False) so links are stable and cheap.
        Set include_download_urls=True for immediate one-click file previews.
        """
        owner_link = self.get_owner_viewer_url(
            receipt_id=receipt_id,
            viewer_base_url=viewer_base_url,
        )
        files = self.list_files(receipt_id=receipt_id)

        file_rows: List[FileViewerInfo] = []
        for f in files:
            link = self.get_file_viewer_url(
                receipt_id=receipt_id,
                filename=f.filename,
                viewer_base_url=viewer_base_url,
                ttl_min=ttl_min,
                include_download_url=include_download_urls,
            )
            file_rows.append(
                FileViewerInfo(
                    filename=f.filename,
                    content_type=f.content_type,
                    created_at=f.created_at,
                    updated_at=f.updated_at,
                    object_key=f.object_key,
                    viewer_url=link.url,
                    download_url=link.download_url,
                    expires_in_sec=link.expires_in_sec,
                )
            )

        return ViewerFileList(
            receipt_id=receipt_id,
            locker_id=owner_link.locker_id,
            owner_viewer_url=owner_link.url,
            files=file_rows,
        )

    def get_owner_viewer_handoff(
        self,
        receipt_id: str,
        viewer_base_url: str = "https://nukez.xyz",
        button_label: str = "Open Nukez Viewer",
    ) -> Dict[str, Any]:
        """
        Build MCP-friendly owner viewer payload with UI button metadata.
        """
        link = self.get_owner_viewer_url(
            receipt_id=receipt_id,
            viewer_base_url=viewer_base_url,
        )
        return {
            "renderer_contract": self._viewer_renderer_contract(),
            "kind": link.kind,
            "viewer_url": link.url,
            "locker_id": link.locker_id,
            "receipt_id": link.receipt_id,
            "ui": self._viewer_button_ui(button_label, link.url),
        }

    def get_viewer_renderer_contract(self) -> Dict[str, str]:
        """
        Return the stable MCP renderer contract descriptor used by viewer handoff payloads.
        """
        return self._viewer_renderer_contract()

    def get_viewer_container_contract(self) -> Dict[str, str]:
        """
        Return the stable viewer-container contract descriptor.
        """
        return self._viewer_container_contract()

    def get_file_viewer_handoff(
        self,
        receipt_id: str,
        filename: str,
        viewer_base_url: str = "https://nukez.xyz",
        ttl_min: int = 30,
        include_download_url: bool = True,
        button_label: str = "Open File Viewer",
    ) -> Dict[str, Any]:
        """
        Build MCP-friendly file viewer payload with UI button metadata.
        """
        link = self.get_file_viewer_url(
            receipt_id=receipt_id,
            filename=filename,
            viewer_base_url=viewer_base_url,
            ttl_min=ttl_min,
            include_download_url=include_download_url,
        )
        return {
            "renderer_contract": self._viewer_renderer_contract(),
            "kind": link.kind,
            "viewer_url": link.url,
            "locker_id": link.locker_id,
            "receipt_id": link.receipt_id,
            "filename": link.filename,
            "download_url": link.download_url,
            "expires_in_sec": link.expires_in_sec,
            "includes_download_url": link.includes_download_url,
            "ui": self._viewer_button_ui(button_label, link.url),
        }

    def list_files_with_viewer_handoffs(
        self,
        receipt_id: str,
        viewer_base_url: str = "https://nukez.xyz",
        include_download_urls: bool = False,
        ttl_min: int = 30,
    ) -> Dict[str, Any]:
        """
        Build MCP-friendly owner + file viewer payloads with UI metadata.
        """
        bundle = self.list_files_with_viewer_urls(
            receipt_id=receipt_id,
            viewer_base_url=viewer_base_url,
            include_download_urls=include_download_urls,
            ttl_min=ttl_min,
        )
        owner_ui = self._viewer_button_ui("Open Nukez Viewer", bundle.owner_viewer_url)
        file_rows = []
        for f in bundle.files:
            file_rows.append(
                {
                    "filename": f.filename,
                    "content_type": f.content_type,
                    "viewer_url": f.viewer_url,
                    "download_url": f.download_url,
                    "expires_in_sec": f.expires_in_sec,
                    "ui": self._viewer_button_ui(f"View {f.filename}", f.viewer_url),
                }
            )

        return {
            "renderer_contract": self._viewer_renderer_contract(),
            "kind": "owner_and_files",
            "receipt_id": bundle.receipt_id,
            "locker_id": bundle.locker_id,
            "owner_viewer_url": bundle.owner_viewer_url,
            "ui": owner_ui,
            "files": file_rows,
        }
    
    def delete_file(self, receipt_id: str, filename: str) -> DeleteResult:
        """
        Delete file from locker.
        
        Args:
            receipt_id: Receipt ID from confirm_storage()
            filename: File name to delete
            
        Returns:
            DeleteResult with deletion confirmation
        """
        keypair = self._require_keypair("delete_file")
        locker_id = compute_locker_id(receipt_id)
        
        envelope = build_signed_envelope(
            keypair=keypair,
            receipt_id=receipt_id,
            method="DELETE",
            path=f"/v1/lockers/{locker_id}/files/{filename}",
            ops=["locker:write"]
        )
        
        response = self.http.delete(
            f"/v1/lockers/{locker_id}/files/{filename}",
            headers=envelope.headers
        )
        
        return DeleteResult(
            filename=filename,
            deleted=response.get("deleted", True),
            deleted_at=response.get("deleted_at")
        )
    
    def get_manifest(self, receipt_id: str) -> dict:
        """Full locker state in one call — all files, hashes, metadata."""
        keypair = self._require_keypair("get_manifest")
        locker_id = compute_locker_id(receipt_id)

        envelope = build_signed_envelope(
            keypair=keypair,
            receipt_id=receipt_id,
            method="GET",
            path=f"/v1/lockers/{locker_id}/manifest",
            ops=["locker:read"],
        )

        return self.http.get(
            f"/v1/lockers/{locker_id}/manifest",
            headers=envelope.headers,
        )
    
    # =========================================================================
    # VERIFICATION
    # =========================================================================
    
    def verify_storage(self, receipt_id: str) -> VerificationResult:
        """
        Verify storage integrity and get cryptographic attestation.
        
        Returns the unified verification response including:
        - Receipt verification (payment confirmed on-chain)
        - Attestation data (merkle root, file hashes, gateway signature)
        - On-chain attestation code (if Switchboard push has occurred)
        
        Args:
            receipt_id: Receipt ID from confirm_storage()
            
        Returns:
            VerificationResult with:
            - verified: True if receipt + attestation are valid
            - merkle_root: SHA256 merkle root of all files in locker
            - manifest_signature: Gateway's Ed25519 signature over the merkle root
            - att_code: Numeric attestation code (from Switchboard on-chain push)
            - file_count: Number of files covered by attestation
            - files: List of {filename, content_hash, size_bytes} for each file
            - verify_url: Public verification page URL
        """
        response = self.http.get(
            "/v1/storage/verify", 
            params={"receipt_id": receipt_id}
        )
        
        # Extract attestation from nested object (verify endpoint returns unified response)
        attestation = response.get("attestation") or {}
        
        return VerificationResult(
            receipt_id=receipt_id,
            verified=response.get("verified", False),
            result_hash=attestation.get("result_hash", response.get("result_hash", "")),
            att_code=str(attestation.get("att_code", response.get("att_code", ""))),
            verified_at=attestation.get("attested_at", response.get("verified_at", "")),
            # Phase 5 attestation fields
            merkle_root=attestation.get("merkle_root", ""),
            manifest_signature=attestation.get("manifest_signature", ""),
            file_count=attestation.get("file_count", 0),
            files=attestation.get("files"),
            locker_id=response.get("locker_id", attestation.get("locker_id", "")),
            verify_url=response.get("verify_url", ""),
        )
    
    def get_merkle_proof(self, receipt_id: str, filename: str) -> dict:
        """
        Get a merkle inclusion proof for a specific file in an attested locker.

        The proof allows a verifier to confirm that a single file is included
        in the attested merkle tree without needing all other files.

        Verification algorithm:
          1. Compute leaf = SHA256("{filename}:{size_bytes}:{content_hash}")
          2. Walk proof: for each step, if position=='right' then
             current=SHA256(current+step.hash), else current=SHA256(step.hash+current)
          3. Assert final current == merkle_root

        Args:
            receipt_id: Receipt ID from confirm_storage()
            filename: Name of the file to prove inclusion for

        Returns:
            dict with:
            - receipt_id: The receipt ID
            - filename: The target filename
            - leaf_hash: SHA256 hash of the leaf node
            - leaf_index: Position in the sorted file list
            - merkle_root: The attested merkle root (sha256:...)
            - proof: List of {hash, position} steps from leaf to root
            - tree_depth: Number of levels in the merkle tree
            - file_count: Total files in the attestation
            - file_entry: {filename, size_bytes, content_hash} of the target file
            - schema_version: Attestation schema version
            - switchboard: {slot, tx} on-chain attestation data
            - verification_algorithm: Human-readable verification instructions
        """
        return self.http.get(
            "/v1/storage/merkle-proof",
            params={"receipt_id": receipt_id, "filename": filename},
        )

    def compute_hash(self, data: Union[str, bytes]) -> str:
        """
        Compute SHA256 hash of data for verification.

        Args:
            data: Data to hash (string or bytes)

        Returns:
            Hex-encoded SHA256 hash
        """
        if isinstance(data, str):
            data = data.encode('utf-8')
        return hashlib.sha256(data).hexdigest()
    
    # =========================================================================
    # UTILITIES
    # =========================================================================
    
    def get_wallet_info(self) -> WalletInfo:
        """
        Get wallet information for current keypair.
        
        Returns:
            WalletInfo with pubkey, balance, network
        """
        self._require_keypair("get_wallet_info")
        
        if self._payment is None:
            PaymentClass = _get_solana_payment()
            self._payment = PaymentClass(
                keypair_path=str(self.keypair.keypair_path),
                network=self.network,
                rpc_url=self.rpc_url,
            )
        
        return WalletInfo(
            pubkey=self._payment.pubkey,
            balance_sol=self._payment.get_balance(),
            network=self.network
        )
    
    def sign_message(self, message: str) -> str:
        """
        Sign message with current keypair.
        
        Args:
            message: Message to sign
            
        Returns:
            Base58-encoded signature
        """
        keypair = self._require_keypair("sign_message")
        return keypair.sign_message(message.encode('utf-8'))

    # =========================================================================
    # CONFIRMATION & ATTESTATION (Trust boundary closure)
    # =========================================================================

    def confirm_file(self, receipt_id: str, filename: str) -> ConfirmResult:
        """
        Confirm a file upload by computing its content hash server-side.

        Call this AFTER uploading content to the signed URL returned by
        create_file(). The server downloads the content from storage,
        computes SHA256, and records it in the manifest.

        This closes the trust boundary at upload time — the server now
        has a verified hash of what was actually stored, not just what
        the client claimed to send.

        Args:
            receipt_id: Receipt ID from confirm_storage()
            filename: Name of the file to confirm

        Returns:
            ConfirmResult with:
            - filename: Confirmed filename
            - content_hash: Server-computed SHA256 hash (sha256:... prefixed)
            - size_bytes: Actual size of stored content
            - confirmed: True if hash was recorded successfully

        Note:
            If AUTO_REATTEST is enabled server-side, this also triggers
            re-attestation so the merkle root stays current.
        """
        response = self.http.post(
            "/v1/files/confirm",
            params={"receipt_id": receipt_id, "filename": filename},
        )

        return ConfirmResult(
            filename=response.get("filename", filename),
            content_hash=response.get("content_hash", ""),
            size_bytes=response.get("size_bytes", 0),
            confirmed=True,
        )

    def confirm_files(self, receipt_id: str, filenames: List[str]) -> BatchConfirmResult:
        """
        Confirm multiple file uploads in a single operation.

        One manifest read-modify-write, one re-attestation (if AUTO_REATTEST
        is enabled). More efficient than calling confirm_file() in a loop.

        Args:
            receipt_id: Receipt ID from confirm_storage()
            filenames: List of filenames to confirm

        Returns:
            BatchConfirmResult with:
            - results: Per-file confirmation results
            - confirmed_count: Number successfully confirmed
            - failed_count: Number that failed
        """
        response = self.http.post(
            "/v1/files/confirm-batch",
            params={
                "receipt_id": receipt_id,
                "filenames": filenames,
            },
        )

        results = []
        for r in response.get("results", []):
            results.append(ConfirmResult(
                filename=r.get("filename", ""),
                content_hash=r.get("content_hash", ""),
                size_bytes=r.get("size_bytes", 0),
                confirmed=r.get("status") != "error",
            ))

        return BatchConfirmResult(
            results=results,
            confirmed_count=response.get("confirmed", len([r for r in results if r.confirmed])),
            failed_count=response.get("failed", len([r for r in results if not r.confirmed])),
        )

    def attest(self, receipt_id: str, sync: bool = True) -> AttestResult:
        """
        Trigger attestation — compute merkle root and optionally push on-chain.

        This is the core protocol primitive: given all confirmed files in a
        locker, compute the merkle tree, sign the root, and (if SB_AUTO_PUSH
        is enabled server-side) push the attestation code to Switchboard.

        Args:
            receipt_id: Receipt ID from confirm_storage()
            sync: If True (default), wait for attestation to complete and
                  return the full result. If False, return immediately with
                  status="accepted" — poll verify_storage() for completion.

        Returns:
            AttestResult with:
            - merkle_root: SHA256 merkle root of all files
            - file_count: Number of files in the attestation
            - att_code: Numeric attestation code (if push occurred)
            - status: "complete" or "accepted" (async)
            - push_result: On-chain push details (if SB_AUTO_PUSH enabled)

        Example:
            # Upload files, confirm them, then attest
            client.upload_bytes(urls.upload_url, data)
            client.confirm_file(receipt_id, "data.txt")
            proof = client.attest(receipt_id)
            print(proof.merkle_root)  # sha256:abc123...
        """
        params = {"receipt_id": receipt_id}
        if sync:
            params["sync"] = "true"

        response = self.http.post(
            "/v1/storage/attest",
            params=params,
        )

        push_result = response.get("push_result") or {}

        return AttestResult(
            receipt_id=receipt_id,
            merkle_root=response.get("merkle_root", ""),
            file_count=response.get("file_count", 0),
            att_code=response.get("att_code"),
            status="complete" if response.get("merkle_root") else response.get("status", "accepted"),
            push_ok=push_result.get("ok", False),
            tx_signature=push_result.get("tx_signature"),
            switchboard_slot=push_result.get("slot"),
        )

    def upload_files(
        self,
        receipt_id: str,
        files: List[Dict[str, Any]],
        workers: int = 10,
        confirm: bool = True,
        on_progress: Optional[Any] = None,
    ) -> BatchUploadResult:
        """
        Upload multiple files concurrently with optional confirmation.

        Handles create_file → upload_bytes → confirm_file for each file
        using a thread pool. This is the production method for bulk uploads.

        Args:
            receipt_id: Receipt ID from confirm_storage()
            files: List of file dicts, each with:
                - filename (str): Name for the file
                - content (bytes): File content
                - content_type (str, optional): MIME type (default: application/octet-stream)
            workers: Number of concurrent upload threads (default: 10)
            confirm: If True (default), call confirm_file() after each upload
                     to close the trust boundary. If False, skip confirmation
                     (caller is responsible for confirming later).
            on_progress: Optional callback(filename, success, index, total)
                         called after each file completes.

        Returns:
            BatchUploadResult with:
            - uploaded: Number of files successfully uploaded
            - failed: Number of files that failed
            - total: Total files attempted
            - elapsed_sec: Wall-clock time for all uploads
            - errors: List of (filename, error_message) for failures
            - results: List of per-file UploadResult objects

        Example:
            files = [
                {"filename": "a.txt", "content": b"hello"},
                {"filename": "b.txt", "content": b"world"},
            ]
            result = client.upload_files(receipt_id, files, workers=5)
            print(f"Uploaded {result.uploaded}/{result.total} in {result.elapsed_sec:.1f}s")

            # Then attest once for the whole batch
            proof = client.attest(receipt_id)
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        t0 = time.time()
        uploaded_count = 0
        errors = []
        results = []

        def _do_one(sf: Dict[str, Any]) -> tuple:
            """Upload a single file. Returns (filename, success, error, upload_result)."""
            fname = sf["filename"]
            content = sf["content"]
            ctype = sf.get("content_type", "application/octet-stream")
            try:
                urls = self.create_file(receipt_id, fname, content_type=ctype)
                result = self.upload_bytes(urls.upload_url, content, content_type=ctype)

                if confirm:
                    try:
                        self.confirm_file(receipt_id, fname)
                    except Exception:
                        pass  # Non-fatal — attestation will still compute hashes

                return (fname, True, None, result)
            except Exception as e:
                return (fname, False, str(e), None)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_do_one, sf): sf
                for sf in files
            }
            for i, future in enumerate(as_completed(futures), 1):
                fname, success, error, result = future.result()
                if success:
                    uploaded_count += 1
                    if result:
                        results.append(result)
                else:
                    errors.append((fname, error))

                if on_progress:
                    try:
                        on_progress(fname, success, i, len(files))
                    except Exception:
                        pass

        elapsed = time.time() - t0

        return BatchUploadResult(
            uploaded=uploaded_count,
            failed=len(errors),
            total=len(files),
            elapsed_sec=round(elapsed, 2),
            errors=errors,
            results=results,
        )

    def get_batch_urls(
        self,
        receipt_id: str,
        filenames: list,
        ttl_min: int = 30,
    ) -> dict:
        """
        Get signed download URLs for multiple files in one API call.

        One envelope, one round-trip, N URLs back. Avoids rate limits
        that occur when calling get_file_urls() per file concurrently.

        Args:
            receipt_id: Receipt ID from confirm_storage()
            filenames: List of filenames to get URLs for
            ttl_min: URL lifetime in minutes (default: 30)

        Returns:
            Raw response dict with 'urls' list, 'found', 'not_found'.
        """
        keypair = self._require_keypair("get_batch_urls")
        locker_id = compute_locker_id(receipt_id)

        body = {
            "filenames": filenames,
            "ttl_min": ttl_min,
        }

        envelope = build_signed_envelope(
            keypair=keypair,
            receipt_id=receipt_id,
            method="POST",
            path=f"/v1/lockers/{locker_id}/files/urls",
            ops=["locker:read"],
            body=body,
        )

        return self.http.post(
            f"/v1/lockers/{locker_id}/files/urls",
            json=body,
            headers=envelope.headers,
        )


    def download_files(
        self,
        receipt_id: str,
        filenames: list = None,
        workers: int = 5,
        verify: bool = True,
        on_progress=None,
    ) -> "BatchDownloadResult":
        """
        Download multiple files with full concurrency.

        Uses batch URL endpoint (one API call) then downloads all files
        from GCS concurrently. No rate limit risk.

        Args:
            receipt_id: Receipt ID from confirm_storage()
            filenames: Optional list of filenames. None = all files.
            workers: Concurrent download threads (default: 5)
            verify: Compare downloaded content SHA256 to manifest hash
            on_progress: Optional callback(filename, success, index, total)

        Returns:
            BatchDownloadResult with downloaded files and verification.
        """
        import hashlib as _hashlib
        from concurrent.futures import ThreadPoolExecutor, as_completed

        t0 = time.time()

        # Step 1: Get manifest for file list and hashes
        manifest = self.get_manifest(receipt_id)
        manifest_files = manifest.get("files", [])
        hash_lookup = {
            f.get("filename", ""): f.get("content_hash", "")
            for f in manifest_files
        }

        # Determine target filenames
        if filenames is None:
            filenames = list(hash_lookup.keys())

        # Step 2: Get all download URLs in ONE call
        batch_response = self.get_batch_urls(receipt_id, filenames)

        # Build filename → download_url map
        url_map = {}
        for entry in batch_response.get("urls", []):
            url_map[entry["filename"]] = entry["download_url"]

        errors = []
        # Track files not found on server
        for fn in batch_response.get("not_found", []):
            errors.append((fn, "File not found in locker"))

        # Step 3: Download from GCS at full concurrency
        # GCS signed URLs go to storage.googleapis.com — no rate limit
        downloaded_count = 0
        files_out = []

        def _download(fn: str):
            try:
                data = self.download_bytes(url_map[fn])
                local_hash = f"sha256:{_hashlib.sha256(data).hexdigest()}"
                verified_ok = True
                if verify:
                    expected = hash_lookup.get(fn, "")
                    verified_ok = (local_hash == expected) if expected else False

                return (fn, True, None, DownloadedFile(
                    filename=fn,
                    content=data,
                    content_hash=local_hash,
                    size_bytes=len(data),
                    verified=verified_ok,
                ))
            except Exception as e:
                return (fn, False, str(e), None)

        targets = [fn for fn in filenames if fn in url_map]

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_download, fn): fn for fn in targets}
            for i, future in enumerate(as_completed(futures), 1):
                fn, success, error, df = future.result()
                if success:
                    downloaded_count += 1
                    files_out.append(df)
                else:
                    errors.append((fn, error))

                if on_progress:
                    try:
                        on_progress(fn, success, i, len(targets))
                    except Exception:
                        pass

        elapsed = time.time() - t0

        return BatchDownloadResult(
            downloaded=downloaded_count,
            failed=len(errors),
            total=len(filenames),
            elapsed_sec=round(elapsed, 2),
            errors=errors,
            files=files_out,
        )
    
    @staticmethod
    def make_header_block(
        title: str,
        subtitle: str = "",
        description: str = "",
        badge: str = "",
    ) -> Dict[str, Any]:
        """Build a header block for viewer_container blocks."""
        block: Dict[str, Any] = {"type": "header", "title": title}
        if subtitle:
            block["subtitle"] = subtitle
        if description:
            block["description"] = description
        if badge:
            block["badge"] = badge
        return block

    @staticmethod
    def make_stats_block(items: List[Dict[str, Any]], title: str = "Stats") -> Dict[str, Any]:
        """Build a stats block."""
        return {"type": "stats", "title": title, "items": items}

    @staticmethod
    def make_links_block(items: List[Dict[str, Any]], title: str = "Links") -> Dict[str, Any]:
        """Build a links block."""
        return {"type": "links", "title": title, "items": items}

    @staticmethod
    def make_table_block(
        columns: List[Dict[str, Any]],
        rows: List[Dict[str, Any]],
        title: str = "Table",
    ) -> Dict[str, Any]:
        """Build a table block."""
        return {
            "type": "table",
            "title": title,
            "columns": columns,
            "rows": rows,
        }

    @staticmethod
    def make_kv_block(items: List[Dict[str, Any]], title: str = "Details") -> Dict[str, Any]:
        """Build a key-value block."""
        return {"type": "kv", "title": title, "items": items}

    @staticmethod
    def make_status_block(status: str, label: str = "Status", detail: str = "") -> Dict[str, Any]:
        """Build a status block."""
        block: Dict[str, Any] = {"type": "status", "status": status, "label": label}
        if detail:
            block["detail"] = detail
        return block

    @staticmethod
    def make_proofs_block(items: List[Dict[str, Any]], title: str = "Proofs") -> Dict[str, Any]:
        """Build a proofs block."""
        return {"type": "proofs", "title": title, "items": items}

    @staticmethod
    def make_json_block(data: Any, title: str = "Raw JSON") -> Dict[str, Any]:
        """Build a JSON block."""
        return {"type": "json", "title": title, "data": data}

    @staticmethod
    def make_file_meta_block(
        filename: str,
        content_type: str = "",
        size_bytes: Optional[int] = None,
        updated_at: Optional[str] = None,
        sha256: str = "",
        extra: Optional[Dict[str, Any]] = None,
        title: str = "File Metadata",
    ) -> Dict[str, Any]:
        """Build a file metadata block."""
        items: List[Dict[str, Any]] = [{"key": "Filename", "value": filename}]
        if content_type:
            items.append({"key": "Content-Type", "value": content_type})
        if size_bytes is not None:
            items.append({"key": "Size Bytes", "value": size_bytes})
        if updated_at:
            items.append({"key": "Updated At", "value": updated_at})
        if sha256:
            items.append({"key": "SHA-256", "value": sha256})
        if extra:
            for key, value in extra.items():
                items.append({"key": str(key), "value": value})
        return {"type": "file_meta", "title": title, "items": items}

    @staticmethod
    def make_file_preview_block(
        filename: str,
        content_type: str = "",
        url: str = "",
        text_content: str = "",
        json_data: Any = None,
        hex_preview: str = "",
        base64_data: str = "",
        size_bytes: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Build a file preview block.

        The frontend resolves rendering mode from filename/content_type and
        available fields so callers do not manage mime-specific rendering logic.
        """
        block: Dict[str, Any] = {
            "type": "file_preview",
            "filename": filename,
        }
        if content_type:
            block["content_type"] = content_type
        if url:
            block["url"] = url
        if text_content:
            block["text_content"] = text_content
        if json_data is not None:
            block["data"] = json_data
        if hex_preview:
            block["hex_preview"] = hex_preview
        if base64_data:
            block["base64"] = base64_data
        if size_bytes is not None:
            block["size_bytes"] = size_bytes
        return block




















































































































































































