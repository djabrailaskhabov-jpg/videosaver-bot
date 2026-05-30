import asyncio
import json
import logging
import os
import random
import re
import subprocess
import tempfile
from pathlib import Path

from aiohttp import web

import instaloader
import requests
import yt_dlp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
INSTAGRAM_SESSION_ID = os.environ.get("INSTAGRAM_SESSION_ID", "")

bot = Bot(token=TOKEN)
dp = Dispatcher()

user_language: dict[int, str] = {}

# Stores pending YouTube downloads: user_id -> {"url": ..., "title": ..., "tmpdir": ...}
pending_yt: dict[int, dict] = {}

texts = {
    "ru": {
        "start": "👋 Привет! Выбери язык:",
        "send_link": "✅ Теперь отправь ссылку на фото, видео или Reels из Instagram, TikTok, YouTube или Twitter/X.",
        "downloading": "⏳ Скачиваю... Подожди немного",
        "fetching_info": "⏳ Получаю информацию о видео...",
        "error": "❌ Не удалось скачать. Ссылка может быть приватной или Instagram временно заблокировал запрос. Попробуй позже.",
        "yt_error": "❌ Не удалось получить информацию о видео. Проверь ссылку и попробуй снова.",
        "yt_download_error": "❌ Не удалось скачать видео. YouTube мог заблокировать запрос или видео слишком большое для этого качества. Попробуй качество ниже.",
        "too_large": "❌ Файл слишком большой (больше 50 МБ) и не может быть отправлен.",
        "not_supported": "❌ Поддерживаю только Instagram, TikTok, YouTube и Twitter/X.",
        "done": "✅ Готово!\n@VideoSaver95bot",
        "choose_quality": "🎬 Выбери качество:",
        "audio_only": "🎵 Только аудио (MP3)",
        "cancelled": "❌ Отменено.",
    },
    "en": {
        "start": "👋 Hello! Choose language:",
        "send_link": "✅ Send link to photo, video or Reels from Instagram, TikTok, YouTube or Twitter/X.",
        "downloading": "⏳ Downloading... Please wait",
        "fetching_info": "⏳ Fetching video info...",
        "error": "❌ Failed to download. The link may be private or Instagram is temporarily blocking the request. Try again later.",
        "yt_error": "❌ Could not get video info. Check the link and try again.",
        "yt_download_error": "❌ Could not download the video. YouTube may have blocked the request or the file is too large for this quality. Try a lower quality.",
        "too_large": "❌ The file is too large (over 50 MB) and cannot be sent.",
        "not_supported": "❌ Only Instagram, TikTok, YouTube and Twitter/X supported.",
        "done": "✅ Done!\n@VideoSaver95bot",
        "choose_quality": "🎬 Choose quality:",
        "audio_only": "🎵 Audio only (MP3)",
        "cancelled": "❌ Cancelled.",
    },
}

# Telegram Bot API hard limit — files larger than this will be rejected by Telegram itself.
# To send larger files, a local Telegram Bot API server is required.
TELEGRAM_API_LIMIT_BYTES = 50 * 1024 * 1024

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1",
]

COBALT_INSTANCES = [
    "https://cobalt.meowing.de/api/json",
    "https://cobalt.canine.tools/api/json",
]

QUALITY_OPTIONS = [360, 480, 720, 1080]

lang_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🇷🇺 Русский")],
        [KeyboardButton(text="🇬🇧 English")],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

MEDIA_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov"}


def get_text(user_id: int, key: str) -> str:
    lang = user_language.get(user_id, "ru")
    return texts[lang][key]


def detect_media_type(path: str) -> str:
    return "video" if Path(path).suffix.lower() in VIDEO_EXTENSIONS else "photo"


def collect_media_files(tmpdir: str) -> list[dict]:
    files = []
    for f in sorted(Path(tmpdir).iterdir()):
        if f.is_file() and f.suffix.lower() in MEDIA_EXTENSIONS and f.stat().st_size > 1024:
            files.append({"path": str(f), "type": detect_media_type(str(f))})
    return files


