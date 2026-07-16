#!/usr/bin/env python3
"""
Imagine MCP — image generation for bots.

Streamable HTTP MCP server wrapping local ComfyUI.
  - Python + FastMCP
  - Streamable HTTP at /mcp, Bearer token auth
  - Player ID from MCP initialize handshake (clientInfo.player_id)
  - SQLite for generation history
  - Discord webhook on generation (optional)
  - Async generation with job polling
  - Idempotency key for safe retries
  - Background worker queue (processes one at a time)
"""
import json, sqlite3, subprocess, time, os, sys, uuid, threading, queue
import requests as http_requests
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from mcp.server.fastmcp import FastMCP, Context

# Monkey-patch FastMCP's DNS rebinding check — it rejects valid Host headers
# when the server is behind a reverse proxy or Cloudflare tunnel
# (Host: your-domain.com != localhost).
#
# Safe because auth is handled by our own BearerAuthMiddleware.
# If you don't use a reverse proxy, remove this patch entirely.
from mcp.server.transport_security import TransportSecurityMiddleware

_orig_validate = TransportSecurityMiddleware.validate_request
async def _patched_validate(self, request, is_post=False):
    return None  # Skip Host header validation

TransportSecurityMiddleware.validate_request = _patched_validate

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BEARER_TOKEN = os.environ.get("DESIGNER_MCP_TOKEN")
if BEARER_TOKEN:
    print(f"[ImagineMCP] Auth enabled (token: {BEARER_TOKEN[:8]}...)")
else:
    print("[ImagineMCP] Auth DISABLED — open access")
COMFYUI_URL  = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
if DISCORD_WEBHOOK_URL:
    print(f"[ImagineMCP] Discord webhook enabled")
else:
    print("[ImagineMCP] Discord webhook disabled — no DISCORD_WEBHOOK_URL set")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8003"))
DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).parent / "imagine.db"))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", str(Path.home() / "ComfyUI" / "output"))
PUBLIC_URL_BASE = os.environ.get("PUBLIC_URL_BASE", "").strip()
if not PUBLIC_URL_BASE:
    print("[ImagineMCP] WARNING: PUBLIC_URL_BASE not set — image URLs will be relative")

# Model names (set in .env or ComfyUI defaults)
MODEL_UNET = os.environ.get("MODEL_UNET", "flux-2-klein-4b-fp8.safetensors")
MODEL_CLIP = os.environ.get("MODEL_CLIP", "Qwen3-4B-Q4_K_M.gguf")
MODEL_VAE  = os.environ.get("MODEL_VAE", "flux2_klein_vae.safetensors")

# Image defaults
DEFAULT_WIDTH  = 512
DEFAULT_HEIGHT = 512
STEPS = 4
CFG   = 3.5

# Background worker queue — accepts all requests, processes one at a time
_generation_queue = queue.Queue()
_queue_worker_started = False

# ComfyUI lifecycle
COMFYUI_DIR = os.path.expanduser(os.environ.get("COMFYUI_DIR", "~/ComfyUI"))
IDLE_TIMEOUT = 300  # 5 minutes
_comfyui_process = None
_last_gen_time = time.time()
_backend_start_time = time.time()

def _comfyui_running() -> bool:
    try:
        resp = http_requests.get(f"{COMFYUI_URL}/system_stats", timeout=3)
        return resp.status_code == 200
    except Exception:
        return False

def _start_comfyui() -> bool:
    global _comfyui_process, _last_gen_time
    if _comfyui_running():
        return True
    print("[ImagineMCP] Starting ComfyUI...")
    _last_gen_time = time.time()
    _comfyui_process = subprocess.Popen(
        [f"{COMFYUI_DIR}/venv/bin/python", "main.py",
         "--listen", "127.0.0.1", "--port", "8188",
         "--output-directory", f"{COMFYUI_DIR}/output"],
        cwd=COMFYUI_DIR,
    )
    for i in range(60):
        if _comfyui_running():
            print(f"[ImagineMCP] ComfyUI ready ({i+1}s)")
            return True
        time.sleep(1)
    print("[ImagineMCP] ComfyUI failed to start")
    return False

