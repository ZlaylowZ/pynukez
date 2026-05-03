# API Reference

## Nukez

The main client. Initialize once, use everywhere.

```python
from pynukez import Nukez

client = Nukez(
    keypair_path="~/.config/solana/id.json",  # Local Ed25519 envelope signer
    network="devnet",                          # or "mainnet-beta"
    base_url="https://api.nukez.xyz"        # Optional
)
```

`keypair_path` is one supported signer source. Use it when local scripts or
agents should sign protected gateway envelopes from a Solana keypair file. For
protected locker/file operations, PyNukez still needs a signer source:
`keypair_path`, `evm_private_key_path`, or an explicit `signing_key`. Nukez
does not custody, receive, or store client keypair material.

---

## Payment Methods

### `request_storage(units=1)`

Start the payment process. Returns x402 payment instructions. Complete the
transfer with your own wallet, CLI, signer, or custody workflow, then submit
the resulting transaction signature to confirm storage.

```python
request = client.request_storage(units=1)

# Returns:
request.pay_req_id      # Save this
request.pay_to_address  # Send payment here
request.amount_sol      # SOL amount
request.amount          # Human-readable amount (any chain)
request.pay_asset       # Token symbol
request.network         # Chain
request.next_step       # Human-readable guidance
```

### Execute the transfer (externally)

pynukez 4.0.0 does not execute on-chain transfers. Use your wallet, CLI,
or any other signer to send the amount to `request.pay_to_address`.
Capture the resulting transaction signature for the next step.

### `confirm_storage(pay_req_id, tx_sig)`

Confirm payment and get your receipt.

```python
receipt = client.confirm_storage(request.pay_req_id, tx_sig=<your_tx_signature>)

# Returns:
receipt.id          # YOUR KEY TO EVERYTHING - save this!
receipt.locker_id   # Derived automatically
```

> ⚠️ May raise `TransactionNotFoundError` if the tx hasn't propagated yet. Wait a few seconds and retry.

---

## Locker Methods

### `provision_locker(receipt_id, tags=[])`

Create your storage space. Call once per receipt.

```python
manifest = client.provision_locker(receipt.id, tags=["myapp"])

# Returns:
manifest.locker_id
manifest.bucket
```

---

## File Methods

### `create_file(receipt_id, filename=None, content_type="application/octet-stream", ttl_min=30)`

Get URLs to upload/download a file.

```python
urls = client.create_file(receipt.id, "data.json", content_type="application/json")

# Returns:
urls.filename      # The actual filename (auto-generated if you passed None)
urls.upload_url    # PUT your data here
urls.download_url  # GET your data here
```

### `upload_bytes(upload_url, data)`

Use for direct bytes when the payload is already local in process memory.
For LLM/MCP workflows, prefer path-based uploads to avoid context bloat for large files.

Upload data to the URL you got from `create_file()`.

```python
client.upload_bytes(urls.upload_url, b"Hello!")
# or
client.upload_bytes(urls.upload_url, "Hello!")  # Strings work too
```

### `upload_file_path(receipt_id, filepath, filename=None, content_type=None, ttl_min=30, confirm=True)`

Upload a local file directly by filesystem path.

This avoids passing file content through the LLM context window.

Preferred for large files and binary assets.

```python
result = client.upload_file_path(
    receipt_id=receipt.id,
    filepath="/mnt/user-data/uploads/report.pdf",
)
print(result["filename"], result["confirmed"])
```

### `bulk_upload_paths(receipt_id, sources, workers=6, ttl_min=30, confirm=True, auto_attest=False, attest_sync=False)`

Upload multiple local files by path with:

Preferred for production/agent workflows over data-in-parameter uploads.
- one batch create call for all signed URLs
- parallel uploads
- optional batch confirm
- optional attestation trigger

```python
result = client.bulk_upload_paths(
    receipt_id=receipt.id,
    sources=[
        {"filepath": "/mnt/user-data/uploads/doc.html"},
        {"filepath": "/mnt/user-data/uploads/guide.md"},
        {"filepath": "/mnt/user-data/uploads/logo.png"},
    ],
    workers=6,
    confirm=True,
)
print(result["uploaded"], result["failed"])
```

### `upload_directory(receipt_id, source_dir, pattern="*", recursive=False, exclude_pattern=None, preserve_structure=False, workers=6, ...)`

Upload files from a directory with glob filtering.

```python
result = client.upload_directory(
    receipt_id=receipt.id,
    source_dir="/mnt/user-data/uploads",
    pattern="*.png",
    recursive=True,
)
print(result["uploaded"], result["total"])
```

