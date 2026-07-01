# -*- coding: utf-8 -*-
"""
Модуль скачивания TikTok-видео без водяного знака.
Основной способ — публичный API tikwm.com (качает с глобального CDN,
регион РФ не задействован). Резерв — yt-dlp.
"""
import re
import asyncio
import tempfile
import os
import httpx

# Ссылки TikTok в любом виде
URL_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.|vm\.|vt\.)?tiktok\.com/[^\s]+",
    re.IGNORECASE,
)

TIKWM_API = "https://www.tikwm.com/api/"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json",
}


def extract_urls(text: str):
    """Достаёт ВСЕ ссылки TikTok из текста (пробелы/переносы строк)."""
    if not text:
        return []
    urls = URL_RE.findall(text)
    # убираем дубли, сохраняя порядок
    seen, out = set(), []
    for u in urls:
        u = u.strip().rstrip(".,);]")
        if not u.lower().startswith("http"):
            u = "https://" + u
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


class DownloadError(Exception):
    pass


async def fetch_info(url: str) -> dict:
    """Возвращает dict с прямыми ссылками и метаданными через tikwm."""
    params = {"url": url, "hd": "1"}
    async with httpx.AsyncClient(timeout=40, headers=HEADERS) as client:
        r = await client.get(TIKWM_API, params=params)
        r.raise_for_status()
        data = r.json()
    if data.get("code") != 0 or "data" not in data:
        raise DownloadError(f"tikwm: {data.get('msg', 'нет данных')}")
    d = data["data"]
    video_url = d.get("hdplay") or d.get("play")
    if not video_url:
        raise DownloadError("tikwm: нет ссылки без водяного знака")
    author = (d.get("author") or {})
    return {
        "video_url": video_url,
        "title": (d.get("title") or "").strip(),
        "author": author.get("unique_id") or author.get("nickname") or "",
        "duration": d.get("duration"),
    }


async def download_to_file(video_url: str, suffix=".mp4") -> str:
    """Качает видео во временный файл, возвращает путь."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        async with httpx.AsyncClient(timeout=120, headers=HEADERS,
                                     follow_redirects=True) as client:
            async with client.stream("GET", video_url) as resp:
                resp.raise_for_status()
                with open(path, "wb") as f:
                    async for chunk in resp.aiter_bytes(1 << 16):
                        f.write(chunk)
        if os.path.getsize(path) < 1024:
            raise DownloadError("скачался пустой файл")
        return path
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise


async def _ytdlp_fallback(url: str) -> dict:
    """Резерв: yt-dlp скачивает mp4 без водяного знака."""
    tmpdir = tempfile.mkdtemp()
    out_tpl = os.path.join(tmpdir, "%(id)s.%(ext)s")

    def _run():
        import yt_dlp
        opts = {
            "outtmpl": out_tpl,
            "quiet": True,
            "no_warnings": True,
            "format": "best[ext=mp4]/best",
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
            return info, path

    info, path = await asyncio.to_thread(_run)
    return {
        "path": path,
        "title": (info.get("title") or "").strip(),
        "author": info.get("uploader") or info.get("uploader_id") or "",
        "duration": info.get("duration"),
    }


async def get_video(url: str) -> dict:
    """
    Главная функция: возвращает {path, title, author, duration}.
    Сначала tikwm, при неудаче — yt-dlp.
    """
    try:
        info = await fetch_info(url)
        path = await download_to_file(info["video_url"])
        info["path"] = path
        return info
    except Exception as e:
        # резерв
        try:
            return await _ytdlp_fallback(url)
        except Exception as e2:
            raise DownloadError(f"tikwm: {e} | yt-dlp: {e2}")
