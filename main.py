import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

# ── Rate Limiter ──────────────────────────────────────────

RATE_LIMIT_REQUESTS = 30
RATE_LIMIT_WINDOW = 60  # seconds
_rate_store: dict[str, list[float]] = defaultdict(list)


def rate_limit(ip: str) -> bool:
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW
    timestamps = _rate_store[ip]
    # prune old entries
    _rate_store[ip] = [t for t in timestamps if t > window_start]
    if len(_rate_store[ip]) >= RATE_LIMIT_REQUESTS:
        return False
    _rate_store[ip].append(now)
    return True


def rate_limited(request: Request):
    ip = request.client.host if request.client else "unknown"
    if not rate_limit(ip):
        raise HTTPException(status_code=429, detail="Too many requests. Slow down.")


app = FastAPI(title="Subtitle Downloader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup():
    """Update yt-dlp on startup (fallback only)."""
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "--upgrade", "yt-dlp"],
            capture_output=True, text=True, timeout=60
        )
        ver = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True)
        print(f"yt-dlp version: {ver.stdout.strip() or ver.stderr.strip()}")
    except Exception as e:
        print(f"yt-dlp update failed: {e}")


# ── Helpers ──────────────────────────────────────────────

def extract_video_id(url: str) -> str | None:
    """Extract YouTube video ID from various URL formats."""
    patterns = [
        r"(?:youtube\.com/watch\?.*v=)([\w-]+)",
        r"(?:youtu\.be/)([\w-]+)",
        r"(?:youtube\.com/embed/)([\w-]+)",
        r"(?:youtube\.com/v/)([\w-]+)",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


def format_output(text: str, fmt: str) -> str:
    if fmt == "txt":
        return text.strip()

    lines = [l for l in text.strip().split("\n") if l.strip()]
    if fmt in ("srt", "vtt"):
        result = []
        for i, line in enumerate(lines):
            start_s = i * 3
            end_s = start_s + 3
            result.append(str(i + 1))
            result.append(f"{start_s // 3600:02d}:{(start_s % 3600) // 60:02d}:{start_s % 60:02d},000 --> {end_s // 3600:02d}:{(end_s % 3600) // 60:02d}:{end_s % 60:02d},000")
            result.append(line.strip())
            result.append("")
        if fmt == "vtt":
            result.insert(0, "WEBVTT")
            result.insert(1, "")
        return "\n".join(result)

    if fmt == "json":
        words = [{"word": w, "index": i}
                 for i, w in enumerate(text.split()) if w.strip()]
        return json.dumps({"text": text.strip(), "words": words}, indent=2)

    return text.strip()


# ── Primary: youtube-transcript-api ─────────────────────

def _extract_transcript_text(transcript) -> str | None:
    segments = transcript if isinstance(transcript, list) else list(transcript)
    texts = []
    for s in segments:
        if isinstance(s, dict):
            texts.append(s.get("text", ""))
        elif isinstance(s, str):
            texts.append(s)
        elif hasattr(s, "text"):
            texts.append(s.text)
    return "\n".join(texts) if texts else None


def fetch_via_transcript_api(video_id: str, lang: str = "en") -> str | None:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
    except ImportError:
        return None

    try:
        api = YouTubeTranscriptApi()
        transcript_list = api.list(video_id)
        langs = [l.strip() for l in lang.split(",")]

        try:
            transcript = transcript_list.find_transcript(langs).fetch()
            result = _extract_transcript_text(transcript)
            if result:
                return result
        except (NoTranscriptFound, TranscriptsDisabled):
            pass

        # Fallback: try any available transcript
        try:
            for t in transcript_list:
                transcript = t.fetch()
                result = _extract_transcript_text(transcript)
                if result:
                    return result
        except Exception:
            pass
    except TranscriptsDisabled:
        return None
    except Exception:
        pass

    return None


# ── Fallback: yt-dlp with Android client ──────────────

def fetch_via_ytdlp(video_url: str, lang: str) -> str | None:
    with tempfile.TemporaryDirectory() as tmp:
        outtmpl = os.path.join(tmp, "%(title)s.%(ext)s")
        cmd = [
            "yt-dlp", "--skip-download",
            "--write-subs", "--write-auto-subs",
            "--sub-langs", lang, "--sub-format", "vtt",
            "--extractor-args", "youtube:player_client=android",
            "--sleep-requests", "1", "--sleep-interval", "2",
            "--max-sleep-interval", "5", "--retries", "10",
            "--ignore-errors", "--no-warnings",
            "-o", outtmpl, video_url,
        ]
        subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        vtt_files = sorted([f for f in os.listdir(tmp) if f.endswith(".vtt")])
        if not vtt_files:
            return None

        with open(os.path.join(tmp, vtt_files[0]), encoding="utf-8") as f:
            vtt_text = f.read()

        lines = []
        for entry in re.split(r"\n\n+", vtt_text):
            entry = entry.strip()
            if not entry or entry == "WEBVTT":
                continue
            text_parts = []
            for part in entry.split("\n"):
                part = part.strip()
                if not part or "-->" in part or part.startswith("Kind:") or part.startswith("Language:"):
                    continue
                part = re.sub(r"<[^>]+>", "", part).strip()
                if part:
                    text_parts.append(part)
            if text_parts:
                lines.append(" ".join(text_parts))
        return "\n".join(lines) if lines else None


# ── Backup: youtubetranscript.com API ───────────────────

def fetch_via_ytcom(video_id: str) -> str | None:
    """Fetch via youtubetranscript.com (free third-party API)."""
    url = f"https://youtubetranscript.com/?v={video_id}&format=json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read().decode())
        if isinstance(data, list):
            return "\n".join(s["text"] for s in data if "text" in s)
    except Exception:
        pass
    return None


# ── OpenSubtitles Proxy (key + user JWT) ─────────────────

OPENSEARCH_API_KEY = os.environ.get("OPENSUBTITLES_API_KEY", "")
USER_AGENT = "SubtitleHub v1.0"

# HTTP opener that follows redirects
_opener = None


def _get_opener():
    global _opener
    if _opener is None:
        _opener = urllib.request.build_opener(
            urllib.request.HTTPRedirectHandler(),
            urllib.request.HTTPSHandler(),
        )
    return _opener


def _fetch_json(url, headers=None, data=None, method=None, timeout=15):
    req = urllib.request.Request(url, headers=headers or {}, data=data, method=method)
    resp = _get_opener().open(req, timeout=timeout)
    return json.loads(resp.read().decode())


def _os_headers(token: str = "") -> dict:
    """Return headers for OpenSubtitles API. If token given, use Bearer auth."""
    h = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    else:
        h["Api-Key"] = OPENSEARCH_API_KEY
    return h


@app.post("/opensubtitles/login")
def opensubtitles_login(body: dict):
    """Login to OpenSubtitles with user credentials, return JWT token."""
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    if not username or not password:
        return {"error": "Username and password are required"}
    try:
        payload = json.dumps({"username": username, "password": password}).encode()
        headers = {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        resp_data = _fetch_json(
            "https://api.opensubtitles.com/api/v1/login",
            headers=headers, data=payload, method="POST", timeout=15,
        )
        token = resp_data.get("token")
        if token:
            return {"ok": True, "token": token, "user": resp_data.get("user", {})}
        return {"error": resp_data.get("message", "Login failed")}
    except Exception as e:
        return {"error": f"OpenSubtitles login failed: {e}"}


@app.get("/opensubtitles")
def opensubtitles_proxy(
    request: Request,
    query: str = Query(...),
    imdb_id: str = Query(""),
    language: str = Query("en"),
    format: str = Query("srt"),
    token: str = Query(""),
):
    """Proxy for OpenSubtitles API.
    - If token provided, uses user's JWT (20 downloads/day).
    - Otherwise uses server API key (5 downloads/day fallback).
    """
    rate_limited(request)

    if not token and not OPENSEARCH_API_KEY:
        return {"error": "OpenSubtitles API key not configured on server"}
    if not token and not OPENSEARCH_API_KEY:
        return {"error": "Login required — no server API key configured"}

    search_query = imdb_id if imdb_id else query
    search_url = f"https://api.opensubtitles.com/api/v1/subtitles?query={urllib.parse.quote(search_query)}&languages={language}"
    headers = _os_headers(token)
    try:
        data = _fetch_json(search_url, headers=headers)
    except Exception as e:
        return {"error": f"OpenSubtitles search failed: {e}"}

    items = data.get("data") or []
    if not items:
        return {"error": f'No subtitles found for "{query}"'}

    first = items[0]
    attrs = first.get("attributes") or {}
    files = attrs.get("files") or []
    if not files:
        return {"error": "No subtitle files found"}
    file_id = files[0].get("file_id")
    if not file_id:
        return {"error": "No file ID"}

    dl_body = json.dumps({"file_id": file_id}).encode()
    dl_headers = _os_headers(token)
    dl_headers["Content-Type"] = "application/json"
    try:
        dl_data = _fetch_json(
            "https://api.opensubtitles.com/api/v1/download",
            headers=dl_headers,
            data=dl_body,
            method="POST",
            timeout=15,
        )
    except Exception as e:
        return {"error": f"OpenSubtitles download failed: {e}"}

    link = dl_data.get("link")
    if not link:
        return {"error": "No download link returned"}

    try:
        file_req = urllib.request.Request(link, headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.5",
        })
        file_resp = _get_opener().open(file_req, timeout=30)
        file_text = file_resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return {"error": f"Subtitle file fetch failed: {e}"}

    if not file_text.strip():
        return {"error": "Empty subtitle file"}

    clean_text = re.sub(r"\d+\s*\n\d{2}:\d{2}:\d{2}[.,]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[.,]\d{3}\s*", "", file_text)
    clean_text = re.sub(r"<[^>]+>", "", clean_text).strip()
    word_count = len([w for w in clean_text.split() if w.strip()])

    output = format_output(clean_text, format) if format in ("txt", "srt", "vtt", "json") else clean_text

    return {
        "ok": True,
        "text": output,
        "word_count": word_count,
        "language": language,
        "source": "opensubtitles",
    }


# ── Endpoints ───────────────────────────────────────────

class SubtitleRequest(BaseModel):
    url: str
    format: str = "txt"
    lang: str = "en"


@app.get("/")
def root():
    return {"status": "ok", "usage": "POST /download with {'url': '...', 'format': 'txt|srt|vtt|json', 'lang': 'en'}"}


@app.get("/download")
@app.post("/download")
def download(
    request: Request,
    url: str = Query(...),
    format: str = Query("txt"),
    lang: str = Query("en"),
):
    rate_limited(request)
    video_id = extract_video_id(url)
    if not video_id:
        return {"error": "Invalid YouTube URL"}

    # Try multiple methods in order
    text = fetch_via_transcript_api(video_id, lang)
    if text is None:
        text = fetch_via_ytcom(video_id)
    if text is None:
        text = fetch_via_ytdlp(url, lang)

    if text is None:
        return {"error": "No subtitles found for this video. Try a different video or language."}

    output = format_output(text, format)
    word_count = len(text.split())

    return {
        "ok": True,
        "url": url,
        "language": lang,
        "format": format,
        "word_count": word_count,
        "text": output,
    }


@app.get("/download/raw")
def download_raw(
    request: Request,
    url: str = Query(...),
    format: str = Query("txt"),
    lang: str = Query("en"),
):
    rate_limited(request)
    result = download(url, format, lang)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return PlainTextResponse(result["text"], media_type="text/plain; charset=utf-8",
                             headers={"Content-Disposition": f"attachment; filename=subtitles.{format}"})


@app.get("/playlist")
def playlist(
    request: Request,
    url: str = Query(...),
    format: str = Query("txt"),
    lang: str = Query("en"),
):
    rate_limited(request)
    playlist_id = None
    m = re.search(r"[&?]list=([\w-]+)", url)
    if m:
        playlist_id = m.group(1)

    if not playlist_id:
        return {"error": "Could not extract playlist ID from URL"}

    try:
        cmd = [
            "yt-dlp", "--flat-playlist", "--dump-json",
            "--no-warnings", "--ignore-errors",
            f"https://www.youtube.com/playlist?list={playlist_id}",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0 and not result.stdout:
            return {"error": f"yt-dlp failed: {result.stderr.strip()}"}

        entries = []
        for line in result.stdout.strip().split("\n"):
            if line.strip():
                try:
                    entry = json.loads(line)
                    vid = entry.get("id")
                    if vid:
                        entries.append(vid)
                except json.JSONDecodeError:
                    continue

        if not entries:
            return {"error": "No videos found in playlist"}
    except Exception as e:
        return {"error": f"Failed to fetch playlist: {e}"}

    limited = entries[:50]
    results = []
    success_count = 0
    total_words = 0

    for vid in limited:
        vid_url = f"https://www.youtube.com/watch?v={vid}"
        text = fetch_via_transcript_api(vid, lang)
        if text is None:
            text = fetch_via_ytcom(vid)
        if text is None:
            text = fetch_via_ytdlp(vid_url, lang)
        if text:
            success_count += 1
            total_words += len(text.split())
            results.append(f"=== Video {vid} ===\n{format_output(text, format)}")
        else:
            results.append(f"=== Video {vid} === [No subtitles]")

    if success_count == 0:
        return {"error": "No subtitles found in any playlist video"}

    combined = "\n\n".join(results)
    return {
        "ok": True,
        "text": f"Playlist: {success_count}/{len(limited)} videos successful\n\n{combined}",
        "word_count": total_words,
        "total_videos": len(limited),
        "successful": success_count,
        "language": lang,
        "source": "playlist",
    }


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
