#!/usr/bin/env python3
"""
================================================================================
 SAAVN INLINE MUSIC BOT — FastTrack Advanced Edition
 Single-file. Railway-ready. No commands. Pure inline search-and-send.
 Bot Owner / Developer: @stillrahul
================================================================================

USAGE (Telegram, in ANY chat — DM, group, channel comments):

    @your_bot_username song name

...a live dropdown of matching songs appears. Tap one → the MP3 is sent
right there, instantly, credited to the bot owner.

--------------------------------------------------------------------------------
RUNNING LOCALLY
--------------------------------------------------------------------------------
    pip install -r requirements.txt        (or: pip install httpx python-telegram-bot)
    python3 saavn_bot.py

If BOT_TOKEN is not set as an environment variable, you'll be asked for it
in the terminal (hidden input) and it'll be cached to .saavn_bot_token so
you don't have to retype it locally.

--------------------------------------------------------------------------------
DEPLOYING ON RAILWAY
--------------------------------------------------------------------------------
Railway has no interactive terminal at boot, so on Railway you MUST set the
token as an environment variable — the terminal prompt is automatically
skipped whenever BOT_TOKEN is present.

    1. Push this file (+ requirements.txt) to a GitHub repo.
    2. On railway.app -> New Project -> Deploy from GitHub repo.
    3. In the service's "Variables" tab, add:
           BOT_TOKEN = <your token from @BotFather>
       Optional variables (all have sensible defaults, see CONFIG below):
           SAAVN_API_BASE, PAGE_SIZE, CACHE_TTL_SECONDS,
           RATE_LIMIT_WINDOW, RATE_LIMIT_MAX_CALLS, LOG_LEVEL,
           OWNER_USERNAME, OWNER_TAG_IN_CAPTION
    4. Railway auto-detects Python and runs this file (or set a custom
       Start Command: `python3 saavn_bot.py`). No Procfile required, but
       one is included for clarity/robustness.
    5. Deploy. Check the Deploy Logs — you should see:
           "Bot is live: @YourBotUsername"
       This bot uses POLLING (not webhooks), so no public domain, PORT
       binding, or SSL setup is needed on Railway — it just needs to stay
       running, which Railway's worker/service model handles natively.

================================================================================
"""

from __future__ import annotations

import asyncio
import getpass
import html
import logging
import os
import re
import stat
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

# ==============================================================================
# DEPENDENCY CHECK — fail with a helpful message instead of a raw traceback
# ==============================================================================
try:
    import httpx
except ImportError:
    sys.exit(
        "Missing dependency 'httpx'.\n"
        "Install with:  pip install httpx python-telegram-bot"
    )

try:
    from telegram import (
        InlineQueryResultArticle,
        InlineQueryResultAudio,
        InputTextMessageContent,
        Update,
    )
    from telegram.constants import ParseMode
    from telegram.ext import Application, InlineQueryHandler, ContextTypes
    from telegram.error import TelegramError
except ImportError:
    sys.exit(
        "Missing dependency 'python-telegram-bot'.\n"
        "Install with:  pip install httpx python-telegram-bot"
    )


# ==============================================================================
# CONFIG — everything tunable lives here, all overridable via env vars
# (env vars are what you set in Railway's "Variables" tab)
# ==============================================================================

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


IS_RAILWAY = bool(
    os.environ.get("RAILWAY_ENVIRONMENT")
    or os.environ.get("RAILWAY_PROJECT_ID")
    or os.environ.get("RAILWAY_SERVICE_ID")
)

API_BASE = os.environ.get("SAAVN_API_BASE", "https://saavn.sumit.co").rstrip("/")
PAGE_SIZE = _env_int("PAGE_SIZE", 15)                    # songs per inline results page (max sensible: 50)
CACHE_TTL_SECONDS = _env_int("CACHE_TTL_SECONDS", 600)    # search result cache lifetime
RATE_LIMIT_WINDOW = _env_float("RATE_LIMIT_WINDOW", 3.0)  # seconds
RATE_LIMIT_MAX_CALLS = _env_int("RATE_LIMIT_MAX_CALLS", 4)
HTTP_TIMEOUT = _env_float("HTTP_TIMEOUT", 10.0)
HTTP_MAX_RETRIES = _env_int("HTTP_MAX_RETRIES", 2)
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# --- Owner / branding ---
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "@stillrahul")
OWNER_TAG_IN_CAPTION = os.environ.get("OWNER_TAG_IN_CAPTION", "true").lower() in ("1", "true", "yes")
BOT_TAGLINE = "FastTrack Music"

TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".saavn_bot_token")

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    level=getattr(logging, LOG_LEVEL, logging.INFO),
)
logger = logging.getLogger("saavn-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)


# ==============================================================================
# TOKEN HANDLING
#   - On Railway (or whenever BOT_TOKEN env var is set): use it directly,
#     NEVER prompt (Railway has no interactive terminal at boot anyway).
#   - Locally without BOT_TOKEN set: prompt once, offer to cache to disk
#     (0600 permissions) so you're not retyping it every run.
# ==============================================================================

def _looks_like_token(token: str) -> bool:
    return bool(re.match(r"^\d{6,}:[A-Za-z0-9_-]{20,}$", token.strip()))


def load_or_prompt_token() -> str:
    env_token = os.environ.get("BOT_TOKEN", "").strip()
    if env_token:
        if not _looks_like_token(env_token):
            logger.warning("BOT_TOKEN doesn't match the usual token format — trying it anyway.")
        logger.info("Using BOT_TOKEN from environment variable.")
        return env_token

    if IS_RAILWAY:
        # We're on Railway but no token was set — this is a hard config error,
        # not something we can fix by prompting (there's no terminal to prompt).
        sys.exit(
            "❌ BOT_TOKEN environment variable is not set.\n"
            "   On Railway: go to your service -> Variables -> add BOT_TOKEN=<your token>\n"
            "   Get a token from @BotFather on Telegram if you don't have one yet."
        )

    # Local/interactive path only
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r") as f:
                cached = f.read().strip()
            if cached and _looks_like_token(cached):
                print(f"Found a saved bot token in {TOKEN_FILE}")
                use_cached = input("Use this saved token? [Y/n]: ").strip().lower()
                if use_cached in ("", "y", "yes"):
                    return cached
        except OSError:
            pass

    print("=" * 64)
    print(f" {BOT_TAGLINE} — first-time setup   (dev: {OWNER_USERNAME})")
    print("=" * 64)
    print("Paste the bot token you got from @BotFather on Telegram.")
    print("(Input is hidden while you type, for privacy.)")
    print("Tip: on Railway, set this as the BOT_TOKEN env var instead —")
    print("     this prompt is automatically skipped there.\n")

    while True:
        try:
            token = getpass.getpass("Bot token: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            sys.exit(1)

        if not token:
            print("Token can't be empty. Try again.\n")
            continue
        if not _looks_like_token(token):
            confirm = input(
                "That doesn't look like a typical bot token (format: 123456:ABC-...). "
                "Use it anyway? [y/N]: "
            ).strip().lower()
            if confirm not in ("y", "yes"):
                continue
        break

    try:
        save = input("Save this token locally so you're not asked again? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        save = "n"

    if save in ("", "y", "yes"):
        try:
            with open(TOKEN_FILE, "w") as f:
                f.write(token)
            os.chmod(TOKEN_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 0600, owner-only
            print(f"Saved to {TOKEN_FILE} (readable only by you).\n")
        except OSError as e:
            print(f"Couldn't save token file ({e}) — you'll be asked again next run.\n")

    return token


# ==============================================================================
# UTILITIES
# ==============================================================================

def clean_html(text: Optional[str]) -> str:
    """Strip stray HTML tags/entities JioSaavn sometimes embeds in titles."""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    return text.strip()


def esc(text: str) -> str:
    """Escape text for safe use inside Telegram HTML-parse-mode captions."""
    return html.escape(text or "")


def best_download_url(download_urls: list[dict]) -> Optional[str]:
    if not download_urls:
        return None
    quality_order = ["320kbps", "160kbps", "96kbps", "48kbps", "12kbps"]
    by_quality = {d.get("quality"): d.get("url") for d in download_urls}
    for q in quality_order:
        if by_quality.get(q):
            return by_quality[q]
    return download_urls[-1].get("url")


def best_image_url(images: list[dict]) -> Optional[str]:
    if not images:
        return None
    quality_order = ["500x500", "150x150", "50x50"]
    by_quality = {i.get("quality"): i.get("url") for i in images}
    for q in quality_order:
        if by_quality.get(q):
            return by_quality[q]
    return images[-1].get("url")


def format_duration(seconds) -> str:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return ""
    return f"{seconds // 60}:{seconds % 60:02d}"


# ==============================================================================
# IN-MEMORY TTL CACHE (search results) — no external deps / DB required
# ==============================================================================

@dataclass
class CacheEntry:
    songs: list[dict]
    created_at: float = field(default_factory=time.time)


class SearchCache:
    def __init__(self, ttl_seconds: float):
        self.ttl = ttl_seconds
        self._store: dict[str, CacheEntry] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[list[dict]]:
        entry = self._store.get(key)
        if not entry:
            self.misses += 1
            return None
        if time.time() - entry.created_at > self.ttl:
            del self._store[key]
            self.misses += 1
            return None
        self.hits += 1
        return entry.songs

    def set(self, key: str, songs: list[dict]) -> None:
        self._store[key] = CacheEntry(songs=songs)

    def cleanup(self) -> int:
        now = time.time()
        expired = [k for k, v in self._store.items() if now - v.created_at > self.ttl]
        for k in expired:
            del self._store[k]
        return len(expired)

    def stats(self) -> str:
        total = self.hits + self.misses
        rate = (self.hits / total * 100) if total else 0
        return f"{len(self._store)} entries | {self.hits}/{total} hits ({rate:.0f}%)"


# ==============================================================================
# SIMPLE IN-MEMORY PER-USER RATE LIMITER (sliding window)
# ==============================================================================

class RateLimiter:
    def __init__(self, window: float, max_calls: int):
        self.window = window
        self.max_calls = max_calls
        self._hits: dict[int, list[float]] = {}

    def allow(self, user_id: int) -> bool:
        now = time.time()
        hits = self._hits.setdefault(user_id, [])
        hits[:] = [t for t in hits if now - t < self.window]
        if len(hits) >= self.max_calls:
            return False
        hits.append(now)
        return True

    def cleanup(self) -> None:
        now = time.time()
        stale = [uid for uid, hits in self._hits.items() if not hits or now - hits[-1] > self.window * 10]
        for uid in stale:
            del self._hits[uid]


# ==============================================================================
# JIOSAAVN API CLIENT (https://saavn.sumit.co)
# ==============================================================================

class SaavnClient:
    def __init__(self, base_url: str, timeout: float = HTTP_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout)
        self.calls_made = 0
        self.calls_failed = 0

    async def close(self) -> None:
        await self._client.aclose()

    async def search_songs(self, query: str, page: int = 0, limit: int = PAGE_SIZE) -> list[dict]:
        """GET /api/search/songs?query=...&page=...&limit=..."""
        url = f"{self.base_url}/api/search/songs"
        params = {"query": query, "page": page, "limit": limit}

        last_error = None
        for attempt in range(HTTP_MAX_RETRIES + 1):
            try:
                self.calls_made += 1
                resp = await self._client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
                if not data.get("success"):
                    return []
                return data.get("data", {}).get("results", []) or []
            except (httpx.HTTPError, ValueError) as e:
                last_error = e
                self.calls_failed += 1
                if attempt < HTTP_MAX_RETRIES:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
        logger.warning("Search failed for %r after retries: %s", query, last_error)
        return []


# ==============================================================================
# BOT STATE
# ==============================================================================

search_cache = SearchCache(CACHE_TTL_SECONDS)
rate_limiter = RateLimiter(RATE_LIMIT_WINDOW, RATE_LIMIT_MAX_CALLS)
saavn: Optional[SaavnClient] = None
bot_start_time = time.time()
total_songs_served = 0


# ==============================================================================
# BOT LOGIC
# ==============================================================================

def song_to_inline_result(song: dict) -> Optional[InlineQueryResultAudio]:
    song_id = song.get("id")
    if not song_id:
        return None

    audio_url = best_download_url(song.get("downloadUrl", []))
    if not audio_url:
        return None

    title = clean_html(song.get("name") or "Unknown Title")
    artists = song.get("artists", {}).get("primary", []) or []
    artist_names = ", ".join(clean_html(a.get("name", "")) for a in artists) or "Unknown Artist"

    album_name = clean_html((song.get("album") or {}).get("name") or "")
    duration = song.get("duration")
    duration_int = None
    try:
        duration_int = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_int = None

    caption_lines = [f"🎵 <b>{esc(title)}</b>", f"👤 {esc(artist_names)}"]
    if album_name and album_name.lower() != title.lower():
        caption_lines.append(f"💿 {esc(album_name)}")
    if OWNER_TAG_IN_CAPTION:
        caption_lines.append(f"\n<i>via {esc(BOT_TAGLINE)} • {esc(OWNER_USERNAME)}</i>")

    return InlineQueryResultAudio(
        id=song_id,
        audio_url=audio_url,
        title=title,
        performer=artist_names,
        audio_duration=duration_int,
        caption="\n".join(caption_lines),
        parse_mode=ParseMode.HTML,
    )


async def get_songs_for_query(query: str, page: int) -> list[dict]:
    cache_key = f"{query.lower()}::{page}"
    cached = search_cache.get(cache_key)
    if cached is not None:
        return cached
    songs = await saavn.search_songs(query, page=page, limit=PAGE_SIZE)
    search_cache.set(cache_key, songs)
    return songs


def hint_result(rid: str, title: str, description: str, message: str):
    return InlineQueryResultArticle(
        id=rid,
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(message),
    )


async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global total_songs_served

    query = update.inline_query
    query_text = query.query.strip()
    user_id = query.from_user.id if query.from_user else 0

    # ---- Empty query: hint instead of hitting the API ----
    if not query_text:
        await query.answer(
            results=[
                hint_result(
                    "hint",
                    f"🎵 {BOT_TAGLINE} — type a song name",
                    f"e.g. Tum Hi Ho, Blinding Lights, Kesariya  •  dev {OWNER_USERNAME}",
                    f"Type a song name after my username to search. Powered by {BOT_TAGLINE} ({OWNER_USERNAME}).",
                )
            ],
            cache_time=1,
            is_personal=True,
        )
        return

    # ---- Rate limit ----
    if not rate_limiter.allow(user_id):
        await query.answer(
            results=[
                hint_result(
                    "ratelimited",
                    "⏳ Slow down a little",
                    "Too many searches at once — try again in a moment.",
                    "You're searching a bit fast — please wait a second and try again.",
                )
            ],
            cache_time=1,
            is_personal=True,
        )
        return

    # ---- Pagination via Telegram's inline offset ----
    offset = query.offset
    page = int(offset) if offset and offset.isdigit() else 0

    try:
        songs = await get_songs_for_query(query_text, page)
    except Exception as e:
        logger.exception("Unexpected error handling inline query: %s", e)
        songs = []

    if not songs:
        if page == 0:
            await query.answer(
                results=[
                    hint_result(
                        "noresult",
                        "😕 No songs found",
                        f'Nothing matched "{query_text}"',
                        f'No songs found for "{query_text}". Try a different search term.',
                    )
                ],
                cache_time=30,
                is_personal=True,
            )
        else:
            await query.answer(results=[], cache_time=30)
        return

    results = []
    for song in songs:
        result = song_to_inline_result(song)
        if result:
            results.append(result)

    if not results:
        await query.answer(
            results=[
                hint_result(
                    "nourls",
                    "😕 Found songs, but none are playable",
                    "Try a different search term.",
                    "Found matching songs but couldn't get playable audio for them. Try another search.",
                )
            ],
            cache_time=10,
            is_personal=True,
        )
        return

    total_songs_served += len(results)
    next_offset = str(page + 1) if len(songs) >= PAGE_SIZE else ""

    await query.answer(
        results=results,
        cache_time=300,
        is_personal=False,
        next_offset=next_offset,
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update %s caused error: %s", update, context.error)


async def post_init(application: Application) -> None:
    global saavn
    saavn = SaavnClient(API_BASE)
    bot_user = await application.bot.get_me()

    banner = f"""
╔══════════════════════════════════════════════════════════╗
   {BOT_TAGLINE} — LIVE
   Bot: @{bot_user.username}
   Developer: {OWNER_USERNAME}
   Mode: Polling  |  Host: {"Railway" if IS_RAILWAY else "Local"}
   API: {API_BASE}
╚══════════════════════════════════════════════════════════╝

  Use it now — open ANY chat and type:
    @{bot_user.username} <song name>
"""
    print(banner)
    logger.info("Bot started as @%s (dev: %s) — inline search ready.", bot_user.username, OWNER_USERNAME)


async def post_shutdown(application: Application) -> None:
    if saavn:
        await saavn.close()
    uptime = time.time() - bot_start_time
    logger.info(
        "Shutting down. Uptime %.0fs | songs served: %d | cache: %s",
        uptime, total_songs_served, search_cache.stats(),
    )


def periodic_maintenance(context: ContextTypes.DEFAULT_TYPE) -> None:
    expired = search_cache.cleanup()
    rate_limiter.cleanup()
    if expired:
        logger.debug("Cache cleanup: removed %d expired entries. Stats: %s", expired, search_cache.stats())


# ==============================================================================
# ENTRYPOINT
# ==============================================================================

def main() -> None:
    token = load_or_prompt_token()

    try:
        application = (
            Application.builder()
            .token(token)
            .post_init(post_init)
            .post_shutdown(post_shutdown)
            .build()
        )
    except Exception as e:
        sys.exit(f"❌ Failed to start bot — check your token is correct.\nDetails: {e}")

    application.add_handler(InlineQueryHandler(inline_query_handler))
    application.add_error_handler(error_handler)

    if application.job_queue:
        application.job_queue.run_repeating(periodic_maintenance, interval=300, first=300)

    logger.info(
        "Starting polling... (host=%s, api=%s, page_size=%d, cache_ttl=%ds, rate_limit=%d/%.0fs)",
        "Railway" if IS_RAILWAY else "local",
        API_BASE, PAGE_SIZE, CACHE_TTL_SECONDS, RATE_LIMIT_MAX_CALLS, RATE_LIMIT_WINDOW,
    )
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except TelegramError as e:
        sys.exit(f"❌ Telegram error: {e}\nDouble-check your bot token is valid and try again.")
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
