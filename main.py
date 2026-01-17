import os
import uuid
import time
import threading
import requests
from datetime import datetime, timedelta
from fastapi import FastAPI, Body, Request
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
# TEMP STORAGE (TTL 24h)
# =========================
GITHUB_EVENTS = {}   # event_id -> {payload, created_at}
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
# KEYBOARDS
# =========================
def main_keyboard():
    return {
        "keyboard": [
            [{"text": "➕ Подключить сервис"}],
            [{"text": "📦 Мои сервисы"}],
            [{"text": "📜 Последние уведомления"}],
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

def github_event_keyboard(event_id: str, url: str):
    return {
        "inline_keyboard": [
            [
                {"text": "📄 Подробнее", "callback_data": f"github_details:{event_id}"},
                {"text": "🌐 Открыть на GitHub", "url": url}
            ]
        ]
    }

def event_open_keyboard(event_id: str):
    return {
        "inline_keyboard": [
            [{"text": "📄 Открыть событие", "callback_data": f"open_event:{event_id}"}]
        ]
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
# TELEGRAM WEBHOOK
# =========================
@app.post("/telegram")
async def telegram_update(payload: dict = Body(...)):
    # ---------- CALLBACK ----------
    callback = payload.get("callback_query")
    if callback:
        data = callback["data"]
        chat_id = callback["message"]["chat"]["id"]

        # DETAILS (TTL storage)
        if data.startswith("github_details:"):
            event_id = data.split(":", 1)[1]
            event = GITHUB_EVENTS.get(event_id)

            if not event:
                send_message(chat_id, "⌛ Детали устарели")
                return {"ok": True}

            payload = event["payload"]
            repo = payload.get("repository", {}).get("name", "unknown")
            branch = payload.get("ref", "").replace("refs/heads/", "")
            pusher = payload.get("sender", {}).get("login", "unknown")

            text = (
                "📄 *GitHub — подробности*\n\n"
                f"Repo: `{repo}`\n"
                f"Branch: `{branch}`\n"
                f"Pusher: `{pusher}`\n\n"
                "Коммиты:\n"
            )

            for i, c in enumerate(payload.get("commits", []), 1):
                msg = c.get("message", "").split("\n")[0]
                text += f"{i}) {msg}\n"

            send_message(chat_id, text)
            return {"ok": True}

        # OPEN EVENT (FROM HISTORY)
        if data.startswith("open_event:"):
            event_id = data.split(":", 1)[1]

            event = supabase.table("events").select("*").eq("id", event_id).execute()
            if not event.data:
                send_message(chat_id, "❌ Событие не найдено")
                return {"ok": True}

            e = event.data[0]
            created = e["created_at"].split("T")[0]

            text = (
                "📄 *Событие*\n\n"
                f"Источник: `{e['source']}`\n"
                f"Дата: `{created}`\n\n"
                f"```{e['data']}```"
            )

            send_message(chat_id, text)
            return {"ok": True}

    # ---------- MESSAGE ----------
    message = payload.get("message")
    if not message:
        return {"ok": True}

    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()

    supabase.table("users").upsert({"chat_id": chat_id}, on_conflict="chat_id").execute()

    # START
    if text in ("/start", "Главное меню"):
        send_message(
            chat_id,
            "👋 *Integration Hub*\n\nПодключай сервисы и получай события в Telegram.",
            main_keyboard()
        )
        return {"ok": True}

    if text == "➕ Подключить сервис":
        send_message(chat_id, "Выбери сервис:", services_keyboard())
        return {"ok": True}

    if text == "GitHub":
        user_id = supabase.table("users").select("id").eq("chat_id", chat_id).execute().data[0]["id"]

        webhook = supabase.table("webhooks").insert({
            "user_id": user_id,
            "source": "github",
            "connected": False
        }).execute()

        url = f"{BASE_URL}/webhook/github/{webhook.data[0]['id']}"

        send_message(
            chat_id,
            f"🔗 *Подключение GitHub*\n\nPayload URL:\n`{url}`",
            main_keyboard()
        )
        return {"ok": True}

    if text == "Webhook (custom)":
        user_id = supabase.table("users").select("id").eq("chat_id", chat_id).execute().data[0]["id"]

        webhook = supabase.table("webhooks").insert({
            "user_id": user_id,
            "source": "custom",
            "connected": False
        }).execute()

        url = f"{BASE_URL}/webhook/custom/{webhook.data[0]['id']}"

        send_message(chat_id, f"🔔 Custom Webhook:\n`{url}`", main_keyboard())
        return {"ok": True}

    # LAST EVENTS
    if text == "📜 Последние уведомления":
        user_id = supabase.table("users").select("id").eq("chat_id", chat_id).execute().data[0]["id"]

        events = supabase.table("events") \
            .select("id, source, title, created_at") \
            .eq("user_id", user_id) \
            .order("created_at", desc=True) \
            .limit(10) \
            .execute()

        if not events.data:
            send_message(chat_id, "Пока нет событий", main_keyboard())
            return {"ok": True}

        send_message(chat_id, "📜 *Последние уведомления:*", main_keyboard())

        for e in events.data:
            icon = "🐙" if e["source"] == "github" else "🔔"
            date = e["created_at"].split("T")[0]
            send_message(
                chat_id,
                f"{icon} {e['title']}\n🕒 {date}",
                event_open_keyboard(e["id"])
            )
        return {"ok": True}

    send_message(chat_id, "Используй кнопки ниже 👇", main_keyboard())
    return {"ok": True}

# =========================
# GITHUB WEBHOOK
# =========================
@app.post("/webhook/github/{webhook_id}")
async def github_webhook(webhook_id: str, request: Request):
    payload = await request.json()
    event_type = request.headers.get("X-GitHub-Event")

    wh = supabase.table("webhooks").select("user_id").eq("id", webhook_id).execute()
    if not wh.data:
        return {"status": "unknown"}

    supabase.table("webhooks").update({"connected": True}).eq("id", webhook_id).execute()

    if event_type == "ping":
        return {"status": "verified"}

    event_id = str(uuid.uuid4())
    GITHUB_EVENTS[event_id] = {
        "payload": payload,
        "created_at": datetime.utcnow()
    }

    user_id = wh.data[0]["user_id"]
    chat_id = supabase.table("users").select("chat_id").eq("id", user_id).execute().data[0]["chat_id"]

    repo = payload.get("repository", {}).get("name", "unknown")
    author = payload.get("sender", {}).get("login", "unknown")
    commits = len(payload.get("commits", []))
    repo_url = payload.get("repository", {}).get("html_url", "https://github.com")

    # SAVE EVENT
    supabase.table("events").insert({
        "user_id": user_id,
        "source": "github",
        "title": f"GitHub push: {repo}",
        "data": payload
    }).execute()

    send_message(
        chat_id,
        f"🔔 *GitHub push*\n\nRepo: `{repo}`\nAuthor: `{author}`\nCommits: `{commits}`",
        github_event_keyboard(event_id, repo_url)
    )
    return {"status": "ok"}

# =========================
# CUSTOM WEBHOOK
# =========================
@app.post("/webhook/custom/{webhook_id}")
async def custom_webhook(webhook_id: str, payload: dict = Body(...)):
    wh = supabase.table("webhooks").select("user_id").eq("id", webhook_id).execute()
    if not wh.data:
        return {"status": "unknown"}

    supabase.table("webhooks").update({"connected": True}).eq("id", webhook_id).execute()

    user_id = wh.data[0]["user_id"]
    chat_id = supabase.table("users").select("chat_id").eq("id", user_id).execute().data[0]["chat_id"]

    supabase.table("events").insert({
        "user_id": user_id,
        "source": "custom",
        "title": "Custom webhook event",
        "data": payload
    }).execute()

    send_message(chat_id, f"🔔 Custom webhook\n```{payload}```")
    return {"status": "ok"}
