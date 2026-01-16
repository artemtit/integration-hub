import os
import uuid
import requests
from fastapi import FastAPI, Body
from dotenv import load_dotenv
from supabase import create_client

# =========================
# ENV
# =========================
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI()

# =========================
# HELPERS
# =========================
def send_telegram_message(chat_id: int, text: str, keyboard: dict | None = None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text
    }
    if keyboard:
        payload["reply_markup"] = keyboard

    r = requests.post(url, json=payload)
    if r.status_code != 200:
        print("Telegram error:", r.text)


# =========================
# HEALTH (for cron / monitoring)
# =========================
@app.get("/health")
async def health():
    return {"status": "ok"}


# =========================
# TELEGRAM WEBHOOK
# =========================
@app.post("/telegram")
async def telegram_update(payload: dict = Body(...)):
    message = payload.get("message")
    if not message:
        return {"ok": True}

    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    # ---- регистрация пользователя ----
    supabase.table("users").upsert({
        "chat_id": chat_id
    }).execute()

    # ---- /start ----
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

    # ---- Подключить GitHub ----
    elif text == "🔗 Подключить GitHub":
        user = supabase.table("users") \
            .select("id") \
            .eq("chat_id", chat_id) \
            .single() \
            .execute()

        user_id = user.data["id"]

        webhook = supabase.table("webhooks").insert({
            "user_id": user_id,
            "source": "github"
        }).execute()

        webhook_id = webhook.data[0]["id"]
        webhook_url = f"{BASE_URL}/webhook/github/{webhook_id}"

        send_telegram_message(
            chat_id,
            "🔗 Подключение GitHub\n\n"
            "1️⃣ Зайди в репозиторий GitHub\n"
            "2️⃣ Settings → Webhooks → Add webhook\n"
            "3️⃣ Payload URL:\n"
            f"{webhook_url}\n"
            "4️⃣ Content type: application/json\n"
            "5️⃣ Events: Push\n\n"
            "После этого коммиты начнут приходить сюда 👇"
        )

    # ---- Помощь ----
    elif text == "ℹ️ Помощь":
        send_telegram_message(
            chat_id,
            "ℹ️ Помощь\n\n"
            "• Подключай GitHub и получай уведомления о коммитах\n"
            "• Каждому пользователю — своя ссылка\n"
            "• Сервис работает 24/7"
        )

    return {"ok": True}


# =========================
# GITHUB WEBHOOK
# =========================
@app.post("/webhook/github/{webhook_id}")
async def github_webhook(webhook_id: str, payload: dict = Body(...)):
    # найти webhook
    webhook = supabase.table("webhooks") \
        .select("user_id") \
        .eq("id", webhook_id) \
        .single() \
        .execute()

    if not webhook.data:
        return {"status": "unknown webhook"}

    user_id = webhook.data["user_id"]

    # найти пользователя
    user = supabase.table("users") \
        .select("chat_id") \
        .eq("id", user_id) \
        .single() \
        .execute()

    chat_id = user.data["chat_id"]

    # данные из GitHub payload
    repo = payload.get("repository", {}).get("name", "unknown")
    sender = payload.get("sender", {}).get("login", "unknown")

    message = (
        "🔔 GitHub push event\n"
        f"Repository: {repo}\n"
        f"Author: {sender}"
    )

    send_telegram_message(chat_id, message)

    return {"status": "ok"}
