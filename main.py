import os
import uuid
import time
import threading
import requests
from datetime import datetime, timedelta
from fastapi import FastAPI, Body, Request
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI()

GITHUB_EVENTS = {}
PENDING_DELETE = {}
TTL_HOURS = 24


# =========================
# TTL CLEANER
# =========================
def ttl_cleaner():
    while True:
        now = datetime.utcnow()
        for k, v in list(GITHUB_EVENTS.items()):
            if now - v["created_at"] > timedelta(hours=TTL_HOURS):
                del GITHUB_EVENTS[k]
        time.sleep(600)

threading.Thread(target=ttl_cleaner, daemon=True).start()


# =========================
# TELEGRAM HELPERS
# =========================
def send_message(chat_id, text, keyboard=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if keyboard:
        payload["reply_markup"] = keyboard

    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json=payload
    )


def edit_message(chat_id, message_id, text, keyboard):
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText",
        json={
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "Markdown",
            "reply_markup": keyboard
        }
    )


def answer_callback(callback_id):
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery",
        json={"callback_query_id": callback_id}
    )


# =========================
# KEYBOARDS
# =========================
def main_keyboard():
    return {
        "keyboard": [
            [{"text": "➕ Подключить сервис"}],
            [{"text": "📦 Мои сервисы"}],
            [{"text": "📜 Последние уведомления"}],
            [{"text": "❓ Популярные вопросы"}]
        ],
        "resize_keyboard": True
    }


def service_row_keyboard(webhook_id):
    return {
        "inline_keyboard": [[
            {"text": "⚙️ Управление сервисом", "callback_data": f"manage:{webhook_id}"}
        ]]
    }


def service_settings_keyboard(webhook_id, enabled):
    return {
        "inline_keyboard": [
            [{
                "text": f"🔔 Уведомления: {'ВКЛ' if enabled else 'ВЫКЛ'}",
                "callback_data": f"toggle:{webhook_id}"
            }],
            [{
                "text": "❌ Удалить сервис",
                "callback_data": f"delete:{webhook_id}"
            }],
            [{
                "text": "⬅️ Назад",
                "callback_data": "back"
            }]
        ]
    }


# =========================
# TELEGRAM WEBHOOK
# =========================
@app.post("/telegram")
async def telegram_update(payload: dict = Body(...)):
    # ---------- CALLBACK ----------
    callback = payload.get("callback_query")
    if callback:
        data = callback["data"]
        chat_id = callback["message"]["chat"]["id"]
        message_id = callback["message"]["message_id"]
        answer_callback(callback["id"])

        # BACK
        if data == "back":
            show_services(chat_id)
            return {"ok": True}

        # OPEN SETTINGS
        if data.startswith("manage:"):
            wid = data.split(":", 1)[1]
            wh = supabase.table("webhooks") \
                .select("notifications_enabled,display_name") \
                .eq("id", wid).execute().data[0]

            edit_message(
                chat_id,
                message_id,
                f"⚙️ *Управление сервисом*\n\n{wh['display_name']}",
                service_settings_keyboard(wid, wh["notifications_enabled"])
            )
            return {"ok": True}

        # TOGGLE NOTIFICATIONS (ONLY BUTTON CHANGES)
        if data.startswith("toggle:"):
            wid = data.split(":", 1)[1]
            wh = supabase.table("webhooks") \
                .select("notifications_enabled,display_name") \
                .eq("id", wid).execute().data[0]

            new_state = not wh["notifications_enabled"]

            supabase.table("webhooks").update({
                "notifications_enabled": new_state
            }).eq("id", wid).execute()

            edit_message(
                chat_id,
                message_id,
                f"⚙️ *Управление сервисом*\n\n{wh['display_name']}",
                service_settings_keyboard(wid, new_state)
            )
            return {"ok": True}

        # DELETE
        if data.startswith("delete:"):
            wid = data.split(":", 1)[1]
            PENDING_DELETE[chat_id] = wid
            send_message(chat_id, "⚠️ Удалить сервис? Напиши ДА или НЕТ")
            return {"ok": True}

    # ---------- MESSAGE ----------
    message = payload.get("message")
    if not message:
        return {"ok": True}

    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()

    supabase.table("users").upsert(
        {"chat_id": chat_id},
        on_conflict="chat_id"
    ).execute()

    if chat_id in PENDING_DELETE:
        if text == "ДА":
            supabase.table("webhooks").delete().eq("id", PENDING_DELETE[chat_id]).execute()
            PENDING_DELETE.pop(chat_id)
            send_message(chat_id, "✅ Сервис удалён", main_keyboard())
            return {"ok": True}
        if text == "НЕТ":
            PENDING_DELETE.pop(chat_id)
            send_message(chat_id, "❎ Удаление отменено", main_keyboard())
            return {"ok": True}

    if text in ("/start", "⬅️ Назад"):
        send_message(
            chat_id,
            "👋 *Добро пожаловать в Integration Hub!*\n\n"
            "Я помогу получать события из GitHub и других сервисов прямо в Telegram.\n\n"
            "🔹 Подключай сервисы\n"
            "🔹 Получай уведомления\n"
            "🔹 Управляй всем из одного бота",
            main_keyboard()
        )
        return {"ok": True}

    if text == "📦 Мои сервисы":
        show_services(chat_id)
        return {"ok": True}

    send_message(chat_id, "Используй кнопки ниже 👇", main_keyboard())
    return {"ok": True}


# =========================
# HELPERS
# =========================
def show_services(chat_id):
    user_id = supabase.table("users").select("id").eq("chat_id", chat_id).execute().data[0]["id"]
    services = supabase.table("webhooks") \
        .select("id,display_name,connected") \
        .eq("user_id", user_id).execute().data

    if not services:
        send_message(chat_id, "📦 У тебя пока нет сервисов.\n\nНапиши @ligr5", main_keyboard())
        return

    send_message(chat_id, "📦 *Твои сервисы:*", main_keyboard())
    for s in services:
        status = "🟢" if s["connected"] else "🔴"
        send_message(
            chat_id,
            f"{status} {s['display_name']}",
            service_row_keyboard(s["id"])
        )


# =========================
# GITHUB WEBHOOK
# =========================
@app.post("/webhook/github/{webhook_id}")
async def github_webhook(webhook_id: str, request: Request):
    payload = await request.json()
    event = request.headers.get("X-GitHub-Event")

    wh = supabase.table("webhooks") \
        .select("user_id,notifications_enabled") \
        .eq("id", webhook_id).execute()

    if not wh.data:
        return {"status": "unknown"}

    notify = wh.data[0]["notifications_enabled"]
    user_id = wh.data[0]["user_id"]

    repo = payload["repository"]["name"]
    repo_url = payload["repository"]["html_url"]

    supabase.table("webhooks").update({
        "connected": True,
        "display_name": f"GitHub ({repo})"
    }).eq("id", webhook_id).execute()

    if not notify:
        return {"status": "ok"}

    chat_id = supabase.table("users").select("chat_id").eq("id", user_id).execute().data[0]["chat_id"]

    title = f"🔔 GitHub событие\n\n📦 Репозиторий:\n{repo}"
    event_id = str(uuid.uuid4())

    GITHUB_EVENTS[event_id] = {"payload": payload, "created_at": datetime.utcnow()}

    send_message(chat_id, title)
    return {"status": "ok"}
