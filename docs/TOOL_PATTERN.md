Nukez Tool Pattern: Agent-Native Architecture
Document Status
Canonical Reference for Nukez SDK tool patterns
Version: 2.0 (Corrected)
Location: pynukez/docs/TOOL_PATTERN.md (SDK repository)
-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
Overview
Nukez implements a three-layer architecture that separates SDK methods, tool adapters, and execution contexts. This architecture addresses the fundamental distinction between
ergonomic Python APIs and agent-compatible tool interfaces.
┌─────────────────────────────────────────────────────────────┐
│ Layer 1: SDK Methods (pynukez/client.py)                  │
│   - Ergonomic Python API                                    │
│   - Returns typed dataclasses + bytes                       │
│   - May have optional parameters                            │
│   - Example: provision_locker(receipt_id, tags=None)        │
└─────────────────────────────────────────────────────────────┘
↓
┌─────────────────────────────────────────────────────────────┐
│ Layer 2: Tool Adapters (user code / examples/)             │
│   - JSON-serializable returns only                          │
│   - Base64 encoding for bytes                               │
│   - Explicit required parameters                            │
│   - Schema matches execution behavior                       │
│   - Example: provision_locker_tool(receipt_id, tags)        │
└─────────────────────────────────────────────────────────────┘
↓
┌─────────────────────────────────────────────────────────────┐
│ Layer 3: Executor (framework-specific)                     │
│   - Dispatch tool calls to adapters                         │
│   - Log operations for observability                        │
│   - Translate errors to agent-friendly messages             │
│   - Example: OpenAI function calling, MCP server            │
└─────────────────────────────────────────────────────────────┘
-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
Critical Corrections: Canonical Parameter Names
receipt_id is the Primary Authorization Handle
The Nukez SDK uses receipt_id as the canonical parameter for all authenticated operations. This reflects the Nukez backend architecture where:
• receipt_id: Immutable payment proof signed by the gateway
• Contains payer_pubkey (root of authority)
• Publicly verifiable via /v1/receipts/{receipt_id}/verify
• Used in provisioning and authentication flows
• locker_id: Operational storage namespace
• Deterministically derived from receipt_id
• Used in file operation URLs after provisioning
• One-to-one mapping with receipt_id
Corrected SDK Signatures
# CORRECT SDK signatures (from pynukez/client.py)
# Provisioning (uses receipt_id)
def provision_locker(
receipt_id: str,
tags: Optional[List[str]] = None
) -> ProvisionedLocker:
"""Provision a storage locker from a payment receipt."""
# File operations (use locker_id after provisioning)
def create_file(
locker_id: str,
filename: str,
content: bytes,
content_type: str = "application/octet-stream"
) -> FileOperationResult:
"""Create a file in the provisioned locker."""
def list_files(locker_id: str) -> List[FileMetadata]:
"""List files in the provisioned locker."""
def download_file(locker_id: str, filename: str) -> FileContent:
"""Download file content (returns bytes)."""
def delete_file(locker_id: str, filename: str) -> FileOperationResult:
"""Delete a file from the locker."""
# Payment flow (generates receipt_id)
def request_storage(units: int) -> PaymentRequest:
"""Request storage payment (step 1)."""
def confirm_storage(
pay_req_id: str,
tx_hash: str
) -> StorageReceipt:
"""Confirm payment and receive receipt_id (step 2)."""
Authentication Flow
1. request_storage(units=10)
→ pay_req_id
2. confirm_storage(pay_req_id, tx_hash)
→ receipt_id (immutable payment proof)
3. provision_locker(receipt_id, tags=["project_x"])
→ locker_id + capability_token
4. create_file(locker_id, "data.json", content)
→ uses locker_id for file operations
-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
Layer 1: SDK Methods
Purpose: Provide ergonomic Python API for direct SDK usage.
Characteristics:
• Returns typed dataclasses (ProvisionedLocker, FileContent, etc.)
• May return raw bytes (e.g., download_file() returns FileContent.content: bytes)
• Uses optional parameters for developer convenience
• Raises typed exceptions (TransactionNotFoundError, URLExpiredError)
Example:
from pynukez import NukezClient
client = NukezClient(wallet_path="wallet.json")
# SDK returns dataclass with bytes
file_content = client.download_file(locker_id="locker_abc", filename="data.bin")
raw_bytes = file_content.content  # bytes object
-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
Layer 2: Tool Adapters
Purpose: Transform SDK methods into agent-compatible tool callables.
Requirements:
1 JSON-serializable returns: No bytes, no custom classes
2 Explicit parameters: All required params in schema (no optional defaults)
3 Normalized encodings: Base64 for binary data
4 Schema-execution match: Tool schema must exactly match implementation
Data Transformation Rules
SDK Return Type  Tool Return Type  Transformation
bytes            str               Base64 encode
Dataclass        dict              .to_dict() + encode bytes fields
Exception        {"error": str}    Error message extraction
List[DataClass]  List[dict]        List comprehension + .to_dict()
Corrected Tool Adapter Examples
import base64
from typing import List, Optional
def provision_locker_tool(receipt_id: str, tags: List[str]) -> dict:
"""
Tool adapter for provision_locker.
Args:
receipt_id: Payment receipt ID (REQUIRED)
tags: List of metadata tags (REQUIRED - no optional params)
Returns:
{
"locker_id": str,
"capability_token": str,
"urls": {"upload": str, "download": str}
}
"""
try:
result = client.provision_locker(receipt_id=receipt_id, tags=tags)
return {
"locker_id": result.locker_id,
"capability_token": result.capability_token,
"urls": result.urls
}
except Exception as e:
return {"error": str(e)}
def create_file_tool(
locker_id: str,
filename: str,
content_base64: str,
content_type: str
) -> dict:
"""
Tool adapter for create_file.
Args:
locker_id: Storage locker ID (REQUIRED)
filename: File name (REQUIRED)
content_base64: Base64-encoded file content (REQUIRED)
content_type: MIME type (REQUIRED - no default)
Returns:
{"filename": str, "size_bytes": int}
"""
try:
content_bytes = base64.b64decode(content_base64)
result = client.create_file(
locker_id=locker_id,
filename=filename,
content=content_bytes,
content_type=content_type
)
return {
"filename": result.filename,
"size_bytes": result.size_bytes
}
except Exception as e:
return {"error": str(e)}
def download_file_tool(locker_id: str, filename: str) -> dict:
"""
Tool adapter for download_file.
Returns:
{
"filename": str,
"content_base64": str,  # ← Base64-encoded bytes
"content_type": str,
"size_bytes": int
}
"""
try:
result = client.download_file(locker_id=locker_id, filename=filename)
return {
"filename": result.filename,
"content_base64": base64.b64encode(result.content).decode(),
"content_type": result.content_type,
"size_bytes": len(result.content)
}
except Exception as e:
return {"error": str(e)}
Tool Schema Validation
Critical Rule: Tool schemas must match tool adapter signatures exactly.
# CORRECT: Schema matches adapter signature
{
"type": "function",
"function": {
"name": "provision_locker_tool",
"description": "Provision storage locker from payment receipt",
"parameters": {
"type": "object",
"properties": {
"receipt_id": {
"type": "string",
"description": "Payment receipt ID from confirm_storage"
},
"tags": {
"type": "array",
"items": {"type": "string"},
"description": "Metadata tags for the locker"
}
},
"required": ["receipt_id", "tags"]  # ← Both required
}
}
}
# WRONG: Missing required parameter
{
"required": ["receipt_id"]  # ❌ Agent can omit tags, causing error
}
# WRONG: Parameter name mismatch
{
"properties": {
"locker_id": {...},  # ❌ SDK uses receipt_id for provisioning
"receipt_id": {...}
}
}
-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
Layer 3: Executor Pattern
Purpose: Framework-specific dispatch, logging, and error translation.
from typing import Callable, Dict, Any
import logging
class ToolExecutor:
"""Generic executor for any LLM framework."""
def __init__(self):
self.tools: Dict[str, Callable] = {}
self.logger = logging.getLogger(__name__)
def register(self, name: str, func: Callable):
"""Register a tool adapter."""
self.tools[name] = func
def execute(self, tool_name: str, arguments: Dict[str, Any]) -> dict:
"""
Execute tool call with logging and error handling.
Returns:
Tool result (always JSON-serializable dict)
"""
self.logger.info(f"Executing {tool_name}", extra={"args": arguments})
try:
if tool_name not in self.tools:
return {"error": f"Unknown tool: {tool_name}"}
result = self.tools[tool_name](**arguments)
if "error" in result:
self.logger.error(f"{tool_name} failed: {result['error']}")
else:
self.logger.info(f"{tool_name} succeeded")
return result
except Exception as e:
error_msg = f"Executor error in {tool_name}: {str(e)}"
self.logger.exception(error_msg)
return {"error": error_msg}
# Usage
executor = ToolExecutor()
executor.register("provision_locker_tool", provision_locker_tool)
executor.register("create_file_tool", create_file_tool)
# Execute from LLM function call
result = executor.execute(
tool_name="provision_locker_tool",
arguments={"receipt_id": "abc123", "tags": ["project_x"]}
)
-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
Complete Payment → Storage Workflow
Tool Sequence Pattern
# Agent executes this sequence using tool adapters:
# 1. Request storage payment
payment_request = request_storage_tool(units=10)
# Returns: {"pay_req_id": "pid_123", "payment_address": "...", "amount_lamports": 1000}
# 2. User completes Solana payment (off-chain)
tx_hash = user_wallet.transfer(
to=payment_request["payment_address"],
amount=payment_request["amount_lamports"]
)
# 3. Confirm payment and get receipt
receipt = confirm_storage_tool(
pay_req_id=payment_request["pay_req_id"],
tx_hash=tx_hash
)
# Returns: {"receipt_id": "rec_456", "units": 10, "payer_pubkey": "wallet_abc"}
# 4. Provision locker using receipt_id
locker = provision_locker_tool(
receipt_id=receipt["receipt_id"],  # ← Use receipt_id
tags=["agent_workspace"]
)
# Returns: {"locker_id": "locker_789", "capability_token": "cap_xyz"}
# 5. Store file using locker_id
file_result = create_file_tool(
locker_id=locker["locker_id"],  # ← Now use locker_id
filename="agent_memory.json",
content_base64=base64.b64encode(b'{"state": "..."}').decode(),
content_type="application/json"
)
# Returns: {"filename": "agent_memory.json", "size_bytes": 42}
Error Recovery Patterns
def handle_transaction_not_found(receipt_id: str, max_retries: int = 3):
"""Pattern for handling blockchain confirmation delays."""
for attempt in range(max_retries):
try:
return provision_locker_tool(receipt_id=receipt_id, tags=[])
except Exception as e:
if "TransactionNotFoundError" in str(e):
time.sleep(2 ** attempt)  # Exponential backoff
continue
raise
return {"error": "Transaction confirmation timeout"}
def handle_url_expiration(locker_id: str, filename: str):
"""Pattern for refreshing expired signed URLs."""
result = download_file_tool(locker_id=locker_id, filename=filename)
if "error" in result and "URL_EXPIRED" in result["error"]:
# Refresh capability token
refresh_result = refresh_locker_tool(locker_id=locker_id)
if "error" not in refresh_result:
# Retry with new URLs
return download_file_tool(locker_id=locker_id, filename=filename)
return result
-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
Framework-Specific Integration
OpenAI Function Calling
def get_openai_tool_schemas() -> List[dict]:
"""Generate OpenAI-compatible tool schemas."""
return [
{
"type": "function",
"function": {
"name": "provision_locker_tool",
"description": "Provision storage locker from payment receipt",
"parameters": {
"type": "object",
"properties": {
"receipt_id": {
"type": "string",
"description": "Receipt ID from confirm_storage_tool"
},
"tags": {
"type": "array",
"items": {"type": "string"},
"description": "Metadata tags (e.g., ['project_name'])"
}
},
"required": ["receipt_id", "tags"]
}
}
},
# ... other tools
]
# Usage with OpenAI
client = OpenAI()
response = client.chat.completions.create(
model="gpt-4",
messages=[{"role": "user", "content": "Store my config file"}],
tools=get_openai_tool_schemas()
)
Model Context Protocol (MCP)
# pynukez/mcp_server.py provides MCP-compatible server
from mcp.server import Server, Tool
from mcp.types import TextContent
server = Server("pynukez-mcp")
@server.list_tools()
async def list_tools() -> List[Tool]:
return [
Tool(
name="provision_locker_tool",
description="Provision storage locker",
inputSchema={
"type": "object",
"properties": {
"receipt_id": {"type": "string"},
"tags": {"type": "array", "items": {"type": "string"}}
},
"required": ["receipt_id", "tags"]
}
)
]
@server.call_tool()
async def call_tool(name: str, arguments: dict) -> List[TextContent]:
executor = ToolExecutor()
result = executor.execute(name, arguments)
return [TextContent(type="text", text=json.dumps(result))]
-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
Design Principles
1. Explicit Parameters, No Hidden State
Why: Agents cannot track implicit context across tool calls.
# ❌ WRONG: Implicit state
class StatefulClient:
def __init__(self):
self.current_locker = None
def create_file(self, filename: str):  # Missing locker_id!
return self._upload(self.current_locker, filename)
# ✓ CORRECT: Explicit parameters
def create_file_tool(locker_id: str, filename: str, content_base64: str):
"""Every parameter required in every call."""
2. Atomic Operations
Why: Agents need clear success/failure boundaries.
# ❌ WRONG: Multi-step operation
def upload_and_verify(locker_id: str, filename: str, content: str):
upload_file(...)      # What if this succeeds...
verify_checksum(...)  # ...but this fails?
# ✓ CORRECT: Separate atomic tools
upload_result = create_file_tool(locker_id, filename, content)
if "error" not in upload_result:
verify_result = verify_file_tool(locker_id, filename, expected_hash)
3. JSON-Serializable Returns
Why: LLM frameworks require JSON for function call returns.
# ❌ WRONG: Returns bytes
def download_file_tool(locker_id: str, filename: str) -> bytes:
return client.download_file(locker_id, filename).content
# ✓ CORRECT: Returns base64-encoded string
def download_file_tool(locker_id: str, filename: str) -> dict:
content = client.download_file(locker_id, filename).content
return {
"filename": filename,
"content_base64": base64.b64encode(content).decode(),
"size_bytes": len(content)
}
4. Schema-Execution Consistency
Why: Schema mismatches cause silent failures or agent confusion.
# Tool schema
{
"required": ["receipt_id", "tags"]
}
# ✓ Adapter signature MUST match
def provision_locker_tool(receipt_id: str, tags: List[str]):
# Both parameters required, no defaults
...
# ❌ WRONG: Optional parameter
def provision_locker_tool(receipt_id: str, tags: Optional[List[str]] = None):
# Schema says required, but implementation allows None
-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
Testing Tool Patterns
Note: Testing belongs in Nukez Testing Kit (PTK), not SDK.
PTK validates that SDK tool patterns work correctly with autonomous agents:
# ptk/test_contract_validation.py
def test_tool_schema_matches_adapter():
"""Validate schema required params match adapter signature."""
schemas = get_openai_tool_schemas()
for schema in schemas:
func_name = schema["function"]["name"]
required_params = schema["function"]["parameters"]["required"]
# Get adapter function
adapter_func = globals()[func_name]
sig = inspect.signature(adapter_func)
# All schema-required params must be in signature
for param in required_params:
assert param in sig.parameters
# ptk/test_sdk_autonomous.py
def test_payment_storage_workflow():
"""Test agents can complete payment → storage flow."""
agent = AutonomousAgent(tools=get_openai_tool_schemas())
result = agent.run_task(
"Request 10 units of storage, pay for it, and store a file"
)
assert "locker_id" in result
assert "filename" in result
See PTK/TESTING.md for full test suite documentation.