def _stop_comfyui():
    global _comfyui_process
    if _comfyui_process:
        print("[ImagineMCP] Stopping ComfyUI (5 min idle)")
        _comfyui_process.terminate()
        _comfyui_process = None
        try:
            http_requests.post(f"{COMFYUI_URL}/api/shutdown", timeout=3)
        except Exception:
            pass

def _idle_watchdog():
    global _last_gen_time
    while True:
        time.sleep(30)
        idle = time.time() - _last_gen_time
        if idle > IDLE_TIMEOUT and _comfyui_running():
            print(f"[ImagineMCP] Idle {int(idle)}s, stopping ComfyUI")
            _stop_comfyui()

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("""
        CREATE TABLE IF NOT EXISTS generations (
            id                TEXT PRIMARY KEY,
            player_id         TEXT NOT NULL,
            prompt            TEXT NOT NULL,
            width             INTEGER DEFAULT 512,
            height            INTEGER DEFAULT 512,
            seed              INTEGER,
            image_path        TEXT,
            status            TEXT DEFAULT 'pending',
            created_at        TEXT DEFAULT (datetime('now')),
            metadata_json     TEXT,
            idempotency_key   TEXT
        )
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_generations_player
        ON generations(player_id)
    """)
    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_generations_idempotency
        ON generations(idempotency_key)
    """)
    db.commit()
    return db

# ---------------------------------------------------------------------------
# Discord webhook
# ---------------------------------------------------------------------------

def send_discord(generation: dict):
    if not DISCORD_WEBHOOK_URL:
        return False
    embed = {
        "title": "🎨 Generation Complete",
        "color": 0x9B59B6,
        "fields": [
            {"name": "Player", "value": generation["player_id"][:12] + "...", "inline": True},
            {"name": "Prompt", "value": generation["prompt"][:200], "inline": False},
            {"name": "Size",   "value": f'{generation["width"]}×{generation["height"]}', "inline": True},
            {"name": "Seed",   "value": str(generation.get("seed", "N/A")), "inline": True},
            {"name": "Created", "value": generation["created_at"], "inline": False},
        ],
        "footer": {"text": f"Generation {generation['id'][:8]}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    image_path = generation.get("image_path")
    if image_path and os.path.exists(image_path):
        try:
            with open(image_path, "rb") as f:
                resp = http_requests.post(
                    DISCORD_WEBHOOK_URL,
                    data={"payload_json": json.dumps({"embeds": [embed]})},
                    files={"file": (Path(image_path).name, f, "image/png")},
                    timeout=15,
                )
            return resp.status_code in (200, 204)
        except Exception as e:
            print(f"[ImagineMCP] Discord webhook (with image) failed: {e}")
            return False

    try:
        resp = http_requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=15)
        return resp.status_code in (200, 204)
    except Exception as e:
        print(f"[ImagineMCP] Discord webhook failed: {e}")
        return False

# ---------------------------------------------------------------------------
# ComfyUI helpers
# ---------------------------------------------------------------------------

def build_workflow(prompt: str, seed: int, gen_id: str,
                   width: int = DEFAULT_WIDTH,
                   height: int = DEFAULT_HEIGHT) -> dict:
    return {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": MODEL_UNET,
            "weight_dtype": "fp8_e4m3fn",
        }},
        "2": {"class_type": "CLIPLoaderGGUF", "inputs": {
            "clip_name": MODEL_CLIP,
            "type": "flux2",
        }},
        "3": {"class_type": "VAELoader", "inputs": {
            "vae_name": MODEL_VAE,
        }},
        "4": {"class_type": "CLIPTextEncode", "inputs": {
            "text": prompt,
            "clip": ["2", 0],
        }},
        "5": {"class_type": "CLIPTextEncode", "inputs": {
            "text": "blurry, low quality, distorted, ugly, deformed, text, watermark, signature, realistic, 3D, modern, smooth shading",
            "clip": ["2", 0],
        }},
        "6": {"class_type": "EmptyLatentImage", "inputs": {
            "width": width, "height": height, "batch_size": 1,
        }},
        "7": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": STEPS, "cfg": CFG,
            "sampler_name": "euler", "scheduler": "normal",
            "denoise": 1,
            "model": ["1", 0], "positive": ["4", 0],
            "negative": ["5", 0], "latent_image": ["6", 0],
        }},
        "8": {"class_type": "VAEDecode", "inputs": {
            "samples": ["7", 0], "vae": ["3", 0],
        }},
        "9": {"class_type": "SaveImage", "inputs": {
            "filename_prefix": f"designer_{gen_id}",
            "images": ["8", 0],
        }},
    }

def queue_prompt(workflow: dict) -> dict | None:
    try:
        resp = http_requests.post(
            f"{COMFYUI_URL}/api/prompt",
            json={"prompt": workflow, "client_id": "designer-mcp"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[ImagineMCP] Queue prompt failed: {e}")
        return None

def wait_for_image(prompt_id: str, timeout_secs: int = 120) -> str | None:
    for _ in range(timeout_secs * 2):
        try:
            resp = http_requests.get(
                f"{COMFYUI_URL}/api/history/{prompt_id}",
                timeout=5,
            )
            if resp.status_code == 200:
                history = resp.json()
                if prompt_id in history:
                    outputs = history[prompt_id].get("outputs", {})
                    for node_id, node_out in outputs.items():
                        for img in node_out.get("images", []):
                            filename = img["filename"]
                            subfolder = img.get("subfolder", "")
                            return str(Path(OUTPUT_DIR) / subfolder / filename)
        except Exception:
            pass
        time.sleep(0.5)
    return None

# ---------------------------------------------------------------------------
# Background generation worker
# ---------------------------------------------------------------------------

def _run_generation(gen_id: str, prompt: str, seed: int,
                    width: int, height: int):
    """Run a generation in a background thread and update the DB when done."""
    db = get_db()
    try:
        wf = build_workflow(prompt, seed, gen_id, width, height)
        result = queue_prompt(wf)
        if not result:
            db.execute("UPDATE generations SET status = 'error', metadata_json = ? WHERE id = ?",
                       (json.dumps({"error": "Failed to queue prompt"}), gen_id))
            db.commit()
            return

        prompt_id = result.get("prompt_id", "")
        image_path = wait_for_image(prompt_id)

        if image_path:
            db.execute(
                "UPDATE generations SET status = 'completed', image_path = ? WHERE id = ?",
                (image_path, gen_id),
            )
            db.commit()

            generation = dict(db.execute(
                "SELECT * FROM generations WHERE id = ?", (gen_id,)
            ).fetchone())

            try:
                send_discord(generation)
            except Exception:
                pass

            _last_gen_time = time.time()
            print(f"[ImagineMCP] Generation {gen_id[:8]} completed: {Path(image_path).name}")
        else:
            db.execute("UPDATE generations SET status = 'timeout' WHERE id = ?", (gen_id,))
            db.commit()
            _last_gen_time = time.time()
            print(f"[ImagineMCP] Generation {gen_id[:8]} timed out")
    except Exception as e:
        print(f"[ImagineMCP] Generation {gen_id[:8]} failed: {e}")
        try:
            db.execute("UPDATE generations SET status = 'error', metadata_json = ? WHERE id = ?",
                       (json.dumps({"error": str(e)}), gen_id))
            db.commit()
        except Exception:
            pass
    finally:
        db.close()


def _generation_worker():
    """Background worker: processes queued generations one at a time."""
    while True:
        gen_id = _generation_queue.get()
        try:
            db = get_db()
            row = db.execute(
                "SELECT * FROM generations WHERE id = ?", (gen_id,)
            ).fetchone()
            db.close()
            if not row:
                continue
            gen = dict(row)
            _run_generation(
                gen_id=gen["id"],
                prompt=gen["prompt"],
                seed=gen["seed"],
                width=gen["width"],
                height=gen["height"],
            )
        except Exception as e:
            print(f"[ImagineMCP] Worker error for {gen_id[:8]}: {e}")


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "Imagine",
    instructions=(
        "Generate images using a local ComfyUI model. "
        "Defaults to 512x512, 4 steps. Returns a job ID immediately — poll "
        "get_generation_status for the result. "
        "The image is auto-sent to the conversation when ready."
    ),
    streamable_http_path="/mcp",
    json_response=True,
)

@mcp.tool()
def generate_image(
    ctx: Context,
    prompt: str,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    idempotency_key: str = "",
) -> str:
    """
    Generate an image from a text prompt using ComfyUI.

    Starts generation and returns a job ID immediately. Poll
    get_generation_status(job_id) to check when the image is ready
    (typically 3-15 seconds). The image is auto-sent to the conversation
    when get_generation_status returns 'completed'.

    Provide an idempotency_key to safely retry without creating duplicates.
    If the same key was used before, returns the cached result.

    Defaults to 512x512, 4 sampling steps.
    """
    # 1. Get player identity from MCP handshake
    player_id = "unknown"
    try:
        session = ctx.session
        if session and session.client_params and session.client_params.clientInfo:
            client_info = session.client_params.clientInfo
            if hasattr(client_info, "player_id") and client_info.player_id:
                player_id = client_info.player_id
    except Exception as e:
        print(f"[ImagineMCP] Could not get player_id from session: {e}")

    # 2. Check idempotency key — return cached result if exists
    if idempotency_key:
        db = get_db()
        existing = db.execute(
            "SELECT * FROM generations WHERE idempotency_key = ? AND player_id = ?",
            (idempotency_key, player_id),
        ).fetchone()
        if existing:
            result = dict(existing)
            if result.get("image_path"):
                result["image_url"] = f"{PUBLIC_URL_BASE}/images/{Path(result['image_path']).name}"
            result["was_cached"] = True
            db.close()
            return json.dumps(result, default=str)
        db.close()

    # 3. Check backend readiness — return retry signal if not ready
    if not _comfyui_running():
        return json.dumps({
            "error_type": "service_unavailable",
            "error": "Generation backend is starting up",
            "estimated_wait_seconds": 15,
            "retry_suggestion": "Call generate_image again in a few seconds",
        })

    # 4. (No concurrency check — all requests accepted, queued for background worker)

    # 5. Create DB record
    gen_id = uuid.uuid4().hex[:16]
    seed = int(time.time() * 1000) % (2**31)

    db = get_db()
    try:
        db.execute(
            "INSERT INTO generations (id, player_id, prompt, width, height, seed, status, metadata_json, idempotency_key) "
            "VALUES (?, ?, ?, ?, ?, ?, 'processing', ?, ?)",
            (gen_id, player_id, prompt, width, height, seed,
             json.dumps({"source": "imagine_mcp"}), idempotency_key or None),
        )
        db.commit()

        # 6. Queue for background worker (processes one at a time)
        _generation_queue.put(gen_id)

        return json.dumps({
            "job_id": gen_id,
            "status": "processing",
            "estimated_seconds": 15,
        })
    finally:
        db.close()


@mcp.tool()
def get_generation_status(ctx: Context, generation_id: str) -> str:
    """
    Poll generation status by job ID.

    Returns the current status ('processing', 'completed', 'error', 'timeout').
    When completed, includes the image_url and generation metadata.
    Call this after generate_image() to check when the image is ready.
    """
    db = get_db()
    row = db.execute(
        "SELECT * FROM generations WHERE id = ?",
        (generation_id,),
    ).fetchone()
    db.close()

    if not row:
        return json.dumps({"error": f"Generation {generation_id} not found"})

    result = dict(row)
    if result.get("image_path"):
        result["image_url"] = f"{PUBLIC_URL_BASE}/images/{Path(result['image_path']).name}"
    return json.dumps(result, default=str)


@mcp.tool()
def get_generation(ctx: Context, generation_id: str) -> str:
    """
    Retrieve a past generation by its ID.

    Returns the prompt, image path, seed, status, and timestamps.
    """
    db = get_db()
    row = db.execute("SELECT * FROM generations WHERE id = ?", (generation_id,)).fetchone()
    db.close()

    if not row:
        return json.dumps({"error": f"Generation {generation_id} not found"})

    result = dict(row)
    if result.get("image_path"):
        result["image_url"] = f"{PUBLIC_URL_BASE}/images/{Path(result['image_path']).name}"
    return json.dumps(result, default=str)


@mcp.tool()
def list_generations(ctx: Context, limit: int = 10, since: str = "") -> str:
    """
    List recent generations for the current player.

    Returns the most recent generations first, newest first.
    Optionally filter by ISO 8601 timestamp (since) to only return
    generations created after that time.
    """
    player_id = "unknown"
    try:
        session = ctx.session
        if session and session.client_params and session.client_params.clientInfo:
            client_info = session.client_params.clientInfo
            if hasattr(client_info, "player_id") and client_info.player_id:
                player_id = client_info.player_id
    except Exception as e:
        print(f"[ImagineMCP] Could not get player_id: {e}")

    db = get_db()
    if since:
        rows = db.execute(
            "SELECT * FROM generations WHERE player_id = ? AND created_at > ? ORDER BY created_at DESC LIMIT ?",
            (player_id, since, limit),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM generations WHERE player_id = ? ORDER BY created_at DESC LIMIT ?",
            (player_id, limit),
        ).fetchall()
    db.close()

    results = []
    for row in rows:
        r = dict(row)
        if r.get("image_path"):
            r["image_url"] = f"{PUBLIC_URL_BASE}/images/{Path(r['image_path']).name}"
        results.append(r)
    return json.dumps(results, default=str)


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Check Authorization: Bearer *** on every request except OPTIONS."""

    async def dispatch(self, request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        if request.url.path.startswith("/images/"):
            return await call_next(request)

        if request.url.path == "/health":
            return await call_next(request)

        if not BEARER_TOKEN:
            return await call_next(request)

        auth_header = request.headers.get("x-api-key", "") or request.headers.get("authorization", "")

        if auth_header.startswith("Bearer "):
            token = auth_header.removeprefix("Bearer ")
        else:
            token = auth_header

        if not token:
            return JSONResponse(
                {"error": "Missing or invalid Authorization header"},
                status_code=401,
            )

        if token != BEARER_TOKEN:
            return JSONResponse(
                {"error": "Invalid token"},
                status_code=403,
            )

        return await call_next(request)


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

from starlette.routing import Route

async def _health_endpoint(request):
    """Server health check. Excluded from auth."""
    backend_ready = _comfyui_running()
    return JSONResponse({
        "status": "ok" if backend_ready else "warming",
        "backend_ready": backend_ready,
        "active_jobs": _generation_queue.qsize(),
        "max_concurrent": 1,
        "uptime_seconds": int(time.time() - _backend_start_time),
    })

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Mount
    from starlette.staticfiles import StaticFiles

    # Start idle watchdog
    threading.Thread(target=_idle_watchdog, daemon=True).start()

    # Warm-start: begin ComfyUI startup immediately (doesn't block uvicorn)
    threading.Thread(target=_start_comfyui, daemon=True).start()

    # Background worker: processes queued generations one at a time
    threading.Thread(target=_generation_worker, daemon=True).start()

    _backend_start_time = time.time()

    # MCP server on port 8003
    mcp_asgi = mcp.streamable_http_app()
    mcp_asgi.add_middleware(BearerAuthMiddleware)

    # Static image server on port 8004
    images_app = StaticFiles(directory=OUTPUT_DIR, check_dir=False)
    images_asgi = Starlette(routes=[
        Mount("/images", app=images_app),
    ])

    def run_mcp():
        uvicorn.run(mcp_asgi, host=HOST, port=PORT, log_level="info")

    def run_images():
        uvicorn.run(images_asgi, host=HOST, port=PORT + 1, log_level="info")

    t1 = threading.Thread(target=run_mcp, daemon=True)
    t2 = threading.Thread(target=run_images, daemon=True)
    t1.start()
    t2.start()

    # Health endpoint — simple HTTP server on port 8005
    health_app = Starlette(routes=[
        Route("/health", endpoint=_health_endpoint),
    ])

    def run_health():
        uvicorn.run(health_app, host=HOST, port=PORT + 2, log_level="info")

    t3 = threading.Thread(target=run_health, daemon=True)
    t3.start()

    print(f"[ImagineMCP] Starting servers...")
    print(f"[ImagineMCP]   MCP:     http://{HOST}:{PORT}/mcp")
    print(f"[ImagineMCP]   Images:  http://{HOST}:{PORT+1}/images")
    print(f"[ImagineMCP]   Health:  http://{HOST}:{PORT+2}/health")

    t1.join()
