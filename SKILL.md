# YT Tracker Skill — April 🐦

YouTube 頻道監控與分析推送。

## 🎯 Gemini 影片分析（優先使用）

所有影片分析**優先使用 Gemini 2.5 Flash**，直接給 YouTube URL，免下載、免轉錄：

```bash
python3 /home/node/.openclaw/workspace/projects/yt-tracker/gemini-analyze.py \
  --url "https://youtube.com/watch?v={videoId}" --mode hourly --json
```

- `--mode hourly`：簡潔摘要（~200 字）
- `--mode daily`：深度批判性分析（300-500 字，含立場偏差、邏輯漏洞、缺失觀點）

**Fallback 順序：**
1. ✅ Gemini 2.5 Flash（免下載，直接分析影片）
2. 🔄 yt-dlp 字幕 + Groq Whisper（Gemini 失敗時）
3. 🔄 YouTube Data API 描述文字（都失敗時）

若 Gemini 回傳錯誤（429 rate limit / API key 問題），才回退到舊的 yt-dlp + Whisper 流程。

## 🔍 Search Fallback

If `web_search` is unavailable or returns errors (quota exhausted, rate limited):

```bash
python3 ~/.openclaw/workspace/skills/smart-search/scripts/smart_search.py \
  --query "your query" --type news --freshness day --limit 10 --json
```

Fallback chain: Brave API → SearXNG (local) → DuckDuckGo. Use `--type news` for current events, `--type text` for general search.

## 模式說明

| 模式 | 觸發 | 分析深度 | 目標 |
|------|------|----------|------|
| `hourly` | 每 2h | 中度（web_search + 字幕嘗試；若失敗改用 smart_search.py）| 即時有料通知 |
| `daily` | 每日 01:00 JST | 完整深度分析 | 深度研究報告 |

---

## Hourly 模式

> ⚡ 目標：偵測新影片並立即發出**有實質內容**的通知。每部影片必做 web_search，有字幕則補充。

### Step 0 — 環境預檢（每次必做）

確保 yt-dlp 已安裝，若缺失則自動安裝：

```bash
which yt-dlp || curl -sL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
  -o /usr/local/bin/yt-dlp && chmod +x /usr/local/bin/yt-dlp
```

### Step 1 — 預檢

```bash
cd /home/node/.openclaw/workspace/projects/yt-tracker
python3 yt-check-new.py --frequency hourly
```

- `hasNew: false` → 回覆 **HEARTBEAT_OK**，結束
- `hasNew: true` → 繼續

### Step 2 — 快速分析 + 推送

對每部新影片：

**① 會員限定早期偵測**：若 yt-dlp 輸出含 `members-only` 或 `This video is available to this channel's members` → 立即標記為已見（加入 `lastSeenVideoIds`，**不加入** `lastNotifiedAt`），**跳過所有後續步驟，不發通知**。不需嘗試 Groq 或 YouTube API。

> 📝 **會員限定影片的特殊處理**：
> - 加入 `membersOnlyIds`（避免每次 RSS 掃到都重複檢查）
> - 加入 `lastSeenVideoIds`（去重紀錄）
> - **不加入** `lastNotifiedAt`（保留開放後被偵測的可能）
>
> `yt-check-new.py` 會自動跳過 `membersOnlyIds` 中的影片。**自動畢業機制**：每次 RSS 掃描時，若 `membersOnlyIds` 中的影片出現在 RSS 且不再有 `membersOnly` 標記，自動從 `membersOnlyIds` 移除，並在本次 run 中當作新影片處理推送。

**② Gemini 影片分析**（優先，免下載）：

```bash
python3 /home/node/.openclaw/workspace/projects/yt-tracker/gemini-analyze.py \
  --url "https://youtube.com/watch?v={videoId}" --mode hourly --json
```

解析 JSON 回傳的 `analysis` 欄位。若 `success: true` → 直接用 Gemini 分析結果，跳到 ③。

**Gemini 失敗時的 Fallback**（依序嘗試）：

