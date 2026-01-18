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
# CONFIG
# =========================
GITHUB_EVENTS = {}      # event_id -> {payload, created_at}
PENDING_DELETE = {}     # chat_id -> webhook_id
TTL_HOURS = 24

NOTIF_PAGE_SIZE = 3     # показываем по 3 уведомления

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
# HELPERS
# =========================
def fmt_dt(dt):
    """Форматирует datetime или ISO-строку в 'DD.MM.YYYY HH:MM UTC' (без секунд)."""
    if not dt:
        return ""
    if isinstance(dt, datetime):
        return dt.strftime("%d.%m.%Y %H:%M UTC")
    try:
        # ожидаем ISO строку: 2026-01-17T12:34:56...
        base = dt.replace("T", " ").split(".")[0]  # "YYYY-MM-DD HH:MM:SS"
        # обрежем секунды
        parts = base.split(" ")
        if len(parts) >= 2:
            date = parts[0]
            time_hm = ":".join(parts[1].split(":")[:2])
            return f"{datetime.strptime(date, '%Y-%m-%d').strftime('%d.%m.%Y')} {time_hm} UTC"
        return base + " UTC"
    except Exception:
        return str(dt)

def strip_time_from_title(title: str) -> str:
    """
    Убирает блок:
    "\n🕒 Время:\n{...}\n\n"
    если он присутствует в title, чтобы в списке не дублировать время.
    """
    if not title:
        return title
    marker = "\n🕒 Время:\n"
    idx = title.find(marker)
    if idx == -1:
        return title
    # найти следующее двойное переведение строки после marker
    start = idx
    after = title.find("\n\n", start)
    if after == -1:
        # нет двойного переноса — удаляем до конца
        return title[:start]
    # удаляем участок [start: after+2]
    return title[:start] + title[after+2:]

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

    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json=payload
    )

def edit_message_text(chat_id: int, message_id: int, text: str, keyboard: dict | None = None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if keyboard:
        payload["reply_markup"] = keyboard
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText",
        json=payload
    )

def edit_message_reply_markup(chat_id: int, message_id: int, keyboard: dict | None = None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id
    }
    if keyboard:
        payload["reply_markup"] = keyboard
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageReplyMarkup",
        json=payload
    )

def delete_message(chat_id: int, message_id: int):
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage",
        json={"chat_id": chat_id, "message_id": message_id}
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

def service_manage_keyboard(webhook_id: str):
    return {
        "inline_keyboard": [
            [
                {"text": "⚙️ Управление сервисом", "callback_data": f"manage:{webhook_id}"}
            ]
        ]
    }

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

def github_event_keyboard(event_id: str, url: str):
    return {
        "inline_keyboard": [[
            {"text": "🌐 Открыть на GitHub", "url": url},
            {"text": "📄 Подробнее", "callback_data": f"details:{event_id}"}
        ]]
    }

def event_open_keyboard(event_id: str):
    return {
        "inline_keyboard": [[
            {"text": "📄 Открыть событие", "callback_data": f"open:{event_id}"}
        ]]
    }

def load_more_keyboard(next_offset: int):
    return {
        "inline_keyboard": [
            [{"text": "Загрузить ещё 3", "callback_data": f"load_more:{next_offset}"}]
        ]
    }

