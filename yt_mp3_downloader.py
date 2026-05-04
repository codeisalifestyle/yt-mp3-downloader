#!/usr/bin/env python3
"""
yt-mp3-downloader

Download audio from a YouTube video or playlist as 320kbps MP3 (LAME).

Examples:
    # Single video, default ./downloads folder
    python yt_mp3_downloader.py "https://www.youtube.com/watch?v=VIDEO_ID"

    # Playlist (auto-creates a subfolder named after the playlist)
    python yt_mp3_downloader.py "https://www.youtube.com/playlist?list=LIST_ID"

    # Custom output folder
    python yt_mp3_downloader.py URL -o ~/Music/yt

    # Through an HTTP / HTTPS proxy (SOCKS is not supported — see README)
    python yt_mp3_downloader.py URL --proxy http://user:pass@host:port

    # Auto-rotate the proxy IP if YouTube blocks the request
    python yt_mp3_downloader.py URL \\
        --rotate-url 'https://relay.example.com/api/proxies/<id>/rotate' \\
        --rotate-auth 'Bearer <token>'
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from itertools import cycle
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlparse

try:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError
except ImportError:
    sys.stderr.write(
        "Error: yt-dlp is not installed.\n"
        "Install it with:  pip install -r requirements.txt\n"
    )
    sys.exit(1)

try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )
    from rich.table import Table
except ImportError:
    sys.stderr.write(
        "Error: rich is not installed.\n"
        "Install it with:  pip install -r requirements.txt\n"
    )
    sys.exit(1)


DEFAULT_OUTPUT_DIR = Path("downloads")
MP3_BITRATE_KBPS = "320"

# Environment variable names (also read from a local .env file if present).
ENV_PROXY = "YTMP3_PROXY"
ENV_OUTPUT = "YTMP3_OUTPUT"
ENV_ROTATE_URL = "YTMP3_ROTATE_URL"
ENV_ROTATE_AUTH = "YTMP3_ROTATE_AUTH"
ENV_ROTATE_STATUS_URL = "YTMP3_ROTATE_STATUS_URL"

# When routing through a proxy, yt-dlp's default of 4 concurrent fragments per
# video, combined with a playlist's many sequential videos, easily generates
# 50+ parallel TLS handshakes to googlevideo. Many proxies (especially
# residential / mobile exits) treat that as abusive traffic and either
# throttle, drop the connection, or trip a block. We default to a far gentler
# profile whenever a proxy is in use and let the user opt out with
# --aggressive.
PROXY_SAFE_FRAGMENTS = 1
NO_PROXY_FRAGMENTS = 4
# 20 s lets a slow first byte through a proxied tunnel, while still failing
# fast (2 × 20 = 40 s worst case for the watch page) instead of the
# minutes-long stalls we saw with longer timeouts.
PROXY_SOCKET_TIMEOUT_S = 20
PROXY_INTER_VIDEO_SLEEP_S = (2, 6)  # random sleep between playlist items
PROXY_RETRIES = 2
PROXY_FRAGMENT_RETRIES = 3

# yt-dlp's default User-Agent often triggers a 302 redirect from YouTube to
# the mobile / consent flow when proxied. Rotating between mainstream desktop
# fingerprints both serves the regular watch-page HTML and gives us a
# distinct identity to use after a proxy IP rotation.
UA_FINGERPRINTS: list[dict[str, str]] = [
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) "
            "Gecko/20100101 Firefox/122.0"
        ),
        "Accept-Language": "en-US,en;q=0.5",
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.2 Safari/605.1.15"
        ),
        "Accept-Language": "en-us",
    },
]

# Substring / regex patterns that we treat as a YouTube-side block. When one
# is detected and a rotation URL is configured, we trigger an IP rotation,
# rotate the User-Agent fingerprint, and retry the failed item.
BLOCK_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"http error 403",
        r"http error 429",
        r"sign in to confirm.*not a bot",
        r"sign in to confirm your age",
        r"this video is not available",
        r"video unavailable",
        r"the uploader has not made this video available",
        r"failed to extract any player response",
        r"unable to extract.*(player|youtube)",
        r"requested format is not available",
        r"too many requests",
        r"forbidden",
    ]
]

# Falconproxy staging/production health endpoints, keyed by the HTTP-proxy
# port. We auto-detect by host and use these to give the user a clear error
# before yt-dlp ever tries.
FALCONPROXY_HEALTH_PORT_BY_PROXY_PORT = {
    "8081": "9001",  # staging
    "8080": "9000",  # production
}

console = Console()


# ---------------------------------------------------------------------------
# .env loader & URL helpers
# ---------------------------------------------------------------------------


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ.

    Existing environment variables take precedence (so explicit `export`s win).
    Supports `#` comments, blank lines, and optional surrounding quotes.
    """
    if not path.is_file():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]
            os.environ.setdefault(key, value)
    except OSError:
        pass


