from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import shutil
import socket
import ipaddress
import sqlite3
import sys
import json
import secrets
import string
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, quote

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ================================================================
# RGNODES™ VPS Management Bot — hardened/stable build
# UI/command names are intentionally kept compatible with the build
# supplied by the user.
# ================================================================

load_dotenv()


def env_int(name: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def normalize_bot_token(raw: str | None) -> str:
    """Return a clean Discord bot token without exposing it in logs."""
    token = str(raw or "").strip()
    if not token:
        return ""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
        token = token[1:-1].strip()
    if token.lower().startswith("bot "):
        token = token[4:].strip()
    token = token.replace("\r", "").replace("\n", "").strip()
    return token


def load_discord_token() -> tuple[str, str]:
    """Return (token, source), supporting common environment variable names."""
    for name in ("TOKEN", "DISCORD_TOKEN", "BOT_TOKEN"):
        token = normalize_bot_token(os.getenv(name))
        if token:
            return token, name
    return "", "none"


TOKEN, TOKEN_SOURCE = load_discord_token()
ADMIN_ID = env_int("ADMIN_ID", 0, 0)
DATABASE_FILE = os.getenv("DATABASE_FILE", "vps_bot.db").strip() or "vps_bot.db"
LOG_FILE = os.getenv("LOG_FILE", "vps_bot.log").strip() or "vps_bot.log"
BOT_STATUS_NAME = os.getenv("BOT_STATUS_NAME", "RGNODES™ VPS Management").strip() or "RGNODES™ VPS Management"
PREFIX = (os.getenv("PREFIX") or "!").strip() or "!"
VPS_HOSTNAME_PREFIX = os.getenv("VPS_HOSTNAME_PREFIX", "rgnodes").strip() or "rgnodes"
DEFAULT_RAM = os.getenv("DEFAULT_RAM", "4G").strip() or "4G".strip() or "2g"
DEFAULT_CPU = os.getenv("DEFAULT_CPU", "1").strip() or "1".strip() or "1"
DEFAULT_DISK = os.getenv("DEFAULT_DISK", "10G").strip() or "10G".strip() or "10g"
DEFAULT_LOCATION = os.getenv("DEFAULT_LOCATION", "SG").strip().upper() or "SG"
if DEFAULT_LOCATION not in {"SG", "IN"}:
    DEFAULT_LOCATION = "SG"
SERVER_LIMIT = env_int("SERVER_LIMIT", 1, 1, 100)
TOTAL_RUNNING_LIMIT = env_int("TOTAL_RUNNING_LIMIT", 50, 1, 10_000)
ADMIN_BYPASS_LIMITS = env_bool("ADMIN_BYPASS_LIMITS", True)
STATUS_INTERVAL = env_int("STATUS_INTERVAL", 45, 15, 300)
DOCKER_TIMEOUT = env_int("DOCKER_TIMEOUT", 120, 30, 900)
ACCESS_TIMEOUT = env_int("ACCESS_TIMEOUT", 120, 30, 300)
DEPLOY_TIMEOUT = env_int("DEPLOY_TIMEOUT", 600, 120, 840)
IMAGE_PULL_TIMEOUT = env_int("IMAGE_PULL_TIMEOUT", 300, 60, 600)
INTERACTION_LOG_UNKNOWN_AS_DEBUG = env_bool("INTERACTION_LOG_UNKNOWN_AS_DEBUG", True)
ENABLE_HARD_DISK_QUOTA = env_bool("ENABLE_HARD_DISK_QUOTA", False)
QUOTA_FALLBACK = env_bool("QUOTA_FALLBACK", True)
DOCKER_FEATURE_FALLBACK = env_bool("DOCKER_FEATURE_FALLBACK", True)
DOCKER_RETRIES = env_int("DOCKER_RETRIES", 2, 0, 5)
STATUS_CONCURRENCY = env_int("STATUS_CONCURRENCY", 5, 1, 25)
MEMORY_RESERVATION_PERCENT = env_int("MEMORY_RESERVATION_PERCENT", 75, 0, 100)
DISABLE_CONTAINER_SWAP = env_bool("DISABLE_CONTAINER_SWAP", True)
HOST_TOTAL_RAM = os.getenv("HOST_TOTAL_RAM", "64G").strip() or "64G"
HOST_TOTAL_CPU = env_int("HOST_TOTAL_CPU", 10, 1, 256)
HOST_TOTAL_DISK = os.getenv("HOST_TOTAL_DISK", "10T").strip() or "10T"
MAX_PORTS_PER_VPS = env_int("MAX_PORTS_PER_VPS", 10, 1, 50)
PORT_RANGE_START = env_int("PORT_RANGE_START", 20000, 1024, 65534)
PORT_RANGE_END = env_int("PORT_RANGE_END", 40000, 1025, 65535)
PORT_SUPERVISOR_INTERVAL = env_int("PORT_SUPERVISOR_INTERVAL", 20, 5, 120)
PUBLIC_IP_REFRESH = env_int("PUBLIC_IP_REFRESH", 300, 60, 3600)
REAL_LOCATION_REFRESH = env_int("REAL_LOCATION_REFRESH", 900, 120, 7200)
IPV4_MODE = os.getenv("IPV4_MODE", "shared").strip().lower() or "shared"
if IPV4_MODE not in {"shared"}:
    IPV4_MODE = "shared"
REQUIRE_REAL_PUBLIC_IPV4 = env_bool("REQUIRE_REAL_PUBLIC_IPV4", True)
IPV4_REFRESH = env_int("IPV4_REFRESH", 300, 30, 3600)

# Hosting-platform health/keep-alive HTTP listener. Most platforms (Render,
# Railway, etc.) provide PORT automatically; locally it falls back to 8080.
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0").strip() or "0.0.0.0"
WEB_PORT = env_int("PORT", 8080, 1, 65535)
WEB_PATH = os.getenv("WEB_PATH", "/").strip() or "/"

# VPS backend selection:
#   docker        -> native Docker backend (default, works without Pterodactyl)
#   pterodactyl   -> create/control servers through Pterodactyl Application API
#   auto          -> use Pterodactyl when fully configured, otherwise Docker
VPS_BACKEND = os.getenv("VPS_BACKEND", "auto").strip().lower() or "auto"
if VPS_BACKEND not in {"docker", "pterodactyl", "auto"}:
    VPS_BACKEND = "auto"

PTERO_URL = os.getenv("PTERO_URL", "").strip().rstrip("/")
PTERO_API_KEY = os.getenv("PTERO_API_KEY", "").strip()
PTERO_CLIENT_API_KEY = os.getenv("PTERO_CLIENT_API_KEY", "").strip()
PTERO_PANEL_PUBLIC_URL = os.getenv("PTERO_PANEL_PUBLIC_URL", PTERO_URL).strip().rstrip("/")
PTERO_NODE_ID = env_int("PTERO_NODE_ID", 0, 0)
PTERO_NEST_ID = env_int("PTERO_NEST_ID", 0, 0)
PTERO_EGG_ID = env_int("PTERO_EGG_ID", 0, 0)
PTERO_ALLOCATION_ID = env_int("PTERO_ALLOCATION_ID", 0, 0)
PTERO_DEFAULT_USER_ID = env_int("PTERO_DEFAULT_USER_ID", 0, 0)
PTERO_AUTO_CREATE_USERS = env_bool("PTERO_AUTO_CREATE_USERS", True)
PTERO_MEMORY_SWAP = env_int("PTERO_MEMORY_SWAP", 0)
PTERO_IO = env_int("PTERO_IO", 500, 10, 1000)
PTERO_DATABASES = env_int("PTERO_DATABASES", 0, 0, 100)
PTERO_ALLOCATIONS = env_int("PTERO_ALLOCATIONS", 1, 1, 100)
PTERO_BACKUPS = env_int("PTERO_BACKUPS", 5, 0, 100)
PTERO_DOCKER_IMAGE = os.getenv("PTERO_DOCKER_IMAGE", "").strip()
PTERO_STARTUP = os.getenv("PTERO_STARTUP", "").strip()
PTERO_SKIP_SCRIPTS = env_bool("PTERO_SKIP_SCRIPTS", False)
PTERO_ENVIRONMENT_JSON = os.getenv("PTERO_ENVIRONMENT_JSON", "{}").strip() or "{}"
try:
    PTERO_ENVIRONMENT = json.loads(PTERO_ENVIRONMENT_JSON)
    if not isinstance(PTERO_ENVIRONMENT, dict):
        PTERO_ENVIRONMENT = {}
except (TypeError, ValueError, json.JSONDecodeError):
    PTERO_ENVIRONMENT = {}

def ptero_application_configured() -> bool:
    return bool(
        PTERO_URL and PTERO_API_KEY and PTERO_DEFAULT_USER_ID
        and PTERO_NODE_ID and PTERO_NEST_ID and PTERO_EGG_ID
        and PTERO_ALLOCATION_ID
    )


def ptero_client_configured() -> bool:
    return bool(PTERO_URL and PTERO_CLIENT_API_KEY)


def ptero_configured() -> bool:
    return ptero_application_configured() and ptero_client_configured()

def active_backend() -> str:
    if VPS_BACKEND == "pterodactyl":
        return "pterodactyl"
    if VPS_BACKEND == "auto" and ptero_configured():
        return "pterodactyl"
    return "docker"

if not WEB_PATH.startswith("/"):
    WEB_PATH = "/" + WEB_PATH

LOCATION_CONFIG = {
    "SG": {"label": "Singapore 🇸🇬", "short": "SG"},
    "IN": {"label": "India 🇮🇳", "short": "IN"},
}

OS_CONFIG = {
    "ubuntu-26.04": {"label": "Ubuntu 26.04 LTS", "image": "ubuntu:26.04"},
    "ubuntu-24.04": {"label": "Ubuntu 24.04 LTS", "image": "ubuntu:24.04"},
    "ubuntu-22.04": {"label": "Ubuntu 22.04 LTS", "image": "ubuntu:22.04"},
    "debian-12": {"label": "Debian 12", "image": "debian:12"},
    "debian-11": {"label": "Debian 11", "image": "debian:11"},
}

LOCATION_ALIASES = {
    "sg": "SG",
    "singapore": "SG",
    "in": "IN",
    "india": "IN",
}

OS_ALIASES = {
    "ubuntu": "ubuntu-24.04", "ubuntu26": "ubuntu-26.04", "ubuntu26.04": "ubuntu-26.04", "ubuntu-26.04": "ubuntu-26.04",
    "ubuntu24": "ubuntu-24.04", "ubuntu24.04": "ubuntu-24.04", "ubuntu-24.04": "ubuntu-24.04",
    "ubuntu22": "ubuntu-22.04", "ubuntu22.04": "ubuntu-22.04", "ubuntu-22.04": "ubuntu-22.04",
    "debian": "debian-12", "debian12": "debian-12", "debian-12": "debian-12",
    "debian11": "debian-11", "debian-11": "debian-11",
}

ACCESS_URL_RE = re.compile(r"https://sshx\.io/s/[^\s<>\]\[\"'`]+", re.IGNORECASE)
Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("rgnodes")


# ================================================================
# Lightweight 24/7 HTTP health/landing server
# ================================================================
# This server intentionally uses only Python's asyncio standard library so the
# existing dependency set does not need Flask/FastAPI/aiohttp. It runs in the
# same event loop as discord.py and therefore stays alive for the full process
# lifetime. Hosting platforms can probe the assigned PORT and receive HTTP 200.
WEB_SERVER: asyncio.AbstractServer | None = None
WEB_SERVER_TASK: asyncio.Task[None] | None = None
WEB_STARTED_AT = datetime.now(timezone.utc)


def _html_escape(value: Any) -> str:
    text = str(value)
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
                .replace("'", "&#39;"))


def _health_html() -> bytes:
    discord_online = bool(bot.is_ready()) if "bot" in globals() else False
    bot_name = str(bot.user) if discord_online and getattr(bot, "user", None) else "Starting…"
    state = "Online" if discord_online else "Starting"
    started = WEB_STARTED_AT.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    body = f"""<!doctype html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<meta http-equiv=\"refresh\" content=\"30\">
<title>RGNODES™ • Bot Status</title>
<style>
body{{margin:0;min-height:100vh;background:#111318;color:#f5f7fa;font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;display:grid;place-items:center}}
.card{{width:min(680px,calc(100% - 40px));padding:34px;border:1px solid #2a2e38;border-radius:20px;background:#181b22;box-shadow:0 20px 70px rgba(0,0,0,.35)}}
.badge{{display:inline-flex;align-items:center;gap:8px;padding:7px 12px;border-radius:999px;background:#20252e;font-size:14px}}
.dot{{width:9px;height:9px;border-radius:50%;background:#48d597;box-shadow:0 0 14px #48d597}}
h1{{margin:18px 0 10px;font-size:34px}}
p{{color:#aeb6c4;line-height:1.6}}
.row{{display:flex;justify-content:space-between;gap:18px;margin-top:22px;padding-top:18px;border-top:1px solid #2a2e38}}
code{{color:#dfe5ee}}
</style>
</head>
<body>
<main class=\"card\">
<div class=\"badge\"><span class=\"dot\"></span>Bot is <strong>{_html_escape(state)}</strong></div>
<h1>RGNODES™ Bot is online…</h1>
<p>The 24/7 health endpoint is running and ready for hosting-platform health checks. Opening or pinging this port confirms the process is serving HTTP.</p>
<div class=\"row\"><span>Status</span><strong>{_html_escape(state)}</strong></div>
<div class=\"row\"><span>Discord</span><strong>{_html_escape(bot_name)}</strong></div>
<div class=\"row\"><span>Started</span><code>{_html_escape(started)}</code></div>
</main>
</body>
</html>
"""
    return body.encode("utf-8")


async def _http_health_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    try:
        # Read only the request headers, with a hard cap to avoid oversized or
        # slowloris-style requests taking resources from the Discord bot.
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
            return
        if len(request) > 16 * 1024:
            return

        first_line = request.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        parts = first_line.split()
        method = parts[0].upper() if parts else ""
        target = parts[1] if len(parts) >= 2 else "/"
        path = target.split("?", 1)[0].split("#", 1)[0]

        if method not in {"GET", "HEAD"}:
            payload = b"Method Not Allowed\n"
            headers = (
                b"HTTP/1.1 405 Method Not Allowed\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                + f"Content-Length: {len(payload)}\r\n".encode()
                + b"Connection: close\r\n\r\n"
            )
        elif path in {"/", WEB_PATH, "/health", "/healthz", "/ping"}:
            if path in {"/health", "/healthz"}:
                discord_online = bool(bot.is_ready())
                payload = (
                    ("{\"status\":\"online\",\"discord_ready\":" + ("true" if discord_online else "false") + "}")
                    .encode("utf-8")
                )
                content_type = b"application/json; charset=utf-8"
            else:
                payload = _health_html()
                content_type = b"text/html; charset=utf-8"
            headers = (
                b"HTTP/1.1 200 OK\r\n"
                + b"Content-Type: " + content_type + b"\r\n"
                + f"Content-Length: {len(payload)}\r\n".encode()
                + b"Cache-Control: no-store, no-cache, must-revalidate\r\n"
                + b"Connection: close\r\n\r\n"
            )
        else:
            payload = b"Not Found\n"
            headers = (
                b"HTTP/1.1 404 Not Found\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                + f"Content-Length: {len(payload)}\r\n".encode()
                + b"Connection: close\r\n\r\n"
            )

        writer.write(headers)
        if method != "HEAD":
            writer.write(payload)
        await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass
    except Exception as exc:
        logger.debug("Health client error from %s: %s", peer, safe_log(exc))
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def start_health_server() -> asyncio.AbstractServer | None:
    global WEB_SERVER, WEB_SERVER_TASK
    if WEB_SERVER is not None:
        return WEB_SERVER
    try:
        WEB_SERVER = await asyncio.start_server(
            _http_health_client,
            host=WEB_HOST,
            port=WEB_PORT,
            limit=16 * 1024,
            reuse_address=True,
        )
        WEB_SERVER_TASK = asyncio.create_task(
            WEB_SERVER.serve_forever(),
            name="rgnodes-http-health",
        )
        sockets = WEB_SERVER.sockets or []
        bound = ", ".join(str(sock.getsockname()) for sock in sockets) or f"{WEB_HOST}:{WEB_PORT}"
        logger.info("24/7 HTTP health server online at %s | GET / or /health", bound)
        return WEB_SERVER
    except (OSError, asyncio.CancelledError) as exc:
        WEB_SERVER = None
        WEB_SERVER_TASK = None
        if isinstance(exc, asyncio.CancelledError):
            raise
        logger.error("Could not start HTTP health server on %s:%s: %s", WEB_HOST, WEB_PORT, safe_log(exc))
        return None


async def stop_health_server() -> None:
    global WEB_SERVER, WEB_SERVER_TASK
    server, task = WEB_SERVER, WEB_SERVER_TASK
    WEB_SERVER = None
    WEB_SERVER_TASK = None
    if server is not None:
        server.close()
        with contextlib.suppress(Exception):
            await server.wait_closed()
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def safe_log(value: Any, limit: int = 1800) -> str:
    text = str(value)
    text = re.sub(r"https://sshx\.io/s/\S+", "<private-console-url>", text, flags=re.I)
    text = re.sub(r"Bearer\s+\S+", "Bearer <redacted>", text, flags=re.I)
    text = re.sub(r"ssh\s+\S+@\S+", "ssh <redacted>", text, flags=re.I)
    return text[:max(1, int(limit))]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_os(value: str | None) -> str | None:
    return OS_ALIASES.get((value or "").strip().lower())


def os_label(value: str | None) -> str:
    normalized = normalize_os(value) or (value or "Unknown")
    return OS_CONFIG.get(normalized, {"label": normalized})["label"]


def normalize_location(value: str | None) -> str | None:
    raw = (value or "").strip().lower()
    if raw in LOCATION_ALIASES:
        return LOCATION_ALIASES[raw]
    key = raw.upper()
    return key if key in LOCATION_CONFIG else None


def location_label(value: str | None) -> str:
    return LOCATION_CONFIG[(normalize_location(value) or DEFAULT_LOCATION)]["label"]


def clean(value: Any, limit: int = 1024) -> str:
    return str(value if value is not None else "N/A").replace("`", "'")[:limit]