### `start_bulk_upload_job(receipt_id, sources, ...)` and `get_upload_job(job_id)`

Start a non-blocking background upload and poll status later.

Use this for long-running uploads so chat turns are not blocked.

```python
job = client.start_bulk_upload_job(
    receipt_id=receipt.id,
    sources=["/mnt/user-data/uploads/a.png", "/mnt/user-data/uploads/b.png"],
)
print(job["job_id"], job["status"])  # queued

status = client.get_upload_job(job["job_id"])
print(status["status"], status.get("uploaded"), status.get("failed"))
```

### `download_bytes(download_url)`

Get your data back.

```python
data = client.download_bytes(urls.download_url)
# Returns: bytes
```

### `list_files(receipt_id)`

See what's in your locker.

```python
files = client.list_files(receipt.id)

for f in files:
    print(f.filename, f.content_type)
```

### `get_file_urls(receipt_id, filename, ttl_min=30)`

Get fresh URLs for an existing file (use when URLs expire).

```python
urls = client.get_file_urls(receipt.id, "data.json")
```

### `get_viewer_container_handoff(viewer_base_url="https://nukez.xyz", request_type="container", view_kind="custom", receipt_id=None, locker_id=None, filename=None, blocks=None, renderables=None, embed_payload_in_url=True, button_label="Open Nukez Viewer")`

Build a generic, render-agnostic viewer container payload for agent-to-human handoff.

```python
container = client.get_viewer_container_handoff()
print(container.viewer_url)
# https://nukez.xyz/viewer?request_type=container
```

You can also fetch the container contract descriptor:

```python
contract = client.get_viewer_container_contract()
print(contract)  # {"name": "nukez.viewer_container", "version": "1.0.0"}
```

To render content cards in the container, pass `renderables=[...]`:

```python
renderables = [
    client.make_text_renderable("hello world", title="Agent Note"),
    client.make_json_renderable({"score": 0.98, "status": "ok"}, title="Analysis"),
    client.make_pdf_renderable("https://example.com/file.pdf", title="Report"),
    client.make_image_renderable("https://example.com/image.png", title="Screenshot"),
    client.make_binary_renderable(hex_preview="00 ff ab cd", size_bytes=4096, title="Binary Preview"),
]
container = client.get_viewer_container_handoff(
    request_type="file_view",
    renderables=renderables,
)
print(container.result["state"])  # ready
print(container.meta["payload_embedded_in_url"])  # True when URL size permits
```

For structured views, prefer `view_kind + blocks`:

```python
blocks = [
    client.make_header_block("Locker Contents", subtitle="locker_..."),
    client.make_stats_block([{"label": "File Count", "value": 12}], title="Locker Stats"),
    client.make_links_block([{"label": "Owner Portal", "href": "https://nukez.xyz/owner?..."}]),
    client.make_table_block(
        columns=[{"key": "filename", "label": "Filename"}],
        rows=[{"filename": "report.pdf"}],
        title="Files",
    ),
]
container = client.get_viewer_container_handoff(
    view_kind="locker",
    request_type="locker_view",
    blocks=blocks,
)
```

### `get_locker_view_container(receipt_id, viewer_base_url="https://nukez.xyz", include_download_urls=False, ttl_min=30, embed_payload_in_url=True, button_label="Open Locker Viewer")`

Builds canonical locker view blocks:
- table + stats + links

```python
container = client.get_locker_view_container(receipt.id)
print(container.viewer_url)
```

### `get_attestation_view_container(receipt_id, viewer_base_url="https://nukez.xyz", embed_payload_in_url=True, button_label="Open Attestation Viewer")`

Builds attestation view blocks:
- kv + status + proofs + json

```python
container = client.get_attestation_view_container(receipt.id)
print(container.viewer_url)
```

### `get_file_view_container(receipt_id, filename, viewer_base_url="https://nukez.xyz", ttl_min=30, include_download_url=True, embed_payload_in_url=True, button_label="Open File Viewer")`

Builds file view blocks:
- file_meta + file_preview

`file_preview` is an internal viewer abstraction. Callers do not branch by mime-type.

```python
container = client.get_file_view_container(receipt.id, "report.pdf")
print(container.viewer_url)
```

### `get_owner_viewer_url(receipt_id, viewer_base_url="https://nukez.xyz")`

Build a stable locker portal URL for agent-to-human handoff.

```python
viewer = client.get_owner_viewer_url(receipt.id)
print(viewer.url)
# https://nukez.xyz/owner?locker_id=...&receipt_id=...
```

