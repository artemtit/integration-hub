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
BASE_URL = os.getenv("BASE_URL")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI()

# =========================
# TEMP STATE
# =========================
GITHUB_EVENTS = {}          # event_id -> {payload, created_at}
PENDING_DELETE = {}        # chat_id -> webhook_id
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
def send_message(chat_id: int, text: str, keyboard: dict | None = None):
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "reply_markup": keyboard
        } if keyboard else {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown"
        }
    )

def answer_callback(callback_id: str):
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
        "inline_keyboard": [[
            {"text": "📄 Подробнее", "callback_data": f"github_details:{event_id}"},
            {"text": "🌐 Открыть на GitHub", "url": url}
        ]]
    }

def event_open_keyboard(event_id: str):
    return {
        "inline_keyboard": [[
            {"text": "📄 Открыть событие", "callback_data": f"open_event:{event_id}"}
        ]]
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
        answer_callback(callback["id"])

        # GITHUB DETAILS (TTL)
        if data.startswith("github_details:"):
            event_id = data.split(":", 1)[1]
            ev = GITHUB_EVENTS.get(event_id)
            if not ev:
                send_message(chat_id, "⌛ Детали устарели")
                return {"ok": True}

            p = ev["payload"]
            text = (
                "📄 *GitHub — подробности*\n\n"
                f"Repo: `{p['repository']['name']}`\n"
                f"Branch: `{p.get('ref','').replace('refs/heads/','')}`\n"
                f"Author: `{p['sender']['login']}`\n\n"
                "Коммиты:\n"
            )
            for i, c in enumerate(p.get("commits", []), 1):
                text += f"{i}) {c['message'].splitlines()[0]}\n"

            send_message(chat_id, text)
            return {"ok": True}

        # OPEN EVENT (HISTORY)
        if data.startswith("open_event:"):
            event_id = data.split(":", 1)[1]
            res = supabase.table("events").select("source,title,created_at").eq("id", event_id).execute()
            if not res.data:
                send_message(chat_id, "❌ Событие не найдено")
                return {"ok": True}

            e = res.data[0]
            send_message(
                chat_id,
                "📄 *Событие*\n\n"
                f"Источник: `{e['source']}`\n"
                f"Дата: `{e['created_at'].split('T')[0]}`\n"
                f"Описание:\n`{e['title']}`"
            )
            return {"ok": True}

    # ---------- MESSAGE ----------
    message = payload.get("message")
    if not message:
        return {"ok": True}

    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()

    supabase.table("users").upsert({"chat_id": chat_id}, on_conflict="chat_id").execute()

    # DELETE CONFIRM
    if chat_id in PENDING_DELETE:
        wid = PENDING_DELETE[chat_id]
        if text == "ДА":
            supabase.table("webhooks").delete().eq("id", wid).execute()
            PENDING_DELETE.pop(chat_id)
            send_message(chat_id, "✅ Сервис удалён", main_keyboard())
            return {"ok": True}
        if text == "НЕТ":
            PENDING_DELETE.pop(chat_id)
            send_message(chat_id, "❎ Удаление отменено", main_keyboard())
            return {"ok": True}
        send_message(chat_id, "Напиши ДА или НЕТ", confirm_keyboard())
        return {"ok": True}

    # START / BACK
    if text in ("/start", "Главное меню", "⬅️ Назад"):
        send_message(chat_id, "👋 *Integration Hub*", main_keyboard())
        return {"ok": True}

    if text == "➕ Подключить сервис":
        send_message(chat_id, "Выбери сервис:", services_keyboard())
        return {"ok": True}

    # GITHUB INSTRUCTION
    if text == "GitHub":
        user_id = supabase.table("users").select("id").eq("chat_id", chat_id).execute().data[0]["id"]
        wh = supabase.table("webhooks").insert({
            "user_id": user_id,
            "source": "github",
            "connected": False
        }).execute()

        url = f"{BASE_URL}/webhook/github/{wh.data[0]['id']}"
        send_message(
            chat_id,
            "🔗 *Подключение GitHub*\n\n"
            "Payload URL:\n"
            f"`{url}`\n\n"
            "Content type: `application/json`\n"
            "Event: Push",
            main_keyboard()
        )
        return {"ok": True}

    # SERVICES LIST
    if text == "📦 Мои сервисы":
        user_id = supabase.table("users").select("id").eq("chat_id", chat_id).execute().data[0]["id"]
        res = supabase.table("webhooks").select("id,source,connected").eq("user_id", user_id).execute()

        if not res.data:
            send_message(chat_id, "Нет сервисов", main_keyboard())
            return {"ok": True}

        out = "📦 *Твои сервисы:*\n\n"
        for s in res.data:
            out += f"{'🟢' if s['connected'] else '🔴'} `{s['source']}` — `{s['id']}`\n"

        out += "\n❌ Отправь ID сервиса для удаления"
        send_message(chat_id, out, main_keyboard())
        return {"ok": True}

    # DELETE STEP 1
    if len(text) == 36 and "-" in text:
        PENDING_DELETE[chat_id] = text
        send_message(chat_id, f"⚠️ Удалить сервис `{text}` ?", confirm_keyboard())
        return {"ok": True}

    # HELP
    if text == "ℹ️ Помощь":
        send_message(
            chat_id,
            "ℹ️ *Помощь*\n\n"
            "🟢 подключён\n"
            "🔴 ожидает подключения\n\n"
            "📜 история событий\n"
            "❌ удаление по ID",
            main_keyboard()
        )
        return {"ok": True}

    # LAST EVENTS
    if text == "📜 Последние уведомления":
        user_id = supabase.table("users").select("id").eq("chat_id", chat_id).execute().data[0]["id"]
        evs = supabase.table("events").select("id,title,source").eq("user_id", user_id).order("created_at", desc=True).limit(10).execute()

        if not evs.data:
            send_message(chat_id, "Пока нет событий", main_keyboard())
            return {"ok": True}

        send_message(chat_id, "📜 *Последние уведомления:*", main_keyboard())
        for e in evs.data:
            send_message(chat_id, f"{'🐙' if e['source']=='github' else '🔔'} {e['title']}", event_open_keyboard(e["id"]))
        return {"ok": True}

    send_message(chat_id, "Используй кнопки ниже 👇", main_keyboard())
    return {"ok": True}

# =========================
# GITHUB WEBHOOK
# =========================
@app.post("/webhook/github/{webhook_id}")
async def github_webhook(webhook_id: str, request: Request):
    payload = await request.json()
    wh = supabase.table("webhooks").select("user_id").eq("id", webhook_id).execute()
    if not wh.data:
        return {"status": "unknown"}

    supabase.table("webhooks").update({"connected": True}).eq("id", webhook_id).execute()

    if request.headers.get("X-GitHub-Event") == "ping":
        return {"status": "verified"}

    event_id = str(uuid.uuid4())
    GITHUB_EVENTS[event_id] = {"payload": payload, "created_at": datetime.utcnow()}

    user_id = wh.data[0]["user_id"]
    chat_id = supabase.table("users").select("chat_id").eq("id", user_id).execute().data[0]["chat_id"]

    repo = payload["repository"]["name"]
    author = payload["sender"]["login"]
    commits = len(payload.get("commits", []))
    url = payload["repository"]["html_url"]

    supabase.table("events").insert({
        "user_id": user_id,
        "source": "github",
        "title": f"GitHub push: {repo}",
        "data": payload
    }).execute()

    send_message(
        chat_id,
        f"🔔 *GitHub push*\n\nRepo: `{repo}`\nAuthor: `{author}`\nCommits: `{commits}`",
        github_event_keyboard(event_id, url)
    )
    return {"status": "ok"}
