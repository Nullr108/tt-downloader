# -*- coding: utf-8 -*-
"""
Downloader Bot — TikTok (без водяного знака) + YouTube (видео/аудио).

TikTok:  кидаешь ссылку/список — сразу качаю видео без ватермарки.
YouTube: кидаешь ссылку/список — спрашиваю "Видео или Аудио?", потом качаю.

Каждый файл сначала показывается как анимированная "рекламная" заглушка
(«✨ Здесь могла быть ваша реклама ✨»), которая затем ПРЕВРАЩАЕТСЯ в сам
скачанный файл (через editMessageMedia).
"""
import os
import re
import glob
import uuid
import random
import asyncio
import logging

from telegram import (
    Update,
    InputMediaVideo,
    InputMediaAudio,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatAction
from telegram.error import RetryAfter
from telegram.ext import (
    Application,
    AIORateLimiter,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

import downloader
import ytdl
import ymusic
import make_placeholder

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("ttbot")

HERE = os.path.dirname(os.path.abspath(__file__))
# Каталог для ИЗМЕНЯЕМЫХ файлов (.env, token.txt, cookies.txt). В Docker
# сюда монтируется том (DATA_DIR=/data), чтобы правки /auth переживали
# пересоздание контейнера. Локально по умолчанию — папка проекта.
DATA_DIR = os.environ.get("DATA_DIR", HERE)
PLACEHOLDER = os.path.join(HERE, "ad_placeholder.gif")
LOADING_DIR = os.path.join(HERE, "loading_gifs")

AD_TEXT = "✨🎬  Здесь могла быть ваша реклама  🎬✨"


def _random_loading_gif():
    """Случайная гифка загрузки из loading_gifs/ (или None, если пусто)."""
    gifs = glob.glob(os.path.join(LOADING_DIR, "*.gif"))
    return random.choice(gifs) if gifs else None


def _ad_frames(kind: str):
    what = "аудио" if kind == "audio" else "видео"
    return [
        f"✨🎬  Здесь могла быть ваша реклама  🎬✨\n⏳ качаю {what}.",
        f"✨🎬  Здесь могла быть ваша реклама  🎬✨\n⌛ качаю {what}..",
        f"✨🎬  Здесь могла быть ваша реклама  🎬✨\n⏳ качаю {what}...",
        f"🎬✨  Здесь могла быть ваша реклама  ✨🎬\n⌛ почти готово 🚀",
    ]


def _load_frames(kind: str):
    what = "аудио" if kind == "audio" else "видео"
    return [
        f"⏳ Качаю {what}.",
        f"⌛ Качаю {what}..",
        f"⏳ Качаю {what}...",
        f"🚀 Почти готово…",
    ]


WELCOME = (
    "👋 Привет!\n\n"
    "🎵 *TikTok* — кинь ссылку, пришлю видео *без водяного знака*.\n"
    "▶️ *YouTube* — кинь ссылку, спрошу *видео или аудио* и пришлю файл.\n"
    "🎧 *Яндекс.Музыка* — трек / альбом / плейлист, пришлю mp3.\n\n"
    "Можно сразу *несколько ссылок* — через пробел или с новой строки.\n"
    "🌍 TikTok качаю через глобальный источник, регион не важен."
)

# ожидающие выбора YouTube-задачи: token -> {"urls": [...], "thread": id}
PENDING = {}
# задачи скачивания глав: token -> {"url","kind","chapters","thread"}
PENDING_CH = {}
# отложенные ссылки Яндекса до входа админа: admin_id -> {urls,chat,thread}
PENDING_YM = {}

# админ и переключатель рекламы (/noAds)
ADMIN_ID = 420796944
ADS = {"on": True}  # dict, чтобы менять из хендлеров без global
AWAIT_COOKIES = set()  # id админов, от которых ждём файл cookies.txt


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean(msg) -> str:
    """Убирает ANSI-коды (yt-dlp красит ошибки) и лишний шум."""
    s = _ANSI_RE.sub("", str(msg)).strip()
    return s[:300] if s else "неизвестная ошибка"


def _thread_id(msg):
    """message_thread_id только если это сообщение в топике форума."""
    if msg is not None and getattr(msg, "is_topic_message", False):
        return msg.message_thread_id
    return None


def _build_caption(info, kind):
    author = info.get("author") or ""
    title = info.get("title") or ""
    caption = "✅ Готово!"
    if author:
        caption += f"  •  @{author}" if kind == "video" else f"  •  {author}"
    if title:
        caption += f"\n📝 {title[:180]}"
    return caption, title, author


def _too_big_ui(info, kind, thread_id):
    """
    Сообщение "губа не дура" при превышении лимита + кнопки глав, если у
    ролика есть таймкоды. Возвращает (caption, reply_markup|None).
    """
    try:
        mb = round(os.path.getsize(info["path"]) / 1024 / 1024)
    except OSError:
        mb = "over 9000"
    what = "аудио" if kind == "audio" else "видео"
    cap = (f"😏 Губа не дура!\n\n"
           f"Это {what} весит ~{mb} МБ, а Telegram разрешает ботам слать "
           f"максимум *50 МБ*. Так что целиком — увы.")
    chapters = info.get("chapters") or []
    url = info.get("url") or ""
    if len(chapters) >= 2 and url:
        token = uuid.uuid4().hex[:8]
        PENDING_CH[token] = {"url": url, "kind": kind,
                             "chapters": chapters, "thread": thread_id}
        cap += ("\n\n📑 Зато у ролика есть главы — могу прислать кусками "
                "по таймкодам. Жми нужную:")
        return cap, _build_chapter_kb(token, chapters)
    cap += "\n\n(таймкодов у ролика нет — резать не на что 🤷)"
    return cap, None


def _build_chapter_kb(token, chapters, limit=30):
    rows = []
    for i, ch in enumerate(chapters[:limit]):
        m, s = int(ch["start"] // 60), int(ch["start"] % 60)
        title = ch["title"] or f"Глава {i + 1}"
        label = f"{i + 1}. [{m:02d}:{s:02d}] {title}"
        rows.append([InlineKeyboardButton(
            label[:45], callback_data=f"ch|{token}|{i}")])
    if len(chapters) > limit:
        rows.append([InlineKeyboardButton(
            f"… и ещё {len(chapters) - limit} глав (показаны первые {limit})",
            callback_data="ch|noop|0")])
    return InlineKeyboardMarkup(rows)


def get_token() -> str:
    _load_dotenv()
    tok = os.environ.get("BOT_TOKEN", "").strip()
    if tok:
        return tok
    tpath = os.path.join(DATA_DIR, "token.txt")
    if os.path.exists(tpath):
        with open(tpath, "r", encoding="utf-8") as f:
            return f.read().strip()
    raise SystemExit(
        "Нет токена! Вставь токен в файл .env (BOT_TOKEN=...), в token.txt "
        "или задай переменную окружения BOT_TOKEN (получить у @BotFather)."
    )


def _update_env(key, value):
    """Записать/обновить key=value в .env и в текущем окружении процесса."""
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, ".env")
    lines, found = [], False
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
                    lines.append(f"{key}={value}\n")
                    found = True
                else:
                    lines.append(line if line.endswith("\n") else line + "\n")
    if not found:
        lines.append(f"{key}={value}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.environ[key] = value


def _load_dotenv():
    """
    Парсер .env из DATA_DIR. Этот файл бот ведёт САМ (/auth дописывает сюда
    токены), поэтому он АВТОРИТЕТНЕЕ переменных окружения из compose/env_file:
    значения отсюда перекрывают os.environ. Так обновления через /auth всегда
    применяются на рестарте, независимо от корневого .env.
    """
    path = os.path.join(DATA_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ[key.strip()] = val.strip().strip('"').strip("'")


def ensure_placeholder():
    if not os.path.exists(PLACEHOLDER):
        log.info("Генерирую анимацию-заглушку...")
        make_placeholder.build(PLACEHOLDER)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        WELCOME, parse_mode="Markdown",
        message_thread_id=_thread_id(update.message),
    )


async def cmd_noads(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Только для админа: включить/выключить рекламную заглушку."""
    thread_id = _thread_id(update.message)
    user = update.effective_user
    if not user or user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Команда только для админа.",
                                        message_thread_id=thread_id)
        return
    ADS["on"] = not ADS["on"]
    state = "🟢 включена" if ADS["on"] else "🔴 выключена"
    await update.message.reply_text(
        f"Реклама-заглушка теперь {state}.", message_thread_id=thread_id,
    )


async def cmd_auth(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Только для админа: обновить авторизацию Яндекса / куки YouTube."""
    thread_id = _thread_id(update.message)
    user = update.effective_user
    if not user or user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Команда только для админа.",
                                        message_thread_id=thread_id)
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎵 Обновить токен Яндекса",
                              callback_data="auth|yandex")],
        [InlineKeyboardButton("🍪 Обновить куки YouTube",
                              callback_data="auth|ytcookies")],
    ])
    await update.message.reply_text(
        "🔐 *Обновление авторизации*\n\n"
        "• *Яндекс* — вход по коду: открою ссылку, подтвердишь аккаунт, "
        "токен сам обновится.\n"
        "• *YouTube* — пришлёшь мне файл `cookies.txt` (экспорт из браузера), "
        "буду использовать его при 403.",
        parse_mode="Markdown", reply_markup=kb, message_thread_id=thread_id,
    )