def is_playlist_url(url: str) -> bool:
    """Return True if the URL points to a playlist we should expand into a folder.

    We treat any URL that exposes a `list=` query parameter as a playlist,
    EXCEPT YouTube's auto-generated "Mix" / "Radio" lists (IDs starting with
    `RD`), which are effectively endless and should not be expanded by default.
    """
    try:
        qs = parse_qs(urlparse(url).query)
    except Exception:
        return False

    list_ids = qs.get("list") or []
    if not list_ids:
        return False

    list_id = list_ids[0]
    if list_id.startswith("RD"):
        return False
    return True


def ensure_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        sys.stderr.write(
            "Error: ffmpeg is not installed or not on PATH.\n"
            "Install it with:  brew install ffmpeg   (macOS)\n"
            "                  sudo apt install ffmpeg   (Debian/Ubuntu)\n"
        )
        sys.exit(1)


def reject_unsupported_proxy(proxy: str) -> None:
    """Hard-stop early if the user passes a SOCKS proxy.

    yt-dlp's underlying PySocks integration is unreliable on consecutive
    requests through many proxies. HTTP CONNECT through urllib3's stable
    proxy handler is the supported path. See README "Notes" for the full
    story.
    """
    scheme = urlparse(proxy).scheme.lower()
    if scheme.startswith("socks"):
        console.print(
            f"[red]Error:[/] SOCKS proxies (`{scheme}://`) are not supported."
        )
        console.print(
            "       yt-dlp's SOCKS transport is unreliable; use an HTTP / HTTPS proxy."
        )
        console.print("       See the README for details.")
        sys.exit(1)


def preflight_falconproxy(proxy: str) -> None:
    """Best-effort health check of the falconproxy relay before downloading."""
    parsed = urlparse(proxy)
    if not parsed.hostname or "falconproxy" not in parsed.hostname:
        return
    port = str(parsed.port or "")
    health_port = FALCONPROXY_HEALTH_PORT_BY_PROXY_PORT.get(port)
    if not health_port:
        return
    health_url = f"http://{parsed.hostname}:{health_port}/health"
    try:
        with urllib.request.urlopen(health_url, timeout=5) as resp:
            if resp.status != 200:
                console.print(
                    f"[yellow]preflight:[/] {health_url} returned HTTP {resp.status}"
                )
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
        console.print(f"[yellow]preflight:[/] falconproxy health check failed ({health_url}): {e}")
        console.print(
            "[yellow]preflight:[/] The relay may be down or unreachable from this network. "
            "yt-dlp will still try, but expect failures."
        )


# ---------------------------------------------------------------------------
# Block detection & IP / fingerprint rotation
# ---------------------------------------------------------------------------


class BlockDetected(Exception):
    """Raised when a YouTube-side block is detected and rotation is warranted."""


@dataclass
class RotateConfig:
    """Configuration for triggering a proxy IP rotation on YouTube blocks."""

    url: str | None = None
    auth: str | None = None
    status_url: str | None = None
    wait_s: int = 30
    max_attempts: int = 2
    rotate_fingerprint: bool = True

    @property
    def enabled(self) -> bool:
        return bool(self.url) or self.rotate_fingerprint


class CapturingLogger:
    """yt-dlp logger that silences output and captures warnings/errors for inspection."""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def error(self, msg: str) -> None:
        self.errors.append(msg)


