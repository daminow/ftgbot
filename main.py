#!/usr/bin/env python3
import re
import json
import logging
import os
import psycopg2
import threading
import asyncio
from datetime import datetime, timedelta
from urllib.parse import urlparse
from dotenv import load_dotenv

from telegram import (
    Update,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ChatMemberHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ========= Загрузка .env =========
load_dotenv()

# ========= Конфигурация =========
TOKEN            = os.getenv("BOT_TOKEN")
FRONTEND_CHAT_ID = int(os.getenv("FRONTEND_CHAT_ID"))
ADMIN_GROUP_ID   = int(os.getenv("ADMIN_GROUP_ID"))
BOT_THREAD_ID    = int(os.getenv("BOT_THREAD_ID"))
LOGS_THREAD_ID   = int(os.getenv("LOGS_THREAD_ID"))
DATABASE_URL     = os.getenv("DATABASE_URL")
SCHEMA           = os.getenv("SCHEMA")
# ========= Логирование =========
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========= Инициализация БД =========
def init_db_postgres():
    """Создаёт схему и таблицы: users, allowed_links, allowed_file_extensions."""
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA} AUTHORIZATION frontendtgbot_db_admin;")
                cur.execute(f'''
                    CREATE TABLE IF NOT EXISTS {SCHEMA}.users (
                        user_id BIGINT PRIMARY KEY,
                        join_date TIMESTAMP,
                        alias TEXT,
                        warns INTEGER DEFAULT 0,
                        bans INTEGER DEFAULT 0,
                        history JSONB
                    );
                ''')
                cur.execute(f'''
                    CREATE TABLE IF NOT EXISTS {SCHEMA}.allowed_links (
                        url TEXT PRIMARY KEY
                    );
                ''')
                cur.execute(f'''
                    CREATE TABLE IF NOT EXISTS {SCHEMA}.allowed_file_extensions (
                        ext TEXT PRIMARY KEY
                    );
                ''')
                conn.commit()
        logger.info("Схема и таблицы успешно созданы или уже существуют.")
    except Exception as e:
        logger.error("Ошибка инициализации БД: %s", e)

def load_allowed_links():
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT url FROM {SCHEMA}.allowed_links;")
                return [row[0] for row in cur.fetchall()]
    except Exception as e:
        logger.error("Ошибка загрузки allowed_links: %s", e)
        return []

def load_allowed_extensions():
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT ext FROM {SCHEMA}.allowed_file_extensions;")
                return [row[0] for row in cur.fetchall()]
    except Exception as e:
        logger.error("Ошибка загрузки allowed_file_extensions: %s", e)
        return []

# Загружаем списки в память
ALLOWED_LINKS = load_allowed_links()
ALLOWED_EXTS  = load_allowed_extensions()

# ========= Работа с пользователями =========
def get_user(user_id: int):
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT user_id, join_date, alias, warns, bans, history "
                    f"FROM {SCHEMA}.users WHERE user_id = %s;",
                    (user_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "user_id": row[0],
                    "join_date": row[1],
                    "alias": row[2],
                    "warns": row[3],
                    "bans": row[4],
                    "history": row[5] or [],
                }
    except Exception as e:
        logger.error("Ошибка get_user: %s", e)
        return None

def create_user(user_id: int, alias: str):
    now = datetime.now()
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f'''
                    INSERT INTO {SCHEMA}.users
                      (user_id, join_date, alias, warns, bans, history)
                    VALUES (%s, %s, %s, 0, 0, %s)
                    ON CONFLICT (user_id) DO NOTHING;
                    ''',
                    (user_id, now, alias, json.dumps([])),
                )
                conn.commit()
    except Exception as e:
        logger.error("Ошибка create_user: %s", e)

def update_user_record(user: dict):
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f'''
                    UPDATE {SCHEMA}.users
                    SET alias = %s, warns = %s, bans = %s, history = %s
                    WHERE user_id = %s;
                    ''',
                    (
                        user["alias"],
                        user["warns"],
                        user["bans"],
                        json.dumps(user["history"]),
                        user["user_id"],
                    ),
                )
                conn.commit()
    except Exception as e:
        logger.error("Ошибка update_user_record: %s", e)

def add_punishment(user_id: int, alias: str, p_type: str, reason: str, duration: int, issued_by: str):
    now = datetime.now()
    entry = {
        "id": int(now.timestamp()),
        "date": now.strftime("%Y-%m-%d %H:%M:%S"),
        "type": p_type,
        "reason": reason,
        "duration": duration,
        "issued_by": issued_by,
    }
    user = get_user(user_id)
    if not user:
        create_user(user_id, alias)
        user = get_user(user_id)
    if p_type == "warn":
        user["warns"] += 1
    elif p_type == "ban":
        user["bans"] += 1
    user["history"].append(entry)
    update_user_record(user)
    return user

