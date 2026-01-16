import os
import requests
from fastapi import FastAPI, Body
from dotenv import load_dotenv
from supabase import create_client

# -------------------------
# ENV
# -------------------------
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI()

# -------------------------
# Keyboards (reply keyboards)
# -------------------------
def keyboard_main():
    return {
        "keyboard": [
            [{"text": "➕ Подключить сервис"}],
            [{"text": "📦 Мои сервисы"}, {"text": "🗑️ Управление"}],
            [{"text": "ℹ️ Помощь"}, {"text": "🔁 Главное меню"}]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }

def keyboard_services():
    return {
        "keyboard": [
            [{"text": "GitHub"}, {"text": "GitLab (скоро)"}],
            [{"text": "Notion (скоро)"}, {"text": "Webhook (custom)"}],
            [{"text": "⬅️ Назад"}]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }

def keyboard_back_to_main():
    return {
        "keyboard": [
            [{"text": "🔁 Главное меню"}]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }

def keyboard_manage():
    return {
        "keyboard": [
            [{"text": "Удалить сервис"}, {"text": "Обновить список"}],
            [{"text": "⬅️ Назад"}]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }

# -------------------------
# Telegram helpers
# -------------------------
def send_message(chat_id: int, text: str, reply_markup: dict | None = None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    r = requests.post(url, json=payload)
    if r.status_code != 200:
        print("Telegram send error:", r.status_code, r.text)

# -------------------------
# Health
# -------------------------
@app.get("/health")
async def health():
    return {"status": "ok"}

# -------------------------
# Telegram webhook (message-based keyboard UX)
# -------------------------
@app.post("/telegram")
async def telegram_update(payload: dict = Body(...)):
    # only accept regular messages (we no longer use callback_query)
    message = payload.get("message")
    if not message:
        return {"ok": True}

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    text = message.get("text", "").strip()

    # idempotent upsert: если уже есть — обновит, иначе вставит
    try:
        supabase.table("users").upsert(
            {"chat_id": chat_id},
            on_conflict="chat_id"
        ).execute()
    except Exception as e:
        # логируем, но не падаем
        print("Supabase upsert error:", e)

    # ---- Main flows: text equal to the keyboard labels ----
    if text in ("/start", "🔁 Главное меню", "Главное меню"):
        send_message(
            chat_id,
            "👋 *Integration Hub*\n\n"
            "Подключай сервисы и получай уведомления прямо в Telegram.\n\n"
            "Выбери действие кнопкой ниже 👇",
            keyboard_main()
        )
        return {"ok": True}

    if text == "➕ Подключить сервис":
        send_message(
            chat_id,
            "➕ *Подключение сервиса*\n\nВыбери сервис для подключения:",
            keyboard_services()
        )
        return {"ok": True}

    if text == "GitHub":
        # получаем user id
        user_resp = supabase.table("users").select("id").eq("chat_id", chat_id).execute()
        if not user_resp.data:
            send_message(chat_id, "❌ Пользователь не найден. Нажми /start.", keyboard_back_to_main())
            return {"ok": True}
        user_id = user_resp.data[0]["id"]
        webhook = supabase.table("webhooks").insert({
            "user_id": user_id,
            "source": "github"
        }).execute()
        webhook_id = webhook.data[0]["id"]
        webhook_url = f"{BASE_URL}/webhook/github/{webhook_id}"

        send_message(
            chat_id,
            "🔗 *GitHub подключение*\n\n"
            "1) Зайди в репозиторий GitHub\n"
            "2) Settings → Webhooks → Add webhook\n"
            f"3) Payload URL: `{webhook_url}`\n"
            "4) Content type: `application/json`\n"
            "5) Events: `Push`\n\n"
            "После этого коммиты будут приходить в этот чат.",
            keyboard_main()
        )
        return {"ok": True}

    if text == "Webhook (custom)":
        # quick generic webhook creation
        user_resp = supabase.table("users").select("id").eq("chat_id", chat_id).execute()
        if not user_resp.data:
            send_message(chat_id, "❌ Пользователь не найден. Нажми /start.", keyboard_back_to_main())
            return {"ok": True}
        user_id = user_resp.data[0]["id"]
        webhook = supabase.table("webhooks").insert({
            "user_id": user_id,
            "source": "custom"
        }).execute()
        webhook_id = webhook.data[0]["id"]
        webhook_url = f"{BASE_URL}/webhook/custom/{webhook_id}"
        send_message(
            chat_id,
            "🔗 *Generic Webhook (custom)*\n\n"
            f"POST your JSON to `{webhook_url}`\n\n"
            "Мы автоматически отправим красиво отформатированное сообщение в Telegram.",
            keyboard_main()
        )
        return {"ok": True}

    if text == "📦 Мои сервисы":
        # показать только сервисы текущего пользователя
        user_resp = supabase.table("users").select("id").eq("chat_id", chat_id).execute()
        if not user_resp.data:
            send_message(chat_id, "У вас ещё нет аккаунта, нажмите /start.", keyboard_main())
            return {"ok": True}
        user_id = user_resp.data[0]["id"]
        services = supabase.table("webhooks").select("id,source,created_at").eq("user_id", user_id).execute()
        if not services.data:
            send_message(chat_id, "📦 У вас пока нет подключённых сервисов.", keyboard_main())
            return {"ok": True}
        text_out = "📦 *Ваши сервисы:*\n\n"
        for row in services.data:
            text_out += f"• `{row['source']}` — id: `{row['id']}`\n"
        send_message(chat_id, text_out, keyboard_manage())
        return {"ok": True}

    if text == "Обновить список":
        # просто триггер для пересоздания списка
        send_message(chat_id, "Обновляю список...", keyboard_main())
        # делаем вызов "Мои сервисы"
        return await telegram_update({"message": {"chat": {"id": chat_id}, "text": "📦 Мои сервисы"}})

    if text == "Удалить сервис":
        send_message(chat_id,
                     "Чтобы удалить сервис, отправь сообщение в формате:\n`Удалить <id>`\nПример: `Удалить eb3a-...`",
                     keyboard_back_to_main())
        return {"ok": True}

    if text.startswith("Удалить "):
        parts = text.split()
        if len(parts) >= 2:
            token = parts[1].strip()
            # удаляем webhook только если он принадлежит пользователю
            user_resp = supabase.table("users").select("id").eq("chat_id", chat_id).execute()
            if not user_resp.data:
                send_message(chat_id, "Пользователь не найден.", keyboard_main())
                return {"ok": True}
            user_id = user_resp.data[0]["id"]
            # проверяем наличие
            wh = supabase.table("webhooks").select("id").eq("id", token).eq("user_id", user_id).execute()
            if not wh.data:
                send_message(chat_id, "Сервис не найден или не принадлежит вам.", keyboard_main())
                return {"ok": True}
            supabase.table("webhooks").delete().eq("id", token).execute()
            send_message(chat_id, "✅ Сервис удалён.", keyboard_main())
            return {"ok": True}

    if text == "ℹ️ Помощь":
        send_message(chat_id,
                     "ℹ️ *Помощь*\n\n"
                     "• Используй клавиатуру для управления\n"
                     "• Подключай GitHub и Generic Webhook\n"
                     "• Для удаления используйте `Удалить <id>`\n\n"
                     "Если нужно — пиши разработчику.",
                     keyboard_main())
        return {"ok": True}

    # default: если не распознано
    send_message(chat_id,
                 "Не понял команду. Используй клавиатуру ниже.",
                 keyboard_main())
    return {"ok": True}

# -------------------------
# GITHUB webhook (same safe handling)
# -------------------------
@app.post("/webhook/github/{webhook_id}")
async def github_webhook(webhook_id: str, payload: dict = Body(...)):
    webhook_resp = supabase.table("webhooks").select("user_id,source").eq("id", webhook_id).execute()
    if not webhook_resp.data:
        print(f"Unknown webhook_id: {webhook_id}")
        return {"status": "unknown webhook"}

    user_id = webhook_resp.data[0]["user_id"]
    user_resp = supabase.table("users").select("chat_id").eq("id", user_id).execute()
    if not user_resp.data:
        print(f"User not found for webhook: {webhook_id}")
        return {"status": "unknown user"}

    chat_id = user_resp.data[0]["chat_id"]
    repo = payload.get("repository", {}).get("name", "unknown")
    author = payload.get("sender", {}).get("login", "unknown")
    # более аккуратный формат сообщения
    message = f"🔔 *GitHub push*\nRepository: `{repo}`\nAuthor: `{author}`"
    send_message(chat_id, message)
    return {"status": "ok"}

# -------------------------
# GENERIC custom webhook
# -------------------------
@app.post("/webhook/custom/{webhook_id}")
async def custom_webhook(webhook_id: str, payload: dict = Body(...)):
    webhook_resp = supabase.table("webhooks").select("user_id,source").eq("id", webhook_id).execute()
    if not webhook_resp.data:
        print(f"Unknown custom webhook_id: {webhook_id}")
        return {"status": "unknown webhook"}
    user_id = webhook_resp.data[0]["user_id"]
    user_resp = supabase.table("users").select("chat_id").eq("id", user_id).execute()
    if not user_resp.data:
        print(f"User not found for custom webhook: {webhook_id}")
        return {"status": "unknown user"}

    chat_id = user_resp.data[0]["chat_id"]
    # постим упрощённый prettified JSON
    try:
        import json
        pretty = json.dumps(payload, ensure_ascii=False, indent=2)
        text = f"🔔 *Custom webhook event*\n```json\n{pretty}\n```"
    except Exception:
        text = "🔔 *Custom webhook event*\n(не смогли отобразить тело)"
    send_message(chat_id, text)
    return {"status": "ok"}
