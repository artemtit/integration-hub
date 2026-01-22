import os
import uuid
import time
import threading
import requests
import secrets
import hmac
import hashlib
from json import JSONDecodeError
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
# HELPERS: формат времени, склонения, утилиты
# =========================
def verify_github_signature(body: bytes, signature_header: str | None, secret: str | None) -> bool:
    """Проверяет подпись GitHub webhook (X-Hub-Signature-256)."""
    if not signature_header or not secret:
        return False
    try:
        algo, signature = signature_header.split("=")
    except ValueError:
        return False
    if algo != "sha256":
        return False
    mac = hmac.new(secret.encode(), msg=body, digestmod=hashlib.sha256)
    return hmac.compare_digest(mac.hexdigest(), signature)

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

def strip_time_from_title(title: str) -> str:
    """
    Убирает блок с '🕒 Время:' внутри title чтобы не дублировать время в деталях.
    Ищет '\n🕒 Время:\n' и удаляет до следующего двойного перевода строки.
    """
    if not title:
        return title
    marker = "\n🕒 Время:\n"
    idx = title.find(marker)
    if idx == -1:
        return title
    after = title.find("\n\n", idx)
    if after == -1:
        return title[:idx]
    # сохраним части без блока времени
    return (title[:idx] + title[after+2:]).strip()

# === добавьте этот хелпер сразу после strip_time_from_title ===
def strip_commits_line(title: str) -> str:
    """
    Убирает строку(и) с подсчётом коммитов вида '📝 ...' из заголовка,
    чтобы не дублировать счётчик в деталях.
    """
    if not title:
        return title
    lines = title.splitlines()
    filtered = [ln for ln in lines if not ln.strip().startswith("📝 ")]
    return "\n".join(filtered).strip()

