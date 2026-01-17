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
# TEMP STATE (MVP)
# =========================
PENDING_DELETE = {}  # chat_id -> webhook_id

# =========================
# KEYBOARDS
# =========================
def main_keyboard():
    return {
        "keyboard": [
            [{"text": "➕ Подключить сервис"}],
            [{"text": "📦 Мои сервисы"}],
            [{"text": "ℹ️ Помощь"}]
        ],
        "resize_keyboard": True
    }

def services_keyboard():
    return {
        "keyboard": [
            [{"text": "GitHub"}, {"text": "Webhook (custom)"}],
            [{"text": "⬅️ Назад"}]
        ],
        "resize_keyboard": True
    }

def confirm_keyboard():
    return {
        "keyboard": [
            [{"text": "ДА"}, {"text": "НЕТ"}],
            [{"text": "⬅️ Назад"}]
        ],
        "resize_keyboard": True
    }

# =========================
# TELEGRAM HELPER
# =========================
def send_message(chat_id: int, text: str, keyboard: dict | None = None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if keyboard:
        payload["reply_markup"] = keyboard
    requests.post(url, json=payload)

# =========================
# HEALTH
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
    text = message.get("text", "").strip()

    # upsert user
    supabase.table("users").upsert(
        {"chat_id": chat_id},
        on_conflict="chat_id"
    ).execute()

    # =========================
    # BACK BUTTON
    # =========================
    if text == "⬅️ Назад":
        if chat_id in PENDING_DELETE:
            del PENDING_DELETE[chat_id]

        send_message(
            chat_id,
            "🔁 Возвращаюсь в главное меню.",
            main_keyboard()
        )
        return {"ok": True}

    # =========================
    # DELETE CONFIRM FLOW
    # =========================
    if chat_id in PENDING_DELETE:
        webhook_id = PENDING_DELETE[chat_id]

        if text == "ДА":
            supabase.table("webhooks").delete().eq("id", webhook_id).execute()
            del PENDING_DELETE[chat_id]
            send_message(chat_id, "✅ Сервис удалён.", main_keyboard())
            return {"ok": True}

        if text == "НЕТ":
            del PENDING_DELETE[chat_id]
            send_message(chat_id, "❎ Удаление отменено.", main_keyboard())
            return {"ok": True}

        send_message(chat_id, "⚠️ Напиши **ДА**, **НЕТ** или нажми ⬅️ Назад.", confirm_keyboard())
        return {"ok": True}

    # =========================
    # MAIN MENU
    # =========================
    if text in ("/start", "Главное меню"):
        send_message(
            chat_id,
            "👋 *Integration Hub*\n\n"
            "Подключай сервисы и получай события в Telegram.",
            main_keyboard()
        )
        return {"ok": True}

    if text == "➕ Подключить сервис":
        send_message(
            chat_id,
            "➕ *Подключение сервиса*\n\nВыбери сервис:",
            services_keyboard()
        )
        return {"ok": True}

    # =========================
    # CONNECT SERVICES
    # =========================
    if text == "GitHub":
        user = supabase.table("users").select("id").eq("chat_id", chat_id).execute()
        user_id = user.data[0]["id"]

        webhook = supabase.table("webhooks").insert({
            "user_id": user_id,
            "source": "github"
        }).execute()

        webhook_id = webhook.data[0]["id"]
        url = f"{BASE_URL}/webhook/github/{webhook_id}"

        send_message(
            chat_id,
            f"🔗 *GitHub подключение*\n\nPayload URL:\n`{url}`",
            main_keyboard()
        )
        return {"ok": True}

    if text == "Webhook (custom)":
        user = supabase.table("users").select("id").eq("chat_id", chat_id).execute()
        user_id = user.data[0]["id"]

        webhook = supabase.table("webhooks").insert({
            "user_id": user_id,
            "source": "custom"
        }).execute()

        webhook_id = webhook.data[0]["id"]
        url = f"{BASE_URL}/webhook/custom/{webhook_id}"

        send_message(
            chat_id,
            f"🔔 *Custom Webhook*\n\nPOST JSON сюда:\n`{url}`",
            main_keyboard()
        )
        return {"ok": True}

    # =========================
    # LIST SERVICES
    # =========================
    if text == "📦 Мои сервисы":
        user = supabase.table("users").select("id").eq("chat_id", chat_id).execute()
        user_id = user.data[0]["id"]

        services = supabase.table("webhooks").select("id,source").eq("user_id", user_id).execute()

        if not services.data:
            send_message(chat_id, "У тебя нет подключённых сервисов.", main_keyboard())
            return {"ok": True}

        out = "📦 *Твои сервисы:*\n\n"
        for s in services.data:
            out += f"• `{s['source']}` — `{s['id']}`\n"

        out += "\n❌ Отправь ID сервиса, который хочешь удалить."

        send_message(chat_id, out, main_keyboard())
        return {"ok": True}

    # =========================
    # DELETE STEP 1 (ID)
    # =========================
    if len(text) == 36 and "-" in text:
        PENDING_DELETE[chat_id] = text
        send_message(
            chat_id,
            f"⚠️ Ты точно хочешь удалить сервис:\n`{text}`",
            confirm_keyboard()
        )
        return {"ok": True}

    # =========================
    # HELP
    # =========================
    if text == "ℹ️ Помощь":
        send_message(
            chat_id,
            "ℹ️ *Помощь*\n\n"
            "• Используй кнопки\n"
            "• Удаление требует подтверждения\n"
            "• «Назад» возвращает в меню",
            main_keyboard()
        )
        return {"ok": True}

    send_message(chat_id, "Используй кнопки ниже 👇", main_keyboard())
    return {"ok": True}

# =========================
# WEBHOOKS
# =========================
@app.post("/webhook/github/{webhook_id}")
async def github_webhook(webhook_id: str, payload: dict = Body(...)):
    wh = supabase.table("webhooks").select("user_id").eq("id", webhook_id).execute()
    if not wh.data:
        return {"status": "unknown webhook"}

    user_id = wh.data[0]["user_id"]
    user = supabase.table("users").select("chat_id").eq("id", user_id).execute()
    chat_id = user.data[0]["chat_id"]

    repo = payload.get("repository", {}).get("name", "unknown")
    author = payload.get("sender", {}).get("login", "unknown")

    send_message(chat_id, f"🔔 *GitHub push*\nRepository: `{repo}`\nAuthor: `{author}`")
    return {"status": "ok"}

@app.post("/webhook/custom/{webhook_id}")
async def custom_webhook(webhook_id: str, payload: dict = Body(...)):
    wh = supabase.table("webhooks").select("user_id").eq("id", webhook_id).execute()
    if not wh.data:
        return {"status": "unknown webhook"}

    user_id = wh.data[0]["user_id"]
    user = supabase.table("users").select("chat_id").eq("id", user_id).execute()
    chat_id = user.data[0]["chat_id"]

    send_message(chat_id, f"🔔 *Custom webhook*\n```{payload}```")
    return {"status": "ok"}
