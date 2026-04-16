import os
import sqlite3
import asyncio
import threading
import json
import hashlib
import platform
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, List
from collections import deque
from urllib.parse import urlparse

import yt_dlp
import requests
from flask import Flask, send_from_directory, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle, InputTextMessageContent
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, InlineQueryHandler
)

# ==================== КОНФИГ ====================
TOKEN = os.environ.get("BOT_TOKEN", "8410866218:AAFwRJj2RbRuEAMJayfAYnpAOMMdEKdpA_A")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "okey2010").lower().replace("@", "")
APP_URL = os.environ.get("APP_URL", "https://telegram-music-bot-a9vg.onrender.com")

QUALITIES = {'128': '128k', '192': '192k', '320': '320k'}
DEFAULT_QUALITY = '192'

# Состояния
user_queues: Dict[int, deque] = {}
user_processing: Dict[int, bool] = {}

# ==================== РАСШИРЕННАЯ БД ====================
DB_PATH = '/app/data/music_bot.db' if os.path.exists('/app/data') else 'music_bot.db'

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Основная таблица пользователей
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        language_code TEXT,
        is_premium BOOLEAN DEFAULT 0,
        is_bot BOOLEAN DEFAULT 0,
        phone_number TEXT,
        bio TEXT,
        registered_at TIMESTAMP,
        last_active TIMESTAMP,
        last_ip TEXT,
        user_agent TEXT,
        device_type TEXT,
        os_name TEXT,
        os_version TEXT,
        app_version TEXT,
        total_requests INTEGER DEFAULT 0,
        total_downloads INTEGER DEFAULT 0,
        total_likes INTEGER DEFAULT 0,
        total_playlists INTEGER DEFAULT 0,
        total_listening_time INTEGER DEFAULT 0,
        quality TEXT DEFAULT '192',
        referral_code TEXT,
        referred_by INTEGER,
        is_blocked BOOLEAN DEFAULT 0,
        block_reason TEXT,
        notes TEXT
    )''')
    
    # Таблица лайков
    c.execute('''CREATE TABLE IF NOT EXISTS user_likes (
        telegram_id INTEGER,
        track_id TEXT,
        track_title TEXT,
        track_artist TEXT,
        track_url TEXT,
        track_thumbnail TEXT,
        liked_at TIMESTAMP,
        PRIMARY KEY (telegram_id, track_id)
    )''')
    
    # Таблица плейлистов
    c.execute('''CREATE TABLE IF NOT EXISTS playlists (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER,
        name TEXT,
        description TEXT,
        cover_url TEXT,
        is_public BOOLEAN DEFAULT 0,
        created_at TIMESTAMP,
        updated_at TIMESTAMP
    )''')
    
    # Треки в плейлистах
    c.execute('''CREATE TABLE IF NOT EXISTS playlist_tracks (
        playlist_id INTEGER,
        track_id TEXT,
        track_title TEXT,
        track_artist TEXT,
        track_url TEXT,
        track_thumbnail TEXT,
        added_at TIMESTAMP,
        position INTEGER,
        PRIMARY KEY (playlist_id, track_id)
    )''')
    
    # Сессии пользователей
    c.execute('''CREATE TABLE IF NOT EXISTS user_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        session_id TEXT,
        login_time TIMESTAMP,
        logout_time TIMESTAMP,
        ip_address TEXT,
        device_info TEXT
    )''')
    
    # Действия пользователей
    c.execute('''CREATE TABLE IF NOT EXISTS user_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        action_type TEXT,
        action_data TEXT,
        created_at TIMESTAMP
    )''')
    
    # Реферальная система
    c.execute('''CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_chat_id INTEGER,
        referred_chat_id INTEGER,
        created_at TIMESTAMP,
        is_active BOOLEAN DEFAULT 1
    )''')
    
    # Ежедневная статистика
    c.execute('''CREATE TABLE IF NOT EXISTS user_daily_stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        date DATE,
        searches INTEGER DEFAULT 0,
        downloads INTEGER DEFAULT 0,
        likes_added INTEGER DEFAULT 0,
        listening_time INTEGER DEFAULT 0,
        UNIQUE(chat_id, date)
    )''')
    
    # Индексы
    c.execute('CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_users_last_ip ON users(last_ip)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_likes_telegram_id ON user_likes(telegram_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_playlists_telegram_id ON playlists(telegram_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_actions_chat_id ON user_actions(chat_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_actions_created_at ON user_actions(created_at)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_daily_stats_date ON user_daily_stats(date)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_sessions_chat_id ON user_sessions(chat_id)')
    
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

def log_action(chat_id: int, action_type: str, action_data: dict = None):
    execute_query("INSERT INTO user_actions (chat_id, action_type, action_data, created_at) VALUES (?, ?, ?, ?)",
                  (chat_id, action_type, json.dumps(action_data, ensure_ascii=False) if action_data else None, datetime.now()))

def update_daily_stats(chat_id: int, searches=0, downloads=0, likes=0, listening=0):
    today = datetime.now().date()
    execute_query('''INSERT INTO user_daily_stats (chat_id, date, searches, downloads, likes_added, listening_time)
                     VALUES (?, ?, ?, ?, ?, ?)
                     ON CONFLICT(chat_id, date) DO UPDATE SET
                     searches = searches + ?, downloads = downloads + ?,
                     likes_added = likes_added + ?, listening_time = listening_time + ?''',
                  (chat_id, today, searches, downloads, likes, listening, searches, downloads, likes, listening))

def add_user_full(chat_id: int, username: str, first_name: str, last_name: str, language_code: str = None, is_premium: bool = False):
    referral_code = hashlib.md5(f"{chat_id}_{datetime.now()}".encode()).hexdigest()[:8]
    execute_query('''INSERT OR IGNORE INTO users 
        (chat_id, username, first_name, last_name, language_code, is_premium, registered_at, last_active, quality, referral_code)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (chat_id, username or "", first_name or "", last_name or "",
         language_code, 1 if is_premium else 0, datetime.now(), datetime.now(), DEFAULT_QUALITY, referral_code))
    
    # Обновляем существующего
    execute_query('''UPDATE users SET 
        last_active = ?, username = ?, first_name = ?, last_name = ?, language_code = ?, is_premium = ?
        WHERE chat_id = ?''',
        (datetime.now(), username or "", first_name or "", last_name or "", language_code, 1 if is_premium else 0, chat_id))