def is_youtube(url: str) -> bool:
    return any(d in url.lower() for d in ["youtube.com", "youtu.be"])


def is_twitter(url: str) -> bool:
    return any(d in url.lower() for d in ["twitter.com", "x.com", "t.co"])


def format_duration(seconds: int | None) -> str:
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_views(n: int | None) -> str:
    if not n:
        return ""
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)


# ── YouTube info + quality keyboard ──────────────────────────────────────────

async def fetch_youtube_info(url: str) -> dict | None:
    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}

    def _run():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        logger.warning("fetch_youtube_info failed: %s", e)
        return None


def get_available_qualities(info: dict) -> list[int]:
    """Return sorted list of available video heights."""
    heights = set()
    for fmt in info.get("formats", []):
        h = fmt.get("height")
        if h and fmt.get("vcodec", "none") != "none":
            # Round to nearest standard quality
            for q in QUALITY_OPTIONS:
                if abs(h - q) <= 50:
                    heights.add(q)
    return sorted(heights) if heights else [720]


def build_quality_keyboard(user_id: int, available: list[int]) -> InlineKeyboardMarkup:
    lang = user_language.get(user_id, "ru")
    buttons = []
    row = []
    for q in available:
        row.append(InlineKeyboardButton(text=f"📹 {q}p", callback_data=f"yt_quality:{q}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(
        text=texts[lang]["audio_only"],
        callback_data="yt_quality:audio"
    )])
    buttons.append([InlineKeyboardButton(
        text="❌ " + ("Отмена" if lang == "ru" else "Cancel"),
        callback_data="yt_quality:cancel"
    )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ── Downloader: yt-dlp with quality ──────────────────────────────────────────

async def download_ydl(url: str, tmpdir: str, quality: int | str = 720) -> tuple[list[dict] | None, str]:
    headers: dict = {"User-Agent": random.choice(USER_AGENTS)}

    if INSTAGRAM_SESSION_ID and "instagram.com" in url.lower():
        headers["Cookie"] = f"sessionid={INSTAGRAM_SESSION_ID}"
        headers["Referer"] = "https://www.instagram.com/"

    if quality == "audio":
        fmt = "bestaudio/best"
        postprocessors = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
        outtmpl = os.path.join(tmpdir, "%(title).60s.%(ext)s")
    else:
        h = int(quality)
        fmt = (
            f"bestvideo[ext=mp4][height<={h}]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={h}]+bestaudio"
            f"/best[height<={h}]/best"
        )
        postprocessors = [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}]
        outtmpl = os.path.join(tmpdir, "%(id)s.%(ext)s")

    audio_exts = {".mp3", ".m4a", ".ogg", ".opus", ".aac", ".flac", ".wav"}
    is_audio_only = quality == "audio"

    ydl_opts = {
        "outtmpl": outtmpl,
        "format": fmt,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "http_headers": headers,
        "postprocessors": postprocessors,
    }
    if not is_audio_only:
        ydl_opts["merge_output_format"] = "mp4"

    def _run():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                return None, ""
            title = info.get("title", "")

            if is_audio_only:
                # Collect audio files only
                files = []
                for f in sorted(Path(tmpdir).iterdir()):
                    ext = f.suffix.lower()
                    if f.is_file() and f.stat().st_size > 1024 and ext in audio_exts:
                        files.append({"path": str(f), "type": "audio"})
                return files or None, title
            else:
                # Only return the final merged .mp4 — ignore raw intermediate streams
                # Try prepared filename first
                expected = ydl.prepare_filename(info)
                if not expected.endswith(".mp4"):
                    expected = os.path.splitext(expected)[0] + ".mp4"
                if os.path.exists(expected) and os.path.getsize(expected) > 1024:
                    return [{"path": expected, "type": "video"}], title
                # Fallback: pick the largest .mp4 in tmpdir
                mp4s = [f for f in Path(tmpdir).iterdir()
                        if f.suffix.lower() == ".mp4" and f.stat().st_size > 1024]
                if mp4s:
                    best = max(mp4s, key=lambda f: f.stat().st_size)
                    return [{"path": str(best), "type": "video"}], title
                return None, title

    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        logger.warning("yt-dlp failed: %s", e)
        return None, ""


# ── Instagram downloaders ─────────────────────────────────────────────────────

async def download_gallery_dl(url: str, tmpdir: str) -> tuple[list[dict] | None, str]:
    if not INSTAGRAM_SESSION_ID:
        return None, ""

    def _run():
        config = {
            "extractor": {
                "instagram": {
                    "cookies": {"sessionid": INSTAGRAM_SESSION_ID},
                    "videos": True,
                }
            }
        }
        config_path = os.path.join(tmpdir, "gdl_config.json")
        with open(config_path, "w") as f:
            json.dump(config, f)

        cmd = ["gallery-dl", "--config", config_path, "--destination", tmpdir, "--no-part", "--quiet", url]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            logger.warning("gallery-dl stderr: %s", result.stderr[:300])

        return collect_media_files(tmpdir) or None, ""

    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        logger.warning("gallery-dl failed: %s", e)
        return None, ""


async def download_instaloader(url: str, tmpdir: str) -> tuple[list[dict] | None, str]:
    match = re.search(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_\-]+)", url)
    if not match:
        return None, ""
    shortcode = match.group(1)

    def _run():
        loader = instaloader.Instaloader(
            dirname_pattern=tmpdir,
            filename_pattern="{shortcode}_{mediaid}",
            download_videos=True,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            post_metadata_txt_pattern="",
            quiet=True,
        )
        if INSTAGRAM_SESSION_ID:
            loader.context._session.cookies.set("sessionid", INSTAGRAM_SESSION_ID, domain=".instagram.com")
            loader.context._session.headers.update({"User-Agent": random.choice(USER_AGENTS)})
        try:
            post = instaloader.Post.from_shortcode(loader.context, shortcode)
            loader.download_post(post, target=tmpdir)
        except Exception as e:
            logger.warning("instaloader error: %s", e)
            return None, ""
        return collect_media_files(tmpdir) or None, ""

    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        logger.warning("instaloader failed: %s", e)
        return None, ""


