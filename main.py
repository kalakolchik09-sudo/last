import os
import asyncio
import random
import string
import datetime
import aiosqlite
from dotenv import load_dotenv
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, filters, ContextTypes
)

load_dotenv()

ADMIN_BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
DB_PATH = "broadcaster.db"

# ============ СОСТОЯНИЯ ============
(
    ASK_LICENSE,
    ASK_PHONE, ASK_CODE, ASK_2FA,
    ASK_MODE,
    ASK_NORMAL_TEXT,
    ASK_SAFE_TEXT_1, ASK_SAFE_TEXT_2, ASK_SAFE_TEXT_3,
    ASK_INTERVAL,
) = range(10)

# Активные сессии авторизации (в памяти)
pending_auth = {}          # user_id -> dict(client, phone, phone_code_hash)
active_clients = {}        # user_id -> TelegramClient (активные userbot'ы)
running_broadcasts = {}    # user_id -> bool флаг остановки


# ==================== БД ====================
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
            CREATE TABLE IF NOT EXISTS user_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                session_string TEXT,
                phone TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                mode TEXT,
                normal_text TEXT,
                safe_text1 TEXT,
                safe_text2 TEXT,
                safe_text3 TEXT,
                interval INTEGER
            )
        """)
        await db.commit()


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
        exp = row[0]
        if exp is None:
            return True
        return datetime.datetime.now() < datetime.datetime.fromisoformat(exp)


async def save_account(user_id: int, session_string: str, phone: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO user_accounts (user_id, session_string, phone) VALUES (?, ?, ?)",
            (user_id, session_string, phone)
        )
        await db.commit()


async def get_accounts(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id, session_string, phone FROM user_accounts WHERE user_id = ?",
            (user_id,)
        ) as cur:
            return await cur.fetchall()


async def delete_account(account_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM user_accounts WHERE id = ?", (account_id,))
        await db.commit()


async def save_settings(user_id: int, **kwargs):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT user_id FROM user_settings WHERE user_id = ?", (user_id,)
        ) as cur:
            exists = await cur.fetchone()
        if not exists:
            await db.execute(
                "INSERT INTO user_settings (user_id) VALUES (?)", (user_id,)
            )
        for k, v in kwargs.items():
            await db.execute(
                f"UPDATE user_settings SET {k} = ? WHERE user_id = ?",
                (v, user_id)
            )
        await db.commit()


async def get_settings(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.row_factory
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM user_settings WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


# ==================== АВТОРИЗАЦИЯ АККАУНТА ====================
async def start_auth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало добавления аккаунта"""
    user_id = update.effective_user.id
    if not await check_access(user_id):
        await update.message.reply_text("❌ Нет активной лицензии")
        return ConversationHandler.END

    await update.message.reply_text(
        "📱 Введите номер телефона в формате +380XXXXXXXXX\n"
        "Отмена: /cancel"
    )
    return ASK_PHONE


async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    phone = update.message.text.strip()

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()

    try:
        sent = await client.send_code_request(phone)
        pending_auth[user_id] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": sent.phone_code_hash,
        }
        await update.message.reply_text(
            "📨 Введите код из Telegram (только цифры, например 12345):"
        )
        return ASK_CODE
    except Exception as e:
        await client.disconnect()
        await update.message.reply_text(f"❌ Ошибка: {e}\nПопробуйте /add_account")
        return ConversationHandler.END


