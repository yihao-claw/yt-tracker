#!/usr/bin/env python3
"""
YT pre-check: fetch RSS for all tracked channels, compare against state.
Prints JSON with only channels that have new videos.
Exit 0 + empty newVideos = nothing new (skip LLM).
Exit 0 + non-empty newVideos = new videos found (trigger LLM).
"""

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

STATE_FILE = Path(__file__).parent / "yt-tracker-state.json"
CHANNELS_FILE = Path(__file__).parent / "yt-channels.json"
RSS_TEMPLATE = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
TIMEOUT = 15


def fetch_rss(channel_id: str) -> list[dict]:
    """Fetch RSS and return list of {videoId, title, published}."""
    url = RSS_TEMPLATE.format(channel_id)
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(req, timeout=TIMEOUT) as resp:
            xml_text = resp.read().decode("utf-8")
    except (URLError, Exception) as e:
        print(f"INFO: RSS failed for {channel_id}, will try yt-dlp: {e}", file=sys.stderr)
        return []

    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
    }
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        print(f"WARNING: Failed to parse RSS for {channel_id}", file=sys.stderr)
        return []

    entries = []
    for entry in root.findall("atom:entry", ns):
        vid = entry.find("yt:videoId", ns)
        title = entry.find("atom:title", ns)
        published = entry.find("atom:published", ns)
        if vid is not None:
            entries.append({
                "videoId": vid.text,
                "title": title.text if title is not None else "",
                "published": published.text if published is not None else "",
            })
    return entries


def fetch_via_ytdlp(channel_id: str, handle: str = "") -> list[dict]:
    """Fallback: use yt-dlp --flat-playlist to get recent videos."""
    if handle:
        url = f"https://www.youtube.com/{handle}/videos"
    else:
        url = f"https://www.youtube.com/channel/{channel_id}/videos"

    try:
        r = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--dump-json", "--playlist-end", "5", url],
            capture_output=True, text=True, timeout=45,
        )
        entries = []
        for line in r.stdout.strip().split("\n"):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                entries.append({
                    "videoId": d.get("id", ""),
                    "title": d.get("title", ""),
                    "published": "",
                })
            except json.JSONDecodeError:
                pass
        if entries:
            print(f"  yt-dlp got {len(entries)} videos for {handle or channel_id}", file=sys.stderr)
        return entries
    except Exception as e:
        print(f"WARNING: yt-dlp failed for {handle or channel_id}: {e}", file=sys.stderr)
        return []


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--frequency", "-f", help="Filter by frequency (hourly/daily)")
    args = parser.parse_args()

    if not STATE_FILE.exists():
        print(json.dumps({"error": "state file not found"}))
        sys.exit(1)

    state = json.loads(STATE_FILE.read_text())

    # Load channel list from yt-channels.json (user-editable)
    # Falls back to state file channels if yt-channels.json doesn't exist
    if CHANNELS_FILE.exists():
        channel_list = json.loads(CHANNELS_FILE.read_text())
        channels = {}
        for ch in channel_list:
            if not ch.get("enabled", True):
                continue
            if args.frequency and ch.get("frequency", "daily") != args.frequency:
                continue
            cid = ch["channelId"]
            # Merge with state data (preserves lastSeenVideoIds etc.)
            state_ch = state.get("channels", {}).get(cid, {})
            channels[cid] = {
                **state_ch,
                "name": ch.get("name", state_ch.get("name", "")),
                "handle": ch.get("handle", state_ch.get("handle", "")),
                "category": ch.get("category", state_ch.get("category", "")),
            }
            # Initialize state entry for new channels
            if cid not in state.get("channels", {}):
                state.setdefault("channels", {})[cid] = {
                    "name": ch.get("name", ""),
                    "handle": ch.get("handle", ""),
                    "category": ch.get("category", ""),
                    "lastSeenVideoIds": [],
                }
                STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
                print(f"NEW CHANNEL added to state: {ch.get('name', cid)}", file=sys.stderr)
    else:
        channels = state.get("channels", {})

    # Phase 1: Fetch all RSS in parallel
    results = {}
    rss_failed = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(fetch_rss, cid): cid for cid in channels
        }
        for future in as_completed(futures):
            cid = futures[future]
            try:
                entries = future.result()
                if entries:
                    results[cid] = entries
                else:
                    rss_failed.append(cid)
            except Exception as e:
                print(f"WARNING: {cid} error: {e}", file=sys.stderr)
                rss_failed.append(cid)

    # Phase 2: For failed RSS, use yt-dlp fallback (sequential to avoid throttling)
    if rss_failed:
        print(f"Using yt-dlp fallback for {len(rss_failed)} channels...", file=sys.stderr)
        for cid in rss_failed:
            handle = channels.get(cid, {}).get("handle", "")
            entries = fetch_via_ytdlp(cid, handle)
            if entries:
                results[cid] = entries

    # Compare against state
    new_videos = {}
    for cid, entries in results.items():
        seen = set(channels.get(cid, {}).get("lastSeenVideoIds", []))
        new_entries = [e for e in entries if e["videoId"] not in seen]
        if new_entries:
            new_videos[cid] = {
                "name": channels.get(cid, {}).get("name", cid),
                "videos": new_entries,
            }

    output = {
        "hasNew": len(new_videos) > 0,
        "newVideos": new_videos,
        "checkedChannels": len(channels),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