async def download_cobalt(url: str, tmpdir: str) -> tuple[list[dict] | None, str]:
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    payload = {"url": url, "videoQuality": "1080"}

    def _run():
        for api_url in COBALT_INSTANCES:
            try:
                resp = requests.post(api_url, headers=headers, json=payload, timeout=15)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                status = data.get("status")
                if status in ("stream", "redirect"):
                    ext = "mp4" if status == "stream" else "jpg"
                    fp = os.path.join(tmpdir, f"cobalt_0.{ext}")
                    r = requests.get(data["url"], stream=True, timeout=60)
                    with open(fp, "wb") as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)
                    return [{"path": fp, "type": detect_media_type(fp)}], ""
                elif status == "picker":
                    files = []
                    for i, item in enumerate(data.get("picker", [])[:10]):
                        fp = os.path.join(tmpdir, f"cobalt_{i}.jpg")
                        r = requests.get(item["url"], stream=True, timeout=60)
                        with open(fp, "wb") as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                f.write(chunk)
                        files.append({"path": fp, "type": "photo"})
                    if files:
                        return files, ""
            except Exception as e:
                logger.warning("Cobalt %s failed: %s", api_url, e)
        return None, ""

    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        logger.warning("Cobalt error: %s", e)
        return None, ""


# ── Instagram parallel race ───────────────────────────────────────────────────

async def _guarded(coro):
    try:
        return await coro
    except (asyncio.CancelledError, Exception) as e:
        if not isinstance(e, asyncio.CancelledError):
            logger.warning("Downloader raised: %s", e)
        return None, ""