async def ask_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    code = update.message.text.strip().replace(" ", "")

    auth = pending_auth.get(user_id)
    if not auth:
        await update.message.reply_text("Сессия истекла. /add_account")
        return ConversationHandler.END

    client = auth["client"]
    try:
        await client.sign_in(
            phone=auth["phone"],
            code=code,
            phone_code_hash=auth["phone_code_hash"]
        )
    except errors.SessionPasswordNeededError:
        await update.message.reply_text(
            "🔐 Введите пароль двухфакторной аутентификации (2FA):"
        )
        return ASK_2FA
    except Exception as e:
        await client.disconnect()
        pending_auth.pop(user_id, None)
        await update.message.reply_text(f"❌ Ошибка входа: {e}")
        return ConversationHandler.END

    # Успешный вход
    session_str = client.session.save()
    await save_account(user_id, session_str, auth["phone"])
    await client.disconnect()
    pending_auth.pop(user_id, None)

    await update.message.reply_text(
        f"✅ Аккаунт {auth['phone']} добавлен!\n\nИспользуйте /menu"
    )
    return ConversationHandler.END


async def ask_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    password = update.message.text.strip()

    auth = pending_auth.get(user_id)
    if not auth:
        await update.message.reply_text("Сессия истекла. /add_account")
        return ConversationHandler.END

    client = auth["client"]
    try:
        await client.sign_in(password=password)
    except Exception as e:
        await client.disconnect()
        pending_auth.pop(user_id, None)
        await update.message.reply_text(f"❌ Ошибка 2FA: {e}")
        return ConversationHandler.END

    session_str = client.session.save()
    await save_account(user_id, session_str, auth["phone"])
    await client.disconnect()
    pending_auth.pop(user_id, None)

    await update.message.reply_text(
        f"✅ Аккаунт {auth['phone']} добавлен!\n\nИспользуйте /menu"
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    auth = pending_auth.pop(user_id, None)
    if auth:
        await auth["client"].disconnect()
    await update.message.reply_text("Отменено")
    return ConversationHandler.END


# ==================== ПОЛУЧЕНИЕ ГРУПП ====================
async def get_user_groups(client: TelegramClient):
    """Возвращает список групп (id) где состоит аккаунт"""
    groups = []
    async for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            # Пропускаем broadcast-каналы (write forbidden)
            if dialog.is_channel and not dialog.entity.broadcast:
                groups.append(dialog.id)
            elif dialog.is_group:
                groups.append(dialog.id)
    return groups


# ==================== РАССЫЛКА ====================
async def run_broadcast(user_id: int, bot):
    """Основной цикл рассылки для конкретного пользователя"""
    settings = await get_settings(user_id)
    if not settings:
        await bot.send_message(user_id, "❌ Настройки не найдены")
        return

    accounts = await get_accounts(user_id)
    if not accounts:
        await bot.send_message(user_id, "❌ Нет добавленных аккаунтов")
        return

    mode = settings["mode"] or "normal"
    interval = settings["interval"] or 60

    if mode == "safe":
        texts = [
            settings["safe_text1"] or "Привет!",
            settings["safe_text2"] or "Здравствуйте!",
            settings["safe_text3"] or "Добрый день!",
        ]
    else:
        texts = [settings["normal_text"] or "Привет!"]

    await bot.send_message(user_id, f"🚀 Рассылка запущена (режим: {mode})")

    total_sent = 0
    for acc_id, session_str, phone in accounts:
        if not running_broadcasts.get(user_id, False):
            await bot.send_message(user_id, "⛔ Остановлено")
            break

        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await bot.send_message(user_id, f"⚠️ {phone} — сессия невалидна")
                await client.disconnect()
                continue

            groups = await get_user_groups(client)
            if not groups:
                await bot.send_message(user_id, f"⚠️ {phone} — нет групп")
                await client.disconnect()
                continue

            await bot.send_message(
                user_id, f"📤 {phone}: найдено {len(groups)} групп"
            )

            flood_count = 0
            idx = 0
            for group_id in groups:
                if not running_broadcasts.get(user_id, False):
                    break
                if flood_count >= 3:
                    await bot.send_message(
                        user_id,
                        f"⚠️ {phone}: 3 FloodWait → пропуск аккаунта"
                    )
                    break

                text = texts[idx % len(texts)]
                idx += 1

                try:
                    await client.send_message(group_id, text)
                    total_sent += 1

                    if mode == "safe":
                        delay = interval * random.uniform(0.8, 1.2)
                    else:
                        delay = interval
                    await asyncio.sleep(delay)

                except errors.FloodWait as e:
                    flood_count += 1
                    await asyncio.sleep(e.value * 1.3)
                except errors.PeerFloodError:
                    await bot.send_message(
                        user_id, f"❌ {phone}: PeerFlood, пропуск"
                    )
                    break
                except Exception as ex:
                    print(f"[ERR] {phone} → {group_id}: {ex}")

            await client.disconnect()
        except Exception as e:
            await bot.send_message(user_id, f"❌ {phone}: {e}")

    running_broadcasts[user_id] = False
    await bot.send_message(user_id, f"✅ Рассылка завершена. Отправлено: {total_sent}")


# ==================== ДИАЛОГ НАСТРОЙКИ РАССЫЛКИ ====================
async def start_broadcast_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await check_access(user_id):
        await update.message.reply_text("❌ Нет активной лицензии")
        return ConversationHandler.END

    accounts = await get_accounts(user_id)
    if not accounts:
        await update.message.reply_text(
            "❌ Сначала добавьте аккаунт: /add_account"
        )
        return ConversationHandler.END

    kb = [
        [InlineKeyboardButton("📤 Обычная (1 текст)", callback_data="mode_normal")],
        [InlineKeyboardButton("🛡 Безопасная (3 текста)", callback_data="mode_safe")],
    ]
    await update.message.reply_text(
        "Выберите режим рассылки:", reply_markup=InlineKeyboardMarkup(kb)
    )
    return ASK_MODE


async def choose_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode = "normal" if query.data == "mode_normal" else "safe"
    context.user_data["mode"] = mode

    if mode == "normal":
        await query.edit_message_text("📝 Введите текст рассылки:")
        return ASK_NORMAL_TEXT
    else:
        await query.edit_message_text("📝 Введите текст №1 из 3:")
        return ASK_SAFE_TEXT_1


async def get_normal_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["normal_text"] = update.message.text
    await update.message.reply_text("⏱ Введите интервал в секундах (например 60):")
    return ASK_INTERVAL


async def get_safe_text1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["safe_text1"] = update.message.text
    await update.message.reply_text("📝 Введите текст №2 из 3:")
    return ASK_SAFE_TEXT_2


async def get_safe_text2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["safe_text2"] = update.message.text
    await update.message.reply_text("📝 Введите текст №3 из 3:")
    return ASK_SAFE_TEXT_3


async def get_safe_text3(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["safe_text3"] = update.message.text
    await update.message.reply_text("⏱ Введите интервал в секундах:")
    return ASK_INTERVAL


async def get_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        interval = int(update.message.text.strip())
        if interval < 5:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Введите число ≥ 5")
        return ASK_INTERVAL

    data = context.user_data
    mode = data.get("mode", "normal")

    await save_settings(
        user_id,
        mode=mode,
        normal_text=data.get("normal_text"),
        safe_text1=data.get("safe_text1"),
        safe_text2=data.get("safe_text2"),
        safe_text3=data.get("safe_text3"),
        interval=interval,
    )

    running_broadcasts[user_id] = True
    asyncio.create_task(run_broadcast(user_id, context.bot))

    await update.message.reply_text(
        f"🚀 Рассылка запущена!\nРежим: {mode}\nИнтервал: {interval}с\n\n"
        f"Остановить: /stop"
    )
    return ConversationHandler.END


# ==================== МЕНЮ ====================
async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await check_access(user_id):
        await update.message.reply_text("❌ Введите код: /activate ВАШ_КЛЮЧ")
        return

    accounts = await get_accounts(user_id)
    acc_list = "\n".join([f"• {p}" for _, _, p in accounts]) or "нет"

    kb = [
        [InlineKeyboardButton("➕ Добавить аккаунт", callback_data="add_account")],
        [InlineKeyboardButton("🚀 Старт рассылки", callback_data="start_bc")],
        [InlineKeyboardButton("📋 Мои аккаунты", callback_data="list_accounts")],
        [InlineKeyboardButton("⛔ Остановить рассылку", callback_data="stop_bc")],
    ]
    await update.message.reply_text(
        f"🔧 Меню\n\nАккаунты:\n{acc_list}",
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "add_account":
        await query.edit_message_text("📱 Введите номер: /add_account")
    elif query.data == "start_bc":
        await query.edit_message_text("🚀 Запуск: /broadcast")
    elif query.data == "list_accounts":
        accounts = await get_accounts(user_id)
        if not accounts:
            await query.edit_message_text("Список пуст")
        else:
            txt = "\n".join([f"ID {i}: {p}" for i, _, p in accounts])
            await query.edit_message_text(f"📋 Аккаунты:\n{txt}")
    elif query.data == "stop_bc":
        running_broadcasts[user_id] = False
        await query.edit_message_text("⛔ Остановка...")


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    running_broadcasts[user_id] = False
    await update.message.reply_text("⛔ Команда остановки отправлена")


# ==================== АДМИН ====================
async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Введите ключ: /activate ВАШ_КЛЮЧ")
        return
    kb = [
        [InlineKeyboardButton("🔑 Создать ключ", callback_data="create_key")],
    ]
    await update.message.reply_text(
        "🔧 Админ-панель", reply_markup=InlineKeyboardMarkup(kb)
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return

    if query.data == "create_key":
        kb = [[InlineKeyboardButton(f"{d} дн.", callback_data=f"key_{d}")]
              for d in [1, 2, 3, 4, 30, 360]]
        kb.append([InlineKeyboardButton("♾ Безлимит", callback_data="key_-1")])
        await query.edit_message_text("Срок:",
                                       reply_markup=InlineKeyboardMarkup(kb))
    elif query.data.startswith("key_"):
        days = int(query.data.split("_")[1])
        key = ''.join(random.choices(string.ascii_uppercase + string.digits, k=16))
        await create_key(key, days)
        label = "бессрочно" if days == -1 else f"{days} дн."
        await query.edit_message_text(f"✅ Ключ ({label}):\n`{key}`",
                                       parse_mode="Markdown")


async def activate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /activate КЛЮЧ")
        return
    user_id = update.effective_user.id
    exp, err = await activate_key(context.args[0], user_id)
    if err:
        await update.message.reply_text(f"❌ {err}")
    elif exp:
        await update.message.reply_text(
            f"✅ Доступ до {exp.strftime('%d.%m.%Y %H:%M')}\n\n/menu"
        )
    else:
        await update.message.reply_text("✅ Бессрочный доступ!\n\n/menu")


# ==================== MAIN ====================
async def main():
    await init_db()
    print("[DB] ok")

    app = Application.builder().token(ADMIN_BOT_TOKEN).build()

    # Диалог добавления аккаунта
    add_acc_conv = ConversationHandler(
        entry_points=[CommandHandler("add_account", start_auth)],
        states={
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
            ASK_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_code)],
            ASK_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_2fa)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
    )

    # Диалог настройки рассылки
    bc_conv = ConversationHandler(
        entry_points=[CommandHandler("broadcast", start_broadcast_setup)],
        states={
            ASK_MODE: [CallbackQueryHandler(choose_mode, pattern="^mode_")],
            ASK_NORMAL_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_normal_text)],
            ASK_SAFE_TEXT_1: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_safe_text1)],
            ASK_SAFE_TEXT_2: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_safe_text2)],
            ASK_SAFE_TEXT_3: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_safe_text3)],
            ASK_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_interval)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
    )

    app.add_handler(add_acc_conv)
    app.add_handler(bc_conv)
    app.add_handler(CommandHandler("start", admin_start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("activate", activate_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^(create_key|key_)"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^(add_account|start_bc|list_accounts|stop_bc)$"))

    print("[BOT] started")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
