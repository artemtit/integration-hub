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
# CONFIG / STATE
# =========================
GITHUB_EVENTS = {}      # event_id -> {"payload": ..., "created_at": datetime}
PENDING_DELETE = {}     # chat_id -> webhook_id (text-confirmation flow)
TTL_HOURS = 24
NOTIF_PAGE_SIZE = 3     # сколько уведомлений показываем по умолчанию

# =========================
# TTL CLEANER (in-memory details)
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
# HELPERS: формат времени, склонения
# =========================
def fmt_dt(dt):
    """Форматирует datetime или ISO-строку в 'DD.MM.YYYY HH:MM UTC' (без секунд)."""
    if not dt:
        return ""
    if isinstance(dt, datetime):
        return dt.strftime("%d.%m.%Y %H:%M UTC")
    try:
        base = dt.replace("T", " ").split(".")[0]  # YYYY-MM-DD HH:MM:SS
        date_part, time_part = base.split(" ")
        time_hm = ":".join(time_part.split(":")[:2])
        dt_obj = datetime.strptime(date_part, "%Y-%m-%d")
        return f"{dt_obj.strftime('%d.%m.%Y')} {time_hm} UTC"
    except Exception:
        return str(dt)

def pluralize_commits(n: int) -> str:
    """Возвращает: '1 новый коммит', '2 новых коммита', '5 новых коммитов'."""
    try:
        n = int(n)
    except Exception:
        return f"{n} новых коммитов"
    if n % 10 == 1 and n % 100 != 11:
        form = "новый коммит"
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        form = "новых коммита"
    else:
        form = "новых коммитов"
    return f"{n} {form}"

# =========================
# TELEGRAM HELPERS
# =========================
def send_message(chat_id: int, text: str, keyboard: dict | None = None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if keyboard:
        payload["reply_markup"] = keyboard
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload)