async def race_instagram(url: str, base_tmpdir: str) -> tuple[list[dict] | None, str]:
    dirs = [os.path.join(base_tmpdir, f"d{i}") for i in range(4)]
    for d in dirs:
        os.makedirs(d, exist_ok=True)

    tasks = [
        asyncio.create_task(_guarded(download_ydl(url, dirs[0]))),
        asyncio.create_task(_guarded(download_gallery_dl(url, dirs[1]))),
        asyncio.create_task(_guarded(download_instaloader(url, dirs[2]))),
        asyncio.create_task(_guarded(download_cobalt(url, dirs[3]))),
    ]

    # Overall timeout: cancel everything after 40 seconds
    deadline = asyncio.get_event_loop().time() + 40
    pending = set(tasks)
    while pending:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        done, pending = await asyncio.wait(pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            try:
                files, title = task.result()
            except Exception:
                continue
            if files:
                for t in pending:
                    t.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                return files, title

    # Cancel remaining tasks
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return None, ""


# ── Send helpers ──────────────────────────────────────────────────────────────

async def send_media(message: types.Message, media_files: list[dict], caption: str) -> None:
    audio_exts = {".mp3", ".m4a", ".ogg", ".opus", ".aac", ".flac", ".wav"}
    user_id = message.from_user.id

    async def _send_one(m: dict) -> None:
        size = os.path.getsize(m["path"])
        if size > TELEGRAM_API_LIMIT_BYTES:
            size_mb = size / 1024 / 1024
            lang = user_language.get(user_id, "ru")
            if lang == "ru":
                await message.answer(f"❌ Видео весит {size_mb:.0f} МБ — Telegram не принимает файлы крупнее 50 МБ через бота. Попробуй качество ниже.")
            else:
                await message.answer(f"❌ Video is {size_mb:.0f} MB — Telegram bots cannot send files over 50 MB. Try a lower quality.")
            return
        file = FSInputFile(m["path"])
        ext = Path(m["path"]).suffix.lower()
        if ext in audio_exts or m["type"] == "audio":
            await message.answer_audio(audio=file, caption=caption)
        elif m["type"] == "video":
            await message.answer_video(video=file, caption=caption, supports_streaming=True)
        else:
            await message.answer_photo(photo=file, caption=caption)

    if len(media_files) == 1:
        await _send_one(media_files[0])
    else:
        # Filter oversized files from albums and warn if any dropped
        valid = [m for m in media_files if os.path.getsize(m["path"]) <= TELEGRAM_API_LIMIT_BYTES]
        dropped = len(media_files) - len(valid)
        if not valid:
            lang = user_language.get(user_id, "ru")
            if lang == "ru":
                await message.answer("❌ Все файлы превышают 50 МБ — Telegram не принимает их через бота.")
            else:
                await message.answer("❌ All files exceed 50 MB — Telegram bots cannot send them.")
            return
        album = []
        for i, m in enumerate(valid[:10]):
            file = FSInputFile(m["path"])
            item_caption = caption if i == 0 else None
            if m["type"] == "video":
                album.append(InputMediaVideo(media=file, caption=item_caption, supports_streaming=True))
            else:
                album.append(InputMediaPhoto(media=file, caption=item_caption))
        await message.answer_media_group(album)
        if dropped:
            lang = user_language.get(user_id, "ru")
            if lang == "ru":
                await message.answer(f"⚠️ {dropped} файл(ов) пропущено — больше 50 МБ.")
            else:
                await message.answer(f"⚠️ {dropped} file(s) skipped — over 50 MB.")


# ── Bot handlers ──────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def start(message: types.Message) -> None:
    await message.answer(get_text(message.from_user.id, "start"), reply_markup=lang_keyboard)


@dp.message(F.text.in_(["🇷🇺 Русский", "🇬🇧 English"]))
async def set_language(message: types.Message) -> None:
    user_id = message.from_user.id
    if "Русский" in message.text:
        user_language[user_id] = "ru"
        await message.answer("✅ Язык установлен: Русский", reply_markup=types.ReplyKeyboardRemove())
    else:
        user_language[user_id] = "en"
        await message.answer("✅ Language set: English", reply_markup=types.ReplyKeyboardRemove())
    await message.answer(get_text(user_id, "send_link"))


@dp.message(F.text)
async def handle_message(message: types.Message) -> None:
    url = message.text.strip()
    user_id = message.from_user.id

    SUPPORTED = ["instagram.com", "tiktok.com", "youtube.com", "youtu.be", "twitter.com", "x.com", "t.co"]
    if not any(d in url.lower() for d in SUPPORTED):
        if url.startswith("http"):
            await message.answer(get_text(user_id, "not_supported"))
        return

    if is_youtube(url):
        # Show quality picker
        info_msg = await message.answer(get_text(user_id, "fetching_info"))
        info = await fetch_youtube_info(url)
        if not info:
            await info_msg.edit_text(get_text(user_id, "yt_error"))
            return

        title = info.get("title", "")
        duration = format_duration(info.get("duration"))
        views = format_views(info.get("view_count"))
        channel = info.get("uploader") or info.get("channel") or ""

        # Build info caption
        lang = user_language.get(user_id, "ru")
        meta_parts = []
        if channel:
            meta_parts.append(f"👤 {channel}")
        if duration:
            meta_parts.append(f"⏱ {duration}")
        if views:
            meta_parts.append(f"👁 {views}")
        meta = "  •  ".join(meta_parts)

        caption = f"🎬 <b>{title}</b>"
        if meta:
            caption += f"\n{meta}"
        caption += f"\n\n{get_text(user_id, 'choose_quality')}"

        available = get_available_qualities(info)
        keyboard = build_quality_keyboard(user_id, available)

        # Store pending download
        pending_yt[user_id] = {"url": url, "title": title}

        await info_msg.edit_text(caption, reply_markup=keyboard, parse_mode="HTML")
        return

    # Instagram / TikTok / Twitter — direct download
    wait_msg = await message.answer(get_text(user_id, "downloading"))
    is_instagram = "instagram.com" in url.lower()

    with tempfile.TemporaryDirectory() as tmpdir:
        if is_instagram:
            media_files, title = await race_instagram(url, tmpdir)
        else:
            media_files, title = await download_ydl(url, tmpdir)

        if not media_files:
            await wait_msg.edit_text(get_text(user_id, "error"))
            return

        caption = get_text(user_id, "done")
        try:
            await wait_msg.delete()
            await send_media(message, media_files, caption)
        except Exception:
            logger.exception("Failed to send media for %s", url)
            await message.answer(get_text(user_id, "error"))


@dp.callback_query(F.data.startswith("yt_quality:"))
async def handle_quality_callback(callback: types.CallbackQuery) -> None:
    user_id = callback.from_user.id
    choice = callback.data.split(":", 1)[1]

    await callback.answer()

    if choice == "cancel":
        pending_yt.pop(user_id, None)
        await callback.message.edit_text(get_text(user_id, "cancelled"))
        return

    pending = pending_yt.pop(user_id, None)
    if not pending:
        await callback.message.edit_text(get_text(user_id, "yt_error"))
        return

    url = pending["url"]
    title = pending["title"]
    quality: int | str = int(choice) if choice != "audio" else "audio"

    lang = user_language.get(user_id, "ru")
    label = f"🎵 MP3" if quality == "audio" else f"📹 {quality}p"
    await callback.message.edit_text(
        f"🎬 <b>{title}</b>\n{label}\n\n{'⏳ Скачиваю...' if lang == 'ru' else '⏳ Downloading...'}",
        parse_mode="HTML",
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        media_files, _ = await download_ydl(url, tmpdir, quality=quality)

        if not media_files:
            await callback.message.edit_text(get_text(user_id, "yt_download_error"))
            return

        caption = f"✅ {title[:200]}\n@VideoSaver95bot"
        try:
            await callback.message.delete()
            await send_media(callback.message, media_files, caption)
        except Exception:
            logger.exception("Failed to send YouTube media")
            await callback.message.edit_text(get_text(user_id, "yt_download_error"))


async def health_handler(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def run_web_server() -> None:
    port = int(os.environ.get("HEALTH_PORT", 5000))
    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Keep-alive web server running on port %d", port)


async def main() -> None:
    logger.info("Starting bot...")
    await asyncio.gather(
        run_web_server(),
        dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
