import os
import uuid
import requests
from fastapi import FastAPI, Body
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")




def send_telegram_message(chat_id: int, text: str, keyboard=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text
    }
    if keyboard:
        payload["reply_markup"] = keyboard

    requests.post(url, json=payload)


@app.post("/telegram")
async def telegram_update(payload: dict = Body(...)):
    message = payload.get("message")
    if not message:
        return {"ok": True}

    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    if text == "/start":
        keyboard = {
            "keyboard": [
                [{"text": "🔗 Подключить GitHub"}],
                [{"text": "ℹ️ Помощь"}]
            ],
            "resize_keyboard": True
        }

        send_telegram_message(
            chat_id,
            "👋 Добро пожаловать в Integration Hub!\n\n"
            "Я помогу получать события из GitHub прямо в Telegram.",
            keyboard
        )

    elif text == "🔗 Подключить GitHub":
        user_token = str(uuid.uuid4())
        webhook_url = f"{BASE_URL}/webhook/github/{user_token}"

        send_telegram_message(
            chat_id,
            "🔗 Подключение GitHub\n\n"
            "1️⃣ Зайди в репозиторий GitHub\n"
            "2️⃣ Settings → Webhooks → Add webhook\n"
            f"3️⃣ Payload URL:\n{webhook_url}\n"
            "4️⃣ Content type: application/json\n"
            "5️⃣ Events: Push\n\n"
            "После этого коммиты начнут приходить сюда 👇"
        )

    elif text == "ℹ️ Помощь":
        send_telegram_message(
            chat_id,
            "ℹ️ Помощь\n\n"
            "• Этот бот принимает события из GitHub\n"
            "• Используй кнопку «Подключить GitHub»\n"
            "• Поддерживаются push events"
        )

    return {"ok": True}


@app.post("/webhook/github/{user_token}")
async def github_webhook(user_token: str, payload: dict = Body(...)):
    repo = payload.get("repository", {}).get("name", "unknown")
    pusher = payload.get("pusher", {}).get("name", "unknown")

    message = (
        "🔔 GitHub push event\n"
        f"Repository: {repo}\n"
        f"Author: {pusher}"
    )

    # ⚠️ Пока отправляем ТЕБЕ (позже сделаем БД)
    chat_id = int(os.getenv("TELEGRAM_CHAT_ID"))
    send_telegram_message(chat_id, message)

    return {"status": "ok"}

# Запуск: uvicorn main:app --reload