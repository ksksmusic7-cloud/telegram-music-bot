import os
import sqlite3
import threading
import json
import hashlib
import requests as http_requests
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Dict, Optional, Tuple, List
from collections import deque

import yt_dlp
from flask import Flask, send_from_directory, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)

# ==================== КОНФИГ ====================
TOKEN = os.environ.get("BOT_TOKEN", "8410866218:AAFwRJj2RbRuEAMJayfAYnpAOMMdEKdpA_A")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "okey2010").lower().replace("@", "")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "7889817936"))
APP_URL = os.environ.get("APP_URL", "https://telegram-music-bot-a9vg.onrender.com")

QUALITIES = {'128': '128k', '192': '192k', '320': '320k'}
DEFAULT_QUALITY = '192'

# Состояния
user_queues: Dict[int, deque] = {}
user_processing: Dict[int, bool] = {}

# ==================== БД (оптимизированная) ====================
DB_PATH = 'music_bot.db'

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        language_code TEXT,
        is_premium BOOLEAN DEFAULT 0,
        registered_at TIMESTAMP,
        last_active TIMESTAMP,
        last_ip TEXT,
        country TEXT,
        city TEXT,
        device_type TEXT,
        os_name TEXT,
        total_requests INTEGER DEFAULT 0,
        total_downloads INTEGER DEFAULT 0,
        total_likes INTEGER DEFAULT 0,
        total_playlists INTEGER DEFAULT 0,
        quality TEXT DEFAULT '192',
        referral_code TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS user_likes (
        telegram_id INTEGER,
        track_id TEXT,
        track_title TEXT,
        track_artist TEXT,
        track_url TEXT,
        liked_at TIMESTAMP,
        PRIMARY KEY (telegram_id, track_id)
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS playlists (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER,
        name TEXT,
        created_at TIMESTAMP
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS playlist_tracks (
        playlist_id INTEGER,
        track_id TEXT,
        track_title TEXT,
        track_artist TEXT,
        track_url TEXT,
        added_at TIMESTAMP,
        position INTEGER,
        PRIMARY KEY (playlist_id, track_id)
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS user_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        action_type TEXT,
        created_at TIMESTAMP
    )''')
    
    # Индексы для скорости
    c.execute('CREATE INDEX IF NOT EXISTS idx_users_chat_id ON users(chat_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_likes_telegram_id ON user_likes(telegram_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_playlists_telegram_id ON playlists(telegram_id)')
    
    conn.commit()
    conn.close()

def execute_query(query, params=(), fetch_one=False, fetch_all=False):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute(query, params)
        if fetch_one:
            return c.fetchone()
        if fetch_all:
            return c.fetchall()
        conn.commit()
        return c.lastrowid if query.strip().upper().startswith('INSERT') else None

def add_user(chat_id: int, username: str, first_name: str, last_name: str):
    referral_code = hashlib.md5(f"{chat_id}_{datetime.now()}".encode()).hexdigest()[:8]
    execute_query('''INSERT OR IGNORE INTO users 
        (chat_id, username, first_name, last_name, registered_at, last_active, quality, referral_code)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        (chat_id, username or "", first_name or "", last_name or "", datetime.now(), datetime.now(), DEFAULT_QUALITY, referral_code))

def update_activity(chat_id: int, is_download: bool = False):
    if is_download:
        execute_query('''UPDATE users SET last_active = ?, total_requests = total_requests + 1, total_downloads = total_downloads + 1
                         WHERE chat_id = ?''', (datetime.now(), chat_id))
    else:
        execute_query('''UPDATE users SET last_active = ?, total_requests = total_requests + 1
                         WHERE chat_id = ?''', (datetime.now(), chat_id))

def get_user_quality(chat_id: int) -> str:
    r = execute_query("SELECT quality FROM users WHERE chat_id = ?", (chat_id,), fetch_one=True)
    return r[0] if r and r[0] else DEFAULT_QUALITY

def set_user_quality(chat_id: int, quality: str):
    execute_query("UPDATE users SET quality = ? WHERE chat_id = ?", (quality, chat_id))

def add_like(telegram_id: int, track_id: str, title: str, artist: str, url: str):
    execute_query('''INSERT OR REPLACE INTO user_likes (telegram_id, track_id, track_title, track_artist, track_url, liked_at)
                     VALUES (?, ?, ?, ?, ?, ?)''',
                  (telegram_id, track_id, title, artist, url, datetime.now()))
    execute_query("UPDATE users SET total_likes = total_likes + 1 WHERE chat_id = ?", (telegram_id,))

def remove_like(telegram_id: int, track_id: str):
    execute_query("DELETE FROM user_likes WHERE telegram_id = ? AND track_id = ?", (telegram_id, track_id))
    execute_query("UPDATE users SET total_likes = total_likes - 1 WHERE chat_id = ?", (telegram_id,))

def get_likes(telegram_id: int):
    return execute_query('''SELECT track_id, track_title, track_artist, track_url, liked_at 
                            FROM user_likes WHERE telegram_id = ? ORDER BY liked_at DESC''', (telegram_id,), fetch_all=True) or []

def create_playlist(telegram_id: int, name: str):
    pid = execute_query("INSERT INTO playlists (telegram_id, name, created_at) VALUES (?, ?, ?)",
                        (telegram_id, name, datetime.now()))
    execute_query("UPDATE users SET total_playlists = total_playlists + 1 WHERE chat_id = ?", (telegram_id,))
    return pid

def get_playlists(telegram_id: int):
    return execute_query("SELECT id, name, created_at FROM playlists WHERE telegram_id = ? ORDER BY created_at DESC", 
                         (telegram_id,), fetch_all=True) or []

def add_track_to_playlist(playlist_id: int, track_id: str, title: str, artist: str, url: str):
    pos = execute_query("SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,), fetch_one=True)[0]
    execute_query('''INSERT OR REPLACE INTO playlist_tracks 
                     (playlist_id, track_id, track_title, track_artist, track_url, added_at, position)
                     VALUES (?, ?, ?, ?, ?, ?, ?)''',
                  (playlist_id, track_id, title, artist, url, datetime.now(), pos))

def get_playlist_tracks(playlist_id: int):
    return execute_query('''SELECT track_id, track_title, track_artist, track_url, position
                            FROM playlist_tracks WHERE playlist_id = ? ORDER BY position ASC''', (playlist_id,), fetch_all=True) or []

# ==================== КЭШИРОВАННЫЙ ПОИСК АУДИО (экономия памяти) ====================
@lru_cache(maxsize=100)
def get_cached_audio_url(url: str) -> Optional[str]:
    """Кэширует прямую ссылку на аудио (без скачивания файла)"""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'format': 'bestaudio/best',
        'extract_flat': False,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info:
                for f in info.get('formats', []):
                    if f.get('acodec') != 'none' and f.get('vcodec') == 'none':
                        return f.get('url')
                return info.get('url')
    except Exception as e:
        print(f"Audio error: {e}")
    return None

# ==================== ВЕБ-СЕРВЕР ====================
web_app = Flask(__name__)

@web_app.route('/')
def serve_index():
    return send_from_directory('.', 'index.html')

@web_app.route('/api/profile/<int:telegram_id>')
def api_profile(telegram_id):
    user = execute_query("SELECT first_name, username, country, city, device_type, os_name, total_requests, total_downloads, quality FROM users WHERE chat_id = ?", 
                         (telegram_id,), fetch_one=True)
    if not user:
        return jsonify({'exists': False})
    
    likes_count = execute_query("SELECT COUNT(*) FROM user_likes WHERE telegram_id = ?", (telegram_id,), fetch_one=True)[0]
    playlists_count = execute_query("SELECT COUNT(*) FROM playlists WHERE telegram_id = ?", (telegram_id,), fetch_one=True)[0]
    
    return jsonify({
        'exists': True,
        'display_name': user[0] or 'User',
        'username': user[1] or '',
        'country': user[2],
        'city': user[3],
        'device_type': user[4],
        'os_name': user[5],
        'total_requests': user[6] or 0,
        'total_downloads': user[7] or 0,
        'quality': user[8] or '192',
        'stats': {'likes': likes_count, 'playlists': playlists_count}
    })

@web_app.route('/api/likes/<int:telegram_id>', methods=['GET', 'POST', 'DELETE'])
def api_likes(telegram_id):
    if request.method == 'GET':
        likes = get_likes(telegram_id)
        return jsonify([{'track_id': l[0], 'title': l[1], 'artist': l[2], 'url': l[3], 'liked_at': l[4]} for l in likes])
    elif request.method == 'POST':
        data = request.json
        add_like(telegram_id, data.get('track_id'), data.get('title'), data.get('artist'), data.get('url'))
        return jsonify({'success': True})
    elif request.method == 'DELETE':
        track_id = request.args.get('track_id')
        if track_id:
            remove_like(telegram_id, track_id)
        return jsonify({'success': True})

@web_app.route('/api/playlists/<int:telegram_id>', methods=['GET', 'POST'])
def api_playlists(telegram_id):
    if request.method == 'GET':
        playlists = get_playlists(telegram_id)
        return jsonify([{'id': p[0], 'name': p[1], 'created_at': p[2]} for p in playlists])
    elif request.method == 'POST':
        data = request.json
        pid = create_playlist(telegram_id, data.get('name'))
        return jsonify({'id': pid, 'success': True})

@web_app.route('/api/playlists/<int:playlist_id>/tracks', methods=['GET', 'POST', 'DELETE'])
def api_playlist_tracks(playlist_id):
    if request.method == 'GET':
        tracks = get_playlist_tracks(playlist_id)
        return jsonify([{'track_id': t[0], 'title': t[1], 'artist': t[2], 'url': t[3], 'position': t[4]} for t in tracks])
    elif request.method == 'POST':
        data = request.json
        add_track_to_playlist(playlist_id, data.get('track_id'), data.get('title'), data.get('artist'), data.get('url'))
        return jsonify({'success': True})
    elif request.method == 'DELETE':
        track_id = request.args.get('track_id')
        if track_id:
            execute_query("DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?", (playlist_id, track_id))
        return jsonify({'success': True})

@web_app.route('/search')
def api_search():
    query = request.args.get('q', '')
    if not query:
        return jsonify([])
    ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True, 'default_search': 'scsearch', 'playlistend': 15}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"scsearch15:{query}", download=False)
            results = []
            if info and 'entries' in info:
                for entry in info['entries'][:15]:
                    if entry:
                        results.append({
                            'id': entry.get('id', ''),
                            'title': entry.get('title', 'Без названия')[:70],
                            'artist': entry.get('uploader', 'SoundCloud'),
                            'duration': entry.get('duration', 0),
                            'url': entry.get('webpage_url', ''),
                            'thumbnail': entry.get('thumbnail', '')
                        })
            return jsonify(results)
    except:
        return jsonify([])