def is_block_message(msg: str) -> bool:
    return any(p.search(msg) for p in BLOCK_PATTERNS)


def trigger_ip_rotation(cfg: RotateConfig) -> bool:
    """Trigger a proxy IP rotation and (optionally) wait for completion.

    Returns True if the rotation appears to have completed, False otherwise.
    """
    if not cfg.url:
        return False

    headers = {"Accept": "application/json"}
    if cfg.auth:
        headers["Authorization"] = cfg.auth

    console.print(f"  [yellow]⟳ rotating IP via[/] {_redact_url(cfg.url)}")
    try:
        req = urllib.request.Request(cfg.url, method="POST", headers=headers, data=b"")
        with urllib.request.urlopen(req, timeout=15) as resp:
            if resp.status >= 400:
                console.print(f"  [red]✗ rotation request failed: HTTP {resp.status}[/]")
                return False
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
        console.print(f"  [red]✗ rotation request failed: {e}[/]")
        return False

    if cfg.status_url:
        return _poll_rotation_status(cfg, headers)

    # No status endpoint: just wait a fixed period for the rotation to settle.
    _sleep_with_dots(cfg.wait_s, label="waiting for new IP")
    return True


def _poll_rotation_status(cfg: RotateConfig, headers: dict[str, str]) -> bool:
    deadline = time.time() + cfg.wait_s
    while time.time() < deadline:
        try:
            req = urllib.request.Request(cfg.status_url, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode())
            rotating = bool(body.get("rotating"))
            last_result = body.get("last_result") or body.get("status")
            if not rotating and last_result and str(last_result).lower() in {"success", "completed", "ok"}:
                ip = body.get("current_ip", "")
                console.print(f"  [green]✓ rotation complete[/]" + (f" — new IP {ip}" if ip else ""))
                return True
            if not rotating and last_result and str(last_result).lower() in {"failed", "error"}:
                console.print(f"  [red]✗ rotation failed: {body.get('last_result_reason') or last_result}[/]")
                return False
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError, ValueError):
            pass
        time.sleep(1)

    console.print(f"  [yellow]⚠ rotation status poll timed out after {cfg.wait_s}s[/]")
    return False


def _sleep_with_dots(seconds: int, label: str) -> None:
    if seconds <= 0:
        return
    for _ in range(seconds):
        time.sleep(1)
    console.print(f"  [dim]{label}: waited {seconds}s[/]")


def _redact_url(url: str) -> str:
    """Strip userinfo from a URL so we never log credentials."""
    try:
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc += f":{parsed.port}"
            return parsed._replace(netloc=netloc).geturl()
    except Exception:
        pass
    return url


# ---------------------------------------------------------------------------
# yt-dlp option builder & per-entry download
# ---------------------------------------------------------------------------


def build_ydl_options(
    output_dir: Path,
    proxy: str | None,
    treat_as_playlist: bool,
    aggressive: bool,
    fingerprint: dict[str, str] | None,
    logger: CapturingLogger,
    progress_hooks: list | None = None,
    postprocessor_hooks: list | None = None,
) -> dict:
    if treat_as_playlist:
        out_template = str(output_dir / "%(title)s.%(ext)s")
    else:
        out_template = str(output_dir / "%(title)s.%(ext)s")

    proxy_in_use = bool(proxy)
    use_safe_profile = proxy_in_use and not aggressive

    options: dict = {
        "format": "bestaudio/best",
        "outtmpl": out_template,
        "noplaylist": True,
        "ignoreerrors": False,
        "retries": PROXY_RETRIES if use_safe_profile else 10,
        "fragment_retries": PROXY_FRAGMENT_RETRIES if use_safe_profile else 10,
        "concurrent_fragment_downloads": (
            PROXY_SAFE_FRAGMENTS if use_safe_profile else NO_PROXY_FRAGMENTS
        ),
        "windowsfilenames": True,
        "restrictfilenames": False,
        "writethumbnail": False,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": logger,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": MP3_BITRATE_KBPS,
            },
            {
                "key": "FFmpegMetadata",
            },
        ],
        "postprocessor_args": {
            "FFmpegExtractAudio": ["-codec:a", "libmp3lame", "-b:a", f"{MP3_BITRATE_KBPS}k"],
        },
    }

    if use_safe_profile:
        options["socket_timeout"] = PROXY_SOCKET_TIMEOUT_S

    if fingerprint:
        options["http_headers"] = dict(fingerprint)

    if proxy:
        options["proxy"] = proxy

    if progress_hooks:
        options["progress_hooks"] = progress_hooks

    if postprocessor_hooks:
        options["postprocessor_hooks"] = postprocessor_hooks

    return options


