"""
Async Nukez client - mirrors the sync Nukez client's public API with async methods.

Every I/O method is ``async def`` and uses ``await``.
Pure-computation helpers (auth, sanitization, viewer URL builders) are
reused directly from the sync client and auth modules.

Usage:
    async with AsyncNukez(keypair_path="~/.config/solana/id.json") as client:
        request = await client.request_storage(units=1)
        transfer = await client.solana_transfer(request.pay_to_address, request.amount_sol)
        receipt = await client.confirm_storage(request.pay_req_id, transfer.signature)

        manifest = await client.provision_locker(receipt.id)
        urls = await client.create_file(receipt.id, "data.txt")
        await client.upload_bytes(urls.upload_url, b"Hello!")
        data = await client.download_bytes(urls.download_url)
"""

import asyncio
import base64
import binascii
import hashlib
import json
import mimetypes
import os
import re
import time
import uuid
import httpx as _httpx
from pathlib import Path
from typing import Dict, List, Optional, Union, Any, Callable
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
from ._async_http import AsyncHTTPClient

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


class AsyncNukez:
    """
    Async agent-native Nukez client.

    Mirrors the sync ``Nukez`` class exactly — every public method that
    performs I/O is ``async def``.  Pure-computation helpers are sync.

    Usage:
        async with AsyncNukez(keypair_path="~/.config/solana/id.json") as client:
            request = await client.request_storage(units=1)
            ...
    """

    def __init__(
        self,
        keypair_path: Optional[Union[str, Path]] = None,
        base_url: str = "https://api.nukez.xyz",
        network: str = "devnet",
        rpc_url: Optional[str] = "https://api.devnet.solana.com",
        timeout: int = None,
        evm_private_key_path: Optional[Union[str, Path]] = None,
        evm_rpc_url: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip('/')
        self.network = network
        self.rpc_url = rpc_url
        self.timeout = timeout or 120
        self.http = AsyncHTTPClient(base_url, timeout=self.timeout)
        self._raw_client = _httpx.AsyncClient(timeout=60, follow_redirects=True)

        # Optional keypair for signing operations
        self.keypair: Optional[Keypair] = None
        if keypair_path:
            self.keypair = Keypair(keypair_path)

        # Fail-fast: if caller wants EVM payments, verify web3 is available now
        if evm_private_key_path:
            try:
                from .evm_payment import HAS_WEB3
            except ImportError:
                HAS_WEB3 = False
            if not HAS_WEB3:
                raise ImportError(
                    "web3 libraries required for EVM payments "
                    "(evm_private_key_path was provided). "
                    "Install with: pip install pynukez[evm]"
                )

        # Lazy-initialized payment handlers
        self._payment = None
        self._keypair_path = keypair_path
        self._evm_private_key_path = evm_private_key_path
        self._evm_rpc_url = evm_rpc_url
        self._evm_payment = None
        self._upload_jobs: Dict[str, Dict[str, Any]] = {}
        self._upload_jobs_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self):
        """Close underlying HTTP clients."""
        await self.http.aclose()
        await self._raw_client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    # ------------------------------------------------------------------
    # Internal helpers (pure computation, sync)
    # ------------------------------------------------------------------

    def _require_keypair(self, operation: str) -> Keypair:
        """Ensure keypair is available, with helpful error message."""
        if not self.keypair:
            raise NukezError(
                f"{operation} requires keypair_path. "
                f"Initialize AsyncNukez(keypair_path='~/.config/solana/id.json')"
            )
        return self.keypair

    @staticmethod
    def _infer_content_type(filename: str, explicit: Optional[str] = None) -> str:
        """Infer MIME type from filename when explicit value is not provided."""
        if explicit:
            return explicit
        guessed = mimetypes.guess_type(filename)[0]
        return guessed or "application/octet-stream"

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        """Sanitize filename for gateway: replace spaces and disallowed chars."""
        s = name.replace(" ", "_")
        s = s.lstrip(".")
        s = re.sub(r"[^a-zA-Z0-9._/\-]", "_", s)
        if s and not re.match(r"[a-zA-Z0-9_]", s[0]):
            s = "_" + s
        return s or "file"

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

    @staticmethod
    def _is_sandbox_path_unavailable_error(exc: Exception) -> bool:
        message = str(exc).lower()
        details_text = ""
        details = getattr(exc, "details", None)
        if details:
            try:
                details_text = json.dumps(details, sort_keys=True).lower()
            except Exception:
                details_text = str(details).lower()
        return any(marker in message or marker in details_text for marker in _SANDBOX_PATH_BLOCKED_MARKERS)

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

            filename = self._sanitize_filename(
                str(spec.get("filename") or p.name).strip()
            )
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

    @staticmethod
    def _normalize_viewer_base_url(viewer_base_url: str) -> str:
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
        return {
            "kind": "button",
            "label": label,
            "href": url,
            "variant": variant,
            "target": "_blank",
        }

    @staticmethod
    def _viewer_renderer_contract() -> Dict[str, str]:
        return {
            "name": VIEWER_RENDERER_CONTRACT_NAME,
            "version": VIEWER_RENDERER_CONTRACT_VERSION,
        }

    @staticmethod
    def _viewer_container_contract() -> Dict[str, str]:
        return {
            "name": VIEWER_CONTAINER_CONTRACT_NAME,
            "version": VIEWER_CONTAINER_CONTRACT_VERSION,
        }

    @staticmethod
    def compute_hash(data: Union[str, bytes]) -> str:
        """Compute SHA256 hash of data for verification."""
        if isinstance(data, str):
            data = data.encode('utf-8')
        return hashlib.sha256(data).hexdigest()

    def sign_message(self, message: str) -> str:
        """Sign message with current keypair (pure computation, sync)."""
        keypair = self._require_keypair("sign_message")
        return keypair.sign_message(message.encode('utf-8'))

    def get_provider_info(self, provider: str = "gcs"):
        """Get capabilities for a storage provider (pure lookup, sync)."""
        from .types import PROVIDERS
        info = PROVIDERS.get(provider)
        if not info:
            raise NukezError(
                f"Unknown provider '{provider}'. "
                f"Available: {', '.join(PROVIDERS.keys())}"
            )
        return info

    # ------------------------------------------------------------------
    # DISCOVERY & PRICING (No auth required)
    # ------------------------------------------------------------------

    async def get_price(self, units: int = 1) -> PriceInfo:
        """Get current storage pricing."""
        response = await self.http.get("/v1/price", params={"units": units})
        meta = response.get("meta", {})
        sol = meta.get("sol", {})

        return PriceInfo(
            units=units,
            unit_price_usd=response.get("unit_price_usd", 0.0),
            total_usd=meta.get("total_usd", response.get("unit_price_usd", 0.0) * units),
            amount_sol=float(sol.get("amount_sol", 0) or 0),
            amount_lamports=int(sol.get("amount_lamports", 0) or 0),
            network=meta.get("network", self.network),
            pay_asset=meta.get("pay_asset", "SOL"),
            provider=meta.get("provider", ""),
            mode=meta.get("mode", "static"),
            cost_breakdown={
                "base_cost": meta.get("base_cost"),
                "attestation_fee": meta.get("attestation_fee"),
                "egress_allowance": meta.get("egress_allowance"),
                "margin": meta.get("margin"),
                "discount": meta.get("discount"),
            } if meta else None,
            payment_options=meta.get("payment_options"),
        )

    # ------------------------------------------------------------------
    # PAYMENT FLOW
    # ------------------------------------------------------------------

    async def request_storage(
        self,
        units: int = 1,
        provider: str = None,
        pay_network: str = None,
        pay_asset: str = None,
    ) -> StorageRequest:
        """Step 1: Start the x402 payment flow to purchase storage."""
        try:
            body = {"units": units}
            if provider:
                body["provider"] = provider
            if pay_network:
                body["pay_network"] = pay_network
            if pay_asset:
                body["pay_asset"] = pay_asset
            await self.http.post("/v1/storage/request", json=body)
            raise NukezError(
                "Expected HTTP 402 Payment Required, got success. "
                "This indicates an API change - please report this issue."
            )
        except PaymentRequiredError as e:
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
                payment_options=e.payment_options,
                quote_expires_at=e.quote_expires_at,
                quote_schema=e.details.get("quote_schema"),
                idempotency_key=e.details.get("idempotency_key"),
                terms=e.terms,
                price_breakdown=e.details.get("price_breakdown"),
            )
            return request

    async def solana_transfer(
        self,
        to_address: str,
        amount_sol: Union[str, float],
    ) -> TransferResult:
        """Step 2: Execute Solana SOL transfer (wraps sync payment in thread)."""
        self._require_keypair("solana_transfer")

        if self._payment is None:
            PaymentClass = _get_solana_payment()
            self._payment = PaymentClass(
                keypair_path=str(self.keypair.keypair_path),
                network=self.network,
                rpc_url=self.rpc_url,
            )

        signature = await asyncio.to_thread(
            self._payment.transfer_sol,
            to_address=to_address,
            amount_sol=float(amount_sol),
        )

        return TransferResult(
            signature=signature,
            to_address=to_address,
            amount_sol=float(amount_sol),
            network=self.network,
        )

    async def evm_transfer(
        self,
        to_address: str,
        amount_raw: int,
        pay_asset: str = "MON",
        token_address: Optional[str] = None,
        network: str = "monad-testnet",
    ) -> TransferResult:
        """Step 2 (EVM): Execute EVM token transfer for storage payment."""
        if self._evm_payment is None:
            if not self._evm_private_key_path:
                raise NukezError(
                    "EVM private key not configured. Pass evm_private_key_path "
                    "to the AsyncNukez constructor, or use solana_transfer() for "
                    "Solana payments."
                )
            from .evm_payment import EVMPayment
            self._evm_payment = EVMPayment(
                private_key_path=self._evm_private_key_path,
                network=network,
                rpc_url=self._evm_rpc_url,
            )

        tx_hash = await asyncio.to_thread(
            self._evm_payment.transfer,
            to_address=to_address,
            amount_raw=amount_raw,
            pay_asset=pay_asset,
            token_address=token_address,
        )

        return TransferResult(
            signature=tx_hash,
            to_address=to_address,
            amount_sol=0.0,
            network=network,
            chain="evm",
            pay_asset=pay_asset,
            amount_raw=amount_raw,
            tx_hash=tx_hash,
        )

    async def confirm_storage(
        self,
        pay_req_id: str,
        tx_sig: str,
        max_retries: int = 5,
        initial_delay: float = 2.0,
        payment_chain: Optional[str] = None,
        payment_asset: Optional[str] = None,
    ) -> Receipt:
        """Step 3: Confirm payment and receive storage receipt (with retry)."""
        url = f"{self.base_url}/v1/storage/confirm"
        payload = {"pay_req_id": pay_req_id}
        if payment_chain:
            payload["payment_chain"] = payment_chain
        if payment_asset:
            payload["payment_asset"] = payment_asset
        headers = {
            "Content-Type": "application/json",
            "X402-TX": tx_sig,
        }

        last_error: Optional[Exception] = None

        for attempt in range(max_retries):
            try:
                resp = await self._raw_client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout,
                )

                if resp.status_code == 200:
                    data = resp.json()
                    rcpt = data.get("receipt", {})
                    receipt = Receipt(
                        id=data["receipt_id"],
                        units=rcpt.get("units", data.get("units", 1)),
                        payer_pubkey=data.get("payer_pubkey", rcpt.get("payer_pubkey", "")),
                        network=rcpt.get("network", self.network),
                        created_at=rcpt.get("created_at"),
                        provider=rcpt.get("provider", ""),
                        pay_asset=rcpt.get("pay_asset", "SOL"),
                        tx_hash=rcpt.get("tx_hash", data.get("tx_sig", "")),
                        paid_amount=str(rcpt.get("paid_amount", "")) if rcpt.get("paid_amount") else None,
                        paid_raw=rcpt.get("paid_raw"),
                        block_number=rcpt.get("block_number"),
                        slot=rcpt.get("slot"),
                        sig_alg=data.get("sig_alg", rcpt.get("receipt_sig_alg", "")),
                        unit_price_usd=float(rcpt.get("unit_price_usd", 0)),
                        price_usd=float(rcpt.get("price_usd", 0)),
                    )
                    return receipt

                if resp.status_code == 402:
                    try:
                        body = resp.json()
                    except Exception:
                        body = {"raw": resp.text}

                    verify_info = (body.get("details") or {}).get("verify") or {}
                    err = verify_info.get("err", "")
                    error_code = body.get("error_code", "")
                    message = body.get("message", "")

                    is_tx_not_found = (
                        err == "tx_not_found"
                        or error_code == "TX_NOT_FOUND"
                        or "tx_not_found" in message.lower()
                        or ("transaction" in message.lower() and "not found" in message.lower())
                    )

                    if is_tx_not_found and attempt < max_retries - 1:
                        delay = initial_delay * (2 ** attempt)
                        print(f"[pynukez] Transaction not found, retrying in {delay}s (attempt {attempt + 1}/{max_retries})")
                        await asyncio.sleep(delay)
                        continue

                    last_error = TransactionNotFoundError(
                        tx_sig=tx_sig,
                        suggested_delay=int(initial_delay * (2 ** attempt)),
                    ) if is_tx_not_found else NukezError(
                        f"Payment confirmation failed: {body.get('message', resp.text)}",
                        details=body,
                    )
                    raise last_error

                resp.raise_for_status()

            except _httpx.HTTPError as e:
                last_error = NukezError(f"Request failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(initial_delay * (2 ** attempt))
                    continue
                raise last_error

        if last_error:
            raise last_error
        raise NukezError(f"confirm_storage failed after {max_retries} attempts")

    # ------------------------------------------------------------------
    # LOCKER OPERATIONS
    # ------------------------------------------------------------------

    async def provision_locker(
        self,
        receipt_id: str,
        tags: Optional[List[str]] = None,
        operator_pubkey: Optional[str] = None,
    ) -> NukezManifest:
        """Create storage locker namespace for files."""
        keypair = self._require_keypair("provision_locker")
        locker_id = compute_locker_id(receipt_id)
        body = {"receipt_id": receipt_id, "tags": tags or []}
        if operator_pubkey:
            body["operator_pubkey"] = operator_pubkey

        envelope = build_signed_envelope(
            keypair=keypair,
            receipt_id=receipt_id,
            method="POST",
            path="/v1/storage/signed_provision",
            ops=["locker:provision"],
            body=body,
        )

        response = await self.http.post(
            "/v1/storage/signed_provision",
            headers=envelope.headers,
            data=envelope.canonical_body.encode("utf-8"),
        )

        space = response.get("space", response)

        return NukezManifest(
            locker_id=space.get("locker_id", locker_id),
            receipt_id=receipt_id,
            bucket=space.get("bucket", ""),
            path_prefix=space.get("path_prefix", ""),
            tags=space.get("tags", tags or []),
            cap_token=space.get("cap_token"),
            cap_expires_in_sec=space.get("cap_expires_in_sec"),
            created_at=space.get("created_at"),
        )

    async def create_file(
        self,
        receipt_id: str,
        filename: str,
        content_type: str = "application/octet-stream",
        ttl_min: int = 30,
    ) -> FileUrls:
        """Create a new file and get upload/download URLs."""
        keypair = self._require_keypair("create_file")
        locker_id = compute_locker_id(receipt_id)

        body = {
            "filename": filename,
            "content_type": content_type,
            "ttl_min": ttl_min,
        }

        envelope = build_signed_envelope(
            keypair=keypair,
            receipt_id=receipt_id,
            method="POST",
            path=f"/v1/lockers/{locker_id}/files",
            ops=["locker:write"],
            body=body,
        )

        response = await self.http.post(
            f"/v1/lockers/{locker_id}/files",
            headers=envelope.headers,
            data=envelope.canonical_body.encode("utf-8"),
        )

        return FileUrls(
            filename=response.get("filename", filename),
            upload_url=response["upload_url"],
            download_url=response["download_url"],
            content_type=response.get("content_type", content_type),
            expires_in_sec=response.get("urls_expire_in_sec", ttl_min * 60),
        )

    async def create_files_batch(
        self,
        receipt_id: str,
        files: List[Dict[str, Any]],
        ttl_min: int = 30,
    ) -> Dict[str, Any]:
        """Create multiple file entries and mint upload/download URLs in one call."""
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

        return await self.http.post(
            f"/v1/lockers/{locker_id}/files/batch",
            headers=envelope.headers,
            data=envelope.canonical_body.encode("utf-8"),
        )

    # ------------------------------------------------------------------
    # PATH-BASED UPLOADS
    # ------------------------------------------------------------------

    async def upload_file_path(
        self,
        receipt_id: str,
        filepath: str,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
        ttl_min: int = 30,
        confirm: bool = True,
    ) -> Dict[str, Any]:
        """Upload one local file by path."""
        source: Dict[str, Any] = {"filepath": filepath}
        if filename:
            source["filename"] = filename
        if content_type:
            source["content_type"] = content_type

        result = await self.bulk_upload_paths(
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

    async def bulk_upload_paths(
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
        """Upload multiple local files by path with optional confirm + attestation."""
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
        create_response = await self.create_files_batch(
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
        progress_counter = {"done": 0}
        progress_lock = asyncio.Lock()

        async def _upload_one(spec: Dict[str, Any]) -> Dict[str, Any]:
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
                body = await asyncio.to_thread(Path(spec["filepath"]).read_bytes)
                await self.upload_bytes(
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

        sem = asyncio.Semaphore(max(1, int(workers or 1)))

        async def _limited(spec: Dict[str, Any]) -> Dict[str, Any]:
            async with sem:
                row = await _upload_one(spec)
                if on_progress:
                    async with progress_lock:
                        progress_counter["done"] += 1
                        try:
                            on_progress(row["filename"], row["success"], progress_counter["done"], total)
                        except Exception:
                            pass
                return row

        results = await asyncio.gather(*[_limited(s) for s in normalized], return_exceptions=True)

        uploaded_names: List[str] = []
        for r in results:
            if isinstance(r, Exception):
                errors.append({"filename": "unknown", "filepath": "", "error": str(r)})
                file_results.append({
                    "filename": "unknown",
                    "filepath": "",
                    "content_type": "",
                    "size_bytes": 0,
                    "success": False,
                    "error": str(r),
                })
            else:
                file_results.append(r)
                if r["success"]:
                    uploaded_names.append(r["filename"])
                else:
                    errors.append({
                        "filename": r["filename"],
                        "filepath": r["filepath"],
                        "error": r["error"],
                    })

        confirmed_map: Dict[str, ConfirmResult] = {}
        if confirm and uploaded_names:
            confirm_result = await self.confirm_files(receipt_id, uploaded_names)
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
                att = await self.attest(receipt_id, sync=attest_sync)
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
        uploaded_count = len([r for r in file_results if r.get("success")])

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

    async def upload_directory(
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
        """Upload files from a directory using glob pattern filtering."""
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

        return await self.bulk_upload_paths(
            receipt_id=receipt_id,
            sources=sources,
            workers=workers,
            ttl_min=ttl_min,
            confirm=confirm,
            auto_attest=auto_attest,
            attest_sync=attest_sync,
        )

    # ------------------------------------------------------------------
    # BACKGROUND UPLOAD JOBS (asyncio.create_task)
    # ------------------------------------------------------------------

    async def _set_upload_job_state(self, job_id: str, **updates: Any) -> None:
        async with self._upload_jobs_lock:
            job = self._upload_jobs.get(job_id)
            if not job:
                return
            job.update(updates)
            job["updated_at"] = int(time.time())

    async def start_bulk_upload_job(
        self,
        receipt_id: str,
        sources: List[Union[str, Dict[str, Any]]],
        workers: int = 6,
        ttl_min: int = 30,
        confirm: bool = True,
        auto_attest: bool = False,
        attest_sync: bool = False,
    ) -> Dict[str, Any]:
        """Start a non-blocking background bulk upload job. Returns immediately with job_id."""
        normalized = self._normalize_path_sources(sources)
        total = len(normalized)
        now = int(time.time())
        job_id = f"up_{uuid.uuid4().hex[:12]}"

        async with self._upload_jobs_lock:
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

        def _progress_cb(_filename: str, success: bool, _index: int, _total: int) -> None:
            if success:
                progress["uploaded"] += 1
            else:
                progress["failed"] += 1
            # Fire-and-forget state update — progress callback is sync
            # The actual state update will happen after gather completes

        async def _run_job() -> None:
            await self._set_upload_job_state(job_id, status="running", started_at=int(time.time()))
            try:
                result = await self.bulk_upload_paths(
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
                await self._set_upload_job_state(
                    job_id,
                    status=terminal,
                    finished_at=int(time.time()),
                    uploaded=result.get("uploaded", 0),
                    failed=result.get("failed", 0),
                    confirmed=result.get("confirmed", 0),
                    result=result,
                )
            except Exception as e:
                await self._set_upload_job_state(
                    job_id,
                    status="failed",
                    finished_at=int(time.time()),
                    error=str(e),
                )

        asyncio.create_task(_run_job())

        return {
            "job_id": job_id,
            "status": "queued",
            "created_at": now,
            "total": total,
            "receipt_id": receipt_id,
        }

    async def get_upload_job(self, job_id: str) -> Dict[str, Any]:
        """Return the current state of a previously started upload job."""
        async with self._upload_jobs_lock:
            job = self._upload_jobs.get(job_id)
            if not job:
                raise NukezError(f"Upload job not found: {job_id}")
            return dict(job)

    async def list_upload_jobs(self, limit: int = 20) -> Dict[str, Any]:
        """List recent upload jobs tracked in this SDK process."""
        async with self._upload_jobs_lock:
            jobs = list(self._upload_jobs.values())
        jobs.sort(key=lambda j: j.get("created_at", 0), reverse=True)
        if limit > 0:
            jobs = jobs[:limit]
        return {
            "count": len(jobs),
            "jobs": [dict(j) for j in jobs],
        }

    # ------------------------------------------------------------------
    # SANDBOX INGEST
    # ------------------------------------------------------------------

    async def sandbox_create_ingest_job(
        self,
        receipt_id: str,
        files: List[Dict[str, Any]],
        execution_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a sandbox-ingest job."""
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

        return await self.http.post(path, headers=envelope.headers, data=envelope.canonical_body.encode("utf-8"))

    async def sandbox_append_ingest_part(
        self,
        receipt_id: str,
        job_id: str,
        file_id: str,
        part_no: int,
        payload_b64: str,
        *,
        is_last: bool = False,
    ) -> Dict[str, Any]:
        """Append one base64 chunk into a sandbox-ingest job file."""
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
        return await self.http.post(path, headers=envelope.headers, data=envelope.canonical_body.encode("utf-8"))

    async def sandbox_complete_ingest_job(
        self,
        receipt_id: str,
        job_id: str,
        *,
        file_ids: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Finalize a sandbox-ingest job."""
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
        return await self.http.post(path, headers=envelope.headers, data=envelope.canonical_body.encode("utf-8"))

    async def sandbox_upload_bytes(
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
        """Upload bytes through the sandbox-ingest job flow (chunked base64 parts)."""
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

        created = await self.sandbox_create_ingest_job(
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
            chunk = data_bytes[offset: offset + part_size]
            payload_b64 = base64.b64encode(chunk).decode("ascii")
            is_last = offset + part_size >= len(data_bytes)
            await self.sandbox_append_ingest_part(
                receipt_id=receipt_id,
                job_id=job_id,
                file_id=file_id,
                part_no=idx,
                payload_b64=payload_b64,
                is_last=is_last,
            )

        terminal = await self.sandbox_complete_ingest_job(
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

    async def sandbox_upload_base64(
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
        """Decode base64 and upload via sandbox-ingest chunk flow."""
        raw = (payload_b64 or "").strip()
        if not raw:
            raise NukezError("sandbox_upload_base64 requires non-empty payload_b64")
        if raw.lower() in {"none", "null", "undefined"}:
            raise NukezError("payload_b64 sentinel values are rejected")
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise NukezError("payload_b64 must be valid base64") from exc

        return await self.sandbox_upload_bytes(
            receipt_id=receipt_id,
            filename=filename,
            data=data,
            content_type=content_type,
            expected_sha256=expected_sha256,
            part_size_bytes=part_size_bytes,
            execution_mode=execution_mode,
        )

    async def sandbox_upload_file_path(
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
        """Read a local file and upload it through sandbox-ingest chunk flow."""
        p = Path(filepath).expanduser()
        if not p.exists():
            raise NukezError(f"File not found: {p}")
        if not p.is_file():
            raise NukezError(f"Expected file path, got non-file: {p}")
        remote_name = (filename or p.name).strip()
        if not remote_name:
            raise NukezError("sandbox_upload_file_path produced an empty filename")

        file_data = await asyncio.to_thread(p.read_bytes)

        return await self.sandbox_upload_bytes(
            receipt_id=receipt_id,
            filename=remote_name,
            data=file_data,
            content_type=content_type or self._infer_content_type(remote_name),
            expected_sha256=expected_sha256,
            part_size_bytes=part_size_bytes,
            execution_mode=execution_mode,
        )

    # ------------------------------------------------------------------
    # RAW HTTP UPLOAD / DOWNLOAD
    # ------------------------------------------------------------------

    async def upload_bytes(self, upload_url: str, data: bytes, content_type: str = None) -> UploadResult:
        """Upload data to signed URL."""
        headers = {"Content-Type": content_type or "application/octet-stream"}

        response = await self._raw_client.put(upload_url, content=data, headers=headers)
        response.raise_for_status()

        return UploadResult(
            upload_url=upload_url,
            size_bytes=len(data),
            content_type=content_type or "application/octet-stream",
            uploaded_at=int(time.time()),
        )

    async def upload_string(
        self,
        upload_url: str,
        data: str,
        content_type: str = None,
    ) -> UploadResult:
        """Upload string data to signed URL (agent-native interface)."""
        url_err = validate_signed_url(upload_url, "upload_url")
        if url_err:
            raise NukezError(url_err)

        raw_bytes = len(data.encode("utf-8"))
        if raw_bytes > UPLOAD_STRING_MAX_BYTES:
            raise NukezError(
                "PAYLOAD_TOO_LARGE_FOR_CONTEXT: upload_string payload exceeds context-safe limit. "
                "Use upload_file_path, bulk_upload_paths, upload_directory, or start_bulk_upload_job."
            )

        cleaned, fix_applied = sanitize_upload_data(data)

        cleaned_bytes = len(cleaned.encode("utf-8"))
        if cleaned_bytes > UPLOAD_STRING_MAX_BYTES:
            raise NukezError(
                "PAYLOAD_TOO_LARGE_FOR_CONTEXT: cleaned upload payload exceeds context-safe limit. "
                "Use upload_file_path, bulk_upload_paths, upload_directory, or start_bulk_upload_job."
            )

        return await self.upload_bytes(upload_url, cleaned.encode("utf-8"), content_type)

    async def download_bytes(
        self,
        download_url: str,
        max_retries: int = 3,
        initial_delay: float = 2.0,
    ) -> bytes:
        """Download data from signed URL with retry on 404."""
        url_err = validate_signed_url(download_url, "download_url")
        if url_err:
            raise NukezError(url_err)

        max_attempts = 1 + max_retries
        for attempt in range(max_attempts):
            if attempt > 0:
                delay = initial_delay * (2 ** (attempt - 1))
                print(
                    f"[pynukez] Download returned 404, retrying in {delay}s "
                    f"(attempt {attempt}/{max_retries})"
                )
                await asyncio.sleep(delay)

            try:
                response = await self._raw_client.get(download_url)
                if response.status_code == 404 and attempt < max_attempts - 1:
                    continue
                response.raise_for_status()
                return response.content
            except _httpx.HTTPStatusError as e:
                status = e.response.status_code if e.response is not None else None
                if status == 404:
                    error_details = {"retryable": True, "status": 404}
                    message = (
                        "Download failed (HTTP 404). The file may still be propagating on a "
                        "content-addressed provider (Arweave/Filecoin). "
                        "Call confirm_file(receipt_id, filename) to verify availability, "
                        "then retry download_bytes(). If the file truly does not exist, "
                        "call list_files(receipt_id) to check."
                    )
                    try:
                        body = e.response.json()
                        if body.get("error_code") == "CONTENT_PROPAGATION_PENDING":
                            provider = body.get("details", {}).get("provider", "unknown")
                            suggested = body.get("details", {}).get("suggested_delay", 15)
                            message = (
                                f"Download failed: content-addressed storage ({provider}) "
                                f"has not finished propagating. The upload was confirmed but "
                                f"data is not yet downloadable. Wait {suggested}s and retry "
                                f"download_bytes(), or call confirm_file(receipt_id, filename) "
                                f"to verify availability."
                            )
                            error_details.update(body.get("details", {}))
                            error_details["error_code"] = "CONTENT_PROPAGATION_PENDING"
                    except Exception:
                        pass
                    raise NukezError(message, details=error_details) from e
                if status in (400, 403):
                    raise NukezError(
                        f"Download failed (HTTP {status}). The signed URL may be expired or malformed. "
                        f"Call get_file_urls(receipt_id=..., filename=...) or list_files(receipt_id=...) "
                        f"to get fresh download URLs."
                    ) from e
                raise

        raise NukezError(
            "Download failed (HTTP 404) after retries. The file may still be propagating. "
            "Call confirm_file(receipt_id, filename) to verify availability, then retry.",
            details={"retryable": True, "status": 404},
        )

    # ------------------------------------------------------------------
    # FILE LISTING / URLS
    # ------------------------------------------------------------------

    async def list_files(self, receipt_id: str) -> List[FileInfo]:
        """List all files in locker."""
        keypair = self._require_keypair("list_files")
        locker_id = compute_locker_id(receipt_id)

        envelope = build_signed_envelope(
            keypair=keypair,
            receipt_id=receipt_id,
            method="GET",
            path=f"/v1/lockers/{locker_id}/files",
            ops=["locker:list"],
        )

        response = await self.http.get(
            f"/v1/lockers/{locker_id}/files",
            headers=envelope.headers,
        )

        files = response.get("files", [])
        return [
            FileInfo(
                filename=f["filename"],
                content_type=f.get("content_type", "application/octet-stream"),
                size_bytes=f.get("size_bytes", 0),
                content_hash=f.get("content_hash"),
                provider_ref=f.get("provider_ref"),
                created_at=f.get("created_at"),
                updated_at=f.get("updated_at"),
                object_key=f.get("object_key"),
            )
            for f in files
        ]

    async def get_file_urls(
        self,
        receipt_id: str,
        filename: str,
        ttl_min: int = 30,
    ) -> FileUrls:
        """Get fresh upload/download URLs for existing file."""
        keypair = self._require_keypair("get_file_urls")
        locker_id = compute_locker_id(receipt_id)

        envelope = build_signed_envelope(
            keypair=keypair,
            receipt_id=receipt_id,
            method="GET",
            path=f"/v1/lockers/{locker_id}/files/{filename}",
            ops=["locker:read"],
        )

        response = await self.http.get(
            f"/v1/lockers/{locker_id}/files/{filename}",
            headers=envelope.headers,
        )

        return FileUrls(
            filename=response["filename"],
            upload_url=response["upload_url"],
            download_url=response["download_url"],
            content_type=response.get("content_type", "application/octet-stream"),
            expires_in_sec=response.get("expires_in_sec", ttl_min * 60),
        )

    async def delete_file(self, receipt_id: str, filename: str) -> DeleteResult:
        """Delete file from locker."""
        keypair = self._require_keypair("delete_file")
        locker_id = compute_locker_id(receipt_id)

        envelope = build_signed_envelope(
            keypair=keypair,
            receipt_id=receipt_id,
            method="DELETE",
            path=f"/v1/lockers/{locker_id}/files/{filename}",
            ops=["locker:write"],
        )

        response = await self.http.delete(
            f"/v1/lockers/{locker_id}/files/{filename}",
            headers=envelope.headers,
        )

        return DeleteResult(
            filename=filename,
            deleted=response.get("deleted", True),
            deleted_at=response.get("deleted_at"),
        )

    async def get_manifest(self, receipt_id: str) -> dict:
        """Full locker state in one call."""
        keypair = self._require_keypair("get_manifest")
        locker_id = compute_locker_id(receipt_id)

        envelope = build_signed_envelope(
            keypair=keypair,
            receipt_id=receipt_id,
            method="GET",
            path=f"/v1/lockers/{locker_id}/manifest",
            ops=["locker:read"],
        )

        return await self.http.get(
            f"/v1/lockers/{locker_id}/manifest",
            headers=envelope.headers,
        )

    # ------------------------------------------------------------------
    # VERIFICATION & ATTESTATION
    # ------------------------------------------------------------------

    async def verify_storage(self, receipt_id: str) -> VerificationResult:
        """Verify storage integrity and get cryptographic attestation."""
        response = await self.http.get(
            "/v1/storage/verify",
            params={"receipt_id": receipt_id},
        )

        attestation = response.get("attestation") or {}

        return VerificationResult(
            receipt_id=receipt_id,
            verified=response.get("verified", False),
            result_hash=attestation.get("result_hash", response.get("result_hash", "")),
            att_code=str(attestation.get("att_code", response.get("att_code", ""))),
            verified_at=attestation.get("attested_at", response.get("verified_at", "")),
            merkle_root=attestation.get("merkle_root", ""),
            manifest_signature=attestation.get("manifest_signature", ""),
            file_count=attestation.get("file_count", 0),
            files=attestation.get("files"),
            locker_id=response.get("locker_id", attestation.get("locker_id", "")),
            verify_url=response.get("verify_url", ""),
        )

    async def get_merkle_proof(self, receipt_id: str, filename: str) -> dict:
        """Get a merkle inclusion proof for a specific file."""
        return await self.http.get(
            "/v1/storage/merkle-proof",
            params={"receipt_id": receipt_id, "filename": filename},
        )

    async def confirm_file(self, receipt_id: str, filename: str) -> ConfirmResult:
        """Confirm a file upload by computing its content hash server-side."""
        response = await self.http.post(
            "/v1/files/confirm",
            params={"receipt_id": receipt_id, "filename": filename},
        )

        return ConfirmResult(
            filename=response.get("filename", filename),
            content_hash=response.get("content_hash", ""),
            size_bytes=response.get("size_bytes", 0),
            confirmed=True,
        )

    async def confirm_files(self, receipt_id: str, filenames: List[str]) -> BatchConfirmResult:
        """Confirm multiple file uploads in a single operation."""
        response = await self.http.post(
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

    async def attest(self, receipt_id: str, sync: bool = True) -> AttestResult:
        """Trigger attestation -- compute merkle root and optionally push on-chain."""
        params = {"receipt_id": receipt_id}
        if sync:
            params["sync"] = "true"

        response = await self.http.post(
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

    # ------------------------------------------------------------------
    # BATCH UPLOAD / DOWNLOAD
    # ------------------------------------------------------------------

    async def upload_files(
        self,
        receipt_id: str,
        files: List[Dict[str, Any]],
        workers: int = 10,
        confirm: bool = True,
        on_progress: Optional[Any] = None,
    ) -> BatchUploadResult:
        """Upload multiple files concurrently with optional confirmation."""
        t0 = time.time()
        uploaded_count = 0
        errors = []
        results = []

        async def _do_one(sf: Dict[str, Any]):
            fname = sf["filename"]
            content = sf["content"]
            ctype = sf.get("content_type", "application/octet-stream")
            try:
                urls = await self.create_file(receipt_id, fname, content_type=ctype)
                result = await self.upload_bytes(urls.upload_url, content, content_type=ctype)

                if confirm:
                    try:
                        await self.confirm_file(receipt_id, fname)
                    except Exception:
                        pass
                return (fname, True, None, result)
            except Exception as e:
                return (fname, False, str(e), None)

        sem = asyncio.Semaphore(workers)

        async def _limited(sf: Dict[str, Any], index: int):
            async with sem:
                result = await _do_one(sf)
                if on_progress:
                    try:
                        on_progress(result[0], result[1], index, len(files))
                    except Exception:
                        pass
                return result

        gather_results = await asyncio.gather(
            *[_limited(sf, i + 1) for i, sf in enumerate(files)],
            return_exceptions=True,
        )

        for r in gather_results:
            if isinstance(r, Exception):
                errors.append(("unknown", str(r)))
            else:
                fname, success, error, result = r
                if success:
                    uploaded_count += 1
                    if result:
                        results.append(result)
                else:
                    errors.append((fname, error))

        elapsed = time.time() - t0

        return BatchUploadResult(
            uploaded=uploaded_count,
            failed=len(errors),
            total=len(files),
            elapsed_sec=round(elapsed, 2),
            errors=errors,
            results=results,
        )

    async def get_batch_urls(
        self,
        receipt_id: str,
        filenames: list,
        ttl_min: int = 30,
    ) -> dict:
        """Get signed download URLs for multiple files in one API call."""
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

        return await self.http.post(
            f"/v1/lockers/{locker_id}/files/urls",
            headers=envelope.headers,
            data=envelope.canonical_body.encode("utf-8"),
        )

    async def download_files(
        self,
        receipt_id: str,
        filenames: list = None,
        workers: int = 5,
        verify: bool = True,
        on_progress=None,
    ) -> BatchDownloadResult:
        """Download multiple files with full concurrency."""
        import hashlib as _hashlib

        t0 = time.time()

        manifest = await self.get_manifest(receipt_id)
        manifest_files = manifest.get("files", [])
        hash_lookup = {
            f.get("filename", ""): f.get("content_hash", "")
            for f in manifest_files
        }

        if filenames is None:
            filenames = list(hash_lookup.keys())

        batch_response = await self.get_batch_urls(receipt_id, filenames)

        url_map = {}
        for entry in batch_response.get("urls", []):
            url_map[entry["filename"]] = entry["download_url"]

        errors = []
        for fn in batch_response.get("not_found", []):
            errors.append((fn, "File not found in locker"))

        downloaded_count = 0
        files_out = []

        async def _download(fn: str):
            try:
                data = await self.download_bytes(url_map[fn])
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
        sem = asyncio.Semaphore(workers)

        async def _limited(fn: str, index: int):
            async with sem:
                result = await _download(fn)
                if on_progress:
                    try:
                        on_progress(result[0], result[1], index, len(targets))
                    except Exception:
                        pass
                return result

        gather_results = await asyncio.gather(
            *[_limited(fn, i + 1) for i, fn in enumerate(targets)],
            return_exceptions=True,
        )

        for r in gather_results:
            if isinstance(r, Exception):
                errors.append(("unknown", str(r)))
            else:
                fn, success, error, df = r
                if success:
                    downloaded_count += 1
                    files_out.append(df)
                else:
                    errors.append((fn, error))

        elapsed = time.time() - t0

        return BatchDownloadResult(
            downloaded=downloaded_count,
            failed=len(errors),
            total=len(filenames),
            elapsed_sec=round(elapsed, 2),
            errors=errors,
            files=files_out,
        )

    # ------------------------------------------------------------------
    # WALLET
    # ------------------------------------------------------------------

    async def get_wallet_info(self) -> WalletInfo:
        """Get wallet information for current keypair (wraps sync in thread)."""
        self._require_keypair("get_wallet_info")

        if self._payment is None:
            PaymentClass = _get_solana_payment()
            self._payment = PaymentClass(
                keypair_path=str(self.keypair.keypair_path),
                network=self.network,
                rpc_url=self.rpc_url,
            )

        balance = await asyncio.to_thread(self._payment.get_balance)

        return WalletInfo(
            pubkey=self._payment.pubkey,
            balance_sol=balance,
            network=self.network,
        )

    # ------------------------------------------------------------------
    # VIEWER PORTAL HANDOFF (sync computation, some methods call list_files)
    # ------------------------------------------------------------------

    def get_viewer_container_url(
        self,
        viewer_base_url: str = "https://nukez.xyz",
        request_type: str = "container",
        receipt_id: Optional[str] = None,
        locker_id: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> str:
        """Build a generic viewer container URL (pure computation, sync)."""
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
        """Build a generic viewer-container handoff payload (pure computation, sync)."""
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

    def get_owner_viewer_url(
        self,
        receipt_id: str,
        viewer_base_url: str = "https://nukez.xyz",
    ) -> ViewerLink:
        """Build a stable owner portal URL (pure computation, sync)."""
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
        """Build a file-scoped viewer URL (pure computation, sync)."""
        locker_id = compute_locker_id(receipt_id)
        base = self._normalize_viewer_base_url(viewer_base_url)

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
            expires_in_sec=None,
            includes_download_url=True,
        )

    async def list_files_with_viewer_urls(
        self,
        receipt_id: str,
        viewer_base_url: str = "https://nukez.xyz",
        include_download_urls: bool = False,
        ttl_min: int = 30,
    ) -> ViewerFileList:
        """List locker files and enrich each with a human viewer URL."""
        owner_link = self.get_owner_viewer_url(
            receipt_id=receipt_id,
            viewer_base_url=viewer_base_url,
        )
        files = await self.list_files(receipt_id=receipt_id)

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
        """Build MCP-friendly owner viewer payload (sync, no I/O)."""
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
        """Return the stable MCP renderer contract descriptor."""
        return self._viewer_renderer_contract()

    def get_viewer_container_contract(self) -> Dict[str, str]:
        """Return the stable viewer-container contract descriptor."""
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
        """Build MCP-friendly file viewer payload (sync, no I/O)."""
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

    async def list_files_with_viewer_handoffs(
        self,
        receipt_id: str,
        viewer_base_url: str = "https://nukez.xyz",
        include_download_urls: bool = False,
        ttl_min: int = 30,
    ) -> Dict[str, Any]:
        """Build MCP-friendly owner + file viewer payloads."""
        bundle = await self.list_files_with_viewer_urls(
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

    async def get_locker_view_container(
        self,
        receipt_id: str,
        viewer_base_url: str = "https://nukez.xyz",
        include_download_urls: bool = False,
        ttl_min: int = 30,
        embed_payload_in_url: bool = True,
        button_label: str = "Open Locker Viewer",
    ) -> ViewerContainer:
        """Build a locker view payload: table + stats + links."""
        from .client import Nukez  # for static block builders

        bundle = await self.list_files_with_viewer_urls(
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
            Nukez.make_header_block(
                title="Locker Contents",
                subtitle=bundle.locker_id,
                description="Canonical manifest view of files stored through Nukez protocol flows.",
            ),
            Nukez.make_stats_block(
                [
                    {"label": "Locker ID", "value": bundle.locker_id},
                    {"label": "Receipt ID", "value": bundle.receipt_id},
                    {"label": "File Count", "value": len(bundle.files)},
                ],
                title="Locker Stats",
            ),
            Nukez.make_links_block(
                [{"label": "Open Owner Portal", "href": bundle.owner_viewer_url}],
                title="Locker Links",
            ),
            Nukez.make_table_block(
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

    async def get_attestation_view_container(
        self,
        receipt_id: str,
        viewer_base_url: str = "https://nukez.xyz",
        embed_payload_in_url: bool = True,
        button_label: str = "Open Attestation Viewer",
    ) -> ViewerContainer:
        """Build an attestation view payload: kv + status + proofs + json."""
        from .client import Nukez

        verification = await self.verify_storage(receipt_id)
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
            Nukez.make_header_block(
                title="Attestation View",
                subtitle=locker_id,
                description="Verification state and cryptographic proof data.",
            ),
            Nukez.make_status_block(
                status=verified_status,
                label="Verification Status",
                detail=status_detail,
            ),
            Nukez.make_kv_block(kv_items, title="Attestation Summary"),
        ]
        if proofs:
            blocks.append(Nukez.make_proofs_block(proofs, title="Proof Material"))
        blocks.append(Nukez.make_json_block(raw_json, title="Raw Verification JSON"))

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

    async def get_file_view_container(
        self,
        receipt_id: str,
        filename: str,
        viewer_base_url: str = "https://nukez.xyz",
        ttl_min: int = 30,
        include_download_url: bool = True,
        embed_payload_in_url: bool = True,
        button_label: str = "Open File Viewer",
    ) -> ViewerContainer:
        """Build a file view payload: file_meta + file_preview."""
        from .client import Nukez

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
        for f in await self.list_files(receipt_id=receipt_id):
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
            Nukez.make_header_block(
                title="File View",
                subtitle=filename,
                description="Single-file viewer with automatic preview mode selection.",
            ),
            Nukez.make_file_meta_block(
                filename=filename,
                content_type=content_type,
                updated_at=updated_at,
                extra={
                    "Locker ID": locker_id,
                    "Receipt ID": receipt_id,
                },
            ),
            Nukez.make_links_block(links, title="File Links"),
            Nukez.make_file_preview_block(
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
