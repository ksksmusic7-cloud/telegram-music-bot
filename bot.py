import os
import sqlite3
import requests
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, List
from collections import deque
import json

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, InlineQueryHandler
)

# ===== НАСТРОЙКИ =====
TOKEN = "8410866218:AAFwRJj2RbRuEAMJayfAYnpAOMMdEKdpA_A"
ADMIN_USERNAME = "okey2010"

# Очередь запросов (храним для каждого пользователя)
user_queues: Dict[int, deque] = {}
user_processing: Dict[int, bool] = {}

# Доступные качества
QUALITIES = {
    '128': '128k',
    '192': '192k', 
    '320': '320k'
}
user_quality: Dict[int, str] = {}  # качество по умолчанию для пользователя
# ====================

# ===== ПРОВЕРКА АДМИНА =====
async def is_admin(update: Update) -> bool:
    if not ADMIN_USERNAME:
        return False
    user = update.effective_user
    return user.username and user.username.lower() == ADMIN_USERNAME

# ===== БАЗА ДАННЫХ =====
def init_db():
    conn = sqlite3.connect('music_bot.db')
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (chat_id INTEGER PRIMARY KEY,
                  username TEXT,
                  first_name TEXT,
                  last_name TEXT,
                  registered_at TIMESTAMP,
                  last_active TIMESTAMP,
                  total_requests INTEGER DEFAULT 0,
                  total_downloads INTEGER DEFAULT 0,
                  quality TEXT DEFAULT '192')''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS search_history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  chat_id INTEGER,
                  query TEXT,
                  source TEXT,
                  timestamp TIMESTAMP,
                  success BOOLEAN)''')
    
    conn.commit()
    conn.close()

def add_user(chat_id: int, username: str, first_name: str, last_name: str):
    conn = sqlite3.connect('music_bot.db')
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO users 
                 (chat_id, username, first_name, last_name, registered_at, last_active, total_requests, total_downloads)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
              (chat_id, username, first_name, last_name, datetime.now(), datetime.now(), 0, 0))
    conn.commit()
    conn.close()

def update_activity(chat_id: int, is_download: bool = False):
    conn = sqlite3.connect('music_bot.db')
    c = conn.cursor()
    if is_download:
        c.execute('''UPDATE users 
                     SET last_active = ?, total_requests = total_requests + 1, total_downloads = total_downloads + 1
                     WHERE chat_id = ?''', (datetime.now(), chat_id))
    else:
        c.execute('''UPDATE users 
                     SET last_active = ?, total_requests = total_requests + 1
                     WHERE chat_id = ?''', (datetime.now(), chat_id))
    conn.commit()
    conn.close()

def save_search(chat_id: int, query: str, source: str, success: bool):
    conn = sqlite3.connect('music_bot.db')
    c = conn.cursor()
    c.execute('''INSERT INTO search_history (chat_id, query, source, timestamp, success)
                 VALUES (?, ?, ?, ?, ?)''',
              (chat_id, query, source, datetime.now(), success))
    conn.commit()
    conn.close()

def get_user_count() -> int:
    conn = sqlite3.connect('music_bot.db')
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM users")
    count = c.fetchone()[0]
    conn.close()
    return count

def get_total_requests() -> int:
    conn = sqlite3.connect('music_bot.db')
    c = conn.cursor()
    c.execute("SELECT SUM(total_requests) FROM users")
    total = c.fetchone()[0] or 0
    conn.close()
    return total

def get_total_downloads() -> int:
    conn = sqlite3.connect('music_bot.db')
    c = conn.cursor()
    c.execute("SELECT SUM(total_downloads) FROM users")
    total = c.fetchone()[0] or 0
    conn.close()
    return total

def get_today_stats() -> Dict:
    conn = sqlite3.connect('music_bot.db')
    c = conn.cursor()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    c.execute("SELECT COUNT(*) FROM search_history WHERE timestamp > ? AND success = 1", (today,))
    searches = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users WHERE registered_at > ?", (today,))
    new_users = c.fetchone()[0]
    conn.close()
    return {"searches": searches, "new_users": new_users}

def get_popular_tracks(limit: int = 5):
    conn = sqlite3.connect('music_bot.db')
    c = conn.cursor()
    c.execute('''SELECT query, COUNT(*) as cnt 
                 FROM search_history 
                 WHERE success = 1 
                 GROUP BY query 
                 ORDER BY cnt DESC 
                 LIMIT ?''', (limit,))
    popular = c.fetchall()
    conn.close()
    return popular

def get_user_quality(chat_id: int) -> str:
    """Получает качество для пользователя"""
    conn = sqlite3.connect('music_bot.db')
    c = conn.cursor()
    c.execute("SELECT quality FROM users WHERE chat_id = ?", (chat_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result and result[0] else '192'

def set_user_quality(chat_id: int, quality: str):
    conn = sqlite3.connect('music_bot.db')
    c = conn.cursor()
    c.execute("UPDATE users SET quality = ? WHERE chat_id = ?", (quality, chat_id))
    conn.commit()
    conn.close()

# ===== ФУНКЦИИ ПОИСКА =====
async def search_youtube(query: str, quality: str = '192') -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Поиск на YouTube (резервный источник)"""
    filename = f"youtube_{abs(hash(query))}"
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': f'{filename}.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'default_search': 'ytsearch',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': quality,
        }],
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=True)
            if info and 'entries' and len(info['entries']) > 0:
                original_file = ydl.prepare_filename(info['entries'][0])
                mp3_file = original_file.replace('.webm', '.mp3').replace('.m4a', '.mp3')
                if os.path.exists(mp3_file):
                    title = info['entries'][0].get('title', query)
                    return mp3_file, title, None
    except Exception as e:
        print(f"YouTube ошибка: {e}")
    return None, None, None

def get_soundcloud_info(url: str) -> Tuple[Optional[str], Optional[str]]:
    """Получает название трека и URL обложки со SoundCloud"""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info:
                title = info.get('title', '')
                thumbnail = info.get('thumbnail', '')
                uploader = info.get('uploader', '')
                duration = info.get('duration', 0)
                return title, thumbnail, uploader, duration
    except Exception as e:
        print(f"Ошибка получения метаданных: {e}")
    return None, None, None, None

def download_thumbnail(url: str, track_name: str) -> Optional[str]:
    """Скачивает обложку и возвращает путь к файлу"""
    if not url:
        return None
    
    try:
        safe_name = "".join(c for c in track_name if c.isalnum() or c in (' ', '-', '_')).rstrip()
        thumb_path = f"thumb_{safe_name[:30]}.jpg"
        
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            with open(thumb_path, 'wb') as f:
                f.write(response.content)
            return thumb_path
    except Exception as e:
        print(f"Ошибка скачивания обложки: {e}")
    return None

async def search_soundcloud(query: str, quality: str = '192') -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Поиск на SoundCloud"""
    filename = f"sc_{abs(hash(query))}"
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': f'{filename}.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'default_search': 'scsearch',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': quality,
        }],
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"scsearch1:{query}", download=True)
            if info and 'entries' and len(info['entries']) > 0:
                track_url = info['entries'][0]['webpage_url']
                real_title, thumbnail_url, uploader, duration = get_soundcloud_info(track_url)
                
                original_file = ydl.prepare_filename(info['entries'][0])
                mp3_file = original_file.replace('.webm', '.mp3').replace('.m4a', '.mp3')
                
                if os.path.exists(mp3_file):
                    thumb_path = None
                    if thumbnail_url:
                        thumb_path = download_thumbnail(thumbnail_url, real_title or query)
                    return mp3_file, real_title or query, thumb_path, uploader
    except Exception as e:
        print(f"SoundCloud ошибка: {e}")
    return None, None, None, None

async def download_audio_with_queue(chat_id: int, query: str):
    """Обработка очереди запросов"""
    # Инициализация очереди для пользователя
    if chat_id not in user_queues:
        user_queues[chat_id] = deque()
        user_processing[chat_id] = False
    
    # Добавляем запрос в очередь
    user_queues[chat_id].append(query)
    
    # Если уже обрабатывается - просто добавляем в очередь
    if user_processing[chat_id]:
        return
    
    # Начинаем обработку очереди
    user_processing[chat_id] = True
    while user_queues[chat_id]:
        next_query = user_queues[chat_id].popleft()
        await process_search(chat_id, next_query)
    user_processing[chat_id] = False

async def process_search(chat_id: int, query: str):
    """Обработка одного поискового запроса"""
    quality = get_user_quality(chat_id)
    
    # Сначала пробуем SoundCloud
    audio_path, real_title, thumb_path, uploader = await search_soundcloud(query, quality)
    source = "SoundCloud"
    
    # Если SoundCloud не нашёл - пробуем YouTube
    if not audio_path:
        audio_path, real_title, _ = await search_youtube(query, quality)
        source = "YouTube"
    
    if audio_path and os.path.exists(audio_path):
        # Отправляем аудио с метаданными
        with open(audio_path, 'rb') as audio_file:
            if thumb_path and os.path.exists(thumb_path):
                with open(thumb_path, 'rb') as thumb_file:
                    await application.bot.send_audio(
                        chat_id=chat_id,
                        audio=audio_file,
                        title=real_title[:60] if real_title else query[:60],
                        performer=uploader if uploader else source,
                        thumbnail=thumb_file
                    )
                os.remove(thumb_path)
            else:
                await application.bot.send_audio(
                    chat_id=chat_id,
                    audio=audio_file,
                    title=real_title[:60] if real_title else query[:60],
                    performer=uploader if uploader else source
                )
        
        os.remove(audio_path)
        save_search(chat_id, query, source, success=True)
        update_activity(chat_id, is_download=True)
    else:
        await application.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Не нашёл: *{query}*\nПопробуй другой запрос.",
            parse_mode='Markdown'
        )
        save_search(chat_id, query, "none", success=False)

# ===== КЛАВИАТУРЫ =====
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

# ===== ОБРАБОТЧИКИ =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(
        chat_id=user.id,
        username=user.username or "",
        first_name=user.first_name or "",
        last_name=user.last_name or ""
    )
    await update.message.reply_text(
        f"🎵 Привет, {user.first_name}!\n\n"
        "Я музыкальный бот. Просто напиши название песни или исполнителя.\n\n"
        "✨ *Возможности:*\n"
        "• Поиск на SoundCloud и YouTube\n"
        "• Обложки и метаданные\n"
        "• Очередь запросов\n"
        "• Инлайн-режим (работает в любом чате)\n"
        "• Выбор качества (128/192/320 kbps)",
        reply_markup=get_main_keyboard(),
        parse_mode='Markdown'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
📖 *Как пользоваться ботом:*

• Просто напиши название песни или исполнителя
• Используй кнопки для быстрого доступа
• Введи @bot_username текст в любом чате (инлайн-режим)

🎵 *Примеры запросов:*
• Imagine Dragons Believer
• Billie Eilish
• Metallica

✨ *Возможности:*
• Поиск на SoundCloud → если не нашёл → YouTube
• Очередь запросов (отправляй несколько подряд)
• Обложки и полные метаданные
• Выбор качества через кнопку "Качество"

⚙️ *Команды:*
/start - начать
/help - помощь
/stats - статистика (только для админа)

📌 *Источники:* SoundCloud → YouTube
    """
    await update.message.reply_text(text, parse_mode='Markdown')

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("⛔ Эта команда только для администратора.")
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

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    
    if query.data == 'help':
        text = """
📖 *Помощь*

Просто напиши название песни или исполнителя.
Бот найдёт трек на SoundCloud (или YouTube) и отправит тебе.

*Примеры:*
• Imagine Dragons Believer
• h.. znaet #сднёмрожденияника
• анна асти царица
        """
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=get_main_keyboard())
    
    elif query.data == 'stats':
        if not await is_admin(update):
            await query.edit_message_text("⛔ Только для админа.", reply_markup=get_main_keyboard())
            return
        
        total_users = get_user_count()
        total_requests = get_total_requests()
        total_downloads = get_total_downloads()
        today = get_today_stats()
        
        text = f"""
📊 *Статистика*

👥 Пользователей: {total_users}
📈 Запросов всего: {total_requests}
🎵 Скачиваний: {total_downloads}

📅 За сегодня:
• Поисков: {today['searches']}
• Новых: {today['new_users']}
        """
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
        await query.edit_message_text(
            "⚙️ *Выбери качество аудио:*\n(чем выше качество, тем больше размер файла)",
            parse_mode='Markdown',
            reply_markup=get_quality_keyboard(current)
        )
    
    elif query.data == 'back':
        await query.edit_message_text(
            "🎵 Главное меню",
            reply_markup=get_main_keyboard()
        )
    
    elif query.data.startswith('set_quality_'):
        quality = query.data.replace('set_quality_', '')
        if quality in QUALITIES:
            set_user_quality(chat_id, QUALITIES[quality])
            await query.edit_message_text(
                f"✅ Качество установлено: *{QUALITIES[quality]}*",
                parse_mode='Markdown',
                reply_markup=get_main_keyboard()
            )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_text = update.message.text.strip()
    chat_id = update.message.chat_id
    
    if not query_text:
        return
    
    update_activity(chat_id, is_download=False)
    
    # Проверяем, не команда ли это
    if query_text.startswith('/'):
        return
    
    msg = await update.message.reply_text(f"🔍 Ищу: *{query_text}*\n⏳ Добавлено в очередь...", parse_mode='Markdown')
    
    # Добавляем в очередь
    await download_audio_with_queue(chat_id, query_text)
    await msg.delete()

# ===== ИНЛАЙН-РЕЖИМ =====
async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_text = update.inline_query.query.strip()
    
    if not query_text:
        results = []
        await update.inline_query.answer(results, cache_time=0)
        return
    
    # Поиск треков (быстрый, без скачивания)
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
        'default_search': 'scsearch',
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"scsearch5:{query_text}", download=False)
            results = []
            
            if info and 'entries' in info:
                for i, entry in enumerate(info['entries'][:5]):
                    if entry:
                        title = entry.get('title', 'Без названия')
                        uploader = entry.get('uploader', 'SoundCloud')
                        duration = entry.get('duration', 0)
                        url = entry.get('webpage_url', '')
                        
                        # Форматируем длительность
                        duration_str = f"{duration // 60}:{duration % 60:02d}" if duration else "?"
                        
                        result = InlineQueryResultArticle(
                            id=str(i),
                            title=title[:60],
                            description=f"{uploader} • {duration_str}",
                            input_message_content=InputTextMessageContent(f"!play {url}"),
                            thumbnail_url=entry.get('thumbnail', '')
                        )
                        results.append(result)
            
            await update.inline_query.answer(results, cache_time=30)
    except Exception as e:
        print(f"Инлайн-ошибка: {e}")
        await update.inline_query.answer([], cache_time=0)

# ===== ЗАПУСК =====
application = None

def main():
    global application
    print("🚀 Запуск бота...")
    print(f"Токен: {TOKEN[:10]}... (скрыто)")
    
    init_db()
    
    # Без прокси
    application = Application.builder().token(TOKEN).build()
    
    # Команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))
    
    # Кнопки
    application.add_handler(CallbackQueryHandler(button_callback))
    
    # Сообщения
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Инлайн-режим
    application.add_handler(InlineQueryHandler(inline_query))
    
    print("✅ Бот запущен!")
    print("✨ Функции: SoundCloud + YouTube | Очередь | Инлайн-режим | Метаданные | Выбор качества")
    print(f"Админ: @{ADMIN_USERNAME}")
    
    application.run_polling()

if __name__ == "__main__":
    main()
