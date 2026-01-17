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
# KEYBOARDS (REPLY)
# =========================
def keyboard_main():
    return {
        "keyboard": [
            [{"text": "➕ Подключить сервис"}],
            [{"text": "📦 Мои сервисы"}, {"text": "🗑️ Управление"}],
            [{"text": "ℹ️ Помощь"}, {"text": "🔁 Главное меню"}]
        ],
        "resize_keyboard": True
    }

def keyboard_services():
    return {
        "keyboard": [
            [{"text": "GitHub"}, {"text": "GitLab (скоро)"}],
            [{"text": "Notion (скоро)"}, {"text": "Webhook (custom)"}],
            [{"text": "⬅️ Назад"}]
        ],
        "resize_keyboard": True
    }

def keyboard_manage():
    return {
        "keyboard": [
            [{"text": "Удалить сервис"}, {"text": "Обновить список"}],
            [{"text": "⬅️ Назад"}]
        ],
        "resize_keyboard": True
    }

# =========================
# TELEGRAM HELPERS
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
    r = requests.post(url, json=payload)
    if r.status_code != 200:
        print("Telegram error:", r.text)

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

    # ---- user upsert (safe) ----
    try:
        supabase.table("users").upsert(
            {"chat_id": chat_id},
            on_conflict="chat_id"
        ).execute()
    except Exception as e:
        print("Supabase upsert error:", e)

    # =========================
    # MAIN NAVIGATION
    # =========================
    if text in ("/start", "🔁 Главное меню"):
        send_message(
            chat_id,
            "👋 *Integration Hub*\n\n"
            "Подключай сервисы и получай уведомления прямо в Telegram.\n\n"
            "Выбери действие кнопкой ниже 👇",
            keyboard_main()
        )
        return {"ok": True}

    if text == "⬅️ Назад":
        send_message(
            chat_id,
            "🔁 Возвращаюсь в главное меню.",
            keyboard_main()
        )
        return {"ok": True}

    # =========================
    # CONNECT SERVICE
    # =========================
    if text == "➕ Подключить сервис":
        send_message(
            chat_id,
            "➕ *Подключение сервиса*\n\nВыбери сервис:",
            keyboard_services()
        )
        return {"ok": True}

    if text == "GitHub":
        user = supabase.table("users").select("id").eq("chat_id", chat_id).execute()
        if not user.data:
            send_message(chat_id, "❌ Пользователь не найден.", keyboard_main())
            return {"ok": True}

        user_id = user.data[0]["id"]
        webhook = supabase.table("webhooks").insert({
            "user_id": user_id,
            "source": "github"
        }).execute()

        webhook_id = webhook.data[0]["id"]
        webhook_url = f"{BASE_URL}/webhook/github/{webhook_id}"

        send_message(
            chat_id,
            "🔗 *GitHub подключение*\n\n"
            "1️⃣ Зайди в репозиторий GitHub\n"
            "2️⃣ Settings → Webhooks → Add webhook\n"
            f"3️⃣ Payload URL:\n`{webhook_url}`\n"
            "4️⃣ Content type: `application/json`\n"
            "5️⃣ Events: Push\n\n"
            "После этого коммиты начнут приходить сюда 👇",
            keyboard_main()
        )
        return {"ok": True}

    if text == "Webhook (custom)":
        user = supabase.table("users").select("id").eq("chat_id", chat_id).execute()
        if not user.data:
            send_message(chat_id, "❌ Пользователь не найден.", keyboard_main())
            return {"ok": True}

        user_id = user.data[0]["id"]
        webhook = supabase.table("webhooks").insert({
            "user_id": user_id,
            "source": "custom"
        }).execute()

        webhook_id = webhook.data[0]["id"]
        webhook_url = f"{BASE_URL}/webhook/custom/{webhook_id}"

        send_message(
            chat_id,
            "🔔 *Generic Webhook*\n\n"
            f"Отправляй POST JSON на:\n`{webhook_url}`\n\n"
            "Любые данные придут в этот чат.",
            keyboard_main()
        )
        return {"ok": True}

    # =========================
    # SERVICES LIST / MANAGEMENT
    # =========================
    if text == "📦 Мои сервисы":
        user = supabase.table("users").select("id").eq("chat_id", chat_id).execute()
        if not user.data:
            send_message(chat_id, "Нет данных пользователя.", keyboard_main())
            return {"ok": True}

        user_id = user.data[0]["id"]
        services = supabase.table("webhooks").select("id,source").eq("user_id", user_id).execute()

        if not services.data:
            send_message(chat_id, "📦 У тебя пока нет подключённых сервисов.", keyboard_main())
            return {"ok": True}

        text_out = "📦 *Твои сервисы:*\n\n"
        for s in services.data:
            text_out += f"• `{s['source']}` — `{s['id']}`\n"

        send_message(chat_id, text_out, keyboard_manage())
        return {"ok": True}

    if text == "🗑️ Управление":
        send_message(
            chat_id,
            "🗑️ *Управление сервисами*\n\n"
            "Выбери действие:",
            keyboard_manage()
        )
        return {"ok": True}

    if text == "Обновить список":
        return await telegram_update({
            "message": {
                "chat": {"id": chat_id},
                "text": "📦 Мои сервисы"
            }
        })

    if text == "Удалить сервис":
        send_message(
            chat_id,
            "❌ Чтобы удалить сервис, отправь:\n"
            "`Удалить <id>`\n\n"
            "Пример:\n`Удалить 123e4567-...`",
            keyboard_main()
        )
        return {"ok": True}

    if text.startswith("Удалить "):
        token = text.replace("Удалить ", "").strip()
        user = supabase.table("users").select("id").eq("chat_id", chat_id).execute()
        if not user.data:
            send_message(chat_id, "Пользователь не найден.", keyboard_main())
            return {"ok": True}

        user_id = user.data[0]["id"]
        wh = supabase.table("webhooks").select("id").eq("id", token).eq("user_id", user_id).execute()

        if not wh.data:
            send_message(chat_id, "❌ Сервис не найден или не твой.", keyboard_main())
            return {"ok": True}

        supabase.table("webhooks").delete().eq("id", token).execute()
        send_message(chat_id, "✅ Сервис удалён.", keyboard_main())
        return {"ok": True}

    # =========================
    # HELP
    # =========================
    if text == "ℹ️ Помощь":
        send_message(
            chat_id,
            "ℹ️ *Помощь*\n\n"
            "• Используй кнопки для навигации\n"
            "• Каждый сервис имеет свой webhook\n"
            "• Можно подключать несколько сервисов\n\n"
            "Если что-то не работает — просто вернись в меню.",
            keyboard_main()
        )
        return {"ok": True}

    # =========================
    # FALLBACK
    # =========================
    send_message(
        chat_id,
        "❓ Не понял команду. Используй кнопки ниже 👇",
        keyboard_main()
    )
    return {"ok": True}

