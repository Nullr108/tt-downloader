# -*- coding: utf-8 -*-
"""
Скачивание с YouTube через yt-dlp: видео (mp4) или аудио (mp3).
Использует bundled ffmpeg (imageio-ffmpeg), так что ручная установка
ffmpeg не нужна — работают и конвертация в mp3, и склейка видео+аудио.

Telegram-бот отдаёт файлы до ~50 МБ, поэтому для видео есть каскад
качеств с понижением, если файл получился слишком большим.
"""
import re
import os
import shutil
import asyncio
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def _proxy_url() -> str:
    """Прокси для yt-dlp; по умолчанию используем прокси Telegram."""
    return (os.environ.get("YT_PROXY", "").strip()
            or os.environ.get("TELEGRAM_PROXY", "").strip())


# лимит отправки файла ботом. Обычный Bot API ~50 МБ; локальный Bot API
# server поднимает лимит до ~2 ГБ. Управляется env MAX_UPLOAD_MB.
def max_bytes() -> int:
    try:
        mb = int(os.environ.get("MAX_UPLOAD_MB", "49"))
    except ValueError:
        mb = 49
    return mb * 1024 * 1024

YT_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.|music\.)?"
    r"(?:youtube\.com/(?:watch\?[^\s]*v=|shorts/|live/|embed/)|youtu\.be/)"
    r"[^\s]+",
    re.IGNORECASE,
)


def extract_youtube_urls(text: str):
    if not text:
        return []
    urls = YT_URL_RE.findall(text)
    seen, out = set(), []
    for u in urls:
        u = u.strip().rstrip(".,);]")
        if not u.lower().startswith("http"):
            u = "https://" + u
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def is_youtube(url: str) -> bool:
    return bool(YT_URL_RE.match(url.strip()))


class YtError(Exception):
    pass


def _ffmpeg_dir():
    """
    Возвращает папку с ffmpeg под ИМЕНЕМ ffmpeg(.exe).
      1) если системный ffmpeg есть в PATH (Docker/Linux с apt) — берём его;
      2) иначе bundled imageio-ffmpeg. Его бинарник назван версионно
         (ffmpeg-win-...exe) — для склейки хватает, но нарезка по таймкодам
         (download_ranges) требует имя 'ffmpeg' в PATH, поэтому кладём копию
         в ./bin и добавляем в PATH.
    """
    sys_ff = shutil.which("ffmpeg")
    if sys_ff:
        return os.path.dirname(sys_ff)
    try:
        import imageio_ffmpeg
        src = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None
    bind = os.path.join(HERE, "bin")
    os.makedirs(bind, exist_ok=True)
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    dst = os.path.join(bind, name)
    try:
        if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src):
            shutil.copy2(src, dst)
    except Exception:
        # если скопировать не вышло — вернём хотя бы исходный путь-файл
        return src
    if bind not in os.environ.get("PATH", ""):
        os.environ["PATH"] = bind + os.pathsep + os.environ.get("PATH", "")
    return bind


def _tier(h):
    return (f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/"
            f"b[height<={h}][ext=mp4]/b[height<={h}]")


def _max_height() -> int:
    """
    Потолок разрешения. По умолчанию 1080p — щадит VPS (не тянем
    многогигабайтные 4K, склейка = дешёвый ремукс). Поднять при желании:
    env MAX_VIDEO_HEIGHT=2160.
    """
    try:
        return int(os.environ.get("MAX_VIDEO_HEIGHT", "1080"))
    except ValueError:
        return 1080


def _video_formats(limit_bytes):
    """
    Каскад качеств сверху вниз. Стартовое качество зависит от лимита
    (локальный Bot API server поднимает лимит), но не выше MAX_VIDEO_HEIGHT.
    """
    cap = _max_height()
    if limit_bytes >= 1024 * 1024 * 1024:      # большой лимит (лок. сервер)
        heights = [2160, 1440, 1080, 720, 480, 360]
    elif limit_bytes >= 200 * 1024 * 1024:
        heights = [1080, 720, 480, 360]
    else:                                       # обычный Bot API ~50 МБ
        heights = [720, 480, 360]
    heights = [h for h in heights if h <= cap] or [min(heights)]
    fmts = [_tier(h) for h in heights[:-1]]
    low = heights[-1]
    fmts.append(
        f"bv*[height<={low}][ext=mp4]+ba[ext=m4a]/b[height<={low}]"
        f"/worst[ext=mp4]/worst"
    )
    return fmts


