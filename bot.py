import os
import sqlite3
import requests
import asyncio
import threading
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, List
from collections import deque
import json

import yt_dlp
from flask import Flask, send_from_directory, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, InlineQueryHandler
)

# ===== НАСТРОЙКИ =====
TOKEN = "8410866218:AAFwRJj2RbRuEAMJayfAYnpAOMMdEKdpA_A"
ADMIN_USERNAME = "okey2010"

# Очередь запросов
user_queues: Dict[int, deque] = {}
user_processing: Dict[int, bool] = {}

# Качество
QUALITIES = {
    '128': '128k',
    '192': '192k', 
    '320': '320k'
}

# Веб-сервер для Mini App
web_app = Flask(__name__)

# ===== API ДЛЯ MINI APP =====
@web_app.route('/')
def serve_index():
    return send_from_directory('.', 'index.html')

@web_app.route('/search')
def search():
    query = request.args.get('q', '')
    if not query:
        return jsonify([])
    
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
        'default_search': 'scsearch',
        'playlistend': 20,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"scsearch20:{query}", download=False)
            results = []
            if info and 'entries' in info:
                for entry in info['entries'][:20]:
                    if entry:
                        results.append({
                            'id': entry.get('id', ''),
                            'title': entry.get('title', 'Без названия')[:80],
                            'artist': entry.get('uploader', 'SoundCloud'),
                            'duration': entry.get('duration', 0),
                            'url': entry.get('webpage_url', ''),
                            'thumbnail': entry.get('thumbnail', '')
                        })
            return jsonify(results)
    except Exception as e:
        print(f"API ошибка: {e}")
        return jsonify([])

@web_app.route('/download')
def download():
    url = request.args.get('url', '')
    if not url:
        return jsonify({'error': 'No URL'})
    
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'format': 'bestaudio/best',
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info:
                for f in info.get('formats', []):
                    if f.get('acodec') != 'none' and f.get('vcodec') == 'none':
                        return jsonify({'url': f.get('url'), 'title': info.get('title', '')})
                if info.get('url'):
                    return jsonify({'url': info.get('url'), 'title': info.get('title', '')})
    except Exception as e:
        print(f"Download API ошибка: {e}")
    return jsonify({'error': 'Download failed'})

# ===== API ДЛЯ ПРОФИЛЯ И ЛАЙКОВ =====
def init_app_db():
    conn = sqlite3.connect('music_bot.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS user_profiles
                 (telegram_id INTEGER PRIMARY KEY,
                  username TEXT,
                  display_name TEXT,
                  avatar_url TEXT,
                  created_at TIMESTAMP)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS user_likes
                 (telegram_id INTEGER,
                  track_id TEXT,
                  track_title TEXT,
                  track_artist TEXT,
                  track_url TEXT,
                  track_thumbnail TEXT,
                  liked_at TIMESTAMP,
                  PRIMARY KEY (telegram_id, track_id))''')
    conn.commit()
    conn.close()

@web_app.route('/api/profile/<int:telegram_id>', methods=['GET', 'POST', 'PUT'])
def handle_profile(telegram_id):
    conn = sqlite3.connect('music_bot.db')
    c = conn.cursor()
    
    if request.method == 'GET':
        c.execute("SELECT * FROM user_profiles WHERE telegram_id = ?", (telegram_id,))
        profile = c.fetchone()
        conn.close()
        if profile:
            return jsonify({
                'telegram_id': profile[0],
                'username': profile[1],
                'display_name': profile[2],
                'avatar_url': profile[3],
                'created_at': profile[4]
            })
        return jsonify({'exists': False})
    
    elif request.method == 'POST':
        data = request.json
        c.execute('''INSERT OR REPLACE INTO user_profiles 
                     (telegram_id, username, display_name, avatar_url, created_at)
                     VALUES (?, ?, ?, ?, ?)''',
                  (telegram_id, data.get('username', ''), data.get('display_name', ''),
                   data.get('avatar_url', ''), datetime.now()))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    
    elif request.method == 'PUT':
        data = request.json
        c.execute('''UPDATE user_profiles 
                     SET display_name = ?, avatar_url = ?
                     WHERE telegram_id = ?''',
                  (data.get('display_name', ''), data.get('avatar_url', ''), telegram_id))
        conn.commit()
        conn.close()
        return jsonify({'success': True})

@web_app.route('/api/likes/<int:telegram_id>', methods=['GET', 'POST', 'DELETE'])
def handle_likes(telegram_id):
    conn = sqlite3.connect('music_bot.db')
    c = conn.cursor()
    
    if request.method == 'GET':
        c.execute('''SELECT track_id, track_title, track_artist, track_url, track_thumbnail, liked_at 
                     FROM user_likes WHERE telegram_id = ? ORDER BY liked_at DESC''', (telegram_id,))
        likes = c.fetchall()
        conn.close()
        return jsonify([{
            'track_id': l[0],
            'title': l[1],
            'artist': l[2],
            'url': l[3],
            'thumbnail': l[4],
            'liked_at': l[5]
        } for l in likes])
    
    elif request.method == 'POST':
        data = request.json
        c.execute('''INSERT OR REPLACE INTO user_likes 
                     (telegram_id, track_id, track_title, track_artist, track_url, track_thumbnail, liked_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?)''',
                  (telegram_id, data.get('track_id'), data.get('title'), data.get('artist'),
                   data.get('url'), data.get('thumbnail'), datetime.now()))
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    
    elif request.method == 'DELETE':
        track_id = request.args.get('track_id')
        if track_id:
            c.execute("DELETE FROM user_likes WHERE telegram_id = ? AND track_id = ?", (telegram_id, track_id))
            conn.commit()
        conn.close()
        return jsonify({'success': True})

@web_app.route('/api/likes/<int:telegram_id>/check/<track_id>')
def check_like(telegram_id, track_id):
    conn = sqlite3.connect('music_bot.db')
    c = conn.cursor()
    c.execute("SELECT 1 FROM user_likes WHERE telegram_id = ? AND track_id = ?", (telegram_id, track_id))
    exists = c.fetchone() is not None
    conn.close()
    return jsonify({'liked': exists})

def run_web():
    port = int(os.environ.get('PORT', 10000))
    web_app.run(host='0.0.0.0', port=port)
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
    init_app_db()

def add_user(chat_id: int, username: str, first_name: str, last_name: str):
    conn = sqlite3.connect('music_bot.db')
    c = conn.cursor()
    c.execute('''INSERT OR REPLACE INTO users 
                 (chat_id, username, first_name, last_name, registered_at, last_active, total_requests, total_downloads, quality)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
              (chat_id, username, first_name, last_name, datetime.now(), datetime.now(), 0, 0, '192'))
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

