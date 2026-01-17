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
GITHUB_EVENTS = {}      # event_id -> {payload, created_at}
PENDING_DELETE = {}    # chat_id -> webhook_id
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
# HELPERS
# =========================
def fmt_dt(dt):
    """Форматирует datetime или ISO-строку в 'YYYY-MM-DD HH:MM:SS UTC'."""
    if not dt:
        return ""
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        # ожидаем ISO строку
        return dt.replace("T", " ").split(".")[0] + " UTC"
    except Exception:
        return str(dt)

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

# один button под сервисом
def service_manage_keyboard(webhook_id: str):
    return {
        "inline_keyboard": [
            [
                {"text": "⚙️ Управление сервисом", "callback_data": f"manage:{webhook_id}"}
            ]
        ]
    }

# настройки внутри экрана управления: toggle + delete + back
def service_settings_keyboard(webhook_id: str, enabled: bool):
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"🔔 Уведомления: {'ВКЛ' if enabled else 'ВЫКЛ'}",
                    "callback_data": f"toggle:{webhook_id}"
                }
            ],
            [
                {"text": "❌ Удалить сервис", "callback_data": f"delete:{webhook_id}"}
            ],
            [
                {"text": "⬅️ Назад", "callback_data": "back_services"}
            ]
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

        # details (из GITHUB_EVENTS) — добавлен вывод времени из created_at
        if data.startswith("details:"):
            event_id = data.split(":", 1)[1]
            ev = GITHUB_EVENTS.get(event_id)
            if not ev:
                send_message(
                    chat_id,
                    "⌛ Детали устарели.\n\nЕсли есть вопросы — напиши @ligr5",
                    main_keyboard()
                )
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

        # open event (показываем полную дату/время created_at из БД)
        if data.startswith("open:"):
            event_id = data.split(":", 1)[1]
            ev = supabase.table("events").select("*").eq("id", event_id).execute()
            if not ev.data:
                send_message(
                    chat_id,
                    "❌ Событие не найдено.\n\nЕсли что-то не так — напиши @ligr5",
                    main_keyboard()
                )
                return {"ok": True}

            e = ev.data[0]
            created_str = fmt_dt(e.get("created_at"))
            send_message(
                chat_id,
                "📄 *Событие*\n\n"
                f"Источник: `{e.get('source')}`\n"
                f"Дата и время прихода: `{created_str}`\n\n"
                f"{e.get('title')}"
            )
            return {"ok": True}

        # delete from inline (start confirmation flow)
        if data.startswith("delete:"):
            wid = data.split(":", 1)[1]
            PENDING_DELETE[chat_id] = wid
            send_message(chat_id, "⚠️ Удалить этот сервис?", confirm_keyboard())
            return {"ok": True}

        # manage -> открываем экран настроек (редактируем сообщение)
        if data.startswith("manage:"):
            wid = data.split(":", 1)[1]
            # get display_name and notifications_enabled (default True if missing)
            res = supabase.table("webhooks").select("display_name,notifications_enabled").eq("id", wid).execute()
            if not res.data:
                send_message(chat_id, "❌ Сервис не найден.", main_keyboard())
                return {"ok": True}
            wh = res.data[0]
            enabled = True
            if "notifications_enabled" in wh and wh["notifications_enabled"] is not None:
                enabled = bool(wh["notifications_enabled"])
            display = wh.get("display_name", "GitHub (ожидает подключения)")

            # edit the existing message (so the manage button becomes the settings view)
            edit_message_text(
                chat_id,
                message_id,
                f"⚙️ *Управление сервисом*\n\n{display}",
                service_settings_keyboard(wid, enabled)
            )
            return {"ok": True}

        # toggle: change only the inline keyboard and DB flag
        if data.startswith("toggle:"):
            wid = data.split(":", 1)[1]
            res = supabase.table("webhooks").select("display_name,notifications_enabled").eq("id", wid).execute()
            if not res.data:
                send_message(chat_id, "❌ Сервис не найден.", main_keyboard())
                return {"ok": True}
            wh = res.data[0]
            current = True
            if "notifications_enabled" in wh and wh["notifications_enabled"] is not None:
                current = bool(wh["notifications_enabled"])

            new_state = not current
            # update DB
            supabase.table("webhooks").update({"notifications_enabled": new_state}).eq("id", wid).execute()

            # change only the inline keyboard for the same message
            edit_message_reply_markup(
                chat_id,
                message_id,
                service_settings_keyboard(wid, new_state)
            )
            return {"ok": True}

        # back to services
        if data == "back_services":
            show_services(chat_id)
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
            # ensure default exists in DB; if not, rely on DB default
            "notifications_enabled": True
        }).execute()

        url = f"{BASE_URL}/webhook/github/{wh.data[0]['id']}"
        send_message(
            chat_id,
            "🔗 *Подключение GitHub*\n\n"
            "1️⃣ Зайди в репозиторий GitHub\n"
            "2️⃣ Settings → Webhooks → Add webhook\n"
            "3️⃣ Вставь Payload URL:\n"
            f"`{url}`\n\n"
            "4️⃣ Content type: `application/json`\n"
            "5️⃣ Events: Push, Pull requests, Issues\n\n"
            "После подключения события начнут приходить сюда 👇",
            main_keyboard()
        )
        return {"ok": True}

    if text == "📦 Мои сервисы":
        user_id = supabase.table("users").select("id").eq("chat_id", chat_id).execute().data[0]["id"]
        res = supabase.table("webhooks").select("id,display_name,connected").eq("user_id", user_id).execute()

        if not res.data:
            send_message(
                chat_id,
                "📦 У тебя пока нет сервисов.\n\n"
                "Если есть вопросы — напиши @ligr5",
                main_keyboard()
            )
            return {"ok": True}

        send_message(chat_id, "📦 *Твои сервисы:*", main_keyboard())
        for s in res.data:
            status = "🟢" if s["connected"] else "🔴"
            # send single manage button under each service
            send_message(chat_id, f"{status} {s['display_name']}", service_manage_keyboard(s["id"]))
        return {"ok": True}

    if text == "📜 Последние уведомления":
        user_id = supabase.table("users").select("id").eq("chat_id", chat_id).execute().data[0]["id"]
        evs = supabase.table("events").select("id,title,created_at").eq("user_id", user_id).order("created_at", desc=True).limit(10).execute()

        if not evs.data:
            send_message(
                chat_id,
                "Пока нет событий.\n\nЕсли кажется, что что-то не работает — напиши @ligr5",
                main_keyboard()
            )
            return {"ok": True}

        send_message(chat_id, "📜 *Последние уведомления:*", main_keyboard())
        for e in evs.data:
            # show title already contains the time, but ensure created_at is visible — include created_at in the list preview
            created_str = fmt_dt(e.get("created_at"))
            send_message(chat_id, f"🐙 {e.get('title')}\n\n🕒 {created_str}", event_open_keyboard(e["id"]))
        return {"ok": True}

    # ---------- FAQ ----------
    if text == "❓ Популярные вопросы":
        send_message(
            chat_id,
            "❓ *Популярные вопросы*\n\n"
            "Выбери вопрос ниже 👇\n\n"
            "❓ *Не нашёл ответ?*\n"
            "Напиши напрямую: @ligr5",
            faq_keyboard()
        )
        return {"ok": True}

    if text == "❓ Как подключить GitHub":
        send_message(
            chat_id,
            "1️⃣ Зайди в репозиторий GitHub\n"
            "2️⃣ Settings → Webhooks → Add webhook\n"
            "3️⃣ Вставь Payload URL из бота\n"
            "4️⃣ Content type: application/json\n"
            "5️⃣ Events: Push, Pull requests, Issues\n\n"
            "Если остались вопросы — напиши @ligr5",
            faq_keyboard()
        )
        return {"ok": True}

    if text == "❓ Почему сервис не подключён":
        send_message(
            chat_id,
            "🔴 Статус «ожидает подключения» означает,\n"
            "что GitHub ещё не отправил ни одного события.\n\n"
            "Сделай любой push или GitHub сам пришлёт ping —\n"
            "статус обновится автоматически.\n\n"
            "Если остались вопросы — напиши @ligr5",
            faq_keyboard()
        )
        return {"ok": True}

    if text == "❓ Что означают статусы":
        send_message(
            chat_id,
            "🟢 — сервис подключён и присылает события\n"
            "🔴 — ожидает первого события от GitHub\n\n"
            "Если остались вопросы — напиши @ligr5",
            faq_keyboard()
        )
        return {"ok": True}

    if text == "❓ Почему нет событий":
        send_message(
            chat_id,
            "Если событий нет:\n"
            "• не было push / PR / issues\n"
            "• webhook ещё не подключён\n"
            "• репозиторий неактивен\n\n"
            "Если остались вопросы — напиши @ligr5",
            faq_keyboard()
        )
        return {"ok": True}

    send_message(chat_id, "Используй кнопки ниже 👇", main_keyboard())
    return {"ok": True}