# ========= Авторазбан =========
async def schedule_unban(bot, user_id: int, hours: int):
    await asyncio.sleep(hours * 3600)
    try:
        await bot.unban_chat_member(
            chat_id=FRONTEND_CHAT_ID,
            user_id=user_id,
            only_if_banned=True
        )
        logger.info(f"Авторазбан пользователя {user_id} спустя {hours}ч.")
    except Exception as e:
        logger.error("Ошибка авторазбана: %s", e)

# ========= Вспомогательные функции =========
def extract_domains(text: str):
    urls = re.findall(r'https?://\S+', text)
    domains = []
    for u in urls:
        host = urlparse(u).hostname or ""
        domains.append(host.replace("www.", ""))
    return domains

# ========= Обработка нарушений =========
async def process_violation(update: Update, context: ContextTypes.DEFAULT_TYPE, reason: str, detail: str = ""):
    msg = update.effective_message
    user = msg.from_user
    alias = f"@{user.username}" if user.username else user.first_name

    # Удаляем нарушающее сообщение
    try:
        await msg.delete()
    except:
        pass

    # Логируем в админской теме логов
    log_text = (
        f"ID: {user.id} Alias: {alias} Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Message: {msg.text or detail}"
    )
    await context.bot.send_message(
        chat_id=ADMIN_GROUP_ID,
        message_thread_id=LOGS_THREAD_ID,
        text=log_text
    )

    # Накладываем санкции
    record = get_user(user.id) or {"warns": 0}
    if record["warns"] == 0:
        # Первое нарушение — предупреждение
        await context.bot.send_message(
            chat_id=msg.chat.id,
            text=(
                f"{alias}, {reason}\n"
                f"Разрешённые ссылки: {', '.join(ALLOWED_LINKS) or 'нет'}\n"
                f"Разрешённые расширения: {', '.join(ALLOWED_EXTS) or 'нет'}"
            ),
            parse_mode=ParseMode.HTML
        )
        add_punishment(user.id, alias, "warn", reason, 0, "bot")
    else:
        # Повтор — мут на 1 час
        until = datetime.now() + timedelta(hours=1)
        await context.bot.restrict_chat_member(
            chat_id=msg.chat.id,
            user_id=user.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until
        )
        add_punishment(user.id, alias, "mute", reason, 60, "bot")
        await context.bot.send_message(
            chat_id=msg.chat.id,
            text=f"{alias}, вы получили мут на 1 час за повторное нарушение."
        )

# ========= Хэндлеры сообщений =========
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text or ""
    for dom in extract_domains(text):
        if dom and dom not in ALLOWED_LINKS:
            await process_violation(update, context, "Эта ссылка запрещена.", text)
            return

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.effective_message.document
    ext = os.path.splitext(doc.file_name)[1].lstrip(".").lower()
    if ext not in ALLOWED_EXTS:
        await process_violation(update, context, "Этот тип файла запрещён.", doc.file_name)