def get_soundcloud_info(url: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[int]]:
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

async def process_search(chat_id: int, query: str):
    quality = get_user_quality(chat_id)
    
    audio_path, real_title, thumb_path, uploader = await search_soundcloud(query, quality)
    source = "SoundCloud"
    
    if not audio_path:
        audio_path, real_title, _ = await search_youtube(query, quality)
        source = "YouTube"
    
    if audio_path and os.path.exists(audio_path):
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

async def download_audio_with_queue(chat_id: int, query: str):
    if chat_id not in user_queues:
        user_queues[chat_id] = deque()
        user_processing[chat_id] = False
    
    user_queues[chat_id].append(query)
    
    if user_processing[chat_id]:
        return
    
    user_processing[chat_id] = True
    while user_queues[chat_id]:
        next_query = user_queues[chat_id].popleft()
        await process_search(chat_id, next_query)
    user_processing[chat_id] = False

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
        "• Выбор качества (128/192/320 kbps)\n"
        "• Веб-приложение с профилем и библиотекой",
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
• Веб-приложение с профилем и библиотекой

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
• Billie Eilish
• Metallica Nothing Else Matters
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
    
    if not query_text or query_text.startswith('/'):
        return
    
    update_activity(chat_id, is_download=False)
    
    msg = await update.message.reply_text(f"🔍 Ищу: *{query_text}*\n⏳ Добавлено в очередь...", parse_mode='Markdown')
    await download_audio_with_queue(chat_id, query_text)
    await msg.delete()

async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.message.web_app_data
    if data and data.data:
        try:
            payload = json.loads(data.data)
            if payload.get('action') == 'search':
                query = payload.get('query')
                if query:
                    await download_audio_with_queue(update.effective_chat.id, query)
        except Exception as e:
            print(f"WebApp ошибка: {e}")

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_text = update.inline_query.query.strip()
    
    if not query_text:
        await update.inline_query.answer([], cache_time=0)
        return
    
    def search_sync():
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
            'default_search': 'scsearch',
            'playlistend': 20,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"scsearch20:{query_text}", download=False)
                results = []
                if info and 'entries' in info:
                    for entry in info['entries'][:20]:
                        if entry:
                            title = entry.get('title', 'Без названия')[:60]
                            uploader = entry.get('uploader', 'SoundCloud')
                            duration = entry.get('duration', 0)
                            url = entry.get('webpage_url', '')
                            if url:
                                results.append((title, uploader, duration, url))
                return results
        except Exception as e:
            print(f"Инлайн поиск ошибка: {e}")
            return []
    
    try:
        import concurrent.futures
        loop = asyncio.get_event_loop()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            results = await loop.run_in_executor(pool, search_sync)
    except Exception as e:
        print(f"Таймаут: {e}")
        results = []
    
    inline_results = []
    for i, (title, uploader, duration, url) in enumerate(results[:20]):
        duration_str = f"{duration // 60}:{duration % 60:02d}" if duration else "?"
        result = InlineQueryResultArticle(
            id=str(i),
            title=title,
            description=f"{uploader} • {duration_str}",
            input_message_content=InputTextMessageContent(f"🎵 {title}\n{url}"),
        )
        inline_results.append(result)
    
    if not inline_results:
        inline_results.append(
            InlineQueryResultArticle(
                id="0",
                title=f"Ничего не найдено: {query_text[:40]}",
                description="Попробуйте другой запрос",
                input_message_content=InputTextMessageContent(f"Не удалось найти: {query_text}")
            )
        )
    
    await update.inline_query.answer(inline_results[:20], cache_time=30)

# ===== ЗАПУСК =====
application = None

def main():
    global application
    print("🚀 Запуск бота и веб-сервера...")
    
    threading.Thread(target=run_web, daemon=True).start()
    
    init_db()
    
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    application.add_handler(InlineQueryHandler(inline_query))
    
    print("✅ Бот запущен!")
    print("✨ Mini App доступен по адресу: https://telegram-music-bot-a9vg.onrender.com")
    print(f"Админ: @{ADMIN_USERNAME}")
    
    application.run_polling()

if __name__ == "__main__":
    main()
