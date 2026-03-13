# YT Tracker 🐦

YouTube channel monitor with intelligent pre-loading. Detects new videos, downloads transcripts (subtitles or Whisper), and feeds them to an LLM for deep analysis.

## Architecture

```
Hourly cron trigger
  ↓
yt-check-new.py (RSS + yt-dlp fallback, 0 tokens)
  ├─ No new videos → HEARTBEAT_OK (done)
  └─ New videos found ↓
yt-preload.py (subtitles → audio → Groq Whisper, 0 tokens)
  ↓
LLM: analyze transcript → research → synthesize → push to Telegram
```

## Files

| File | Description |
|------|-------------|
| `yt-channels.json` | Channel list (user-editable, add/remove channels here) |
| `yt-check-new.py` | Pre-check script: RSS + yt-dlp fallback for new video detection |
| `yt-preload.py` | Preload script: subtitles/audio download + Whisper transcription |
| `yt-tracker-state.json` | Runtime state (lastSeenVideoIds, lastNotifiedAt) |

## Adding a Channel

Edit `yt-channels.json`:

```json
{
  "channelId": "UC...",
  "name": "Channel Name",
  "handle": "@handle",
  "category": "Category",
  "enabled": true
}
```

Set `"enabled": false` to pause tracking without removing.

## Dependencies

- `yt-dlp` — video/audio download + playlist scraping
- `ffmpeg` — audio conversion
- `deno` — yt-dlp JS runtime for subtitle extraction
- Groq API key — for Whisper transcription (free tier: 8h audio/day)

## Usage

```bash
# Check for new videos
python3 yt-check-new.py

# Full pipeline: check + preload transcripts
python3 yt-check-new.py | GROQ_API_KEY=... python3 yt-preload.py -o /tmp/yt-preload
```
