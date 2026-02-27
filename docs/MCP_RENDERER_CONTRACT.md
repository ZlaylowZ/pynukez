# MCP Renderer Contract (Viewer Links)

This document defines the stable rendering contract for agent-returned viewer links.

- Contract name: `nukez.mcp.viewer_link`
- Contract version: `1.0`
- UI variant token: `nukez-neon`
- Container contract name: `nukez.viewer_container`
- Container contract version: `1.0.0`

## Purpose

The Nukez SDK returns viewer link payloads for MCP/LLM tools.  
Renderers (chat apps, MCP hosts, web clients) should use this contract to display a consistent "Open Viewer" button.

## Payload Shape

Viewer handoff payloads include:

```json
{
  "renderer_contract": {
    "name": "nukez.mcp.viewer_link",
    "version": "1.0"
  },
  "kind": "owner",
  "viewer_url": "https://nukez.xyz/owner?...",
  "locker_id": "locker_...",
  "receipt_id": "...",
  "ui": {
    "kind": "button",
    "label": "Open Nukez Viewer",
    "href": "https://nukez.xyz/owner?...",
    "variant": "nukez-neon",
    "target": "_blank"
  }
}
```

For file-scoped payloads, `filename`, `download_url`, and `expires_in_sec` may also appear.

## Viewer Container Payload (v1)

For container-first handoff, Nukez can return:

```json
{
  "contract": "nukez.viewer_container",
  "version": "1.0.0",
  "request_type": "container",
  "view_kind": "locker",
  "viewer_url": "https://nukez.xyz/viewer?request_type=container",
  "input": {
    "receipt_id": null,
    "locker_id": null,
    "filename": null
  },
  "result": {
    "kind": "container",
    "state": "empty",
    "view_kind": "locker",
    "viewer_url": "https://nukez.xyz/viewer?request_type=container",
    "blocks": [],
    "renderables": []
  },
  "render_hints": {
    "variant": "nukez-neon",
    "layout": "container",
    "primary_action_label": "Open Nukez Viewer",
    "target": "_blank"
  },
  "auth_state": { "mode": "keypair_signature" },
  "errors": [],
  "meta": {
    "generated_at": 1771347000,
    "sdk_contract": { "name": "nukez.viewer_container", "version": "1.0.0" }
  },
  "ui": {
    "kind": "button",
    "label": "Open Nukez Viewer",
    "href": "https://nukez.xyz/viewer?request_type=container",
    "variant": "nukez-neon",
    "target": "_blank"
  }
}
```

`result.blocks` is the primary structured UI model. Supported block types:

- `header`
- `stats`
- `links`
- `table`
- `kv`
- `status`
- `proofs`
- `json`
- `file_meta`
- `file_preview` (mime-specific rendering is handled by the viewer internally)

Canonical view presets:

- Locker view: `table + stats + links`
- Attestation view: `kv + status + proofs + json`
- File view: `file_meta + file_preview`

`result.renderables` remains backward-compatible for legacy consumers. Legacy typed objects:

- `text`: `{ "type":"text", "title":"...", "content":"..." }`
- `json`: `{ "type":"json", "title":"...", "data": { ... } }`
- `pdf`: `{ "type":"pdf", "title":"...", "url":"https://..." }`
- `image`: `{ "type":"image", "title":"...", "url":"https://..." }`
- `binary`: `{ "type":"binary", "title":"...", "hex_preview":"...", "size_bytes":123 }`

When blocks or renderables are present, Nukez may embed the payload in `viewer_url` as a `payload` query parameter for immediate render-on-open. If the encoded URL is too large, SDK returns the base viewer URL and includes `errors=[{"code":"PAYLOAD_TOO_LARGE", ...}]`.

## Required Fields

- `renderer_contract.name`
- `renderer_contract.version`
- `viewer_url`
- `ui.kind`
- `ui.label`
- `ui.href`
- `ui.variant`

## Renderer Behavior

- If `renderer_contract.name` is recognized and `version == "1.0"`, render using the contract.
- If `ui.variant == "nukez-neon"`, apply Nukez themed button styling.
- If variant is unknown, fall back to default button styling.
- If any required `ui` field is missing, show a plain hyperlink using `viewer_url`.

## Compatibility Policy

- Minor style tweaks in frontend CSS do not change contract version.
- Renaming/removing variant tokens (`nukez-neon`) is a breaking change.
- Changing required fields or semantics is a breaking change.
- Breaking changes require a new contract version (`2.0`, etc.).
