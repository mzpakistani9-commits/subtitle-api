import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

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
    """Format plain text into requested output format."""
    if fmt == "txt":
        return text.strip()

    lines = text.strip().split("\n")
    if fmt in ("srt", "vtt"):
        result = []
        idx = 0
        for i, line in enumerate(lines):
            if line.strip():
                idx += 1
                result.append(str(idx))
                result.append(f"00:00:{i:02d},000 --> 00:00:{i+3:02d},000")
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

def fetch_via_transcript_api(video_id: str, lang: str = "en") -> str | None:
    """Fetch subtitles using youtube-transcript-api (no auth needed)."""
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
            segments = transcript if isinstance(transcript, list) else list(transcript)
            texts = []
            for s in segments:
                if isinstance(s, dict):
                    texts.append(s.get("text", ""))
                elif isinstance(s, str):
                    texts.append(s)
            if texts:
                return "\n".join(texts)
        except (NoTranscriptFound, TranscriptsDisabled):
            # Try any transcript
            try:
                transcript = transcript_list.find_transcript([]).fetch()
                segments = transcript if isinstance(transcript, list) else list(transcript)
                texts = []
                for s in segments:
                    if isinstance(s, dict):
                        texts.append(s.get("text", ""))
                    elif isinstance(s, str):
                        texts.append(s)
                if texts:
                    return "\n".join(texts)
            except Exception:
                pass
    except TranscriptsDisabled:
        return None
    except Exception:
        pass

    return None


# ── Fallback: yt-dlp with Android client ──────────────

def fetch_via_ytdlp(video_url: str, lang: str) -> str | None:
    """Fallback: download subtitles using yt-dlp (Android client to avoid bot detection)."""
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
        for line in vtt_text.split("\n"):
            line = line.strip()
            if not line or line == "WEBVTT" or "-->" in line or line.isdigit():
                continue
            lines.append(re.sub(r"<[^>]+>", "", line).strip())
        return "\n".join(l for l in lines if l)


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


# ── OpenSubtitles Proxy (key lives server-side only) ────

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


@app.get("/opensubtitles")
def opensubtitles_proxy(
    query: str = Query(...),
    imdb_id: str = Query(""),
    language: str = Query("en"),
    format: str = Query("srt"),
):
    """Proxy for OpenSubtitles API — the API key stays on the server."""
    if not OPENSEARCH_API_KEY:
        return {"error": "OpenSubtitles API key not configured on server"}

    search_query = imdb_id if imdb_id else query
    search_url = f"https://api.opensubtitles.com/api/v1/subtitles?query={urllib.parse.quote(search_query)}&languages={language}"
    headers = {
        "Api-Key": OPENSEARCH_API_KEY,
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
    }
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
    dl_headers = {
        "Api-Key": OPENSEARCH_API_KEY,
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.5",
    }
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

    word_count = len(file_text.split())
    return {
        "ok": True,
        "text": file_text,
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
def download(url: str = Query(...), format: str = Query("txt"), lang: str = Query("en")):
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
def download_raw(url: str = Query(...), format: str = Query("txt"), lang: str = Query("en")):
    result = download(url, format, lang)
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return PlainTextResponse(result["text"], media_type="text/plain; charset=utf-8",
                             headers={"Content-Disposition": f"attachment; filename=subtitles.{format}"})


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