def explain_download_error(err: DownloadError) -> str | None:
    msg = str(err).lower()
    if "exit relay" in msg and "inactive" in msg:
        return (
            "The proxy reports the device's exit relay is inactive. "
            "Toggle it on from the device or the dashboard and retry."
        )
    if "tunnel" in msg and "not connected" in msg:
        return (
            "The proxy device tunnel is not connected. "
            "Open the proxy app on the device to bring it back online."
        )
    return None


@dataclass
class Entry:
    id: str
    title: str
    url: str


@dataclass
class ItemResult:
    entry: Entry
    success: bool
    error: str | None = None
    attempts: int = 1


def extract_entries(
    url: str,
    proxy: str | None,
    playlist_items: str | None,
) -> tuple[str | None, list[Entry]]:
    """Resolve a URL into a list of entries plus an optional playlist title.

    Uses yt-dlp's `extract_flat='in_playlist'` so it's fast (one webpage fetch
    plus one API JSON for playlists; just one webpage for single videos).
    """
    logger = CapturingLogger()
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "ignoreerrors": True,
        "logger": logger,
        "noplaylist": not is_playlist_url(url),
    }
    if proxy:
        opts["proxy"] = proxy
    if playlist_items:
        opts["playlist_items"] = playlist_items

    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info is None:
        raise RuntimeError("yt-dlp returned no info for the URL")

    if info.get("_type") == "playlist" and info.get("entries"):
        title = info.get("title") or info.get("id")
        entries: list[Entry] = []
        for e in info["entries"]:
            if not e:
                continue
            video_id = e.get("id") or ""
            video_url = e.get("url") or e.get("webpage_url") or (
                f"https://www.youtube.com/watch?v={video_id}" if video_id else ""
            )
            if not video_url:
                continue
            entries.append(
                Entry(
                    id=video_id,
                    title=e.get("title") or video_id,
                    url=video_url,
                )
            )
        return title, entries

    return None, [
        Entry(
            id=info.get("id", ""),
            title=info.get("title") or info.get("id", ""),
            url=info.get("webpage_url") or url,
        )
    ]


