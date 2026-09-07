# Subtitle API

FastAPI service that fetches and generates subtitles for YouTube videos — backend for SubtitleHub.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/opensubtitles/login` | OpenSubtitles auth (returns token) |
| `GET` | `/opensubtitles` | Search OpenSubtitles for movie/TV subs |
| `GET` | `/download?url=<video>` | Fetch captions as SRT/VTT/TXT |
| `POST` | `/download` | Same, POST variant |
| `GET` | `/download/raw` | Raw transcript text |
| `GET` | `/playlist` | Captions for a YouTube playlist |
| `GET` | `/` | Health check + endpoint list |

## How it works

Fetch strategy (fallback chain):
1. **`youtube-transcript-api`** — fast, direct caption extraction
2. **`yt-dlp`** — resilient pipeline for videos without auto-captions
3. YouTube timedtext proxy as last resort

Format conversion (`format_output`) → **srt, vtt, txt, json**.

## Features

- Rate limiting: 30 req / 60s per IP (`RATE_LIMIT_REQUESTS`)
- CORS enabled for web clients
- Optional OpenSubtitles integration for movie/TV subtitle search

## Run locally

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

## Deploy

One-click on Render via `render.yaml` (uvicorn). If using OpenSubtitles, import `OPENSUBTITLES_API_KEY` as a **Render secret** — never commit it (this repo had one leaked in history; rotate it).