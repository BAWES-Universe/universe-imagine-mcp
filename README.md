# Universe Imagine MCP

MCP server for AI image generation. Wraps local ComfyUI as a Streamable HTTP MCP server. Works with any MCP-compatible bot client.

## Features

- **Streamable HTTP** — MCP `2024-11-05` protocol, Bearer auth
- **Synchronous generation** — `generate_image` blocks and returns the image URL directly
- **Background queue** — multiple requests accepted, processed one at a time
- **Idempotency keys** — safe retries without duplicates
- **Player-aware** — reads `player_id` from MCP initialize handshake
- **Generation history** — SQLite, with `since` filter for incremental queries
- **Health endpoint** — reports backend status, queue depth, uptime
- **Image serving** — built-in static file server for generated images
- **Idle watchdog** — stops ComfyUI after 5 minutes of inactivity to free VRAM
- **Warm start** — ComfyUI starts in background when MCP server boots
- **Discord webhook** — optional generation notifications

## Prerequisites

- Python 3.11+
- [ComfyUI](https://github.com/comfyanonymous/ComfyUI) installed locally
- At least one model downloaded (e.g. Flux, SDXL, SD3, etc.)

## Setup

```bash
# 1. Clone the repo
git clone https://github.com/BAWES-Universe/universe-imagine-mcp.git
cd universe-imagine-mcp

# 2. Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
cp .env.example .env
# Edit .env — at minimum set DESIGNER_MCP_TOKEN and PUBLIC_URL_BASE
# Set MODEL_UNET, MODEL_CLIP, MODEL_VAE to match your ComfyUI models

# 5. Run
python3 designer_mcp.py
```

## Configuration

All config goes in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `DESIGNER_MCP_TOKEN` | — | Bearer token for MCP auth (required) |
| `PUBLIC_URL_BASE` | — | Public base URL for image serving (required) |
| `HOST` | `0.0.0.0` | Listen address |
| `PORT` | `8003` | MCP server port |
| `COMFYUI_URL` | `http://127.0.0.1:8188` | ComfyUI API URL |
| `COMFYUI_DIR` | `~/ComfyUI` | ComfyUI install directory |
| `DB_PATH` | `./imagine.db` | SQLite database path |
| `OUTPUT_DIR` | `~/ComfyUI/output` | ComfyUI output directory |
| `DISCORD_WEBHOOK_URL` | — | Discord webhook for generation notifications (optional) |
| `MODEL_UNET` | `flux-2-klein-4b-fp8.safetensors` | UNet model filename |
| `MODEL_CLIP` | `Qwen3-4B-Q4_K_M.gguf` | CLIP model filename |
| `MODEL_VAE` | `flux2_klein_vae.safetensors` | VAE model filename |

## Tools

### `generate_image(prompt, width=512, height=512, idempotency_key="")` → JSON

Generate an image from a text prompt. Blocks until complete (typically 3-15 seconds) and returns the image URL directly. The image is auto-sent to the conversation by the bot.

Use `idempotency_key` to safely retry without duplicates — same key = cached result.

### `get_generation(generation_id)` → JSON

Retrieve a past generation's full record by ID.

### `list_generations(limit=10, since="")` → JSON array

List recent generations for the calling player. Optionally filter by ISO 8601 timestamp.

### `get_generation_status(generation_id)` → JSON (legacy)

Legacy polling tool kept for backwards compatibility. The current `generate_image` is synchronous and returns the URL directly — polling is not needed.

## DNS Rebinding — Monkey Patch

The server includes a monkey-patch for FastMCP's built-in DNS rebinding check (see `designer_mcp.py` lines 23-33). This is needed when running behind a reverse proxy or Cloudflare tunnel where the incoming `Host` header (e.g., `imagine.yourdomain.com`) doesn't match `localhost`.

**If you're running behind a reverse proxy:** The patch is safe because auth is handled by the `BearerAuthMiddleware`. Keep it.

**If you're running on localhost only (no tunnel/proxy):** Remove the patch entirely — delete lines 23-33 from `designer_mcp.py`.

The patch:
```python
from mcp.server.transport_security import TransportSecurityMiddleware

_orig_validate = TransportSecurityMiddleware.validate_request
async def _patched_validate(self, request, is_post=False):
    return None  # Skip Host header validation

TransportSecurityMiddleware.validate_request = _patched_validate
```

## Connecting to a Universe Bot

Register the MCP server on a bot via the admin API:

```json
POST /api/bots/{botId}/mcp-servers
{
    "serverUrl": "https://your-domain.com/mcp",
    "transport": "streamable-http",
    "authType": "bearer",
    "authConfig": "your-token"
}
```

Player ID is sent automatically in the MCP initialize handshake — no extra config needed.

## Architecture

```
┌─────────────┐     Streamable HTTP      ┌──────────────────┐
│   Bot AI    │ ◄─────────────────────── │  Imagine MCP     │
│ (MCP client)│     /mcp (port 8003)     │  (FastMCP)       │
└─────────────┘                          │                  │
                                         │  ┌────────────┐  │
┌─────────────┐     Static files         │  │  Queue (1)  │──┼──► _run_generation()
│   Browser   │ ◄─────────────────────── │  └────────────┘  │
│ /images/*   │     /images (port 8004)  │         │         │
└─────────────┘                          │         ▼         │
                                         │  ┌────────────┐  │
    curl /health                          │  │  ComfyUI   │  │
    ─────────►  /health (port 8005)      │  │ (port 8188)│  │
                                         │  └────────────┘  │
                                         └──────────────────┘
```

## License

MIT