def _download_sync(url: str, mode: str, section=None) -> dict:
    import yt_dlp

    tmpdir = tempfile.mkdtemp()
    out_tpl = os.path.join(tmpdir, "%(id)s.%(ext)s")
    ffdir = _ffmpeg_dir()

    base = {
        "outtmpl": out_tpl,
        "quiet": True,
        "no_warnings": True,
        "no_color": True,          # без ANSI-кодов в тексте ошибок
        "noplaylist": True,
        "restrictfilenames": True,
        # YouTube сейчас отдаёт форматы стабильно только через android-клиент
        # (web/ios/tv ловят "No formats"/DRM/403). android + web как запас.
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        "socket_timeout": 60,
    }
    proxy = _proxy_url()
    if proxy:
        base["proxy"] = proxy
    if ffdir:
        base["ffmpeg_location"] = ffdir
    # нарезка по таймкодам [start,end] (для скачивания глав кусками)
    if section:
        start, end = float(section[0]), float(section[1])
        base["download_ranges"] = yt_dlp.utils.download_range_func(
            None, [(start, end)])
        base["force_keyframes_at_cuts"] = True
    # на VPS YouTube иногда 403 по IP датацентра — тогда помогает cookies.txt.
    # export из браузера, путь в .env: YT_COOKIES=/path/cookies.txt
    cookies = os.environ.get("YT_COOKIES", "").strip()
    if cookies and os.path.exists(cookies):
        base["cookiefile"] = cookies

    def _find_file(info, ydl):
        # реальный путь после постобработки
        rd = info.get("requested_downloads")
        if rd and rd[0].get("filepath"):
            return rd[0]["filepath"]
        return ydl.prepare_filename(info)

    limit = max_bytes()

    if mode == "audio":
        opts = dict(base)
        opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"
        if ffdir:
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = _find_file(info, ydl)
            if ffdir and not path.lower().endswith(".mp3"):
                cand = os.path.splitext(path)[0] + ".mp3"
                if os.path.exists(cand):
                    path = cand
        packed = _pack(info, path)
        # аудио — один файл, каскада нет: проверяем размер вручную
        if os.path.exists(path) and os.path.getsize(path) > limit:
            packed["too_big"] = True
        return packed

    # mode == video: пробуем качества по убыванию, пока не влезем в лимит
    last_path, last_info = None, None
    for fmt in _video_formats(limit):
        opts = dict(base)
        opts["format"] = fmt
        opts["merge_output_format"] = "mp4"
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                path = _find_file(info, ydl)
        except Exception:
            continue
        last_path, last_info = path, info
        if os.path.exists(path) and os.path.getsize(path) <= limit:
            return _pack(info, path)
        # слишком большое — удаляем и пробуем ниже качеством
        try:
            os.remove(path)
        except OSError:
            pass
    if last_path and os.path.exists(last_path):
        # даже минимальное не влезло — вернём с пометкой, бот решит что делать
        info = _pack(last_info, last_path)
        info["too_big"] = os.path.getsize(last_path) > limit
        return info
    raise YtError("не удалось получить видео")


def _pack(info, path):
    chapters = []
    for c in (info.get("chapters") or []):
        if c.get("start_time") is not None and c.get("end_time") is not None:
            chapters.append({
                "start": float(c["start_time"]),
                "end": float(c["end_time"]),
                "title": (c.get("title") or "").strip(),
            })
    return {
        "path": path,
        "title": (info.get("title") or "").strip(),
        "author": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration"),
        "chapters": chapters,
        "url": info.get("webpage_url") or info.get("original_url") or "",
    }


def _probe_sync(url: str) -> dict:
    """Только метаданные (без скачивания): главы, длительность, название."""
    import yt_dlp
    opts = {
        "quiet": True, "no_warnings": True, "no_color": True,
        "noplaylist": True, "skip_download": True,
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        "socket_timeout": 60,
    }
    proxy = _proxy_url()
    if proxy:
        opts["proxy"] = proxy
    cookies = os.environ.get("YT_COOKIES", "").strip()
    if cookies and os.path.exists(cookies):
        opts["cookiefile"] = cookies
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return _pack(info, "")


async def get_media(url: str, mode: str, section=None) -> dict:
    """
    mode = 'video' | 'audio'. section=(start,end) — скачать только кусок.
    Возвращает {path,title,author,duration,chapters,[too_big]}.
    """
    return await asyncio.to_thread(_download_sync, url, mode, section)


async def probe(url: str) -> dict:
    """Метаданные без скачивания (главы и т.д.)."""
    return await asyncio.to_thread(_probe_sync, url)