def parse_size_bytes(value: str) -> int:
    text = str(value or "").strip().lower().replace(" ", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(b|kb|k|mb|m|gb|g|tb|t)?", text)
    if not match:
        raise ValueError(f"Invalid resource value: {value}")
    amount = float(match.group(1))
    unit = match.group(2) or "g"
    multiplier = {"b": 1, "k": 1024, "kb": 1024, "m": 1024**2, "mb": 1024**2, "g": 1024**3, "gb": 1024**3, "t": 1024**4, "tb": 1024**4}[unit]
    return int(amount * multiplier)


def format_bytes(value: int) -> str:
    if value < 1024**2:
        return f"{value / 1024:.0f}KB"
    if value < 1024**3:
        return f"{value / 1024**2:.1f}MB"
    return f"{value / 1024**3:.2f}GB"


def validate_resources(ram: str, cpu: str, disk: str) -> tuple[str, str, str]:
    try:
        ram_bytes = parse_size_bytes(ram)
        disk_bytes = parse_size_bytes(disk)
        cpu_value = float(str(cpu).strip())
    except (TypeError, ValueError):
        raise ValueError("RAM, CPU, or disk format is invalid. Example: `2g`, `2`, `10g`.")
    if not 256 * 1024**2 <= ram_bytes <= 256 * 1024**3:
        raise ValueError("RAM must be between 256MB and 256GB.")
    if not 1 * 1024**3 <= disk_bytes <= 10 * 1024**4:
        raise ValueError("Disk must be between 1GB and 10TB.")
    if not 0 < cpu_value <= 64:
        raise ValueError("CPU must be greater than 0 and no more than 64 cores.")
    ram = str(ram).strip().lower()
    cpu = f"{cpu_value:g}"
    disk = str(disk).strip().lower()
    return ram, cpu, disk


# ================================================================
# SQLite — serialized writes + resilient migration
# ================================================================

DB_WRITE_LOCK = asyncio.Lock()


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_FILE, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    conn = db_connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                container_id TEXT UNIQUE NOT NULL,
                container_name TEXT NOT NULL,
                os_type TEXT NOT NULL,
                location TEXT NOT NULL DEFAULT 'SG',
                hostname TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'stopped',
                ssh_command TEXT,
                ram TEXT NOT NULL DEFAULT '2g',
                cpu TEXT NOT NULL DEFAULT '1',
                disk TEXT NOT NULL DEFAULT '10g',
                suspended INTEGER NOT NULL DEFAULT 0,
                sshx_url TEXT,
                sshx_pid TEXT,
                public_ipv4 TEXT,
                ipv4_verified_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vps_shares (
                vps_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                shared_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (vps_id, user_id),
                FOREIGN KEY(vps_id) REFERENCES vps(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bans (
                user_id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_slots (
                user_id INTEGER PRIMARY KEY,
                slots INTEGER NOT NULL DEFAULT 1 CHECK(slots > 0),
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vps_ports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vps_id INTEGER NOT NULL,
                container_port INTEGER NOT NULL,
                host_port INTEGER NOT NULL UNIQUE,
                protocol TEXT NOT NULL DEFAULT 'tcp',
                target_ip TEXT,
                pid INTEGER,
                status TEXT NOT NULL DEFAULT 'stopped',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(vps_id, container_port, protocol),
                FOREIGN KEY(vps_id) REFERENCES vps(id) ON DELETE CASCADE
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS vps_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vps_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                image_ref TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(vps_id, name),
                FOREIGN KEY(vps_id) REFERENCES vps(id) ON DELETE CASCADE
            )
        """)
        def cols(table: str) -> set[str]:
            return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        vps_cols = cols("vps")
        for name, ddl in {
            "location": "ALTER TABLE vps ADD COLUMN location TEXT NOT NULL DEFAULT 'SG'",
            "sshx_url": "ALTER TABLE vps ADD COLUMN sshx_url TEXT",
            "sshx_pid": "ALTER TABLE vps ADD COLUMN sshx_pid TEXT",
            "public_ipv4": "ALTER TABLE vps ADD COLUMN public_ipv4 TEXT",
            "ipv4_verified_at": "ALTER TABLE vps ADD COLUMN ipv4_verified_at TEXT",
            "backend": "ALTER TABLE vps ADD COLUMN backend TEXT NOT NULL DEFAULT 'docker'",
            "ptero_server_id": "ALTER TABLE vps ADD COLUMN ptero_server_id INTEGER",
            "ptero_identifier": "ALTER TABLE vps ADD COLUMN ptero_identifier TEXT",
            "ptero_user_id": "ALTER TABLE vps ADD COLUMN ptero_user_id INTEGER",
        }.items():
            if name not in vps_cols:
                conn.execute(ddl)

        share_cols = cols("vps_shares")
        if "shared_by" not in share_cols:
            conn.execute("ALTER TABLE vps_shares ADD COLUMN shared_by INTEGER")
        if "created_at" not in share_cols:
            conn.execute("ALTER TABLE vps_shares ADD COLUMN created_at TEXT")

        if "updated_at" not in cols("users"):
            conn.execute("ALTER TABLE users ADD COLUMN updated_at TEXT")
        if "created_at" not in cols("bans"):
            conn.execute("ALTER TABLE bans ADD COLUMN created_at TEXT")
        now = utc_now()
        conn.execute("UPDATE users SET updated_at=COALESCE(updated_at,created_at,?)", (now,))
        conn.execute("UPDATE vps SET location=COALESCE(location,'SG'),updated_at=COALESCE(updated_at,created_at,?)", (now,))
        conn.execute("UPDATE bans SET created_at=COALESCE(created_at,?)", (now,))
        conn.execute("UPDATE user_slots SET updated_at=COALESCE(updated_at,?)", (now,))
    finally:
        conn.close()


init_db()


def db_upsert_user(user_id: int, username: str) -> None:
    now = utc_now()
    conn = db_connect()
    try:
        conn.execute("""
            INSERT INTO users(user_id,username,created_at,updated_at) VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,updated_at=excluded.updated_at
        """, (user_id, username[:200], now, now))
    finally:
        conn.close()


def db_is_banned(user_id: int) -> bool:
    conn = db_connect()
    try:
        return conn.execute("SELECT 1 FROM bans WHERE user_id=?", (user_id,)).fetchone() is not None
    finally:
        conn.close()


def db_set_ban(user_id: int, banned: bool) -> None:
    conn = db_connect()
    try:
        if banned:
            conn.execute("INSERT OR IGNORE INTO bans(user_id,created_at) VALUES(?,?)", (user_id, utc_now()))
        else:
            conn.execute("DELETE FROM bans WHERE user_id=?", (user_id,))
    finally:
        conn.close()


def db_insert_vps(**data: Any) -> None:
    conn = db_connect()
    try:
        now = utc_now()
        conn.execute("""
            INSERT INTO vps(user_id,container_id,container_name,os_type,location,hostname,status,ram,cpu,disk,sshx_url,sshx_pid,public_ipv4,ipv4_verified_at,created_at,updated_at,backend,ptero_server_id,ptero_identifier,ptero_user_id)
            VALUES(?,?,?,?,? ,?,'running',?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            data["user_id"], data["container_id"], data["container_name"], data["os_type"], data["location"], data["hostname"],
            data["ram"], data["cpu"], data["disk"], data.get("sshx_url"), data.get("sshx_pid"), data.get("public_ipv4"), data.get("ipv4_verified_at"), now, now,
            data.get("backend", "docker"), data.get("ptero_server_id"), data.get("ptero_identifier"), data.get("ptero_user_id"),
        ))
    finally:
        conn.close()


def db_get_vps(vps_id: int) -> sqlite3.Row | None:
    conn = db_connect()
    try:
        return conn.execute("SELECT * FROM vps WHERE id=?", (vps_id,)).fetchone()
    finally:
        conn.close()


def db_get_user_vps(user_id: int) -> list[sqlite3.Row]:
    conn = db_connect()
    try:
        return conn.execute("SELECT * FROM vps WHERE user_id=? ORDER BY id DESC", (user_id,)).fetchall()
    finally:
        conn.close()


def db_get_all_vps() -> list[sqlite3.Row]:
    conn = db_connect()
    try:
        return conn.execute("SELECT * FROM vps ORDER BY id DESC").fetchall()
    finally:
        conn.close()


def db_vps_count(user_id: int) -> int:
    conn = db_connect()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM vps WHERE user_id=?", (user_id,)).fetchone()[0])
    finally:
        conn.close()


def db_running_count() -> int:
    conn = db_connect()
    try:
        return int(conn.execute("SELECT COUNT(*) FROM vps WHERE status='running' AND suspended=0").fetchone()[0])
    finally:
        conn.close()


def db_allocated_resources() -> tuple[int, float, int]:
    """Sum requested resources of all managed VPS records for capacity checks."""
    conn = db_connect()
    try:
        rows = conn.execute("SELECT ram,cpu,disk FROM vps").fetchall()
    finally:
        conn.close()
    ram = cpu = disk = 0.0
    for row in rows:
        try:
            ram += parse_size_bytes(row["ram"])
            disk += parse_size_bytes(row["disk"])
            cpu += float(row["cpu"])
        except (TypeError, ValueError):
            logger.warning("Ignoring malformed resource allocation while checking host capacity")
    return int(ram), cpu, int(disk)


def resource_capacity_error(ram: str, cpu: str, disk: str) -> str | None:
    try:
        request_ram = parse_size_bytes(ram)
        request_cpu = float(cpu)
        request_disk = parse_size_bytes(disk)
        total_ram = parse_size_bytes(HOST_TOTAL_RAM)
        total_disk = parse_size_bytes(HOST_TOTAL_DISK)
    except (TypeError, ValueError):
        return "Host resource capacity configuration is invalid."
    used_ram, used_cpu, used_disk = db_allocated_resources()
    if used_ram + request_ram > total_ram:
        return f"RAM capacity exceeded: requested `{ram}`, allocated `{format_bytes(used_ram)}`, host capacity `{format_bytes(total_ram)}`."
    if used_cpu + request_cpu > HOST_TOTAL_CPU:
        return f"CPU capacity exceeded: requested `{cpu}` core(s), allocated `{used_cpu:g}`, host capacity `{HOST_TOTAL_CPU}`."
    if used_disk + request_disk > total_disk:
        return f"Disk allocation exceeded: requested `{disk}`, allocated `{format_bytes(used_disk)}`, allocation capacity `{format_bytes(total_disk)}`."
    return None


def db_effective_slots(user_id: int) -> int:
    conn = db_connect()
    try:
        row = conn.execute("SELECT slots FROM user_slots WHERE user_id=?", (int(user_id),)).fetchone()
        return max(1, int(row[0])) if row else max(1, SERVER_LIMIT)
    finally:
        conn.close()


def db_add_slots(user_id: int, amount: int) -> int:
    if amount <= 0:
        raise ValueError("Slot amount must be positive.")
    now = utc_now()
    conn = db_connect()
    try:
        row = conn.execute("SELECT slots FROM user_slots WHERE user_id=?", (int(user_id),)).fetchone()
        current = int(row[0]) if row else max(1, SERVER_LIMIT)
        new_total = current + int(amount)
        conn.execute("INSERT INTO user_slots(user_id,slots,updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET slots=excluded.slots,updated_at=excluded.updated_at", (int(user_id), new_total, now))
        return new_total
    finally:
        conn.close()


def db_list_ports(vps_id: int) -> list[sqlite3.Row]:
    conn = db_connect()
    try:
        return conn.execute("SELECT * FROM vps_ports WHERE vps_id=? ORDER BY host_port", (int(vps_id),)).fetchall()
    finally:
        conn.close()


def db_get_port(port_id: int) -> sqlite3.Row | None:
    conn = db_connect()
    try:
        return conn.execute("SELECT * FROM vps_ports WHERE id=?", (int(port_id),)).fetchone()
    finally:
        conn.close()


def db_find_port(vps_id: int, container_port: int, protocol: str = "tcp") -> sqlite3.Row | None:
    conn = db_connect()
    try:
        return conn.execute("SELECT * FROM vps_ports WHERE vps_id=? AND container_port=? AND protocol=?", (int(vps_id), int(container_port), protocol.lower())).fetchone()
    finally:
        conn.close()


def db_insert_port(vps_id: int, container_port: int, host_port: int, protocol: str = "tcp") -> int:
    now = utc_now()
    conn = db_connect()
    try:
        cur = conn.execute("INSERT INTO vps_ports(vps_id,container_port,host_port,protocol,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (int(vps_id), int(container_port), int(host_port), protocol.lower(), "stopped", now, now))
        return int(cur.lastrowid)
    finally:
        conn.close()


def db_update_port(port_id: int, **fields: Any) -> None:
    allowed = {"target_ip", "pid", "status"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    assignments = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [utc_now(), int(port_id)]
    conn = db_connect()
    try:
        conn.execute(f"UPDATE vps_ports SET {assignments},updated_at=? WHERE id=?", values)
    finally:
        conn.close()


def db_delete_port(port_id: int) -> None:
    conn = db_connect()
    try:
        conn.execute("DELETE FROM vps_ports WHERE id=?", (int(port_id),))
    finally:
        conn.close()


def db_delete_all_vps() -> None:
    conn = db_connect()
    try:
        conn.execute("DELETE FROM vps")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='vps'")
        conn.execute("DELETE FROM sqlite_sequence WHERE name='vps_ports'")
    finally:
        conn.close()


def db_find_vps(user_id: int, identifier: str | None, admin: bool = False) -> sqlite3.Row | None:
    rows = db_get_all_vps() if admin else db_get_user_vps(user_id)
    needle = (identifier or "").strip().lower()
    if not needle:
        return rows[0] if rows else None
    exact = [r for r in rows if needle in {str(r["id"]).lower(), str(r["container_id"]).lower(), str(r["container_name"]).lower()}]
    if exact:
        return exact[0]
    partial = [r for r in rows if needle in str(r["container_id"]).lower() or needle in str(r["container_name"]).lower()]
    return partial[0] if len(partial) == 1 else None


def db_share_vps(vps_id: int, user_id: int, shared_by: int) -> tuple[bool, str]:
    if int(user_id) == int(shared_by):
        return False, "You cannot share a VPS with yourself."
    conn = db_connect()
    try:
        if conn.execute("SELECT 1 FROM vps WHERE id=?", (vps_id,)).fetchone() is None:
            return False, "VPS not found."
        cur = conn.execute(
            "INSERT OR IGNORE INTO vps_shares(vps_id,user_id,shared_by,created_at) VALUES(?,?,?,?)",
            (vps_id, user_id, shared_by, utc_now()),
        )
        if cur.rowcount == 0:
            return False, "That user already has access to this VPS."
        return True, "VPS access granted."
    finally:
        conn.close()


def db_unshare_vps(vps_id: int, user_id: int) -> tuple[bool, str]:
    conn = db_connect()
    try:
        cur = conn.execute("DELETE FROM vps_shares WHERE vps_id=? AND user_id=?", (vps_id, user_id))
        return (cur.rowcount > 0, "VPS access removed." if cur.rowcount else "That user does not have shared access.")
    finally:
        conn.close()


def db_find_accessible_vps(user_id: int, identifier: str | None) -> sqlite3.Row | None:
    owner = db_find_vps(user_id, identifier, admin=False)
    if owner:
        return owner
    needle = (identifier or "").strip().lower()
    conn = db_connect()
    try:
        rows = conn.execute(
            "SELECT v.* FROM vps v INNER JOIN vps_shares s ON s.vps_id=v.id WHERE s.user_id=? ORDER BY v.id DESC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    if not needle:
        return rows[0] if rows else None
    exact = [r for r in rows if needle in {str(r["id"]).lower(), str(r["container_id"]).lower(), str(r["container_name"]).lower()}]
    if exact:
        return exact[0]
    partial = [r for r in rows if needle in str(r["container_id"]).lower() or needle in str(r["container_name"]).lower()]
    return partial[0] if len(partial) == 1 else None


def db_is_owner_or_admin(user_id: int, vps: sqlite3.Row) -> bool:
    return int(user_id) == int(vps["user_id"]) or (ADMIN_ID > 0 and int(user_id) == int(ADMIN_ID))


def db_update_vps(container_id: str, **fields: Any) -> None:
    allowed = {"status", "suspended", "ssh_command", "sshx_url", "sshx_pid", "os_type", "location", "hostname", "public_ipv4", "ipv4_verified_at", "backend", "ptero_server_id", "ptero_identifier", "ptero_user_id"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    assignments = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [utc_now(), container_id]
    conn = db_connect()
    try:
        conn.execute(f"UPDATE vps SET {assignments},updated_at=? WHERE container_id=?", values)
    finally:
        conn.close()


def db_set_vps_ipv4(container_id: str, ipv4: str | None) -> None:
    if ipv4 is not None and not valid_public_ipv4(ipv4):
        raise ValueError("Refusing to store a non-public IPv4 address.")
    db_update_vps(container_id, public_ipv4=ipv4, ipv4_verified_at=utc_now() if ipv4 else None)



def db_list_shared(vps_id: int) -> list[sqlite3.Row]:
    conn = db_connect()
    try:
        return conn.execute(
            """SELECT s.vps_id, s.user_id, s.shared_by, s.created_at,
                      COALESCE(u.username, CAST(s.user_id AS TEXT)) AS username
               FROM vps_shares s LEFT JOIN users u ON u.user_id=s.user_id
               WHERE s.vps_id=? ORDER BY s.created_at""",
            (int(vps_id),),
        ).fetchall()
    finally:
        conn.close()


def db_find_vps_by_ptero_identifier(identifier: str) -> sqlite3.Row | None:
    conn = db_connect()
    try:
        return conn.execute(
            "SELECT * FROM vps WHERE ptero_identifier=? OR container_id=? LIMIT 1",
            (identifier, identifier),
        ).fetchone()
    finally:
        conn.close()


def db_insert_snapshot(vps_id: int, name: str, image_ref: str) -> int:
    conn = db_connect()
    try:
        cur = conn.execute(
            "INSERT INTO vps_snapshots(vps_id,name,image_ref,created_at) VALUES(?,?,?,?)",
            (int(vps_id), name, image_ref, utc_now()),
        )
        return int(cur.lastrowid)
    finally:
        conn.close()


def db_list_snapshots(vps_id: int) -> list[sqlite3.Row]:
    conn = db_connect()
    try:
        return conn.execute(
            "SELECT * FROM vps_snapshots WHERE vps_id=? ORDER BY id DESC",
            (int(vps_id),),
        ).fetchall()
    finally:
        conn.close()


def db_get_snapshot(vps_id: int, name: str) -> sqlite3.Row | None:
    conn = db_connect()
    try:
        return conn.execute(
            "SELECT * FROM vps_snapshots WHERE vps_id=? AND lower(name)=lower(?)",
            (int(vps_id), name),
        ).fetchone()
    finally:
        conn.close()


def db_delete_snapshot(vps_id: int, name: str) -> None:
    conn = db_connect()
    try:
        conn.execute("DELETE FROM vps_snapshots WHERE vps_id=? AND lower(name)=lower(?)", (int(vps_id), name))
    finally:
        conn.close()


def db_delete_vps(container_id: str) -> None:
    conn = db_connect()
    try:
        conn.execute("DELETE FROM vps WHERE container_id=?", (container_id,))
    finally:
        conn.close()


# ================================================================
# Process/Docker execution
# ================================================================

async def run_process(*args: str, timeout: float = 60, stdin: bytes | None = None) -> tuple[int, bytes, bytes]:
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        return 127, b"", str(exc).encode()
    except OSError as exc:
        return 126, b"", str(exc).encode()

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout=timeout)
        return process.returncode if process.returncode is not None else -1, stdout, stderr
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=5)
        return -1, b"", b"process timeout"
    except asyncio.CancelledError:
        # A cancelled Docker operation must not leave a live child process behind.
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=5)
        raise


async def docker_cli(*args: str, timeout: float = DOCKER_TIMEOUT, retries: int = 0) -> tuple[int, bytes, bytes]:
    last: tuple[int, bytes, bytes] = (126, b"", b"docker command failed")
    for attempt in range(max(0, retries) + 1):
        last = await run_process("docker", *args, timeout=timeout)
        if last[0] == 0:
            return last
        text = last[2].decode("utf-8", "replace").lower()
        transient = any(x in text for x in ("connection reset", "temporarily unavailable", "i/o timeout", "context deadline exceeded", "connection refused", "tls handshake timeout", "unexpected eof", "eof"))
        if not transient or attempt >= retries:
            break
        await asyncio.sleep(1.5 * (attempt + 1))
    return last


async def spawn_detached(*args: str) -> tuple[int | None, str]:
    try:
        proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        await asyncio.sleep(0.15)
        if proc.returncode is not None:
            return None, f"process exited with code {proc.returncode}"
        return proc.pid, ""
    except FileNotFoundError:
        return None, f"{args[0]} is not installed."
    except OSError as exc:
        return None, str(exc)


async def docker_exists(container: str) -> bool:
    rc, _, _ = await docker_cli("inspect", container, timeout=20, retries=1)
    return rc == 0


async def docker_state(container: str) -> str | None:
    rc, out, _ = await docker_cli("inspect", "-f", "{{.State.Status}}", container, timeout=20, retries=1)
    if rc != 0:
        return None
    return out.decode("utf-8", "replace").strip().lower() or None


async def docker_info() -> tuple[bool, str]:
    rc, out, err = await docker_cli("info", timeout=30, retries=2)
    if rc == 0:
        return True, out.decode("utf-8", "replace")
    msg = safe_log(err.decode("utf-8", "replace").strip() or "Docker daemon is unavailable.")
    return False, msg


async def docker_running_count() -> tuple[bool, int]:
    # Do not depend only on labels: older Docker clients may not advertise --label.
    # The RGNODES namespace is the canonical container-name prefix as well.
    rc, out, _ = await docker_cli("ps", "--format", "{{.ID}}\t{{.Names}}", timeout=20, retries=1)
    if rc != 0:
        return False, db_running_count()
    count = 0
    for line in out.decode("utf-8", "replace").splitlines():
        parts = line.strip().split("\t", 1)
        if len(parts) == 2 and re.fullmatch(r"rgnodes-\d+", parts[1].strip(), flags=re.I):
            count += 1
    return True, count


async def docker_pull(image: str) -> tuple[bool, str]:
    rc, _, err = await docker_cli("pull", image, timeout=IMAGE_PULL_TIMEOUT, retries=2)
    if rc == 0:
        return True, ""
    return False, safe_log(err.decode("utf-8", "replace").strip() or "Docker image pull failed.")


def quota_error(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in ("storage-opt", "disk quota", "quota", "btrfs", "overlay2", "storage driver"))


DOCKER_RUN_FEATURES: set[str] | None = None
DOCKER_FEATURE_LOCK = asyncio.Lock()


async def docker_run_features() -> set[str]:
    global DOCKER_RUN_FEATURES
    if DOCKER_RUN_FEATURES is not None:
        return DOCKER_RUN_FEATURES
    async with DOCKER_FEATURE_LOCK:
        if DOCKER_RUN_FEATURES is not None:
            return DOCKER_RUN_FEATURES
        features: set[str] = set()
        rc, out, _ = await docker_cli("run", "--help", timeout=20, retries=0)
        if rc == 0:
            text = out.decode("utf-8", "replace")
            for flag in ("--init", "--pids-limit", "--storage-opt", "--cpus", "--memory", "--memory-reservation", "--memory-swap", "--memory-swappiness", "--restart", "--hostname", "--name", "--label", "--log-driver", "--log-opt"):
                if flag in text:
                    features.add(flag)
        DOCKER_RUN_FEATURES = features
        logger.info("Docker run capabilities detected: %s", ", ".join(sorted(features)) or "basic-only")
        return features


def feature_error(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in (
        "unknown flag", "unknown option", "invalid option", "not supported",
        "not supported by this daemon", "no such option", "unrecognized option",
    ))


async def docker_run(*, image: str, hostname: str, ram: str, cpu: str, disk: str, container_name: str, location: str) -> tuple[str | None, str]:
    # Build the command from the flags this particular Docker CLI actually
    # advertises. This avoids noisy failed variants on older/lightweight Docker
    # clients where --init and --pids-limit are unavailable.
    features = await docker_run_features()
    command = ["run", "--detach"]

    if "--restart" in features:
        command += ["--restart", "unless-stopped"]
    if "--memory" in features:
        command += ["--memory", ram]
        try:
            ram_bytes = parse_size_bytes(ram)
            if "--memory-reservation" in features and MEMORY_RESERVATION_PERCENT:
                reservation_bytes = max(6 * 1024**2, int(ram_bytes * MEMORY_RESERVATION_PERCENT / 100))
                command += ["--memory-reservation", str(reservation_bytes)]
            if DISABLE_CONTAINER_SWAP and "--memory-swap" in features:
                command += ["--memory-swap", ram]
            if DISABLE_CONTAINER_SWAP and "--memory-swappiness" in features:
                command += ["--memory-swappiness", "0"]
        except ValueError:
            pass
    if "--cpus" in features:
        command += ["--cpus", cpu]
    if "--hostname" in features:
        command += ["--hostname", hostname]
    if "--name" in features:
        command += ["--name", container_name]
    if "--label" in features:
        command += ["--label", "com.rgnodes.managed=true", "--label", f"com.rgnodes.location={location}"]
    if "--log-driver" in features and "--log-opt" in features:
        command += ["--log-driver", "json-file", "--log-opt", "max-size=10m", "--log-opt", "max-file=3"]

    if "--init" in features:
        command.append("--init")
    if "--pids-limit" in features:
        command += ["--pids-limit", "1024"]

    # Docker syntax requires IMAGE after all options. Never execute an
    # options-only `docker run`, because Docker rejects that with:
    # "docker run requires at least 1 argument".
    image = str(image or "").strip()
    if not image:
        return None, "Docker image is empty; deployment configuration is invalid."

    run_command = command + [image, "tail", "-f", "/dev/null"]
    if len(run_command) < 3 or not run_command[2]:
        return None, "Docker run command construction failed before execution."

    quota_requested = ENABLE_HARD_DISK_QUOTA and "--storage-opt" in features
    quota_attempt = (
        command + ["--storage-opt", f"size={disk}", image, "tail", "-f", "/dev/null"]
        if quota_requested
        else run_command
    )
    attempts = [quota_attempt]
    if quota_requested and QUOTA_FALLBACK:
        attempts.append(run_command)

    last_error = "Docker container creation failed."
    for index, attempt in enumerate(attempts):
        if len(attempt) < 3 or not attempt[2]:
            last_error = "Docker run command was incomplete; refusing to execute it."
            continue
        rc, out, err = await docker_cli(*attempt, timeout=120, retries=0)
        if rc == 0:
            container_id = out.decode("utf-8", "replace").strip().splitlines()[0] if out else ""
            if container_id:
                return container_id, ""
            last_error = "Docker returned no container ID."
            continue
        last_error = safe_log(err.decode("utf-8", "replace").strip() or "unknown Docker error")
        if index + 1 < len(attempts) and (quota_error(last_error) or feature_error(last_error)):
            logger.warning("Docker hard-quota create failed; retrying without storage quota: %s", last_error)
            continue
        break
    return None, last_error


async def docker_start(container: str) -> tuple[bool, str]:
    rc, _, err = await docker_cli("start", container, timeout=60, retries=1)
    return rc == 0, safe_log(err.decode("utf-8", "replace").strip())


async def docker_stop(container: str) -> bool:
    rc, _, _ = await docker_cli("stop", "--time", "20", container, timeout=40, retries=1)
    if rc == 0:
        return True
    rc, _, _ = await docker_cli("kill", container, timeout=25, retries=1)
    return rc == 0


async def docker_restart(container: str) -> tuple[bool, str]:
    rc, _, err = await docker_cli("restart", "--time", "20", container, timeout=60, retries=1)
    return rc == 0, safe_log(err.decode("utf-8", "replace").strip())


async def docker_remove(container: str) -> bool:
    rc, _, _ = await docker_cli("rm", "--force", container, timeout=60, retries=1)
    if rc == 0:
        return True
    return not await docker_exists(container)


async def docker_exec(container: str, *command: str, timeout: float = 120) -> tuple[int, bytes, bytes]:
    return await docker_cli("exec", container, *command, timeout=timeout, retries=1)


async def docker_exec_shell(container: str, script: str, timeout: float = ACCESS_TIMEOUT) -> tuple[int, bytes, bytes]:
    # /bin/sh is present on all supported images; use POSIX sh -c for minimal images.
    return await docker_exec(container, "sh", "-c", script, timeout=timeout)


async def docker_stats(container: str) -> dict[str, str]:
    """Live CPU/network plus a cache-adjusted working-set estimate. Never hides a live 0.00%% CPU reading."""
    memory_text = "N/A"
    cgroup_script = r'''set -u
if [ -r /sys/fs/cgroup/memory.current ]; then
  current=$(cat /sys/fs/cgroup/memory.current 2>/dev/null || echo 0)
  inactive=$(awk '$1=="inactive_file"{print $2; found=1} END{if(!found) print 0}' /sys/fs/cgroup/memory.stat 2>/dev/null)
  max=$(cat /sys/fs/cgroup/memory.max 2>/dev/null || echo max)
  case "$current" in ''|*[!0-9]*) current=0;; esac
  case "$inactive" in ''|*[!0-9]*) inactive=0;; esac
  usage=$(( current > inactive ? current-inactive : 0 ))
  printf 'v2\t%s\t%s\n' "$usage" "$max"
  exit 0
fi
if [ -r /sys/fs/cgroup/memory/memory.usage_in_bytes ]; then
  current=$(cat /sys/fs/cgroup/memory/memory.usage_in_bytes 2>/dev/null || echo 0)
  inactive=$(awk '$1=="total_inactive_file"{print $2; found=1} END{if(!found) print 0}' /sys/fs/cgroup/memory/memory.stat 2>/dev/null)
  limit=$(cat /sys/fs/cgroup/memory/memory.limit_in_bytes 2>/dev/null || echo 0)
  case "$current" in ''|*[!0-9]*) current=0;; esac
  case "$inactive" in ''|*[!0-9]*) inactive=0;; esac
  case "$limit" in ''|*[!0-9]*) limit=0;; esac
  usage=$(( current > inactive ? current-inactive : 0 ))
  printf 'v1\t%s\t%s\n' "$usage" "$limit"
  exit 0
fi
exit 1
'''
    rc_mem, out_mem, _ = await docker_exec_shell(container, cgroup_script, timeout=10)
    if rc_mem == 0:
        parts = out_mem.decode("utf-8", "replace").strip().split("\t")
        if len(parts) == 3:
            try:
                used = int(parts[1])
                raw_limit = parts[2].strip()
                if raw_limit.isdigit():
                    limit = int(raw_limit)
                    if 0 < limit < (1 << 50):
                        memory_text = f"{format_bytes(max(0, used))} / {format_bytes(limit)}"
            except (ValueError, TypeError):
                pass

    rc, out, err = await docker_cli(
        "stats", "--no-stream", "--format", "{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}",
        container, timeout=30, retries=1,
    )
    if rc == 0:
        parts = out.decode("utf-8", "replace").strip().split("\t")
        if len(parts) == 3:
            cpu = parts[0].strip() or "0.00%"
            mem = memory_text if memory_text != "N/A" else parts[1].strip()
            return {"cpu": cpu, "memory": mem or "0 B", "network": parts[2].strip() or "0 B / 0 B"}
    # Fallback: old/minimal Docker clients can return a plain row. Keep CPU visible instead of N/A when possible.
    plain = out.decode("utf-8", "replace").strip()
    if plain:
        match = re.search(r"(\d+(?:\.\d+)?%)", plain)
        if match:
            return {"cpu": match.group(1), "memory": memory_text, "network": "N/A"}
    logger.debug("docker stats failed for %s: %s", clean(container, 32), safe_log(err.decode("utf-8", "replace")))
    return {"cpu": "N/A", "memory": memory_text, "network": "N/A"}


async def docker_uptime(container: str) -> str:
    rc, out, _ = await docker_cli("inspect", "-f", "{{.State.StartedAt}}", container, timeout=20, retries=1)
    if rc != 0:
        return "N/A"
    raw = out.decode("utf-8", "replace").strip()
    try:
        started = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        seconds = max(0, int((datetime.now(timezone.utc) - started).total_seconds()))
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        return f"{days}d {hours}h {minutes}m"
    except (ValueError, TypeError):
        return "N/A"


async def docker_disk_usage(container: str) -> dict[str, str]:
    """Measure container writable-layer usage instead of the host overlay filesystem."""
    rc, out, _ = await docker_cli(
        "inspect", "--size", "-f", "{{.SizeRw}}\t{{.SizeRootFs}}", container,
        timeout=20, retries=1,
    )
    if rc == 0:
        parts = out.decode("utf-8", "replace").strip().split("\t")
        if len(parts) == 2:
            try:
                writable = max(0, int(parts[0]))
                rootfs = max(0, int(parts[1]))
                return {"used": format_bytes(writable), "total": "Host-backed", "percent": "no hard quota", "rootfs": format_bytes(rootfs)}
            except ValueError:
                pass
    script = "df -B1 / 2>/dev/null | awk 'NR==2 {print $2, $3, $4, $5}'"
    rc, out, _ = await docker_exec_shell(container, script, timeout=20)
    parts = out.decode("utf-8", "replace").strip().split()
    if rc != 0 or len(parts) < 4:
        return {"used": "N/A", "total": "N/A", "percent": "N/A"}
    try:
        return {"used": format_bytes(int(parts[1])), "total": format_bytes(int(parts[0])), "percent": parts[3]}
    except ValueError:
        return {"used": "N/A", "total": "N/A", "percent": "N/A"}


async def docker_logs(container: str, lines: int = 50) -> str:
    safe_lines = max(1, min(int(lines), 200))
    rc, out, err = await docker_cli("logs", "--tail", str(safe_lines), container, timeout=30, retries=1)
    if rc != 0:
        return "Unable to fetch container logs."
    text = out.decode("utf-8", "replace") or err.decode("utf-8", "replace")
    return text.replace("\x00", "")[-3800:] or "No recent logs."


# ================================================================
# SSHx — verified current installer flow, no fragile line continuations
# ================================================================

SSHX_INSTALL_SCRIPT = r"""
set -eu
export NO_COLOR=1
BASE=/var/lib/rgnodes/sshx
LOG="$BASE/sshx.log"
PID="$BASE/sshx.pid"
URL="$BASE/sshx.url"
BIN="$BASE/sshx"
mkdir -p "$BASE"

have_cmd() { command -v "$1" >/dev/null 2>&1; }

if ! have_cmd curl; then
  if have_cmd apt-get; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y >/dev/null 2>&1
    apt-get install -y --no-install-recommends curl ca-certificates >/dev/null 2>&1
  elif have_cmd apk; then
    apk add --no-cache curl ca-certificates >/dev/null 2>&1
  else
    echo "curl is not available and no supported package manager was found" >&2
    exit 11
  fi
fi

if [ ! -e /etc/ssl/certs/ca-certificates.crt ] && have_cmd apt-get; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y >/dev/null 2>&1 || true
  apt-get install -y --no-install-recommends ca-certificates >/dev/null 2>&1 || true
fi

cd "$BASE"

if [ ! -x "$BIN" ]; then
  rm -f "$BIN.tmp"
  curl -sSf https://sshx.io/get | sh -s download
  if [ ! -x "$BIN" ]; then
    echo "sshx binary was not downloaded" >&2
    exit 12
  fi
  chmod 700 "$BIN" 2>/dev/null || true
fi

if have_cmd apt-get; then
  apt-get clean >/dev/null 2>&1 || true
  rm -rf /var/lib/apt/lists/* /var/cache/apt/* 2>/dev/null || true
fi

# Stop only a previous sshx process recorded by this installation.
if [ -s "$PID" ]; then
  OLD="$(cat "$PID" 2>/dev/null || true)"
  case "$OLD" in
    ''|*[!0-9]*) ;;
    *)
      if [ -r "/proc/$OLD/cmdline" ]; then
        CMD="$(tr '\000' ' ' < "/proc/$OLD/cmdline" 2>/dev/null || true)"
        case "$CMD" in *sshx*) kill "$OLD" 2>/dev/null || true;; esac
      fi
      ;;
  esac
fi
rm -f "$PID" "$URL"
: > "$LOG"

nohup "$BIN" --quiet --name RGNODES >"$LOG" 2>&1 </dev/null &
PID_NOW=$!
echo "$PID_NOW" > "$PID"

for _ in $(seq 1 120); do
  if [ -s "$LOG" ]; then
    tr -d '\r' < "$LOG" 2>/dev/null \
      | sed -E 's/\x1B\[[0-9;?]*[ -\/]*[@-~]//g' \
      | grep -Eo 'https://sshx\.io/s/[^[:space:]<>\"\x27`]+' \
      | tail -n 1 > "$URL" || true
  fi
  if [ -s "$URL" ]; then
    cat "$URL"
    exit 0
  fi
  if ! kill -0 "$PID_NOW" 2>/dev/null; then
    break
  fi
  sleep 1
done

echo "SSHx did not return a share URL." >&2
cat "$LOG" >&2 2>/dev/null || true
exit 13
"""


def normalize_sshx_url(raw: str | None) -> str | None:
    match = ACCESS_URL_RE.search(raw or "")
    if not match:
        return None
    url = match.group(0).rstrip(".,;:)]}'\"")
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or parsed.netloc.lower() != "sshx.io":
        return None
    if not re.fullmatch(r"/s/[^/\s]+", parsed.path, flags=re.I):
        return None
    return url


async def sshx_process_alive(container: str, pid: str | None) -> bool:
    if not pid or not str(pid).isdigit():
        return False
    script = (
        'PID="$1"; '
        'if [ -r "/proc/$PID/cmdline" ]; then '
        'CMD="$(tr "\\000" " " < "/proc/$PID/cmdline" 2>/dev/null || true)"; '
        'case "$CMD" in *sshx*) exit 0;; esac; '
        'fi; exit 1'
    )
    rc, _, _ = await docker_exec_shell(container, script.replace('$1', pid), timeout=10)
    return rc == 0


async def install_and_start_sshx(container: str) -> dict[str, str] | None:
    if await docker_state(container) != "running":
        return None
    # Two attempts cover transient DNS/TLS startup conditions after a new container starts.
    for attempt in range(2):
        rc, out, err = await docker_exec_shell(container, SSHX_INSTALL_SCRIPT, timeout=ACCESS_TIMEOUT)
        if rc == 0:
            text = out.decode("utf-8", "replace")
            url = normalize_sshx_url(text)
            if not url:
                _, saved, _ = await docker_exec_shell(
                    container,
                    "cat /var/lib/rgnodes/sshx/sshx.url 2>/dev/null || true",
                    timeout=15,
                )
                url = normalize_sshx_url(saved.decode("utf-8", "replace"))
            if url:
                _, pid_out, _ = await docker_exec_shell(
                    container,
                    "cat /var/lib/rgnodes/sshx/sshx.pid 2>/dev/null || true",
                    timeout=15,
                )
                pid = pid_out.decode("utf-8", "replace").strip()
                if pid and await sshx_process_alive(container, pid):
                    return {"url": url, "pid": pid}
                # URL exists even if the pid probe races the just-started process.
                if pid.isdigit():
                    return {"url": url, "pid": pid}
        logger.warning(
            "SSHx setup attempt %d failed for container %s: %s",
            attempt + 1,
            clean(container, 24),
            safe_log(err.decode("utf-8", "replace") or out.decode("utf-8", "replace")),
        )
        if attempt == 0:
            await asyncio.sleep(2)
    return None


async def stop_sshx(container: str) -> None:
    script = r"""
set +e
PID_FILE=/var/lib/rgnodes/sshx/sshx.pid
if [ -s "$PID_FILE" ]; then
  PID="$(cat "$PID_FILE" 2>/dev/null)"
  case "$PID" in
    ''|*[!0-9]*) ;;
    *)
      if [ -r "/proc/$PID/cmdline" ]; then
        CMD="$(tr '\000' ' ' < "/proc/$PID/cmdline" 2>/dev/null || true)"
        case "$CMD" in *sshx*) kill "$PID" 2>/dev/null || true; sleep 1; kill -9 "$PID" 2>/dev/null || true;; esac
      fi
      ;;
  esac
fi
rm -f /var/lib/rgnodes/sshx/sshx.pid /var/lib/rgnodes/sshx/sshx.url
"""
    await docker_exec_shell(container, script, timeout=15)


# ================================================================
# Discord UI helpers — text/layout retained
# ================================================================

EMBED_COLOR = discord.Color.from_rgb(43, 45, 49)
FOOTER = "⚡ RGNODES™ • VPS Management"


def make_embed(title: str, description: str | None = None) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=EMBED_COLOR, timestamp=discord.utils.utcnow())
    embed.set_footer(text=FOOTER)
    return embed


def slot_status_text(user_id: int) -> str:
    """Return accurate per-user slot usage, including admin-added slots."""
    try:
        used = db_vps_count(int(user_id))
        limit = db_effective_slots(int(user_id))
    except Exception:
        used, limit = 0, max(1, SERVER_LIMIT)
    remaining = max(0, limit - used)
    return f"`{used}/{limit}` used • `{remaining}` available"


def status_text(status: str, suspended: bool = False) -> str:
    if suspended:
        return "⛔ SUSPENDED"
    return {"running": "🟢 RUNNING", "stopped": "🔴 STOPPED", "created": "🟡 CREATED", "starting": "🟡 STARTING", "restarting": "🟡 RESTARTING"}.get(status, "⚪ " + clean(status).upper())


def is_unknown_interaction(exc: BaseException) -> bool:
    return isinstance(exc, discord.NotFound) and getattr(exc, "code", None) == 10062


async def safe_defer(interaction: discord.Interaction, ephemeral: bool = False) -> bool:
    if interaction.response.is_done():
        return True
    try:
        # thinking=True gives both slash commands and component buttons a real deferred
        # channel response that can be edited safely later.
        await interaction.response.defer(ephemeral=ephemeral, thinking=True)
        return True
    except discord.InteractionResponded:
        return True
    except discord.NotFound as exc:
        if is_unknown_interaction(exc):
            logger.debug("Ignoring expired interaction during defer (10062).")
            return False
        logger.warning("Interaction defer failed: %s", safe_log(exc))
        return False
    except discord.HTTPException as exc:
        logger.warning("Interaction defer failed: %s", safe_log(exc))
        return False


async def safe_edit_original(
    interaction: discord.Interaction,
    *,
    embed: discord.Embed,
    view: discord.ui.View | None = None,
) -> bool:
    try:
        await interaction.edit_original_response(embed=embed, view=view)
        return True
    except discord.NotFound as exc:
        if is_unknown_interaction(exc):
            if not INTERACTION_LOG_UNKNOWN_AS_DEBUG:
                return False
            logger.debug("Ignoring expired interaction while editing original response (10062).")
            return False
        logger.warning("Interaction edit failed: %s", safe_log(exc))
        return False
    except (discord.HTTPException, TypeError) as exc:
        logger.warning("Interaction edit failed: %s", safe_log(exc))
        return False


async def safe_followup(
    interaction: discord.Interaction,
    *,
    embed: discord.Embed,
    view: discord.ui.View | None = None,
    ephemeral: bool = True,
) -> bool:
    # Every long-running command/button path defers first. Editing the original
    # deferred response avoids duplicate messages and webhook-token edge cases.
    if interaction.response.is_done():
        return await safe_edit_original(interaction, embed=embed, view=view)
    try:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=ephemeral)
        return True
    except discord.NotFound as exc:
        if is_unknown_interaction(exc):
            logger.debug("Ignoring expired interaction while responding (10062).")
            return False
        logger.warning("Interaction response failed: %s", safe_log(exc))
        return False
    except discord.HTTPException as exc:
        logger.warning("Interaction response failed: %s", safe_log(exc))
        return False


async def safe_respond(interaction: discord.Interaction, *, embed: discord.Embed, ephemeral: bool = True, view: discord.ui.View | None = None) -> None:
    try:
        if interaction.response.is_done():
            await safe_edit_original(interaction, embed=embed, view=view)
        else:
            kwargs: dict[str, Any] = {"embed": embed, "ephemeral": ephemeral}
            if view is not None:
                kwargs["view"] = view
            await interaction.response.send_message(**kwargs)
    except discord.NotFound as exc:
        if is_unknown_interaction(exc):
            logger.debug("Ignoring expired interaction response (10062).")
            return
        logger.warning("Response failed: %s", safe_log(exc))
    except discord.HTTPException as exc:
        logger.warning("Response failed: %s", safe_log(exc))


async def safe_component_edit(
    interaction: discord.Interaction,
    *,
    embed: discord.Embed,
    view: discord.ui.View | None = None,
) -> bool:
    try:
        await interaction.response.edit_message(embed=embed, view=view)
        return True
    except discord.NotFound as exc:
        if is_unknown_interaction(exc):
            logger.debug("Ignoring expired component interaction (10062).")
            return False
        logger.warning("Component edit failed: %s", safe_log(exc))
        return False
    except discord.HTTPException as exc:
        logger.warning("Component edit failed: %s", safe_log(exc))
        return False


async def safe_dm(user: discord.User | discord.Member, embed: discord.Embed, view: discord.ui.View | None = None) -> bool:
    try:
        kwargs: dict[str, Any] = {"embed": embed}
        if view is not None:
            kwargs["view"] = view
        await user.send(**kwargs)
        return True
    except discord.Forbidden:
        return False
    except discord.HTTPException as exc:
        logger.warning("DM failed: %s", safe_log(exc))
        return False


def sshx_view(url: str) -> discord.ui.View:
    view = discord.ui.View(timeout=900)
    view.add_item(discord.ui.Button(label="Click to Open", emoji="🌐", style=discord.ButtonStyle.link, url=url))
    return view


def console_embed(vps_name: str, url: str, ipv4: str | None = None, location: str | None = None) -> discord.Embed:
    embed = make_embed("✨ RGNODES™ • 🌐 SSHx Access", "Your private web SSH console is ready.")
    embed.add_field(name="🖥️ VPS", value=f"`{clean(vps_name)}`", inline=False)
    if valid_public_ipv4(ipv4):
        embed.add_field(name="🌐 Verified IPv4", value=f"`{ipv4}`", inline=True)
    if location:
        embed.add_field(name="🌍 Node", value=clean(location, 80), inline=True)
    embed.add_field(name="🔗 Link", value="Click **Open Console** below.", inline=False)
    embed.add_field(name="⚠️ Security", value="This link grants direct root access. Do not share it. Generate a new link if it is exposed.", inline=False)
    return embed


def ipv4_dm_embed(vps: sqlite3.Row, network: dict[str, str]) -> discord.Embed:
    ip = network.get("ip")
    embed = make_embed("🔐 RGNODES™ • Private Network Details", "Your verified public IPv4 is provided privately in this DM.")
    embed.add_field(name="🖥️ VPS", value=f"`{clean(vps['container_name'])}` • ID `{vps['id']}`", inline=False)
    embed.add_field(name="🌐 Verified Public IPv4", value=f"`{clean(ip, 64)}`", inline=True)
    embed.add_field(name="🌍 Detected Location", value=clean(actual_location_label(network), 80), inline=True)
    embed.add_field(name="🔒 Privacy", value="This IPv4 is intentionally hidden from public/channel embeds.", inline=False)
    return embed


async def send_private_ipv4(user: discord.User | discord.Member, vps: sqlite3.Row) -> bool:
    network = await detect_public_network(force=True)
    ip = network.get("ip")
    if not valid_public_ipv4(ip):
        return False
    db_set_vps_ipv4(vps["container_id"], ip)
    return await safe_dm(user, ipv4_dm_embed(vps, network))


async def host_uptime() -> str:
    try:
        raw = Path("/proc/uptime").read_text(encoding="utf-8", errors="replace").split()[0]
        seconds = max(0, int(float(raw)))
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, secs = divmod(rem, 60)
        return f"{days}d {hours}h {minutes}m {secs}s"
    except (OSError, ValueError, IndexError):
        return "N/A"


async def backend_state(vps: sqlite3.Row) -> str | None:
    if str(vps["backend"] or "docker").lower() == "pterodactyl":
        return await ptero_status(vps)
    return await docker_state(vps["container_id"])


async def backend_stats(vps: sqlite3.Row) -> dict[str, str]:
    if str(vps["backend"] or "docker").lower() == "pterodactyl":
        return await ptero_utilization(vps)
    return await docker_stats(vps["container_id"])


def format_duration_ms(ms: int | float | str | None) -> str:
    try:
        seconds = max(0, int(float(ms or 0) / 1000))
    except (TypeError, ValueError):
        return "N/A"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


async def backend_uptime(vps: sqlite3.Row) -> str:
    if str(vps["backend"] or "docker").lower() == "pterodactyl":
        if not ptero_client_configured():
            return "N/A"
        identifier = vps["ptero_identifier"] or vps["container_id"]
        status, body = await ptero_request(
            "GET", f"client/servers/{quote(str(identifier), safe='')}/resources",
            api_key=PTERO_CLIENT_API_KEY,
        )
        if status == 200 and isinstance(body, dict):
            attrs = _ptero_attr(body) or {}
            resources = attrs.get("resources") or {}
            return format_duration_ms(resources.get("uptime"))
        return "N/A"
    return await docker_uptime(vps["container_id"])


async def backend_disk(vps: sqlite3.Row) -> dict[str, str]:
    if str(vps["backend"] or "docker").lower() == "pterodactyl":
        stats = await ptero_utilization(vps)
        return {"used": str(stats.get("disk", "N/A")), "total": clean(vps["disk"]), "percent": "panel limit"}
    return await docker_disk_usage(vps)


async def backend_panel_or_console(vps: sqlite3.Row) -> str | None:
    if str(vps["backend"] or "docker").lower() == "pterodactyl":
        return await ptero_panel_link(vps)
    return normalize_sshx_url(vps["sshx_url"])


async def refresh_vps_record_state(vps: sqlite3.Row) -> sqlite3.Row:
    state = await backend_state(vps)
    backend = str(vps["backend"] or "docker").lower()
    if state == "running":
        db_update_vps(vps["container_id"], status="running", sshx_pid=None if backend == "pterodactyl" else vps["sshx_pid"])
    elif state in {"stopped", "off"}:
        db_update_vps(vps["container_id"], status="stopped", sshx_url=None if backend == "docker" else vps["sshx_url"], sshx_pid=None)
    latest = db_get_vps(vps["id"]) or vps
    return latest


def dashboard_embed(vps: sqlite3.Row, stats: dict[str, str], uptime: str, disk: dict[str, str], network: dict[str, str] | None = None, ports: list[sqlite3.Row] | None = None) -> discord.Embed:
    network = network or NETWORK_CACHE
    ports = ports if ports is not None else db_list_ports(vps["id"])
    port_summary = "None configured" if not ports else " • ".join(f"`{p['host_port']}→{p['container_port']}/{str(p['protocol']).upper()}`" for p in ports[:10])
    live = status_text(vps["status"], bool(vps["suspended"]))
    embed = make_embed(f"🖥️ VPS #{vps['id']} • VMID `{vps['id']}`", f"**{live}**  •  `{clean(vps['container_name'])}`")
    embed.add_field(name="📦 Resources", value=(f"╭ **RAM:** {clean(vps['ram'])}\n├ **CPU Limit:** {clean(vps['cpu'])} Core(s)\n├ **Storage:** {clean(vps['disk'])}\n├ **OS:** {os_label(vps['os_type'])}\n╰ **Node:** {location_label(vps['location'])}"), inline=True)
    embed.add_field(name="⚙️ Configuration", value=(f"╭ **Slots:** {slot_status_text(vps['user_id'])}\n├ **Uptime:** {clean(uptime)}\n├ **Hostname:** `{clean(vps['hostname'])}`\n├ **IPv4:** 🔒 Sent privately in DM\n╰ **Detected Node:** {actual_location_label(network)}"), inline=True)
    embed.add_field(name="📈 Live Stats", value=(f"💻 **CPU:** {clean(stats.get('cpu'))} used / {clean(vps['cpu'])} limit\n🧠 **Memory:** {clean(stats.get('memory'))}\n💾 **Disk:** {clean(disk.get('used'))} / {clean(vps['disk'])}\n🌐 **Network:** {clean(stats.get('network'))}"), inline=False)
    if str(vps["backend"] or "docker").lower() == "pterodactyl":
        embed.add_field(name="🦖 Pterodactyl", value=f"Server ID: `{clean(vps['ptero_server_id'])}` • Identifier: `{clean(vps['ptero_identifier'])}`", inline=False)
        embed.add_field(name="🌐 Allocations", value="Managed by Pterodactyl Panel/Wings.", inline=False)
    else:
        embed.add_field(name=f"🌐 Port Forwarding • {len(ports)}/{MAX_PORTS_PER_VPS}", value=port_summary, inline=False)
    embed.add_field(name="🎮 Actions", value="▶️ Start • ⏹️ Stop • 🖥️ Console/Panel • 📊 Stats • 🔄 Restart • 🔃 Refresh • 🗑️ Delete", inline=False)
    return embed



def progress_embed(stage: int, title: str, os_type: str, location: str, ram: str, cpu: str, disk: str, name: str) -> discord.Embed:
    total = 10
    filled = max(0, min(stage, total))
    bar = "▰" * filled + "▱" * (total - filled)
    embed = make_embed("✨ RGNODES™ VPS Deployment", f"**{title}**\n`{bar}` **{filled * 10}%**")
    embed.add_field(name="🖥️ OS", value=os_label(os_type), inline=True)
    embed.add_field(name="🌍 Location", value=location_label(location), inline=True)
    embed.add_field(name="📦 VPS", value=f"`{clean(name)}`", inline=True)
    embed.add_field(name="⚙️ Resources", value=f"`{ram}` RAM • `{cpu}` CPU • `{disk}` Disk", inline=False)
    return embed



# ================================================================
# Pterodactyl Application/Client API integration
# ================================================================
PTERO_API_LOCK = asyncio.Lock()

def _ptero_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "Application/vnd.pterodactyl.v1+json",
        "User-Agent": "RGNODES-VPS-Manager/2.0",
    }


def _ptero_request_sync(method: str, url: str, api_key: str, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any] | str]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method.upper(), headers=_ptero_headers(api_key))
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read().decode("utf-8", "replace")
            if not raw:
                return resp.status, {}
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", "replace")
            try:
                body: dict[str, Any] | str = json.loads(raw)
            except json.JSONDecodeError:
                body = raw
        except Exception:
            body = str(exc)
        return int(exc.code), body
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 599, str(exc)


