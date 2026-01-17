import os
import uuid
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
# TEMP STORAGE (MVP)
# =========================
PENDING_DELETE = {}          # chat_id -> webhook_id
GITHUB_EVENTS = {}           # event_id -> payload

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

def github_event_keyboard(event_id: str, url: str):
    return {
        "inline_keyboard": [
            [
                {"text": "📄 Подробнее", "callback_data": f"github_details:{event_id}"},
                {"text": "🌐 Открыть на GitHub", "url": url}
            ]
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
    # ---------- CALLBACK ----------
    callback = payload.get("callback_query")
    if callback:
        data = callback["data"]
        chat_id = callback["message"]["chat"]["id"]

        if data.startswith("github_details:"):
            event_id = data.split(":", 1)[1]
            event = GITHUB_EVENTS.get(event_id)

            if not event:
                send_message(chat_id, "❌ Детали недоступны")
                return {"ok": True}

            repo = event.get("repository", {}).get("name", "unknown")
            branch = event.get("ref", "").replace("refs/heads/", "")
            pusher = event.get("sender", {}).get("login", "unknown")

            text = (
                "📄 *GitHub — подробности*\n\n"
                f"• Repo: `{repo}`\n"
                f"• Branch: `{branch}`\n"
                f"• Pusher: `{pusher}`\n\n"
                "Коммиты:\n"
            )

            for i, c in enumerate(event.get("commits", []), 1):
                msg = c.get("message", "").split("\n")[0]
                text += f"{i}) {msg}\n"

            send_message(chat_id, text)
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

    # BACK
    if text == "⬅️ Назад":
        PENDING_DELETE.pop(chat_id, None)
        send_message(chat_id, "🔁 Главное меню", main_keyboard())
        return {"ok": True}

    # DELETE CONFIRM
    if chat_id in PENDING_DELETE:
        webhook_id = PENDING_DELETE[chat_id]

        if text == "ДА":
            supabase.table("webhooks").delete().eq("id", webhook_id).execute()
            PENDING_DELETE.pop(chat_id, None)
            send_message(chat_id, "✅ Сервис удалён", main_keyboard())
            return {"ok": True}

        if text == "НЕТ":
            PENDING_DELETE.pop(chat_id, None)
            send_message(chat_id, "❎ Удаление отменено", main_keyboard())
            return {"ok": True}

        send_message(chat_id, "Напиши **ДА** или **НЕТ**", confirm_keyboard())
        return {"ok": True}

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

    # CONNECT GITHUB
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
            "🔗 *Подключение GitHub*\n\n"
            "1️⃣ Репозиторий → Settings → Webhooks\n"
            "2️⃣ Add webhook\n"
            "3️⃣ Payload URL:\n"
            f"`{url}`\n\n"
            "4️⃣ Content type: `application/json`\n"
            "5️⃣ Events: Push\n\n"
            "⏳ Ожидается ping",
            main_keyboard()
        )
        return {"ok": True}

    # CONNECT CUSTOM
    if text == "Webhook (custom)":
        user_id = supabase.table("users").select("id").eq("chat_id", chat_id).execute().data[0]["id"]

        webhook = supabase.table("webhooks").insert({
            "user_id": user_id,
            "source": "custom",
            "connected": False
        }).execute()

        url = f"{BASE_URL}/webhook/custom/{webhook.data[0]['id']}"

        send_message(
            chat_id,
            "🔔 *Custom Webhook*\n\n"
            "Отправляй POST JSON сюда:\n"
            f"`{url}`",
            main_keyboard()
        )
        return {"ok": True}

    # LIST SERVICES
    if text == "📦 Мои сервисы":
        user_id = supabase.table("users").select("id").eq("chat_id", chat_id).execute().data[0]["id"]
        limit = (datetime.utcnow() - timedelta(minutes=30)).isoformat()

        services = supabase.table("webhooks") \
            .select("id, source, connected, created_at") \
            .eq("user_id", user_id) \
            .or_(f"connected.eq.true,and(connected.eq.false,created_at.gte.{limit})") \
            .execute()

        if not services.data:
            send_message(chat_id, "Нет активных сервисов", main_keyboard())
            return {"ok": True}

        out = "📦 *Твои сервисы:*\n\n"
        for s in services.data:
            status = "🟢" if s["connected"] else "🔴"
            out += f"{status} `{s['source']}` — `{s['id']}`\n"

        out += "\n❌ Отправь ID сервиса для удаления"
        send_message(chat_id, out, main_keyboard())
        return {"ok": True}

    # DELETE STEP 1
    if len(text) == 36 and "-" in text:
        PENDING_DELETE[chat_id] = text
        send_message(chat_id, f"⚠️ Удалить сервис:\n`{text}` ?", confirm_keyboard())
        return {"ok": True}

    if text == "ℹ️ Помощь":
        send_message(
            chat_id,
            "ℹ️ *Помощь*\n\n🟢 подключён\n🔴 ожидает подключения\n\n"
            "Неподключённые исчезают через 30 минут",
            main_keyboard()
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
    event = request.headers.get("X-GitHub-Event")

    wh = supabase.table("webhooks").select("user_id, connected").eq("id", webhook_id).execute()
    if not wh.data:
        return {"status": "unknown webhook"}

    supabase.table("webhooks").update({"connected": True}).eq("id", webhook_id).execute()

    if event == "ping":
        return {"status": "verified"}

    event_id = str(uuid.uuid4())
    GITHUB_EVENTS[event_id] = payload

    user_id = wh.data[0]["user_id"]
    chat_id = supabase.table("users").select("chat_id").eq("id", user_id).execute().data[0]["chat_id"]

    repo = payload.get("repository", {}).get("name", "unknown")
    author = payload.get("sender", {}).get("login", "unknown")
    commits = len(payload.get("commits", []))
    repo_url = payload.get("repository", {}).get("html_url", "https://github.com")

    send_message(
        chat_id,
        f"🔔 *GitHub push*\n\n"
        f"Repo: `{repo}`\n"
        f"Author: `{author}`\n"
        f"Commits: `{commits}`",
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
        return {"status": "unknown webhook"}

    supabase.table("webhooks").update({"connected": True}).eq("id", webhook_id).execute()

    user_id = wh.data[0]["user_id"]
    chat_id = supabase.table("users").select("chat_id").eq("id", user_id).execute().data[0]["chat_id"]

    send_message(chat_id, f"🔔 Custom webhook\n```{payload}```")
    return {"status": "ok"}