a. yt-dlp 字幕：
```bash
timeout 30 yt-dlp --write-auto-sub --sub-lang zh-Hant,zh-Hans,en --skip-download \
  --output "/tmp/%(id)s" "https://www.youtube.com/watch?v={videoId}" 2>&1
```

b. Groq Whisper 轉錄：
```bash
GROQ_KEY=$(python3 -c "import json; print(json.load(open('/home/node/.openclaw/agents/bird/agent/secrets/groq.json'))['GROQ_API_KEY'])")
timeout 60 yt-dlp -f "bestaudio[filesize<20M]" \
  --output "/tmp/%(id)s.%(ext)s" "https://www.youtube.com/watch?v={videoId}" 2>&1
curl -s -X POST https://api.groq.com/openai/v1/audio/transcriptions \
  -H "Authorization: Bearer $GROQ_KEY" \
  -F "file=@/tmp/{videoId}.webm" \
  -F "model=whisper-large-v3-turbo" \
  -F "language=zh" \
  -F "response_format=text"
```

c. YouTube Data API 描述文字（最後手段）

> 若全部失敗 → 略過影片，更新 state 標記已見，不發通知。

**② web_search 補充**（Gemini 成功時可選做，失敗 fallback 時必做）：搜尋 `{影片標題} {頻道名}` 取得補充背景（若 web_search 失敗，改用 smart_search.py —— 見上方 Search Fallback）

**③ 整合摘要**：使用 Gemini 分析結果（或 fallback 的轉錄 + web_search），寫出有實質內容的摘要

**發送格式：**
```
📺 [頻道名]
🎬 [標題]
🔗 https://youtube.com/watch?v=[videoId]

👤 [講者/來源背景一句話]
📌 [核心主題 2-3 行，說明為什麼這支影片值得看]
💡 [一個關鍵洞察或數據點]
```

發送方式：`message` tool
- channel: telegram
- accountId: bird
- target: `-1003767828002`
- threadId: `36`

### Step 3 — 更新狀態

更新 `yt-tracker-state.json`：
- 把 videoId 加入 `lastNotifiedAt`（timestamp: 現在 UTC ISO 時間）
- 把 videoId 加入 `lastSeenVideoIds`

---

## Daily 模式（完整分析）

> 🔬 目標：深度分析每部影片，提供投資/科技洞察

### Step 0 — 環境預檢（每次必做）

```bash
which yt-dlp || curl -sL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
  -o /usr/local/bin/yt-dlp && chmod +x /usr/local/bin/yt-dlp
```

### Step 1 — 預檢

```bash
cd /home/node/.openclaw/workspace/projects/yt-tracker
python3 yt-check-new.py --frequency daily
```

- `hasNew: false` → 回覆 **HEARTBEAT_OK**，結束
- `hasNew: true` → 繼續 Step 2

### Step 2 — 預載字幕/轉錄

```bash
cd /home/node/.openclaw/workspace/projects/yt-tracker
GROQ_KEY=$(python3 -c "import json; print(json.load(open('/home/node/.openclaw/agents/bird/agent/secrets/groq.json'))['GROQ_API_KEY'])")
PATH="$HOME/.deno/bin:$PATH" GROQ_API_KEY="$GROQ_KEY" python3 yt-preload.py -o /tmp/yt-preload-daily
```

讀取 `/tmp/yt-preload-daily/summary.json` 取得影片清單。

### Step 3 — 取得字幕/轉錄

優先順序：
1. **yt-dlp 自動字幕**（最快，免費）
2. **preload 的 Groq Whisper 轉錄**（無字幕時）
3. **影片描述 fallback**

```bash
cd /tmp
yt-dlp --write-auto-sub --sub-lang zh-Hant,zh-Hans,en --skip-download \
  --output "%(id)s" "https://www.youtube.com/watch?v={videoId}" 2>/dev/null

# 清理 VTT 取得純文字
python3 -c "
import re, sys
with open('{videoId}.en.vtt') as f: text = f.read()
lines, prev = [], ''
for line in text.split('\n'):
    if '-->' in line or line.strip().isdigit() or line.startswith('WEBVTT'): continue
    line = re.sub(r'<[^>]+>', '', line).strip()
    if line and line != prev: lines.append(line); prev = line
print(' '.join(lines))
" > {videoId}-transcript.txt
```