async def ptero_request(method: str, path: str, *, payload: dict[str, Any] | None = None, api_key: str | None = None) -> tuple[int, dict[str, Any] | str]:
    key = api_key or PTERO_API_KEY
    if not PTERO_URL or not key:
        return 503, "Pterodactyl is not configured."
    url = f"{PTERO_URL}/api/{path.lstrip('/')}"
    async with PTERO_API_LOCK:
        return await asyncio.to_thread(_ptero_request_sync, method, url, key, payload)


def ptero_error_message(status: int, body: dict[str, Any] | str) -> str:
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list):
            msgs: list[str] = []
            for item in errors[:4]:
                if isinstance(item, dict):
                    detail = item.get("detail") or item.get("code") or item.get("title")
                    if detail:
                        msgs.append(str(detail))
            if msgs:
                return "; ".join(msgs)
        if body.get("message"):
            return str(body["message"])
    if status == 599:
        return "Pterodactyl panel is unreachable or timed out."
    return safe_log(str(body or f"HTTP {status}"), 1200)


def _ptero_attr(body: dict[str, Any] | str) -> dict[str, Any] | None:
    if not isinstance(body, dict):
        return None
    attrs = body.get("attributes")
    return attrs if isinstance(attrs, dict) else None


def _ptero_limit_mb(value: str) -> int:
    return max(256, int(parse_size_bytes(value) / 1024**2))


async def ptero_get_server(server_id: int | str) -> dict[str, Any] | None:
    status, body = await ptero_request("GET", f"application/servers/{quote(str(server_id), safe='')}?include=allocations,node,user")
    if status != 200:
        logger.warning("Pterodactyl server lookup failed (%s): %s", status, ptero_error_message(status, body))
        return None
    return _ptero_attr(body)


