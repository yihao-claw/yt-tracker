#!/usr/bin/env python3
"""
YT Preload: Download subtitles/audio for new videos, transcribe via Groq Whisper.
Input: JSON from yt-check-new.py (stdin or --input file)
Output: Per-video transcript + metadata in output dir

Usage:
  python3 yt-check-new.py | python3 yt-preload.py --output /tmp/yt-preload
  python3 yt-preload.py --input check-result.json --output /tmp/yt-preload
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_WHISPER_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
WHISPER_MODEL = "whisper-large-v3-turbo"
MAX_AUDIO_SIZE_MB = 25  # Groq limit


def run_cmd(cmd: list[str], timeout: int = 300) -> tuple[int, str, str]:
    """Run command, return (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


def download_subtitles(video_id: str, work_dir: Path) -> str | None:
    """Try to download subtitles. Returns transcript text or None."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    
    # Try manual subs first, then auto-generated
    for sub_args in [
        ["--write-sub", "--sub-lang", "zh-Hant,zh-Hans,zh,en,ja"],
        ["--write-auto-sub", "--sub-lang", "zh-Hant,zh-Hans,zh,en,ja"],
    ]:
        cmd = [
            "yt-dlp", "--skip-download",
            *sub_args,
            "--sub-format", "vtt/srt/best",
            "--convert-subs", "srt",
            "-o", str(work_dir / "%(id)s.%(ext)s"),
            url,
        ]
        rc, stdout, stderr = run_cmd(cmd, timeout=60)
        
        # Check if any subtitle file was created
        for f in work_dir.iterdir():
            if f.suffix in (".srt", ".vtt") and video_id in f.name:
                text = parse_subtitle(f)
                if text and len(text.strip()) > 50:
                    return text
    
    return None


def parse_subtitle(path: Path) -> str:
    """Parse SRT/VTT to plain text."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    text_lines = []
    for line in lines:
        line = line.strip()
        # Skip timestamps, sequence numbers, VTT headers
        if not line:
            continue
        if line.isdigit():
            continue
        if "-->" in line:
            continue
        if line.startswith("WEBVTT") or line.startswith("Kind:") or line.startswith("Language:"):
            continue
        # Remove HTML tags
        import re
        line = re.sub(r"<[^>]+>", "", line)
        if line and line not in text_lines[-1:]:  # dedup consecutive
            text_lines.append(line)
    return "\n".join(text_lines)


def download_audio(video_id: str, work_dir: Path) -> Path | None:
    """Download audio only. Returns path to mp3 or None."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    output_template = str(work_dir / f"{video_id}.%(ext)s")
    
    cmd = [
        "yt-dlp", "-x",
        "--audio-format", "mp3",
        "--audio-quality", "5",  # lower quality = smaller file
        "--no-playlist",
        "-o", output_template,
        url,
    ]
    rc, stdout, stderr = run_cmd(cmd, timeout=300)
    
    # Find the mp3 file
    for f in work_dir.iterdir():
        if f.suffix == ".mp3" and video_id in f.name:
            size_mb = f.stat().st_size / (1024 * 1024)
            if size_mb > MAX_AUDIO_SIZE_MB:
                print(f"  WARNING: Audio {size_mb:.1f}MB exceeds {MAX_AUDIO_SIZE_MB}MB limit", file=sys.stderr)
                # Trim to first 20 minutes
                trimmed = work_dir / f"{video_id}_trimmed.mp3"
                trim_cmd = ["ffmpeg", "-i", str(f), "-t", "1200", "-y", str(trimmed)]
                run_cmd(trim_cmd, timeout=60)
                if trimmed.exists():
                    return trimmed
            return f
    return None


def transcribe_groq(audio_path: Path) -> str | None:
    """Transcribe audio via Groq Whisper API."""
    if not GROQ_API_KEY:
        print("  WARNING: GROQ_API_KEY not set, skipping transcription", file=sys.stderr)
        return None

    import http.client
    import mimetypes
    from urllib.parse import urlparse

    boundary = "----FormBoundary" + str(int(time.time()))
    
    # Build multipart form data
    audio_data = audio_path.read_bytes()
    filename = audio_path.name
    
    body = b""
    # File field
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
    body += b"Content-Type: audio/mpeg\r\n\r\n"
    body += audio_data
    body += b"\r\n"
    # Model field
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="model"\r\n\r\n'
    body += WHISPER_MODEL.encode()
    body += b"\r\n"
    # Language hint (optional, helps accuracy)
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="language"\r\n\r\n'
    body += b"zh"
    body += b"\r\n"
    # Response format
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="response_format"\r\n\r\n'
    body += b"text"
    body += b"\r\n"
    body += f"--{boundary}--\r\n".encode()

    parsed = urlparse(GROQ_WHISPER_URL)
    conn = http.client.HTTPSConnection(parsed.hostname, timeout=120)
    
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    
    for attempt in range(3):
        try:
            conn.request("POST", parsed.path, body=body, headers=headers)
            resp = conn.getresponse()
            resp_body = resp.read().decode("utf-8")
            
            if resp.status == 200:
                return resp_body
            elif resp.status == 429:
                wait = min(30 * (attempt + 1), 90)
                print(f"  Rate limited, waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
            else:
                print(f"  Groq API error {resp.status}: {resp_body[:200]}", file=sys.stderr)
                return None
        except Exception as e:
            print(f"  Groq API exception: {e}", file=sys.stderr)
            if attempt < 2:
                time.sleep(10)
    
    return None


def get_video_metadata(video_id: str, work_dir: Path) -> dict:
    """Get video metadata via yt-dlp."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [
        "yt-dlp", "--dump-json", "--no-download", url,
    ]
    rc, stdout, stderr = run_cmd(cmd, timeout=60)
    if rc == 0 and stdout.strip():
        try:
            data = json.loads(stdout)
            return {
                "title": data.get("title", ""),
                "description": data.get("description", "")[:2000],
                "duration": data.get("duration", 0),
                "upload_date": data.get("upload_date", ""),
                "channel": data.get("channel", ""),
                "view_count": data.get("view_count", 0),
                "like_count": data.get("like_count", 0),
                "tags": data.get("tags", [])[:10],
            }
        except json.JSONDecodeError:
            pass
    return {"title": "", "description": "", "duration": 0}