def download_one(
    entry: Entry,
    output_dir: Path,
    proxy: str | None,
    treat_as_playlist: bool,
    aggressive: bool,
    fingerprint: dict[str, str],
    item_progress: Progress,
    item_task_id: int,
) -> None:
    """Download a single entry. Raises BlockDetected on YouTube-side blocks."""

    title_short = (entry.title[:60] + "…") if len(entry.title) > 60 else entry.title

    def progress_hook(d: dict) -> None:
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = int(d.get("downloaded_bytes") or 0)
            if total:
                item_progress.update(item_task_id, total=int(total), completed=downloaded)
            else:
                item_progress.update(item_task_id, completed=downloaded)
        elif status == "finished":
            task = next((t for t in item_progress.tasks if t.id == item_task_id), None)
            if task and task.total:
                item_progress.update(item_task_id, completed=task.total)

    def postprocessor_hook(d: dict) -> None:
        status = d.get("status")
        pp = d.get("postprocessor", "")
        if status == "started" and pp == "FFmpegExtractAudio":
            item_progress.update(
                item_task_id,
                description=f"[yellow]encode[/]  {title_short}",
            )

    logger = CapturingLogger()
    opts = build_ydl_options(
        output_dir=output_dir,
        proxy=proxy,
        treat_as_playlist=treat_as_playlist,
        aggressive=aggressive,
        fingerprint=fingerprint,
        logger=logger,
        progress_hooks=[progress_hook],
        postprocessor_hooks=[postprocessor_hook],
    )

    item_progress.update(item_task_id, description=f"[cyan]fetch[/]   {title_short}")

    try:
        with YoutubeDL(opts) as ydl:
            ydl.download([entry.url])
    except DownloadError as e:
        text = " ".join([str(e), *logger.errors, *logger.warnings])
        if is_block_message(text):
            raise BlockDetected(str(e)) from e
        raise
    else:
        if any(is_block_message(m) for m in logger.errors):
            raise BlockDetected("blocked: " + "; ".join(logger.errors[:3]))


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _format_header(
    treat_as_playlist: bool,
    playlist_title: str | None,
    n_items: int,
    output_dir: Path,
    proxy: str | None,
    aggressive: bool,
    rotate_cfg: RotateConfig,
    playlist_items: str | None,
) -> Panel:
    rows: list[str] = []
    rows.append(
        f"[bold]playlist[/]  {playlist_title}" if treat_as_playlist
        else "[bold]video[/]"
    )
    rows.append(f"[bold]items[/]     {n_items}" + (f"  ([dim]filter:[/] {playlist_items})" if playlist_items else ""))
    rows.append(f"[bold]output[/]    {output_dir}")
    if proxy:
        rows.append(
            f"[bold]proxy[/]     {_redact_url(proxy)}  "
            + (f"[dim](aggressive)[/]" if aggressive else "[dim](safe profile)[/]")
        )
    else:
        rows.append("[bold]proxy[/]     [dim]none — direct connection[/]")
    if rotate_cfg.url:
        rows.append(
            f"[bold]rotate[/]    {_redact_url(rotate_cfg.url)}  "
            f"[dim](max {rotate_cfg.max_attempts} attempts, wait {rotate_cfg.wait_s}s)[/]"
        )
    elif rotate_cfg.rotate_fingerprint:
        rows.append("[bold]rotate[/]    [dim]fingerprint only (no IP rotation URL configured)[/]")
    return Panel(
        "\n".join(rows),
        title="[bold cyan]yt-mp3-downloader[/]",
        border_style="cyan",
        padding=(1, 2),
    )


def _summary_table(results: list[ItemResult]) -> Table:
    table = Table(title=None, show_header=True, header_style="bold", border_style="dim")
    table.add_column("#", justify="right", style="dim", width=3)
    table.add_column("status", width=8)
    table.add_column("title", overflow="fold")
    table.add_column("attempts", justify="right", style="dim")
    table.add_column("error", overflow="fold", style="red")
    for i, r in enumerate(results, 1):
        if r.success:
            status = "[green]✓ ok[/]"
            err_cell = ""
        else:
            status = "[red]✗ fail[/]"
            err_cell = r.error or ""
        table.add_row(str(i), status, r.entry.title, str(r.attempts), err_cell)
    return table