# =========================
# HELPERS (повторно для доступности)
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
            service_manage_keyboard(s["id"])
        )

# =========================
# GITHUB WEBHOOK
# =========================
@app.post("/webhook/github/{webhook_id}")
async def github_webhook(webhook_id: str, request: Request):
    payload = await request.json()
    event = request.headers.get("X-GitHub-Event")
    action = payload.get("action")

    wh = supabase.table("webhooks").select("user_id,notifications_enabled").eq("id", webhook_id).execute()
    if not wh.data:
        return {"status": "unknown"}

    repo = payload["repository"]["name"]
    repo_url = payload["repository"]["html_url"]
    user_id = wh.data[0]["user_id"]
    chat_id = supabase.table("users").select("chat_id").eq("id", user_id).execute().data[0]["chat_id"]

    # Update display name on first event (as before)
    supabase.table("webhooks").update({
        "connected": True,
        "display_name": f"GitHub ({repo})"
    }).eq("id", webhook_id).execute()

    # точное время прихода события
    received_at = datetime.utcnow()
    received_str = fmt_dt(received_at)

    title = ""
    if event == "push":
        commits = len(payload.get("commits", []))
        author = payload["sender"]["login"]
        title = (
            "🔔 GitHub · Push\n\n"
            f"📦 Репозиторий:\n{repo}\n"
            f"👤 Автор:\n{author}\n"
            f"🕒 Время:\n{received_str}\n\n"
            f"📝 {commits} новых коммита"
        )

    elif event == "pull_request" and action in ("opened", "closed"):
        pr = payload["pull_request"]
        author = pr["user"]["login"]
        num = pr["number"]
        msg = pr["title"]
        state = "влит" if pr.get("merged") else "закрыт" if action == "closed" else "открыт"
        emoji = "✅" if pr.get("merged") else "❌" if action == "closed" else "🔀"

        title = (
            f"{emoji} GitHub · Pull Request\n\n"
            f"📦 Репозиторий:\n{repo}\n"
            f"👤 Автор:\n{author}\n"
            f"🕒 Время:\n{received_str}\n\n"
            f"📝 PR #{num} {state}:\n{msg}"
        )
        repo_url = pr["html_url"]

    elif event == "issues" and action in ("opened", "closed"):
        issue = payload["issue"]
        author = issue["user"]["login"]
        num = issue["number"]
        msg = issue["title"]
        emoji = "🐞" if action == "opened" else "✅"

        title = (
            f"{emoji} GitHub · Issue\n\n"
            f"📦 Репозиторий:\n{repo}\n"
            f"👤 Автор:\n{author}\n"
            f"🕒 Время:\n{received_str}\n\n"
            f"📝 Issue #{num} {action}:\n{msg}"
        )
        repo_url = issue["html_url"]

    else:
        return {"status": "ignored"}

    event_id = str(uuid.uuid4())
    # сохраняем payload и время прихода в оперативной памяти для деталей
    GITHUB_EVENTS[event_id] = {"payload": payload, "created_at": received_at}

    # сохраняем событие в БД и явно передаём received_at (если нужно, будет в created_at тоже)
    supabase.table("events").insert({
        "user_id": user_id,
        "source": "github",
        "title": title,
        "data": payload,
        "received_at": received_at.isoformat()  # опционально, удобно для поиска/фильтров
    }).execute()

    # отправляем только если включены уведомления
    notify = True
    if "notifications_enabled" in wh.data[0]:
        notify = bool(wh.data[0]["notifications_enabled"])

    if notify:
        send_message(chat_id, title, github_event_keyboard(event_id, repo_url))

    return {"status": "ok"}
