# yt-mp3-downloader

A tiny CLI that downloads the audio of a public YouTube video, playlist, or
channel and encodes it as **320 kbps MP3 (LAME)**. Renders a clean live
progress UI and recovers from YouTube blocks by rotating proxy IPs and
User-Agent fingerprints.

> Use only with content you have the rights to download.

## Features

- Single video, playlist, **or** channel/account input — pass a URL or a bare handle
- **Order & limit** — pick the N latest, oldest, or most-popular items from
any playlist or channel
- Output folder is configurable (default: `./downloads`)
- Playlists and channels are written into a subfolder named after the
playlist or channel title
- 320 kbps MP3 via `libmp3lame`, with metadata tags
- Optional HTTP / HTTPS proxy (SOCKS is **not** supported — see [Notes](#notes))
- **Block-aware retry** — when YouTube returns a 403/429 or "sign in to confirm"
page, the downloader can call your proxy's IP-rotation endpoint and retry
with a new browser fingerprint
- **Resume on rerun** — a per-output download archive remembers what's already
on disk so a partial run can be re-issued without re-downloading anything
- Live progress UI (rich) with overall + per-track bars and a final summary table

## Requirements

- Python 3.9+
- `[ffmpeg](https://ffmpeg.org/)` on your `PATH` (this is what does the actual MP3 encoding via LAME)
- `[yt-dlp](https://github.com/yt-dlp/yt-dlp)` and `[rich](https://github.com/Textualize/rich)` (installed from `requirements.txt`)

### Install ffmpeg

```bash
# macOS
brew install ffmpeg

# Debian / Ubuntu
sudo apt install ffmpeg
```

### Install Python deps

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration via `.env` (optional)

You can store the default proxy, output folder, and rotation endpoint in a
local `.env` file so you don't have to pass them on the command line every
time. `.env` is gitignored.

```bash
cp .env.example .env
# then edit .env and fill in your own values:
#   YTMP3_PROXY=http://USER:PASS@host:port
#   YTMP3_OUTPUT=/Volumes/Drive/Music
#   YTMP3_ROTATE_URL=https://relay.example.com/api/proxies/<id>/rotate
#   YTMP3_ROTATE_AUTH=Bearer <token>
#   YTMP3_ROTATE_STATUS_URL=https://relay.example.com/api/proxies/<id>/rotation-status
```

CLI flags always override the values from `.env`. Pass `--proxy ""` to force
no proxy even when `YTMP3_PROXY` is set.

## Usage

```bash
# Single video → <output>/<Video Title>.mp3
python yt_mp3_downloader.py "https://www.youtube.com/watch?v=VIDEO_ID"

# Playlist → <output>/<Playlist Title>/<Video Title>.mp3
python yt_mp3_downloader.py "https://www.youtube.com/playlist?list=LIST_ID"

# Channel / account by URL → <output>/<Channel Name>/<Video Title>.mp3
python yt_mp3_downloader.py "https://www.youtube.com/@MrBeast"
python yt_mp3_downloader.py "https://www.youtube.com/@MrBeast/videos"
python yt_mp3_downloader.py "https://www.youtube.com/channel/UCX6OQ3DkcsbYNE6H8uQQuVA"

# Channel / account by bare handle (auto-expanded to /@<handle>/videos)
python yt_mp3_downloader.py @MrBeast
python yt_mp3_downloader.py MrBeast
python yt_mp3_downloader.py MrBeast/videos

# Custom output folder
python yt_mp3_downloader.py URL -o ~/Music/yt

# Limit a playlist / channel run to N items (after ordering)
python yt_mp3_downloader.py @MrBeast --limit 10

# Pick the order: 'latest' (default), 'oldest', or 'popular'
python yt_mp3_downloader.py @MrBeast --order popular --limit 10
python yt_mp3_downloader.py @MrBeast --order oldest  --limit 5

# Restrict a playlist or channel to specific items (mutually exclusive with --limit)
python yt_mp3_downloader.py URL --playlist-items 1-3,7

# Force a complete re-download (ignore the resume archive)
python yt_mp3_downloader.py URL --no-archive

# Through an HTTP / HTTPS proxy
python yt_mp3_downloader.py URL --proxy http://user:pass@host:port

# Aggressive (4 parallel fragments, no inter-video sleep) — direct
# connection or high-throughput datacenter proxy only.
python yt_mp3_downloader.py URL --aggressive
```

### Picking how many items, and in what order

Two flags control which items get downloaded from a playlist or channel.
They apply to both, so any example using `@handle` works equally well with
a `playlist?list=…` URL.

- `--limit N` — download at most `N` items from the collection. Mutually
exclusive with `--playlist-items`. When the order is `latest` (the
default), the slice is pushed down into yt-dlp's extraction so only the
N most-recent entries are fetched (fast). For `oldest` / `popular` the
full grid is fetched once, then ranked client-side and trimmed to N.
- `--order {latest,oldest,popular}` — ranking strategy.
  - `latest` (default): YouTube's natural newest-first order.
  - `oldest`: reversed (oldest uploads first).
  - `popular`: ranked by view count, descending.

For `--order popular`, view counts are read directly from the channel's
video grid (the same numbers shown next to each thumbnail), so no
per-video metadata fetch is required. If a few entries don't expose a
view count (e.g. private or members-only items in a playlist), they're
ranked last and the run continues.

```bash
# 5 newest uploads from a channel
python yt_mp3_downloader.py @MrBeast --limit 5

# 5 most-popular uploads from a channel (ranked over the whole channel)
python yt_mp3_downloader.py @MrBeast --order popular --limit 5

# 5 oldest uploads from a channel
python yt_mp3_downloader.py @MrBeast --order oldest --limit 5

# 10 most-popular videos from a playlist
python yt_mp3_downloader.py "https://www.youtube.com/playlist?list=…" \
    --order popular --limit 10
```

### Channel / account downloads

Channels are downloaded into a subfolder named after the channel (the same
way playlists are). Accepted input shapes:


| Input                                     | Resolves to                                    |
| ----------------------------------------- | ---------------------------------------------- |
| `@MrBeast`                                | `https://www.youtube.com/@MrBeast/videos`      |
| `MrBeast`                                 | `https://www.youtube.com/@MrBeast/videos`      |
| `MrBeast/videos`                          | `https://www.youtube.com/@MrBeast/videos`      |
| `https://www.youtube.com/@MrBeast`        | `https://www.youtube.com/@MrBeast/videos`      |
| `https://www.youtube.com/@MrBeast/videos` | unchanged                                      |
| `https://www.youtube.com/@MrBeast/shorts` | unchanged (downloads the Shorts tab as audio)  |
| `https://www.youtube.com/channel/UC…`     | `https://www.youtube.com/channel/UC…/videos`   |
| `https://www.youtube.com/c/<custom>`      | `https://www.youtube.com/c/<custom>/videos`    |
| `https://www.youtube.com/user/<legacy>`   | `https://www.youtube.com/user/<legacy>/videos` |


Bare channel root URLs and bare handles are auto-rewritten to the `/videos`
tab so the run downloads regular uploads only (no shorts/streams). If you
want a different tab, pass the full URL to that tab explicitly. You can use
`--playlist-items` to restrict the run (e.g. `--playlist-items 1-10` for the
10 most-recent uploads).

### Proxy-friendly defaults

When `--proxy` (or `YTMP3_PROXY`) is set, the script automatically:

- Drops `concurrent_fragment_downloads` from 4 → 1 to avoid swarming the
proxy with parallel TLS handshakes.
- Sets a 20 s `socket_timeout` so a stalled stream fails fast instead of
wedging the whole playlist.
- Sleeps 2–6 s between playlist items so the proxy network can breathe.
- Sends a mainstream desktop browser `User-Agent` so YouTube serves the
normal watch-page HTML instead of the mobile / consent redirect.
- Pre-flights the FalconProxy relay health endpoint when the proxy points
at `*.falconproxy.com` and prints a clear hint if it's down.

Use `--aggressive` to disable the safe profile when you're on a direct
connection or a datacenter HTTP proxy.

### Resume on rerun

Every successful track is recorded as a `youtube <video_id>` line in a
hidden archive file (default: `<output>/.yt_mp3_archive.txt`, per-playlist
for playlist URLs). When you re-run the same URL, items already in the
archive are skipped instantly and shown as `⏭ skip` in the summary table —
the failed ones get retried, no waste.

```bash
# First run — partial failure
python yt_mp3_downloader.py URL          # 20 ✓ ok, 3 ✗ fail

# Network recovers — re-run; only the 3 failures are retried
python yt_mp3_downloader.py URL          # 20 ⏭ skip, 3 ✓ ok
```

You can:

- Point `--archive PATH` at any file (e.g. a single shared archive across
multiple playlists).
- Disable the archive entirely with `--no-archive` to force every item to
be re-downloaded.
- Delete or edit the archive file to retry specific items.

The archive uses the standard yt-dlp format, so it's interchangeable with
yt-dlp's `--download-archive` flag if you want to reuse the same file.

### Block handling — IP rotation + fingerprint cycling

When YouTube returns a block (`403`, `429`, "Sign in to confirm you're not a
bot", "video unavailable", etc.), the downloader can:

1. **POST** your proxy's IP-rotation endpoint to swap to a new exit IP.
2. **Optionally poll** a status endpoint until the rotation completes.
3. **Rotate the User-Agent fingerprint** (Chrome / Firefox / Safari mix) so
  the retry doesn't carry the same signature as the blocked request.
4. **Retry** the failed item up to `--rotate-max-attempts` times.

```bash
python yt_mp3_downloader.py URL \
    --proxy http://USER:PASS@proxy.example.com:8081 \
    --rotate-url 'https://relay.example.com/api/proxies/<id>/rotate' \
    --rotate-auth 'Bearer <token>' \
    --rotate-status-url 'https://relay.example.com/api/proxies/<id>/rotation-status' \
    --rotate-wait 30 \
    --rotate-max-attempts 3
```

Or, equivalently, set `YTMP3_ROTATE_URL`, `YTMP3_ROTATE_AUTH`, and
`YTMP3_ROTATE_STATUS_URL` in `.env` and just run:

```bash
python yt_mp3_downloader.py URL
```

The rotation URL is plug-pluggable — anything that accepts a `POST` and
either returns a 2xx (with a fixed wait) or exposes a JSON status endpoint
with a `rotating` boolean and `last_result` field will work. The defaults
match the [FalconProxy](https://api.falconproxy.com) relay API:

- Trigger: `POST /api/proxies/<proxy_endpoint_id>/rotate`
- Status: `GET  /api/proxies/<proxy_endpoint_id>/rotation-status`

If your proxy doesn't expose a rotation API, you can omit `--rotate-url`;
the downloader will still cycle User-Agent fingerprints across items.
Disable that with `--no-rotate-fingerprint`.

### CLI

```
positional arguments:
  TARGET                    YouTube video URL, playlist URL, channel URL,
                            or bare channel handle (e.g. '@handle').

options:
  -o, --output OUTPUT       Output folder (default: ./downloads).
  --proxy PROXY             HTTP / HTTPS proxy URL. Pass "" to disable.
                            SOCKS proxies are not supported (see Notes).
  --playlist-items SPEC     Restrict a playlist or channel to specific
                            items (e.g. '1-3', '1,5,8').
  --limit N                 Download at most N items (applied after
                            ordering). Mutually exclusive with
                            --playlist-items.
  --order {latest,oldest,popular}
                            Order in which collection items are picked.
                            Default: latest.
  --archive PATH            Path to the resume-on-rerun download archive
                            (default: <output>/.yt_mp3_archive.txt).
  --no-archive              Disable the archive (force full re-download).
  --aggressive              Disable the gentle proxy profile.

block handling:
  --rotate-url URL          POST URL hit when a block is detected.
  --rotate-auth HEADER      Authorization header value (e.g. 'Bearer xyz').
  --rotate-status-url URL   Optional GET URL polled until rotation completes.
  --rotate-wait SEC         Max seconds to wait per rotation. Default 30.
  --rotate-max-attempts N   Max attempts per item on blocks. Default 2.
  --no-rotate-fingerprint   Pin one User-Agent for the whole run.
```

## How playlist & channel detection works

- URLs containing a `list=` query parameter are treated as **playlists**,
**except** auto-generated YouTube "Mix" / "Radio" lists (IDs starting
with `RD`), which are effectively endless and are downloaded as a single
video instead.
- URLs matching `/@handle`, `/@handle/<tab>`, `/channel/UC…`, `/c/<name>`,
or `/user/<name>` are treated as **channels**. Bare channel root URLs
(no tab) are auto-rewritten to the `/videos` tab.
- Anything that isn't an `http(s)://` URL is interpreted as a bare channel
handle and expanded to `https://www.youtube.com/@<handle>/videos`.

## Notes

- **SOCKS proxies are not supported.** yt-dlp's SOCKS transport relies on
PySocks, which monkey-patches `socket.socket` and is unreliable on
consecutive requests through some proxies — the first request typically
succeeds, then subsequent connections silently stall. The HTTP / HTTPS
path goes through `urllib3`'s proxy handler instead and works reliably.
If you only have a SOCKS endpoint available, terminate it locally with a
tool like `dante` or `srelay` and point the downloader at the local HTTP
proxy. The script rejects `socks*://` URLs with a clear error.
- The MP3 quality is fixed at **320 kbps**, codec `**libmp3lame`** (hard-coded;
edit `MP3_BITRATE_KBPS` in `yt_mp3_downloader.py` if you ever want to change it).
- Filenames are constrained to be Windows-safe so the output is portable.
- Each playlist item is downloaded in its own yt-dlp invocation, so a single
unavailable video doesn't abort the rest of the playlist; the failed item
is reported in the final summary table.

