# Telegram Video Downloader Bot

A Telegram bot that downloads videos from Instagram Reels, TikTok, and YouTube and sends them back to the user.

## Run & Operate

- `python bot/bot.py` — run the Telegram bot (managed via "Telegram Bot" workflow)
- Required secret: `TELEGRAM_BOT_TOKEN` — from @BotFather on Telegram

## Stack

- Python 3.11
- aiogram 3.7 — Telegram bot framework
- yt-dlp — video downloading (Instagram, TikTok, YouTube)
- ffmpeg — video post-processing/conversion

## Where things live

- `bot/bot.py` — main bot entrypoint (handlers + download logic)

## Architecture decisions

- Downloads happen in a thread pool executor (`run_in_executor`) so async event loop is never blocked
- Videos are downloaded to a `tempfile.TemporaryDirectory` and deleted automatically after sending
- File size is capped at 50 MB (Telegram bot API limit)
- Videos are re-encoded to MP4 via yt-dlp's FFmpegVideoConvertor postprocessor for compatibility

## Product

Send any Instagram Reel, TikTok, or YouTube (including Shorts) link to the bot and it will download and deliver the video as a Telegram video message.

## User preferences

_Populate as you build — explicit user instructions worth remembering across sessions._

## Gotchas

- yt-dlp format selection prefers 720p MP4 + M4A audio merged into MP4; falls back gracefully to best available
- Private/age-restricted videos return a user-friendly error message rather than crashing
- Max file size enforced both at yt-dlp level (`max_filesize`) and before upload

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