def update_user_stats(chat_id: int, field: str, increment: int = 1):
    execute_query(f"UPDATE users SET {field} = {field} + ?, last_active = ? WHERE chat_id = ?",
                  (increment, datetime.now(), chat_id))

def update_user_ip(chat_id: int, ip: str, user_agent: str = None, device_type: str = None, os_name: str = None):
    """Обновляет IP и информацию об устройстве пользователя"""
    if user_agent:
        execute_query("UPDATE users SET last_ip = ?, user_agent = ?, device_type = ?, os_name = ? WHERE chat_id = ?",
                      (ip, user_agent[:500], device_type or 'unknown', os_name or 'unknown', chat_id))
    else:
        execute_query("UPDATE users SET last_ip = ? WHERE chat_id = ?", (ip, chat_id))
    log_action(chat_id, 'ip_update', {'ip': ip, 'user_agent': user_agent[:200] if user_agent else None})

def get_user_full_info(chat_id: int) -> dict:
    user = execute_query('''SELECT * FROM users WHERE chat_id = ?''', (chat_id,), fetch_one=True)
    if not user:
        return {}
    columns = ['chat_id', 'username', 'first_name', 'last_name', 'language_code', 'is_premium', 'is_bot',
               'phone_number', 'bio', 'registered_at', 'last_active', 'last_ip', 'user_agent',
               'device_type', 'os_name', 'os_version', 'app_version', 'total_requests', 'total_downloads',
               'total_likes', 'total_playlists', 'total_listening_time', 'quality', 'referral_code',
               'referred_by', 'is_blocked', 'block_reason', 'notes']
    return {columns[i]: user[i] for i in range(len(columns))}

