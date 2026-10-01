import os
import asyncio
import random
import string
import datetime
import aiosqlite
from dotenv import load_dotenv
from telethon import TelegramClient, errors
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, filters, ContextTypes
)

load_dotenv()

# ============ КОНФИГ ============
ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
DB_PATH = "broadcaster.db"
SESSION_NAME = "userbot_session"

# ============ СОСТОЯНИЯ ДИАЛОГА ============
(
    ASK_NORMAL_TEXT, ASK_SAFE_TEXT_1, ASK_SAFE_TEXT_2,
    ASK_SAFE_TEXT_3, ASK_INTERVAL, ASK_RECIPIENTS
) = range(6)


# ============ БАЗА ДАННЫХ ============
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS activation_keys (
                key TEXT PRIMARY KEY,
                duration_days INTEGER,
                activated_by INTEGER,
                activated_at TIMESTAMP,
                expires_at TIMESTAMP,
                is_used BOOLEAN DEFAULT FALSE
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS recipients (
                user_id INTEGER PRIMARY KEY,
                username TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                is_running INTEGER DEFAULT 0,
                mode TEXT DEFAULT 'safe'
            )
        """)
        await db.execute(
            "INSERT OR IGNORE INTO broadcast_state (id, is_running, mode) VALUES (1, 0, 'safe')"
        )
        await db.commit()


async def db_set(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value)
        )
        await db.commit()


async def db_get(key: str, default=None):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else default


async def create_key(key: str, duration_days: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO activation_keys (key, duration_days) VALUES (?, ?)",
            (key, duration_days)
        )
        await db.commit()


async def activate_key(key: str, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT duration_days, is_used FROM activation_keys WHERE key = ?",
            (key,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None, "Ключ не найден"
        duration_days, is_used = row
        if is_used:
            return None, "Ключ уже использован"
        now = datetime.datetime.now()
        expires_at = None if duration_days == -1 else now + datetime.timedelta(days=duration_days)
        await db.execute(
            """UPDATE activation_keys
               SET is_used = TRUE, activated_by = ?, activated_at = ?, expires_at = ?
               WHERE key = ?""",
            (user_id, now, expires_at, key)
        )
        await db.commit()
        return expires_at, None


async def check_access(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT expires_at FROM activation_keys
               WHERE activated_by = ? AND is_used = TRUE
               ORDER BY activated_at DESC LIMIT 1""",
            (user_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return False
        expires_at = row[0]
        if expires_at is None:
            return True
        return datetime.datetime.now() < datetime.datetime.fromisoformat(expires_at)


async def get_recipients():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM recipients") as cur:
            return [row[0] for row in await cur.fetchall()]


async def add_recipients(user_ids: list[int]):
    async with aiosqlite.connect(DB_PATH) as db:
        for uid in user_ids:
            await db.execute(
                "INSERT OR IGNORE INTO recipients (user_id) VALUES (?)", (uid,)
            )
        await db.commit()


async def clear_recipients():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM recipients")
        await db.commit()


async def set_running(is_running: bool, mode: str = "safe"):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE broadcast_state SET is_running = ?, mode = ? WHERE id = 1",
            (1 if is_running else 0, mode)
        )
        await db.commit()


async def get_state():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT is_running, mode FROM broadcast_state WHERE id = 1") as cur:
            row = await cur.fetchone()
            return {"is_running": bool(row[0]), "mode": row[1]}


# ============ USERBOT (Telethon) ============
userbot_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)


async def safe_broadcast(client, recipients, texts, base_interval):
    """Безопасная рассылка: 3 текста + ±20% интервал + circuit breaker"""
    flood_count = 0
    FLOOD_LIMIT = 3
    for i, user in enumerate(recipients):
        state = await get_state()
        if not state["is_running"]:
            print("[STOP] Рассылка остановлена вручную")
            return

        if flood_count >= FLOOD_LIMIT:
            print("[CIRCUIT BREAKER] Слишком много FloodWait, остановка")
            break

        text = texts[i % len(texts)]
        try:
            await client.send_message(user, text)
            jittered = base_interval * random.uniform(0.8, 1.2)
            print(f"[{i+1}/{len(recipients)}] Отправлено, пауза {jittered:.1f}с")
            await asyncio.sleep(jittered)
        except errors.FloodWait as e:
            flood_count += 1
            sleep_time = e.value * 1.3
            print(f"[FLOOD_WAIT] {e.value}с, спим {sleep_time:.1f}с")
            await asyncio.sleep(sleep_time)
        except errors.PeerFloodError:
            print("[PEER_FLOOD] Аккаунт ограничен. Остановка.")
            break
        except Exception as ex:
            print(f"[ERR] {user}: {ex}")