def edit_message_text(chat_id: int, message_id: int, text: str, keyboard: dict | None = None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if keyboard:
        payload["reply_markup"] = keyboard
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText", json=payload)

def edit_message_reply_markup(chat_id: int, message_id: int, keyboard: dict | None = None):
    payload = {"chat_id": chat_id, "message_id": message_id}
    if keyboard:
        payload["reply_markup"] = keyboard
    requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageReplyMarkup", json=payload)

def delete_message(chat_id: int, message_id: int):
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage", json={"chat_id": chat_id, "message_id": message_id})
    except Exception:
        pass

def answer_callback(callback_id: str):
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery", json={"callback_query_id": callback_id})
    except Exception:
        pass

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

def services_keyboard():
    return {
        "keyboard": [
            [{"text": "GitHub"}],
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

def faq_keyboard():
    return {
        "keyboard": [
            [{"text": "❓ Как подключить GitHub"}],
            [{"text": "❓ Почему сервис не подключён"}],
            [{"text": "❓ Что означают статусы"}],
            [{"text": "❓ Почему нет событий"}],
            [{"text": "⬅️ Назад"}]
        ],
        "resize_keyboard": True
    }

# для списка сервисов: под каждым сервисом — одна кнопка ⚙️ Управление сервисом
def service_manage_keyboard(webhook_id: str):
    return {
        "inline_keyboard": [
            [{"text": "⚙️ Управление сервисом", "callback_data": f"manage:{webhook_id}"}]
        ]
    }

# экран управления сервисом: toggle событий + удалить + назад
def service_settings_keyboard(webhook_id: str, events: dict):
    def btn(label, key):
        emoji = "✅" if events.get(key, True) else "❌"
        return {"text": f"{emoji} {label}", "callback_data": f"toggle_event:{webhook_id}:{key}"}
    return {
        "inline_keyboard": [
            [btn("Push", "push")],
            [btn("Pull Requests", "pull_request")],
            [btn("Issues", "issues")],
            [{"text": "❌ Удалить сервис", "callback_data": f"delete:{webhook_id}"}],
            [{"text": "⬅️ Назад", "callback_data": f"back:{webhook_id}"}]
        ]
    }

# используем при отправке события сразу (github webhook -> telegram)
def github_event_keyboard(event_id: str, url: str):
    return {
        "inline_keyboard": [[
            {"text": "🌐 Открыть на GitHub", "url": url},
            {"text": "📄 Подробнее", "callback_data": f"details:{event_id}"}
        ]]
    }

# в списке оповещений: кнопка "Подробнее" (переходит на details)
def event_open_keyboard(event_id: str):
    return {
        "inline_keyboard": [[{"text": "📄 Подробнее", "callback_data": f"details:{event_id}"}]]
    }

def load_more_keyboard(next_offset: int):
    return {"inline_keyboard": [[{"text": "Загрузить ещё 3", "callback_data": f"load_more:{next_offset}"}]]}

# =========================
# /telegram endpoint (updates from Telegram)
# =========================
@app.post("/telegram")
async def telegram_update(payload: dict = Body(...)):
    # callback query handling
    callback = payload.get("callback_query")
    if callback:
        data = callback.get("data", "")
        message = callback.get("message", {})
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        answer_callback(callback.get("id"))

        # ---------- DETAILS (показываем подробности события) ----------
        if data.startswith("details:"):
            event_id = data.split(":", 1)[1]

            # try memory-first
            mem = GITHUB_EVENTS.get(event_id)
            payload_data = None
            created_at = None
            stored_title = None

            if mem:
                payload_data = mem.get("payload")
                created_at = mem.get("created_at")
            else:
                # fallback to DB
                ev = supabase.table("events").select("*").eq("id", event_id).execute()
                if ev.data:
                    row = ev.data[0]
                    payload_data = row.get("data")
                    created_at = row.get("received_at") or row.get("created_at")
                    stored_title = row.get("title")

            if not payload_data:
                send_message(chat_id, "❌ Детали не найдены (возможно устарели).", main_keyboard())
                return {"ok": True}

            # Compose details message: source + received time, then summary (title) (without duplicate time), then commits
            repo = payload_data.get("repository", {}).get("name", "unknown")
            author = payload_data.get("sender", {}).get("login", "unknown")
            created_str = fmt_dt(created_at)
            # Build summary title if stored_title exists, else reconstruct minimal summary
            if stored_title:
                summary = stored_title
            else:
                # try to reconstruct similar to webhook
                evt_type = payload_data.get("hook", {}).get("type") or "GitHub"
                commits_count = len(payload_data.get("commits", [])) if payload_data.get("commits") else 0
                if evt_type and payload_data.get("commits") is not None:
                    summary = f"🔔 GitHub · Push\n\n📦 Репозиторий:\n{repo}\n👤 Автор:\n{author}\n🕒 Время:\n{created_str}\n\n📝 {pluralize_commits(commits_count)}"
                else:
                    summary = "🔔 GitHub · Событие"

            # commits details
            commits = payload_data.get("commits", []) or []
            commits_text = ""
            for i, c in enumerate(commits, 1):
                msg = (c.get("message") or "").splitlines()[0]
                url = c.get("url") or c.get("html_url") or c.get("distinct") or ""
                author_c = c.get("author", {}).get("name") or c.get("author", {}).get("username") or ""
                commits_text += f"{i}) {msg}"
                if author_c:
                    commits_text += f" — {author_c}"
                if url:
                    commits_text += f"\n   {url}"
                commits_text += "\n"

            commits_count = len(commits)
            commits_count_line = pluralize_commits(commits_count)

            details_msg = (
                f"📄 *Событие*\n\n"
                f"Источник: `github`\n"
                f"Дата и время прихода: `{created_str}`\n\n"
                f"{summary}\n\n"
            )

            if commits_count > 0:
                details_msg += f"📝 {commits_count_line}\n\n*Подробности коммитов:*\n{commits_text}"
            else:
                details_msg += "📝 Нет коммитов в событии.\n"

            send_message(chat_id, details_msg)
            return {"ok": True}

        # ---------- MANAGE: показать настройки сервиса (edit current message) ----------
        if data.startswith("manage:"):
            wid = data.split(":", 1)[1]
            res = supabase.table("webhooks").select("display_name,events_enabled,connected").eq("id", wid).execute()
            if not res.data:
                send_message(chat_id, "❌ Сервис не найден.", main_keyboard())
                return {"ok": True}
            wh = res.data[0]
            events = wh.get("events_enabled") or {"push": True, "pull_request": True, "issues": True}
            display = wh.get("display_name", "GitHub (ожидает подключения)")
            # edit the same message into settings view
            edit_message_text(chat_id, message_id, f"⚙️ *Управление сервисом*\n\n{display}", service_settings_keyboard(wid, events))
            return {"ok": True}

        # ---------- TOGGLE event type ----------
        if data.startswith("toggle_event:"):
            parts = data.split(":", 2)
            if len(parts) != 3:
                return {"ok": True}
            _, wid, event_key = parts
            res = supabase.table("webhooks").select("events_enabled").eq("id", wid).execute()
            if not res.data:
                send_message(chat_id, "❌ Сервис не найден.", main_keyboard())
                return {"ok": True}
            events = res.data[0].get("events_enabled") or {"push": True, "pull_request": True, "issues": True}
            events[event_key] = not events.get(event_key, True)
            supabase.table("webhooks").update({"events_enabled": events}).eq("id", wid).execute()
            # update only reply_markup for the same message
            edit_message_reply_markup(chat_id, message_id, service_settings_keyboard(wid, events))
            return {"ok": True}

        # ---------- BACK: edit the same message back to single service view ----------
        if data.startswith("back:"):
            wid = data.split(":", 1)[1]
            res = supabase.table("webhooks").select("display_name,connected").eq("id", wid).execute()
            if not res.data:
                send_message(chat_id, "❌ Сервис не найден.", main_keyboard())
                return {"ok": True}
            wh = res.data[0]
            display = wh.get("display_name", "GitHub (ожидает подключения)")
            status = "🟢" if wh.get("connected") else "🔴"
            edit_message_text(chat_id, message_id, f"{status} {display}", service_manage_keyboard(wid))
            return {"ok": True}

        # ---------- DELETE: start confirmation ----------
        if data.startswith("delete:"):
            wid = data.split(":", 1)[1]
            PENDING_DELETE[chat_id] = wid
            send_message(chat_id, "⚠️ Удалить этот сервис?", confirm_keyboard())
            return {"ok": True}

        # ---------- LOAD_MORE pagination ----------
        if data.startswith("load_more:"):
            try:
                offset = int(data.split(":", 1)[1])
            except Exception:
                offset = 0

            # delete the button message (the "Загрузить ещё" message)
            try:
                delete_message(chat_id, message_id)
            except Exception:
                pass

            # get user_id for chat_id
            user_res = supabase.table("users").select("id").eq("chat_id", chat_id).execute()
            if not user_res.data:
                send_message(chat_id, "❌ Пользователь не найден.", main_keyboard())
                return {"ok": True}
            user_id = user_res.data[0]["id"]

            evs = supabase.table("events") \
                .select("id,title,received_at") \
                .eq("user_id", user_id) \
                .order("created_at", desc=True) \
                .range(offset, offset + NOTIF_PAGE_SIZE - 1) \
                .execute()

            if not evs.data:
                send_message(chat_id, "Пока нет дополнительных уведомлений.", main_keyboard())
                return {"ok": True}

            for e in evs.data:
                # send preview message (title already contains time formatted)
                preview = e.get("title", "")
                send_message(chat_id, f"{preview}", event_open_keyboard(e["id"]))

            # if exactly page_size, there may be more
            if len(evs.data) == NOTIF_PAGE_SIZE:
                send_message(chat_id, "Загрузить ещё:", load_more_keyboard(offset + NOTIF_PAGE_SIZE))

            return {"ok": True}

        # other callbacks handled above; default:
        return {"ok": True}

    # ---------- MESSAGE handling ----------
    message = payload.get("message")
    if not message:
        return {"ok": True}

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = message.get("text", "").strip()

    # ensure user exists
    supabase.table("users").upsert({"chat_id": chat_id}, on_conflict="chat_id").execute()

    # pending delete via text
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
        send_message(chat_id, "Напиши ДА или НЕТ", confirm_keyboard())
        return {"ok": True}

    # start / back to main
    if text in ("/start", "⬅️ Назад"):
        send_message(chat_id,
                     "👋 *Добро пожаловать в Integration Hub!*\n\n"
                     "Я помогу получать события из GitHub и других сервисов прямо в Telegram.\n\n"
                     "🔹 Подключай сервисы\n"
                     "🔹 Получай уведомления\n"
                     "🔹 Управляй всем из одного бота",
                     main_keyboard())
        return {"ok": True}

    # connect flow
    if text == "➕ Подключить сервис":
        send_message(chat_id, "Выбери сервис:", services_keyboard())
        return {"ok": True}

    if text == "GitHub":
        user_row = supabase.table("users").select("id").eq("chat_id", chat_id).execute()
        if not user_row.data:
            send_message(chat_id, "❌ Не получилось создать пользователя. Попробуй ещё раз.", main_keyboard())
            return {"ok": True}
        user_id = user_row.data[0]["id"]

        wh = supabase.table("webhooks").insert({
            "user_id": user_id,
            "source": "github",
            "connected": False,
            "display_name": "GitHub (ожидает подключения)",
            "notifications_enabled": True,
            "events_enabled": {"push": True, "pull_request": True, "issues": True}
        }).execute()

        if not wh.data:
            send_message(chat_id, "❌ Не удалось создать webhook. Попробуй позже.", main_keyboard())
            return {"ok": True}

        url = f"{BASE_URL}/webhook/github/{wh.data[0]['id']}"
        send_message(chat_id,
                     "🔗 *Подключение GitHub*\n\n"
                     "1️⃣ Зайди в репозиторий GitHub\n"
                     "2️⃣ Settings → Webhooks → Add webhook\n"
                     "3️⃣ Вставь Payload URL из бота:\n"
                     f"`{url}`\n\n"
                     "4️⃣ Content type: `application/json`\n"
                     "5️⃣ Events: Push, Pull requests, Issues\n\n"
                     "После подключения события начнут приходить сюда 👇",
                     main_keyboard())
        return {"ok": True}

    # show services (each service is a single message with one manage button)
    if text == "📦 Мои сервисы":
        user_row = supabase.table("users").select("id").eq("chat_id", chat_id).execute()
        if not user_row.data:
            send_message(chat_id, "❌ Пользователь не найден.", main_keyboard())
            return {"ok": True}
        user_id = user_row.data[0]["id"]

        res = supabase.table("webhooks").select("id,display_name,connected").eq("user_id", user_id).execute()
        if not res.data:
            send_message(chat_id, "📦 У тебя пока нет сервисов.\n\nЕсли есть вопросы — напиши @ligr5", main_keyboard())
            return {"ok": True}

        send_message(chat_id, "📦 *Твои сервисы:*", main_keyboard())
        for s in res.data:
            status = "🟢" if s.get("connected") else "🔴"
            send_message(chat_id, f"{status} {s.get('display_name')}", service_manage_keyboard(s.get("id")))
        return {"ok": True}

    # last notifications (first page)
    if text == "📜 Последние уведомления":
        user_row = supabase.table("users").select("id").eq("chat_id", chat_id).execute()
        if not user_row.data:
            send_message(chat_id, "❌ Пользователь не найден.", main_keyboard())
            return {"ok": True}
        user_id = user_row.data[0]["id"]

        evs = supabase.table("events") \
            .select("id,title,received_at") \
            .eq("user_id", user_id) \
            .order("created_at", desc=True) \
            .range(0, NOTIF_PAGE_SIZE - 1) \
            .execute()

        if not evs.data:
            send_message(chat_id, "Пока нет событий.\n\nЕсли кажется, что что-то не работает — напиши @ligr5", main_keyboard())
            return {"ok": True}

        send_message(chat_id, "📜 *Последние уведомления:*", main_keyboard())
        for e in evs.data:
            preview = e.get("title", "")
            # preview already contains time and formatted block (constructed on webhook)
            send_message(chat_id, f"{preview}", event_open_keyboard(e.get("id")))

        if len(evs.data) == NOTIF_PAGE_SIZE:
            send_message(chat_id, "Загрузить ещё:", load_more_keyboard(NOTIF_PAGE_SIZE))
        return {"ok": True}

    # FAQ
    if text == "❓ Популярные вопросы":
        send_message(chat_id,
                     "❓ *Популярные вопросы*\n\n"
                     "Выбери вопрос ниже 👇\n\n"
                     "❓ *Не нашёл ответ?*\n"
                     "Напиши напрямую: @ligr5",
                     faq_keyboard())
        return {"ok": True}

    if text == "❓ Как подключить GitHub":
        send_message(chat_id,
                     "1️⃣ Зайди в репозиторий GitHub\n"
                     "2️⃣ Settings → Webhooks → Add webhook\n"
                     "3️⃣ Вставь Payload URL из бота\n"
                     "4️⃣ Content type: `application/json`\n"
                     "5️⃣ Events: Push, Pull requests, Issues\n\n"
                     "Если остались вопросы — напиши @ligr5",
                     faq_keyboard())
        return {"ok": True}

    if text == "❓ Почему сервис не подключён":
        send_message(chat_id,
                     "🔴 Статус «ожидает подключения» означает,\n"
                     "что GitHub ещё не отправил ни одного события.\n\n"
                     "Сделай любой push или GitHub пришлёт ping — статус обновится.\n\n"
                     "Если остались вопросы — напиши @ligr5",
                     faq_keyboard())
        return {"ok": True}

    if text == "❓ Что означают статусы":
        send_message(chat_id,
                     "🟢 — сервис подключён и присылает события\n"
                     "🔴 — ожидает первого события от GitHub\n\n"
                     "Если остались вопросы — напиши @ligr5",
                     faq_keyboard())
        return {"ok": True}

    if text == "❓ Почему нет событий":
        send_message(chat_id,
                     "Если событий нет:\n"
                     "• не было push / PR / issues\n"
                     "• webhook ещё не подключён\n"
                     "• репозиторий неактивен\n\n"
                     "Если остались вопросы — напиши @ligr5",
                     faq_keyboard())
        return {"ok": True}

    # default fallback
    send_message(chat_id, "Используй кнопки ниже 👇", main_keyboard())
    return {"ok": True}

# =========================
# HELPERS: show_services (used only on explicit command)
# =========================
def show_services(chat_id):
    user_row = supabase.table("users").select("id").eq("chat_id", chat_id).execute()
    if not user_row.data:
        send_message(chat_id, "❌ Пользователь не найден.", main_keyboard())
        return
    user_id = user_row.data[0]["id"]
    res = supabase.table("webhooks").select("id,display_name,connected").eq("user_id", user_id).execute()
    if not res.data:
        send_message(chat_id, "📦 У тебя пока нет сервисов.\n\nНапиши @ligr5", main_keyboard())
        return
    send_message(chat_id, "📦 *Твои сервисы:*", main_keyboard())
    for s in res.data:
        status = "🟢" if s.get("connected") else "🔴"
        send_message(chat_id, f"{status} {s.get('display_name')}", service_manage_keyboard(s.get("id")))

# =========================
# /webhook/github endpoint (GitHub -> our service)
# =========================
@app.post("/webhook/github/{webhook_id}")
async def github_webhook(webhook_id: str, request: Request):
    payload = await request.json()
    event = request.headers.get("X-GitHub-Event")
    action = payload.get("action")

    wh = supabase.table("webhooks").select("user_id,notifications_enabled,events_enabled").eq("id", webhook_id).execute()
    if not wh.data:
        return {"status": "unknown"}

    events_enabled = wh.data[0].get("events_enabled") or {"push": True, "pull_request": True, "issues": True}

    # filter events according to user settings
    if event == "push" and not events_enabled.get("push", True):
        return {"status": "ignored"}
    if event == "pull_request" and not events_enabled.get("pull_request", True):
        return {"status": "ignored"}
    if event == "issues" and not events_enabled.get("issues", True):
        return {"status": "ignored"}

    repo = payload.get("repository", {}).get("name", "unknown")
    repo_url = payload.get("repository", {}).get("html_url", "")
    user_id = wh.data[0]["user_id"]
    user_row = supabase.table("users").select("chat_id").eq("id", user_id).execute()
    if not user_row.data:
        return {"status": "no_user"}
    chat_id = user_row.data[0]["chat_id"]

    # update display_name on first event
    supabase.table("webhooks").update({
        "connected": True,
        "display_name": f"GitHub ({repo})"
    }).eq("id", webhook_id).execute()

    # precise arrival time (no seconds)
    received_at = datetime.utcnow()
    received_str = received_at.strftime("%d.%m.%Y %H:%M UTC")

    title = ""
    if event == "push":
        commits = len(payload.get("commits", []) or [])
        author = payload.get("sender", {}).get("login", "unknown")
        commits_line = pluralize_commits(commits)
        title = (
            "🔔 GitHub · Push\n\n"
            f"📦 Репозиторий:\n{repo}\n"
            f"👤 Автор:\n{author}\n"
            f"🕒 Время:\n{received_str}\n\n"
            f"📝 {commits_line}"
        )
    elif event == "pull_request" and action in ("opened", "closed"):
        pr = payload.get("pull_request", {}) or {}
        author = pr.get("user", {}).get("login", "unknown")
        num = pr.get("number")
        msg = pr.get("title", "")
        state = "влит" if pr.get("merged") else "закрыт" if action == "closed" else "открыт"
        emoji = "✅" if pr.get("merged") else "❌" if action == "closed" else "🔀"
        title = (
            f"{emoji} GitHub · Pull Request\n\n"
            f"📦 Репозиторий:\n{repo}\n"
            f"👤 Автор:\n{author}\n"
            f"🕒 Время:\n{received_str}\n\n"
            f"📝 PR #{num} {state}:\n{msg}"
        )
        repo_url = pr.get("html_url") or repo_url
    elif event == "issues" and action in ("opened", "closed"):
        issue = payload.get("issue", {}) or {}
        author = issue.get("user", {}).get("login", "unknown")
        num = issue.get("number")
        msg = issue.get("title", "")
        emoji = "🐞" if action == "opened" else "✅"
        title = (
            f"{emoji} GitHub · Issue\n\n"
            f"📦 Репозиторий:\n{repo}\n"
            f"👤 Автор:\n{author}\n"
            f"🕒 Время:\n{received_str}\n\n"
            f"📝 Issue #{num} {action}:\n{msg}"
        )
        repo_url = issue.get("html_url") or repo_url
    else:
        return {"status": "ignored"}

    # store in memory for fast details
    event_id = str(uuid.uuid4())
    GITHUB_EVENTS[event_id] = {"payload": payload, "created_at": received_at}

    # save to DB, try with received_at first, fallback without if schema missing
    event_record = {
        "user_id": user_id,
        "source": "github",
        "title": title,
        "data": payload,
        "received_at": received_at.isoformat()
    }
    try:
        supabase.table("events").insert(event_record).execute()
    except Exception:
        try:
            event_record.pop("received_at")
            supabase.table("events").insert(event_record).execute()
        except Exception:
            # give up and raise to notice in logs
            raise

    # check notifications_enabled flag
    notify = True
    if "notifications_enabled" in wh.data[0]:
        notify = bool(wh.data[0]["notifications_enabled"])

    if notify:
        send_message(chat_id, title, github_event_keyboard(event_id, repo_url))

    return {"status": "ok"}
