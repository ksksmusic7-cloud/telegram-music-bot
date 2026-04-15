import os
import yt_dlp
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

TOKEN = os.environ.get("BOT_TOKEN", "СЮДА_ВСТАВЬ_ТОКЕН_ДЛЯ_ПРОВЕРКИ")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎵 Привет! Я музыкальный бот. Просто напиши название песни, я найду на SoundCloud.")

async def download_audio(query, chat_id):
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
        print(f"Ошибка: {e}")
    return None

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    chat_id = update.message.chat_id
    if not query:
        return
    msg = await update.message.reply_text(f"🔍 Ищу: {query}")
    audio_path = await download_audio(query, chat_id)
    if audio_path and os.path.exists(audio_path):
        with open(audio_path, 'rb') as f:
            await update.message.reply_audio(audio=f, title=query[:60])
        os.remove(audio_path)
        await msg.delete()
    else:
        await msg.edit_text(f"❌ Не нашёл: {query}")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("✅ Бот запущен")
    app.run_polling()

if __name__ == "__main__":
    main()