async def normal_broadcast(client, recipients, text, interval):
    """Обычная рассылка: 1 текст, фиксированный интервал"""
    for i, user in enumerate(recipients):
        state = await get_state()
        if not state["is_running"]:
            print("[STOP] Рассылка остановлена вручную")
            return
        try:
            await client.send_message(user, text)
            print(f"[{i+1}/{len(recipients)}] Отправлено, пауза {interval}с")
            await asyncio.sleep(interval)
        except errors.FloodWait as e:
            await asyncio.sleep(e.value * 1.3)
        except errors.PeerFloodError:
            print("[PEER_FLOOD] Остановка.")
            break
        except Exception as ex:
            print(f"[ERR] {user}: {ex}")


async def run_broadcast():
    """Запуск рассылки по текущему состоянию"""
    recipients = await get_recipients()
    if not recipients:
        print("Нет получателей")
        await set_running(False)
        return

    interval = int(await db_get("interval", "60"))
    mode = (await get_state())["mode"]

    if mode == "normal":
        text = await db_get("text_normal", "Привет!")
        await normal_broadcast(userbot_client, recipients, text, interval)
    else:
        texts = [
            await db_get("text_safe1", "Привет!"),
            await db_get("text_safe2", "Здравствуйте!"),
            await db_get("text_safe3", "Добрый день!"),
        ]
        await safe_broadcast(userbot_client, recipients, texts, interval)

    await set_running(False)
    print("[DONE] Рассылка завершена")


async def userbot_watcher():
    """Следит за флагом is_running и запускает рассылку"""
    await userbot_client.start()
    print("[USERBOT] Запущен")

    was_running = False
    while True:
        state = await get_state()
        if state["is_running"] and not was_running:
            was_running = True
            asyncio.create_task(run_broadcast())
        elif not state["is_running"]:
            was_running = False
        await asyncio.sleep(3)


# ============ АДМИН-БОТ (python-telegram-bot) ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id == ADMIN_ID:
        keyboard = [
            [InlineKeyboardButton("🔑 Создать ключ", callback_data="menu_create_key")],
            [InlineKeyboardButton("📝 Настроить тексты", callback_data="menu_texts")],
            [InlineKeyboardButton("⏱ Интервал", callback_data="menu_interval")],
            [InlineKeyboardButton("👥 Получатели", callback_data="menu_recipients")],
            [InlineKeyboardButton("▶️ Обычная рассылка", callback_data="run_normal")],
            [InlineKeyboardButton("🛡 Безопасная рассылка", callback_data="run_safe")],
            [InlineKeyboardButton("⛔ Остановить", callback_data="stop_broadcast")],
        ]
        await update.message.reply_text(
            "🔧 Админ-панель", reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            "Введите ключ активации:\n/activate ВАШ_КЛЮЧ"
        )


