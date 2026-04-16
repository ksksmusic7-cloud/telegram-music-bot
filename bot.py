import os
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, Optional

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)

# ===== НАСТРОЙКИ =====
TOKEN = "8410866218:AAFwRJj2RbRuEAMJayfAYnpAOMMdEKdpA_A"

# АДМИН (твой юзернейм без @)
ADMIN_USERNAME = "okey2010"
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
                  total_downloads INTEGER DEFAULT 0)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS search_history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  chat_id INTEGER,
                  query TEXT,
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

def save_search(chat_id: int, query: str, success: bool):
    conn = sqlite3.connect('music_bot.db')
    c = conn.cursor()
    c.execute('''INSERT INTO search_history (chat_id, query, timestamp, success)
                 VALUES (?, ?, ?, ?)''',
              (chat_id, query, datetime.now(), success))
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

# ===== ФУНКЦИЯ СКАЧИВАНИЯ =====
async def download_audio(query: str, chat_id: int) -> Optional[str]:
    filename = f"audio_{chat_id}"
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': f'{filename}.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'default_search': 'scsearch',
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"scsearch1:{query}", download=True)
            if info and 'entries' and len(info['entries']) > 0:
                file_path = ydl.prepare_filename(info['entries'][0])
                if os.path.exists(file_path):
                    return file_path
                for ext in ['.webm', '.m4a', '.opus', '.mp3']:
                    test_path = file_path.split('.')[0] + ext
                    if os.path.exists(test_path):
                        return test_path
    except Exception as e:
        print(f"Ошибка скачивания: {e}")
    return None

# ===== КЛАВИАТУРА =====
def get_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("🎵 Популярное", callback_data='popular')],
        [InlineKeyboardButton("📊 Статистика", callback_data='stats')],
        [InlineKeyboardButton("❓ Помощь", callback_data='help')],
    ]
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
        "Я музыкальный бот. Просто напиши название песни или исполнителя,\n"
        "а я найду трек на SoundCloud и отправлю тебе.",
        reply_markup=get_main_keyboard()
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
📖 *Как пользоваться ботом:*

• Просто напиши название песни или исполнителя
• Используй кнопки для быстрого доступа

🎵 *Примеры запросов:*
• Imagine Dragons Believer
• Billie Eilish
• Metallica

⚙️ *Команды:*
/start - начать
/help - помощь
/stats - статистика (только для админа)

📌 *Источники:*
Поиск идёт по SoundCloud
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
    
    if query.data == 'help':
        text = """
📖 *Помощь*

Просто напиши название песни или исполнителя.
Бот найдёт трек на SoundCloud и отправит тебе.

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
        popular = get_popular_tracks(5)
        
        if popular:
            text = "🎵 *Самые популярные запросы:*\n\n"
            for i, (track, count) in enumerate(popular, 1):
                text += f"{i}. {track} — {count} раз(а)\n"
        else:
            text = "Пока нет популярных запросов."
        
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=get_main_keyboard())

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_text = update.message.text.strip()
    chat_id = update.message.chat_id
    
    if not query_text:
        return
    
    update_activity(chat_id, is_download=False)
    msg = await update.message.reply_text(f"🔍 Ищу: *{query_text}*", parse_mode='Markdown')
    
    audio_path = await download_audio(query_text, chat_id)
    
    if audio_path and os.path.exists(audio_path):
        with open(audio_path, 'rb') as f:
            await update.message.reply_audio(
                audio=f,
                title=query_text[:60],
                performer="SoundCloud"
            )
        os.remove(audio_path)
        await msg.delete()
        save_search(chat_id, query_text, success=True)
        update_activity(chat_id, is_download=True)
    else:
        await msg.edit_text(f"❌ Не нашёл: *{query_text}*\nПопробуй другой запрос.", parse_mode='Markdown')
        save_search(chat_id, query_text, success=False)

# ===== ЗАПУСК =====
def main():
    print("🚀 Запуск бота...")
    print(f"Токен: {TOKEN[:10]}... (скрыто)")
    
    init_db()
    
    # БЕЗ ПРОКСИ (сервер во Франкфурте)
    app = Application.builder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("✅ Бот запущен! (без прокси, сервер во Франкфурте)")
    print(f"Админ: @{ADMIN_USERNAME}")
    app.run_polling()

if __name__ == "__main__":
    main()
