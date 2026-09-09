# YouTube Transcript Telegram Bot

Send the bot a YouTube link, get back a `.docx` file with the video's transcript.

## How it works

1. User sends a YouTube URL to the bot (optionally followed by a language keyword, e.g. `eng`, `fr`).
2. Bot extracts the video ID and fetches the transcript:
   - **Primary:** [Supadata's YouTube Transcripts API](https://rapidapi.com/ via [RapidAPI](https://rapidapi.com/8v2FWW4H6AmKw89/api/youtube-transcripts).
   - **Fallback:** `yt-dlp`, if the RapidAPI call fails or returns nothing.
3. Transcript is written into a Word document (Times New Roman 14pt, justified body text, page numbers, video title as the first line) and sent back to the chat.
4. Temp files are deleted after sending.

Default transcript language is Russian (`ru`). Supported overrides: `en`/`eng`/`english`, `fr`/`french`/`francais`/`français`.

## Requirements

- Python 3.10+
- A Telegram bot token ([@BotFather](https://t.me/BotFather))
- A RapidAPI key subscribed to the YouTube Transcripts endpoint

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Bot token from BotFather |
| `WEBHOOK_SECRET` | recommended | Secret path segment for the webhook URL, e.g. `/webhook/<secret>` |
| `RAPIDAPI_KEY` | yes (for primary source) | RapidAPI key |
| `RAPIDAPI_HOST` | no | Defaults to `youtube-transcripts.p.rapidapi.com` |
| `PORT` | no | Defaults to `8000` |

## Setup

```bash
pip install -r requirements.txt
```

Set the environment variables above, then run:

```bash
python main.py
```

Register the webhook with Telegram:

```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=https://<your-domain>/webhook/<WEBHOOK_SECRET>"
```

## Endpoints

- `POST /webhook/{secret}` — Telegram webhook handler
- `GET /` — health check
- `GET /debug` — reports whether required env vars are set (no secret values exposed)

## Deployment

Built to run on [Render](https://render.com) or any host that can run a FastAPI app behind HTTPS (Telegram webhooks require HTTPS).

## Notes

- `yt-dlp` calls can get blocked by YouTube from datacenter IPs (Render, AWS, etc.) — this is why the RapidAPI source is primary and `yt-dlp` is just the fallback.
- Transcript results are cached in memory per `video_id:lang`; the cache resets on restart.

<img width="2088" height="2384" alt="image" src="https://github.com/user-attachments/assets/f969164b-7d17-4c67-b4d9-1ec91776b985" />