async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /activate ВАШ_КЛЮЧ")
        return
    key = context.args[0]
    user_id = update.effective_user.id
    expires_at, error = await activate_key(key, user_id)
    if error:
        await update.message.reply_text(f"❌ {error}")
    elif expires_at:
        await update.message.reply_text(
            f"✅ Доступ до {expires_at.strftime('%d.%m.%Y %H:%M')}"
        )
    else:
        await update.message.reply_text("✅ Бессрочный доступ активирован!")


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "menu_create_key":
        kb = [[InlineKeyboardButton(f"{d} дн.", callback_data=f"key_{d}")]
              for d in [1, 2, 3, 4, 30, 360]]
        kb.append([InlineKeyboardButton("♾ Безлимит", callback_data="key_-1")])
        await query.edit_message_text("Срок действия ключа:",
                                       reply_markup=InlineKeyboardMarkup(kb))

    elif data.startswith("key_"):
        days = int(data.split("_")[1])
        key = ''.join(random.choices(string.ascii_uppercase + string.digits, k=16))
        await create_key(key, days)
        label = "бессрочно" if days == -1 else f"{days} дн."
        await query.edit_message_text(f"✅ Ключ ({label}):\n`{key}`",
                                       parse_mode="Markdown")

    elif data == "menu_texts":
        await query.edit_message_text(
            "Отправьте команды:\n"
            "/set_normal <текст>\n"
            "/set_safe1 <текст>\n"
            "/set_safe2 <текст>\n"
            "/set_safe3 <текст>"
        )

    elif data == "menu_interval":
        await query.edit_message_text("Введите: /set_interval <секунды>")

    elif data == "menu_recipients":
        await query.edit_message_text(
            "Управление получателями:\n"
            "/add_recipients <id1,id2,id3>\n"
            "/clear_recipients\n"
            "/list_recipients"
        )

    elif data == "run_normal":
        await set_running(True, "normal")
        await query.edit_message_text("▶️ Обычная рассылка запущена")

    elif data == "run_safe":
        await set_running(True, "safe")
        await query.edit_message_text("🛡 Безопасная рассылка запущена")

    elif data == "stop_broadcast":
        await set_running(False)
        await query.edit_message_text("⛔ Остановлено")


async def set_normal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    text = " ".join(context.args)
    await db_set("text_normal", text)
    await update.message.reply_text("✅ Текст обычной рассылки сохранён")


async def set_safe1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await db_set("text_safe1", " ".join(context.args))
    await update.message.reply_text("✅ Текст 1 сохранён")


async def set_safe2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await db_set("text_safe2", " ".join(context.args))
    await update.message.reply_text("✅ Текст 2 сохранён")


async def set_safe3(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await db_set("text_safe3", " ".join(context.args))
    await update.message.reply_text("✅ Текст 3 сохранён")


async def set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        interval = int(context.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /set_interval <секунды>")
        return
    await db_set("interval", str(interval))
    await update.message.reply_text(f"✅ Интервал: {interval} сек")


async def add_recipients_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Использование: /add_recipients id1,id2,id3")
        return
    try:
        ids = [int(x.strip()) for x in context.args[0].split(",") if x.strip()]
    except ValueError:
        await update.message.reply_text("Неверный формат ID")
        return
    await add_recipients(ids)
    await update.message.reply_text(f"✅ Добавлено {len(ids)} получателей")


async def clear_recipients_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await clear_recipients()
    await update.message.reply_text("✅ Список получателей очищен")


async def list_recipients_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    rec = await get_recipients()
    if not rec:
        await update.message.reply_text("Список пуст")
        return
    preview = ", ".join(str(x) for x in rec[:50])
    await update.message.reply_text(f"Всего: {len(rec)}\n{preview}")


# ============ ЗАПУСК ============
async def main():
    await init_db()
    print("[DB] Инициализирована")

    # Запуск userbot + watcher
    asyncio.create_task(userbot_watcher())

    # Запуск админ-бота
    app = Application.builder().token(ADMIN_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("activate", activate))
    app.add_handler(CommandHandler("set_normal", set_normal))
    app.add_handler(CommandHandler("set_safe1", set_safe1))
    app.add_handler(CommandHandler("set_safe2", set_safe2))
    app.add_handler(CommandHandler("set_safe3", set_safe3))
    app.add_handler(CommandHandler("set_interval", set_interval))
    app.add_handler(CommandHandler("add_recipients", add_recipients_cmd))
    app.add_handler(CommandHandler("clear_recipients", clear_recipients_cmd))
    app.add_handler(CommandHandler("list_recipients", list_recipients_cmd))
    app.add_handler(CallbackQueryHandler(admin_callback))

    print("[ADMIN] Бот запущен")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    # Держим процесс живым
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