# ==================== ВЕБ-СЕРВЕР ====================
web_app = Flask(__name__)

# ==================== IP ЛОГИРОВАНИЕ ПРИ ОТКРЫТИИ MINI APP ====================
@web_app.route('/api/ip_log', methods=['POST'])
def log_ip():
    """Сохраняет IP и информацию об устройстве, когда пользователь открывает Mini App"""
    data = request.json
    telegram_id = data.get('telegram_id')
    
    if not telegram_id:
        return jsonify({'error': 'No telegram_id'}), 400
    
    # Получаем реальный IP (с учетом прокси)
    ip = request.remote_addr
    if request.headers.get('X-Forwarded-For'):
        ip = request.headers.get('X-Forwarded-For').split(',')[0].strip()
    elif request.headers.get('X-Real-IP'):
        ip = request.headers.get('X-Real-IP')
    
    # Если IP передан с клиента (через api.ipify.org)
    if data.get('ip') and data.get('ip') != 'unknown':
        ip = data.get('ip')
    
    user_agent = data.get('user_agent') or request.headers.get('User-Agent', '')
    device_type = data.get('device_type', 'unknown')
    os_name = data.get('os_name', 'unknown')
    
    # Сохраняем в БД
    update_user_ip(telegram_id, ip, user_agent, device_type, os_name)
    
    # Сохраняем сессию
    session_id = data.get('session_id', hashlib.md5(f"{telegram_id}_{datetime.now()}".encode()).hexdigest()[:16])
    execute_query('''INSERT INTO user_sessions (chat_id, session_id, login_time, ip_address, device_info)
                     VALUES (?, ?, ?, ?, ?)''',
                  (telegram_id, session_id, datetime.now(), ip, user_agent[:200]))
    
    print(f"📡 IP сохранён: {telegram_id} -> {ip} [{device_type}/{os_name}]")
    return jsonify({'success': True, 'ip': ip})

@web_app.route('/')
def serve_index():
    return send_from_directory('.', 'index.html')

@web_app.route('/api/profile/<int:telegram_id>')
def api_profile(telegram_id):
    user = get_user_full_info(telegram_id)
    if not user:
        return jsonify({'exists': False})
    
    likes_count = execute_query("SELECT COUNT(*) FROM user_likes WHERE telegram_id = ?", (telegram_id,), fetch_one=True)[0]
    playlists_count = execute_query("SELECT COUNT(*) FROM playlists WHERE telegram_id = ?", (telegram_id,), fetch_one=True)[0]
    
    return jsonify({
        'exists': True,
        'telegram_id': telegram_id,
        'username': user.get('username'),
        'display_name': user.get('first_name'),
        'last_ip': user.get('last_ip'),
        'device_type': user.get('device_type'),
        'registered_at': user.get('registered_at'),
        'last_active': user.get('last_active'),
        'total_requests': user.get('total_requests'),
        'total_downloads': user.get('total_downloads'),
        'quality': user.get('quality'),
        'stats': {'likes': likes_count, 'playlists': playlists_count}
    })