# ========= Проверка прав админа =========
async def is_admin_in_frontend(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        admins = await context.bot.get_chat_administrators(FRONTEND_CHAT_ID)
        return user_id in {a.user.id for a in admins}
    except:
        return False

async def is_valid_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    msg = update.effective_message
    if msg.chat.id != ADMIN_GROUP_ID or msg.message_thread_id != BOT_THREAD_ID:
        return False
    return await is_admin_in_frontend(msg.from_user.id, context)

async def delete_command_message(update: Update):
    try:
        await update.effective_message.delete()
    except:
        pass

# ========= Админские команды =========
# /ban user_id reason hours
async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_valid_admin_command(update, context):
        return
    args = context.args
    if len(args) < 3:
        await update.effective_message.reply_text("Использование: /ban user_id причина срок(в часах)")
        await delete_command_message(update)
        return
    try:
        target_id = int(args[0])
        duration = int(args[-1])
        reason = " ".join(args[1:-1])
    except:
        await update.effective_message.reply_text("Неверные аргументы.")
        await delete_command_message(update)
        return
    await context.bot.ban_chat_member(chat_id=FRONTEND_CHAT_ID, user_id=target_id)
    record = add_punishment(target_id, str(target_id), "ban", reason, duration, update.effective_message.from_user.first_name)
    await update.effective_message.reply_text(
        f"Пользователь {target_id} забанен на {duration}ч. Всего банов: {record['bans']}."
    )
    log = f"БАН: {target_id} на {duration}ч, причина: {reason}"
    await context.bot.send_message(
        chat_id=ADMIN_GROUP_ID,
        message_thread_id=LOGS_THREAD_ID,
        text=log
    )
    asyncio.create_task(schedule_unban(context.bot, target_id, duration))
    await delete_command_message(update)

# /warn user_id reason days
async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_valid_admin_command(update, context):
        return
    args = context.args
    if len(args) < 3:
        await update.effective_message.reply_text("Использование: /warn user_id причина срок(в днях)")
        await delete_command_message(update)
        return
    try:
        target_id = int(args[0])
        duration = int(args[-1])
        reason = " ".join(args[1:-1])
    except:
        await update.effective_message.reply_text("Неверные аргументы.")
        await delete_command_message(update)
        return
    record = add_punishment(target_id, str(target_id), "warn", reason, duration, update.effective_message.from_user.first_name)
    await update.effective_message.reply_text(
        f"Предупреждение выдано {target_id}. Всего: {record['warns']}."
    )
    log = f"ПРЕДУПРЕЖДЕНИЕ: {target_id}, причина: {reason}"
    await context.bot.send_message(
        chat_id=ADMIN_GROUP_ID,
        message_thread_id=LOGS_THREAD_ID,
        text=log
    )
    await delete_command_message(update)

# /unwarn user_id
async def unwarn_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_valid_admin_command(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Использование: /unwarn user_id")
        await delete_command_message(update)
        return
    try:
        target_id = int(context.args[0])
    except:
        await update.effective_message.reply_text("Неверный user_id.")
        await delete_command_message(update)
        return
    record = get_user(target_id)
    if not record or record["warns"] == 0:
        await update.effective_message.reply_text("Нет предупреждений для снятия.")
    else:
        record["warns"] -= 1
        for i in range(len(record["history"]) - 1, -1, -1):
            if record["history"][i].get("type") == "warn":
                record["history"].pop(i)
                break
        update_user_record(record)
        await update.effective_message.reply_text(f"Снято предупреждение. Осталось: {record['warns']}.")
        log = f"UNWARN: {target_id}, осталось предупреждений: {record['warns']}"
        await context.bot.send_message(
            chat_id=ADMIN_GROUP_ID,
            message_thread_id=LOGS_THREAD_ID,
            text=log
        )
    await delete_command_message(update)

# /mute user_id reason minutes
async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_valid_admin_command(update, context):
        return
    args = context.args
    if len(args) < 3:
        await update.effective_message.reply_text("Использование: /mute user_id причина срок(в минутах)")
        await delete_command_message(update)
        return
    try:
        target_id = int(args[0])
        duration = int(args[-1])
        reason = " ".join(args[1:-1])
    except:
        await update.effective_message.reply_text("Неверные аргументы.")
        await delete_command_message(update)
        return
    until = datetime.now() + timedelta(minutes=duration)
    await context.bot.restrict_chat_member(
        chat_id=FRONTEND_CHAT_ID,
        user_id=target_id,
        permissions=ChatPermissions(can_send_messages=False),
        until_date=until
    )
    add_punishment(target_id, str(target_id), "mute", reason, duration, update.effective_message.from_user.first_name)
    await update.effective_message.reply_text(f"Пользователь {target_id} замьючен на {duration} мин.")
    log = f"MUTE: {target_id}, на {duration} мин, причина: {reason}"
    await context.bot.send_message(
        chat_id=ADMIN_GROUP_ID,
        message_thread_id=LOGS_THREAD_ID,
        text=log
    )
    await delete_command_message(update)

# /unmute user_id
async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_valid_admin_command(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Использование: /unmute user_id")
        await delete_command_message(update)
        return
    try:
        target_id = int(context.args[0])
    except:
        await update.effective_message.reply_text("Неверный user_id.")
        await delete_command_message(update)
        return
    await context.bot.restrict_chat_member(
        chat_id=FRONTEND_CHAT_ID,
        user_id=target_id,
        permissions=ChatPermissions(can_send_messages=True),
    )
    await update.effective_message.reply_text(f"Ограничения сняты с {target_id}.")
    log = f"UNMUTE: {target_id}."
    await context.bot.send_message(
        chat_id=ADMIN_GROUP_ID,
        message_thread_id=LOGS_THREAD_ID,
        text=log
    )
    await delete_command_message(update)

# ========= Приветствия и защита =========
async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    for m in update.effective_message.new_chat_members:
        await context.bot.send_message(
            chat_id=update.effective_message.chat.id,
            text=f"Добро пожаловать, {m.first_name}! Ознакомьтесь с правилами чата."
        )

async def prevent_group_addition(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mu = update.my_chat_member
    if mu and mu.chat.type != ChatType.PRIVATE and mu.new_chat_member.status in ["member", "administrator"]:
        await context.bot.send_message(mu.chat.id, "Извините, я не предназначен для работы в группах.")
        await context.bot.leave_chat(mu.chat.id)

# ========= /links Conversation =========
ADD_LINK = 1

async def links_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg.chat.id != ADMIN_GROUP_ID or msg.message_thread_id != BOT_THREAD_ID:
        return
    keyboard = [[InlineKeyboardButton("➕ Добавить ссылку", callback_data="add_link")]]
    for url in ALLOWED_LINKS:
        keyboard.append([InlineKeyboardButton(url, callback_data=f"del|{url}")])
    await msg.reply_text("Разрешённые ссылки:", reply_markup=InlineKeyboardMarkup(keyboard))

async def links_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    if data == "add_link":
        await q.message.reply_text("Введите URL для добавления:")
        return ADD_LINK
    if data.startswith("del|"):
        url = data.split("|", 1)[1]
        kb = [
            [InlineKeyboardButton("Удалить", callback_data=f"confirm|{url}")],
            [InlineKeyboardButton("Назад", callback_data="back")]
        ]
        await q.message.edit_text(f"Удалить ссылку {url}?", reply_markup=InlineKeyboardMarkup(kb))
    elif data.startswith("confirm|"):
        url = data.split("|", 1)[1]
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(f"DELETE FROM {SCHEMA}.allowed_links WHERE url = %s;", (url,))
                conn.commit()
        global ALLOWED_LINKS
        ALLOWED_LINKS = load_allowed_links()
        await q.message.edit_text("Ссылка удалена.")
    elif data == "back":
        await links_command(update, context)
    return ConversationHandler.END

async def add_link_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.effective_message.text.strip()
    parsed = urlparse(url)
    domain = (parsed.hostname or "").replace("www.", "")
    if not domain:
        await update.effective_message.reply_text("Неверный URL.")
    else:
        with psycopg2.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {SCHEMA}.allowed_links(url) VALUES(%s) ON CONFLICT DO NOTHING;",
                    (domain,)
                )
                conn.commit()
        global ALLOWED_LINKS
        ALLOWED_LINKS = load_allowed_links()
        await update.effective_message.reply_text(f"Добавлена ссылка: {domain}")
    return ConversationHandler.END

# ========= /help =========
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📚 Правила чата:\n"
        "⚠️ 1. Условия действия правил...\n"
        "✅ 2. Разрешено: ...\n"
        "⛔ 3. Запрещено: ...\n\n"
        f"🌐 Разрешённые ссылки: {', '.join(ALLOWED_LINKS) or 'нет'}\n"
        f"📂 Разрешённые расширения файлов: {', '.join(ALLOWED_EXTS) or 'нет'}\n\n"
        "/links — управление списком ссылок (тема команд)\n"
        "/help — показать это сообщение"
    )
    await update.effective_message.reply_text(text)

# ========= Запуск бота =========
if __name__ == "__main__":
    init_db_postgres()

    application = ApplicationBuilder().token(TOKEN).build()

    # Защита от добавления в группы
    application.add_handler(
        ChatMemberHandler(prevent_group_addition, ChatMemberHandler.MY_CHAT_MEMBER)
    )

    # Приветствие новых участников
    application.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member)
    )

    # Обработка ссылок и документов
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    application.add_handler(
        MessageHandler(filters.Document.ALL, handle_document)
    )

    # Админские команды
    application.add_handler(
        CommandHandler("ban", ban_command, filters=filters.ChatType.GROUP)
    )
    application.add_handler(
        CommandHandler("warn", warn_command, filters=filters.ChatType.GROUP)
    )
    application.add_handler(
        CommandHandler("unwarn", unwarn_command, filters=filters.ChatType.GROUP)
    )
    application.add_handler(
        CommandHandler("mute", mute_command, filters=filters.ChatType.GROUP)
    )
    application.add_handler(
        CommandHandler("unmute", unmute_command, filters=filters.ChatType.GROUP)
    )
    application.add_handler(CommandHandler("help", help_command))

    # /links ConversationHandler
    conv = ConversationHandler(
        entry_points=[CommandHandler("links", links_command)],
        states={ADD_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_link_receive)]},
        fallbacks=[],
        allow_reentry=True
    )
    application.add_handler(conv)
    application.add_handler(CallbackQueryHandler(links_callback))

    # On startup notifications
    async def on_startup(app):
        await app.bot.send_message(FRONTEND_CHAT_ID, "Бот подключен в основной чат.")
        await app.bot.send_message(
            ADMIN_GROUP_ID,
            "Бот подключен в тему команд.",
            message_thread_id=BOT_THREAD_ID
        )
        await app.bot.send_message(
            ADMIN_GROUP_ID,
            "Бот подключен в тему логов.",
            message_thread_id=LOGS_THREAD_ID
        )

    application.run_polling(on_startup=on_startup)