async def _refresh_yandex(bot, chat_id, thread_id):
    """Device-flow вход в Яндекс: шлём админу ссылку+код, ждём токен."""
    loop = asyncio.get_running_loop()

    def on_code(code):
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔓 Открыть страницу входа Яндекса",
                                 url=code.verification_url)
        ]])
        asyncio.run_coroutine_threadsafe(
            bot.send_message(
                chat_id,
                f"1️⃣ Открой ссылку ниже и войди в нужный аккаунт Яндекса\n"
                f"2️⃣ Введи код: `{code.user_code}`\n\n"
                f"Жду подтверждения…",
                parse_mode="Markdown", reply_markup=kb,
                message_thread_id=thread_id,
            ),
            loop,
        ).result()

    try:
        token = await asyncio.to_thread(ymusic.device_auth_sync, on_code)
    except Exception as e:
        await bot.send_message(chat_id, f"❌ Не удалось войти: {_clean(e)}",
                               message_thread_id=thread_id)
        return
    _update_env("YANDEX_TOKEN", token)
    ymusic.reset_client()
    await bot.send_message(chat_id, "✅ Токен Яндекса обновлён и сохранён.",
                           message_thread_id=thread_id)
    # если админ пришёл со ссылкой Яндекса до входа — сразу докачиваем
    job = PENDING_YM.pop(ADMIN_ID, None)
    if job:
        await bot.send_message(job["chat"], "▶️ Продолжаю с твоими ссылками…",
                               message_thread_id=job["thread"])
        await _process_yandex(bot, job["chat"], job["thread"], job["urls"])