@web_app.route('/api/likes/<int:telegram_id>', methods=['GET', 'POST', 'DELETE'])
def api_likes(telegram_id):
    if request.method == 'GET':
        likes = execute_query('''SELECT track_id, track_title, track_artist, track_url, track_thumbnail, liked_at 
                                 FROM user_likes WHERE telegram_id = ? ORDER BY liked_at DESC''', 
                              (telegram_id,), fetch_all=True) or []
        return jsonify([{
            'track_id': l[0], 'title': l[1], 'artist': l[2],
            'url': l[3], 'thumbnail': l[4], 'liked_at': l[5]
        } for l in likes])
    
    elif request.method == 'POST':
        data = request.json
        execute_query('''INSERT OR REPLACE INTO user_likes 
                         (telegram_id, track_id, track_title, track_artist, track_url, track_thumbnail, liked_at)
                         VALUES (?, ?, ?, ?, ?, ?, ?)''',
                      (telegram_id, data.get('track_id'), data.get('title'), data.get('artist'),
                       data.get('url'), data.get('thumbnail', ''), datetime.now()))
        update_user_stats(telegram_id, 'total_likes')
        update_daily_stats(telegram_id, likes=1)
        log_action(telegram_id, 'like', {'track_id': data.get('track_id'), 'title': data.get('title')})
        return jsonify({'success': True})
    
    elif request.method == 'DELETE':
        track_id = request.args.get('track_id')
        if track_id:
            execute_query("DELETE FROM user_likes WHERE telegram_id = ? AND track_id = ?", (telegram_id, track_id))
            update_user_stats(telegram_id, 'total_likes', -1)
        return jsonify({'success': True})

@web_app.route('/api/playlists/<int:telegram_id>', methods=['GET', 'POST'])
def api_playlists(telegram_id):
    if request.method == 'GET':
        playlists = execute_query('''SELECT id, name, description, cover_url, is_public, created_at 
                                     FROM playlists WHERE telegram_id = ? ORDER BY created_at DESC''',
                                  (telegram_id,), fetch_all=True) or []
        return jsonify([{
            'id': p[0], 'name': p[1], 'description': p[2],
            'cover_url': p[3], 'is_public': bool(p[4]), 'created_at': p[5]
        } for p in playlists])
    
    elif request.method == 'POST':
        data = request.json
        pid = execute_query('''INSERT INTO playlists (telegram_id, name, description, cover_url, created_at, updated_at)
                               VALUES (?, ?, ?, ?, ?, ?)''',
                            (telegram_id, data.get('name'), data.get('description', ''),
                             data.get('cover_url', ''), datetime.now(), datetime.now()))
        update_user_stats(telegram_id, 'total_playlists')
        log_action(telegram_id, 'create_playlist', {'playlist_id': pid, 'name': data.get('name')})
        return jsonify({'id': pid, 'success': True})

