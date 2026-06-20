import asyncio
import email
import html
import os
import random
import shutil
import sqlite3
import string
import time
import urllib.parse
from email.header import decode_header

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiohttp import web
from aiosmtpd.smtp import SMTP as SMTPServer

# --- НАСТРОЙКИ ---
# Рекомендуется использовать переменные окружения, например: os.getenv("BOT_TOKEN")
BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
DOMAIN = "yourdomain.com"
BOT_PASSWORD = "your_secure_password"
# Если прокси не используется, оставьте строку пустой или удалите инициализацию сессии
PROXY_URL = None

# --- НАСТРОЙКИ ВЕБ-СЕРВЕРА ---
WEB_PORT = 80
WEB_URL_BASE = "http://yourserver.com"

dp = Dispatcher()

# --- БАЗА ДАННЫХ И ПАПКИ ---
ATTACHMENTS_DIR = "attachments"


def init_env():
    # Создаем БД
    conn = sqlite3.connect("mailbot.db")
    c = conn.cursor()
    c.execute(
        """CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, auth INTEGER)"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS emails (email TEXT PRIMARY KEY, user_id INTEGER)"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS messages (token TEXT PRIMARY KEY, html TEXT, expires_at REAL)"""
    )
    conn.commit()
    conn.close()

    # Создаем папку для вложений
    if not os.path.exists(ATTACHMENTS_DIR):
        os.makedirs(ATTACHMENTS_DIR)


def is_auth(user_id):
    conn = sqlite3.connect("mailbot.db")
    c = conn.cursor()
    c.execute("SELECT auth FROM users WHERE user_id = ?", (user_id,))
    res = c.fetchone()
    conn.close()
    return res and res[0] == 1


def set_auth(user_id):
    conn = sqlite3.connect("mailbot.db")
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO users (user_id, auth) VALUES (?, 1)", (user_id,))
    conn.commit()
    conn.close()


def format_size(size_bytes):
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.2f} MB"


# --- ОБРАБОТЧИКИ ТЕЛЕГРАМ БОТА ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    if is_auth(message.from_user.id):
        await message.answer(
            "Вы уже авторизованы.\n/create - создать почту\n/panel - посмотреть все почты"
        )
    else:
        await message.answer(
            f"Добро пожаловать в бота почты {DOMAIN}!\nВведите пароль:"
        )


@dp.message(F.text == BOT_PASSWORD)
async def auth_password(message: Message):
    if is_auth(message.from_user.id):
        return
    set_auth(message.from_user.id)
    await message.answer("✅ Пароль принят!\nИспользуйте /create для создания ящика.")


@dp.message(Command("create"))
async def cmd_create(message: Message):
    if not is_auth(message.from_user.id):
        return await message.answer("Введите пароль.")
    prefix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    new_email = f"{prefix}@{DOMAIN}"

    conn = sqlite3.connect("mailbot.db")
    c = conn.cursor()
    c.execute(
        "INSERT INTO emails (email, user_id) VALUES (?, ?)",
        (new_email, message.from_user.id),
    )
    conn.commit()
    conn.close()
    await message.answer(
        f"✅ Создана почта:\n<code>{new_email}</code>", parse_mode="HTML"
    )


@dp.message(Command("panel"))
async def cmd_panel(message: Message):
    if not is_auth(message.from_user.id):
        return await message.answer("Введите пароль.")
    conn = sqlite3.connect("mailbot.db")
    c = conn.cursor()
    c.execute("SELECT email FROM emails")
    emails = c.fetchall()
    conn.close()

    if not emails:
        return await message.answer("В базе пока нет созданных почт.")
    text = "📋 <b>Все существующие почты:</b>\n\n"
    for e in emails:
        text += f"- <code>{e[0]}</code>\n"
    await message.answer(text, parse_mode="HTML")


# --- ПОЧТОВЫЙ СЕРВЕР (SMTP) ---
def decode_mime_words(s):
    if not s:
        return "Нет данных"
    out = ""
    try:
        for word, charset in decode_header(s):
            if isinstance(word, bytes):
                out += word.decode(charset or "utf-8", errors="ignore")
            else:
                out += word
    except Exception:
        return str(s)
    return out


