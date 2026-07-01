# -*- coding: utf-8 -*-
"""
Помощник: получить OAuth-токен Яндекс.Музыки для бота.

Запуск:
    python get_yandex_token.py

Скрипт покажет ссылку и код — открой ссылку, введи код, подтверди вход
в свой аккаунт Яндекса. В конце выведет access_token — вставь его в .env:
    YANDEX_TOKEN=здесь_твой_токен
"""
from yandex_music import Client


def on_code(code):
    print("\n=== ВХОД В ЯНДЕКС ===")
    print("1) Открой ссылку:", code.verification_url)
    print("2) Введи код:    ", code.user_code)
    print("Жду подтверждения...\n")


def main():
    client = Client()
    token = client.device_auth(on_code=on_code)
    print("=" * 50)
    print("ГОТОВО! Твой токен (вставь в .env как YANDEX_TOKEN=...):\n")
    print(token.access_token)
    print("=" * 50)


if __name__ == "__main__":
    main()