# =========================
# TELEGRAM WEBHOOK
# =========================
@app.post("/telegram")
async def telegram_update(payload: dict = Body(...)):
    callback = payload.get("callback_query")
    if callback:
        data = callback["data"]
        chat_id = callback["message"]["chat"]["id"]
        message_id = callback["message"]["message_id"]
        answer_callback(callback["id"])

        # --- details ---
        if data.startswith("details:"):
            event_id = data.split(":", 1)[1]
            ev = GITHUB_EVENTS.get(event_id)
            if not ev:
                send_message(chat_id, "⌛ Детали устарели.\n\nЕсли есть вопросы — напиши @ligr5", main_keyboard())
                return {"ok": True}

            p = ev["payload"]
            created = fmt_dt(ev.get("created_at"))
            text = (
                "📄 *Подробности GitHub события*\n\n"
                f"📦 Репозиторий:\n{p['repository']['name']}\n"
                f"👤 Автор:\n{p.get('sender', {}).get('login', 'unknown')}\n"
                f"🕒 Время прихода:\n{created}\n\n"
                "📝 Коммиты:\n"
            )
            for i, c in enumerate(p.get("commits", []), 1):
                text += f"{i}) {c['message'].splitlines()[0]}\n"

            send_message(chat_id, text)
            return {"ok": True}

        # --- open event ---
        if data.startswith("open:"):
            event_id = data.split(":", 1)[1]
            ev = supabase.table("events").select("*").eq("id", event_id).execute()
            if not ev.data:
                send_message(chat_id, "❌ Событие не найдено.\n\nЕсли что-то не так — напиши @ligr5", main_keyboard())
                return {"ok": True}
            e = ev.data[0]
            created_str = fmt_dt(e.get("received_at") or e.get("created_at"))
            send_message(chat_id,
                         "📄 *Событие*\n\n"
                         f"Источник: `{e.get('source')}`\n"
                         f"Дата и время прихода: `{created_str}`\n\n"
                         f"{e.get('title')}")
            return {"ok": True}

        # --- delete flow ---
        if data.startswith("delete:"):
            wid = data.split(":", 1)[1]
            PENDING_DELETE[chat_id] = wid
            send_message(chat_id, "⚠️ Удалить этот сервис?", confirm_keyboard())
            return {"ok": True}

        # --- manage settings ---
        if data.startswith("manage:"):
            wid = data.split(":", 1)[1]
            res = supabase.table("webhooks").select("display_name,events_enabled,connected").eq("id", wid).execute()
            if not res.data:
                send_message(chat_id, "❌ Сервис не найден.", main_keyboard())
                return {"ok": True}
            wh = res.data[0]
            events = wh.get("events_enabled") or {"push": True, "pull_request": True, "issues": True}
            display = wh.get("display_name", "GitHub (ожидает подключения)")
            edit_message_text(chat_id, message_id, f"⚙️ *Управление сервисом*\n\n{display}", service_settings_keyboard(wid, events))
            return {"ok": True}

        # --- toggle single event type ---
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
            edit_message_reply_markup(chat_id, message_id, service_settings_keyboard(wid, events))
            return {"ok": True}

        # --- back: restore the SAME message to original service entry (no re-list) ---
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

        # --- load more notifications ---
        if data.startswith("load_more:"):
            # format: load_more:{offset}
            try:
                offset = int(data.split(":", 1)[1])
            except Exception:
                offset = 0
            # удаляем кнопку (это то сообщение с кнопкой)
            try:
                delete_message(chat_id, message_id)
            except Exception:
                pass  # не критично

            # получаем user_id по chat_id
            user_res = supabase.table("users").select("id").eq("chat_id", chat_id).execute()
            if not user_res.data:
                send_message(chat_id, "❌ Пользователь не найден.", main_keyboard())
                return {"ok": True}
            user_id = user_res.data[0]["id"]

            # достаём следующие N уведомлений с offset
            start = offset
            end = offset + NOTIF_PAGE_SIZE - 1
            evs = supabase.table("events") \
                .select("id,title,received_at") \
                .eq("user_id", user_id) \
                .order("created_at", desc=True) \
                .range(start, end) \
                .execute()

            if not evs.data:
                send_message(chat_id, "Пока нет дополнительных уведомлений.", main_keyboard())
                return {"ok": True}

            for e in evs.data:
                # в preview не дублируем время — убираем секцию времени из title, если есть
                preview = strip_time_from_title(e.get("title", ""))
                created_str = fmt_dt(e.get("received_at") or e.get("created_at"))
                # НЕ добавляем отдельно время внизу, поскольку время уже присутствует в preview (если нужно) —
                # но пользователь просил не дублировать: поэтому показываем просто preview
                send_message(chat_id, f"🐙 {preview}", event_open_keyboard(e["id"]))

            # если пришло ровно page_size — возможно есть ещё — отправим кнопку с новым offset
            if len(evs.data) == NOTIF_PAGE_SIZE:
                new_offset = offset + NOTIF_PAGE_SIZE
                send_message(chat_id, "Загрузить ещё:", load_more_keyboard(new_offset))
            return {"ok": True}

    # ---------- MESSAGE ----------
    message = payload.get("message")
    if not message:
        return {"ok": True}

    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()

    supabase.table("users").upsert({"chat_id": chat_id}, on_conflict="chat_id").execute()

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

    if text in ("/start", "⬅️ Назад"):
        send_message(chat_id,
                     "👋 *Добро пожаловать в Integration Hub!*\n\n"
                     "Я помогу получать события из GitHub и других сервисов прямо в Telegram.\n\n"
                     "🔹 Подключай сервисы\n"
                     "🔹 Получай уведомления\n"
                     "🔹 Управляй всем из одного бота",
                     main_keyboard())
        return {"ok": True}

    if text == "➕ Подключить сервис":
        send_message(chat_id, "Выбери сервис:", services_keyboard())
        return {"ok": True}

    if text == "GitHub":
        user_id = supabase.table("users").select("id").eq("chat_id", chat_id).execute().data[0]["id"]
        wh = supabase.table("webhooks").insert({
            "user_id": user_id,
            "source": "github",
            "connected": False,
            "display_name": "GitHub (ожидает подключения)",
            "notifications_enabled": True,
            "events_enabled": {"push": True, "pull_request": True, "issues": True}
        }).execute()

        url = f"{BASE_URL}/webhook/github/{wh.data[0]['id']}"
        send_message(chat_id,
                     "🔗 *Подключение GitHub*\n\n"
                     "1️⃣ Зайди в репозиторий GitHub\n"
                     "2️⃣ Settings → Webhooks → Add webhook\n"
                     "3️⃣ Вставь Payload URL:\n"
                     f"`{url}`\n\n"
                     "4️⃣ Content type: `application/json`\n"
                     "5️⃣ Events: Push, Pull requests, Issues\n\n"
                     "После подключения события начнут приходить сюда 👇",
                     main_keyboard())
        return {"ok": True}

    if text == "📦 Мои сервисы":
        show_services(chat_id)
        return {"ok": True}

    if text == "📜 Последние уведомления":
        # показываем первые NOTIF_PAGE_SIZE уведомлений (offset = 0)
        user_res = supabase.table("users").select("id").eq("chat_id", chat_id).execute()
        if not user_res.data:
            send_message(chat_id, "❌ Пользователь не найден.", main_keyboard())
            return {"ok": True}
        user_id = user_res.data[0]["id"]

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
            preview = strip_time_from_title(e.get("title", ""))
            # НЕ добавляем время снизу (чтобы не дублировать)
            send_message(chat_id, f"🐙 {preview}", event_open_keyboard(e["id"]))

        # если их ровно page_size — возможно есть ещё — отправляем кнопку load_more:3
        if len(evs.data) == NOTIF_PAGE_SIZE:
            send_message(chat_id, "Загрузить ещё:", load_more_keyboard(NOTIF_PAGE_SIZE))
        return {"ok": True}

    # ---------- FAQ ----------
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
                     "4️⃣ Content type: application/json\n"
                     "5️⃣ Events: Push, Pull requests, Issues\n\n"
                     "Если остались вопросы — напиши @ligr5",
                     faq_keyboard())
        return {"ok": True}

    if text == "❓ Почему сервис не подключён":
        send_message(chat_id,
                     "🔴 Статус «ожидает подключения» означает,\n"
                     "что GitHub ещё не отправил ни одного события.\n\n"
                     "Сделай любой push или GitHub сам пришлёт ping —\n"
                     "статус обновится автоматически.\n\n"
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

    send_message(chat_id, "Используй кнопки ниже 👇", main_keyboard())
    return {"ok": True}

# =========================
# HELPERS
# =========================
def show_services(chat_id):
    user_id = supabase.table("users").select("id").eq("chat_id", chat_id).execute().data[0]["id"]
    services = supabase.table("webhooks").select("id,display_name,connected").eq("user_id", user_id).execute().data
    if not services:
        send_message(chat_id, "📦 У тебя пока нет сервисов.\n\nНапиши @ligr5", main_keyboard())
        return
    send_message(chat_id, "📦 *Твои сервисы:*", main_keyboard())
    for s in services:
        status = "🟢" if s["connected"] else "🔴"
        send_message(chat_id, f"{status} {s['display_name']}", service_manage_keyboard(s["id"]))

# =========================
# GITHUB WEBHOOK
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

    # фильтрация по типу события (если пользователь выключил)
    if event == "push" and not events_enabled.get("push", True):
        return {"status": "ignored"}
    if event == "pull_request" and not events_enabled.get("pull_request", True):
        return {"status": "ignored"}
    if event == "issues" and not events_enabled.get("issues", True):
        return {"status": "ignored"}

    repo = payload["repository"]["name"]
    repo_url = payload["repository"]["html_url"]
    user_id = wh.data[0]["user_id"]
    chat_id = supabase.table("users").select("chat_id").eq("id", user_id).execute().data[0]["chat_id"]

    supabase.table("webhooks").update({
        "connected": True,
        "display_name": f"GitHub ({repo})"
    }).eq("id", webhook_id).execute()

    # точное время прихода события в формате DD.MM.YYYY HH:MM UTC (без секунд)
    received_at = datetime.utcnow()
    received_str = received_at.strftime("%d.%m.%Y %H:%M UTC")

    title = ""
    if event == "push":
        commits = len(payload.get("commits", []))
        author = payload.get("sender", {}).get("login", "unknown")
        title = (
            "🔔 GitHub · Push\n\n"
            f"📦 Репозиторий:\n{repo}\n"
            f"👤 Автор:\n{author}\n"
            f"🕒 Время:\n{received_str}\n\n"
            f"📝 {commits} новых коммита"
        )

    elif event == "pull_request" and action in ("opened", "closed"):
        pr = payload.get("pull_request", {})
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
        repo_url = pr.get("html_url", repo_url)

    elif event == "issues" and action in ("opened", "closed"):
        issue = payload.get("issue", {})
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
        repo_url = issue.get("html_url", repo_url)

    else:
        return {"status": "ignored"}

    event_id = str(uuid.uuid4())
    GITHUB_EVENTS[event_id] = {"payload": payload, "created_at": received_at}

    # save event to DB (include received_at field if present in schema)
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
            raise

    # check notifications_enabled flag
    notify = True
    if "notifications_enabled" in wh.data[0]:
        notify = bool(wh.data[0]["notifications_enabled"])

    if notify:
        send_message(chat_id, title, github_event_keyboard(event_id, repo_url))

    return {"status": "ok"}