async def ptero_create_server(*, name: str, ram: str, cpu: str, disk: str) -> tuple[bool, str, dict[str, Any] | None]:
    if not ptero_application_configured():
        return False, (
            "Pterodactyl Application API is not fully configured. Set "
            "PTERO_URL, PTERO_API_KEY, PTERO_DEFAULT_USER_ID, PTERO_NODE_ID, "
            "PTERO_NEST_ID, PTERO_EGG_ID and PTERO_ALLOCATION_ID."
        ), None
    owner_id = PTERO_DEFAULT_USER_ID

    environment = dict(PTERO_ENVIRONMENT)
    payload: dict[str, Any] = {
        "name": name,
        "user": owner_id,
        "node": PTERO_NODE_ID,
        "nest": PTERO_NEST_ID,
        "egg": PTERO_EGG_ID,
        "docker_image": PTERO_DOCKER_IMAGE or None,
        "startup": PTERO_STARTUP or None,
        "environment": environment,
        "limits": {
            "memory": _ptero_limit_mb(ram),
            "swap": PTERO_MEMORY_SWAP,
            "disk": int(parse_size_bytes(disk) / 1024**2),
            "io": PTERO_IO,
            "cpu": max(1, int(float(cpu) * 100)),
        },
        "feature_limits": {
            "databases": PTERO_DATABASES,
            "allocations": PTERO_ALLOCATIONS,
            "backups": PTERO_BACKUPS,
        },
        "allocation": {"default": PTERO_ALLOCATION_ID},
        "deploy": {
            "locations": [],
            "port_range": [],
            "dedicated_ip": False,
        },
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    status, body = await ptero_request("POST", "application/servers", payload=payload)
    if status not in {200, 201}:
        return False, ptero_error_message(status, body), None
    attrs = _ptero_attr(body)
    if not attrs:
        return False, "Pterodactyl created the server but returned no server attributes.", None
    return True, "Pterodactyl server created successfully.", attrs


async def ptero_power(vps: sqlite3.Row, action: str) -> tuple[bool, str]:
    identifier = vps["ptero_identifier"] or vps["container_id"]
    if not ptero_client_configured():
        return False, "PTERO_CLIENT_API_KEY is not configured; Pterodactyl power and live-resource commands require a Client API key."
    signal_name = {"start": "start", "stop": "stop", "restart": "restart", "kill": "kill"}.get(action)
    if not signal_name:
        return False, "Unsupported Pterodactyl power action."
    status, body = await ptero_request("POST", f"client/servers/{quote(str(identifier), safe='')}/power", payload={"signal": signal_name}, api_key=PTERO_CLIENT_API_KEY)
    if status not in {200, 204}:
        return False, ptero_error_message(status, body)
    return True, f"Pterodactyl power action `{signal_name}` completed."


async def ptero_utilization(vps: sqlite3.Row) -> dict[str, str]:
    identifier = vps["ptero_identifier"] or vps["container_id"]
    if PTERO_CLIENT_API_KEY:
        status, body = await ptero_request(
            "GET", f"client/servers/{quote(str(identifier), safe='')}/resources",
            api_key=PTERO_CLIENT_API_KEY,
        )
        if status == 200 and isinstance(body, dict):
            attrs = _ptero_attr(body) or {}
            current = attrs.get("current_state", "offline")
            res = attrs.get("resources") or {}
            memory = int(res.get("memory_bytes") or 0)
            cpu_ns = float(res.get("cpu_absolute") or 0.0)
            disk = int(res.get("disk_bytes") or 0)
            return {
                "cpu": f"{cpu_ns:.2f}%",
                "memory": format_bytes(memory),
                "network": f"{format_bytes(int(res.get('network_rx_bytes') or 0))} ↓ / {format_bytes(int(res.get('network_tx_bytes') or 0))} ↑",
                "state": str(current),
                "disk": format_bytes(disk),
            }
    app = await ptero_get_server(vps["ptero_server_id"])
    if app:
        suspended = bool(app.get("suspended", False))
        installed = bool(app.get("installed", True))
        return {
            "cpu": "N/A", "memory": "N/A", "network": "N/A", "disk": "N/A",
            "state": "suspended" if suspended else ("installing" if not installed else "unknown"),
        }
    return {"cpu": "N/A", "memory": "N/A", "network": "N/A", "disk": "N/A", "state": "unknown"}


async def ptero_status(vps: sqlite3.Row) -> str:
    data = await ptero_utilization(vps)
    state = str(data.get("state", "unknown")).lower()
    if state in {"running", "on"}:
        return "running"
    if state in {"starting", "restarting", "installing"}:
        return state
    if state in {"stopped", "offline", "off"}:
        return "stopped"
    return "unknown"


async def ptero_panel_link(vps: sqlite3.Row) -> str:
    identifier = str(vps["ptero_identifier"] or vps["container_id"])
    return f"{PTERO_PANEL_PUBLIC_URL}/server/{identifier}" if PTERO_PANEL_PUBLIC_URL else ""


# ================================================================
# Lifecycle / deployment
# ================================================================

OperationCallback = Callable[[discord.Embed], Awaitable[None]]
CREATE_LOCK = asyncio.Lock()
CAPACITY_LOCK = asyncio.Lock()
VPS_LOCKS: dict[str, asyncio.Lock] = {}


def vps_lock(vps_id: int) -> asyncio.Lock:
    key = str(vps_id)
    return VPS_LOCKS.setdefault(key, asyncio.Lock())


async def next_container_name() -> str:
    rc, out, _ = await docker_cli("ps", "--all", "--format", "{{.Names}}", timeout=20, retries=1)
    used: set[int] = set()
    if rc == 0:
        for raw in out.decode("utf-8", "replace").splitlines():
            m = re.fullmatch(r"rgnodes-(\d+)", raw.strip(), flags=re.I)
            if m:
                used.add(int(m.group(1)))
    for row in db_get_all_vps():
        m = re.fullmatch(r"rgnodes-(\d+)", str(row["container_name"]), flags=re.I)
        if m:
            used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    return f"rgnodes-{n}"


async def update_progress(callback: OperationCallback | None, stage: int, title: str, *, os_type: str, location: str, ram: str, cpu: str, disk: str, name: str) -> None:
    if callback:
        await callback(progress_embed(stage, title, os_type, location, ram, cpu, disk, name))


async def create_vps(
    user: discord.User | discord.Member,
    *,
    os_type: str,
    location: str,
    ram: str,
    cpu: str,
    disk: str,
    progress: OperationCallback | None = None,
    backend_override: str | None = None,
) -> tuple[bool, str, sqlite3.Row | None]:
    normalized_os = normalize_os(os_type)
    normalized_location = normalize_location(location)
    if not normalized_os:
        return False, "Unsupported operating system.", None
    if not normalized_location:
        return False, "Unsupported location. Choose Singapore (SG) or India (IN).", None
    try:
        ram, cpu, disk = validate_resources(ram, cpu, disk)
    except ValueError as exc:
        return False, str(exc), None
    if active_backend() == "docker":
        capacity_error = resource_capacity_error(ram, cpu, disk)
        if capacity_error:
            return False, capacity_error, None
    if db_is_banned(user.id):
        return False, "You are not allowed to create VPS instances.", None

    backend = (backend_override or active_backend()).strip().lower()
    if backend not in {"docker", "pterodactyl"}:
        return False, "Invalid VPS backend configuration.", None
    if backend == "pterodactyl" and not ptero_configured():
        return False, "Pterodactyl backend is not fully configured. Set the required PTERO_* Application and Client API settings.", None
    verified_ipv4 = None
    if backend == "docker":
        verified_ipv4 = await real_public_ipv4(force=True)
        if REQUIRE_REAL_PUBLIC_IPV4 and not valid_public_ipv4(verified_ipv4):
            return False, "A verified real public IPv4 could not be detected on this host. VPS creation was blocked to avoid showing a fake IPv4.", None

    async with CREATE_LOCK:
        is_admin_user = ADMIN_BYPASS_LIMITS and ADMIN_ID > 0 and int(user.id) == int(ADMIN_ID)
        slot_limit = db_effective_slots(user.id)
        if not is_admin_user and db_vps_count(user.id) >= slot_limit:
            return False, f"You have reached your limit of {slot_limit} VPS instance(s). Ask an administrator to add more slots.", None

        async with CAPACITY_LOCK:
            if backend == "docker":
                live_ok, live_running = await docker_running_count()
                if not live_ok:
                    live_running = db_running_count()
            else:
                live_running = sum(
                    1 for row in db_get_all_vps()
                    if str(row["backend"] or "docker").lower() == "pterodactyl"
                    and str(row["status"]).lower() == "running"
                    and not row["suspended"]
                )
            if not is_admin_user and live_running >= TOTAL_RUNNING_LIMIT:
                return False, f"Global running VPS limit reached ({TOTAL_RUNNING_LIMIT}).", None

        name = await next_container_name() if backend == "docker" else f"{VPS_HOSTNAME_PREFIX}-{user.id}-{int(datetime.now(timezone.utc).timestamp()) % 100000}"[:63]
        hostname = f"{VPS_HOSTNAME_PREFIX}-{user.id}"[:63]
        image = OS_CONFIG[normalized_os]["image"]
        resource_id: str | None = None
        ptero_server_id: int | None = None
        ptero_identifier: str | None = None

        try:
            await update_progress(progress, 1, f"Validating {backend} backend", os_type=normalized_os, location=normalized_location, ram=ram, cpu=cpu, disk=disk, name=name)

            if backend == "pterodactyl":
                await update_progress(progress, 3, "Creating Pterodactyl server", os_type=normalized_os, location=normalized_location, ram=ram, cpu=cpu, disk=disk, name=name)
                ok, create_error, attrs = await ptero_create_server(name=name, ram=ram, cpu=cpu, disk=disk)
                if not ok or not attrs:
                    return False, f"Pterodactyl server creation failed: {create_error}", None
                ptero_server_id = int(attrs.get("id") or 0) or None
                ptero_identifier = str(attrs.get("identifier") or attrs.get("uuid") or "")
                resource_id = ptero_identifier or (str(ptero_server_id) if ptero_server_id else None)
                if not resource_id:
                    raise RuntimeError("Pterodactyl did not return a server identifier.")
                await update_progress(progress, 6, "Waiting for Pterodactyl server", os_type=normalized_os, location=normalized_location, ram=ram, cpu=cpu, disk=disk, name=name)
                for _ in range(12):
                    attrs_now = await ptero_get_server(ptero_server_id)
                    if attrs_now and bool(attrs_now.get("installed", True)):
                        break
                    await asyncio.sleep(1)
                panel_url = f"{PTERO_PANEL_PUBLIC_URL}/server/{ptero_identifier}" if PTERO_PANEL_PUBLIC_URL and ptero_identifier else None
                db_upsert_user(user.id, str(user))
                db_insert_vps(
                    user_id=user.id,
                    container_id=resource_id,
                    container_name=name,
                    os_type=normalized_os,
                    location=normalized_location,
                    hostname=hostname,
                    ram=ram,
                    cpu=cpu,
                    disk=disk,
                    sshx_url=panel_url,
                    sshx_pid=None,
                    public_ipv4=None,
                    ipv4_verified_at=None,
                    backend="pterodactyl",
                    ptero_server_id=ptero_server_id,
                    ptero_identifier=ptero_identifier,
                    ptero_user_id=PTERO_DEFAULT_USER_ID,
                )
                row = db_find_vps(user.id, resource_id)
                if not row:
                    raise RuntimeError("Pterodactyl server was created but could not be saved to SQLite.")
                db_update_vps(resource_id, status=await ptero_status(row))
                await update_progress(progress, 10, "Pterodactyl VPS Ready", os_type=normalized_os, location=normalized_location, ram=ram, cpu=cpu, disk=disk, name=name)
                return True, "Pterodactyl server created successfully.", db_get_vps(row["id"]) or row

            await update_progress(progress, 2, "Checking Docker", os_type=normalized_os, location=normalized_location, ram=ram, cpu=cpu, disk=disk, name=name)
            ok, docker_error = await docker_info()
            if not ok:
                return False, f"Docker daemon is unavailable. {docker_error}", None
            await update_progress(progress, 3, "Pulling official image", os_type=normalized_os, location=normalized_location, ram=ram, cpu=cpu, disk=disk, name=name)
            pulled, pull_error = await docker_pull(image)
            if not pulled:
                return False, f"Could not pull `{image}`. {pull_error}", None
            await update_progress(progress, 4, "Creating isolated VPS", os_type=normalized_os, location=normalized_location, ram=ram, cpu=cpu, disk=disk, name=name)
            resource_id, create_error = await docker_run(image=image, hostname=hostname, ram=ram, cpu=cpu, disk=disk, container_name=name, location=normalized_location)
            if not resource_id:
                return False, f"Docker container creation failed: {create_error}", None
            await update_progress(progress, 5, "Starting VPS", os_type=normalized_os, location=normalized_location, ram=ram, cpu=cpu, disk=disk, name=name)
            running, start_error = await docker_start(resource_id)
            if not running:
                raise RuntimeError(start_error or "Container could not be started.")
            ready = False
            for _ in range(20):
                if await docker_state(resource_id) == "running":
                    ready = True
                    break
                await asyncio.sleep(0.5)
            if not ready:
                raise RuntimeError("Container started but did not reach running state.")
            await update_progress(progress, 6, "Installing secure console", os_type=normalized_os, location=normalized_location, ram=ram, cpu=cpu, disk=disk, name=name)
            console = await install_and_start_sshx(resource_id)
            if not console:
                raise RuntimeError("SSHx could not start. Check outbound HTTPS access from the container and retry.")
            await update_progress(progress, 8, "Saving VPS record", os_type=normalized_os, location=normalized_location, ram=ram, cpu=cpu, disk=disk, name=name)
            db_upsert_user(user.id, str(user))
            db_insert_vps(
                user_id=user.id,
                container_id=resource_id,
                container_name=name,
                os_type=normalized_os,
                location=normalized_location,
                hostname=hostname,
                ram=ram,
                cpu=cpu,
                disk=disk,
                sshx_url=console["url"],
                sshx_pid=console.get("pid"),
                public_ipv4=verified_ipv4 if valid_public_ipv4(verified_ipv4) else None,
                ipv4_verified_at=utc_now() if valid_public_ipv4(verified_ipv4) else None,
                backend="docker",
                ptero_server_id=None,
                ptero_identifier=None,
                ptero_user_id=None,
            )
            row = db_find_vps(user.id, resource_id)
            if not row:
                raise RuntimeError("VPS was created but could not be read from SQLite.")
            await supervise_vps_ports(row)
            await update_progress(progress, 10, "VPS Ready", os_type=normalized_os, location=normalized_location, ram=ram, cpu=cpu, disk=disk, name=name)
            return True, "Docker VPS created successfully.", row

        except asyncio.CancelledError:
            logger.error("VPS creation cancelled for user %s", user.id)
            if resource_id and backend == "docker":
                with contextlib.suppress(Exception):
                    await stop_sshx(resource_id)
                with contextlib.suppress(Exception):
                    await docker_remove(resource_id)
            elif ptero_server_id:
                with contextlib.suppress(Exception):
                    await ptero_delete_server(ptero_server_id, force=True)
            raise
        except Exception as exc:
            logger.error("VPS creation failed: %s", safe_log(exc))
            if resource_id and backend == "docker":
                with contextlib.suppress(Exception):
                    await stop_sshx(resource_id)
                with contextlib.suppress(Exception):
                    await docker_remove(resource_id)
            elif ptero_server_id:
                with contextlib.suppress(Exception):
                    await ptero_delete_server(ptero_server_id, force=True)
            return False, f"VPS creation failed safely: {safe_log(exc)}", None


async def ptero_delete_server(server_id: int, force: bool = False) -> tuple[bool, str]:
    suffix = "?force=true" if force else ""
    status, body = await ptero_request("DELETE", f"application/servers/{int(server_id)}{suffix}")
    if status not in {200, 204}:
        return False, ptero_error_message(status, body)
    return True, "Pterodactyl server deleted."


async def ptero_suspend_server(server_id: int, suspended: bool) -> tuple[bool, str]:
    action = "suspend" if suspended else "unsuspend"
    status, body = await ptero_request("POST", f"application/servers/{int(server_id)}/{action}")
    if status not in {200, 204}:
        return False, ptero_error_message(status, body)
    return True, f"Pterodactyl server {action}ed."


async def lifecycle_action(vps: sqlite3.Row, action: str) -> tuple[bool, str]:
    async with vps_lock(vps["id"]):
        backend = str(vps["backend"] or "docker").lower()

        if backend == "pterodactyl":
            server_id = int(vps["ptero_server_id"] or 0)
            if not server_id:
                return False, "Pterodactyl server ID is missing from this VPS record."

            if action in {"start", "stop", "restart"}:
                if action == "start" and vps["suspended"]:
                    return False, "This VPS is suspended by an administrator."
                if action == "start" and not vps["suspended"]:
                    current_state = await ptero_status(vps)
                    if current_state != "running":
                        running_count = sum(
                            1 for row in db_get_all_vps()
                            if str(row["backend"] or "docker").lower() == "pterodactyl"
                            and str(row["status"]).lower() == "running"
                            and not row["suspended"]
                        )
                        if running_count >= TOTAL_RUNNING_LIMIT and not (ADMIN_BYPASS_LIMITS and int(vps["user_id"]) == int(ADMIN_ID)):
                            return False, f"Global running VPS limit reached ({TOTAL_RUNNING_LIMIT})."
                ok, message = await ptero_power(vps, action)
                if ok:
                    await asyncio.sleep(1)
                    status_now = await ptero_status(vps)
                    db_update_vps(vps["container_id"], status=status_now, sshx_url=await ptero_panel_link(vps))
                    return True, message
                return False, message

            if action == "suspend":
                ok, message = await ptero_suspend_server(server_id, True)
                if ok:
                    db_update_vps(vps["container_id"], status="stopped", suspended=1)
                return ok, message

            if action == "unsuspend":
                ok, message = await ptero_suspend_server(server_id, False)
                if ok:
                    db_update_vps(vps["container_id"], suspended=0)
                return ok, message

            if action == "delete":
                for p_row in db_list_ports(vps["id"]):
                    await stop_port_forward(p_row)
                ok, message = await ptero_delete_server(server_id, force=False)
                if not ok and "not found" in message.lower():
                    ok, message = await ptero_delete_server(server_id, force=True)
                if ok:
                    db_delete_vps(vps["container_id"])
                return ok, message

            if action == "reinstall":
                status, body = await ptero_request("POST", f"application/servers/{server_id}/reinstall")
                if status not in {200, 204}:
                    return False, ptero_error_message(status, body)
                return True, "Pterodactyl reinstall requested."

            if action == "rebuild":
                status, body = await ptero_request("POST", f"application/servers/{server_id}/rebuild")
                if status not in {200, 204}:
                    return False, ptero_error_message(status, body)
                return True, "Pterodactyl rebuild requested."

            return False, "Unsupported Pterodactyl VPS action."

        container = vps["container_id"]
        exists = await docker_exists(container)
        if action == "start":
            if vps["suspended"]:
                return False, "This VPS is suspended by an administrator."
            if not exists:
                return False, "The Docker container no longer exists. Ask an administrator to recreate this VPS."
            async with CAPACITY_LOCK:
                _, current = await docker_running_count()
                already_running = (await docker_state(container)) == "running"
                if not already_running and current >= TOTAL_RUNNING_LIMIT:
                    return False, f"Global running VPS limit reached ({TOTAL_RUNNING_LIMIT})."
                ok, error = await docker_start(container)
            if not ok:
                return False, error or "Failed to start the VPS."
            for _ in range(20):
                if await docker_state(container) == "running":
                    break
                await asyncio.sleep(0.5)
            else:
                return False, "The VPS start command returned, but the container is not running."
            console = await install_and_start_sshx(container)
            db_update_vps(container, status="running", sshx_url=console["url"] if console else None, sshx_pid=console.get("pid") if console else None)
            await supervise_vps_ports(vps)
            return True, "VPS started. Console refreshed." if console else "VPS started; press Console to retry SSHx."

        if action == "stop":
            if exists:
                await stop_sshx(container)
                for p_row in db_list_ports(vps["id"]):
                    await stop_port_forward(p_row)
                if not await docker_stop(container) and await docker_state(container) not in {"exited", "stopped"}:
                    return False, "Failed to stop the VPS."
            db_update_vps(container, status="stopped", sshx_url=None, sshx_pid=None)
            return True, "VPS stopped successfully."

        if action == "restart":
            if not exists:
                return False, "The Docker container no longer exists."
            async with CAPACITY_LOCK:
                _, current = await docker_running_count()
                if current >= TOTAL_RUNNING_LIMIT and (await docker_state(container)) != "running":
                    return False, f"Global running VPS limit reached ({TOTAL_RUNNING_LIMIT})."
                await stop_sshx(container)
                ok, error = await docker_restart(container)
            if not ok:
                return False, error or "Failed to restart the VPS."
            for _ in range(20):
                if await docker_state(container) == "running":
                    break
                await asyncio.sleep(0.5)
            else:
                return False, "The VPS restart command returned, but the container is not running."
            console = await install_and_start_sshx(container)
            db_update_vps(container, status="running", sshx_url=console["url"] if console else None, sshx_pid=console.get("pid") if console else None)
            await supervise_vps_ports(vps)
            return True, "VPS restarted successfully." if console else "VPS restarted; press Console to retry SSHx."

        if action == "delete":
            for p_row in db_list_ports(vps["id"]):
                await stop_port_forward(p_row)
            if exists:
                await stop_sshx(container)
                if not await docker_remove(container):
                    return False, "Docker cleanup failed; the VPS record was kept."
            db_delete_vps(container)
            return True, "VPS deleted successfully."

        if action == "suspend":
            ok, message = await lifecycle_action(vps, "stop")
            if ok:
                db_update_vps(container, suspended=1)
                return True, "VPS stopped and suspended."
            return False, message

        if action == "unsuspend":
            db_update_vps(container, suspended=0)
            return True, "VPS unsuspended."

        return False, "Unsupported VPS action."


async def create_console_access(vps: sqlite3.Row, user: discord.User | discord.Member) -> tuple[bool, str]:
    backend = str(vps["backend"] or "docker").lower()
    if backend == "pterodactyl":
        status = await ptero_status(vps)
        if status != "running":
            return False, "Start the Pterodactyl server before opening Console."
        url = await ptero_panel_link(vps)
        if not url:
            return False, "Pterodactyl panel URL is not configured."
        db_update_vps(vps["container_id"], status="running", sshx_url=url, sshx_pid=None)
        sent = await safe_dm(user, make_embed("✨ RGNODES™ • 🦖 Pterodactyl Panel", "Open your VPS panel from the button below."), sshx_view(url))
        return True, "Pterodactyl panel access link sent by DM." if sent else "Pterodactyl panel is ready, but your DM is closed."

    state = await docker_state(vps["container_id"])
    if state != "running":
        db_update_vps(vps["container_id"], status="stopped", sshx_url=None, sshx_pid=None)
        return False, "Start the VPS before opening Console."
    console = await install_and_start_sshx(vps["container_id"])
    if not console:
        db_update_vps(vps["container_id"], sshx_url=None, sshx_pid=None)
        return False, "SSHx could not generate a link. Check container outbound connectivity and retry."
    db_update_vps(vps["container_id"], sshx_url=console["url"], sshx_pid=console.get("pid"))
    network = await detect_public_network(force=True)
    ip_ok = valid_public_ipv4(network.get("ip"))
    if ip_ok:
        db_set_vps_ipv4(vps["container_id"], network["ip"])
    dm_sent = await safe_dm(user, console_embed(vps["container_name"], console["url"], network.get("ip") if ip_ok else None, actual_location_label(network)), sshx_view(console["url"]))
    if ip_ok:
        await safe_dm(user, ipv4_dm_embed(vps, network))
    return (True, "Private SSHx link generated and sent to your DM." if dm_sent else "Console is ready, but your DM is closed. Enable DMs and press Console again.")


# ================================================================
# Public network identity + real host location
# ================================================================

NETWORK_CACHE: dict[str, str] = {"ip": "N/A", "country": "N/A", "region": "N/A", "city": "N/A"}
NETWORK_CACHE_AT = 0.0
NETWORK_LOCK = asyncio.Lock()


def valid_public_ipv4(value: str | None) -> bool:
    try:
        ip = ipaddress.ip_address(str(value or "").strip())
        return isinstance(ip, ipaddress.IPv4Address) and ip.is_global
    except ValueError:
        return False


def local_ipv4_addresses() -> set[str]:
    addresses: set[str] = set()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(item[4][0])
    except OSError:
        pass
    # iproute2 is already a bootstrap dependency; use it when available so
    # cloud secondary IPv4 addresses are detected reliably.
    return addresses


def _fetch_real_public_ipv4_sync() -> str | None:
    """Return an externally observed, globally routable IPv4 only after quorum.

    This is the host's real Internet egress/public IPv4. We intentionally do
    not invent or derive a public address from private container addresses.
    A value must be independently observed by at least two providers.
    """
    headers = {"User-Agent": "RGNODES-VPS/IPv4-verify"}
    endpoints = (
        "https://api.ipify.org",
        "https://icanhazip.com",
        "https://ifconfig.me/ip",
        "https://checkip.amazonaws.com",
    )
    observations: list[str] = []
    for url in endpoints:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=7) as resp:
                value = resp.read().decode("utf-8", "replace").strip()
            # Reject anything containing extra text, not only invalid ipaddress objects.
            if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", value) and valid_public_ipv4(value):
                observations.append(value)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            continue
    counts: dict[str, int] = {}
    for value in observations:
        counts[value] = counts.get(value, 0) + 1
    if not counts:
        return None
    winner, votes = max(counts.items(), key=lambda item: item[1])
    # Two independent confirmations are required for a verified address.
    return winner if votes >= 2 else None


async def real_public_ipv4(force: bool = False) -> str | None:
    global NETWORK_CACHE_AT, NETWORK_CACHE
    now = asyncio.get_running_loop().time()
    cached = str(NETWORK_CACHE.get("ip") or "")
    if not force and NETWORK_CACHE_AT and now - NETWORK_CACHE_AT < IPV4_REFRESH and valid_public_ipv4(cached):
        return cached
    ip = await asyncio.to_thread(_fetch_real_public_ipv4_sync)
    if ip:
        NETWORK_CACHE["ip"] = ip
        NETWORK_CACHE_AT = asyncio.get_running_loop().time()
    return ip


async def detect_public_network(force: bool = False) -> dict[str, str]:
    global NETWORK_CACHE_AT, NETWORK_CACHE
    now = asyncio.get_running_loop().time()
    if not force and NETWORK_CACHE_AT and now - NETWORK_CACHE_AT < PUBLIC_IP_REFRESH:
        return dict(NETWORK_CACHE)
    async with NETWORK_LOCK:
        now = asyncio.get_running_loop().time()
        if not force and NETWORK_CACHE_AT and now - NETWORK_CACHE_AT < PUBLIC_IP_REFRESH:
            return dict(NETWORK_CACHE)

        verified_ip = await real_public_ipv4(force=force)
        if not valid_public_ipv4(verified_ip):
            # Keep previously verified information only while it is still valid.
            return dict(NETWORK_CACHE)

        def fetch_geo() -> dict[str, str]:
            headers = {"User-Agent": "RGNODES-VPS/1.0"}
            for url in ("https://ipapi.co/json/", "https://ipinfo.io/json"):
                try:
                    req = urllib.request.Request(url, headers=headers)
                    with urllib.request.urlopen(req, timeout=7) as resp:
                        data = json.loads(resp.read().decode("utf-8", "replace"))
                    # The IP shown to users always comes from quorum verification;
                    # geo providers supply location metadata only.
                    return {
                        "ip": verified_ip,
                        "country": str(data.get("country_name") or data.get("country") or "N/A"),
                        "region": str(data.get("region") or data.get("regionName") or "N/A"),
                        "city": str(data.get("city") or "N/A"),
                    }
                except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
                    continue
            return {"ip": verified_ip, "country": "N/A", "region": "N/A", "city": "N/A"}

        NETWORK_CACHE = fetch_geo()
        NETWORK_CACHE["ip"] = verified_ip
        NETWORK_CACHE_AT = asyncio.get_running_loop().time()
        return dict(NETWORK_CACHE)


def actual_location_label(network: dict[str, str]) -> str:
    country = network.get("country", "N/A")
    if country in {"Singapore", "SG"}:
        return "Singapore 🇸🇬"
    if country in {"India", "IN"}:
        return "India 🇮🇳"
    return clean(country, 64)


# ================================================================
# Port forwarding (10 TCP mappings per VPS, supervised 24/7)
# ================================================================

PORT_LOCK = asyncio.Lock()


