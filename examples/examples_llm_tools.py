"""
LLM integration - use Nukez as tools for your AI agent.

This example shows how to wire up Nukez with OpenAI function calling.

Run:
    pip install openai
    export OPENAI_API_KEY=your_key
    python examples/examples_llm_tools.py "Store a note that says hello"
"""

import json
import sys
from openai import OpenAI
import pynukez

# Get tool definitions from SDK
tools = pynukez.get_tool_definitions()

# Your storage client
storage = pynukez.Nukez(keypair_path="~/.config/solana/id.json")
receipt_id = None  # Set this if you have existing storage

def run_tool(name, args):
    """Execute a Nukez tool."""
    global receipt_id
    
    if name == "nukez_request_storage":
        r = storage.request_storage(args.get("units", 1))
        return {
            "pay_req_id": r.pay_req_id,
            "pay_to_address": r.pay_to_address,
            "amount_sol": r.amount_sol,
            "amount": r.amount,
            "pay_asset": r.pay_asset,
            "network": r.network,
            "next_step": r.next_step,
            "note": "pynukez does not move funds. Execute the transfer externally and pass the tx signature to nukez_confirm_storage.",
        }

    elif name == "nukez_confirm_storage":
        r = storage.confirm_storage(args["pay_req_id"], args["tx_sig"])
        receipt_id = r.id
        return {"receipt_id": r.id}
    
    elif name == "nukez_provision_locker":
        storage.provision_locker(args.get("receipt_id") or receipt_id)
        return {"done": True}
    
    elif name == "nukez_create_file":
        r = storage.create_file(args.get("receipt_id") or receipt_id, args.get("filename"))
        return {"upload_url": r.upload_url, "download_url": r.download_url, "filename": r.filename}
    
    elif name == "nukez_upload_bytes":
        # LLM tool-calling always passes strings through JSON, so route to
        # upload_string() which sanitizes common agent formatting artifacts
        # (JSON wrappers, markdown fencing) and encodes to UTF-8.
        data = args["data"]
        if isinstance(data, str):
            storage.upload_string(args["upload_url"], data, content_type=args.get("content_type"))
        else:
            storage.upload_bytes(args["upload_url"], data, content_type=args.get("content_type"))
        return {"done": True}

    elif name == "nukez_upload_file_path":
        return storage.upload_file_path(
            receipt_id=args.get("receipt_id") or receipt_id,
            filepath=args["filepath"],
            filename=args.get("filename"),
            content_type=args.get("content_type"),
            ttl_min=args.get("ttl_min", 30),
            confirm=args.get("confirm", True),
        )

    elif name == "nukez_bulk_upload_paths":
        return storage.bulk_upload_paths(
            receipt_id=args.get("receipt_id") or receipt_id,
            sources=args["sources"],
            workers=args.get("workers", 6),
            ttl_min=args.get("ttl_min", 30),
            confirm=args.get("confirm", True),
            auto_attest=args.get("auto_attest", False),
            attest_sync=args.get("attest_sync", False),
        )

    elif name == "nukez_upload_directory":
        return storage.upload_directory(
            receipt_id=args.get("receipt_id") or receipt_id,
            source_dir=args["source_dir"],
            pattern=args.get("pattern", "*"),
            recursive=args.get("recursive", False),
            exclude_pattern=args.get("exclude_pattern"),
            preserve_structure=args.get("preserve_structure", False),
            workers=args.get("workers", 6),
            ttl_min=args.get("ttl_min", 30),
            confirm=args.get("confirm", True),
            auto_attest=args.get("auto_attest", False),
            attest_sync=args.get("attest_sync", False),
        )

    elif name == "nukez_start_bulk_upload_job":
        return storage.start_bulk_upload_job(
            receipt_id=args.get("receipt_id") or receipt_id,
            sources=args["sources"],
            workers=args.get("workers", 6),
            ttl_min=args.get("ttl_min", 30),
            confirm=args.get("confirm", True),
            auto_attest=args.get("auto_attest", False),
            attest_sync=args.get("attest_sync", False),
        )

    elif name == "nukez_get_upload_job":
        return storage.get_upload_job(args["job_id"])

    elif name == "nukez_download_bytes":
        data = storage.download_bytes(args["download_url"])
        return {"data": data.decode()}
    
    elif name == "nukez_list_files":
        files = storage.list_files(args.get("receipt_id") or receipt_id)
        return {"files": [f.filename for f in files]}

    elif name == "nukez_get_viewer_container_contract":
        return storage.get_viewer_container_contract()

    elif name == "nukez_get_viewer_container":
        c = storage.get_viewer_container_handoff(
            viewer_base_url=args.get("viewer_base_url", "https://nukez.xyz"),
            request_type=args.get("request_type", "container"),
            view_kind=args.get("view_kind", "custom"),
            receipt_id=args.get("receipt_id") or receipt_id,
            locker_id=args.get("locker_id"),
            filename=args.get("filename"),
            blocks=args.get("blocks"),
            renderables=args.get("renderables"),
            embed_payload_in_url=args.get("embed_payload_in_url", True),
            button_label=args.get("button_label", "Open Nukez Viewer"),
        )
        return {
            "contract": c.contract,
            "version": c.version,
            "request_type": c.request_type,
            "viewer_url": c.viewer_url,
            "input": c.input,
            "result": c.result,
            "render_hints": c.render_hints,
            "auth_state": c.auth_state,
            "errors": c.errors,
            "meta": c.meta,
            "ui": c.ui,
        }

    elif name == "nukez_get_locker_view":
        c = storage.get_locker_view_container(
            receipt_id=args.get("receipt_id") or receipt_id,
            viewer_base_url=args.get("viewer_base_url", "https://nukez.xyz"),
            include_download_urls=args.get("include_download_urls", False),
            ttl_min=args.get("ttl_min", 30),
            embed_payload_in_url=args.get("embed_payload_in_url", True),
            button_label=args.get("button_label", "Open Locker Viewer"),
        )
        return {
            "contract": c.contract,
            "version": c.version,
            "request_type": c.request_type,
            "viewer_url": c.viewer_url,
            "input": c.input,
            "result": c.result,
            "render_hints": c.render_hints,
            "auth_state": c.auth_state,
            "errors": c.errors,
            "meta": c.meta,
            "ui": c.ui,
        }

    elif name == "nukez_get_attestation_view":
        c = storage.get_attestation_view_container(
            receipt_id=args.get("receipt_id") or receipt_id,
            viewer_base_url=args.get("viewer_base_url", "https://nukez.xyz"),
            embed_payload_in_url=args.get("embed_payload_in_url", True),
            button_label=args.get("button_label", "Open Attestation Viewer"),
        )
        return {
            "contract": c.contract,
            "version": c.version,
            "request_type": c.request_type,
            "viewer_url": c.viewer_url,
            "input": c.input,
            "result": c.result,
            "render_hints": c.render_hints,
            "auth_state": c.auth_state,
            "errors": c.errors,
            "meta": c.meta,
            "ui": c.ui,
        }

    elif name == "nukez_get_file_view":
        c = storage.get_file_view_container(
            receipt_id=args.get("receipt_id") or receipt_id,
            filename=args["filename"],
            viewer_base_url=args.get("viewer_base_url", "https://nukez.xyz"),
            ttl_min=args.get("ttl_min", 30),
            include_download_url=args.get("include_download_url", True),
            embed_payload_in_url=args.get("embed_payload_in_url", True),
            button_label=args.get("button_label", "Open File Viewer"),
        )
        return {
            "contract": c.contract,
            "version": c.version,
            "request_type": c.request_type,
            "viewer_url": c.viewer_url,
            "input": c.input,
            "result": c.result,
            "render_hints": c.render_hints,
            "auth_state": c.auth_state,
            "errors": c.errors,
            "meta": c.meta,
            "ui": c.ui,
        }

    elif name == "nukez_get_owner_viewer_url":
        r = storage.get_owner_viewer_handoff(
            args.get("receipt_id") or receipt_id,
            args.get("viewer_base_url", "https://nukez.xyz"),
            args.get("button_label", "Open Nukez Viewer"),
        )
        return r

    elif name == "nukez_get_viewer_renderer_contract":
        return storage.get_viewer_renderer_contract()

    elif name == "nukez_get_file_viewer_url":
        r = storage.get_file_viewer_handoff(
            args.get("receipt_id") or receipt_id,
            args["filename"],
            viewer_base_url=args.get("viewer_base_url", "https://nukez.xyz"),
            ttl_min=args.get("ttl_min", 30),
            include_download_url=args.get("include_download_url", True),
            button_label=args.get("button_label", "Open File Viewer"),
        )
        return r

    elif name == "nukez_list_files_with_viewer_urls":
        r = storage.list_files_with_viewer_handoffs(
            args.get("receipt_id") or receipt_id,
            viewer_base_url=args.get("viewer_base_url", "https://nukez.xyz"),
            include_download_urls=args.get("include_download_urls", False),
            ttl_min=args.get("ttl_min", 30),
        )
        return r
    
    return {"error": f"Unknown tool: {name}"}

def chat(message):
    """Send a message and let the agent use tools."""
    client = OpenAI()
    messages = [{"role": "user", "content": message}]
    
    while True:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=tools
        )
        
        msg = response.choices[0].message
        
        if not msg.tool_calls:
            return msg.content
        
        messages.append(msg)
        for tc in msg.tool_calls:
            result = run_tool(tc.function.name, json.loads(tc.function.arguments))
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})

if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(chat(" ".join(sys.argv[1:])))
    else:
        print("Usage: python llm_tools.py 'your message'")
