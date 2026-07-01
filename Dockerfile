# TikTok/YouTube/Яндекс.Музыка downloader bot
FROM python:3.12-slim

# ffmpeg — склейка/нарезка/конвертация; fonts-dejavu — кириллица в гифке-заглушке
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# генерим анимацию-заглушку на этапе сборки (чтобы не делать на старте)
RUN python make_placeholder.py

# ВСЕ изменяемые файлы (.env, token.txt, cookies.txt) держим на томе /data,
# чтобы обновления через /auth переживали пересоздание контейнера.
ENV DATA_DIR=/data
RUN mkdir -p /data
VOLUME ["/data"]

# нет веб-порта — бот работает по long polling
CMD ["python", "bot.py"]