若 yt-dlp 失敗，改讀 `/tmp/yt-preload-daily/{videoId}.txt`。

### Step 4 — 分析每部影片

對每部影片：

1. **Gemini 深度分析**（優先）：
```bash
python3 /home/node/.openclaw/workspace/projects/yt-tracker/gemini-analyze.py \
  --url "https://youtube.com/watch?v={videoId}" --mode daily --json
```
Gemini daily 模式會自動產出批判性分析（立場偏差、邏輯漏洞、缺失觀點等）。

若 Gemini 失敗 → fallback 讀取 Step 3 的 transcript 自行分析。

2. **外部驗證**：跑 **2-3 次** `web_search`（若 web_search 失敗，改用 smart_search.py —— 見上方 Search Fallback）
   - 搜尋影片標題 + 講者名 → 確認影片背景
   - 搜尋影片中提到的關鍵事件/數據 → 交叉驗證
3. **綜合撰寫**（300-500 字元），融合 Gemini 分析 + 外部驗證：
   - 📌 核心論點
   - 📊 關鍵數據（附外部驗證來源）
   - 🔍 批判性觀點（立場偏差、邏輯漏洞、缺失觀點）
   - 💡 投資/科技啟示
   - ⚖️ 評價（1-5 ⭐）

### Step 5 — 推送 Telegram

格式：
```
📺 [頻道名] 每日分析
🎬 [標題]
🔗 https://youtube.com/watch?v=[videoId]

[分析內容]
```

發送方式：`message` tool
- channel: telegram
- accountId: bird
- target: `-1003767828002`
- threadId: `36`

### Step 6 — 更新狀態

更新 `yt-tracker-state.json`：
- 把 videoId 加入 `lastNotifiedAt`（timestamp: 現在 UTC ISO 時間）
- 把 videoId 加入 `lastSeenVideoIds`

---

## Step 7 — 存檔到 Obsidian + Push GitHub（Daily 模式必做）

每部影片分析完後，存成獨立 Obsidian note 並 push GitHub。

### Note 路徑
`/home/node/obsidian-vault/Projects/YT-Tracker/YYYY-MM-DD-{videoId}.md`

### Note 格式
```markdown
# {影片標題}

> **狀態**：✅ 已分析
> **日期**：YYYY-MM-DD
> **頻道**：{頻道名}
> **影片 ID**：{videoId}
> **URL**：https://youtube.com/watch?v={videoId}
> **字幕來源**：subtitles / whisper / description_fallback
> **評分**：⭐（1-5）

## 摘要
（一兩句話）

## 📌 核心論點
## 📊 關鍵數據
## 💡 核心洞察
## ⚖️ 評價

## Tags
#相關 #標籤
```

### 更新索引
在 `/home/node/obsidian-vault/Projects/YT-Tracker.md` 的「影片分析索引」區段加入新條目：
```
- [[YYYY-MM-DD-{videoId}]] — {頻道}：{標題摘要} ⭐
```

### Git Push
```bash
cd /home/node/obsidian-vault
git add -A
git commit -m "feat(yt-tracker): add video analysis {videoId} ({YYYY-MM-DD})"
git push
```

---

## 注意事項

- `lastNotifiedAt` 是去重的唯一依據，**不要依賴 `lastSeenVideoIds`**
- hourly 通知過的影片，daily 會因 `lastNotifiedAt` 已有記錄而**跳過**
  → 如需 daily 也分析，可設計 hourly 只加 `lastSeenVideoIds` 而不加 `lastNotifiedAt`（目前設計是通知即記錄）
- State file 位置：`/home/node/.openclaw/workspace/projects/yt-tracker/yt-tracker-state.json`
- Obsidian vault：`/home/node/obsidian-vault`（GitHub: openyhclaw-dot/obsidian-vault）
