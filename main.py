import os
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
# TELEGRAM HELPERS
# =========================
def send_message(chat_id: int, text: str, keyboard: dict | None = None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if keyboard:
        payload["reply_markup"] = keyboard
    requests.post(url, json=payload)


def edit_message(chat_id: int, message_id: int, text: str, keyboard: dict | None = None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text
    }
    if keyboard:
        payload["reply_markup"] = keyboard
    requests.post(url, json=payload)


# =========================
# KEYBOARDS
# =========================
def main_menu_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "➕ Подключить сервис", "callback_data": "connect_service"}],
            [{"text": "📦 Мои сервисы", "callback_data": "my_services"}],
            [{"text": "ℹ️ Помощь", "callback_data": "help"}]
        ]
    }


def services_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "GitHub", "callback_data": "connect_github"}],
            [{"text": "GitLab (скоро)", "callback_data": "soon"}],
            [{"text": "Notion (скоро)", "callback_data": "soon"}],
            [{"text": "Stripe (скоро)", "callback_data": "soon"}],
            [{"text": "Webhook (custom)", "callback_data": "soon"}],
            [{"text": "⬅️ Назад", "callback_data": "back_to_menu"}]
        ]
    }


def back_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "⬅️ Назад", "callback_data": "back_to_menu"}]
        ]
    }


# =========================
# HEALTH (cron / monitoring)
# =========================
@app.get("/health")
async def health():
    return {"status": "ok"}


# =========================
# TELEGRAM WEBHOOK
# =========================
@app.post("/telegram")
async def telegram_update(payload: dict = Body(...)):
    # ---------- MESSAGE ----------
    if "message" in payload:
        message = payload["message"]
        chat_id = message["chat"]["id"]
        text = message.get("text", "")

        # регистрация пользователя (idempotent)
        supabase.table("users").upsert({
            "chat_id": chat_id
        }).execute()

        if text == "/start":
            send_message(
                chat_id,
                "👋 *Integration Hub*\n\n"
                "Подключай сервисы и получай уведомления прямо в Telegram.\n\n"
                "Выбери действие ниже 👇",
                main_menu_keyboard()
            )

        return {"ok": True}

    # ---------- CALLBACK QUERY ----------
    if "callback_query" in payload:
        callback = payload["callback_query"]
        data = callback["data"]
        chat_id = callback["message"]["chat"]["id"]
        message_id = callback["message"]["message_id"]

        # Главное меню
        if data == "back_to_menu":
            edit_message(
                chat_id,
                message_id,
                "👋 *Integration Hub*\n\n"
                "Выбери действие 👇",
                main_menu_keyboard()
            )

        # Подключить сервис
        elif data == "connect_service":
            edit_message(
                chat_id,
                message_id,
                "➕ *Подключение сервиса*\n\n"
                "Выбери сервис:",
                services_keyboard()
            )

        # GitHub
        elif data == "connect_github":
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

            edit_message(
                chat_id,
                message_id,
                "🔗 *GitHub подключение*\n\n"
                "1️⃣ Зайди в репозиторий GitHub\n"
                "2️⃣ Settings → Webhooks → Add webhook\n"
                "3️⃣ Payload URL:\n"
                f"{webhook_url}\n"
                "4️⃣ Content type: application/json\n"
                "5️⃣ Events: Push\n\n"
                "После этого коммиты начнут приходить сюда 👇",
                back_keyboard()
            )

        # Мои сервисы
        elif data == "my_services":
            services = supabase.table("webhooks") \
                .select("source") \
                .execute()

            if services.data:
                text = "📦 *Ваши сервисы:*\n\n"
                for s in services.data:
                    text += f"• {s['source']} — ✅ активен\n"
            else:
                text = "📦 *У вас пока нет подключённых сервисов*"

            edit_message(
                chat_id,
                message_id,
                text,
                back_keyboard()
            )

        # Помощь
        elif data == "help":
            edit_message(
                chat_id,
                message_id,
                "ℹ️ *Помощь*\n\n"
                "• Подключай сервисы кнопками\n"
                "• У каждого сервиса своя ссылка\n"
                "• Уведомления приходят автоматически\n\n"
                "Скоро появятся новые интеграции 🚀",
                back_keyboard()
            )

        # Заглушка
        elif data == "soon":
            edit_message(
                chat_id,
                message_id,
                "⏳ Этот сервис скоро будет доступен.\n\n"
                "GitHub уже работает ✅",
                back_keyboard()
            )

        return {"ok": True}

    return {"ok": True}


# =========================
# GITHUB WEBHOOK
# =========================
@app.post("/webhook/github/{webhook_id}")
async def github_webhook(webhook_id: str, payload: dict = Body(...)):
    webhook = supabase.table("webhooks") \
        .select("user_id") \
        .eq("id", webhook_id) \
        .single() \
        .execute()

    if not webhook.data:
        return {"status": "unknown webhook"}

    user_id = webhook.data["user_id"]

    user = supabase.table("users") \
        .select("chat_id") \
        .eq("id", user_id) \
        .single() \
        .execute()

    chat_id = user.data["chat_id"]

    repo = payload.get("repository", {}).get("name", "unknown")
    author = payload.get("sender", {}).get("login", "unknown")

    message = (
        "🔔 *GitHub push*\n"
        f"Repository: {repo}\n"
        f"Author: {author}"
    )

    send_message(chat_id, message)
    return {"status": "ok"}
