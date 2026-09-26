# Mielen Voima — Video Generation Engine (Render Web Service)

Production-grade automated video generation microservice for the **Mielen Voima** (@Mielen_Voima) Finnish YouTube Shorts channel. Built with FastAPI, FFmpeg, Cloudflare Workers AI (Flux-1-schnell), Edge TTS, and Groq Whisper.

---

## 🚀 Features

- **100% $0/month Architecture:** Engineered specifically for the Render Free Web Service (512MB RAM).
- **Asynchronous Execution:** `POST /api/generate-short` responds in `<100ms` with HTTP 202 Accepted and a `job_id`, preventing network timeouts.
- **Webhook Callback:** Automatically calls back to n8n when the video render finishes.
- **Fail-safe Cloudflare Accounts:** Automatic fallback to secondary/tertiary accounts on HTTP 429 or quota exhaustion.
- **Smooth 4K Ken Burns:** Pre-upscaled subpixel zoompan eliminating FFmpeg integer-rounding stutter.
- **Word-Synced Captions:** Groq Whisper large-v3 Finnish alignment with Amber Gold (`#F59E0B`) active word pop-in.
- **RAM Bounded:** Strictly pinned to single-threaded FFmpeg processing (`-threads 1`) to stay under 300MB RAM.

---

## 🛠️ Environment Variables on Render

In your Render Dashboard (**Dashboard -> Your Web Service -> Environment**), add these variables:

| Variable | Value | Description |
| :--- | :--- | :--- |
| `CF_ACCOUNT_ID_1` | `your_cf_account_id_1` | Primary Cloudflare Account ID |
| `CF_API_TOKEN_1` | `your_cf_api_token_1` | Primary Cloudflare API Token |
| `CF_ACCOUNT_ID_2` | `your_cf_account_id_2` | Fallback Account ID 1 |
| `CF_API_TOKEN_2` | `your_cf_api_token_2` | Fallback Account Token 1 |
| `CF_ACCOUNT_ID_3` | `your_cf_account_id_3` | Fallback Account ID 2 |
| `CF_API_TOKEN_3` | `your_cf_api_token_3` | Fallback Account Token 2 |
| `GROQ_API_KEY` | `your_groq_api_key_here` | Groq Whisper Subtitles Key |
| `API_SECRET_KEY` | `your_secret_api_key_here` | Required security header token for API auth |

---

## 📦 How to Push to GitHub & Deploy to Render

### 1. Initialize Git & Push (Local Terminal)
Inside this folder (`e:\Antigravity\twitter automation\mielen-voima-render`):

```bash
git init
git add .
git commit -m "feat: initial mielen voima render video engine"
git branch -M main
git remote add origin https://github.com/<YOUR_USERNAME>/mielen-voima-video-engine.git
git push -u origin main
```

### 2. Deploy on Render
1. Go to [https://dashboard.render.com/](https://dashboard.render.com/)
2. Click **New +** -> **Web Service**
3. Connect your GitHub repository `mielen-voima-video-engine`
4. Render will automatically detect the **Dockerfile**
5. Select **Free** instance type ($0/mo, 512MB RAM)
6. Choose region: **Frankfurt (EU)**
7. Under **Advanced** -> **Health Check Path**, set: `/health`
8. Add the Environment Variables listed above
9. Click **Create Web Service**!

---

## 📡 API Endpoints

### 1. `GET /health`
Returns 200 OK. Used by n8n to ping every 13 minutes and keep the service awake.

### 2. `POST /api/generate-short`
Dispatches video rendering.
**Payload:**
```json
{
  "topic": "Miksi aivosi kaipaavat kaaosta?",
  "video_id": "short_001",
  "webhook_url": "https://moazzamn8n.dpdns.org/webhook/video-ready",
  "scenes": [
    {
      "scene_id": 1,
      "voiceover": "Aivosi eivät etsi rauhaa — ne ovat koukussa kaaokseen.",
      "image_prompt": "Minimalist flat 2D vector stickman, extreme 9:16 vertical orientation, pure #FFFFFF white background, high-contrast black line art, Close-Up Shot. Stickman circular head with Amber Gold (#F59E0B) glowing brain on head. Text overlay at top: 'KAAOS'. Flat 2D vector graphic only.",
      "keyword": "KAAOS"
    }
  ]
}
```

**Response (HTTP 202):**
```json
{
  "status": "queued",
  "job_id": "mv_1790360000_a1b2c3",
  "message": "Video rendering started. Monitor via /api/status/{job_id} or wait for webhook callback."
}
```

### 3. `GET /api/status/{job_id}`
Returns real-time rendering status (`queued`, `generating_media`, `assembling_video`, `completed`, `failed`).

### 4. `GET /api/download/{job_id}`
Directly downloads the finished 1080x1920 MP4 file.
