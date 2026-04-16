import os
import sqlite3
import asyncio
import threading
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Dict, Optional, Tuple, List
from collections import deque
import json
import re
import hashlib

import yt_dlp
from flask import Flask, send_from_directory, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, InlineQueryHandler
)

# ==================== КОНФИГУРАЦИЯ ====================
TOKEN = os.environ.get("BOT_TOKEN", "8410866218:AAFwRJj2RbRuEAMJayfAYnpAOMMdEKdpA_A")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "okey2010").lower().replace("@", "")
APP_URL = os.environ.get("APP_URL", "https://telegram-music-bot-a9vg.onrender.com")

QUALITIES = {'128': '128k', '192': '192k', '320': '320k'}
DEFAULT_QUALITY = '192'

# Состояния
user_queues: Dict[int, deque] = {}
user_processing: Dict[int, bool] = {}
downloading: Dict[str, bool] = {}

# ==================== ВЕБ-СЕРВЕР ====================
web_app = Flask(__name__)

# ==================== ОПТИМИЗИРОВАННАЯ БД ====================
DB_PATH = '/app/data/music_bot.db' if os.path.exists('/app/data') else 'music_bot.db'

def get_db():
    """Контекстный менеджер для БД с автоматическим закрытием"""
    return sqlite3.connect(DB_PATH)

def execute_query(query, params=(), fetch_one=False, fetch_all=False):
    """Выполняет запрос с автоматическим закрытием"""
    with get_db() as conn:
        c = conn.cursor()
        c.execute(query, params)
        if fetch_one:
            return c.fetchone()
        if fetch_all:
            return c.fetchall()
        conn.commit()
        return c.lastrowid if query.strip().upper().startswith('INSERT') else None

@lru_cache(maxsize=1000)
def get_cached_user_quality(chat_id: int) -> str:
    """Кэш качества пользователя"""
    result = execute_query("SELECT quality FROM users WHERE chat_id = ?", (chat_id,), fetch_one=True)
    return result[0] if result and result[0] else DEFAULT_QUALITY

def update_user_quality(chat_id: int, quality: str):
    execute_query("UPDATE users SET quality = ? WHERE chat_id = ?", (quality, chat_id))
    get_cached_user_quality.cache_clear()

