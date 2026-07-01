# -*- coding: utf-8 -*-
"""
Скачивание с Яндекс.Музыки через официальную (неофициальную) библиотеку
yandex-music (MarshalX). Требует OAuth-токен аккаунта Яндекса.

Без токена Яндекс.Музыку скачать нельзя (yt-dlp отдаёт лишь 30 сек),
поэтому единственный рабочий путь — библиотека + токен.

Поддержка ссылок: отдельный трек, альбом целиком, плейлист.
Каждый трек качается в mp3 192 kbps и отдаётся как аудио.
"""
import re
import os
import asyncio
import tempfile

YM_URL_RE = re.compile(
    r"(?:https?://)?music\.yandex\.(?:ru|com|by|kz|uz)/[^\s]+",
    re.IGNORECASE,
)

_client = None
_client_lock = asyncio.Lock()


class YmError(Exception):
    pass


def extract_ym_urls(text: str):
    if not text:
        return []
    urls = YM_URL_RE.findall(text)
    seen, out = set(), []
    for u in urls:
        u = u.strip().rstrip(".,);]")
        if not u.lower().startswith("http"):
            u = "https://" + u
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def is_yandex(url: str) -> bool:
    return bool(YM_URL_RE.match(url.strip()))


def _get_token():
    for key in ("YANDEX_TOKEN", "YANDEX_MUSIC_TOKEN"):
        v = os.environ.get(key, "").strip()
        if v:
            return v
    return None


def has_token() -> bool:
    return bool(_get_token())


def reset_client():
    """Сбросить кэш клиента, чтобы подхватить новый токен."""
    global _client
    _client = None


def device_auth_sync(on_code, timeout: float = 300) -> str:
    """
    Вход в Яндекс через device-flow. on_code(code) вызывается с объектом,
    у которого есть .verification_url и .user_code — их показываем админу.
    Блокирующая функция (полит сервер Яндекса), запускать в to_thread.
    Возвращает access_token.
    """
    from yandex_music import Client

    token = Client().device_auth(on_code=on_code, timeout=timeout)
    return token.access_token


async def _get_client():
    global _client
    if _client is not None:
        return _client
    async with _client_lock:
        if _client is not None:
            return _client
        token = _get_token()
        if not token:
            raise YmError(
                "нет токена Яндекс.Музыки. Добавь в .env строку "
                "YANDEX_TOKEN=... (как получить — см. README)."
            )
        from yandex_music import Client

        def _init():
            return Client(token).init()

        _client = await asyncio.to_thread(_init)
        return _client


def _parse(url: str):
    """Определяет тип ссылки: ('track'|'album'|'playlist', параметры)."""
    u = url.split("?")[0].rstrip("/")
    m = re.search(r"/album/(\d+)/track/(\d+)", u)
    if m:
        return ("track", m.group(2))
    m = re.search(r"/track/(\d+)", u)
    if m:
        return ("track", m.group(1))
    m = re.search(r"/users/([^/]+)/playlists/(\d+)", u)
    if m:
        return ("playlist", (m.group(1), m.group(2)))
    m = re.search(r"/album/(\d+)", u)
    if m:
        return ("album", m.group(1))
    raise YmError("не распознал ссылку Яндекс.Музыки")


async def expand(url: str):
    """
    Разворачивает ссылку в список Track-объектов.
    Трек -> 1 шт, альбом/плейлист -> все треки.
    """
    client = await _get_client()
    kind, arg = _parse(url)

    def _work():
        if kind == "track":
            tracks = client.tracks([arg])
            return list(tracks or [])
        if kind == "album":
            album = client.albums_with_tracks(arg)
            out = []
            for vol in (album.volumes or []):
                out.extend(vol)
            return out
        if kind == "playlist":
            user, plkind = arg
            pl = client.users_playlists(plkind, user)
            out = []
            for short in (pl.tracks or []):
                t = short.track or short.fetch_track()
                if t:
                    out.append(t)
            return out
        return []

    return await asyncio.to_thread(_work)


def _meta(track):
    artists = ", ".join(a.name for a in (track.artists or []) if a.name)
    title = track.title or "track"
    if getattr(track, "version", None):
        title = f"{title} ({track.version})"
    dur = None
    if getattr(track, "duration_ms", None):
        dur = int(track.duration_ms / 1000)
    return title, artists, dur


async def download_track(track) -> dict:
    """Качает один Track в mp3, возвращает {path,title,author,duration}."""
    title, artists, dur = _meta(track)
    fd, path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)

    def _dl():
        track.download(path, codec="mp3", bitrate_in_kbps=192)

    try:
        await asyncio.to_thread(_dl)
    except Exception as e:
        try:
            os.remove(path)
        except OSError:
            pass
        raise YmError(f"не удалось скачать трек: {e}")

    if not os.path.exists(path) or os.path.getsize(path) < 1024:
        raise YmError("скачался пустой файл (трек недоступен в регионе?)")
    return {"path": path, "title": title, "author": artists, "duration": dur}