async def on_auth_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    if not user or user.id != ADMIN_ID:
        await query.answer("Только для админа", show_alert=True)
        return
    await query.answer()
    _, what = query.data.split("|", 1)
    chat_id = query.message.chat.id
    thread_id = _thread_id(query.message)

    if what == "yandex":
        await query.edit_message_text("🎵 Запускаю вход в Яндекс…")
        await _refresh_yandex(ctx.bot, chat_id, thread_id)
    elif what == "ytcookies":
        AWAIT_COOKIES.add(ADMIN_ID)
        await query.edit_message_text(
            "🍪 Пришли мне файл cookies.txt одним сообщением (как документ).\n\n"
            "Как получить: залогинься на youtube.com, экспортируй куки "
            "расширением «Get cookies.txt LOCALLY» (формат Netscape) и "
            "пришли сюда файл. Отмена — /cancel."
        )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id == ADMIN_ID:
        AWAIT_COOKIES.discard(ADMIN_ID)
    await update.message.reply_text("Ок, отменил.",
                                    message_thread_id=_thread_id(update.message))


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Приём cookies.txt от админа (после кнопки «Обновить куки YouTube»)."""
    user = update.effective_user
    if not user or user.id != ADMIN_ID or ADMIN_ID not in AWAIT_COOKIES:
        return
    thread_id = _thread_id(update.message)
    doc = update.message.document
    try:
        tg_file = await doc.get_file()
        os.makedirs(DATA_DIR, exist_ok=True)
        dest = os.path.join(DATA_DIR, "cookies.txt")
        await tg_file.download_to_drive(dest)
    except Exception as e:
        await update.message.reply_text(f"❌ Не смог сохранить файл: {_clean(e)}",
                                        message_thread_id=thread_id)
        return
    _update_env("YT_COOKIES", dest)
    AWAIT_COOKIES.discard(ADMIN_ID)
    await update.message.reply_text(
        "✅ Куки YouTube сохранены — буду использовать их при загрузке.",
        message_thread_id=thread_id,
    )


async def _animate_caption(bot, chat_id, msg_id, frames, stop_event):
    """Крутит подпись-загрузку, пока не выставлен stop_event."""
    i = 0
    delay = 3.0  # реже правим подпись, чтобы не ловить 429 Too Many Requests
    while not stop_event.is_set():
        try:
            await bot.edit_message_caption(
                chat_id=chat_id, message_id=msg_id,
                caption=frames[i % len(frames)],
            )
        except RetryAfter as e:
            # Telegram просит подождать — уважаем и притормаживаем анимацию
            delay = max(delay, float(getattr(e, "retry_after", 3)) + 1)
        except Exception:
            pass
        i += 1
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass


async def _send_file(bot, chat_id, thread_id, file_path, kind, caption, title, author):
    """Отправляет готовый файл как видео/аудио в нужный топик."""
    with open(file_path, "rb") as fh:
        if kind == "video":
            await bot.send_video(chat_id, fh, caption=caption,
                                 supports_streaming=True,
                                 message_thread_id=thread_id)
        else:
            await bot.send_audio(chat_id, fh, caption=caption,
                                 title=title[:64] or None,
                                 performer=author or None,
                                 message_thread_id=thread_id)


async def deliver_with_ad(bot, chat_id, tag, coro, kind, thread_id=None):
    """
    Универсальная доставка через эффект "гифка -> файл".
      * реклама включена  -> заглушка "здесь могла быть ваша реклама";
      * /noAds            -> случайная гифка загрузки из loading_gifs/.
    В обоих случаях гифка потом ПРЕВРАЩАЕТСЯ в скачанный файл.
    kind = 'video' | 'audio'. Всё отправляется в топик thread_id.
    """
    action = ChatAction.UPLOAD_VIDEO if kind == "video" else ChatAction.UPLOAD_VOICE

    # выбираем гифку и подписи в зависимости от режима
    if ADS["on"]:
        gif_path = PLACEHOLDER
        first_caption = AD_TEXT + tag
        frames = _ad_frames(kind)
    else:
        gif_path = _random_loading_gif()
        first_caption = ("⏳ Качаю…" + tag).strip()
        frames = _load_frames(kind)

    # если гифок нет (папка пустая) — откат на текстовый режим
    if not gif_path or not os.path.exists(gif_path):
        return await _deliver_plain(bot, chat_id, thread_id, coro, kind, action)

    # a) анимированная заглушка как animation (GIF)
    with open(gif_path, "rb") as gif:
        ad_msg = await bot.send_animation(
            chat_id=chat_id, animation=gif, caption=first_caption,
            message_thread_id=thread_id,
        )

    stop = asyncio.Event()
    animator = asyncio.create_task(
        _animate_caption(bot, chat_id, ad_msg.message_id, frames, stop)
    )

    file_path = None
    try:
        await bot.send_chat_action(chat_id, action, message_thread_id=thread_id)
        info = await coro
        file_path = info["path"]

        stop.set()
        await animator

        if info.get("too_big"):
            cap, kb = _too_big_ui(info, kind, thread_id)
            await bot.edit_message_caption(
                chat_id=chat_id, message_id=ad_msg.message_id,
                caption=cap, parse_mode="Markdown", reply_markup=kb,
            )
            return

        caption, title, author = _build_caption(info, kind)

        # d) ПРЕВРАЩАЕМ рекламную заглушку в сам файл
        with open(file_path, "rb") as fh:
            if kind == "video":
                media = InputMediaVideo(media=fh, caption=caption,
                                        supports_streaming=True)
            else:
                media = InputMediaAudio(media=fh, caption=caption,
                                        title=title[:64] or None,
                                        performer=author or None)
            try:
                await bot.edit_message_media(
                    chat_id=chat_id, message_id=ad_msg.message_id, media=media,
                )
            except Exception:
                # запасной путь: удалить заглушку и отправить файл заново
                await _send_file(bot, chat_id, thread_id, file_path, kind,
                                 caption, title, author)
                try:
                    await bot.delete_message(chat_id, ad_msg.message_id)
                except Exception:
                    pass
    except Exception as e:
        stop.set()
        try:
            await animator
        except Exception:
            pass
        log.exception("Ошибка доставки")
        try:
            await bot.edit_message_caption(
                chat_id=chat_id, message_id=ad_msg.message_id,
                caption=f"❌ Не смог скачать 😔\n\n{_clean(e)}",
            )
        except Exception:
            await bot.send_message(chat_id, f"❌ Ошибка: {_clean(e)}",
                                   message_thread_id=thread_id)
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass


async def _deliver_plain(bot, chat_id, thread_id, coro, kind, action):
    """Режим без рекламы: короткий статус -> файл."""
    status = await bot.send_message(chat_id, "⏳ Качаю…",
                                    message_thread_id=thread_id)
    file_path = None
    try:
        await bot.send_chat_action(chat_id, action, message_thread_id=thread_id)
        info = await coro
        file_path = info["path"]
        if info.get("too_big"):
            cap, kb = _too_big_ui(info, kind, thread_id)
            await bot.edit_message_text(
                cap, chat_id=chat_id, message_id=status.message_id,
                parse_mode="Markdown", reply_markup=kb,
            )
            return
        caption, title, author = _build_caption(info, kind)
        await _send_file(bot, chat_id, thread_id, file_path, kind,
                         caption, title, author)
        try:
            await bot.delete_message(chat_id, status.message_id)
        except Exception:
            pass
    except Exception as e:
        log.exception("Ошибка доставки (noAds)")
        try:
            await bot.edit_message_text(f"❌ Не смог скачать 😔\n\n{_clean(e)}",
                                        chat_id=chat_id,
                                        message_id=status.message_id)
        except Exception:
            await bot.send_message(chat_id, f"❌ Ошибка: {_clean(e)}",
                                   message_thread_id=thread_id)
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass


async def _process_yandex(bot, chat_id, thread_id, urls):
    """Развернуть ссылки Яндекса в треки и скачать по очереди."""
    try:
        tracks = []
        for u in urls:
            tracks.extend(await ymusic.expand(u))
    except ymusic.YmError as e:
        await bot.send_message(chat_id, f"🎧 Яндекс.Музыка: {_clean(e)}",
                               message_thread_id=thread_id)
        return
    if not tracks:
        await bot.send_message(chat_id, "🎧 По ссылке ничего не нашёл 🤔",
                               message_thread_id=thread_id)
        return
    if len(tracks) > 1:
        await bot.send_message(
            chat_id, f"🎧 Яндекс: нашёл {len(tracks)} треков — качаю по очереди…",
            message_thread_id=thread_id)
    for i, tr in enumerate(tracks, 1):
        tag = f" [{i}/{len(tracks)}]" if len(tracks) > 1 else ""
        await deliver_with_ad(bot, chat_id, tag,
                              ymusic.download_track(tr), "audio", thread_id)


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    chat_id = update.effective_chat.id
    thread_id = _thread_id(update.message)
    bot = ctx.bot

    yt_urls = ytdl.extract_youtube_urls(text)
    ym_urls = ymusic.extract_ym_urls(text)
    # tiktok-ссылки, но НЕ пересекающиеся с youtube/яндекс
    tt_urls = [u for u in downloader.extract_urls(text)
               if u not in yt_urls and u not in ym_urls]

    if not yt_urls and not tt_urls and not ym_urls:
        # в группе не засоряем чат — молчим, если ссылок нет
        if update.effective_chat.type == "private":
            await update.message.reply_text(
                "🤔 Не вижу ссылок. Пришли TikTok, YouTube или Яндекс.Музыку:\n"
                "https://www.tiktok.com/... , https://youtube.com/... , "
                "https://music.yandex.ru/...",
                message_thread_id=thread_id,
            )
        return

    # TikTok — сразу качаем видео без ватермарки
    if tt_urls:
        if len(tt_urls) > 1:
            await update.message.reply_text(
                f"🎯 TikTok: нашёл {len(tt_urls)} ссылок — качаю по очереди…",
                message_thread_id=thread_id,
            )
        for i, url in enumerate(tt_urls, 1):
            tag = f" [{i}/{len(tt_urls)}]" if len(tt_urls) > 1 else ""
            await deliver_with_ad(bot, chat_id, tag,
                                  downloader.get_video(url), "video", thread_id)

    # Яндекс.Музыка — сразу качаем mp3 (трек/альбом/плейлист)
    if ym_urls:
        user = update.effective_user
        is_admin = bool(user and user.id == ADMIN_ID)
        if ymusic.has_token():
            await _process_yandex(bot, chat_id, thread_id, ym_urls)
        elif is_admin:
            # токена нет — бот сам предлагает войти, а ссылки запомнит
            PENDING_YM[ADMIN_ID] = {"urls": ym_urls, "chat": chat_id,
                                    "thread": thread_id}
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🎵 Войти в Яндекс",
                                     callback_data="auth|yandex")
            ]])
            await update.message.reply_text(
                "🎧 Яндекс.Музыка ещё не подключена.\n"
                "Нажми — войдёшь по коду, и я *сразу скачаю* эти ссылки 👇",
                parse_mode="Markdown", reply_markup=kb,
                message_thread_id=thread_id,
            )
        else:
            await update.message.reply_text(
                "🎧 Яндекс.Музыка пока не настроена — попроси админа "
                "подключить её.", message_thread_id=thread_id,
            )

    # YouTube — сначала спрашиваем видео/аудио
    if yt_urls:
        token = uuid.uuid4().hex[:8]
        PENDING[token] = {"urls": yt_urls, "thread": thread_id}
        n = len(yt_urls)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🎬 Видео", callback_data=f"yt|video|{token}"),
            InlineKeyboardButton("🎵 Аудио", callback_data=f"yt|audio|{token}"),
        ]])
        word = "ссылку" if n == 1 else f"{n} ссылок"
        await update.message.reply_text(
            f"▶️ YouTube: получил {word}. Что качаем?", reply_markup=kb,
            message_thread_id=thread_id,
        )


async def on_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, mode, token = query.data.split("|", 2)
    except ValueError:
        return
    job = PENDING.pop(token, None)
    if not job:
        await query.edit_message_text("⚠️ Устарело — пришли ссылку заново.")
        return

    urls = job["urls"]
    thread_id = job.get("thread")
    kind = "audio" if mode == "audio" else "video"
    label = "🎵 Аудио" if kind == "audio" else "🎬 Видео"
    await query.edit_message_text(
        f"{label} — качаю {len(urls)} шт., по очереди…"
        if len(urls) > 1 else f"{label} — качаю…"
    )

    chat_id = query.message.chat.id
    bot = ctx.bot
    for i, url in enumerate(urls, 1):
        tag = f" [{i}/{len(urls)}]" if len(urls) > 1 else ""
        await deliver_with_ad(bot, chat_id, tag, ytdl.get_media(url, kind),
                              kind, thread_id)


async def on_chapter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Скачать выбранную главу ролика куском по таймкоду."""
    query = update.callback_query
    try:
        _, token, idx = query.data.split("|", 2)
        idx = int(idx)
    except ValueError:
        await query.answer()
        return
    if token == "noop":
        await query.answer("Показаны первые 30 глав", show_alert=True)
        return
    job = PENDING_CH.get(token)
    if not job or idx >= len(job["chapters"]):
        await query.answer("Устарело — пришли ссылку заново", show_alert=True)
        return
    await query.answer("Качаю главу…")
    ch = job["chapters"][idx]
    kind = job["kind"]
    thread_id = job.get("thread")
    chat_id = query.message.chat.id
    title = ch["title"] or f"Глава {idx + 1}"
    tag = f" · {title[:40]}"
    await deliver_with_ad(
        ctx.bot, chat_id, tag,
        ytdl.get_media(job["url"], kind, section=(ch["start"], ch["end"])),
        kind, thread_id,
    )


def build_application(token):
    builder = Application.builder().token(token)
    # авто-троттлинг и повтор при 429 Too Many Requests
    builder = builder.rate_limiter(AIORateLimiter())
    # опциональный локальный Bot API server -> файлы до ~2 ГБ
    base = os.environ.get("LOCAL_BOT_API", "").strip().rstrip("/")
    if base:
        builder = builder.base_url(f"{base}/bot")
        builder = builder.base_file_url(f"{base}/file/bot")
        # при локальном сервере поднимаем лимит, если не задан вручную
        os.environ.setdefault("MAX_UPLOAD_MB", "1900")
        log.info("Использую локальный Bot API server: %s (лимит %s МБ)",
                 base, os.environ.get("MAX_UPLOAD_MB"))
    return builder.build()


def main():
    ensure_placeholder()
    token = get_token()
    app = build_application(token)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("noads", cmd_noads))
    app.add_handler(CommandHandler("auth", cmd_auth))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_auth_choice, pattern=r"^auth\|"))
    app.add_handler(CallbackQueryHandler(on_chapter, pattern=r"^ch\|"))
    app.add_handler(CallbackQueryHandler(on_choice, pattern=r"^yt\|"))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    log.info("Бот запущен. Ожидаю ссылки…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