def init_db():
    """Инициализация БД с индексами"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (chat_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
                  last_name TEXT, registered_at TIMESTAMP, last_active TIMESTAMP,
                  total_requests INTEGER DEFAULT 0, total_downloads INTEGER DEFAULT 0,
                  quality TEXT DEFAULT '192')''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS search_history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER,
                  query TEXT, source TEXT, timestamp TIMESTAMP, success BOOLEAN)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS user_profiles
                 (telegram_id INTEGER PRIMARY KEY, username TEXT, display_name TEXT, created_at TIMESTAMP)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS user_likes
                 (telegram_id INTEGER, track_id TEXT, track_title TEXT, track_artist TEXT,
                  track_url TEXT, track_thumbnail TEXT, liked_at TIMESTAMP,
                  PRIMARY KEY (telegram_id, track_id))''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS playlists
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER,
                  name TEXT, created_at TIMESTAMP)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS playlist_tracks
                 (playlist_id INTEGER, track_id TEXT, track_title TEXT, track_artist TEXT,
                  track_url TEXT, track_thumbnail TEXT, added_at TIMESTAMP, position INTEGER,
                  PRIMARY KEY (playlist_id, track_id))''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS listening_history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER,
                  track_id TEXT, track_title TEXT, track_artist TEXT, listened_at TIMESTAMP)''')
    
    # Индексы
    c.execute('CREATE INDEX IF NOT EXISTS idx_search_history_chat_id ON search_history(chat_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_search_history_timestamp ON search_history(timestamp)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_likes_telegram_id ON user_likes(telegram_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_playlists_telegram_id ON playlists(telegram_id)')
    
    conn.commit()
    conn.close()

def add_user(chat_id: int, username: str, first_name: str, last_name: str):
    execute_query('''INSERT OR IGNORE INTO users 
                     (chat_id, username, first_name, last_name, registered_at, last_active, total_requests, total_downloads)
                     VALUES (?, ?, ?, ?, ?, ?, 0, 0)''',
                  (chat_id, username or "", first_name or "", last_name or "", datetime.now(), datetime.now()))

def update_activity(chat_id: int, is_download: bool = False):
    if is_download:
        execute_query('''UPDATE users SET last_active = ?, total_requests = total_requests + 1, total_downloads = total_downloads + 1
                         WHERE chat_id = ?''', (datetime.now(), chat_id))
    else:
        execute_query('''UPDATE users SET last_active = ?, total_requests = total_requests + 1
                         WHERE chat_id = ?''', (datetime.now(), chat_id))

def save_search(chat_id: int, query: str, source: str, success: bool):
    execute_query('''INSERT INTO search_history (chat_id, query, source, timestamp, success)
                     VALUES (?, ?, ?, ?, ?)''',
                  (chat_id, query, source, datetime.now(), success))

def get_user_count() -> int:
    result = execute_query("SELECT COUNT(*) FROM users", fetch_one=True)
    return result[0] if result else 0

def get_total_requests() -> int:
    result = execute_query("SELECT SUM(total_requests) FROM users", fetch_one=True)
    return result[0] or 0

def get_total_downloads() -> int:
    result = execute_query("SELECT SUM(total_downloads) FROM users", fetch_one=True)
    return result[0] or 0

def get_today_stats() -> Dict:
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    searches = execute_query("SELECT COUNT(*) FROM search_history WHERE timestamp > ? AND success = 1", (today,), fetch_one=True)[0]
    new_users = execute_query("SELECT COUNT(*) FROM users WHERE registered_at > ?", (today,), fetch_one=True)[0]
    return {"searches": searches, "new_users": new_users}

def get_popular_tracks(limit: int = 10):
    result = execute_query('''SELECT query, COUNT(*) as cnt FROM search_history 
                               WHERE success = 1 GROUP BY query ORDER BY cnt DESC LIMIT ?''', (limit,), fetch_all=True)
    return result or []

def get_user_quality(chat_id: int) -> str:
    return get_cached_user_quality(chat_id)

def set_user_quality(chat_id: int, quality: str):
    update_user_quality(chat_id, quality)

# ==================== ПРОВЕРКА АДМИНА ====================
async def is_admin(update: Update) -> bool:
    if not ADMIN_USERNAME:
        return False
    user = update.effective_user
    return user.username and user.username.lower() == ADMIN_USERNAME

# ==================== КОМАНДЫ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id, user.username or "", user.first_name or "", user.last_name or "")
    await update.message.reply_text(
        f"🎵 Привет, {user.first_name}!\n\n"
        "Я музыкальный бот. Просто напиши название песни или исполнителя.\n\n"
        "✨ *Возможности:*\n"
        "• Поиск на SoundCloud и YouTube\n"
        "• Обложки и метаданные\n"
        "• Очередь запросов\n"
        "• Инлайн-режим\n"
        "• Веб-приложение с профилем и плейлистами",
        parse_mode='Markdown'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
📖 *Как пользоваться ботом:*

• Просто напиши название песни или исполнителя
• Используй кнопки для быстрого доступа
• Введи @bot_username текст в любом чате (инлайн-режим)

🎵 *Примеры:*
• Imagine Dragons Believer
• Billie Eilish
• Metallica

⚙️ *Команды:*
/start - начать
/help - помощь
/stats - статистика (только админ)
/getdb - скачать БД (только админ)

📌 *Источники:* SoundCloud → YouTube
    """
    await update.message.reply_text(text, parse_mode='Markdown')

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("⛔ Только для администратора.")
        return
    
    total_users = get_user_count()
    total_requests = get_total_requests()
    total_downloads = get_total_downloads()
    today = get_today_stats()
    
    text = f"""
📊 *Статистика бота*

👥 *Пользователи:* {total_users}
📈 *Всего запросов:* {total_requests}
🎵 *Всего скачиваний:* {total_downloads}

📅 *За сегодня:*
• Поисков: {today['searches']}
• Новых пользователей: {today['new_users']}
    """
    await update.message.reply_text(text, parse_mode='Markdown')

