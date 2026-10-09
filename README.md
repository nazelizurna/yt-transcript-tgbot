# YouTube Transcript Telegram Bot

Send a YouTube link, get a `.docx` transcript back. English transcripts are automatically translated to Russian.

## How it works

1. Send a YouTube URL to the bot, optionally followed by a language keyword (`eng`, `fr`).
2. The bot fetches the transcript:
   - **Primary:** [Supadata YouTube Transcripts API](https://rapidapi.com/8v2FWW4H6AmKw89/api/youtube-transcripts) via RapidAPI
   - **Fallback:** `yt-dlp`
3. If the transcript is English, it is translated to Russian with an OpenAI-compatible LLM API (default: Google Gemini). Russian transcripts are left as is.
4. The transcript is written to a `.docx` (Times New Roman 14pt, justified, page numbers, video title on top) and sent to the chat.

Default language is Russian (`ru`). Overrides: `en`/`eng`/`english`, `fr`/`french`/`francais`/`français`.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Token from [@BotFather](https://t.me/BotFather) |
| `WEBHOOK_SECRET` | recommended | Secret path segment: `/webhook/<secret>` |
| `RAPIDAPI_KEY` | yes | RapidAPI key |
| `RAPIDAPI_HOST` | no | Default: `youtube-transcripts.p.rapidapi.com` |
| `LLM_API_KEY` | yes (for translation) | API key, e.g. from [Google AI Studio](https://aistudio.google.com/apikey) |
| `LLM_MODEL` | no | Default: `gemini-2.5-flash`. For new Gemini keys use e.g. `gemini-3.5-flash-lite` |
| `LLM_BASE_URL` | no | Default: `https://generativelanguage.googleapis.com/v1beta/openai` |
| `PORT` | no | Default: `8000` |

Other providers work via `LLM_BASE_URL` + `LLM_MODEL`, e.g. DeepSeek (`https://api.deepseek.com`, `deepseek-chat`) or Groq (`https://api.groq.com/openai/v1`, `llama-3.3-70b-versatile`).

## Setup

```bash
pip install -r requirements.txt
python main.py
```

Register the webhook (HTTPS required):

```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=https://<your-domain>/webhook/<WEBHOOK_SECRET>"
```

## Endpoints

- `POST /webhook/{secret}` – Telegram webhook (processing runs in the background)
- `GET /` – health check
- `GET /debug` – shows which env vars are set (no secret values)

## Deployment

Runs on [Render](https://render.com) or any host that serves a FastAPI app over HTTPS.

## Notes

- `yt-dlp` is often blocked from datacenter IPs (Render, AWS), so RapidAPI is the primary source.
- Transcripts are cached in memory per `video_id:lang`; the cache resets on restart.
- Duplicate Telegram updates are ignored.
- Free LLM tiers are rate limited; if translation fails, the original English `.docx` is sent.