@web_app.route('/download')
def api_download():
    """Возвращает прямую ссылку на аудио (экономит память и CPU)"""
    url = request.args.get('url', '')
    if not url:
        return jsonify({'error': 'No URL'})
    
    audio_url = get_cached_audio_url(url)
    if audio_url:
        return jsonify({'url': audio_url})
    return jsonify({'error': 'Download failed'})

# ==================== ТЕЛЕГРАМ КОМАНДЫ ====================
async def is_admin(update: Update) -> bool:
    if not ADMIN_USERNAME:
        return False
    user = update.effective_user
    return user.username and user.username.lower() == ADMIN_USERNAME

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user(user.id, user.username or "", user.first_name or "", user.last_name or "")
    await update.message.reply_text(
        f"🎵 Привет, {user.first_name}!\n\n"
        "Я музыкальный бот. Просто напиши название песни или исполнителя.\n\n"
        "🔗 *Открой Mini App* для полного функционала!",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🎵 Открыть приложение", web_app={"url": APP_URL})
        ]])
    )
    update_activity(user.id)

async def me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = execute_query("SELECT first_name, username, country, city, device_type, os_name, total_requests, total_downloads, total_likes, total_playlists, quality FROM users WHERE chat_id = ?", 
                         (user_id,), fetch_one=True)
    if not user:
        await update.message.reply_text("❌ Информация не найдена")
        return
    
    likes_count = execute_query("SELECT COUNT(*) FROM user_likes WHERE telegram_id = ?", (user_id,), fetch_one=True)[0]
    playlists_count = execute_query("SELECT COUNT(*) FROM playlists WHERE telegram_id = ?", (user_id,), fetch_one=True)[0]
    
    await update.message.reply_text(
        f"📊 *Ваша статистика*\n\n"
        f"👤 *Имя:* {user[0] or '?'}\n"
        f"🔖 *Username:* @{user[1] or 'нет'}\n"
        f"🌍 *Страна:* {user[2] or 'неизвестно'}\n"
        f"📱 *Устройство:* {user[3] or '?'} / {user[4] or '?'}\n\n"
        f"📈 *Активность:*\n"
        f"• Запросов: {user[5] or 0}\n"
        f"• Скачиваний: {user[6] or 0}\n"
        f"• Лайков: {likes_count}\n"
        f"• Плейлистов: {playlists_count}\n\n"
        f"⚙️ *Качество:* {user[10] or '192'} kbps",
        parse_mode='Markdown'
    )

async def get_db_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("⛔ Доступ запрещён")
        return
    if os.path.exists(DB_PATH):
        await update.message.reply_document(
            document=open(DB_PATH, 'rb'),
            filename=f'music_bot_backup.db',
            caption=f'📊 Бэкап БД'
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    if not query or query.startswith('/'):
        return
    update_activity(update.effective_user.id)
    await update.message.reply_text(f"🔍 Ищу: {query}\n(открой Mini App для воспроизведения)")

# ==================== ЗАПУСК ====================
def run_web():
    web_app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)), debug=False, use_reloader=False)

def main():
    print("🚀 Запуск оптимизированного бота...")
    threading.Thread(target=run_web, daemon=True).start()
    init_db()
    
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("me", me))
    app.add_handler(CommandHandler("getdb", get_db_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("✅ Бот запущен!")
    print(f"💾 Память: используется кэш на 100 треков")
    print(f"⚡ Скорость: прямые ссылки, без скачивания MP3")
    app.run_polling()

if __name__ == "__main__":
    main()