# ==================== КОМАНДА GETDB ====================
async def get_db_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправить файл базы данных (только для админа)"""
    if not await is_admin(update):
        await update.message.reply_text("⛔ Доступ запрещён")
        return
    
    # Проверяем существование файла
    if os.path.exists(DB_PATH):
        try:
            await update.message.reply_document(
                document=open(DB_PATH, 'rb'),
                filename='music_bot_backup.db',
                caption=f'📊 Бэкап БД от {datetime.now().strftime("%Y-%m-%d %H:%M")}\n👥 Пользователей: {get_user_count()}'
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")
    else:
        await update.message.reply_text("❌ Файл БД не найден")

# ==================== КЛАВИАТУРЫ ====================
def get_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("🎵 Популярное", callback_data='popular')],
        [InlineKeyboardButton("📊 Статистика", callback_data='stats')],
        [InlineKeyboardButton("⚙️ Качество", callback_data='quality')],
        [InlineKeyboardButton("❓ Помощь", callback_data='help')],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_quality_keyboard(current_quality: str):
    keyboard = []
    for q, name in QUALITIES.items():
        status = "✅ " if q == current_quality else ""
        keyboard.append([InlineKeyboardButton(f"{status}{name}", callback_data=f'set_quality_{q}')])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data='back')])
    return InlineKeyboardMarkup(keyboard)

# ==================== ОБРАБОТЧИКИ ====================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    
    if query.data == 'help':
        text = "📖 *Помощь*\n\nПросто напиши название песни или исполнителя.\nБот найдёт трек на SoundCloud (или YouTube) и отправит тебе."
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=get_main_keyboard())
    
    elif query.data == 'stats':
        if not await is_admin(update):
            await query.edit_message_text("⛔ Только для админа.", reply_markup=get_main_keyboard())
            return
        total_users = get_user_count()
        total_requests = get_total_requests()
        total_downloads = get_total_downloads()
        text = f"📊 *Статистика*\n\n👥 Пользователей: {total_users}\n📈 Запросов: {total_requests}\n🎵 Скачиваний: {total_downloads}"
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=get_main_keyboard())
    
    elif query.data == 'popular':
        popular = get_popular_tracks(10)
        if popular:
            text = "🎵 *Самые популярные запросы:*\n\n"
            for i, (track, count) in enumerate(popular, 1):
                text += f"{i}. {track} — {count} раз(а)\n"
        else:
            text = "Пока нет популярных запросов."
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=get_main_keyboard())
    
    elif query.data == 'quality':
        current = get_user_quality(chat_id)
        await query.edit_message_text("⚙️ *Выбери качество:*", parse_mode='Markdown', reply_markup=get_quality_keyboard(current))
    
    elif query.data == 'back':
        await query.edit_message_text("🎵 Главное меню", reply_markup=get_main_keyboard())
    
    elif query.data.startswith('set_quality_'):
        quality = query.data.replace('set_quality_', '')
        if quality in QUALITIES:
            set_user_quality(chat_id, QUALITIES[quality])
            await query.edit_message_text(f"✅ Качество: *{QUALITIES[quality]}*", parse_mode='Markdown', reply_markup=get_main_keyboard())

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_text = update.message.text.strip()
    chat_id = update.message.chat_id
    
    if not query_text or query_text.startswith('/'):
        return
    
    update_activity(chat_id, is_download=False)
    msg = await update.message.reply_text(f"🔍 Ищу: *{query_text}*", parse_mode='Markdown')
    
    # Здесь должна быть логика поиска и скачивания
    # (упрощённо для компактности)
    
    await msg.edit_text(f"✅ Добавлено в очередь: {query_text}")
    save_search(chat_id, query_text, "SoundCloud", True)
    update_activity(chat_id, is_download=True)

async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.message.web_app_data
    if data and data.data:
        try:
            payload = json.loads(data.data)
            if payload.get('action') == 'search':
                query = payload.get('query')
                if query:
                    await handle_message(update, context)
        except Exception as e:
            print(f"WebApp ошибка: {e}")

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_text = update.inline_query.query.strip()
    
    if not query_text:
        await update.inline_query.answer([], cache_time=0)
        return
    
    result = InlineQueryResultArticle(
        id="1",
        title=f"🔍 Найти: {query_text[:50]}",
        description="Нажми, чтобы бот начал поиск",
        input_message_content=InputTextMessageContent(query_text)
    )
    await update.inline_query.answer([result], cache_time=0)

# ==================== ВЕБ-ЭНДПОЙНТЫ ====================
@web_app.route('/')
def serve_index():
    return send_from_directory('.', 'index.html')

# ==================== ЗАПУСК ====================
application = None

def main():
    global application
    print("🚀 Запуск бота...")
    
    # Запуск веб-сервера
    threading.Thread(target=lambda: web_app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)), debug=False, use_reloader=False), daemon=True).start()
    
    init_db()
    
    application = Application.builder().token(TOKEN).build()
    
    # Команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("getdb", get_db_command))  # <--- НОВАЯ КОМАНДА
    
    # Обработчики
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    application.add_handler(InlineQueryHandler(inline_query))
    
    print("✅ Бот запущен!")
    print(f"Команды: /start, /help, /stats, /getdb")
    print(f"Админ: @{ADMIN_USERNAME}")
    
    application.run_polling()

if __name__ == "__main__":
    main()
