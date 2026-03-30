# PyNukez SDK Reference

**Agent-native storage with cryptographic verification.**

Pay with SOL or MON. Store anything. Get a cryptographic receipt. Verify independently.

- **Version:** 3.2.0
- **License:** MIT
- **Python:** >= 3.9
- **PyPI:** `pip install pynukez`

---

## Table of Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
- [Async Client](#async-client)
- [Client Initialization](#client-initialization)
- [Payment Flow (x402 Protocol)](#payment-flow-x402-protocol)
- [Storage & File Operations](#storage--file-operations)
- [Verification & Attestation](#verification--attestation)
- [Operator Delegation](#operator-delegation)
- [Viewer Portal](#viewer-portal)
- [Authentication & Signing](#authentication--signing)
- [Discovery & Utilities](#discovery--utilities)
- [Error Handling](#error-handling)
- [Data Types Reference](#data-types-reference)
- [Storage Providers](#storage-providers)
- [Agent Integration](#agent-integration)
- [Environment Variables](#environment-variables)

---

## Installation

```bash
# Core SDK (signing + file operations)
pip install pynukez

# With Solana payment support
pip install pynukez[solana]

# With EVM/Monad payment support
pip install pynukez[evm]

# Everything
pip install pynukez[all]
```

Requires Python 3.9+. Supported on macOS, Linux, and Windows.

### Dependencies

| Extra | Packages |
|-------|----------|
| **Core** | `httpx>=0.24.0`, `pynacl>=1.5.0`, `base58>=2.1.0` |
| **solana** | `solana>=0.30.0`, `solders>=0.18.0` |
| **evm** | `web3>=6.0.0`, `eth-account>=0.10.0` |
| **dev** | `pytest`, `pytest-asyncio`, `pytest-mock`, `black`, `isort`, `mypy` |

---

## Quick Start

```python
from pynukez import Nukez

client = Nukez(keypair_path="~/.config/solana/id.json")

# 1. Check pricing
price = client.get_price()

# 2. Request storage (returns payment instructions)
request = client.request_storage(units=1)

# 3. Execute Solana payment
transfer = client.solana_transfer(request.pay_to_address, request.amount_sol)

# 4. Confirm payment and get receipt
receipt = client.confirm_storage(request.pay_req_id, transfer.signature)
# SAVE receipt.id - you need it for everything!

# 5. Provision a locker (one-time per receipt)
manifest = client.provision_locker(receipt.id)

# 6. Create a file and get signed URLs
urls = client.create_file(receipt.id, "notes.txt")

# 7. Upload data
client.upload_bytes(urls.upload_url, b"Hello from an agent!")

# 8. Download data
data = client.download_bytes(urls.download_url)
```

### First Time Setup

```bash
# Install Solana CLI
sh -c "$(curl -sSfL https://release.solana.com/stable/install)"

# Create wallet
solana-keygen new --outfile ~/.config/solana/id.json

# Get free test SOL
solana config set --url devnet
solana airdrop 2
```

---

## Async Client

`AsyncNukez` provides full method parity with `Nukez`. Same method names, same parameters, same return types. The only difference is `async/await` and `httpx.AsyncClient` under the hood.

```python
from pynukez import AsyncNukez

async with AsyncNukez(keypair_path="~/.config/solana/id.json") as client:
    price = await client.get_price()
    request = await client.request_storage(units=1)
    transfer = await client.solana_transfer(request.pay_to_address, request.amount_sol)
    receipt = await client.confirm_storage(request.pay_req_id, transfer.signature)
    files = await client.list_files(receipt.id)
```

Use `AsyncNukez` for FastAPI, MCP servers, and event-loop contexts.

---

## Client Initialization

```python
from pynukez import Nukez

# Solana-only
client = Nukez(
    keypair_path="~/.config/solana/id.json",
    network="devnet",                           # or "mainnet-beta"
    base_url="https://api.nukez.xyz",           # default
    rpc_url="https://api.devnet.solana.com",    # default
)

# EVM-only (Monad)
client = Nukez(
    evm_private_key_path="~/.keys/evm_key.json",
    network="devnet",
)

# Dual-key (Solana signing + EVM payment)
client = Nukez(
    keypair_path="~/.config/solana/id.json",
    evm_private_key_path="~/.keys/evm_key.json",
)

# Custom signer (relay, HSM, etc.)
client = Nukez(signing_key=my_custom_signer)
```

### Constructor Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `keypair_path` | `str \| Path` | `None` | Path to Solana keypair JSON |
| `base_url` | `str` | `NUKEZ_BASE_URL` env or `https://api.nukez.xyz` | Gateway API URL |
| `network` | `str` | `"devnet"` | Network target |
| `rpc_url` | `str` | `"https://api.devnet.solana.com"` | Solana RPC endpoint |
| `timeout` | `int` | `None` | HTTP request timeout (seconds) |
| `evm_private_key_path` | `str \| Path` | `None` | Path to EVM private key (JSON or hex) |
| `evm_rpc_url` | `str` | `None` | EVM RPC endpoint |
| `signing_key` | `Signer` | `None` | Explicit Signer instance (overrides keypair) |

Both clients support context managers:

```python
with Nukez(keypair_path="~/.config/solana/id.json") as client:
    ...

# or
client = Nukez(keypair_path="~/.config/solana/id.json")
try:
    ...
finally:
    client.close()
```

---

## Payment Flow (x402 Protocol)

Nukez uses the x402 HTTP 402 Payment Required protocol. The flow is always three explicit steps:

1. **Request** -- `request_storage()` returns payment instructions (address, amount, `pay_req_id`)
2. **Pay** -- `solana_transfer()` or `evm_transfer()` executes the on-chain payment
3. **Confirm** -- `confirm_storage()` presents `pay_req_id` + tx signature, gateway verifies on-chain, returns receipt

The receipt is the root credential. All subsequent operations require the `receipt_id`.

### Methods

#### `request_storage(units=1, provider=None, pay_network=None, pay_asset=None)` -> `StorageRequest`

Start the payment flow. Returns payment instructions including all available chain/asset options.

```python
request = client.request_storage(units=1, provider="gcs")

request.pay_req_id       # Save for confirm_storage()
request.pay_to_address   # Send payment here
request.amount_sol       # SOL amount (Solana)
request.amount_raw       # Atomic units (any chain)
request.payment_options  # All available chain/asset combos
request.is_evm           # True if default option is EVM
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `units` | `int` | `1` | Number of storage units |
| `provider` | `str` | `None` | Storage backend: `"gcs"`, `"mongodb"`, `"storj"` |
| `pay_network` | `str` | `None` | Payment chain: `"solana-devnet"`, `"monad-testnet"` |
| `pay_asset` | `str` | `None` | Payment token: `"SOL"`, `"MON"`, `"USDC"`, `"USDT"`, `"WETH"` |

#### `solana_transfer(to_address, amount_sol)` -> `TransferResult`

Execute a Solana SOL payment.

```python
transfer = client.solana_transfer(request.pay_to_address, request.amount_sol)
transfer.signature  # Base58 tx signature
```

#### `evm_transfer(to_address, amount_raw, pay_asset="MON", token_address=None, network="monad-testnet")` -> `TransferResult`

Execute an EVM payment (MON, WETH, USDC, etc.).

```python
transfer = client.evm_transfer(
    to_address=request.pay_to_address,
    amount_raw=request.amount_raw,
    pay_asset=request.pay_asset,
    token_address=request.token_address,
    network=request.network,
)
transfer.signature  # 0x-prefixed tx hash
```

#### `confirm_storage(pay_req_id, tx_sig, ...)` -> `Receipt`

Confirm payment and get your receipt.

```python
receipt = client.confirm_storage(request.pay_req_id, transfer.signature)
receipt.id          # YOUR KEY TO EVERYTHING - save this!
receipt.locker_id   # Derived automatically
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `pay_req_id` | `str` | required | Payment request ID from `request_storage()` |
| `tx_sig` | `str` | required | Transaction signature from `solana_transfer()` / `evm_transfer()` |
| `max_retries` | `int` | `5` | Retry count for blockchain confirmation |
| `initial_delay` | `float` | `2.0` | Initial retry delay in seconds |
| `payment_chain` | `str` | `None` | Chain ID (required for EVM) |
| `payment_asset` | `str` | `None` | Asset symbol (required for EVM) |
| `operator_pubkey` | `str` | `None` | Authorize operator at purchase time |

### Multi-Chain Payment Options

| Chain | Token | Signature Algorithm |
|-------|-------|---------------------|
| Solana | SOL | Ed25519 |
| Solana | USDC | Ed25519 |
| Solana | USDT | Ed25519 |
| Monad (EVM) | MON | secp256k1 |
| Monad (EVM) | WETH | secp256k1 |

### Choosing a Payment Path

```python
request = client.request_storage(units=1)

if request.is_evm:
    transfer = client.evm_transfer(
        request.pay_to_address, request.amount_raw,
        pay_asset=request.pay_asset,
        token_address=request.token_address,
        network=request.network,
    )
    receipt = client.confirm_storage(
        request.pay_req_id, transfer.signature,
        payment_chain=request.network,
        payment_asset=request.pay_asset,
    )
else:
    transfer = client.solana_transfer(request.pay_to_address, request.amount_sol)
    receipt = client.confirm_storage(request.pay_req_id, transfer.signature)
```

---

## Storage & File Operations

All file operations require a `receipt_id` from the payment flow.

### Locker Management

| Method | Returns | Description |
|--------|---------|-------------|
| `provision_locker(receipt_id, tags=None, operator_pubkey=None)` | `NukezManifest` | Create storage namespace (one-time per receipt) |
| `get_manifest(receipt_id)` | `dict` | Get locker manifest with all file metadata |
| `list_files(receipt_id)` | `List[FileInfo]` | List all files with metadata (size, hash, timestamps) |

```python
manifest = client.provision_locker(receipt.id, tags=["myapp"])
files = client.list_files(receipt.id)
for f in files:
    print(f.filename, f.size_bytes, f.content_hash)
```

### File URL Management

| Method | Returns | Description |
|--------|---------|-------------|
| `create_file(receipt_id, filename, content_type="application/octet-stream", ttl_min=30)` | `FileUrls` | Create file entry and get signed upload/download URLs |
| `create_files_batch(receipt_id, files, ttl_min=30)` | `dict` | Create multiple file URL pairs in one signed request |
| `get_file_urls(receipt_id, filename, ttl_min=30)` | `FileUrls` | Refresh expired signed URLs for existing file |
| `get_batch_urls(receipt_id, filenames, ttl_min=30)` | `dict` | Refresh URLs for multiple files |
| `delete_file(receipt_id, filename)` | `DeleteResult` | Delete a file permanently |

```python
urls = client.create_file(receipt.id, "data.json", content_type="application/json")
urls.upload_url     # PUT your data here
urls.download_url   # GET your data here
urls.expires_in_sec # URL lifetime
```

### Upload Methods

| Method | Best For | Description |
|--------|----------|-------------|
| `upload_bytes(upload_url, data, content_type=None)` | Small in-memory data | Upload raw bytes to signed URL |
| `upload_string(upload_url, content, content_type="text/plain")` | Agent-generated text | Upload string with auto-sanitization |
| `upload_file_path(receipt_id, filepath, filename=None, content_type=None, ttl_min=30, confirm=True)` | Single local file | Upload by filesystem path (avoids context bloat) |
| `bulk_upload_paths(receipt_id, sources, workers=6, ttl_min=30, confirm=True, auto_attest=False, attest_sync=False, on_progress=None)` | Multiple local files | Parallel upload with batch confirm |
| `upload_directory(receipt_id, source_dir, pattern="*", recursive=False, exclude_pattern=None, preserve_structure=False, workers=6, ...)` | Directories | Glob-filtered directory upload |
| `upload_files(receipt_id, sources, workers=6, ttl_min=30, confirm=True)` | General batch | Concurrent upload from source list |

```python
# Direct bytes
client.upload_bytes(urls.upload_url, b"Hello!")

# From filesystem (preferred for large files)
result = client.upload_file_path(receipt.id, "/path/to/report.pdf")

# Batch upload
result = client.bulk_upload_paths(
    receipt.id,
    sources=[
        {"filepath": "/path/to/doc.html"},
        {"filepath": "/path/to/data.json"},
    ],
    workers=6,
    confirm=True,
)
print(result["uploaded"], result["failed"])

# Upload a whole directory
result = client.upload_directory(
    receipt.id,
    source_dir="/path/to/reports",
    pattern="*.pdf",
    recursive=True,
)
```

### Background Upload Jobs

For long-running uploads that shouldn't block chat turns:

| Method | Returns | Description |
|--------|---------|-------------|
| `start_bulk_upload_job(receipt_id, sources, workers=6, ...)` | `dict` | Start non-blocking background upload; returns `job_id` |
| `get_upload_job(job_id)` | `dict` | Poll job status |
| `list_upload_jobs(limit=20)` | `dict` | List all upload jobs |

```python
job = client.start_bulk_upload_job(receipt.id, sources=[...])
print(job["job_id"], job["status"])  # queued

# Later...
status = client.get_upload_job(job["job_id"])
print(status["status"], status.get("uploaded"))
```

### Download Methods

| Method | Returns | Description |
|--------|---------|-------------|
| `download_bytes(download_url, max_retries=3, initial_delay=2.0)` | `bytes` | Download file content (retries on 404 for propagation) |
| `download_files(receipt_id, filenames, workers=6)` | `BatchDownloadResult` | Concurrent download of multiple files |

```python
data = client.download_bytes(urls.download_url)

batch = client.download_files(receipt.id, ["a.txt", "b.txt"])
for f in batch.files:
    print(f.filename, f.size_bytes, f.verified)
```

### Sandbox Ingest

For proxied runtimes where signed-URL uploads are blocked (e.g., `/mnt/data` restrictions):

| Method | Returns | Description |
|--------|---------|-------------|
| `sandbox_create_ingest_job(receipt_id, files, execution_mode=None)` | `dict` | Create chunked ingest job |
| `sandbox_append_ingest_part(receipt_id, job_id, part_number, data)` | `dict` | Append base64 chunk |
| `sandbox_complete_ingest_job(receipt_id, job_id)` | `dict` | Finalize and commit files |
| `sandbox_upload_bytes(receipt_id, filename, data, ...)` | `dict` | Convenience: bytes -> chunked ingest |
| `sandbox_upload_base64(receipt_id, filename, base64_data, ...)` | `dict` | Convenience: base64 -> chunked ingest |
| `sandbox_upload_file_path(receipt_id, filepath, filename=None, ...)` | `dict` | Convenience: local file -> chunked ingest |

```python
# Full control
job = client.sandbox_create_ingest_job(
    receipt_id=receipt.id,
    files=[{"filename": "image.png", "content_type": "image/png"}],
)
client.sandbox_append_ingest_part(
    receipt_id=receipt.id,
    job_id=job["job_id"],
    part_number=0,
    data=chunk_bytes,
)
result = client.sandbox_complete_ingest_job(receipt.id, job["job_id"])

# Or use convenience helpers
result = client.sandbox_upload_bytes(receipt.id, "file.txt", b"data")
result = client.sandbox_upload_file_path(receipt.id, "/path/to/file.pdf")
```

---

## Verification & Attestation

After uploading, confirm file hashes and generate a merkle attestation.

| Method | Returns | Description |
|--------|---------|-------------|
| `confirm_file(receipt_id, filename)` | `ConfirmResult` | Compute and record content hash for one file |
| `confirm_files(receipt_id, filenames)` | `BatchConfirmResult` | Batch hash confirmation |
| `attest(receipt_id, sync=True)` | `AttestResult` | Compute merkle root and trigger attestation |
| `verify_storage(receipt_id)` | `VerificationResult` | Full verification: merkle root, manifest signature, per-file hashes |
| `get_merkle_proof(receipt_id, filename)` | `dict` | Get inclusion proof for a specific file |
| `compute_hash(data)` | `str` | SHA-256 hash for local verification |

```python
# Confirm individual file hashes
result = client.confirm_file(receipt.id, "notes.txt")
print(result.content_hash)  # "sha256:..."

# Batch confirm
batch = client.confirm_files(receipt.id, ["a.txt", "b.txt"])
print(batch.confirmed_count)

# Generate merkle attestation
att = client.attest(receipt.id, sync=True)
print(att.merkle_root, att.file_count)

# Full verification
v = client.verify_storage(receipt.id)
print(v.verified, v.merkle_root, v.file_count)

# Per-file merkle proof
proof = client.get_merkle_proof(receipt.id, "notes.txt")
```

### Agent-to-Agent Verification

Agent A stores data and shares the `receipt_id` with Agent B. Agent B verifies independently:

```python
# Agent B verifies Agent A's work
verification = client.verify_storage(agent_a_receipt_id)
assert verification.verified == True
```

No shared keys, no shared accounts. The receipt ID is the trust bridge.

---

## Operator Delegation

Owners can delegate file operations to other signers (operators) without sharing keys.

| Method | Returns | Description |
|--------|---------|-------------|
| `add_operator(receipt_id, operator_pubkey)` | `OperatorResult` | Authorize an operator (owner-only) |
| `remove_operator(receipt_id, operator_pubkey)` | `OperatorResult` | Revoke an operator (owner-only) |
| `set_owner(receipt_id, identity=None)` | `None` | Cache the owner identity for delegation logic |

```python
# Owner authorizes an operator
result = client.add_operator(receipt.id, "operator_pubkey_base58")
print(result.ok, result.operator_ids)

# Operator performs file ops with their own signer
operator_client = Nukez(keypair_path="/path/to/operator_key.json")
urls = operator_client.create_file(receipt.id, "delegated.txt")

# Owner revokes
client.remove_operator(receipt.id, "operator_pubkey_base58")
```

### Operator Error Classes

| Error | HTTP | Description |
|-------|------|-------------|
| `OperatorError` | -- | Base class for all operator errors |
| `InvalidOperatorPubkeyError` | 400 | Bad pubkey format |
| `OperatorIsOwnerError` | 400 | Cannot delegate to self |
| `OperatorNotAuthorizedError` | 403 | Signer not in operator list |
| `OwnerOnlyError` | 403 | Action requires owner (not operator) |
| `OperatorNotFoundError` | 404 | Removing non-existent operator |
| `OperatorConflictError` | 409 | Duplicate add or max operators reached |

---

## Viewer Portal

The viewer portal provides agent-to-human handoff via structured container payloads. Agents build typed view payloads that render in the Nukez viewer UI.

### Container System

| Method | Returns | Description |
|--------|---------|-------------|
| `get_viewer_container_handoff(viewer_base_url, request_type, view_kind, receipt_id, locker_id, filename, blocks, renderables, embed_payload_in_url, button_label)` | `ViewerContainer` | Build generic viewer container payload |
| `get_locker_view_container(receipt_id, viewer_base_url, include_download_urls, ttl_min, embed_payload_in_url, button_label)` | `ViewerContainer` | Locker view: table + stats + links |
| `get_attestation_view_container(receipt_id, viewer_base_url, embed_payload_in_url, button_label)` | `ViewerContainer` | Attestation view: kv + status + proofs + json |
| `get_file_view_container(receipt_id, filename, viewer_base_url, ttl_min, include_download_url, embed_payload_in_url, button_label)` | `ViewerContainer` | File view: file_meta + file_preview |
| `get_viewer_container_url(viewer_base_url, request_type, receipt_id, locker_id, filename)` | `str` | Raw container URL builder |

```python
# Locker overview
container = client.get_locker_view_container(receipt.id)
print(container.viewer_url)

# Attestation proof viewer
container = client.get_attestation_view_container(receipt.id)

# Single file viewer
container = client.get_file_view_container(receipt.id, "report.pdf")
```

### URL Builders

| Method | Returns | Description |
|--------|---------|-------------|
| `get_owner_viewer_url(receipt_id, viewer_base_url)` | `ViewerLink` | Stable locker portal URL |
| `get_file_viewer_url(receipt_id, filename, viewer_base_url, ttl_min, include_download_url)` | `ViewerLink` | File-scoped viewer URL (optionally embeds download URL) |
| `list_files_with_viewer_urls(receipt_id, viewer_base_url, include_download_urls, ttl_min)` | `ViewerFileList` | Files enriched with viewer URLs |

```python
# Owner portal link
link = client.get_owner_viewer_url(receipt.id)
print(link.url)

# File viewer with embedded download
link = client.get_file_viewer_url(receipt.id, "data.json")

# All files with viewer URLs
bundle = client.list_files_with_viewer_urls(receipt.id)
for f in bundle.files:
    print(f.filename, f.viewer_url)
```

### MCP Handoff Helpers

For MCP renderers and UI button integration:

| Method | Returns | Description |
|--------|---------|-------------|
| `get_owner_viewer_handoff(receipt_id)` | `dict` | Owner portal with `ui` button payload |
| `get_file_viewer_handoff(receipt_id, filename)` | `dict` | File viewer with `ui` button payload |
| `list_files_with_viewer_handoffs(receipt_id)` | `dict` | All files with `ui` button payloads |
| `get_viewer_renderer_contract()` | `dict` | Renderer contract: `nukez.mcp.viewer_link@1.0` |
| `get_viewer_container_contract()` | `dict` | Container contract: `nukez.viewer_container@1.0.0` |

```python
payload = client.get_owner_viewer_handoff(receipt.id)
print(payload["ui"])  # {"kind": "button", "label": "Open Nukez Viewer", ...}

contract = client.get_viewer_renderer_contract()
# {"name": "nukez.mcp.viewer_link", "version": "1.0"}
```

### Renderable Helpers

Build typed content objects for container payloads:

| Method | Description |
|--------|-------------|
| `make_text_renderable(text, title)` | Plain text content card |
| `make_json_renderable(data, title)` | JSON content card |
| `make_pdf_renderable(url, title)` | PDF embed card |
| `make_image_renderable(url, title)` | Image embed card |
| `make_binary_renderable(hex_preview, size_bytes, title)` | Binary preview card |

### Block Builders

Build structured view blocks:

| Method | Description |
|--------|-------------|
| `make_header_block(title, subtitle, description)` | Section header |
| `make_stats_block(items, title)` | Key-value statistics |
| `make_links_block(links, title)` | Link list |
| `make_table_block(columns, rows, title)` | Data table |
| `make_kv_block(items, title)` | Key-value pairs |
| `make_status_block(status, label, detail)` | Status indicator |
| `make_proofs_block(proofs, title)` | Cryptographic proof material |
| `make_json_block(data, title)` | Raw JSON display |
| `make_file_meta_block(...)` | File metadata summary |
| `make_file_preview_block(...)` | Automatic file preview |

```python
# Custom container with renderables
container = client.get_viewer_container_handoff(
    request_type="file_view",
    renderables=[
        client.make_text_renderable("Analysis complete", title="Agent Note"),
        client.make_json_renderable({"score": 0.98}, title="Results"),
    ],
)

# Structured container with blocks
container = client.get_viewer_container_handoff(
    view_kind="locker",
    request_type="locker_view",
    blocks=[
        client.make_header_block("Locker Contents", subtitle="locker_abc"),
        client.make_stats_block([{"label": "Files", "value": 12}]),
        client.make_table_block(
            columns=[{"key": "filename", "label": "Filename"}],
            rows=[{"filename": "report.pdf"}],
        ),
    ],
)
```

---

## Authentication & Signing

Every mutating request to the gateway requires a signed envelope. PyNukez handles this automatically -- you do not construct envelopes manually unless building a custom integration.

### Signer Protocol

```python
from pynukez import Signer

class Signer(Protocol):
    @property
    def identity(self) -> str:    # Base58 (Ed25519) or 0x address (EVM)
        ...
    @property
    def sig_alg(self) -> str:     # "ed25519" or "secp256k1"
        ...
    def sign(self, message: bytes) -> str:  # Must be synchronous
        ...
```

### Built-in Signers

| Class | Algorithm | Identity Format | Use Case |
|-------|-----------|-----------------|----------|
| `Keypair` | Ed25519 | Base58 pubkey | Solana wallets |
| `EVMSigner` | secp256k1 | 0x address | EVM/Monad wallets |

```python
from pynukez import Keypair, EVMSigner

# Solana keypair from CLI format JSON
kp = Keypair("~/.config/solana/id.json")
print(kp.pubkey_b58, kp.sig_alg)  # "ABC..." "ed25519"

# EVM signer from private key file
signer = EVMSigner.from_file("~/.keys/evm_key.json")
print(signer.identity, signer.sig_alg)  # "0xabc..." "secp256k1"
```

### Envelope Functions

| Function | Returns | Description |
|----------|---------|-------------|
| `build_signed_envelope(signer, receipt_id, method, path, ops, body, ttl_seconds=300, delegating=False)` | `SignedEnvelope` | Create signed auth headers |
| `build_unsigned_envelope(signer_identity, sig_alg, receipt_id, method, path, ops, body, ttl_seconds=300, delegating=False)` | `UnsignedEnvelope` | For relay/external signing |
| `attach_signature(unsigned, signature)` | `SignedEnvelope` | Attach signature to unsigned envelope |
| `compute_locker_id(receipt_id)` | `str` | Deterministic locker ID: `"locker_" + sha256(receipt_id)[:12]` |

```python
from pynukez import build_unsigned_envelope, attach_signature

# Relay signing flow
unsigned = build_unsigned_envelope(
    signer_identity="ABC...",
    sig_alg="ed25519",
    receipt_id="rec_123",
    method="POST",
    path="/v1/files",
)

# Sign externally (HSM, remote signer, etc.)
signature = external_signer.sign(unsigned.envelope_json.encode())

# Attach and use
signed = attach_signature(unsigned, signature)
# signed.headers -> {"X-Nukez-Envelope": "...", "X-Nukez-Signature": "..."}
```

### Envelope Contents

The signed envelope includes:
- Request method and path
- `body_sha256` hash for POST requests
- Timestamp with TTL (default 5 minutes, prevents replay)
- Signer's public key and algorithm
- Nonce for uniqueness
- Locker ID (derived from receipt ID)

---

## Discovery & Utilities

### Module-Level Functions

| Function | Returns | Description |
|----------|---------|-------------|
| `discover(base_url, timeout=10.0)` | `DiscoveryDoc` | Get API capabilities from `/.well-known/nukez.json` |
| `health_check(base_url, timeout=5.0)` | `dict` | Verify API availability |
| `get_current_price(base_url, units=1)` | `PriceInfo` | Check current storage pricing |

```python
from pynukez import discover, health_check, get_current_price

doc = discover()
print(doc.api_version, doc.features)

health = health_check()
price = get_current_price(units=5)
```

### Client Utility Methods

| Method | Returns | Description |
|--------|---------|-------------|
| `get_price(units=1)` | `PriceInfo` | Current storage pricing |
| `get_provider_info(provider="gcs")` | `ProviderInfo` | Provider capabilities and limits |
| `get_wallet_info()` | `WalletInfo` | Wallet balance and address |
| `sign_message(message)` | `str` | Sign arbitrary message string |

```python
price = client.get_price(units=1)
print(f"Cost: {price.amount_sol} SOL (${price.total_usd})")

wallet = client.get_wallet_info()
print(f"Balance: {wallet.balance_sol} SOL")

info = client.get_provider_info("mongodb")
print(info.supports_signed_urls, info.max_object_size)
```

---

## Error Handling

All errors inherit from `NukezError`. Each has a `retryable` attribute indicating whether the operation can be retried.

| Error Class | HTTP | Retryable | Recovery |
|-------------|------|-----------|----------|
| `NukezError` | -- | No | Base class |
| `PaymentRequiredError` | 402 | No | Contains payment instructions -- this is expected from `request_storage()` |
| `TransactionNotFoundError` | -- | Yes | Wait `e.suggested_delay` seconds and retry `confirm_storage()` |
| `AuthenticationError` | 401/403 | No | Check keypair matches the one used to pay |
| `NukezFileNotFoundError` | 404 | No | Check `list_files()` or `create_file()` first |
| `URLExpiredError` | 403/410 | Yes | Call `get_file_urls()` to refresh signed URLs |
| `NukezNotProvisionedError` | 412 | No | Call `provision_locker()` first |
| `InsufficientFundsError` | -- | No | Add SOL/MON to wallet |
| `RateLimitError` | 429 | Yes | Wait `e.retry_after` seconds |

```python
from pynukez.errors import NukezError, URLExpiredError, TransactionNotFoundError

try:
    data = client.download_bytes(urls.download_url)
except URLExpiredError:
    urls = client.get_file_urls(receipt_id, filename)
    data = client.download_bytes(urls.download_url)
except NukezError as e:
    if e.retryable:
        time.sleep(2)
        # retry...
    else:
        raise
```

### PaymentRequiredError Fields

`PaymentRequiredError` is not a failure -- it's the expected response from `request_storage()` containing:

| Field | Type | Description |
|-------|------|-------------|
| `pay_req_id` | `str` | Payment request ID |
| `pay_to_address` | `str` | Destination address |
| `amount_sol` | `float` | SOL amount |
| `amount_lamports` | `int` | Lamports (Solana atomic) |
| `amount_raw` | `int` | Atomic units (any chain) |
| `pay_asset` | `str` | Token symbol |
| `network` | `str` | Chain network |
| `payment_options` | `list` | All available chain/asset options |
| `quote_expires_at` | `int` | Unix timestamp |
| `terms` | `dict` | Storage limits, TTL, file limits |

---

## Data Types Reference

All types are plain Python `@dataclass` objects.

### Payment Types

**`StorageRequest`**
| Field | Type | Description |
|-------|------|-------------|
| `pay_req_id` | `str` | Payment request ID |
| `pay_to_address` | `str` | Destination address |
| `amount_sol` | `float` | SOL amount |
| `amount_lamports` | `int` | Lamports |
| `network` | `str` | Network |
| `units` | `int` | Storage units |
| `provider` | `str` | Storage backend (default `"gcs"`) |
| `pay_asset` | `str` | Token symbol (default `"SOL"`) |
| `amount` | `str?` | Human-readable amount |
| `amount_raw` | `int?` | Atomic units |
| `token_address` | `str?` | ERC-20 contract (EVM) |
| `payment_options` | `list?` | All chain/asset combos |
| `quote_expires_at` | `int?` | Quote expiry timestamp |
| `terms` | `dict?` | Storage terms |
| `is_evm` | property | `True` if EVM payment |
| `parsed_options` | property | List of `PaymentOption` |

**`Receipt`**
| Field | Type | Description |
|-------|------|-------------|
| `id` | `str` | Receipt ID (primary key for all operations) |
| `units` | `int` | Storage units purchased |
| `payer_pubkey` | `str` | Payer's public key |
| `network` | `str` | Network |
| `provider` | `str` | Storage backend |
| `pay_asset` | `str` | Payment token |
| `tx_hash` | `str` | Transaction hash |
| `sig_alg` | `str` | Signature algorithm used |
| `locker_id` | property | Derived locker ID |

**`TransferResult`**
| Field | Type | Description |
|-------|------|-------------|
| `signature` | `str` | Transaction signature |
| `to_address` | `str` | Destination |
| `amount_sol` | `float` | Amount |
| `network` | `str` | Network |
| `chain` | `str` | `"solana"` or `"monad"` |
| `pay_asset` | `str` | Token symbol |

**`PriceInfo`**
| Field | Type | Description |
|-------|------|-------------|
| `units` | `int` | Units priced |
| `unit_price_usd` | `float` | Per-unit USD price |
| `total_usd` | `float` | Total USD |
| `amount_sol` | `float` | SOL equivalent |
| `amount_lamports` | `int` | Lamports |
| `network` | `str` | Network |
| `payment_options` | `list?` | Chain/asset options |

**`PaymentOption`**
| Field | Type | Description |
|-------|------|-------------|
| `chain` | `str` | Chain ID (`"solana-devnet"`, `"monad-mainnet"`) |
| `asset` | `str` | Token symbol |
| `amount` | `str` | Human-readable amount |
| `amount_raw` | `int` | Atomic units |
| `treasury` | `str` | Destination address |
| `decimals` | `int` | Token decimals |
| `token_contract` | `str?` | ERC-20 address |

### File Types

**`NukezManifest`**
| Field | Type | Description |
|-------|------|-------------|
| `locker_id` | `str` | Storage namespace ID |
| `receipt_id` | `str` | Associated receipt |
| `bucket` | `str` | Storage bucket |
| `path_prefix` | `str` | Object path prefix |
| `tags` | `list` | User tags |
| `cap_token` | `str?` | Capability token |

**`FileUrls`**
| Field | Type | Description |
|-------|------|-------------|
| `filename` | `str` | File name |
| `upload_url` | `str` | Signed upload URL (PUT) |
| `download_url` | `str` | Signed download URL (GET) |
| `content_type` | `str` | MIME type |
| `expires_in_sec` | `int` | URL lifetime |

**`FileInfo`**
| Field | Type | Description |
|-------|------|-------------|
| `filename` | `str` | File name |
| `content_type` | `str` | MIME type |
| `size_bytes` | `int` | File size |
| `content_hash` | `str?` | SHA-256 hash |
| `provider_ref` | `str?` | Provider object reference |
| `created_at` | `str?` | Creation timestamp |
| `updated_at` | `str?` | Update timestamp |

**`UploadResult`** -- `upload_url`, `size_bytes`, `content_type`, `uploaded_at`

**`DeleteResult`** -- `filename`, `deleted`, `deleted_at`

**`WalletInfo`** -- `pubkey`, `balance_sol`, `network`

### Verification Types

**`VerificationResult`**
| Field | Type | Description |
|-------|------|-------------|
| `receipt_id` | `str` | Receipt ID |
| `verified` | `bool` | Verification passed |
| `result_hash` | `str` | Verification hash |
| `merkle_root` | `str` | Merkle tree root |
| `manifest_signature` | `str` | Signed manifest |
| `file_count` | `int` | Number of files |
| `files` | `list?` | Per-file hashes and sizes |
| `verify_url` | `str` | Public verification URL |
| `status` | property | `"verified"` or `"unverified"` |
| `attested` | property | True if merkle root present |

**`ConfirmResult`** -- `filename`, `content_hash` (`"sha256:..."`), `size_bytes`, `confirmed`

**`BatchConfirmResult`** -- `results` (list), `confirmed_count`, `failed_count`

**`AttestResult`** -- `receipt_id`, `merkle_root`, `file_count`, `att_code`, `status`, `push_ok`, `tx_signature`

### Batch Types

**`BatchUploadResult`** -- `uploaded`, `failed`, `total`, `elapsed_sec`, `errors`, `results`

**`BatchDownloadResult`** -- `downloaded`, `failed`, `total`, `elapsed_sec`, `errors`, `files`

**`DownloadedFile`** -- `filename`, `content`, `content_hash`, `size_bytes`, `verified`

### Auth Types

**`SignedEnvelope`** -- `headers` (dict with X-Nukez-Envelope, X-Nukez-Signature), `canonical_body`, `locker_id`

**`UnsignedEnvelope`** -- `envelope` (dict), `envelope_json`, `envelope_b64`, `canonical_body`, `locker_id`

### Viewer Types

**`ViewerContainer`** -- `viewer_url`, `result`, `meta`, `contract`, `ui`

**`ViewerLink`** -- `url`, `download_url`

**`ViewerFileList`** -- `receipt_id`, `locker_id`, `owner_viewer_url`, `files`

**`FileViewerInfo`** -- `filename`, `content_type`, `viewer_url`, `download_url`, `created_at`, `updated_at`

### Operator Types

**`OperatorResult`** -- `ok`, `operator_ids`

### Discovery Types

**`DiscoveryDoc`** -- `api_version`, `service`, `description`, `auth_modes`, `endpoints`, `features`, `status`

**`ProviderInfo`** -- `id`, `supports_signed_urls`, `supports_streaming`, `max_object_size`, `immutable`, `content_addressed`

---

## Storage Providers

| Provider | Best For | Signed URLs | Streaming | Immutable |
|----------|----------|-------------|-----------|-----------|
| **GCS** | Archival, large files, proof-of-storage | Yes | Yes | No |
| **MongoDB** | Fast read/write, small context/state data | Yes | Yes | No |
| **Storj** | S3-compatible, decentralized | Yes | Yes | No |
| **Arweave** | Permanent storage | Yes | Yes | Yes |
| **Filecoin** | Content-addressed storage | Yes | Yes | Yes |

Select a provider at storage request time:

```python
request = client.request_storage(units=1, provider="mongodb")
```

Check provider capabilities:

```python
from pynukez import PROVIDERS

for name, info in PROVIDERS.items():
    print(f"{name}: signed_urls={info.supports_signed_urls}, immutable={info.immutable}")
```

---

## Agent Integration

PyNukez is designed for LLM tool-calling patterns. Every method is stateless and returns structured data.

### Tool Definitions

```python
import pynukez

# Get LLM-compatible tool schemas
tools = pynukez.get_tool_definitions()

# Get structured guidance for autonomous agents
instructions = pynukez.get_agent_instructions()
print(instructions["quickstart_flow"])
```

### Design Principles

1. **Explicit parameters** -- No hidden state. Every method gets everything it needs as arguments.
2. **Atomic operations** -- Clear success/failure boundaries. No multi-step operations that can half-succeed.
3. **Dataclass returns** -- Predictable `.field` access. No custom objects.
4. **Agent-friendly errors** -- Error messages tell agents how to fix problems. `retryable` flag enables automated recovery.
5. **Receipt ID as primary key** -- All operations use `receipt_id` for consistent auth.

### Quick Reference (Cheat Sheet)

| What you want | Code |
|---------------|------|
| Buy storage | `request = client.request_storage()` |
| Pay (Solana) | `transfer = client.solana_transfer(request.pay_to_address, request.amount_sol)` |
| Pay (EVM) | `transfer = client.evm_transfer(request.pay_to_address, request.amount_raw, ...)` |
| Get receipt | `receipt = client.confirm_storage(request.pay_req_id, transfer.signature)` |
| Setup locker | `client.provision_locker(receipt.id)` |
| Store bytes | `urls = client.create_file(receipt.id, "f.txt")` then `client.upload_bytes(urls.upload_url, data)` |
| Store file | `client.upload_file_path(receipt.id, "/path/to/file.pdf")` |
| Batch upload | `client.bulk_upload_paths(receipt.id, [{"filepath": "..."}])` |
| Get data | `data = client.download_bytes(urls.download_url)` |
| List files | `files = client.list_files(receipt.id)` |
| Delete file | `client.delete_file(receipt.id, "old.txt")` |
| Verify | `v = client.verify_storage(receipt.id)` |
| Attest | `att = client.attest(receipt.id)` |
| Delegate | `client.add_operator(receipt.id, "pubkey")` |
| Viewer link | `client.get_owner_viewer_url(receipt.id)` |

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `NUKEZ_BASE_URL` | `https://api.nukez.xyz` | Gateway API URL |
| `NUKEZ_NETWORK` | `devnet` | Default network |
| `NUKEZ_KEYPAIR_PATH` | -- | Solana keypair path |
| `NUKEZ_WALLET_PATH` | -- | Alternative keypair path |
| `NUKEZ_RPC_URL` | `https://api.devnet.solana.com` | Solana RPC endpoint |
| `PYNUKEZ_UPLOAD_STRING_MAX_BYTES` | `262144` (256 KB) | Max upload_string size |
| `MONAD_TESTNET_RPC_PRIMARY` | -- | Monad testnet RPC |
| `MONAD_MAINNET_RPC_PRIMARY` | -- | Monad mainnet RPC |

---

## Going to Production

```python
# Devnet (testing)
client = Nukez(keypair_path="~/.config/solana/id.json", network="devnet")

# Mainnet (production)
client = Nukez(keypair_path="~/.config/solana/id.json", network="mainnet-beta")
```

---

## Common Issues

| Problem | Fix |
|---------|-----|
| "Transaction not found" | Wait 3 seconds, retry `confirm_storage()` (auto-retries 5 times) |
| "Insufficient funds" | `solana airdrop 2` (devnet) or fund wallet (mainnet) |
| "URL expired" | `client.get_file_urls(receipt_id, filename)` for fresh URLs |
| "File not found" | Check `client.list_files(receipt_id)` |
| "Locker not provisioned" | Call `client.provision_locker(receipt_id)` first |
| "Authentication error" | Check keypair matches payer. Envelope TTL is 5 minutes. |

---

## License

MIT