def port_in_use(host: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return sock.connect_ex((host, port)) == 0
    except OSError:
        return True
    finally:
        sock.close()


def port_bindable(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


async def docker_container_ip(container: str) -> str | None:
    rc, out, _ = await docker_cli("inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", container, timeout=20, retries=1)
    if rc != 0:
        return None
    ip = out.decode("utf-8", "replace").strip().splitlines()[0] if out else ""
    return ip if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", ip) else None


async def process_matches(pid: int, needle: str) -> bool:
    if pid <= 0:
        return False
    try:
        proc_cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
        return needle.lower() in proc_cmdline.lower()
    except (OSError, UnicodeError):
        return False


async def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


async def kill_host_pid(pid: int | None, expected_command: str | None = None) -> None:
    if not pid or int(pid) <= 0:
        return
    pid = int(pid)
    if expected_command and not await process_matches(pid, expected_command):
        return
    if not await process_alive(pid):
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(pid, signal.SIGTERM)
    for _ in range(10):
        if not await process_alive(pid):
            return
        await asyncio.sleep(0.1)
    if await process_alive(pid):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, signal.SIGKILL)


async def allocate_host_port() -> int | None:
    conn = db_connect()
    try:
        reserved = {int(r[0]) for r in conn.execute("SELECT host_port FROM vps_ports")}
    finally:
        conn.close()
    start = max(1024, PORT_RANGE_START)
    end = min(65535, PORT_RANGE_END)
    for port in range(start, end + 1):
        if port in reserved:
            continue
        if port_bindable(port):
            return port
    return None


async def verify_host_listener(port: int) -> bool:
    """Confirm a local TCP listener exists on the selected host port."""
    try:
        rc, out, _ = await system_command("ss", "-H", "-ltn", timeout=8) if command_available("ss") else (127, "", "")
        if rc == 0:
            for line in out.splitlines():
                if re.search(rf":{int(port)}\b", line):
                    return True
        # Fallback: probe localhost. This does not guarantee Internet reachability
        # but confirms the forwarding process is accepting local TCP connections.
        return await asyncio.to_thread(port_in_use, "127.0.0.1", int(port))
    except Exception:
        return False


async def start_port_forward(port_row: sqlite3.Row, vps: sqlite3.Row) -> tuple[bool, str]:
    if str(port_row["protocol"]).lower() != "tcp":
        return False, "Only TCP forwarding is enabled."
    if await docker_state(vps["container_id"]) != "running":
        db_update_port(port_row["id"], status="stopped", pid=None, target_ip=None)
        return False, "VPS is not running. Start it first."
    if not command_available("socat"):
        return False, "Port forwarding requires `socat`. Run `/install-system confirm` as administrator."

    public_ipv4 = str(vps["public_ipv4"] or "").strip()
    if not valid_public_ipv4(public_ipv4):
        public_ipv4 = await real_public_ipv4(force=True) or ""
        if valid_public_ipv4(public_ipv4):
            db_set_vps_ipv4(vps["container_id"], public_ipv4)
    if not valid_public_ipv4(public_ipv4):
        return False, "Verified real public IPv4 is unavailable; forwarding was not started."

    target_ip = await docker_container_ip(vps["container_id"])
    if not target_ip:
        return False, "Could not determine the VPS container IPv4."
    try:
        target_obj = ipaddress.ip_address(target_ip)
        if not isinstance(target_obj, ipaddress.IPv4Address) or not target_obj.is_private:
            return False, "Container IPv4 validation failed."
    except ValueError:
        return False, "Container IPv4 validation failed."

    host_port = int(port_row["host_port"])
    container_port = int(port_row["container_port"])
    old_pid = int(port_row["pid"]) if str(port_row["pid"] or "").isdigit() else None

    async with PORT_LOCK:
        if old_pid and await process_alive(old_pid) and await process_matches(old_pid, "socat"):
            if str(port_row["target_ip"] or "") == target_ip and await verify_host_listener(host_port):
                db_update_port(port_row["id"], status="running")
                return True, f"Port forwarding is already online on public port `{host_port}`."
            await kill_host_pid(old_pid, "socat")

        if not port_bindable(host_port):
            # It may be the same listener just not represented by our PID; refuse
            # to steal an unrelated service's port.
            db_update_port(port_row["id"], status="error", pid=None, target_ip=target_ip)
            return False, f"Public port `{host_port}` is already in use."

        pid, error = await spawn_detached(
            "socat",
            "-ly",
            f"TCP4-LISTEN:{host_port},bind=0.0.0.0,reuseaddr,fork",
            f"TCP4:{target_ip}:{container_port}",
        )
        if not pid:
            db_update_port(port_row["id"], status="error", pid=None, target_ip=target_ip)
            return False, error or "Could not start the forwarding process."

        await asyncio.sleep(0.25)
        if not await process_alive(pid) or not await process_matches(pid, "socat") or not await verify_host_listener(host_port):
            await kill_host_pid(pid, "socat")
            db_update_port(port_row["id"], status="error", pid=None, target_ip=target_ip)
            return False, f"Forwarding process started but could not be verified on port `{host_port}`."

        db_update_port(port_row["id"], status="running", pid=pid, target_ip=target_ip)
        return True, f"Port forwarding is online: public port `{host_port}` → VPS port `{container_port}/TCP`."


async def stop_port_forward(port_row: sqlite3.Row) -> None:
    await kill_host_pid(int(port_row["pid"]) if port_row["pid"] else None, "socat")
    db_update_port(port_row["id"], status="stopped", pid=None)


async def supervise_vps_ports(vps: sqlite3.Row) -> None:
    try:
        ports = db_list_ports(vps["id"])
        if not ports:
            return
        running = (await docker_state(vps["container_id"])) == "running"
        for p_row in ports:
            try:
                if running:
                    await start_port_forward(p_row, vps)
                else:
                    await stop_port_forward(p_row)
            except Exception as exc:
                logger.warning("Port supervisor failed for VPS #%s port #%s: %s", vps["id"], p_row["id"], safe_log(exc))
    except Exception as exc:
        logger.warning("Port supervisor unavailable for VPS #%s: %s", vps["id"], safe_log(exc))



# ================================================================
# Docker snapshots
# ================================================================
SNAPSHOT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


async def docker_snapshot_create(vps: sqlite3.Row, name: str) -> tuple[bool, str]:
    if str(vps["backend"] or "docker").lower() != "docker":
        return False, "Snapshots are currently available for Docker VPS instances only. Pterodactyl backups are managed by the panel."
    name = name.strip()
    if not SNAPSHOT_NAME_RE.fullmatch(name):
        return False, "Snapshot name must be 1–64 characters and use only letters, numbers, `.`, `_`, or `-`."
    if db_get_snapshot(vps["id"], name):
        return False, "A snapshot with that name already exists."
    if await docker_state(vps["container_id"]) != "running":
        return False, "Start the VPS before creating a snapshot."
    image_ref = f"rgnodes-snapshot:{int(vps['id'])}-{name.lower()}"
    rc, out, err = await docker_cli("commit", vps["container_id"], image_ref, timeout=180, retries=1)
    if rc != 0:
        return False, f"Docker snapshot failed: {safe_log(err.decode('utf-8', 'replace'))}"
    if not out.decode("utf-8", "replace").strip():
        return False, "Docker did not return a snapshot image ID."
    try:
        db_insert_snapshot(vps["id"], name, image_ref)
    except sqlite3.IntegrityError:
        return False, "Snapshot record already exists."
    return True, f"Snapshot `{name}` created successfully."


async def docker_snapshot_restore(vps: sqlite3.Row, name: str) -> tuple[bool, str]:
    if str(vps["backend"] or "docker").lower() != "docker":
        return False, "Snapshot restore is currently available for Docker VPS instances only."
    snap = db_get_snapshot(vps["id"], name.strip())
    if not snap:
        return False, "Snapshot not found."
    container = vps["container_id"]
    snapshot_image = str(snap["image_ref"])
    rc, _, err = await docker_cli("image", "inspect", snapshot_image, timeout=30, retries=1)
    if rc != 0:
        db_delete_snapshot(vps["id"], name.strip())
        return False, "Snapshot image no longer exists; its stale database record was removed."
    new_name = str(vps["container_name"])
    async with vps_lock(vps["id"]):
        await stop_sshx(container)
        for p_row in db_list_ports(vps["id"]):
            await stop_port_forward(p_row)
        if await docker_exists(container) and not await docker_remove(container):
            return False, "Could not remove the current container safely, so restore was aborted."
        new_container, create_error = await docker_run(
            image=snapshot_image,
            hostname=str(vps["hostname"]),
            ram=str(vps["ram"]),
            cpu=str(vps["cpu"]),
            disk=str(vps["disk"]),
            container_name=new_name,
            location=str(vps["location"]),
        )
        if not new_container:
            return False, f"Restore failed while recreating the container: {create_error}"
        ok, error = await docker_start(new_container)
        if not ok:
            await docker_remove(new_container)
            return False, f"Restore created a container but could not start it: {error}"
        for _ in range(20):
            if await docker_state(new_container) == "running":
                break
            await asyncio.sleep(0.5)
        else:
            await docker_remove(new_container)
            return False, "Restored container did not reach running state."
        console = await install_and_start_sshx(new_container)
        db_update_vps(
            container,
            status="running",
            sshx_url=console["url"] if console else None,
            sshx_pid=console.get("pid") if console else None,
        )
        # Replace the container id in the existing DB row.
        conn = db_connect()
        try:
            conn.execute("UPDATE vps SET container_id=?,updated_at=? WHERE id=?", (new_container, utc_now(), int(vps["id"])))
        finally:
            conn.close()
        latest = db_get_vps(vps["id"])
        if latest:
            await supervise_vps_ports(latest)
    return True, f"Snapshot `{name}` restored successfully."


async def snapshot_delete_image(vps: sqlite3.Row, name: str) -> tuple[bool, str]:
    snap = db_get_snapshot(vps["id"], name.strip())
    if not snap:
        return False, "Snapshot not found."
    image_ref = str(snap["image_ref"])
    rc, _, err = await docker_cli("image", "rm", "-f", image_ref, timeout=60, retries=1)
    if rc != 0 and "No such image" not in err.decode("utf-8", "replace"):
        return False, f"Could not remove snapshot image: {safe_log(err.decode('utf-8', 'replace'))}"
    db_delete_snapshot(vps["id"], name.strip())
    return True, f"Snapshot `{name}` deleted."


# ================================================================
# Host/system bootstrap
# ================================================================

SYSTEM_PACKAGE_LOCK = asyncio.Lock()
SYSTEM_PACKAGES = (
    "ca-certificates",
    "curl",
    "bash",
    "coreutils",
    "procps",
    "iproute2",
    "iputils-ping",
    "tar",
    "gzip",
    "unzip",
    "socat",
)
DOCKER_PACKAGE = "docker.io"


def host_os_info() -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        for raw in Path("/etc/os-release").read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" not in raw or raw.startswith("#"):
                continue
            key, value = raw.split("=", 1)
            data[key] = value.strip().strip('"')
    except (OSError, UnicodeError):
        pass
    try:
        pid1 = Path("/proc/1/comm").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        pid1 = "unknown"
    return {
        "id": data.get("ID", "unknown"),
        "name": data.get("PRETTY_NAME", data.get("NAME", "Unknown Linux")),
        "version": data.get("VERSION_ID", "unknown"),
        "pid1": pid1 or "unknown",
        "systemd": str(pid1 == "systemd" or Path("/run/systemd/system").exists()).lower(),
        "root": str(os.geteuid() == 0).lower() if hasattr(os, "geteuid") else "unknown",
    }


async def system_command(*args: str, timeout: float = 180) -> tuple[int, str, str]:
    rc, out, err = await run_process(*args, timeout=timeout)
    return rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def command_available(name: str) -> bool:
    return shutil.which(name) is not None


async def docker_daemon_ready() -> tuple[bool, str]:
    ok, detail = await docker_info()
    if ok:
        return True, "Docker daemon is reachable."
    return False, detail


async def install_system_dependencies() -> tuple[bool, str]:
    async with SYSTEM_PACKAGE_LOCK:
        info = host_os_info()
        if info["root"] != "true":
            return False, "Administrator/root privileges are required. Run the bot as root or grant it the required host permissions."

        package_manager = shutil.which("apt-get")
        if not package_manager:
            if shutil.which("apk"):
                return False, "This host uses Alpine/apk. Automatic bootstrap is intentionally limited to Debian/Ubuntu apt hosts."
            return False, "No supported package manager was found. Supported automatic bootstrap: Debian/Ubuntu with apt-get."

        os_id = info["id"].lower()
        if os_id not in {"debian", "ubuntu", "linuxmint", "pop", "raspbian"}:
            return False, f"Unsupported host OS for automatic bootstrap: `{info['name']}`."

        messages: list[str] = [f"Host: {info['name']}", f"PID 1: `{info['pid1']}`"]
        env = os.environ.copy()
        env["DEBIAN_FRONTEND"] = "noninteractive"

        async def apt(*args: str, timeout: float = 300) -> tuple[int, str, str]:
            return await system_command(
                "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", *args,
                timeout=timeout,
            )

        # Do not assume systemd exists just because systemctl exists. This is
        # important in Docker/containers/WSL-like environments.
        rc, _, err = await apt("update", "-y", timeout=300)
        if rc != 0:
            return False, "apt-get update failed:\n" + safe_log(err.strip() or "unknown apt error")

        # command/package names differ (ca-certificates has no binary check).
        # Install the full small base set; apt safely skips packages already installed.
        rc, out, err = await apt("install", "-y", "--no-install-recommends", *SYSTEM_PACKAGES, timeout=360)
        if rc != 0:
            return False, "Base dependency installation failed:\n" + safe_log(err.strip() or out.strip() or "unknown apt error")
        messages.append("Base Linux dependencies: installed/verified.")

        # Install Docker only when the CLI is actually missing. Existing Docker
        # installations are never replaced by this command.
        if not command_available("docker"):
            rc, out, err = await apt("install", "-y", "--no-install-recommends", DOCKER_PACKAGE, timeout=360)
            if rc != 0:
                return False, "Docker installation failed:\n" + safe_log(err.strip() or out.strip() or "unknown apt error")
            messages.append("Docker CLI: installed from the distro package.")
        else:
            messages.append("Docker CLI: already present.")

        # Refresh PATH-dependent checks after package installation.
        docker_bin = shutil.which("docker")
        if not docker_bin:
            return False, "\n".join(messages + ["Docker CLI is still unavailable after installation."])

        if info["systemd"] == "true" and command_available("systemctl"):
            rc_unit, _, _ = await system_command("systemctl", "list-unit-files", "docker.service", timeout=30)
            if rc_unit == 0:
                rc_start, out_start, err_start = await system_command(
                    "systemctl", "enable", "--now", "docker.service", timeout=90
                )
                if rc_start == 0:
                    messages.append("Docker service: enabled and started via systemd.")
                else:
                    messages.append("Docker service: systemd detected, but start failed: " + safe_log(err_start.strip() or out_start.strip() or "unknown error"))
            else:
                messages.append("systemd: running, but docker.service was not found; Docker daemon may be socket-managed or externally managed.")
        else:
            messages.append(
                "systemd: not active as PID 1. The command did not attempt `systemctl` "
                "because systemd cannot manage services from this environment."
            )

        ready, docker_detail = await docker_daemon_ready()
        if ready:
            messages.append("Docker daemon: ✅ reachable.")
        else:
            messages.append("Docker daemon: ⚠️ not reachable.")
            messages.append("Reason: " + safe_log(docker_detail))

        messages.append("Install-system completed without modifying VPS containers.")
        return ready, "\n".join(messages)


# ================================================================
# Bot + UI
# ================================================================

intents = discord.Intents.default()
intents.message_content = True


class RGNODESBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix=PREFIX, intents=intents, help_command=None)
        self.synced = False
        self.loops_started = False


bot = RGNODESBot()


class ManageView(discord.ui.View):
    def __init__(self, vps_id: int, owner_id: int):
        super().__init__(timeout=900)
        self.vps_id = int(vps_id)
        self.owner_id = int(owner_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        vps = db_get_vps(self.vps_id)
        allowed = bool(vps and (
            int(interaction.user.id) in {int(self.owner_id), int(ADMIN_ID)}
            or db_find_accessible_vps(int(interaction.user.id), str(self.vps_id))
        ))
        if allowed:
            return True
        await safe_respond(
            interaction,
            embed=make_embed("❌ Access Denied", "You do not have access to this VPS."),
        )
        return False

    async def run_action(self, interaction: discord.Interaction, action: str) -> None:
        if not await safe_defer(interaction, ephemeral=True):
            return
        vps = db_get_vps(self.vps_id)
        if not vps:
            await safe_followup(interaction, embed=make_embed("❌ VPS Not Found", "This VPS no longer exists."))
            self.stop()
            return
        try:
            if action in {"stats", "refresh"}:
                await show_dashboard(interaction, vps)
                return
            if action == "console":
                ok, message = await create_console_access(vps, interaction.user)
                latest = db_get_vps(self.vps_id) or vps
                if ok and latest["sshx_url"]:
                    # Keep console result as a single edited response; no duplicate follow-up.
                    await safe_edit_original(
                        interaction,
                        embed=make_embed("✅ Console Ready", message),
                        view=sshx_view(latest["sshx_url"]),
                    )
                else:
                    await safe_edit_original(
                        interaction,
                        embed=make_embed("✅ Console Ready" if ok else "❌ Console Failed", message),
                        view=ManageView(vps["id"], vps["user_id"]) if ok else ManageView(vps["id"], vps["user_id"]),
                    )
                return
            ok, message = await lifecycle_action(vps, action)
            if ok and action in {"start", "restart"}:
                await send_private_ipv4(interaction.user, db_get_vps(vps["id"]) or vps)
            if action == "delete" and ok:
                self.stop()
                await safe_edit_original(interaction, embed=make_embed("🗑️ VPS Removed", f"`{clean(vps['container_name'])}` and its forwarding rules were removed successfully."), view=None)
                return
            latest = await refresh_vps_record_state(db_get_vps(self.vps_id) or vps)
            if str(latest["backend"] or "docker").lower() == "docker":
                await asyncio.gather(supervise_vps_ports(latest), detect_public_network(), return_exceptions=True)
                ports = db_list_ports(latest["id"])
            else:
                ports = []
            stats, uptime, disk = await asyncio.gather(
                backend_stats(latest),
                backend_uptime(latest),
                backend_disk(latest),
            )
            dash = dashboard_embed(latest, stats, uptime, disk, NETWORK_CACHE, ports)
            prefix = "✅" if ok else "❌"
            dash.description = f"{prefix} {message}\n\n`{clean(latest['container_name'])}`\n\n**Status:** {status_text(latest['status'], bool(latest['suspended']))}"
            await safe_edit_original(interaction, embed=dash, view=ManageView(latest["id"], latest["user_id"]))
        except Exception as exc:
            logger.error("Manage action failed for VPS #%s: %s", self.vps_id, safe_log(exc))
            await safe_edit_original(
                interaction,
                embed=make_embed("❌ Action Failed", "The action could not be completed safely."),
                view=ManageView(vps["id"], vps["user_id"]),
            )

    @discord.ui.button(label="Start", emoji="▶️", style=discord.ButtonStyle.secondary, row=0)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button): await self.run_action(interaction, "start")
    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.secondary, row=0)
    async def stop_vps(self, interaction: discord.Interaction, button: discord.ui.Button): await self.run_action(interaction, "stop")
    @discord.ui.button(label="Console", emoji="🖥️", style=discord.ButtonStyle.secondary, row=0)
    async def console(self, interaction: discord.Interaction, button: discord.ui.Button): await self.run_action(interaction, "console")
    @discord.ui.button(label="Stats", emoji="📊", style=discord.ButtonStyle.secondary, row=0)
    async def stats(self, interaction: discord.Interaction, button: discord.ui.Button): await self.run_action(interaction, "stats")
    @discord.ui.button(label="Restart", emoji="🔄", style=discord.ButtonStyle.secondary, row=1)
    async def restart(self, interaction: discord.Interaction, button: discord.ui.Button): await self.run_action(interaction, "restart")
    @discord.ui.button(label="Refresh", emoji="🔃", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button): await self.run_action(interaction, "stats")
    @discord.ui.button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.secondary, row=1)
    async def delete_vps(self, interaction: discord.Interaction, button: discord.ui.Button): await self.run_action(interaction, "delete")


