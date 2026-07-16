# Universe Imagine MCP

MCP server for AI image generation. Wraps local ComfyUI as a Streamable HTTP MCP server. Works with any MCP-compatible bot client.

## Features

- **Streamable HTTP** — MCP `2024-11-05` protocol, Bearer auth
- **Async generation** — `generate_image` returns a `job_id` instantly, poll `get_generation_status`
- **Background queue** — multiple requests accepted immediately, processed one at a time
- **Idempotency keys** — safe retries without duplicates
- **Player-aware** — reads `player_id` from MCP initialize handshake
- **Generation history** — SQLite, with `since` filter for incremental queries
- **Health endpoint** — reports backend status, queue depth, uptime
- **Image serving** — built-in static file server for generated images
- **Idle watchdog** — stops ComfyUI after 5 minutes of inactivity to free VRAM
- **Warm start** — ComfyUI starts in background when MCP server boots

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

Start generating an image. Returns a `job_id` immediately. Poll `get_generation_status` to check when it's done.

The `idempotency_key` prevents duplicates on retry — same key = cached result.

### `get_generation_status(generation_id)` → JSON

Poll a generation by ID. Returns status (`processing` / `completed` / `error` / `timeout`) and `image_url` when done.

### `get_generation(generation_id)` → JSON

Retrieve a past generation's full record.

### `list_generations(limit=10, since="")` → JSON array

List recent generations for the calling player. Optionally filter by ISO 8601 timestamp.

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