@web_app.route('/api/playlists/<int:playlist_id>/tracks', methods=['GET', 'POST', 'DELETE'])
def api_playlist_tracks(playlist_id):
    if request.method == 'GET':
        tracks = execute_query('''SELECT track_id, track_title, track_artist, track_url, track_thumbnail, position
                                  FROM playlist_tracks WHERE playlist_id = ? ORDER BY position ASC''',
                               (playlist_id,), fetch_all=True) or []
        return jsonify([{
            'track_id': t[0], 'title': t[1], 'artist': t[2],
            'url': t[3], 'thumbnail': t[4], 'position': t[5]
        } for t in tracks])
    
    elif request.method == 'POST':
        data = request.json
        pos = execute_query("SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,), fetch_one=True)[0]
        execute_query('''INSERT OR REPLACE INTO playlist_tracks 
                         (playlist_id, track_id, track_title, track_artist, track_url, track_thumbnail, added_at, position)
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                      (playlist_id, data.get('track_id'), data.get('title'), data.get('artist'),
                       data.get('url'), data.get('thumbnail', ''), datetime.now(), pos))
        log_action(None, 'add_to_playlist', {'playlist_id': playlist_id, 'track_id': data.get('track_id')})
        return jsonify({'success': True})
    
    elif request.method == 'DELETE':
        track_id = request.args.get('track_id')
        if track_id:
            execute_query("DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?", (playlist_id, track_id))
        return jsonify({'success': True})

@web_app.route('/api/stats/<int:telegram_id>')
def api_user_stats(telegram_id):
    week_ago = datetime.now() - timedelta(days=7)
    daily = execute_query('''SELECT date, searches, downloads, likes_added, listening_time
                             FROM user_daily_stats 
                             WHERE chat_id = ? AND date > ? 
                             ORDER BY date DESC''', (telegram_id, week_ago.date()), fetch_all=True) or []
    
    actions = execute_query('''SELECT action_type, action_data, created_at
                               FROM user_actions 
                               WHERE chat_id = ? 
                               ORDER BY created_at DESC LIMIT 20''', (telegram_id,), fetch_all=True) or []
    
    return jsonify({
        'daily_stats': [{'date': d[0], 'searches': d[1], 'downloads': d[2], 'likes': d[3], 'listening': d[4]} for d in daily],
        'recent_actions': [{'type': a[0], 'data': json.loads(a[1]) if a[1] else None, 'time': a[2]} for a in actions]
    })

@web_app.route('/search')
def api_search():
    query = request.args.get('q', '')
    if not query:
        return jsonify([])
    ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True, 'default_search': 'scsearch', 'playlistend': 20}
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
    except:
        return jsonify([])

@web_app.route('/download')
def api_download():
    url = request.args.get('url', '')
    if not url:
        return jsonify({'error': 'No URL'})
    ydl_opts = {'quiet': True, 'no_warnings': True, 'format': 'bestaudio/best'}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info:
                for f in info.get('formats', []):
                    if f.get('acodec') != 'none' and f.get('vcodec') == 'none':
                        return jsonify({'url': f.get('url'), 'title': info.get('title', '')})
                if info.get('url'):
                    return jsonify({'url': info.get('url'), 'title': info.get('title', '')})
    except:
        pass
    return jsonify({'error': 'Download failed'})

@web_app.route('/api/top_users')
def api_top_users():
    admin_key = request.headers.get('X-Admin-Key', '')
    if admin_key != hashlib.md5(ADMIN_USERNAME.encode()).hexdigest():
        return jsonify({'error': 'Unauthorized'}), 401
    
    top = execute_query('''SELECT chat_id, username, first_name, total_requests, total_downloads, total_likes, last_ip, device_type
                           FROM users ORDER BY total_requests DESC LIMIT 20''', fetch_all=True) or []
    return jsonify([{
        'chat_id': t[0], 'username': t[1], 'name': t[2],
        'requests': t[3], 'downloads': t[4], 'likes': t[5],
        'last_ip': t[6], 'device': t[7]
    } for t in top])

# ==================== ТЕЛЕГРАМ КОМАНДЫ ====================
async def is_admin(update: Update) -> bool:
    if not ADMIN_USERNAME:
        return False
    user = update.effective_user
    return user.username and user.username.lower() == ADMIN_USERNAME

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    add_user_full(user.id, user.username or "", user.first_name or "", user.last_name or "",
                  user.language_code, user.is_premium)
    
    await update.message.reply_text(
        f"🎵 Привет, {user.first_name}!\n\n"
        "Я музыкальный бот.\n\n"
        "✨ *Возможности:*\n"
        "• Поиск на SoundCloud\n"
        "• Лайки и плейлисты\n"
        "• Веб-приложение (открывается по кнопке ниже)\n\n"
        "🔗 *Открой Mini App* для полного функционала!",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🎵 Открыть приложение", web_app={"url": APP_URL})
        ]])
    )
    update_user_stats(user.id, 'total_requests')
    log_action(user.id, 'start')

async def me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_info = get_user_full_info(user_id)
    
    if not user_info:
        await update.message.reply_text("❌ Информация не найдена")
        return
    
    likes_count = execute_query("SELECT COUNT(*) FROM user_likes WHERE telegram_id = ?", (user_id,), fetch_one=True)[0]
    playlists_count = execute_query("SELECT COUNT(*) FROM playlists WHERE telegram_id = ?", (user_id,), fetch_one=True)[0]
    
    text = f"""
📊 *Ваша статистика*

👤 *Имя:* {user_info.get('first_name', '?')} {user_info.get('last_name', '')}
🔖 *Username:* @{user_info.get('username', 'нет')}
🌐 *Язык:* {user_info.get('language_code', '?')}
💎 *Premium:* {'Да' if user_info.get('is_premium') else 'Нет'}
🌍 *Последний IP:* `{user_info.get('last_ip', 'неизвестен')}`
📱 *Устройство:* {user_info.get('device_type', '?')} / {user_info.get('os_name', '?')}

📈 *Активность:*
• Запросов: {user_info.get('total_requests', 0)}
• Скачиваний: {user_info.get('total_downloads', 0)}
• Лайков: {likes_count}
• Плейлистов: {playlists_count}

📅 *В боте с:* {user_info.get('registered_at', '?')[:16] if user_info.get('registered_at') else '?'}
🕐 *Последний визит:* {user_info.get('last_active', '?')[:16] if user_info.get('last_active') else '?'}

⚙️ *Качество:* {user_info.get('quality', '192')} kbps
    """
    await update.message.reply_text(text, parse_mode='Markdown')

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("⛔ Только для админа")
        return
    
    total_users = execute_query("SELECT COUNT(*) FROM users", fetch_one=True)[0]
    total_requests = execute_query("SELECT SUM(total_requests) FROM users", fetch_one=True)[0] or 0
    total_downloads = execute_query("SELECT SUM(total_downloads) FROM users", fetch_one=True)[0] or 0
    total_likes = execute_query("SELECT COUNT(*) FROM user_likes", fetch_one=True)[0]
    total_playlists = execute_query("SELECT COUNT(*) FROM playlists", fetch_one=True)[0]
    
    today = datetime.now().date()
    active_today = execute_query("SELECT COUNT(DISTINCT chat_id) FROM user_daily_stats WHERE date = ?", (today,), fetch_one=True)[0]
    premium_users = execute_query("SELECT COUNT(*) FROM users WHERE is_premium = 1", fetch_one=True)[0]
    
    # Статистика по IP (уникальные)
    unique_ips = execute_query("SELECT COUNT(DISTINCT last_ip) FROM users WHERE last_ip IS NOT NULL AND last_ip != ''", fetch_one=True)[0]
    
    await update.message.reply_text(
        f"📊 *Общая статистика*\n\n"
        f"👥 *Всего пользователей:* {total_users}\n"
        f"💎 *Premium:* {premium_users}\n"
        f"📈 *Активных сегодня:* {active_today}\n"
        f"🌍 *Уникальных IP:* {unique_ips}\n\n"
        f"🔍 *Поисков:* {total_requests}\n"
        f"🎵 *Скачиваний:* {total_downloads}\n"
        f"❤️ *Лайков:* {total_likes}\n"
        f"📀 *Плейлистов:* {total_playlists}",
        parse_mode='Markdown'
    )

async def get_db_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("⛔ Доступ запрещён")
        return
    if os.path.exists(DB_PATH):
        await update.message.reply_document(
            document=open(DB_PATH, 'rb'),
            filename='music_bot_backup.db',
            caption=f'📊 Бэкап БД от {datetime.now().strftime("%Y-%m-%d %H:%M")}'
        )
    else:
        await update.message.reply_text("❌ Файл БД не найден")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    if not query or query.startswith('/'):
        return
    
    update_user_stats(update.effective_user.id, 'total_requests')
    update_daily_stats(update.effective_user.id, searches=1)
    log_action(update.effective_user.id, 'search', {'query': query})
    
    await update.message.reply_text(f"🔍 Ищу: {query}\n(функция поиска MP3 в разработке)")

# ==================== ЗАПУСК ====================
def run_web():
    web_app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)), debug=False, use_reloader=False)

def main():
    print("🚀 Запуск бота с IP-логированием...")
    threading.Thread(target=run_web, daemon=True).start()
    init_db()
    
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("me", me))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("getdb", get_db_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("✅ Бот запущен!")
    print(f"Админ: @{ADMIN_USERNAME}")
    print("📡 IP-логирование: при открытии Mini App")
    app.run_polling()

if __name__ == "__main__":
    main()