def process_video(video_id: str, title: str, channel_name: str, output_dir: Path) -> dict:
    """Process a single video: subtitles → audio → transcribe."""
    result = {
        "videoId": video_id,
        "title": title,
        "channel": channel_name,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "transcript": None,
        "transcriptSource": None,
        "metadata": None,
        "error": None,
    }
    
    with tempfile.TemporaryDirectory(prefix=f"yt-{video_id}-") as tmp:
        work_dir = Path(tmp)
        
        # 1. Get metadata
        print(f"  [1/3] Fetching metadata...", file=sys.stderr)
        result["metadata"] = get_video_metadata(video_id, work_dir)
        if not result["title"] and result["metadata"].get("title"):
            result["title"] = result["metadata"]["title"]
        
        # 2. Try subtitles first (cheapest)
        print(f"  [2/3] Trying subtitles...", file=sys.stderr)
        subs = download_subtitles(video_id, work_dir)
        if subs and len(subs.strip()) > 100:
            result["transcript"] = subs
            result["transcriptSource"] = "subtitles"
            print(f"  ✅ Got subtitles ({len(subs)} chars)", file=sys.stderr)
        else:
            # 3. Download audio + Whisper
            print(f"  [3/3] Downloading audio + Whisper...", file=sys.stderr)
            audio = download_audio(video_id, work_dir)
            if audio:
                size_mb = audio.stat().st_size / (1024 * 1024)
                print(f"  Audio: {size_mb:.1f}MB, transcribing...", file=sys.stderr)
                transcript = transcribe_groq(audio)
                if transcript:
                    result["transcript"] = transcript
                    result["transcriptSource"] = "whisper"
                    print(f"  ✅ Whisper transcript ({len(transcript)} chars)", file=sys.stderr)
                else:
                    result["error"] = "whisper_failed"
                    # Fall back to description
                    desc = result["metadata"].get("description", "")
                    if desc:
                        result["transcript"] = f"[No transcript available. Video description:]\n{desc}"
                        result["transcriptSource"] = "description_fallback"
            else:
                result["error"] = "audio_download_failed"
                desc = result["metadata"].get("description", "")
                if desc:
                    result["transcript"] = f"[No transcript available. Video description:]\n{desc}"
                    result["transcriptSource"] = "description_fallback"
    
    # Save individual transcript
    video_out = output_dir / f"{video_id}.json"
    video_out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    
    if result["transcript"]:
        txt_out = output_dir / f"{video_id}.txt"
        txt_out.write_text(result["transcript"])
    
    return result


def main():
    parser = argparse.ArgumentParser(description="Preload YT video transcripts")
    parser.add_argument("--input", "-i", help="Input JSON from yt-check-new.py (default: stdin)")
    parser.add_argument("--output", "-o", default="/tmp/yt-preload", help="Output directory")
    args = parser.parse_args()
    
    # Read input
    if args.input:
        check_data = json.loads(Path(args.input).read_text())
    else:
        check_data = json.load(sys.stdin)
    
    if not check_data.get("hasNew"):
        print(json.dumps({"hasNew": False, "videos": []}))
        return
    
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Process each new video
    all_results = []
    new_videos = check_data.get("newVideos", {})
    
    for channel_id, channel_data in new_videos.items():
        channel_name = channel_data.get("name", channel_id)
        videos = channel_data.get("videos", [])
        
        for v in videos:
            vid = v["videoId"]
            title = v.get("title", "")
            print(f"\n📹 Processing: {channel_name} — {title or vid}", file=sys.stderr)
            
            result = process_video(vid, title, channel_name, output_dir)
            all_results.append(result)
    
    # Write summary
    summary = {
        "hasNew": True,
        "processedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "videoCount": len(all_results),
        "withTranscript": sum(1 for r in all_results if r["transcript"]),
        "transcriptSources": {
            "subtitles": sum(1 for r in all_results if r.get("transcriptSource") == "subtitles"),
            "whisper": sum(1 for r in all_results if r.get("transcriptSource") == "whisper"),
            "description_fallback": sum(1 for r in all_results if r.get("transcriptSource") == "description_fallback"),
        },
        "videos": all_results,
    }
    
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