class DeployView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=180)
        self.user_id = int(user_id)
        self.selected_os = "ubuntu-24.04"
        self.selected_location = DEFAULT_LOCATION
        self.os_select = discord.ui.Select(placeholder="1️⃣ Select operating system", options=[discord.SelectOption(label=c["label"], value=k, emoji="🟠" if k.startswith("ubuntu") else "🔵") for k, c in OS_CONFIG.items()], row=0)
        self.location_select = discord.ui.Select(
            placeholder="2️⃣ Select location",
            options=[
                discord.SelectOption(
                    label=cfg["label"],
                    value=code,
                    emoji=("🇸🇬" if code == "SG" else "🇮🇳"),
                )
                for code, cfg in LOCATION_CONFIG.items()
            ],
            row=1,
        )
        self.deploy_button = discord.ui.Button(label="Deploy VPS", emoji="🚀", style=discord.ButtonStyle.secondary, row=2)
        self.os_select.callback = self.select_os
        self.location_select.callback = self.select_location
        self.deploy_button.callback = self.deploy
        self.add_item(self.os_select); self.add_item(self.location_select); self.add_item(self.deploy_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in {self.user_id, ADMIN_ID}:
            return True
        await safe_respond(interaction, embed=make_embed("❌ Access Denied", "This deployment menu belongs to another user."))
        return False

    async def select_os(self, interaction: discord.Interaction) -> None:
        self.selected_os = normalize_os(self.os_select.values[0]) or self.selected_os
        await safe_component_edit(
            interaction,
            embed=make_embed("🚀 Configure RGNODES™ VPS", f"OS: **{os_label(self.selected_os)}**\nLocation: **{location_label(self.selected_location)}**\n\nSelect both options, then press **Deploy VPS**."),
            view=self,
        )

    async def select_location(self, interaction: discord.Interaction) -> None:
        self.selected_location = normalize_location(self.location_select.values[0]) or DEFAULT_LOCATION
        await safe_component_edit(
            interaction,
            embed=make_embed("🚀 Configure RGNODES™ VPS", f"OS: **{os_label(self.selected_os)}**\nLocation: **{location_label(self.selected_location)}**\n\nSelect both options, then press **Deploy VPS**."),
            view=self,
        )

    async def deploy(self, interaction: discord.Interaction) -> None:
        self.deploy_button.disabled = True
        if not await safe_defer(interaction, ephemeral=True):
            return
        try:
            await deploy_flow(interaction, user=interaction.user, os_type=self.selected_os, location=self.selected_location, ram=DEFAULT_RAM, cpu=DEFAULT_CPU, disk=DEFAULT_DISK)
        finally:
            self.stop()


async def show_dashboard(interaction: discord.Interaction, vps: sqlite3.Row) -> None:
    vps = await refresh_vps_record_state(vps)
    backend = str(vps["backend"] or "docker").lower()
    if backend == "docker":
        await asyncio.gather(supervise_vps_ports(vps), detect_public_network(), return_exceptions=True)
        network = NETWORK_CACHE
        ports = db_list_ports(vps["id"])
    else:
        network = NETWORK_CACHE
        ports = []
    stats, uptime, disk = await asyncio.gather(
        backend_stats(vps),
        backend_uptime(vps),
        backend_disk(vps),
    )
    await safe_followup(
        interaction,
        embed=dashboard_embed(vps, stats, uptime, disk, network, ports),
        view=ManageView(vps["id"], vps["user_id"]),
    )


async def deploy_flow(interaction: discord.Interaction, *, user: discord.User | discord.Member, os_type: str, location: str, ram: str, cpu: str, disk: str, backend_override: str | None = None) -> None:
    async def progress(embed: discord.Embed) -> None:
        await safe_edit_original(interaction, embed=embed)

    try:
        ok, message, vps = await asyncio.wait_for(
            create_vps(
                user,
                os_type=os_type,
                location=location,
                ram=ram,
                cpu=cpu,
                disk=disk,
                progress=progress,
                backend_override=backend_override,
            ),
            timeout=DEPLOY_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error("Deployment timed out for user %s after %ss", user.id, DEPLOY_TIMEOUT)
        await safe_edit_original(
            interaction,
            embed=make_embed(
                "❌ VPS Creation Timed Out",
                "Docker took too long to complete the deployment. Any partially created container was cleaned up when possible. Please retry.",
            ),
        )
        return
    if not ok or not vps:
        await safe_edit_original(interaction, embed=make_embed("❌ VPS Creation Failed", message))
        return
    network = await detect_public_network(force=True)
    ip_ok = valid_public_ipv4(network.get("ip"))
    if ip_ok:
        db_set_vps_ipv4(vps["container_id"], network["ip"])
    dm_sent = await safe_dm(user, console_embed(vps["container_name"], vps["sshx_url"], network.get("ip") if ip_ok else None, actual_location_label(network)), sshx_view(vps["sshx_url"]))
    if ip_ok:
        await safe_dm(user, ipv4_dm_embed(vps, network))
    final = make_embed("✅ VPS Ready", f"Your **{os_label(vps['os_type'])}** VPS is online.")
    final.add_field(name="🖥️ VPS", value=f"`{clean(vps['container_name'])}` • ID `{vps['id']}`", inline=False)
    final.add_field(name="🌍 Location", value=location_label(vps["location"]), inline=True)
    final.add_field(name="🌐 Console", value="✅ SSHx link sent by DM" if dm_sent else "⚠️ DM unavailable", inline=True)
    await safe_edit_original(interaction, embed=final, view=ManageView(vps["id"], vps["user_id"]))


def actor_vps(interaction: discord.Interaction, identifier: str | None) -> sqlite3.Row | None:
    if interaction.user.id == ADMIN_ID:
        return db_find_vps(interaction.user.id, identifier, admin=True)
    return db_find_accessible_vps(interaction.user.id, identifier)


# ================================================================
# Slash commands (same public UI)
# ================================================================


def os_choices():
    return [app_commands.Choice(name=c["label"], value=k) for k, c in OS_CONFIG.items()]


def location_choices():
    return [app_commands.Choice(name=c["label"], value=k) for k, c in LOCATION_CONFIG.items()]


@bot.tree.command(name="deploy", description="Deploy a new RGNODES VPS.")
@app_commands.describe(os_type="Operating system", location="VPS location", ram="RAM, e.g. 2g", cpu="CPU cores, e.g. 1", disk="Disk allocation, e.g. 10g")
@app_commands.choices(os_type=os_choices(), location=location_choices())
async def deploy_slash(interaction: discord.Interaction, os_type: str, location: str = DEFAULT_LOCATION, ram: str = DEFAULT_RAM, cpu: str = DEFAULT_CPU, disk: str = DEFAULT_DISK) -> None:
    if await safe_defer(interaction, ephemeral=True):
        await deploy_flow(interaction, user=interaction.user, os_type=os_type, location=location, ram=ram, cpu=cpu, disk=disk)


@bot.tree.command(name="myvm", description="Open your newest RGNODES™ VPS dashboard.")
async def myvm_slash(interaction: discord.Interaction) -> None:
    """Open the caller's newest VPS, matching /manage with no identifier."""
    if not await safe_defer(interaction, ephemeral=True):
        return
    vps = db_get_user_vps(interaction.user.id)
    if not vps:
        await safe_followup(
            interaction,
            embed=make_embed("❌ No VPS Found", "You do not have any VPS instances yet. Use `/deploy` first."),
        )
        return
    await show_dashboard(interaction, vps[0])


@bot.tree.command(name="manage", description="Open your VPS management dashboard.")
@app_commands.describe(vps_identifier="VPS ID/name; blank uses your newest VPS")
async def manage_slash(interaction: discord.Interaction, vps_identifier: str | None = None):
    if not await safe_defer(interaction, ephemeral=True): return
    vps = actor_vps(interaction, vps_identifier)
    if not vps: await safe_followup(interaction, embed=make_embed("❌ VPS Not Found", "No matching VPS was found.")); return
    await show_dashboard(interaction, vps)


async def slash_lifecycle(interaction: discord.Interaction, identifier: str, action: str):
    if not await safe_defer(interaction, ephemeral=True): return
    vps = actor_vps(interaction, identifier)
    if not vps: await safe_followup(interaction, embed=make_embed("❌ VPS Not Found", "No matching VPS was found.")); return
    ok, message = await lifecycle_action(vps, action)
    if ok and action in {"start", "restart"}:
        await send_private_ipv4(interaction.user, db_get_vps(vps["id"]) or vps)
    await safe_followup(interaction, embed=make_embed("✅ Action Complete" if ok else "❌ Action Failed", message))


@bot.tree.command(name="start", description="Start a VPS.")
async def start_slash(interaction: discord.Interaction, vps_identifier: str): await slash_lifecycle(interaction, vps_identifier, "start")
@bot.tree.command(name="stop", description="Stop a VPS.")
async def stop_slash(interaction: discord.Interaction, vps_identifier: str): await slash_lifecycle(interaction, vps_identifier, "stop")
@bot.tree.command(name="restart", description="Restart a VPS.")
async def restart_slash(interaction: discord.Interaction, vps_identifier: str): await slash_lifecycle(interaction, vps_identifier, "restart")


@bot.tree.command(name="console", description="Generate a private SSHx console link and send it by DM.")
async def console_slash(interaction: discord.Interaction, vps_identifier: str):
    if not await safe_defer(interaction, ephemeral=True): return
    vps = actor_vps(interaction, vps_identifier)
    if not vps: await safe_followup(interaction, embed=make_embed("❌ VPS Not Found", "No matching VPS was found.")); return
    ok, message = await create_console_access(vps, interaction.user)
    await safe_followup(interaction, embed=make_embed("✅ Console Ready" if ok else "❌ Console Failed", message))


@bot.tree.command(name="vps-info", description="Show full live VPS information.")
async def vps_info_slash(interaction: discord.Interaction, vps_identifier: str | None = None):
    if not await safe_defer(interaction, ephemeral=True): return
    vps = actor_vps(interaction, vps_identifier)
    if not vps: await safe_followup(interaction, embed=make_embed("❌ VPS Not Found", "No matching VPS was found.")); return
    await show_dashboard(interaction, vps)


@bot.tree.command(name="remove", description="Delete a VPS and its Docker container.")
async def remove_slash(interaction: discord.Interaction, vps_identifier: str): await slash_lifecycle(interaction, vps_identifier, "delete")


@bot.tree.command(name="list", description="List your VPS instances.")
async def list_slash(interaction: discord.Interaction):
    if not await safe_defer(interaction, ephemeral=True): return
    rows = db_get_user_vps(interaction.user.id)
    embed = make_embed("📋 Your RGNODES™ VPS")
    if not rows: embed.description = "You do not have any VPS instances."
    for row in rows[:25]:
        embed.add_field(name=f"{status_text(row['status'], bool(row['suspended']))} {clean(row['container_name'])}", value=f"ID: `{row['id']}` • {os_label(row['os_type'])}\n{clean(row['ram'])} RAM • {clean(row['cpu'])} CPU • {clean(row['disk'])} Disk • {location_label(row['location'])}", inline=False)
    await safe_followup(interaction, embed=embed)


@bot.tree.command(name="ping", description="Check RGNODES™ bot latency.")
async def ping_slash(interaction: discord.Interaction):
    await safe_respond(interaction, embed=make_embed("🏓 Pong!", f"Discord latency: `{round(bot.latency * 1000)}ms`"))



@bot.tree.command(name="myvps", description="Open your newest RGNODES™ VPS dashboard.")
async def myvps_slash_alias(interaction: discord.Interaction):
    await myvm_slash(interaction)


@bot.tree.command(name="vps-stats", description="Show live VPS statistics.")
async def vps_stats_slash(interaction: discord.Interaction, vps_identifier: str):
    if not await safe_defer(interaction, ephemeral=True):
        return
    vps = actor_vps(interaction, vps_identifier)
    if not vps:
        await safe_followup(interaction, embed=make_embed("❌ VPS Not Found", "No matching VPS was found."))
        return
    vps = await refresh_vps_record_state(vps)
    stats, uptime, disk = await asyncio.gather(backend_stats(vps), backend_uptime(vps), backend_disk(vps))
    await safe_followup(interaction, embed=make_embed(
        f"📈 VPS Stats • {clean(vps['container_name'])}",
        f"CPU: `{clean(stats.get('cpu'))}`\nMemory: `{clean(stats.get('memory'))}`\n"
        f"Disk: `{clean(disk.get('used'))} / {clean(vps['disk'])}`\n"
        f"Network: `{clean(stats.get('network'))}`\nUptime: `{clean(uptime)}`"
    ))


@bot.tree.command(name="vps-uptime", description="Show VPS uptime.")
async def vps_uptime_slash(interaction: discord.Interaction, vps_identifier: str):
    if not await safe_defer(interaction, ephemeral=True):
        return
    vps = actor_vps(interaction, vps_identifier)
    if not vps:
        await safe_followup(interaction, embed=make_embed("❌ VPS Not Found", "No matching VPS was found."))
        return
    await safe_followup(interaction, embed=make_embed("⏱️ VPS Uptime", f"`{clean(await backend_uptime(vps))}`"))


@bot.tree.command(name="restart-vps", description="Restart a VPS safely.")
async def restart_vps_slash(interaction: discord.Interaction, vps_identifier: str):
    await slash_lifecycle(interaction, vps_identifier, "restart")


@bot.tree.command(name="snapshot", description="Create a Docker VPS snapshot.")
async def snapshot_slash(interaction: discord.Interaction, vps_identifier: str, name: str | None = None):
    if not await safe_defer(interaction, ephemeral=True):
        return
    vps = actor_vps(interaction, vps_identifier)
    if not vps:
        await safe_followup(interaction, embed=make_embed("❌ VPS Not Found", "No matching VPS was found."))
        return
    snap_name = (name or "").strip() or f"snapshot-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    ok, message = await docker_snapshot_create(vps, snap_name)
    await safe_followup(interaction, embed=make_embed("📸 Snapshot Created" if ok else "❌ Snapshot Failed", message))


@bot.tree.command(name="list-snapshots", description="List snapshots for a VPS.")
async def list_snapshots_slash(interaction: discord.Interaction, vps_identifier: str):
    if not await safe_defer(interaction, ephemeral=True):
        return
    vps = actor_vps(interaction, vps_identifier)
    if not vps:
        await safe_followup(interaction, embed=make_embed("❌ VPS Not Found", "No matching VPS was found."))
        return
    if str(vps["backend"] or "docker").lower() != "docker":
        await safe_followup(interaction, embed=make_embed("🦖 Pterodactyl Backups", "This VPS uses Pterodactyl. Use the Panel's backup system instead of local Docker snapshots."))
        return
    rows = db_list_snapshots(vps["id"])
    body = "\n".join(f"`{clean(r['name'])}` • {str(r['created_at'])[:19]} UTC" for r in rows[:20]) or "No snapshots."
    await safe_followup(interaction, embed=make_embed(f"📋 Snapshots • {clean(vps['container_name'])}", body))


@bot.tree.command(name="restore-snapshot", description="Restore a Docker VPS snapshot.")
async def restore_snapshot_slash(interaction: discord.Interaction, vps_identifier: str, name: str):
    if not await safe_defer(interaction, ephemeral=True):
        return
    vps = actor_vps(interaction, vps_identifier)
    if not vps:
        await safe_followup(interaction, embed=make_embed("❌ VPS Not Found", "No matching VPS was found."))
        return
    if not db_is_owner_or_admin(interaction.user.id, vps):
        await safe_followup(interaction, embed=make_embed("❌ Permission Denied", "Only the VPS owner or administrator can restore snapshots."))
        return
    ok, message = await docker_snapshot_restore(vps, name)
    await safe_followup(interaction, embed=make_embed("✅ Snapshot Restored" if ok else "❌ Restore Failed", message))


@bot.tree.command(name="manage-shared", description="Manage a user's shared VPS access.")
async def manage_shared_slash(interaction: discord.Interaction, owner_user: discord.User, vps_identifier: str):
    if not await safe_defer(interaction, ephemeral=True):
        return
    vps = db_find_vps(owner_user.id, vps_identifier)
    if not vps or not db_is_owner_or_admin(interaction.user.id, vps):
        await safe_followup(interaction, embed=make_embed("❌ Permission Denied", "Only the VPS owner or administrator can manage shared access."))
        return
    shared = db_list_shared(vps["id"])
    body = "\n".join(f"<@{r['user_id']}> • granted by <@{r['shared_by']}>" for r in shared) or "No users currently have shared access."
    await safe_followup(interaction, embed=make_embed(f"👥 Shared Access • {clean(vps['container_name'])}", body), view=ManageView(vps["id"], vps["user_id"]))


@bot.tree.command(name="share-ruser", description="Revoke a user's shared VPS access.")
async def share_ruser_slash(interaction: discord.Interaction, vps_identifier: str, target_user: discord.User):
    if not await safe_defer(interaction, ephemeral=True):
        return
    vps = actor_vps(interaction, vps_identifier)
    if not vps or not db_is_owner_or_admin(interaction.user.id, vps):
        await safe_followup(interaction, embed=make_embed("❌ Permission Denied", "Only the VPS owner or administrator can revoke shared access."))
        return
    ok, message = db_unshare_vps(vps["id"], target_user.id)
    await safe_followup(interaction, embed=make_embed("✅ Access Removed" if ok else "⚠️ Nothing Changed", f"{message}\nVPS: `{clean(vps['container_name'])}` • User: <@{target_user.id}>"))


@bot.tree.command(name="serverstats", description="Show host and VPS server statistics.")
async def serverstats_slash(interaction: discord.Interaction):
    await safe_defer(interaction, ephemeral=True)
    info = host_os_info()
    try:
        load = os.getloadavg()[0]
        load_text = f"{load:.2f}"
    except (AttributeError, OSError):
        load_text = "N/A"
    try:
        meminfo = Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace")
        total_m = re.search(r"^MemTotal:\s+(\d+)", meminfo, re.M)
        avail_m = re.search(r"^MemAvailable:\s+(\d+)", meminfo, re.M)
        ram_text = f"{format_bytes((int(total_m.group(1))-int(avail_m.group(1)))*1024)} / {format_bytes(int(total_m.group(1))*1024)}" if total_m and avail_m else "N/A"
    except Exception:
        ram_text = "N/A"
    du = shutil.disk_usage("/")
    ok, active = await docker_running_count()
    if not ok:
        active = db_running_count()
    await safe_followup(interaction, embed=make_embed("📊 RGNODES™ • Server Statistics",
        f"OS: `{clean(info['name'], 100)}`\nPID 1: `{clean(info['pid1'])}`\n"
        f"Host uptime: `{await host_uptime()}`\nRAM: `{ram_text}`\n"
        f"Disk: `{format_bytes(du.used)} / {format_bytes(du.total)}`\n"
        f"CPU cores: `{os.cpu_count() or 1}` • Load: `{load_text}`\nActive VPS: `{active}`"))


@bot.tree.command(name="thresholds", description="Show RGNODES™ resource thresholds.")
async def thresholds_slash(interaction: discord.Interaction):
    await safe_respond(interaction, embed=make_embed("📋 RGNODES™ • Thresholds",
        f"Per-user slots: `{db_effective_slots(interaction.user.id)}`\nGlobal running VPS: `{TOTAL_RUNNING_LIMIT}`\n"
        f"Max ports/VPS: `{MAX_PORTS_PER_VPS}`\nPort range: `{PORT_RANGE_START}-{PORT_RANGE_END}`\n"
        "RAM per VPS: `256MB-256GB`\nDisk per VPS: `1GB-10TB`\nCPU per VPS: `>0-64 cores`"))


@bot.tree.command(name="set-status", description="Admin: set the bot presence.")
@app_commands.choices(status_type=[
    app_commands.Choice(name="Playing", value="playing"),
    app_commands.Choice(name="Watching", value="watching"),
    app_commands.Choice(name="Listening", value="listening"),
    app_commands.Choice(name="Competing", value="competing"),
])
async def set_status_slash(interaction: discord.Interaction, status_type: str, name: str):
    if not admin_ok(interaction):
        await safe_respond(interaction, embed=make_embed("❌ Permission Denied", "Administrator access is required."))
        return
    if status_type == "watching":
        activity = discord.Activity(type=discord.ActivityType.watching, name=name)
    elif status_type == "listening":
        activity = discord.Activity(type=discord.ActivityType.listening, name=name)
    elif status_type == "competing":
        activity = discord.Activity(type=discord.ActivityType.competing, name=name)
    else:
        activity = discord.Game(name=name)
    await bot.change_presence(activity=activity)
    await safe_respond(interaction, embed=make_embed("✅ Status Updated", f"Type: `{status_type}`\nName: `{clean(name, 200)}`"))


@bot.tree.command(name="about", description="Show RGNODES™ information.")
async def about_slash(interaction: discord.Interaction):
    embed = make_embed("☁️ RGNODES™ VPS Management", "Fast Docker VPS management with private SSHx access.")
    embed.add_field(name="🛠️ Stack", value="Python 3 • discord.py • Docker • SQLite WAL", inline=False)
    embed.add_field(name="🔐 Security", value="Console links are generated on demand and sent by DM only.", inline=False)
    await safe_respond(interaction, embed=embed)


@bot.tree.command(name="logs", description="View recent logs for your VPS.")
async def logs_slash(interaction: discord.Interaction, vps_identifier: str, lines: int = 50):
    if not await safe_defer(interaction, ephemeral=True): return
    vps = actor_vps(interaction, vps_identifier)
    if not vps: await safe_followup(interaction, embed=make_embed("❌ VPS Not Found", "No matching VPS was found.")); return
    logs = await docker_logs(vps["container_id"], lines)
    embed = make_embed(f"📜 Logs • {clean(vps['container_name'])}")
    # Prevent user/container output from closing the Discord code block.
    logs = str(logs).replace("```", "'''")
    embed.add_field(name="Recent output", value=f"```text\n{logs[:3900]}\n```", inline=False)
    await safe_followup(interaction, embed=embed)


@bot.tree.command(name="ports", description="Show your VPS port forwarding rules.")
async def ports_slash(interaction: discord.Interaction, vps_identifier: str):
    if not await safe_defer(interaction, ephemeral=True):
        return
    vps = actor_vps(interaction, vps_identifier)
    if not vps:
        await safe_followup(interaction, embed=make_embed("❌ VPS Not Found", "No matching VPS was found."))
        return
    await supervise_vps_ports(vps)
    ports = db_list_ports(vps["id"])
    network = await detect_public_network(force=True)
    if valid_public_ipv4(network.get("ip")):
        db_set_vps_ipv4(vps["container_id"], network["ip"])
        await safe_dm(interaction.user, ipv4_dm_embed(vps, network))
    embed = make_embed(f"🌐 Ports • {clean(vps['container_name'])}", f"IPv4: 🔒 Sent by DM\nUsed: `{len(ports)}/{MAX_PORTS_PER_VPS}`")
    if not ports:
        embed.description += "\n\nNo forwarding rules configured. Use `/port-add`."
    for p_row in ports:
        embed.add_field(name=f"#{p_row['id']} • {str(p_row['protocol']).upper()}", value=f"Public port `{p_row['host_port']}` → VPS port `:{p_row['container_port']}` • **{clean(p_row['status']).upper()}**", inline=False)
    await safe_followup(interaction, embed=embed)


@bot.tree.command(name="port-add", description="Add a TCP port forward (maximum 10 per VPS).")
@app_commands.describe(vps_identifier="VPS ID/name", container_port="Port inside the VPS", host_port="Public host port; leave 0 for automatic allocation")
async def port_add_slash(interaction: discord.Interaction, vps_identifier: str, container_port: int, host_port: int = 0):
    if not await safe_defer(interaction, ephemeral=True):
        return
    if not 1 <= container_port <= 65535:
        await safe_followup(interaction, embed=make_embed("❌ Invalid Port", "Container port must be between 1 and 65535."))
        return
    vps = actor_vps(interaction, vps_identifier)
    if not vps:
        await safe_followup(interaction, embed=make_embed("❌ VPS Not Found", "No matching VPS was found."))
        return
    async with PORT_LOCK:
        ports = db_list_ports(vps["id"])
        if len(ports) >= MAX_PORTS_PER_VPS:
            await safe_followup(interaction, embed=make_embed("⚠️ Port Limit Reached", f"A VPS can use at most `{MAX_PORTS_PER_VPS}` forwarding rules."))
            return
        if db_find_port(vps["id"], container_port, "tcp"):
            await safe_followup(interaction, embed=make_embed("⚠️ Already Exists", "That container port is already forwarded."))
            return
        if host_port == 0:
            host_port = await allocate_host_port() or 0
        if not 1024 <= host_port <= 65535 or not port_bindable(host_port):
            await safe_followup(interaction, embed=make_embed("❌ Host Port Unavailable", "Choose a free host port from 1024–65535, or use `0` for automatic allocation."))
            return
        try:
            port_id = db_insert_port(vps["id"], container_port, host_port, "tcp")
        except sqlite3.IntegrityError:
            await safe_followup(interaction, embed=make_embed("❌ Port Conflict", "That public port is already reserved by another VPS."))
            return
    row = db_get_port(port_id)
    ok, message = await start_port_forward(row, vps) if row else (False, "Forwarding record disappeared unexpectedly.")
    if not ok:
        if row:
            await stop_port_forward(row)
        db_delete_port(port_id)
    else:
        await send_private_ipv4(interaction.user, db_get_vps(vps["id"]) or vps)
    await safe_followup(interaction, embed=make_embed("✅ Port Forward Added" if ok else "❌ Port Forward Failed", message))


@bot.tree.command(name="port-remove", description="Remove a TCP port forward.")
async def port_remove_slash(interaction: discord.Interaction, vps_identifier: str, port_id: int):
    if not await safe_defer(interaction, ephemeral=True):
        return
    vps = actor_vps(interaction, vps_identifier)
    row = db_get_port(port_id)
    if not vps or not row or int(row["vps_id"]) != int(vps["id"]):
        await safe_followup(interaction, embed=make_embed("❌ Port Not Found", "That forwarding rule does not belong to the selected VPS."))
        return
    await stop_port_forward(row)
    db_delete_port(port_id)
    await safe_followup(interaction, embed=make_embed("🗑️ Port Forward Removed", f"Forwarding rule `#{port_id}` has been removed."))


@bot.tree.command(name="help", description="Open the RGNODES command navigator.")
async def help_slash(interaction: discord.Interaction):
    admin = interaction.user.id == ADMIN_ID
    await safe_respond(interaction, embed=build_help_embed(admin, "home"), ephemeral=True, view=HelpView(interaction.user.id, admin))


# ================================================================
# Admin commands — compatibility surface kept
# ================================================================


def admin_ok(source: discord.Interaction | commands.Context) -> bool:
    """Check the configured admin for slash interactions and prefix contexts."""
    user = getattr(source, "user", None)
    if user is None:
        user = getattr(source, "author", None)
    return bool(user and ADMIN_ID > 0 and int(user.id) == int(ADMIN_ID))


@bot.tree.command(name="admin-create", description="Admin: create a VPS for another user.")
@app_commands.choices(os_type=os_choices(), location=location_choices())
async def admin_create(interaction: discord.Interaction, target_user: discord.User, os_type: str, location: str = DEFAULT_LOCATION, ram: str = DEFAULT_RAM, cpu: str = DEFAULT_CPU, disk: str = DEFAULT_DISK):
    if not admin_ok(interaction): await safe_respond(interaction, embed=make_embed("❌ Permission Denied", "Administrator access is required.")); return
    if await safe_defer(interaction, ephemeral=True): await deploy_flow(interaction, user=target_user, os_type=os_type, location=location, ram=ram, cpu=cpu, disk=disk)


@bot.tree.command(name="add-slots", description="Admin: add VPS slots to a user.")
@app_commands.describe(target_user="Discord user", slots="How many additional VPS slots to add")
async def add_slots_slash(interaction: discord.Interaction, target_user: discord.User, slots: int):
    if not admin_ok(interaction):
        await safe_respond(interaction, embed=make_embed("❌ Permission Denied", "Administrator access is required."))
        return
    if slots <= 0 or slots > 1000:
        await safe_respond(interaction, embed=make_embed("❌ Invalid Slot Amount", "Choose a positive slot amount up to 1000."))
        return
    db_upsert_user(target_user.id, str(target_user))
    total = db_add_slots(target_user.id, slots)
    await safe_respond(interaction, embed=make_embed("🎟️ Slots Added", f"<@{target_user.id}> now has **{total} VPS slots**.\n\nAdditional slots added: `{slots}`"))


@bot.tree.command(name="remove-all", description="Admin: delete every RGNODES VPS and reset VPS IDs.")
@app_commands.describe(confirm="Must be true to perform this destructive action")
async def remove_all_slash(interaction: discord.Interaction, confirm: bool = False):
    if not admin_ok(interaction):
        await safe_respond(interaction, embed=make_embed("❌ Permission Denied", "Administrator access is required."))
        return
    if not confirm:
        await safe_respond(interaction, embed=make_embed("⚠️ Confirm Remove All", "This permanently removes all managed VPS containers, forwarding rules and VPS records. It also resets the VPS ID sequence. Run `/remove-all confirm:true` to continue."))
        return
    if not await safe_defer(interaction, ephemeral=True):
        return
    rows = db_get_all_vps()
    removed = 0
    failed = 0
    for vps in rows:
        try:
            ok, _ = await lifecycle_action(vps, "delete")
            removed += 1 if ok else 0
            failed += 0 if ok else 1
        except Exception as exc:
            failed += 1
            logger.exception("remove-all failed for VPS #%s: %s", vps["id"], exc)
    # Clean orphaned managed Docker containers and reset relational records/sequence.
    rc, out, _ = await docker_cli("ps", "-aq", "--format", "{{.ID}}\t{{.Names}}", timeout=60, retries=1)
    orphans = []
    if rc == 0:
        for line in out.decode("utf-8", "replace").splitlines():
            parts = line.strip().split("\t", 1)
            if len(parts) == 2 and re.fullmatch(r"rgnodes-\d+", parts[1].strip(), flags=re.I):
                orphans.append(parts[0])
    for cid in orphans:
        with contextlib.suppress(Exception):
            await docker_remove(cid)
    db_delete_all_vps()
    await safe_followup(interaction, embed=make_embed("✅ Remove All Complete", f"Managed VPS removed: `{removed}`\nFailures: `{failed}`\nVPS ID sequence reset to `1`.\nAll forwarding records were cleared."))


@bot.tree.command(name="admin-list", description="Admin: list all VPS instances.")
async def admin_list(interaction: discord.Interaction):
    if not admin_ok(interaction): await safe_respond(interaction, embed=make_embed("❌ Permission Denied", "Administrator access is required.")); return
    if not await safe_defer(interaction, ephemeral=True): return
    rows = db_get_all_vps(); embed = make_embed("🗂️ Admin • All VPS")
    if not rows: embed.description = "No VPS instances found."
    for row in rows[:25]: embed.add_field(name=f"#{row['id']} • {clean(row['container_name'])}", value=f"Owner: <@{row['user_id']}>\n{status_text(row['status'], bool(row['suspended']))} • {location_label(row['location'])}", inline=False)
    await safe_followup(interaction, embed=embed)


@bot.tree.command(name="admin-delete-user", description="Admin: delete one VPS belonging to a user.")
async def admin_delete_user(interaction: discord.Interaction, target_user: discord.User, vps_identifier: str):
    if not admin_ok(interaction): await safe_respond(interaction, embed=make_embed("❌ Permission Denied", "Administrator access is required.")); return
    if not await safe_defer(interaction, ephemeral=True): return
    vps = db_find_vps(target_user.id, vps_identifier)
    if not vps: await safe_followup(interaction, embed=make_embed("❌ VPS Not Found", "No matching VPS was found.")); return
    ok, message = await lifecycle_action(vps, "delete")
    await safe_followup(interaction, embed=make_embed("✅ VPS Deleted" if ok else "❌ Delete Failed", message))


@bot.tree.command(name="admin-ban", description="Admin: block a user from creating VPS instances.")
async def admin_ban(interaction: discord.Interaction, target_user: discord.User):
    if not admin_ok(interaction): await safe_respond(interaction, embed=make_embed("❌ Permission Denied", "Administrator access is required.")); return
    db_set_ban(target_user.id, True); await safe_respond(interaction, embed=make_embed("✅ User Restricted", f"<@{target_user.id}> can no longer create VPS instances."))


@bot.tree.command(name="admin-unban", description="Admin: allow a user to create VPS instances again.")
async def admin_unban(interaction: discord.Interaction, target_user: discord.User):
    if not admin_ok(interaction): await safe_respond(interaction, embed=make_embed("❌ Permission Denied", "Administrator access is required.")); return
    db_set_ban(target_user.id, False); await safe_respond(interaction, embed=make_embed("✅ User Restored", f"<@{target_user.id}> may create VPS instances again."))


@bot.tree.command(name="sshx", description="Generate a fresh private SSHx browser console link.")
async def sshx_slash(interaction: discord.Interaction, vps_identifier: str): await console_slash(interaction, vps_identifier)


@bot.tree.command(name="share-user", description="Share your VPS with another Discord user.")
async def share_user_slash(interaction: discord.Interaction, vps_identifier: str, target_user: discord.User):
    if not await safe_defer(interaction, ephemeral=True):
        return
    vps = actor_vps(interaction, vps_identifier)
    if not vps or not db_is_owner_or_admin(interaction.user.id, vps):
        await safe_followup(interaction, embed=make_embed("❌ Permission Denied", "Only the VPS owner or administrator can share this VPS."))
        return
    ok, message = db_share_vps(vps["id"], target_user.id, interaction.user.id)
    await safe_followup(interaction, embed=make_embed("✅ VPS Shared" if ok else "⚠️ Share Failed", f"{message}\nVPS: `{vps['container_name']}` • User: <@{target_user.id}>"))


@bot.tree.command(name="unshare-user", description="Remove a user's access to your VPS.")
async def unshare_user_slash(interaction: discord.Interaction, vps_identifier: str, target_user: discord.User):
    if not await safe_defer(interaction, ephemeral=True):
        return
    vps = actor_vps(interaction, vps_identifier)
    if not vps or not db_is_owner_or_admin(interaction.user.id, vps):
        await safe_followup(interaction, embed=make_embed("❌ Permission Denied", "Only the VPS owner or administrator can remove shared access."))
        return
    ok, message = db_unshare_vps(vps["id"], target_user.id)
    await safe_followup(interaction, embed=make_embed("✅ Access Removed" if ok else "⚠️ Nothing Changed", f"{message}\nVPS: `{vps['container_name']}` • User: <@{target_user.id}>"))


@bot.tree.command(name="reinstall", description="Safely validate a VPS reinstall request.")
async def reinstall_slash(interaction: discord.Interaction, vps_identifier: str):
    if not await safe_defer(interaction, ephemeral=True): return
    vps = actor_vps(interaction, vps_identifier)
    if not vps: await safe_followup(interaction, embed=make_embed("❌ VPS Not Found", "No matching VPS was found.")); return
    await safe_followup(interaction, embed=make_embed("⚠️ Reinstall Protection", "Reinstall is intentionally not destructive in this build. Your VPS is unchanged. Use `/remove` and `/deploy` only when you explicitly want a fresh container."))


@bot.tree.command(name="admin-manage", description="Admin: control a user's VPS.")
@app_commands.choices(action=[app_commands.Choice(name=x.title(), value=x) for x in ("start", "stop", "restart", "delete", "suspend", "unsuspend")])
async def admin_manage(interaction: discord.Interaction, target_user: discord.User, vps_identifier: str, action: str):
    if not admin_ok(interaction): await safe_respond(interaction, embed=make_embed("❌ Permission Denied", "Administrator access is required.")); return
    if not await safe_defer(interaction, ephemeral=True): return
    vps = db_find_vps(target_user.id, vps_identifier)
    if not vps: await safe_followup(interaction, embed=make_embed("❌ VPS Not Found", "No matching VPS was found.")); return
    if action in {"start", "stop", "restart", "delete"}:
        ok, message = await lifecycle_action(vps, action)
    elif action == "suspend":
        ok, message = await lifecycle_action(vps, "stop")
        if ok: db_update_vps(vps["container_id"], suspended=1); message = "VPS stopped and suspended."
    else:
        db_update_vps(vps["container_id"], suspended=0); ok, message = True, "VPS unsuspended."
    await safe_followup(interaction, embed=make_embed("✅ Admin Action Complete" if ok else "❌ Admin Action Failed", message))


@bot.tree.command(name="admin-list-users", description="Admin: list users and VPS counts.")
async def admin_list_users(interaction: discord.Interaction):
    if not admin_ok(interaction): await safe_respond(interaction, embed=make_embed("❌ Permission Denied", "Administrator access is required.")); return
    if not await safe_defer(interaction, ephemeral=True): return
    conn = db_connect()
    try:
        rows = conn.execute("SELECT u.user_id,u.username,COUNT(v.id) AS total_vps,SUM(CASE WHEN v.status='running' AND v.suspended=0 THEN 1 ELSE 0 END) AS running_vps FROM users u LEFT JOIN vps v ON u.user_id=v.user_id GROUP BY u.user_id,u.username ORDER BY total_vps DESC").fetchall()
    finally: conn.close()
    embed = make_embed("👥 Admin • Users")
    if not rows: embed.description = "No users have been recorded yet."
    for row in rows[:25]: embed.add_field(name=clean(row["username"]), value=f"Total: `{row['total_vps']}` • Running: `{row['running_vps'] or 0}`", inline=False)
    await safe_followup(interaction, embed=embed)


@bot.tree.command(name="admin-stats", description="Admin: show RGNODES statistics.")
async def admin_stats(interaction: discord.Interaction):
    if not admin_ok(interaction): await safe_respond(interaction, embed=make_embed("❌ Permission Denied", "Administrator access is required.")); return
    if not await safe_defer(interaction, ephemeral=True): return
    conn = db_connect()
    try:
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]; total = conn.execute("SELECT COUNT(*) FROM vps").fetchone()[0]; running = conn.execute("SELECT COUNT(*) FROM vps WHERE status='running' AND suspended=0").fetchone()[0]; banned = conn.execute("SELECT COUNT(*) FROM bans").fetchone()[0]
    finally: conn.close()
    embed = make_embed("📊 Admin • Statistics")
    docker_ok, docker_running = await docker_running_count()
    if not docker_ok:
        docker_running = db_running_count()
    ptero_running = sum(1 for row in db_get_all_vps() if str(row["backend"] or "docker").lower() == "pterodactyl" and str(row["status"]).lower() in {"running", "starting", "restarting"} and not row["suspended"])
    live_running = docker_running + ptero_running
    for n, v in (("Users", users), ("Banned", banned), ("Total VPS", total), ("DB Running", running), ("Live Active", live_running), ("Running limit", TOTAL_RUNNING_LIMIT)): embed.add_field(name=n, value=str(v), inline=True)
    await safe_followup(interaction, embed=embed)


@bot.tree.command(name="admin-vps-info", description="Admin: view a user's VPS dashboard.")
async def admin_vps_info(interaction: discord.Interaction, target_user: discord.User, vps_identifier: str):
    if not admin_ok(interaction): await safe_respond(interaction, embed=make_embed("❌ Permission Denied", "Administrator access is required.")); return
    if not await safe_defer(interaction, ephemeral=True): return
    vps = db_find_vps(target_user.id, vps_identifier)
    if not vps: await safe_followup(interaction, embed=make_embed("❌ VPS Not Found", "No matching VPS was found.")); return
    vps = await refresh_vps_record_state(vps)
    stats, uptime, disk = await asyncio.gather(backend_stats(vps), backend_uptime(vps), backend_disk(vps))
    ports = db_list_ports(vps["id"]) if str(vps["backend"] or "docker").lower() == "docker" else []
    await safe_followup(interaction, embed=dashboard_embed(vps, stats, uptime, disk, NETWORK_CACHE, ports))


@bot.tree.command(name="admin-logs", description="Admin: view a user's VPS logs.")
async def admin_logs(interaction: discord.Interaction, target_user: discord.User, vps_identifier: str, lines: int = 50):
    if not admin_ok(interaction): await safe_respond(interaction, embed=make_embed("❌ Permission Denied", "Administrator access is required.")); return
    if not await safe_defer(interaction, ephemeral=True): return
    vps = db_find_vps(target_user.id, vps_identifier)
    if not vps: await safe_followup(interaction, embed=make_embed("❌ VPS Not Found", "No matching VPS was found.")); return
    logs = (await docker_logs(vps["container_id"], lines)).replace("```", "'''")
    embed = make_embed(f"📜 Admin Logs • {clean(vps['container_name'])}"); embed.add_field(name="Recent output", value=f"```text\n{logs[:3900]}\n```", inline=False)
    await safe_followup(interaction, embed=embed)


@bot.tree.command(name="admin-kill-all", description="Admin: stop all running VPS instances.")
async def admin_kill_all(interaction: discord.Interaction):
    if not admin_ok(interaction): await safe_respond(interaction, embed=make_embed("❌ Permission Denied", "Administrator access is required.")); return
    if not await safe_defer(interaction, ephemeral=True): return
    results = await asyncio.gather(*(lifecycle_action(vps, "stop") for vps in db_get_all_vps() if vps["status"] == "running"), return_exceptions=True)
    stopped = sum(1 for x in results if isinstance(x, tuple) and x[0])
    await safe_followup(interaction, embed=make_embed("🛑 Admin • Kill All", f"Stopped `{stopped}` VPS instance(s)."))


async def optional_command_notice(interaction: discord.Interaction, feature: str) -> None:
    if not admin_ok(interaction):
        await safe_respond(interaction, embed=make_embed("❌ Permission Denied", "Administrator access is required."))
        return
    await safe_respond(interaction, embed=make_embed(f"⚠️ {feature} Not Configured", "This optional integration is not configured on the current host."))


@bot.tree.command(name="admin-ptero-create", description="Admin: create a Pterodactyl server for a user.")
async def admin_ptero_create(interaction: discord.Interaction, target_user: discord.User):
    if not admin_ok(interaction):
        await safe_respond(interaction, embed=make_embed("❌ Permission Denied", "Administrator access is required."))
        return
    if not await safe_defer(interaction, ephemeral=True):
        return
    await deploy_flow(interaction, user=target_user, os_type="ubuntu-24.04", location=DEFAULT_LOCATION, ram=DEFAULT_RAM, cpu=DEFAULT_CPU, disk=DEFAULT_DISK, backend_override="pterodactyl")


@bot.tree.command(name="admin-ptero-power", description="Admin: control a Pterodactyl server power state.")
@app_commands.choices(action=[app_commands.Choice(name=x.title(), value=x) for x in ("start", "stop", "restart", "kill")])
async def admin_ptero_power(interaction: discord.Interaction, identifier: str, action: str):
    if not admin_ok(interaction):
        await safe_respond(interaction, embed=make_embed("❌ Permission Denied", "Administrator access is required."))
        return
    if not await safe_defer(interaction, ephemeral=True):
        return
    vps = db_find_vps_by_ptero_identifier(identifier)
    if not vps or str(vps["backend"] or "docker").lower() != "pterodactyl":
        await safe_followup(interaction, embed=make_embed("❌ Pterodactyl VPS Not Found", "Use the Pterodactyl server ID/identifier stored by RGNODES™."))
        return
    if action == "kill":
        ok, message = await ptero_power(vps, "kill")
    else:
        ok, message = await lifecycle_action(vps, action)
    if ok and action == "kill":
        db_update_vps(vps["container_id"], status="stopped", sshx_url=await ptero_panel_link(vps))
    await safe_followup(interaction, embed=make_embed("✅ Pterodactyl Action Complete" if ok else "❌ Pterodactyl Action Failed", message))


@bot.tree.command(name="admin-ptero-status", description="Admin: show Pterodactyl server status and resources.")
async def admin_ptero_status(interaction: discord.Interaction, identifier: str):
    if not admin_ok(interaction):
        await safe_respond(interaction, embed=make_embed("❌ Permission Denied", "Administrator access is required."))
        return
    if not await safe_defer(interaction, ephemeral=True):
        return
    vps = db_find_vps_by_ptero_identifier(identifier)
    if not vps or str(vps["backend"] or "docker").lower() != "pterodactyl":
        await safe_followup(interaction, embed=make_embed("❌ Pterodactyl VPS Not Found", "No matching Pterodactyl-backed RGNODES™ VPS was found."))
        return
    stats = await ptero_utilization(vps)
    await safe_followup(interaction, embed=make_embed(
        f"🦖 Pterodactyl • {clean(vps['container_name'])}",
        f"Server ID: `{clean(vps['ptero_server_id'])}`\nIdentifier: `{clean(vps['ptero_identifier'])}`\n"
        f"State: `{clean(stats.get('state'))}`\nCPU: `{clean(stats.get('cpu'))}`\nMemory: `{clean(stats.get('memory'))}`\nDisk: `{clean(stats.get('disk'))}`"
    ))


@bot.tree.command(name="admin-cloudflare-set", description="Admin: Cloudflare DNS compatibility command.")
async def admin_cloudflare_set(interaction: discord.Interaction, name: str, record_type: str, content: str):
    await optional_command_notice(interaction, "Cloudflare DNS")


@bot.tree.command(name="admin-cloudflare-delete", description="Admin: Cloudflare DNS compatibility command.")
async def admin_cloudflare_delete(interaction: discord.Interaction, name: str, record_type: str):
    await optional_command_notice(interaction, "Cloudflare DNS")


@bot.tree.command(name="admin-systemctl", description="Admin: systemd compatibility command.")
async def admin_systemctl(interaction: discord.Interaction, service: str, action: str):
    await optional_command_notice(interaction, "systemd")


# ================================================================
# System bootstrap commands
# ================================================================

def confirm_value(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"confirm", "true", "yes", "y", "1"}


@bot.tree.command(
    name="install-system",
    description="Admin: install/repair Linux and Docker dependencies.",
)
@app_commands.describe(confirm="Set true to actually run the system bootstrap.")
async def install_system_slash(interaction: discord.Interaction, confirm: bool = False):
    if not admin_ok(interaction):
        await safe_respond(interaction, embed=make_embed(
            "❌ Permission Denied",
            "Administrator access is required.",
        ))
        return

    if not confirm:
        info = host_os_info()
        await safe_respond(interaction, embed=make_embed(
            "🛠️ RGNODES™ • Install System",
            (
                "This command can install/repair the small Linux dependency set "
                "and Docker when Docker is missing.\n\n"
                f"**Detected OS:** `{clean(info['name'])}`\n"
                f"**PID 1:** `{clean(info['pid1'])}`\n"
                f"**Root:** `{clean(info['root'])}`\n\n"
                "**Nothing has been changed.**\n"
                "Run `/install-system confirm:true` to proceed."
            ),
        ))
        return

    if not await safe_defer(interaction, ephemeral=True):
        return
    try:
        ok, result = await asyncio.wait_for(install_system_dependencies(), timeout=900)
        title = "✅ Install System Complete" if ok else "⚠️ Install System Finished With Issues"
        await safe_followup(interaction, embed=make_embed(title, f"```text\n{safe_log(result, 3800)}\n```"))
    except asyncio.TimeoutError:
        await safe_followup(interaction, embed=make_embed(
            "❌ Install System Timeout",
            "The host bootstrap exceeded the 15-minute safety timeout. Check the host package manager and Docker daemon manually.",
        ))
    except Exception as exc:
        logger.exception("install-system failed")
        await safe_followup(interaction, embed=make_embed(
            "❌ Install System Failed",
            f"```text\n{safe_log(exc, 3800)}\n```",
        ))


# ================================================================
# Prefix compatibility layer
# ================================================================

@bot.command(name="share-user")
async def prefix_share_user(ctx: commands.Context, target_user: discord.User, vps_identifier: str):
    vps = db_find_vps(ctx.author.id, vps_identifier)
    if not vps or not db_is_owner_or_admin(ctx.author.id, vps):
        await safe_ctx_send(ctx, make_embed("❌ Permission Denied", "Only the VPS owner or administrator can share this VPS."))
        return
    ok, message = db_share_vps(vps["id"], target_user.id, ctx.author.id)
    await safe_ctx_send(ctx, make_embed("✅ VPS Shared" if ok else "⚠️ Share Failed", f"{message}\nVPS: `{clean(vps['container_name'])}` • User: <@{target_user.id}>"))


@bot.command(name="share-ruser", aliases=["unshare-user"])
async def prefix_share_ruser(ctx: commands.Context, target_user: discord.User, vps_identifier: str):
    vps = db_find_vps(ctx.author.id, vps_identifier)
    if not vps or not db_is_owner_or_admin(ctx.author.id, vps):
        await safe_ctx_send(ctx, make_embed("❌ Permission Denied", "Only the VPS owner or administrator can revoke access."))
        return
    ok, message = db_unshare_vps(vps["id"], target_user.id)
    await safe_ctx_send(ctx, make_embed("✅ Access Removed" if ok else "⚠️ Nothing Changed", f"{message}\nVPS: `{clean(vps['container_name'])}` • User: <@{target_user.id}>"))


@bot.command(name="myvps")
async def prefix_myvps(ctx: commands.Context) -> None:
    vps = db_get_user_vps(ctx.author.id)
    if not vps:
        await safe_ctx_send(ctx, make_embed("❌ No VPS Found", f"You do not have any VPS instances yet. Use `{PREFIX}deploy` first."))
        return
    vps = await refresh_vps_record_state(vps[0])
    await asyncio.gather(
        supervise_vps_ports(vps) if str(vps["backend"] or "docker").lower() == "docker" else asyncio.sleep(0),
        detect_public_network(),
        return_exceptions=True,
    )
    stats, uptime, disk = await asyncio.gather(backend_stats(vps), backend_uptime(vps), backend_disk(vps))
    await ctx.send(
        embed=dashboard_embed(vps, stats, uptime, disk, NETWORK_CACHE, db_list_ports(vps["id"])),
        view=ManageView(vps["id"], vps["user_id"]),
    )


@bot.command(name="uptime")
async def prefix_uptime(ctx: commands.Context):
    await safe_ctx_send(ctx, make_embed("⏱️ RGNODES™ • Host Uptime", f"Host uptime: `{await host_uptime()}`"))


@bot.command(name="vpsinfo", aliases=["vps-info"])
async def prefix_vpsinfo(ctx: commands.Context, identifier: str = ""):
    vps = db_find_accessible_vps(ctx.author.id, identifier)
    if not vps:
        await safe_ctx_send(ctx, make_embed("❌ VPS Not Found", "No VPS matches that identifier."))
        return
    await safe_ctx_send(ctx, make_embed(
        f"📊 VPS Info • {clean(vps['container_name'])}",
        f"ID: `{vps['id']}`\nStatus: **{status_text(vps['status'], bool(vps['suspended']))}**\n"
        f"OS: `{os_label(vps['os_type'])}`\nLocation: `{location_label(vps['location'])}`\n"
        f"RAM: `{vps['ram']}` • CPU: `{vps['cpu']}` • Disk: `{vps['disk']}`\n"
        f"Backend: `{str(vps['backend'] or 'docker').lower()}`"
    ))


@bot.command(name="vps-stats")
async def prefix_vps_stats(ctx: commands.Context, identifier: str = ""):
    vps = db_find_accessible_vps(ctx.author.id, identifier)
    if not vps:
        await safe_ctx_send(ctx, make_embed("❌ VPS Not Found", "No VPS matches that identifier."))
        return
    vps = await refresh_vps_record_state(vps)
    stats, uptime, disk = await asyncio.gather(backend_stats(vps), backend_uptime(vps), backend_disk(vps))
    await safe_ctx_send(ctx, make_embed(
        f"📈 VPS Stats • {clean(vps['container_name'])}",
        f"CPU: `{clean(stats.get('cpu'))}`\nMemory: `{clean(stats.get('memory'))}`\n"
        f"Disk: `{clean(disk.get('used'))} / {clean(vps['disk'])}`\nNetwork: `{clean(stats.get('network'))}`\nUptime: `{clean(uptime)}`"
    ))


@bot.command(name="vps-uptime")
async def prefix_vps_uptime(ctx: commands.Context, identifier: str = ""):
    vps = db_find_accessible_vps(ctx.author.id, identifier)
    if not vps:
        await safe_ctx_send(ctx, make_embed("❌ VPS Not Found", "No VPS matches that identifier."))
        return
    await safe_ctx_send(ctx, make_embed("⏱️ VPS Uptime", f"`{clean(await backend_uptime(vps))}`"))


@bot.command(name="restart-vps")
async def prefix_restart_vps(ctx: commands.Context, identifier: str = ""):
    await prefix_action(ctx, identifier, "restart")


@bot.command(name="snapshot")
async def prefix_snapshot(ctx: commands.Context, identifier: str, name: str = ""):
    vps = db_find_accessible_vps(ctx.author.id, identifier)
    if not vps:
        await safe_ctx_send(ctx, make_embed("❌ VPS Not Found", "No VPS matches that identifier."))
        return
    name = name.strip() or f"snapshot-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    ok, message = await docker_snapshot_create(vps, name)
    await safe_ctx_send(ctx, make_embed("📸 Snapshot Created" if ok else "❌ Snapshot Failed", message))


@bot.command(name="list-snapshots")
async def prefix_list_snapshots(ctx: commands.Context, identifier: str = ""):
    vps = db_find_accessible_vps(ctx.author.id, identifier)
    if not vps:
        await safe_ctx_send(ctx, make_embed("❌ VPS Not Found", "No VPS matches that identifier."))
        return
    rows = db_list_snapshots(vps["id"])
    body = "\n".join(f"`{r['name']}` • {str(r['created_at'])[:19]}" for r in rows[:20]) or "No snapshots."
    await safe_ctx_send(ctx, make_embed(f"📋 Snapshots • {clean(vps['container_name'])}", body))


@bot.command(name="restore-snapshot")
async def prefix_restore_snapshot(ctx: commands.Context, identifier: str, name: str):
    vps = db_find_accessible_vps(ctx.author.id, identifier)
    if not vps:
        await safe_ctx_send(ctx, make_embed("❌ VPS Not Found", "No VPS matches that identifier."))
        return
    if not db_is_owner_or_admin(ctx.author.id, vps):
        await safe_ctx_send(ctx, make_embed("❌ Permission Denied", "Only the VPS owner or administrator can restore snapshots."))
        return
    ok, message = await docker_snapshot_restore(vps, name)
    await safe_ctx_send(ctx, make_embed("✅ Snapshot Restored" if ok else "❌ Restore Failed", message))


@bot.command(name="manage-shared")
async def prefix_manage_shared(ctx: commands.Context, target_user: discord.User, vps_identifier: str):
    vps = db_find_vps(target_user.id, vps_identifier)
    if not vps or not db_is_owner_or_admin(ctx.author.id, vps):
        await safe_ctx_send(ctx, make_embed("❌ Permission Denied", "Only the VPS owner or administrator can manage shared access."))
        return
    shared = db_list_shared(vps["id"])
    body = "\n".join(f"<@{r['user_id']}> • granted by <@{r['shared_by']}>" for r in shared) or "No users currently have shared access."
    await safe_ctx_send(ctx, make_embed(f"👥 Shared Access • {clean(vps['container_name'])}", body), ManageView(vps["id"], vps["user_id"]))


@bot.command(name="serverstats")
async def prefix_serverstats(ctx: commands.Context):
    info = host_os_info()
    try:
        load = os.getloadavg()[0]
        load_text = f"{load:.2f}"
    except (AttributeError, OSError):
        load_text = "N/A"
    try:
        meminfo = Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace")
        total_kb = int(re.search(r"^MemTotal:\\s+(\\d+)", meminfo, re.M).group(1))
        avail_kb = int(re.search(r"^MemAvailable:\\s+(\\d+)", meminfo, re.M).group(1))
        ram_text = f"{format_bytes((total_kb-avail_kb)*1024)} / {format_bytes(total_kb*1024)}"
    except Exception:
        ram_text = "N/A"
    disk = shutil.disk_usage("/")
    ok, active = await docker_running_count()
    if not ok:
        active = db_running_count()
    await safe_ctx_send(ctx, make_embed("📊 RGNODES™ • Server Statistics",
        f"OS: `{clean(info['name'], 100)}`\nPID 1: `{clean(info['pid1'])}`\n"
        f"Host uptime: `{await host_uptime()}`\nRAM: `{ram_text}`\n"
        f"Disk: `{format_bytes(disk.used)} / {format_bytes(disk.total)}`\n"
        f"CPU cores: `{os.cpu_count() or 1}` • Load: `{load_text}`\nActive VPS: `{active}`"))


@bot.command(name="thresholds")
async def prefix_thresholds(ctx: commands.Context):
    await safe_ctx_send(ctx, make_embed("📋 RGNODES™ • Thresholds",
        f"Per-user slots: `{db_effective_slots(ctx.author.id)}`\n"
        f"Global running VPS: `{TOTAL_RUNNING_LIMIT}`\n"
        f"Max ports/VPS: `{MAX_PORTS_PER_VPS}`\n"
        f"Port range: `{PORT_RANGE_START}-{PORT_RANGE_END}`\n"
        f"RAM per VPS: `256MB-256GB`\nDisk per VPS: `1GB-10TB`\nCPU per VPS: `0-64 cores`"))


@bot.command(name="set-status")
async def prefix_set_status(ctx: commands.Context, status_type: str, *, name: str):
    if not admin_ok(ctx):
        await safe_ctx_send(ctx, make_embed("❌ Permission Denied", "Administrator access is required."))
        return
    kind = status_type.strip().lower()
    activity: discord.BaseActivity
    if kind == "watching":
        activity = discord.Activity(type=discord.ActivityType.watching, name=name)
    elif kind == "listening":
        activity = discord.Activity(type=discord.ActivityType.listening, name=name)
    elif kind == "competing":
        activity = discord.Activity(type=discord.ActivityType.competing, name=name)
    else:
        kind = "playing"
        activity = discord.Game(name=name)
    await bot.change_presence(activity=activity)
    await safe_ctx_send(ctx, make_embed("✅ Status Updated", f"Type: `{kind}`\nName: `{clean(name, 200)}`"))


@bot.group(name="ports", invoke_without_command=True)
async def prefix_ports(ctx: commands.Context, identifier: str = ""):
    if ctx.invoked_subcommand:
        return
    vps = db_find_accessible_vps(ctx.author.id, identifier)
    if not vps:
        await safe_ctx_send(ctx, make_embed("❌ VPS Not Found", f"Use `{PREFIX}ports list <vps#>` or open `{PREFIX}manage`."))
        return
    if str(vps["backend"] or "docker").lower() != "docker":
        panel = await ptero_panel_link(vps)
        await safe_ctx_send(ctx, make_embed("🦖 Pterodactyl Ports", f"Port allocations are managed by Pterodactyl.\nPanel: {panel or 'not configured'}"))
        return
    rows = db_list_ports(vps["id"])
    body = "\n".join(f"#{r['id']} • `{r['host_port']}→{r['container_port']}/TCP` • `{r['status']}`" for r in rows) or "No forwarding rules."
    await safe_ctx_send(ctx, make_embed(f"🌐 Ports • {clean(vps['container_name'])}", body))


@prefix_ports.command(name="add")
async def prefix_ports_add(ctx: commands.Context, identifier: str, container_port: int, host_port: int = 0):
    vps = db_find_accessible_vps(ctx.author.id, identifier)
    if not vps:
        await safe_ctx_send(ctx, make_embed("❌ VPS Not Found", "No VPS matches that identifier."))
        return
    if str(vps["backend"] or "docker").lower() != "docker":
        await safe_ctx_send(ctx, make_embed("🦖 Pterodactyl Allocations", "This VPS uses Pterodactyl. Manage ports/allocations from the panel."))
        return
    if not 1 <= container_port <= 65535:
        await safe_ctx_send(ctx, make_embed("❌ Invalid Port", "Container port must be between 1 and 65535."))
        return
    async with PORT_LOCK:
        if len(db_list_ports(vps["id"])) >= MAX_PORTS_PER_VPS:
            await safe_ctx_send(ctx, make_embed("⚠️ Port Limit Reached", f"Maximum `{MAX_PORTS_PER_VPS}` forwarding rules per VPS."))
            return
        if db_find_port(vps["id"], container_port, "tcp"):
            await safe_ctx_send(ctx, make_embed("⚠️ Already Exists", "That container port is already forwarded."))
            return
        if host_port == 0:
            host_port = await allocate_host_port() or 0
        if not 1024 <= host_port <= 65535 or not port_bindable(host_port):
            await safe_ctx_send(ctx, make_embed("❌ Host Port Unavailable", "Use a free port 1024–65535 or 0 for auto-allocation."))
            return
        try:
            port_id = db_insert_port(vps["id"], container_port, host_port, "tcp")
        except sqlite3.IntegrityError:
            await safe_ctx_send(ctx, make_embed("❌ Port Conflict", "That public port is already reserved."))
            return
    row = db_get_port(port_id)
    ok, message = await start_port_forward(row, vps) if row else (False, "Forwarding record disappeared.")
    if not ok:
        if row:
            await stop_port_forward(row)
        db_delete_port(port_id)
    await safe_ctx_send(ctx, make_embed("✅ Port Added" if ok else "❌ Port Failed", message))


@prefix_ports.command(name="list")
async def prefix_ports_list(ctx: commands.Context, identifier: str = ""):
    vps = db_find_accessible_vps(ctx.author.id, identifier)
    if not vps:
        await safe_ctx_send(ctx, make_embed("❌ VPS Not Found", "No VPS matches that identifier."))
        return
    if str(vps["backend"] or "docker").lower() != "docker":
        panel = await ptero_panel_link(vps)
        await safe_ctx_send(ctx, make_embed("🦖 Pterodactyl Ports", f"Port allocations are managed by Pterodactyl.\nPanel: {panel or 'not configured'}"))
        return
    rows = db_list_ports(vps["id"])
    body = "\n".join(f"#{r['id']} • `{r['host_port']}→{r['container_port']}/TCP` • `{r['status']}`" for r in rows) or "No forwarding rules."
    await safe_ctx_send(ctx, make_embed(f"🌐 Ports • {clean(vps['container_name'])}", body))


@prefix_ports.command(name="remove")
async def prefix_ports_remove(ctx: commands.Context, port_id: int):
    row = db_get_port(port_id)
    if not row:
        await safe_ctx_send(ctx, make_embed("❌ Port Not Found", "No such forwarding rule exists."))
        return
    vps = db_get_vps(int(row["vps_id"]))
    if not vps or not db_is_owner_or_admin(ctx.author.id, vps):
        await safe_ctx_send(ctx, make_embed("❌ Permission Denied", "You do not own that forwarding rule."))
        return
    await stop_port_forward(row)
    db_delete_port(port_id)
    await safe_ctx_send(ctx, make_embed("🗑️ Port Removed", f"Forwarding rule `#{port_id}` removed."))


@bot.command(name="deploy")
async def prefix_deploy(
    ctx: commands.Context,
    os_type: str | None = None,
    ram: str = DEFAULT_RAM,
    cpu: str = DEFAULT_CPU,
    disk: str = DEFAULT_DISK,
    location: str = DEFAULT_LOCATION,
):
    if not os_type:
        await ctx.send(
            embed=make_embed(
                "🚀 Deploy RGNODES™ VPS",
                "Select the operating system first, then choose a location: Singapore 🇸🇬 or India 🇮🇳.",
            ),
            view=DeployView(ctx.author.id),
        )
        return

    normalized_location = normalize_location(location) or DEFAULT_LOCATION
    message = await ctx.send(
        embed=progress_embed(
            1,
            "Starting deployment",
            normalize_os(os_type) or os_type,
            normalized_location,
            ram,
            cpu,
            disk,
            "rgnodes-pending",
        )
    )

    async def edit(embed: discord.Embed):
        with contextlib.suppress(discord.HTTPException):
            await message.edit(embed=embed)

    ok, reason, vps = await create_vps(
        ctx.author,
        os_type=os_type,
        location=normalized_location,
        ram=ram,
        cpu=cpu,
        disk=disk,
        progress=edit,
    )
    if not ok or not vps: await message.edit(embed=make_embed("❌ VPS Creation Failed", reason)); return
    dm_sent = await safe_dm(ctx.author, console_embed(vps["container_name"], vps["sshx_url"]), sshx_view(vps["sshx_url"]))
    final = make_embed("✅ VPS Ready", f"`{clean(vps['container_name'])}` is online."); final.add_field(name="🌐 Console", value="✅ Link sent by DM" if dm_sent else "⚠️ DM unavailable", inline=False)
    await message.edit(embed=final, view=ManageView(vps["id"], vps["user_id"]))


@bot.command(name="myvm")
async def prefix_myvm(ctx: commands.Context) -> None:
    """Prefix compatibility command for the newest VPS dashboard."""
    vps = db_get_user_vps(ctx.author.id)
    if not vps:
        await safe_ctx_send(ctx, make_embed("❌ No VPS Found", "You do not have any VPS instances yet. Use `/deploy` first."))
        return
    current = await refresh_vps_record_state(vps[0])
    backend = str(current["backend"] or "docker").lower()
    if backend == "docker":
        await asyncio.gather(supervise_vps_ports(current), detect_public_network(), return_exceptions=True)
        ports = db_list_ports(current["id"])
    else:
        ports = []
    stats, uptime, disk = await asyncio.gather(backend_stats(current), backend_uptime(current), backend_disk(current))
    await ctx.send(embed=dashboard_embed(current, stats, uptime, disk, NETWORK_CACHE, ports), view=ManageView(current["id"], current["user_id"]))


@bot.command(name="manage")
async def prefix_manage(ctx: commands.Context, identifier: str = ""):
    vps = db_find_vps(ctx.author.id, identifier, admin=True) if ctx.author.id == ADMIN_ID else db_find_accessible_vps(ctx.author.id, identifier)
    if not vps: await ctx.send(embed=make_embed("❌ VPS Not Found", "No matching VPS was found.")); return
    vps = await refresh_vps_record_state(vps)
    backend = str(vps["backend"] or "docker").lower()
    if backend == "docker":
        await asyncio.gather(supervise_vps_ports(vps), detect_public_network(), return_exceptions=True)
        ports = db_list_ports(vps["id"])
    else:
        ports = []
    stats, uptime, disk = await asyncio.gather(backend_stats(vps), backend_uptime(vps), backend_disk(vps))
    await ctx.send(embed=dashboard_embed(vps, stats, uptime, disk, NETWORK_CACHE, ports), view=ManageView(vps["id"], vps["user_id"]))


@bot.command(name="console")
async def prefix_console(ctx: commands.Context, identifier: str):
    vps = db_find_vps(ctx.author.id, identifier, admin=True) if ctx.author.id == ADMIN_ID else db_find_accessible_vps(ctx.author.id, identifier)
    if not vps: await ctx.send(embed=make_embed("❌ VPS Not Found", "No matching VPS was found.")); return
    ok, message = await create_console_access(vps, ctx.author); await ctx.send(embed=make_embed("✅ Console Ready" if ok else "❌ Console Failed", message))


@bot.command(name="list")
async def prefix_list(ctx: commands.Context):
    rows = db_get_user_vps(ctx.author.id); embed = make_embed("📋 Your RGNODES™ VPS")
    if not rows:
        embed.description = "You do not have any VPS instances."
    else:
        embed.add_field(name="🎟️ VPS Slots", value=slot_status_text(ctx.author.id), inline=False)
    for row in rows[:25]:
        embed.add_field(
            name=f"{status_text(row['status'], bool(row['suspended']))} {clean(row['container_name'])}",
            value=f"ID: `{row['id']}` • {os_label(row['os_type'])} • {location_label(row['location'])}",
            inline=False,
        )
    await ctx.send(embed=embed)


@bot.command(name="remove")
async def prefix_remove(ctx: commands.Context, identifier: str): await prefix_action(ctx, identifier, "delete")


@bot.command(name="start")
async def prefix_start(ctx: commands.Context, identifier: str): await prefix_action(ctx, identifier, "start")
@bot.command(name="stop")
async def prefix_stop(ctx: commands.Context, identifier: str): await prefix_action(ctx, identifier, "stop")
@bot.command(name="restart")
async def prefix_restart(ctx: commands.Context, identifier: str): await prefix_action(ctx, identifier, "restart")


async def prefix_action(ctx: commands.Context, identifier: str, action: str):
    vps = db_find_vps(ctx.author.id, identifier, admin=True) if ctx.author.id == ADMIN_ID else db_find_accessible_vps(ctx.author.id, identifier)
    if not vps: await ctx.send(embed=make_embed("❌ VPS Not Found", "No matching VPS was found.")); return
    ok, message = await lifecycle_action(vps, action); await ctx.send(embed=make_embed("✅ Action Complete" if ok else "❌ Action Failed", message))


class HelpSelect(discord.ui.Select):
    def __init__(self, owner_id: int, admin: bool):
        options = [
            discord.SelectOption(label="User Commands", value="user", emoji="👤", description="Commands for all VPS users"),
            discord.SelectOption(label="VPS Management", value="vps", emoji="🖥️", description="Start, stop, stats, snapshots and access"),
            discord.SelectOption(label="Port Forwarding", value="ports", emoji="🔌", description="Network and port management"),
            discord.SelectOption(label="System Status", value="system", emoji="⚙️", description="Host monitoring and limits"),
            discord.SelectOption(label="Bot Info", value="bot", emoji="🤖", description="Bot information and status"),
        ]
        if admin:
            options.append(discord.SelectOption(label="Admin Commands", value="admin", emoji="🛡️", description="Administrator commands"))
        super().__init__(
            placeholder="Select Category",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"rgnodes:help:{owner_id}",
        )
        self.owner_id = owner_id
        self.admin = admin

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await safe_respond(interaction, embed=make_embed("❌ Access Denied", "This help menu belongs to another user."))
            return
        await safe_component_edit(interaction, embed=build_help_embed(self.admin, self.values[0]), view=self.view)


class HelpView(discord.ui.View):
    def __init__(self, owner_id: int, admin: bool):
        super().__init__(timeout=600)
        self.add_item(HelpSelect(owner_id, admin))

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True


def build_help_embed(admin: bool, category: str = "user") -> discord.Embed:
    embed = make_embed("📚 RGNODES™ Help", "Use the dropdown below to switch categories.")
    category = category if category in {"user", "vps", "ports", "system", "bot", "admin"} else "user"

    if category == "user":
        embed.title = "📚 RGNODES™ • 👤 User Commands"
        embed.add_field(name="Basic commands for all users", value="Use the dropdown below to switch categories.", inline=False)
        embed.add_field(name="Available Commands", value=(
            f"**`{PREFIX}ping`** ╰ Check bot latency\n"
            f"**`{PREFIX}uptime`** ╰ Show host uptime\n"
            f"**`{PREFIX}myvps`** ╰ View your VPS dashboard\n"
            f"**`{PREFIX}manage [vps#]`** ╰ Manage your VPS instances\n"
            f"**`{PREFIX}share-user @user <vps#>`** ╰ Share VPS access\n"
            f"**`{PREFIX}share-ruser @user <vps#>`** ╰ Revoke shared access\n"
            f"**`{PREFIX}manage-shared @owner <vps#>`** ╰ Manage shared VPS"
        ), inline=False)
        return embed

    if category == "vps":
        embed.title = "📚 RGNODES™ • 🖥️ VPS Management"
        embed.add_field(name="Commands for VPS control", value="Use the dropdown below to switch categories.", inline=False)
        embed.add_field(name="Available Commands", value=(
            f"**`{PREFIX}myvps`** ╰ List your VPS\n"
            f"**`{PREFIX}vpsinfo <vps#>`** ╰ VPS information\n"
            f"**`{PREFIX}vps-stats <vps#>`** ╰ Live statistics\n"
            f"**`{PREFIX}vps-uptime <vps#>`** ╰ VPS uptime\n"
            f"**`{PREFIX}restart-vps <vps#>`** ╰ Restart VPS\n"
            f"**`{PREFIX}snapshot <vps#> [name]`** ╰ Create snapshot\n"
            f"**`{PREFIX}list-snapshots <vps#>`** ╰ List snapshots\n"
            f"**`{PREFIX}restore-snapshot <vps#> <name>`** ╰ Restore snapshot"
        ), inline=False)
        embed.add_field(name="Also available", value=f"`{PREFIX}start` • `{PREFIX}stop` • `{PREFIX}restart` • `{PREFIX}console` • `{PREFIX}logs` • `{PREFIX}remove` • `/reinstall`", inline=False)
        return embed

    if category == "ports":
        embed.title = "📚 RGNODES™ • 🔌 Port Forwarding"
        embed.add_field(name="Network and port management", value="Use the dropdown below to switch categories.", inline=False)
        embed.add_field(name="Available Commands", value=(
            f"**`{PREFIX}ports add <vps#> <port>`** ╰ Add port forward\n"
            f"**`{PREFIX}ports list <vps#>`** ╰ List your ports\n"
            f"**`{PREFIX}ports remove <id>`** ╰ Remove port forward\n"
            f"**`/ports <vps#>`** ╰ View forwarding rules\n"
            f"**`/port-add <vps#> <port>`** ╰ Add forwarding rule\n"
            f"**`/port-remove <vps#> <id>`** ╰ Remove forwarding rule"
        ), inline=False)
        return embed

    if category == "system":
        embed.title = "📚 RGNODES™ • ⚙️ System Status"
        embed.add_field(name="System monitoring commands", value="Use the dropdown below to switch categories.", inline=False)
        embed.add_field(name="Available Commands", value=(
            f"**`{PREFIX}serverstats`** ╰ Server statistics\n"
            f"**`{PREFIX}thresholds`** ╰ View thresholds\n"
            f"**`{PREFIX}set-status <type> <name>`** ╰ Set bot status (admin)"
        ), inline=False)
        return embed

    if category == "bot":
        embed.title = "📚 RGNODES™ • 🤖 Bot Info"
        embed.add_field(name="Bot information and status", value="Use the dropdown below to switch categories.", inline=False)
        embed.add_field(name="Available Commands", value=(
            f"**`{PREFIX}ping`** ╰ Check latency\n"
            f"**`{PREFIX}uptime`** ╰ Host uptime\n"
            f"**`{PREFIX}help`** ╰ This help menu\n"
            f"**`/about`** ╰ RGNODES™ information"
        ), inline=False)
        return embed

    embed.title = "📚 RGNODES™ • 🛡️ Admin Commands"
    embed.add_field(name="Administration", value=(
        "`/admin-create` • `/admin-manage` • `/admin-list` • `/admin-list-users` • `/admin-stats`\n"
        "`/admin-vps-info` • `/admin-logs` • `/admin-delete-user` • `/admin-ban` • `/admin-unban`\n"
        "`/add-slots` • `/remove-all confirm:true` • `/admin-kill-all` • `/install-system confirm:true`"
    ), inline=False)
    embed.add_field(name="Pterodactyl / Platform", value=(
        "`/admin-ptero-create` • `/admin-ptero-power` • `/admin-ptero-status`\n"
        "`/admin-cloudflare-set` • `/admin-cloudflare-delete` • `/admin-systemctl`"
    ), inline=False)
    return embed


@bot.command(name="help")
async def prefix_help(ctx: commands.Context):
    admin = ctx.author.id == ADMIN_ID
    await safe_ctx_send(ctx, build_help_embed(admin, "user"), HelpView(ctx.author.id, admin))


async def safe_ctx_send(ctx: commands.Context, embed: discord.Embed, view: discord.ui.View | None = None) -> None:
    try:
        kwargs: dict[str, Any] = {"embed": embed}
        if view is not None:
            kwargs["view"] = view
        await ctx.send(**kwargs)
    except discord.HTTPException:
        pass


# ================================================================
# Background sync / startup recovery
# ================================================================

STATUS_SEMAPHORE = asyncio.Semaphore(STATUS_CONCURRENCY)


async def sync_one(row: sqlite3.Row) -> None:
    async with STATUS_SEMAPHORE:
        try:
            backend = str(row["backend"] or "docker").lower()
            if backend == "pterodactyl":
                state = await ptero_status(row)
                if state in {"running", "starting", "restarting", "stopped"}:
                    db_update_vps(row["container_id"], status=state, sshx_url=await ptero_panel_link(row), sshx_pid=None)
                return

            state = await docker_state(row["container_id"])
            if state == "running":
                if row["status"] != "running":
                    db_update_vps(row["container_id"], status="running")
                await supervise_vps_ports(row)
            elif state in {"exited", "created", "dead", "paused", "restarting", "removing"}:
                if row["status"] != "stopped" or row["sshx_url"] or row["sshx_pid"]:
                    db_update_vps(row["container_id"], status="stopped", sshx_url=None, sshx_pid=None)
                for p_row in db_list_ports(row["id"]):
                    await stop_port_forward(p_row)
            elif state is None:
                logger.debug("Docker inspect unavailable for VPS #%s; retaining current database status.", row["id"])
        except Exception as exc:
            logger.warning("Status sync failed for #%s: %s", row["id"], safe_log(exc))


@tasks.loop(seconds=STATUS_INTERVAL)
async def sync_statuses() -> None:
    rows = db_get_all_vps()
    if rows:
        await asyncio.gather(*(sync_one(row) for row in rows), return_exceptions=True)


@sync_statuses.before_loop
async def before_sync_statuses(): await bot.wait_until_ready()


@tasks.loop(seconds=60)
async def update_presence():
    try:
        if not bot.is_ready():
            return
        docker_ok, docker_active = await docker_running_count()
        if not docker_ok:
            docker_active = db_running_count()
        ptero_active = sum(1 for row in db_get_all_vps() if str(row["backend"] or "docker").lower() == "pterodactyl" and str(row["status"]).lower() in {"running", "starting", "restarting"} and not row["suspended"])
        active = docker_active + ptero_active
        await bot.change_presence(activity=discord.Game(name=f"{BOT_STATUS_NAME} • {active} Active ⚡"))
    except (discord.HTTPException, discord.GatewayNotFound, discord.ClientException, asyncio.CancelledError):
        if isinstance(sys.exc_info()[1], asyncio.CancelledError):
            raise
    except Exception as exc:
        logger.debug("Presence update skipped: %s", safe_log(exc))


@update_presence.before_loop
async def before_update_presence(): await bot.wait_until_ready()


@tasks.loop(seconds=PORT_SUPERVISOR_INTERVAL)
async def supervise_all_ports_loop():
    try:
        await asyncio.gather(*(supervise_vps_ports(vps) for vps in db_get_all_vps()), return_exceptions=True)
    except Exception as exc:
        logger.warning("Port supervisor loop error: %s", safe_log(exc))


@supervise_all_ports_loop.before_loop
async def before_supervise_all_ports_loop(): await bot.wait_until_ready()


@tasks.loop(seconds=REAL_LOCATION_REFRESH)
async def refresh_network_identity():
    try:
        await detect_public_network(force=True)
    except Exception as exc:
        logger.debug("Network identity refresh skipped: %s", safe_log(exc))


@refresh_network_identity.before_loop
async def before_refresh_network_identity(): await bot.wait_until_ready()


@bot.event
async def on_ready():
    logger.info("RGNODES™ online as %s", bot.user)
    if not bot.loops_started:
        if not sync_statuses.is_running():
            sync_statuses.start()
        if not update_presence.is_running():
            update_presence.start()
        if not supervise_all_ports_loop.is_running():
            supervise_all_ports_loop.start()
        if not refresh_network_identity.is_running():
            refresh_network_identity.start()
        bot.loops_started = True
        await detect_public_network()

    if not bot.synced:
        for attempt in range(3):
            try:
                synced = await bot.tree.sync()
                bot.synced = True
                logger.info("Synced %d slash commands", len(synced))
                break
            except discord.HTTPException as exc:
                logger.warning("Slash command sync attempt %d failed: %s", attempt + 1, safe_log(exc))
                if attempt < 2:
                    await asyncio.sleep(3 * (attempt + 1))


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound): return
    if isinstance(error, commands.CommandOnCooldown):
        await safe_ctx_send(ctx, make_embed("⏳ Please Wait", f"Try again in `{error.retry_after:.1f}s`.")); return
    if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument, commands.UserNotFound)):
        await safe_ctx_send(ctx, make_embed("❌ Command Usage Error", f"Use `{PREFIX}help` to view command syntax.")); return
    logger.error("Prefix command error: %s", safe_log(error)); await safe_ctx_send(ctx, make_embed("❌ Command Error", "The command could not be completed safely."))


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    logger.error("Slash command error: %s", safe_log(error))
    await safe_respond(
        interaction,
        embed=make_embed("❌ Command Error", "The command could not be completed safely."),
    )