def download(
    url: str,
    output_dir: Path,
    proxy: str | None,
    aggressive: bool,
    rotate_cfg: RotateConfig,
    playlist_items: str | None = None,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)

    if proxy:
        reject_unsupported_proxy(proxy)
        preflight_falconproxy(proxy)

    treat_as_playlist = is_playlist_url(url)

    try:
        playlist_title, entries = extract_entries(url, proxy, playlist_items)
    except (DownloadError, RuntimeError) as e:
        console.print(f"[red]Failed to resolve URL:[/] {e}")
        return 1

    if not entries:
        console.print("[red]No entries found at the given URL.[/]")
        return 1

    if treat_as_playlist and playlist_title:
        # Playlists download into their own subfolder.
        output_dir = output_dir / _sanitize_filename(playlist_title)
        output_dir.mkdir(parents=True, exist_ok=True)

    console.print(
        _format_header(
            treat_as_playlist=treat_as_playlist,
            playlist_title=playlist_title,
            n_items=len(entries),
            output_dir=output_dir,
            proxy=proxy,
            aggressive=aggressive,
            rotate_cfg=rotate_cfg,
            playlist_items=playlist_items,
        )
    )

    fp_iter: Iterable[dict[str, str]]
    if rotate_cfg.rotate_fingerprint:
        fp_iter = cycle(UA_FINGERPRINTS)
    else:
        # Pin a single fingerprint for the whole run.
        fp_iter = cycle([UA_FINGERPRINTS[0]])
    results: list[ItemResult] = []

    overall_progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=None),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    )
    item_progress = Progress(
        TextColumn("  {task.description}"),
        BarColumn(bar_width=None),
        TaskProgressColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    )
    group = Group(overall_progress, item_progress)

    with Live(group, console=console, refresh_per_second=10):
        overall_task = overall_progress.add_task(
            "downloading tracks", total=len(entries)
        )
        for entry in entries:
            short_title = (entry.title[:60] + "…") if len(entry.title) > 60 else entry.title
            item_task = item_progress.add_task(f"[cyan]queued[/] {short_title}", total=None)
            result = _download_with_retry(
                entry=entry,
                output_dir=output_dir,
                proxy=proxy,
                treat_as_playlist=treat_as_playlist,
                aggressive=aggressive,
                rotate_cfg=rotate_cfg,
                fp_iter=fp_iter,
                item_progress=item_progress,
                item_task_id=item_task,
            )
            results.append(result)
            item_progress.remove_task(item_task)
            overall_progress.update(overall_task, advance=1)

    console.print()
    console.print(_summary_table(results))

    n_ok = sum(1 for r in results if r.success)
    n_fail = len(results) - n_ok
    if n_fail:
        console.print(
            f"\n[bold]done[/] — [green]{n_ok} succeeded[/] / [red]{n_fail} failed[/]"
        )
        return 1
    console.print(f"\n[bold green]done[/] — all {n_ok} item(s) downloaded")
    return 0


def _download_with_retry(
    entry: Entry,
    output_dir: Path,
    proxy: str | None,
    treat_as_playlist: bool,
    aggressive: bool,
    rotate_cfg: RotateConfig,
    fp_iter: Iterable[dict[str, str]],
    item_progress: Progress,
    item_task_id: int,
) -> ItemResult:
    max_attempts = max(1, rotate_cfg.max_attempts) if proxy and rotate_cfg.url else 1
    attempts = 0
    last_error: str | None = None

    for attempt in range(1, max_attempts + 1):
        attempts = attempt
        fingerprint = next(fp_iter)
        try:
            download_one(
                entry=entry,
                output_dir=output_dir,
                proxy=proxy,
                treat_as_playlist=treat_as_playlist,
                aggressive=aggressive,
                fingerprint=fingerprint,
                item_progress=item_progress,
                item_task_id=item_task_id,
            )
            return ItemResult(entry=entry, success=True, attempts=attempts)
        except BlockDetected as e:
            last_error = f"blocked: {str(e)[:200]}"
            console.print(
                f"  [yellow]⚠ block detected on[/] [dim]{entry.title[:60]}[/] "
                f"[yellow](attempt {attempt}/{max_attempts})[/]"
            )
            if attempt < max_attempts and proxy and rotate_cfg.url:
                trigger_ip_rotation(rotate_cfg)
                # next loop iteration will rotate fingerprint via fp_iter
                continue
            return ItemResult(entry=entry, success=False, error=last_error, attempts=attempts)
        except DownloadError as e:
            hint = explain_download_error(e)
            last_error = str(e)[:240] + (f" — {hint}" if hint else "")
            console.print(f"  [red]✗ download error on[/] [dim]{entry.title[:60]}[/]: {last_error}")
            return ItemResult(entry=entry, success=False, error=last_error, attempts=attempts)
        except Exception as e:  # noqa: BLE001
            last_error = f"{type(e).__name__}: {e}"
            console.print(f"  [red]✗ unexpected error on[/] [dim]{entry.title[:60]}[/]: {last_error}")
            return ItemResult(entry=entry, success=False, error=last_error, attempts=attempts)

    return ItemResult(entry=entry, success=False, error=last_error, attempts=attempts)


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


_FILENAME_BAD_CHARS = re.compile(r'[<>:"/\\|?*]')