For MCP/UI button rendering, use:

```python
payload = client.get_owner_viewer_handoff(receipt.id)
print(payload["ui"])
# {"kind":"button","label":"Open Nukez Viewer","href":"...","variant":"nukez-neon","target":"_blank"}
```

Contract details:
- name: `nukez.mcp.viewer_link`
- version: `1.0`
- See: `docs/MCP_RENDERER_CONTRACT.md`

Important:
- Viewer links are canonical-manifest views.
- Objects uploaded outside the Nukez upload flow (for example, directly in a cloud console) are intentionally not included.

### `get_file_viewer_url(receipt_id, filename, viewer_base_url="https://nukez.xyz", ttl_min=30, include_download_url=True)`

Build a file-scoped viewer URL.  
If `include_download_url=True`, Nukez mints a fresh signed `download_url` and embeds it for immediate rendering.

```python
viewer = client.get_file_viewer_url(receipt.id, "data.json")
print(viewer.url)
# https://nukez.xyz/view?locker_id=...&receipt_id=...&filename=...&download_url=...
```

For MCP/UI button rendering, use:

```python
payload = client.get_file_viewer_handoff(receipt.id, "data.json")
print(payload["ui"])
```

### `list_files_with_viewer_urls(receipt_id, viewer_base_url="https://nukez.xyz", include_download_urls=False, ttl_min=30)`

List locker files enriched with viewer URLs.  
Default behavior avoids embedding short-lived download URLs per file.

```python
bundle = client.list_files_with_viewer_urls(receipt.id)
print(bundle.owner_viewer_url)
for f in bundle.files:
    print(f.filename, f.viewer_url)
```

For MCP/UI button rendering, use:

```python
payload = client.list_files_with_viewer_handoffs(receipt.id)
print(payload["ui"])        # owner button
print(payload["files"][0]["ui"])  # file button
```

Important:
- `list_files*` methods reflect protocol-indexed manifest entries only.
- Provider-side objects that bypass Nukez file APIs are non-canonical and excluded by design.

### `delete_file(receipt_id, filename)`

Remove a file permanently.

```python
client.delete_file(receipt.id, "old_data.json")
```

---

## Utility Methods

### `get_price(units=1)`

Check current pricing before buying.

```python
price = client.get_price(units=1)
print(f"Cost: {price.amount_sol} SOL")
```

### `get_viewer_renderer_contract()`

Return the renderer contract descriptor used by viewer handoff payloads.

```python
contract = client.get_viewer_renderer_contract()
print(contract)  # {"name": "nukez.mcp.viewer_link", "version": "1.0"}
```

### `verify_storage(receipt_id)`

Get cryptographic proof your data is stored.

```python
result = client.verify_storage(receipt.id)
print(f"Verified: {result.verified}")
```

### `verify_receipt_hash(receipt_id)`

Recompute and compare the canonical receipt hash. This verifies the receipt
object itself; use `verify_storage()` for storage/content attestation.

```python
check = client.verify_receipt_hash(receipt.id)
print(f"stored:   {check.stored_hash}")
print(f"computed: {check.computed_hash}")
print(f"match:    {check.matches}")
```

Use `client.receipt_hash_matches(receipt.id)` when you only need a boolean.

### `compute_hash(data)`

SHA256 hash for verifying data integrity.

```python
hash = client.compute_hash(b"my data")
```

---

## Errors

All errors inherit from `NukezError`.

| Error | When | What to do |
|-------|------|------------|
| `TransactionNotFoundError` | tx not yet propagated | Wait `e.suggested_delay` seconds, retry |
| `NukezFileNotFoundError` | File doesn't exist | Check `list_files()` |
| `URLExpiredError` | URL older than 30 min | Call `get_file_urls()` |
| `AuthenticationError` | Bad signature | Check keypair matches the one used to pay |

```python
from pynukez import NukezError, TransactionNotFoundError

try:
    receipt = client.confirm_storage(pay_req_id, tx_sig)
except TransactionNotFoundError as e:
    time.sleep(e.suggested_delay)
    receipt = client.confirm_storage(pay_req_id, tx_sig)  # Retry
except NukezError as e:
    print(f"Error: {e}")
    if e.retryable:
        print("You can retry this")
```

---

## For LLM Agents

Get tool definitions for function calling:

```python
import pynukez

# OpenAI-compatible tool schemas
tools = pynukez.get_tool_definitions()

# Human-readable instructions
instructions = pynukez.get_agent_instructions()
```