async def main() -> None:
    # Start the web listener before connecting to Discord. This lets Render,
    # Railway and similar hosts verify the service as soon as the process starts.
    await start_health_server()

    if not TOKEN:
        await stop_health_server()
        raise SystemExit(
            "Discord bot token is missing. Set TOKEN=YOUR_BOT_TOKEN in .env "
            "(or DISCORD_TOKEN/BOT_TOKEN) and restart the bot."
        )

    if ADMIN_ID <= 0:
        logger.warning("ADMIN_ID is not configured; admin commands will be unavailable.")

    logger.info(
        "RGNODES starting | token_source=%s | prefix=%r | backend=%s | quota=%s fallback=%s location=%s | locations=SG,IN",
        TOKEN_SOURCE, PREFIX, active_backend(), ENABLE_HARD_DISK_QUOTA, QUOTA_FALLBACK, DEFAULT_LOCATION,
    )

    try:
        await bot.start(TOKEN, reconnect=True)

    except discord.LoginFailure:
        logger.error("Discord rejected the bot token (HTTP 401 / invalid credentials).")
        logger.error(
            "Check that %s contains the current token for the correct Discord bot, "
            "and that it is not an old/revoked token.", TOKEN_SOURCE
        )
        logger.error("The value should be the raw bot token, without a leading 'Bot '.")
        logger.error("If the token was regenerated in the Discord Developer Portal, update .env/container secrets and restart.")
        raise SystemExit(1) from None

    except discord.HTTPException as exc:
        logger.error(
            "Discord HTTP error during startup: status=%s code=%s message=%s",
            getattr(exc, "status", "unknown"),
            getattr(exc, "code", "unknown"),
            safe_log(str(exc)),
        )
        raise SystemExit(1) from None

    except discord.GatewayNotFound as exc:
        logger.error("Discord Gateway could not be reached: %s", safe_log(exc))
        raise SystemExit(1) from None

    except discord.ClientException as exc:
        logger.error("Discord client startup failed: %s", safe_log(exc))
        raise SystemExit(1) from None

    finally:
        for loop in (sync_statuses, update_presence, supervise_all_ports_loop, refresh_network_identity):
            if loop.is_running():
                loop.cancel()
        with contextlib.suppress(Exception):
            await bot.close()
        with contextlib.suppress(Exception):
            await stop_health_server()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("RGNODES stopped by user.")