def md_escape(text: str) -> str:
    """
    Экранирует наиболее проблемные символы для Telegram Markdown (v1).
    Мы экранируем только части текста, которые вставляем в Markdown-сообщение,
    чтобы подчёркивания, звёздочки и прочее отображались как текст.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    # сначала экранируем обратный слеш
    text = text.replace("\\", "\\\\")
    for ch in ["_", "*", "`", "[", "]", "(", ")"]:
        text = text.replace(ch, f"\\{ch}")
    return text

def format_commit_author(author: dict | None) -> str:
    """
    Форматирует автора коммита: 'Name (username)' при наличии обоих.
    Результат уже экранирован под Markdown.
    """
    if not author:
        return ""
    name = author.get("name")
    username = author.get("username")
    if name and username:
        return f"{md_escape(name)} ({md_escape(username)})"
    if name:
        return md_escape(name)
    if username:
        return md_escape(username)
    return ""

def extract_repo_from_title(title: str) -> str:
    """
    Пытаемся найти строку с '📦 Репозиторий:' в title и вернуть следующую строку.
    Если не нашли — возвращаем пустую строку.
    """
    if not title:
        return ""
    marker = "📦 Репозиторий:"
    idx = title.find(marker)
    if idx == -1:
        return ""
    # получаем подпоследовательность после маркера
    after = title[idx + len(marker):].lstrip("\n ")
    # возьмём первую строку из after
    repo_line = after.splitlines()[0] if after.splitlines() else ""
    return repo_line.strip()

def short_preview_from_title(title: str) -> str:
    """
    Делает короткое превью: первая строка title (например "🔔 GitHub · Push")
    и, если есть, добавляет имя репозитория.
    """
    if not title:
        return ""
    first_line = title.splitlines()[0]
    repo = extract_repo_from_title(title)
    if repo:
        return f"{first_line} · {repo}"
    return first_line

# =========================
# TELEGRAM HELPERS (send/edit/delete)
# =========================
def send_message(chat_id: int, text: str, keyboard: dict | None = None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if keyboard:
        payload["reply_markup"] = keyboard
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        print("send_message error:", e)

def edit_message_text(chat_id: int, message_id: int, text: str, keyboard: dict | None = None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if keyboard:
        payload["reply_markup"] = keyboard
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText", json=payload, timeout=10)
    except Exception as e:
        print("edit_message_text error:", e)

def edit_message_reply_markup(chat_id: int, message_id: int, keyboard: dict | None = None):
    payload = {"chat_id": chat_id, "message_id": message_id}
    if keyboard:
        payload["reply_markup"] = keyboard
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageReplyMarkup", json=payload, timeout=10)
    except Exception as e:
        print("edit_message_reply_markup error:", e)

def delete_message(chat_id: int, message_id: int):
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage", json={"chat_id": chat_id, "message_id": message_id}, timeout=10)
    except Exception:
        pass

def answer_callback(callback_id: str):
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery", json={"callback_query_id": callback_id}, timeout=10)
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

def service_manage_keyboard(webhook_id: str):
    return {
        "inline_keyboard": [
            [{"text": "⚙️ Управление сервисом", "callback_data": f"manage:{webhook_id}"}]
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
        "inline_keyboard": [[{"text": "📄 Подробнее", "callback_data": f"details:{event_id}"}]]
    }

def load_more_keyboard(next_offset: int):
    return {"inline_keyboard": [[{"text": "Загрузить ещё 3", "callback_data": f"load_more:{next_offset}"}]]}

# =========================
# Notifications list builder (short, with pager)
# =========================
def build_notifications_list_short(evs, offset=0, total=0):
    """
    Формирует компактный список уведомлений без верхней строки с "X из Y".
    Внизу остаётся навигационная строка (⬅️ / центр / Вперёд ➡️).
    """
    REPO_DISPLAY_MAX = 18  # макс длина репо в превью (без многоточия)

    lines = []
    inline_rows = []

    for e in evs:
        title = e.get("title") or ""
        first_line = title.splitlines()[0] if title else ""
        first_line = md_escape(first_line)[:150]

        created = fmt_dt(e.get("received_at") or e.get("created_at"))

        # берём репозиторий прямо из payload (более надёжно)
        payload = e.get("data") or {}
        repo_raw = (payload.get("repository") or {}).get("name")
        repo_display = ""
        if repo_raw:
            repo_raw = repo_raw.strip()
            if len(repo_raw) > REPO_DISPLAY_MAX:
                repo_display = md_escape(repo_raw[:REPO_DISPLAY_MAX - 1] + "…")
            else:
                repo_display = md_escape(repo_raw)

        if repo_display:
            lines.append(f"• {first_line} · {repo_display}\n`{created}`")
        else:
            lines.append(f"• {first_line}\n`{created}`")

        # Левая кнопка — детали
        left_label = first_line[:60] or "Подробнее"
        row = [{
            "text": left_label,
            "callback_data": f"details:{e.get('id')}"
        }]

        # Правая кнопка — всегда одинаковая подпись (если есть url)
        repo_url = (payload.get("repository") or {}).get("html_url") or e.get("repo_url") or ""
        if repo_url:
            row.append({
                "text": "🌐 В GitHub",
                "url": repo_url
            })

        inline_rows.append(row)

    # Навигационная строка (внизу)
    displayed_to = min(offset + len(evs), total) if total else (offset + len(evs))
    nav_row = []
    if offset > 0:
        prev_offset = max(0, offset - NOTIF_PAGE_SIZE)
        nav_row.append({"text": "⬅️ Назад", "callback_data": f"page:{prev_offset}"})
    # центр — показывает текущий просмотр (noop)
    nav_row.append({"text": f"{displayed_to} из {total}", "callback_data": f"noop:0"})
    if total and (offset + NOTIF_PAGE_SIZE) < total:
        next_offset = offset + NOTIF_PAGE_SIZE
        nav_row.append({"text": "Вперёд ➡️", "callback_data": f"page:{next_offset}"})

    if nav_row:
        inline_rows.append(nav_row)

    header = "📜 *Последние уведомления:*\n\n"
    text = header + ("\n\n".join(lines) if lines else "Пока нет событий.")
    keyboard = {"inline_keyboard": inline_rows}
    return text, keyboard

# =========================
# TELEGRAM UPDATE ENDPOINT
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

        # NOOP (просто acknowledge)
        if data.startswith("noop:"):
            return {"ok": True}

        # PAGE navigation handler
        if data.startswith("page:"):
            try:
                offset = int(data.split(":", 1)[1])
            except Exception:
                offset = 0

            try:
                user_row = supabase.table("users").select("id").eq("chat_id", chat_id).execute()
                if not user_row.data:
                    send_message(chat_id, "❌ Пользователь не найден.", main_keyboard())
                    return {"ok": True}
                user_id = user_row.data[0]["id"]

                # total count (try to get count; SDK may provide .count)
                try:
                    count_res = supabase.table("events").select("id", count="exact").eq("user_id", user_id).execute()
                    total = getattr(count_res, "count", None)
                    if total is None:
                        # fallback: if .data present and count not provided, try length via separate lightweight query
                        all_res = supabase.table("events").select("id").eq("user_id", user_id).execute()
                        total = len(all_res.data) if all_res.data is not None else 0
                except Exception:
                    total = 0

                evs_res = supabase.table("events") \
                    .select("id,title,received_at,data,created_at") \
                    .eq("user_id", user_id) \
                    .order("created_at", desc=True) \
                    .range(offset, offset + NOTIF_PAGE_SIZE - 1) \
                    .execute()

                if not evs_res.data:
                    edit_message_text(chat_id, message_id, "Больше уведомлений нет.", None)
                    return {"ok": True}

                # enrich with repo_url if possible
                evs = evs_res.data
                for row in evs:
                    payload_row = row.get("data") or {}
                    row["repo_url"] = (payload_row.get("repository") or {}).get("html_url", "")

                text_out, kb = build_notifications_list_short(evs, offset=offset, total=total)
                edit_message_text(chat_id, message_id, text_out, kb)
                return {"ok": True}
            except Exception as e:
                print("DB error in page handler:", e)
                send_message(chat_id, "❌ Ошибка сервера.", main_keyboard())
                return {"ok": True}

        # DETAILS handler
        if data.startswith("details:"):
            event_id = data.split(":", 1)[1]
            mem = GITHUB_EVENTS.get(event_id)
            payload_data = None
            created_at = None
            stored_title = None

            if mem:
                payload_data = mem.get("payload")
                created_at = mem.get("created_at")
            else:
                try:
                    ev = supabase.table("events").select("*").eq("id", event_id).execute()
                    if ev.data:
                        row = ev.data[0]
                        payload_data = row.get("data")
                        created_at = row.get("received_at") or row.get("created_at")
                        stored_title = row.get("title")
                except Exception as e:
                    print("DB error in details fallback:", e)

            if not payload_data:
                send_message(chat_id, "❌ Детали не найдены (возможно устарели).", main_keyboard())
                return {"ok": True}

            repo = (payload_data.get("repository") or {}).get("name", "unknown")
            author = (payload_data.get("sender") or {}).get("login", "unknown")
            created_str = fmt_dt(created_at)

            # Build summary WITHOUT the quick-preview/banner and WITHOUT the commit-count line duplicated.
            if stored_title:
                # strip the time block if present
                summary = strip_time_from_title(stored_title)
                # remove any lines that start with the commits marker "📝 " to avoid duplication
                lines = [ln for ln in summary.splitlines() if not ln.strip().startswith("📝 ")]
                summary = "\n".join(lines).strip()
            else:
                # construct summary without commit count line
                summary = (
                    "🔔 GitHub · Push\n\n"
                    f"📦 Репозиторий:\n{md_escape(repo)}\n"
                    f"👤 Автор:\n{md_escape(author)}\n"
                    f"🕒 Время:\n{created_str}\n\n"
                )

            # Prepare commits details
            commits = payload_data.get("commits") or []
            commits_text = ""
            for i, c in enumerate(commits, 1):
                msg = (c.get("message") or "").splitlines()[0]
                url = c.get("url") or c.get("html_url") or ""
                author_c = format_commit_author(c.get("author"))

                commits_text += f"{i}) {md_escape(msg)}"
                if author_c:
                    commits_text += f" — {author_c}"
                if url:
                    commits_text += f"\n   📎 [посмотреть коммит]({url})"
                commits_text += "\n"

            commits_count = len(commits)
            commits_count_line = pluralize_commits(commits_count)

            # details message: do NOT include the previous "quick preview" banner; include commit count once
            details_msg = f"{summary}\n\n"
            if commits_count > 0:
                details_msg += f"📝 {commits_count_line}\n\n*Подробности коммитов:*\n{commits_text}"
            else:
                details_msg += "📝 Нет коммитов в событии.\n"

            send_message(chat_id, details_msg)
            return {"ok": True}

        # MANAGE (edit message into settings)
        if data.startswith("manage:"):
            wid = data.split(":", 1)[1]
            try:
                res = supabase.table("webhooks").select("display_name,events_enabled,connected").eq("id", wid).execute()
                if not res.data:
                    send_message(chat_id, "❌ Сервис не найден.", main_keyboard())
                    return {"ok": True}
                wh = res.data[0]
                events = wh.get("events_enabled") or {"push": True, "pull_request": True, "issues": True}
                display = wh.get("display_name", "GitHub (ожидает подключения)")
                edit_message_text(chat_id, message_id, f"⚙️ *Управление сервисом*\n\n{display}", service_settings_keyboard(wid, events))
                return {"ok": True}
            except Exception as e:
                print("DB error in manage:", e)
                send_message(chat_id, "❌ Ошибка сервера.", main_keyboard())
                return {"ok": True}

        # TOGGLE event type
        if data.startswith("toggle_event:"):
            parts = data.split(":", 2)
            if len(parts) == 3:
                _, wid, event_key = parts
                try:
                    res = supabase.table("webhooks").select("events_enabled").eq("id", wid).execute()
                    if not res.data:
                        send_message(chat_id, "❌ Сервис не найден.", main_keyboard())
                        return {"ok": True}
                    events = res.data[0].get("events_enabled") or {"push": True, "pull_request": True, "issues": True}
                    events[event_key] = not events.get(event_key, True)
                    supabase.table("webhooks").update({"events_enabled": events}).eq("id", wid).execute()
                    edit_message_reply_markup(chat_id, message_id, service_settings_keyboard(wid, events))
                    return {"ok": True}
                except Exception as e:
                    print("DB error in toggle_event:", e)
                    send_message(chat_id, "❌ Ошибка сервера.", main_keyboard())
                    return {"ok": True}
            return {"ok": True}

        # BACK: revert same message to single-service view (do not re-list all services)
        if data.startswith("back:"):
            wid = data.split(":", 1)[1]
            try:
                res = supabase.table("webhooks").select("display_name,connected").eq("id", wid).execute()
                if not res.data:
                    send_message(chat_id, "❌ Сервис не найден.", main_keyboard())
                    return {"ok": True}
                wh = res.data[0]
                display = wh.get("display_name", "GitHub (ожидает подключения)")
                status = "🟢" if wh.get("connected") else "🔴"
                edit_message_text(chat_id, message_id, f"{status} {display}", service_manage_keyboard(wid))
                return {"ok": True}
            except Exception as e:
                print("DB error in back:", e)
                send_message(chat_id, "❌ Ошибка сервера.", main_keyboard())
                return {"ok": True}

        # DELETE: start confirmation (text-based)
        if data.startswith("delete:"):
            wid = data.split(":", 1)[1]
            PENDING_DELETE[chat_id] = wid
            send_message(chat_id, "⚠️ Удалить этот сервис?", confirm_keyboard())
            return {"ok": True}

        return {"ok": True}

    # ---------- MESSAGE handling ----------
    message = payload.get("message")
    if not message:
        return {"ok": True}

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = message.get("text", "").strip()

    # ensure user exists
    try:
        supabase.table("users").upsert({"chat_id": chat_id}, on_conflict="chat_id").execute()
    except Exception as e:
        print("DB upsert user error:", e)

    # pending delete via text
    if chat_id in PENDING_DELETE:
        if text == "ДА":
            try:
                supabase.table("webhooks").delete().eq("id", PENDING_DELETE[chat_id]).execute()
                PENDING_DELETE.pop(chat_id)
                send_message(chat_id, "✅ Сервис удалён", main_keyboard())
            except Exception as e:
                print("DB delete error:", e)
                send_message(chat_id, "❌ Ошибка удаления.", main_keyboard())
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
        try:
            user_row = supabase.table("users").select("id").eq("chat_id", chat_id).execute()
            if not user_row.data:
                send_message(chat_id, "❌ Не получилось создать пользователя. Попробуй ещё раз.", main_keyboard())
                return {"ok": True}
            user_id = user_row.data[0]["id"]

            # Генерируем уникальный secret для этого webhook
            github_secret = secrets.token_hex(32)

            wh = supabase.table("webhooks").insert({
                "user_id": user_id,
                "source": "github",
                "connected": False,
                "display_name": "GitHub (ожидает подключения)",
                "notifications_enabled": True,
                "events_enabled": {"push": True, "pull_request": True, "issues": True},
                "github_secret": github_secret
            }).execute()
            if not wh.data:
                send_message(chat_id, "❌ Не удалось создать webhook. Попробуй позже.", main_keyboard())
                return {"ok": True}
            url = f"{BASE_URL}/webhook/github/{wh.data[0]['id']}"
            send_message(chat_id,
                         "🔗 *Подключение GitHub*\n\n"
                         "1️⃣ Зайди в репозиторий GitHub\n"
                         "2️⃣ Settings → Webhooks → Add webhook\n"
                         "3️⃣ Вставь данные из бота:\n\n"
                         f"*Payload URL:*\n`{url}`\n\n"
                         f"*Secret:*\n`{github_secret}`\n\n"
                         "4️⃣ Content type: `application/json`\n"
                         "5️⃣ Events: Push, Pull requests, Issues\n\n"
                         "После подключения события начнут приходить сюда 👇",
                         main_keyboard())
            return {"ok": True}
        except Exception as e:
            print("DB error creating webhook:", e)
            send_message(chat_id, "❌ Ошибка сервера.", main_keyboard())
            return {"ok": True}

    # show services (explicit command)
    if text == "📦 Мои сервисы":
        try:
            user_row = supabase.table("users").select("id").eq("chat_id", chat_id).execute()
            if not user_row.data:
                send_message(chat_id, "📦 У тебя пока нет сервисов.\n\nЕсли есть вопросы — напиши @ligr5", main_keyboard())
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
        except Exception as e:
            print("DB error show_services:", e)
            send_message(chat_id, "❌ Ошибка сервера.", main_keyboard())
            return {"ok": True}

    # last notifications (first page) — одно компактное сообщение + pager
    if text == "📜 Последние уведомления":
        try:
            user_row = supabase.table("users").select("id").eq("chat_id", chat_id).execute()
            if not user_row.data:
                send_message(chat_id, "❌ Пользователь не найден.", main_keyboard())
                return {"ok": True}
            user_id = user_row.data[0]["id"]

            # total count (try to get count; SDK may provide .count)
            try:
                count_res = supabase.table("events").select("id", count="exact").eq("user_id", user_id).execute()
                total = getattr(count_res, "count", None)
                if total is None:
                    all_res = supabase.table("events").select("id").eq("user_id", user_id).execute()
                    total = len(all_res.data) if all_res.data is not None else 0
            except Exception:
                total = 0

            evs_res = supabase.table("events") \
                .select("id,title,received_at,data,created_at") \
                .eq("user_id", user_id) \
                .order("created_at", desc=True) \
                .range(0, NOTIF_PAGE_SIZE - 1) \
                .execute()

            if not evs_res.data:
                send_message(chat_id, "Пока нет событий.\n\nЕсли кажется, что что-то не работает — напиши @ligr5", main_keyboard())
                return {"ok": True}

            evs = evs_res.data
            for row in evs:
                payload = row.get("data") or {}
                row["repo_url"] = (payload.get("repository") or {}).get("html_url", "")

            text_out, kb = build_notifications_list_short(evs, offset=0, total=total)
            send_message(chat_id, text_out, kb)
            return {"ok": True}
        except Exception as e:
            print("DB error last_notifications:", e)
            send_message(chat_id, "❌ Ошибка сервера.", main_keyboard())
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
# GITHUB WEBHOOK (safe parsing, ping handling, store event)
# =========================
@app.post("/webhook/github/{webhook_id}")
async def github_webhook(webhook_id: str, request: Request):
    # safe read of body
    try:
        body_bytes = await request.body()
    except Exception as e:
        print("Error reading body:", e)
        body_bytes = b""

    # =========================
    # GitHub signature verification (X-Hub-Signature-256)
    # =========================
    signature = request.headers.get("X-Hub-Signature-256")

    try:
        wh_sec = supabase.table("webhooks") \
            .select("github_secret") \
            .eq("id", webhook_id) \
            .execute()
    except Exception as e:
        print("❌ DB error while fetching webhook secret:", e)
        return {"status": "error"}

    if not wh_sec.data:
        print("❌ Webhook not found:", webhook_id)
        return {"status": "unknown_webhook"}

    secret = wh_sec.data[0].get("github_secret")

    if not verify_github_signature(body_bytes, signature, secret):
        print("❌ Invalid GitHub signature")
        return {"status": "forbidden"}
    payload = {}
    if body_bytes:
        try:
            payload = await request.json()
        except JSONDecodeError:
            # not JSON — log preview and continue with empty payload
            try:
                preview = body_bytes.decode("utf-8", errors="replace")[:500]
            except Exception:
                preview = "<unreadable>"
            print("Warning: webhook body not JSON, preview:", preview)
            payload = {}
        except Exception as e:
            print("Unexpected JSON parse error:", e)
            payload = {}
    else:
        # empty body (possible ping)
        payload = {}

    event = (request.headers.get("X-GitHub-Event") or "").lower()

    # Handle ping early — respond 200 and optionally mark connected + update display_name if repo present
    if event == "ping":
        repo = (payload.get("repository") or {}).get("name")

        # получаем webhook + user_id + chat_id
        try:
            wh_res = supabase.table("webhooks") \
                .select("user_id,connected") \
                .eq("id", webhook_id) \
                .execute()
            if not wh_res.data:
                return {"status": "unknown_webhook"}
            wh = wh_res.data[0]
        except Exception as e:
            print("Ping select webhook error:", e)
            return {"status": "error"}

        was_connected = bool(wh.get("connected"))

        update_data = {"connected": True}
        if repo:
            update_data["display_name"] = f"GitHub ({md_escape(repo)})"

        try:
            supabase.table("webhooks") \
                .update(update_data) \
                .eq("id", webhook_id) \
                .execute()
        except Exception as e:
            print("Ping update error:", e)

        # отправляем сообщение ТОЛЬКО при первом подключении
        if not was_connected:
            try:
                user_row = supabase.table("users") \
                    .select("chat_id") \
                    .eq("id", wh["user_id"]) \
                    .execute()
                if user_row.data:
                    chat_id = user_row.data[0]["chat_id"]
                    service_name = f"GitHub ({md_escape(repo)})" if repo else "GitHub"

                    send_message(
                        chat_id,
                        "✅ *Сервис подключён*\n\n"
                        f"{service_name}\n"
                        "Теперь события будут приходить в этот чат 🚀",
                        main_keyboard()
                    )
            except Exception as e:
                print("Ping notify error:", e)

        return {"status": "pong"}


    # Ensure webhook exists
    try:
        wh_res = supabase.table("webhooks").select("user_id,notifications_enabled,events_enabled").eq("id", webhook_id).execute()
    except Exception as e:
        print("DB error selecting webhook:", e)
        return {"status": "error", "reason": "db_select_failed"}

    if not wh_res.data:
        return {"status": "unknown_webhook"}

    wh_row = wh_res.data[0]
    events_enabled = wh_row.get("events_enabled") or {"push": True, "pull_request": True, "issues": True}

    # filter by user settings
    if event == "push" and not events_enabled.get("push", True):
        return {"status": "ignored"}
    if event == "pull_request" and not events_enabled.get("pull_request", True):
        return {"status": "ignored"}
    if event == "issues" and not events_enabled.get("issues", True):
        return {"status": "ignored"}

    repo = (payload.get("repository") or {}).get("name", "unknown")
    repo_url = (payload.get("repository") or {}).get("html_url", "")
    user_id = wh_row.get("user_id")

    # get chat_id
    try:
        user_row = supabase.table("users").select("chat_id").eq("id", user_id).execute()
        if not user_row.data:
            print("No user found for webhook:", webhook_id)
            return {"status": "no_user"}
        chat_id = user_row.data[0]["chat_id"]
    except Exception as e:
        print("DB error selecting user:", e)
        return {"status": "error", "reason": "db_select_failed"}

    # update connected + display_name (only when repo is known)
    update_data = {
        "connected": True
    }

    if repo and repo != "unknown":
        # сохраняем display_name уже экранированным, чтобы при выводе в Telegram подчёркивания показывались
        update_data["display_name"] = f"GitHub ({md_escape(repo)})"

    try:
        supabase.table("webhooks") \
            .update(update_data) \
            .eq("id", webhook_id) \
            .execute()
    except Exception as e:
        print("DB warning updating webhook:", e)

    received_at = datetime.utcnow()
    received_str = received_at.strftime("%d.%m.%Y %H:%M UTC")

    title = ""
    try:
        if event == "push":
            commits = len(payload.get("commits") or [])
            author = (payload.get("sender") or {}).get("login", "unknown")
            commits_line = pluralize_commits(commits)
            # экранируем части для Markdown
            title = (
                "🔔 GitHub · Push\n\n"
                f"📦 Репозиторий:\n{md_escape(repo)}\n"
                f"👤 Автор:\n{md_escape(author)}\n"
                f"🕒 Время:\n{received_str}\n\n"
                f"📝 {commits_line}"
            )
        elif event == "pull_request":
            action = payload.get("action")
            if action in ("opened", "closed"):
                pr = payload.get("pull_request") or {}
                author = (pr.get("user") or {}).get("login", "unknown")
                num = pr.get("number")
                msg = pr.get("title", "")
                state = "влит" if pr.get("merged") else "закрыт" if action == "closed" else "открыт"
                emoji = "✅" if pr.get("merged") else "❌" if action == "closed" else "🔀"
                title = (
                    f"{emoji} GitHub · Pull Request\n\n"
                    f"📦 Репозиторий:\n{md_escape(repo)}\n"
                    f"👤 Автор:\n{md_escape(author)}\n"
                    f"🕒 Время:\n{received_str}\n\n"
                    f"📝 PR #{num} {state}:\n{md_escape(msg)}"
                )
                repo_url = pr.get("html_url") or repo_url
            else:
                return {"status": "ignored"}
        elif event == "issues":
            action = payload.get("action")
            if action in ("opened", "closed"):
                issue = payload.get("issue") or {}
                author = (issue.get("user") or {}).get("login", "unknown")
                num = issue.get("number")
                msg = issue.get("title", "")
                emoji = "🐞" if action == "opened" else "✅"
                title = (
                    f"{emoji} GitHub · Issue\n\n"
                    f"📦 Репозиторий:\n{md_escape(repo)}\n"
                    f"👤 Автор:\n{md_escape(author)}\n"
                    f"🕒 Время:\n{received_str}\n\n"
                    f"📝 Issue #{num} {action}:\n{md_escape(msg)}"
                )
                repo_url = issue.get("html_url") or repo_url
            else:
                return {"status": "ignored"}
        else:
            return {"status": "ignored"}
    except Exception as e:
        print("Error building title:", e)
        return {"status": "error", "reason": "title_build_failed"}

    # store in memory for quick details
    event_id = str(uuid.uuid4())
    GITHUB_EVENTS[event_id] = {"payload": payload, "created_at": received_at}

    # save to DB (try with received_at first, fallback without)
    event_record = {
        "user_id": user_id,
        "source": "github",
        "title": title,
        "data": payload,
        "received_at": received_at.isoformat()
    }
    try:
        supabase.table("events").insert(event_record).execute()
    except Exception as e:
        print("Insert with received_at failed, trying without:", e)
        try:
            event_record.pop("received_at", None)
            supabase.table("events").insert(event_record).execute()
        except Exception as e2:
            print("Insert without received_at failed:", e2)
            return {"status": "error", "reason": "db_insert_failed"}

    # check notifications_enabled
    notify = bool(wh_row.get("notifications_enabled", True))
    if notify:
        try:
            send_message(chat_id, title, github_event_keyboard(event_id, repo_url))
        except Exception as e:
            print("Failed to send telegram message:", e)

    return {"status": "ok"}