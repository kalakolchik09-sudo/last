import os
import asyncio
import random
import string
import datetime
import aiosqlite
from io import BytesIO
from dotenv import load_dotenv
import qrcode
from telethon import TelegramClient, errors, functions
from telethon.sessions import StringSession
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
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
    ASK_PHONE_QR, ASK_2FA_QR,
    ASK_MODE,
    ASK_NORMAL_TEXT,
    ASK_SAFE_TEXT_1, ASK_SAFE_TEXT_2, ASK_SAFE_TEXT_3,
    ASK_INTERVAL,
) = range(8)

pending_auth = {}          # user_id -> dict(client, qr_login)
running_broadcasts = {}    # user_id -> bool


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
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM user_settings WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


# ==================== QR-АВТОРИЗАЦИЯ ====================
async def start_qr_auth(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало QR-авторизации"""
    user_id = update.effective_user.id
    if not await check_access(user_id):
        await update.message.reply_text("❌ Нет активной лицензии")
        return ConversationHandler.END

    # Создаём клиент
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()

    # Запускаем QR-логин
    qr_login = await client.qr_login()

    # Генерируем QR-картинку из URL
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(qr_login.url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)

    # Отправляем фото с инструкцией
    caption = (
        "📱 **QR-авторизация**\n\n"
        "1. Откройте Telegram на телефоне (где вы уже авторизованы)\n"
        "2. Перейдите: **Настройки → Устройства → Подключить устройство**\n"
        "3. Отсканируйте QR-код выше\n"
        "4. Подтвердите вход\n\n"
        "⏳ QR-код действует 2 минуты.\n"
        "Если истёк — напишите /add_account заново."
    )
    await update.message.reply_photo(
        photo=InputFile(buf, filename="qr.png"),
        caption=caption,
        parse_mode="Markdown"
    )

    pending_auth[user_id] = {
        "client": client,
        "qr_login": qr_login,
        "started_at": datetime.datetime.now()
    }

    return ASK_PHONE_QR


async def wait_qr_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ждём сканирования QR (в фоне) — здесь ловим только сообщения пользователя"""
    # Этот хэндлер не используется, так как ожидание идёт в отдельной задаче
    return ASK_PHONE_QR


async def qr_wait_task(user_id: int, bot):
    """Фоновая задача: ждёт сканирования QR и обрабатывает результат"""
    auth = pending_auth.get(user_id)
    if not auth:
        return

    client = auth["client"]
    qr_login = auth["qr_login"]

    try:
        # Ждём сканирования (максимум 120 секунд)
        result = await qr_login.wait(timeout=120)

        # Проверяем, не нужен ли 2FA
        if isinstance(result, bool) and result:
            # Успешный вход
            session_str = client.session.save()
            me = await client.get_me()
            phone = me.phone or "unknown"

            await save_account(user_id, session_str, phone)
            await client.disconnect()
            pending_auth.pop(user_id, None)

            await bot.send_message(
                user_id,
                f"✅ Аккаунт `{phone}` успешно добавлен!\n\n"
                f"Используйте /menu для дальнейших действий.",
                parse_mode="Markdown"
            )
        else:
            # QR истёк, нужно обновить
            await bot.send_message(
                user_id,
                "⏰ QR-код истёк. Напишите /add_account, чтобы получить новый."
            )
            await client.disconnect()
            pending_auth.pop(user_id, None)

    except errors.SessionPasswordNeededError:
        # Нужен 2FA пароль
        await bot.send_message(
            user_id,
            "🔐 На аккаунте включена двухфакторная аутентификация.\n"
            "Введите пароль (cloud password) от Telegram:"
        )
        # Оставляем клиент в pending_auth, переключаемся на состояние 2FA
        # Но нам нужно как-то переключить состояние диалога...
        # Проще: попросим пользователя написать пароль, а обработаем в отдельном хэндлере
        auth["need_2fa"] = True
        return

    except asyncio.TimeoutError:
        await bot.send_message(
            user_id,
            "⏰ Время ожидания истекло. Напишите /add_account для новой попытки."
        )
        await client.disconnect()
        pending_auth.pop(user_id, None)

    except Exception as e:
        await bot.send_message(user_id, f"❌ Ошибка: {e}")
        await client.disconnect()
        pending_auth.pop(user_id, None)


async def qr_2fa_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ввода 2FA пароля после QR"""
    user_id = update.effective_user.id
    auth = pending_auth.get(user_id)

    if not auth or not auth.get("need_2fa"):
        return

    password = update.message.text.strip()
    client = auth["client"]

    try:
        await client.sign_in(password=password)
        session_str = client.session.save()
        me = await client.get_me()
        phone = me.phone or "unknown"

        await save_account(user_id, session_str, phone)
        await client.disconnect()
        pending_auth.pop(user_id, None)

        await update.message.reply_text(
            f"✅ Аккаунт `{phone}` добавлен!\n\n/menu",
            parse_mode="Markdown"
        )
    except errors.PasswordHashInvalidError:
        await update.message.reply_text("❌ Неверный пароль. Попробуйте ещё раз:")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
        await client.disconnect()
        pending_auth.pop(user_id, None)


# ==================== РАССЫЛКА ====================
async def get_user_groups(client: TelegramClient):
    groups = []
    async for dialog in client.iter_dialogs():
        if dialog.is_group:
            groups.append(dialog.id)
        elif dialog.is_channel and not dialog.entity.broadcast:
            groups.append(dialog.id)
    return groups


async def run_broadcast(user_id: int, bot):
    settings = await get_settings(user_id)
    if not settings:
        await bot.send_message(user_id, "❌ Настройки не найдены")
        return

    accounts = await get_accounts(user_id)
    if not accounts:
        await bot.send_message(user_id, "❌ Нет добавленных аккаунтов")
        return

    mode = settings.get("mode") or "normal"
    interval = settings.get("interval") or 60

    if mode == "safe":
        texts = [
            settings.get("safe_text1") or "Привет!",
            settings.get("safe_text2") or "Здравствуйте!",
            settings.get("safe_text3") or "Добрый день!",
        ]
    else:
        texts = [settings.get("normal_text") or "Привет!"]

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

            await bot.send_message(user_id, f"📤 {phone}: найдено {len(groups)} групп")

            flood_count = 0
            idx = 0
            for group_id in groups:
                if not running_broadcasts.get(user_id, False):
                    break
                if flood_count >= 3:
                    await bot.send_message(user_id, f"⚠️ {phone}: 3 FloodWait → пропуск")
                    break

                text = texts[idx % len(texts)]
                idx += 1

                try:
                    await client.send_message(group_id, text)
                    total_sent += 1
                    delay = interval * random.uniform(0.8, 1.2) if mode == "safe" else interval
                    await asyncio.sleep(delay)
                except errors.FloodWait as e:
                    flood_count += 1
                    await asyncio.sleep(e.value * 1.3)
                except errors.PeerFloodError:
                    await bot.send_message(user_id, f"❌ {phone}: PeerFlood, пропуск")
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
        await update.message.reply_text("❌ Сначала добавьте аккаунт: /add_account")
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
        f"🚀 Рассылка запущена!\nРежим: {mode}\nИнтервал: {interval}с\n\nОстановить: /stop"
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
        await query.edit_message_text("📱 Отправьте /add_account для QR-авторизации")
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
    kb = [[InlineKeyboardButton("🔑 Создать ключ", callback_data="create_key")]]
    await update.message.reply_text("🔧 Админ-панель", reply_markup=InlineKeyboardMarkup(kb))


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return

    if query.data == "create_key":
        kb = [[InlineKeyboardButton(f"{d} дн.", callback_data=f"key_{d}")]
              for d in [1, 2, 3, 4, 30, 360]]
        kb.append([InlineKeyboardButton("♾ Безлимит", callback_data="key_-1")])
        await query.edit_message_text("Срок:", reply_markup=InlineKeyboardMarkup(kb))
    elif query.data.startswith("key_"):
        days = int(query.data.split("_")[1])
        key = ''.join(random.choices(string.ascii_uppercase + string.digits, k=16))
        await create_key(key, days)
        label = "бессрочно" if days == -1 else f"{days} дн."
        await query.edit_message_text(f"✅ Ключ ({label}):\n`{key}`", parse_mode="Markdown")


async def activate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /activate КЛЮЧ")
        return
    user_id = update.effective_user.id
    exp, err = await activate_key(context.args[0], user_id)
    if err:
        await update.message.reply_text(f"❌ {err}")
    elif exp:
        await update.message.reply_text(f"✅ Доступ до {exp.strftime('%d.%m.%Y %H:%M')}\n\n/menu")
    else:
        await update.message.reply_text("✅ Бессрочный доступ!\n\n/menu")


# ==================== MAIN ====================
async def main():
    await init_db()
    print("[DB] ok")

    app = Application.builder().token(ADMIN_BOT_TOKEN).build()

    # Диалог QR-авторизации
    qr_conv = ConversationHandler(
        entry_points=[CommandHandler("add_account", start_qr_auth)],
        states={
            ASK_PHONE_QR: [MessageHandler(filters.TEXT & ~filters.COMMAND, wait_qr_scan)],
            ASK_2FA_QR: [MessageHandler(filters.TEXT & ~filters.COMMAND, qr_2fa_handler)],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
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
        fallbacks=[CommandHandler("cancel", lambda u, c: ConversationHandler.END)],
        per_user=True,
    )

    app.add_handler(qr_conv)
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