class MailHandler:
    def __init__(self, bot: Bot):
        self.bot = bot

    async def handle_DATA(self, server, session, envelope):
        rcpt_tos = envelope.rcpt_tos
        msg = email.message_from_bytes(envelope.content)

        subject = decode_mime_words(msg.get("Subject"))
        sender = decode_mime_words(msg.get("From"))

        plain_text = ""
        html_content = ""
        attachments = []

        # Генерируем токен заранее, чтобы создать папку для файлов этого письма
        token = "".join(random.choices(string.ascii_letters + string.digits, k=16))
        msg_dir = os.path.join(ATTACHMENTS_DIR, token)

        print("\n" + "=" * 40)
        print(f"📥 ПРИШЛО НОВОЕ ПИСЬМО ОТ: {sender}")

        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue

            content_type = part.get_content_type()
            payload = part.get_payload(decode=True)

            if not payload:
                continue

            filename = part.get_filename()
            if filename:
                filename = decode_mime_words(filename)

            # Если это текст и нет имени файла - это тело письма
            if content_type == "text/plain" and not filename:
                if not plain_text:
                    plain_text = payload.decode(errors="ignore")

            elif content_type == "text/html" and not filename:
                if not html_content:
                    html_content = payload.decode(errors="ignore")

            # Если есть имя файла - это ВЛОЖЕНИЕ (любой формат)
            elif filename or "name=" in part.get("Content-Type", ""):
                if not filename:
                    filename = f"file_{random.randint(1000, 9999)}.bin"

                # Создаем папку, если еще не создана
                os.makedirs(msg_dir, exist_ok=True)
                filepath = os.path.join(msg_dir, filename)

                with open(filepath, "wb") as f:
                    f.write(payload)

                attachments.append(
                    {
                        "name": filename,
                        "size": len(payload),
                        "is_image": content_type.startswith("image/"),
                    }
                )

        # Выбираем тело письма
        if html_content:
            body_to_render = html_content
        elif plain_text:
            body_to_render = f'<div style="white-space: pre-wrap; font-family: Roboto, sans-serif; color: #1c1b1f;">{html.escape(plain_text)}</div>'
        else:
            body_to_render = "<p style='color: #7f8c8d; font-style: italic;'>[Текст письма отсутствует]</p>"

        # --- ГЕНЕРАЦИЯ HTML В СТИЛЕ MD3 (DARK THEME) ---
        html_template = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{html.escape(subject)}</title>
    <link href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&display=swap" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@24,400,0,0" rel="stylesheet" />
    <style>
        :root {{
            --md-sys-color-background: #141218;
            --md-sys-color-on-background: #E6E0E9;
            --md-sys-color-surface: #211F26;
            --md-sys-color-surface-container: #2B2930;
            --md-sys-color-primary: #D0BCFF;
            --md-sys-color-on-surface-variant: #CAC4D0;
            --md-sys-color-outline: #938F99;
        }}
        body {{
            font-family: 'Roboto', sans-serif;
            background-color: var(--md-sys-color-background);
            color: var(--md-sys-color-on-background);
            margin: 0; padding: 16px;
            display: flex; justify-content: center;
        }}
        .container {{
            max-width: 800px; width: 100%;
            display: flex; flex-direction: column; gap: 16px;
        }}
        .header-card {{
            background-color: var(--md-sys-color-surface);
            border-radius: 28px; padding: 24px;
            box-shadow: 0 4px 8px rgba(0,0,0,0.2);
        }}
        .subject {{
            font-size: 24px; font-weight: 500; margin-bottom: 16px;
            color: var(--md-sys-color-on-background);
        }}
        .meta-row {{
            display: flex; align-items: center; gap: 12px; margin-bottom: 8px;
            font-size: 14px;
        }}
        .meta-icon {{ color: var(--md-sys-color-on-surface-variant); font-size: 20px; }}
        .meta-label {{ color: var(--md-sys-color-on-surface-variant); width: 50px; }}
        .meta-value {{ color: var(--md-sys-color-primary); font-weight: 500; word-break: break-all; }}

        .body-card {{
            background-color: #FFFFFF; /* Белый фон для корректного отображения HTML писем */
            color: #1C1B1F;
            border-radius: 28px; padding: 24px;
            overflow-x: auto;
            box-shadow: 0 4px 8px rgba(0,0,0,0.2);
        }}

        .attachments-section {{
            background-color: var(--md-sys-color-surface);
            border-radius: 28px; padding: 24px;
            box-shadow: 0 4px 8px rgba(0,0,0,0.2);
        }}
        .attachments-title {{
            font-size: 18px; font-weight: 500; margin-bottom: 16px;
            display: flex; align-items: center; gap: 8px;
        }}
        .attachments-grid {{
            display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 12px;
        }}
        .file-card {{
            background-color: var(--md-sys-color-surface-container);
            border-radius: 16px; padding: 12px;
            display: flex; align-items: center; gap: 16px;
            text-decoration: none; color: inherit;
            transition: background-color 0.2s;
            border: 1px solid var(--md-sys-color-outline);
        }}
        .file-card:hover {{ background-color: #36343B; cursor: pointer; }}
        .file-icon {{
            background-color: var(--md-sys-color-primary);
            color: #381E72;
            width: 40px; height: 40px; border-radius: 50%;
            display: flex; justify-content: center; align-items: center;
        }}
        .file-info {{ flex-grow: 1; overflow: hidden; }}
        .file-name {{ font-size: 14px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
        .file-size {{ font-size: 12px; color: var(--md-sys-color-on-surface-variant); mt-2 }}
        .image-preview {{
            margin-top: 16px; max-width: 100%; border-radius: 16px; display: block;
        }}
    </style>
</head>
<body>
<div class="container">
    <div class="header-card">
        <div class="subject">{html.escape(subject)}</div>
        <div class="meta-row">
            <span class="material-symbols-outlined meta-icon">person</span>
            <span class="meta-label">От:</span>
            <span class="meta-value">{html.escape(sender)}</span>
        </div>
        <div class="meta-row">
            <span class="material-symbols-outlined meta-icon">mail</span>
            <span class="meta-label">Кому:</span>
            <span class="meta-value">{html.escape(", ".join(rcpt_tos))}</span>
        </div>
    </div>

    <div class="body-card">
        {body_to_render}
    </div>
"""
        # Блок с любыми вложениями
        if attachments:
            html_template += """
    <div class="attachments-section">
        <div class="attachments-title">
            <span class="material-symbols-outlined">attachment</span>
            Вложения
        </div>
        <div class="attachments-grid">"""

            for att in attachments:
                safe_name = html.escape(att["name"])
                url_name = urllib.parse.quote(att["name"])
                link = f"/download/{token}/{url_name}"
                size_str = format_size(att["size"])
                icon = "image" if att["is_image"] else "insert_drive_file"

                html_template += f'''
            <a href="{link}" class="file-card" target="_blank">
                <div class="file-icon"><span class="material-symbols-outlined">{icon}</span></div>
                <div class="file-info">
                    <div class="file-name">{safe_name}</div>
                    <div class="file-size">{size_str}</div>
                </div>
                <span class="material-symbols-outlined" style="color: var(--md-sys-color-on-surface-variant)">download</span>
            </a>'''

            html_template += "</div>"

            # Если среди файлов были картинки, покажем их предпросмотр под списком файлов
            for att in attachments:
                if att["is_image"]:
                    url_name = urllib.parse.quote(att["name"])
                    html_template += f'<img src="/download/{token}/{url_name}" class="image-preview">'

            html_template += "</div>"

        html_template += "</div></body></html>"

        # Сохраняем в базу данных
        expires_at = time.time() + 3600
        conn = sqlite3.connect("mailbot.db")
        c = conn.cursor()
        c.execute(
            "INSERT INTO messages (token, html, expires_at) VALUES (?, ?, ?)",
            (token, html_template, expires_at),
        )
        conn.commit()

        email_link = f"{WEB_URL_BASE}/mail/{token}"
        zercalo = f"http://195.93.252.54/mail/{token}"
        if not plain_text:
            plain_text = "[HTML-письмо. Откройте по ссылке]"

        # Отправка уведомлений
        for rcpt in rcpt_tos:
            c.execute("SELECT user_id FROM emails WHERE email = ?", (rcpt,))
            row = c.fetchone()
            if row:
                user_id = row[0]

                att_text = (
                    f"\n📎 <b>Вложений:</b> {len(attachments)} шт."
                    if attachments
                    else ""
                )
                text_for_tg = (
                    f"📧 <b>Новое письмо!</b>\n\n"
                    f"📥 <b>Кому:</b> <code>{html.escape(rcpt)}</code>\n"
                    f"📤 <b>От:</b> <code>{html.escape(sender)}</code>\n"
                    f"📝 <b>Тема:</b> {html.escape(subject)}{att_text}\n\n"
                    f"<b>Превью:</b>\n{html.escape(plain_text[:800])}...\n\n"
                    f"Ссылка: {email_link}\n"
                    f"Зеркало: {zercalo}\n"
                    f"<i>⏳ Ссылка активна 1 час.</i>"
                )

                markup = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="📄 Открыть полное письмо", url=email_link
                            )
                        ]
                    ]
                )

                try:
                    await self.bot.send_message(
                        user_id, text_for_tg, parse_mode="HTML", reply_markup=markup
                    )
                except Exception as e:
                    print(f"❌ Ошибка отправки ТГ: {e}")

        conn.close()
        return "250 Message accepted for delivery"


# --- ОБРАБОТКА ЗАПРОСОВ ВЕБ-СЕРВЕРА ---
async def handle_mail_view(request):
    token = request.match_info.get("token")
    conn = sqlite3.connect("mailbot.db")
    c = conn.cursor()
    c.execute("SELECT html, expires_at FROM messages WHERE token = ?", (token,))
    row = c.fetchone()

    if not row:
        conn.close()
        return web.Response(
            text="<h1>Ошибка 404</h1><p>Письмо не найдено.</p>",
            content_type="text/html",
            status=404,
        )

    html_content, expires_at = row
    if time.time() > expires_at:
        c.execute("DELETE FROM messages WHERE token = ?", (token,))
        conn.commit()
        conn.close()
        shutil.rmtree(os.path.join(ATTACHMENTS_DIR, token), ignore_errors=True)
        return web.Response(
            text="<h1>Ссылка устарела</h1><p>Письмо удалено.</p>",
            content_type="text/html",
            status=404,
        )

    conn.close()
    return web.Response(text=html_content, content_type="text/html")


async def handle_download(request):
    token = request.match_info.get("token")
    filename = request.match_info.get("filename")

    # Защита от выхода из директории (Path Traversal)
    filename = urllib.parse.unquote(filename).replace("/", "").replace("\\", "")
    filepath = os.path.join(ATTACHMENTS_DIR, token, filename)

    if os.path.exists(filepath):
        return web.FileResponse(filepath)
    return web.Response(status=404, text="Файл не найден или срок действия истек.")


# --- ФОНОВАЯ ОЧИСТКА БАЗЫ И ФАЙЛОВ ---
async def cleanup_storage():
    """Раз в минуту проверяет БД и удаляет письма и файлы старше 1 часа"""
    while True:
        now = time.time()
        try:
            conn = sqlite3.connect("mailbot.db")
            c = conn.cursor()
            c.execute("SELECT token FROM messages WHERE expires_at < ?", (now,))
            expired_tokens = c.fetchall()

            for (token,) in expired_tokens:
                # Удаляем файлы с диска
                shutil.rmtree(os.path.join(ATTACHMENTS_DIR, token), ignore_errors=True)
                # Удаляем из БД
                c.execute("DELETE FROM messages WHERE token = ?", (token,))

            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Ошибка очистки БД: {e}")

        await asyncio.sleep(60)


# --- ОСНОВНОЙ ЗАПУСК ---
async def main():
    init_env()

    session = AiohttpSession(proxy=PROXY_URL, timeout=300.0)
    bot = Bot(token=BOT_TOKEN, session=session)

    asyncio.create_task(cleanup_storage())

    app = web.Application()
    app.add_routes(
        [
            web.get("/mail/{token}", handle_mail_view),
            web.get(
                "/download/{token}/{filename}", handle_download
            ),  # Маршрут для файлов
        ]
    )
    runner = web.AppRunner(app)
    await runner.setup()

    try:
        site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
        await site.start()
        print(f"🌐 Веб-сервер запущен на порту {WEB_PORT}")
    except OSError as e:
        print(f"❌ ОШИБКА: Порт {WEB_PORT} занят.")
        return

    handler = MailHandler(bot)
    loop = asyncio.get_running_loop()
    await loop.create_server(lambda: SMTPServer(handler), host="0.0.0.0", port=25)
    print("📧 SMTP Сервер запущен на 25 порту.")

    await dp.start_polling(bot)


if __name__ == "__main__":

    async def run():
        try:
            await main()
        except KeyboardInterrupt:
            print("\nОстановка...")

    asyncio.run(run())