# =========================
# GITHUB WEBHOOK
# =========================
@app.post("/webhook/github/{webhook_id}")
async def github_webhook(webhook_id: str, payload: dict = Body(...)):
    wh = supabase.table("webhooks").select("user_id").eq("id", webhook_id).execute()
    if not wh.data:
        return {"status": "unknown webhook"}

    user_id = wh.data[0]["user_id"]
    user = supabase.table("users").select("chat_id").eq("id", user_id).execute()
    if not user.data:
        return {"status": "unknown user"}

    chat_id = user.data[0]["chat_id"]
    repo = payload.get("repository", {}).get("name", "unknown")
    author = payload.get("sender", {}).get("login", "unknown")

    send_message(
        chat_id,
        f"🔔 *GitHub push*\nRepository: `{repo}`\nAuthor: `{author}`"
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

    user_id = wh.data[0]["user_id"]
    user = supabase.table("users").select("chat_id").eq("id", user_id).execute()
    if not user.data:
        return {"status": "unknown user"}

    chat_id = user.data[0]["chat_id"]

    import json
    pretty = json.dumps(payload, ensure_ascii=False, indent=2)
    send_message(
        chat_id,
        f"🔔 *Custom webhook*\n```json\n{pretty}\n```"
    )
    return {"status": "ok"}