def _sanitize_filename(name: str) -> str:
    """Make a string safe to use as a folder name on the major OSes."""
    return _FILENAME_BAD_CHARS.sub("_", name).strip().rstrip(".")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    env_output = os.environ.get(ENV_OUTPUT) or str(DEFAULT_OUTPUT_DIR)
    env_proxy = os.environ.get(ENV_PROXY) or None
    env_rotate_url = os.environ.get(ENV_ROTATE_URL) or None
    env_rotate_auth = os.environ.get(ENV_ROTATE_AUTH) or None
    env_rotate_status_url = os.environ.get(ENV_ROTATE_STATUS_URL) or None

    parser = argparse.ArgumentParser(
        prog="yt-mp3-downloader",
        description="Download audio from a YouTube video or playlist as 320kbps MP3 (LAME).",
    )
    parser.add_argument("url", help="YouTube video or playlist URL.")
    parser.add_argument(
        "-o", "--output",
        default=env_output,
        help=f"Output folder (default: ${ENV_OUTPUT} env var, or ./{DEFAULT_OUTPUT_DIR}). "
             "Playlists create a subfolder named after the playlist title.",
    )
    parser.add_argument(
        "--proxy",
        default=env_proxy,
        help=f"HTTP / HTTPS proxy URL, e.g. http://user:pass@host:port. "
             f"SOCKS proxies are not supported (see README). "
             f"Defaults to ${ENV_PROXY} env var if set; pass an empty string to disable.",
    )
    parser.add_argument(
        "--aggressive",
        action="store_true",
        help="Disable the gentle proxy profile (revert to 4 parallel fragments, "
             "no inter-video sleep). Only safe on a direct connection or a "
             "high-throughput datacenter proxy.",
    )
    parser.add_argument(
        "--playlist-items",
        default=None,
        help="Restrict a playlist download to specific items, e.g. '1-3', "
             "'1,5,8', or '1-3,7'. Same syntax as yt-dlp's --playlist-items.",
    )

    rotate_group = parser.add_argument_group(
        "block handling",
        "When YouTube blocks the proxy IP, optionally trigger an IP rotation "
        "and retry. The User-Agent fingerprint is rotated on every retry.",
    )
    rotate_group.add_argument(
        "--rotate-url",
        default=env_rotate_url,
        help=f"URL to POST when a block is detected (e.g. a relay's IP-rotation "
             f"endpoint). Defaults to ${ENV_ROTATE_URL}.",
    )
    rotate_group.add_argument(
        "--rotate-auth",
        default=env_rotate_auth,
        help=f"Authorization header value for --rotate-url (e.g. 'Bearer <token>'). "
             f"Defaults to ${ENV_ROTATE_AUTH}.",
    )
    rotate_group.add_argument(
        "--rotate-status-url",
        default=env_rotate_status_url,
        help=f"Optional GET URL polled after a rotation to know when it's "
             f"complete. Defaults to ${ENV_ROTATE_STATUS_URL}.",
    )
    rotate_group.add_argument(
        "--rotate-wait",
        type=int,
        default=30,
        help="Seconds to wait for the rotation to settle (or to poll the "
             "status URL). Default: 30.",
    )
    rotate_group.add_argument(
        "--rotate-max-attempts",
        type=int,
        default=2,
        help="Max attempts (including the first) per item when blocks are "
             "detected. Default: 2.",
    )
    rotate_group.add_argument(
        "--no-rotate-fingerprint",
        action="store_true",
        help="Disable User-Agent fingerprint rotation between attempts.",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path(__file__).resolve().parent / ".env")
    args = parse_args(argv)
    ensure_ffmpeg()
    output_dir = Path(args.output).expanduser().resolve()
    proxy = args.proxy or None

    rotate_cfg = RotateConfig(
        url=args.rotate_url or None,
        auth=args.rotate_auth or None,
        status_url=args.rotate_status_url or None,
        wait_s=int(args.rotate_wait),
        max_attempts=int(args.rotate_max_attempts),
        rotate_fingerprint=not args.no_rotate_fingerprint,
    )

    return download(
        url=args.url,
        output_dir=output_dir,
        proxy=proxy,
        aggressive=args.aggressive,
        rotate_cfg=rotate_cfg,
        playlist_items=args.playlist_items,
    )


if __name__ == "__main__":
    raise SystemExit(main())
