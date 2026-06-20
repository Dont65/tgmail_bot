import asyncio
import email
import html
import json
import os
import secrets
import shutil
import sqlite3
import string
import time
import urllib.parse
import re
from email.header import decode_header

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web
from aiosmtpd.smtp import SMTP as SMTPServer

# --- НАСТРОЙКИ ---
BOT_TOKEN = "your_bot_token"
DOMAIN = "yourdomain.com"
BOT_PASSWORD = "YOUR_PASSWORD"
PROXY_URL = None

# --- НАСТРОЙКИ ВЕБ-СЕРВЕРА ---
WEB_PORT = 80
WEB_URL_BASE = "http://yourdomain.com" # <-- ВАЖНО: Укажите ваш IP без слеша на конце

dp = Dispatcher()
ATTACHMENTS_DIR = "attachments"

def init_env():
    conn = sqlite3.connect("mailbot.db")
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, auth INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS emails (email TEXT PRIMARY KEY, user_id INTEGER)""")
    c.execute("""CREATE TABLE IF NOT EXISTS messages (token TEXT PRIMARY KEY, html TEXT, expires_at REAL)""")
    conn.commit()
    conn.close()

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

class CreateMail(StatesGroup):
    waiting_for_custom_name = State()

# --- ТЕЛЕГРАМ БОТ ---
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    if is_auth(message.from_user.id):
        await message.answer("Вы уже авторизованы.\n/create - создать почту\n/panel - посмотреть все почты\n/delete - удалить почту")
    else:
        await message.answer(f"Добро пожаловать в бота почты {DOMAIN}!\nВведите пароль:")

@dp.message(F.text == BOT_PASSWORD)
async def auth_password(message: Message):
    if is_auth(message.from_user.id): return
    set_auth(message.from_user.id)
    await message.answer("✅ Пароль принят!\nИспользуйте /create для создания ящика.")

@dp.message(Command("create"))
async def cmd_create(message: Message):
    if not is_auth(message.from_user.id): return await message.answer("Введите пароль.")
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Случайная почта", callback_data="create_random")],
        [InlineKeyboardButton(text="✍️ Написать свой юзернейм", callback_data="create_custom")]
    ])
    await message.answer("Как вы хотите создать почту?", reply_markup=markup)

@dp.callback_query(F.data == "create_random")
async def process_create_random(callback: CallbackQuery):
    prefix = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))
    new_email = f"{prefix}@{DOMAIN}"
    conn = sqlite3.connect("mailbot.db")
    c = conn.cursor()
    c.execute("INSERT INTO emails (email, user_id) VALUES (?, ?)", (new_email, callback.from_user.id))
    conn.commit()
    conn.close()
    await callback.message.edit_text(f"✅ Создана случайная почта:\n<code>{new_email}</code>", parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "create_custom")
async def process_create_custom(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(f"Отправьте мне желаемый логин (только a-z, 0-9 и '_').\nОкончание <code>@{DOMAIN}</code> добавится автоматически.", parse_mode="HTML")
    await state.set_state(CreateMail.waiting_for_custom_name)
    await callback.answer()

@dp.message(CreateMail.waiting_for_custom_name)
async def process_custom_name_input(message: Message, state: FSMContext):
    username = message.text.lower().strip()
    if not re.match(r"^[a-z0-9_]{3,20}$", username):
        return await message.answer("❌ Неверный формат! Используйте от 3 до 20 символов (a-z, 0-9, '_').")

    new_email = f"{username}@{DOMAIN}"
    conn = sqlite3.connect("mailbot.db")
    c = conn.cursor()
    c.execute("SELECT email FROM emails WHERE email = ?", (new_email,))
    if c.fetchone():
        conn.close()
        return await message.answer("❌ Эта почта уже занята. Придумайте другой логин.")
    
    c.execute("INSERT INTO emails (email, user_id) VALUES (?, ?)", (new_email, message.from_user.id))
    conn.commit()
    conn.close()
    await state.clear()
    await message.answer(f"✅ Ваша почта успешно создана:\n<code>{new_email}</code>", parse_mode="HTML")

@dp.message(Command("delete"))
async def cmd_delete(message: Message):
    if not is_auth(message.from_user.id): return await message.answer("Введите пароль.")
    conn = sqlite3.connect("mailbot.db")
    c = conn.cursor()
    c.execute("SELECT email FROM emails WHERE user_id = ?", (message.from_user.id,))
    emails = c.fetchall()
    conn.close()

    if not emails: return await message.answer("У вас нет созданных почт.")

    builder = InlineKeyboardBuilder()
    for e in emails:
        builder.row(InlineKeyboardButton(text=f"🗑 {e[0]}", callback_data=f"del_{e[0]}"))
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="del_cancel"))
    await message.answer("Выберите почту для удаления:", reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("del_"))
async def process_delete_mail(callback: CallbackQuery):
    action = callback.data.replace("del_", "")
    if action == "cancel":
        await callback.message.edit_text("Действие отменено.")
        return await callback.answer()

    conn = sqlite3.connect("mailbot.db")
    c = conn.cursor()
    c.execute("DELETE FROM emails WHERE email = ? AND user_id = ?", (action, callback.from_user.id))
    deleted = c.rowcount
    conn.commit()
    conn.close()

    if deleted:
        await callback.message.edit_text(f"✅ Почта <code>{action}</code> успешно удалена.", parse_mode="HTML")
    else:
        await callback.message.edit_text("❌ Ошибка при удалении.")
    await callback.answer()

@dp.message(Command("panel"))
async def cmd_panel(message: Message):
    if not is_auth(message.from_user.id): return await message.answer("Введите пароль.")
    conn = sqlite3.connect("mailbot.db")
    c = conn.cursor()
    c.execute("SELECT email FROM emails WHERE user_id = ?", (message.from_user.id,))
    emails = c.fetchall()
    conn.close()

    if not emails: return await message.answer("В базе пока нет созданных вами почт.")
    text = "📋 <b>Ваши почты:</b>\n\n"
    for e in emails: text += f"- <code>{e[0]}</code>\n"
    await message.answer(text, parse_mode="HTML")

# --- ПОЧТОВЫЙ СЕРВЕР (SMTP) ---
def decode_mime_words(s):
    if not s: return "Нет данных"
    out = ""
    try:
        for word, charset in decode_header(s):
            if isinstance(word, bytes): out += word.decode(charset or "utf-8", errors="ignore")
            else: out += word
    except: return str(s)
    return out

class MailHandler:
    def __init__(self, bot: Bot):
        self.bot = bot

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        conn = sqlite3.connect("mailbot.db")
        c = conn.cursor()
        c.execute("SELECT 1 FROM emails WHERE email = ?", (address,))
        exists = c.fetchone()
        conn.close()
        if not exists:
            print(f"[{time.strftime('%X')}] ❌ ОТКЛОНЕНО (Нет в БД): {address}")
            return '550 5.1.1 User unknown'
        envelope.rcpt_tos.append(address)
        return '250 OK'

    async def handle_DATA(self, server, session, envelope):
        rcpt_tos = envelope.rcpt_tos
        msg = email.message_from_bytes(envelope.content)

        subject = decode_mime_words(msg.get("Subject"))
        sender = decode_mime_words(msg.get("From"))
        plain_text, html_content = "", ""
        attachments = []

        token = secrets.token_urlsafe(16)
        msg_dir = os.path.join(ATTACHMENTS_DIR, token)

        for part in msg.walk():
            if part.get_content_maintype() == "multipart": continue
            content_type = part.get_content_type()
            payload = part.get_payload(decode=True)
            if not payload: continue

            filename = part.get_filename()
            if filename: filename = decode_mime_words(filename)

            if content_type == "text/plain" and not filename:
                if not plain_text: plain_text = payload.decode(errors="ignore")
            elif content_type == "text/html" and not filename:
                if not html_content: html_content = payload.decode(errors="ignore")
            elif filename or "name=" in part.get("Content-Type", ""):
                if not filename: filename = f"file_{secrets.randbelow(9999)}.bin"
                safe_filename = os.path.basename(filename)
                os.makedirs(msg_dir, exist_ok=True)
                filepath = os.path.join(msg_dir, safe_filename)
                with open(filepath, "wb") as f: f.write(payload)
                attachments.append({"name": safe_filename})

        if html_content: body_to_render = html_content
        elif plain_text: body_to_render = f'<div style="white-space: pre-wrap; font-family: sans-serif;">{html.escape(plain_text)}</div>'
        else: body_to_render = "<p>[Текст письма отсутствует]</p>"

        # --- ГЕНЕРИРУЕМ JSON ДЛЯ FRONTEND ---
        email_data = []
        email_data.append({
            "head": html.escape(subject),
            "subhead": f"От: {html.escape(sender)}<br>Кому: {html.escape(', '.join(rcpt_tos))}"
        })
        email_data.append({
            "title": "Содержимое письма",
            "text": body_to_render
        })
        for att in attachments:
            safe_name = html.escape(att["name"])
            url_name = urllib.parse.quote(att["name"])
            email_data.append({
                "title": "Вложение",
                "file": safe_name,
                "download": f"/download/{token}/{url_name}"
            })

        json_string = json.dumps(email_data, ensure_ascii=False)
        expires_at = time.time() + 3600

        conn = sqlite3.connect("mailbot.db")
        c = conn.cursor()
        c.execute("INSERT INTO messages (token, html, expires_at) VALUES (?, ?, ?)", (token, json_string, expires_at))
        conn.commit()

        email_link = f"{WEB_URL_BASE}/mail?token={token}"
        if not plain_text: plain_text = "[HTML-письмо. Откройте по ссылке]"

        for rcpt in rcpt_tos:
            c.execute("SELECT user_id FROM emails WHERE email = ?", (rcpt,))
            row = c.fetchone()
            if row:
                user_id = row[0]
                att_text = f"\n📎 <b>Вложений:</b> {len(attachments)} шт." if attachments else ""
                text_for_tg = (
                    f"📧 <b>Новое письмо!</b>\n\n"
                    f"📥 <b>Кому:</b> <code>{html.escape(rcpt)}</code>\n"
                    f"📤 <b>От:</b> <code>{html.escape(sender)}</code>\n"
                    f"📝 <b>Тема:</b> {html.escape(subject)}{att_text}\n\n"
                    f"<b>Превью:</b>\n{html.escape(plain_text[:800])}...\n\n"
                    f"<i>⏳ Ссылка активна 1 час.</i>"
                )
                markup = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="📄 Открыть письмо", url=email_link)]
                ])
                try: await self.bot.send_message(user_id, text_for_tg, parse_mode="HTML", reply_markup=markup)
                except: pass

        conn.close()
        print(f"[{time.strftime('%X')}] ✅ Успешно обработано: {subject}")
        return "250 Message accepted for delivery"

# --- ВЕБ-СЕРВЕР И API ---
async def handle_static_page(request):
    if not os.path.exists("index.html"):
        return web.Response(text="Файл index.html не найден на сервере!", status=404)
    return web.FileResponse("index.html")

def fetch_mail_sync(token):
    conn = sqlite3.connect("mailbot.db")
    c = conn.cursor()
    c.execute("SELECT html, expires_at FROM messages WHERE token = ?", (token,))
    row = c.fetchone()
    if row and time.time() > row[1]:
        c.execute("DELETE FROM messages WHERE token = ?", (token,))
        conn.commit()
        row = None
    conn.close()
    return row

async def handle_api_mail(request):
    token = request.match_info.get("token")
    row = await asyncio.to_thread(fetch_mail_sync, token)

    if not row:
        shutil.rmtree(os.path.join(ATTACHMENTS_DIR, token), ignore_errors=True)
        return web.json_response({"error": "Письмо не найдено или удалено"}, status=404)

    json_data = row[0]
    return web.Response(text=json_data, content_type="application/json")

async def handle_download(request):
    token = request.match_info.get("token")
    filename = os.path.basename(urllib.parse.unquote(request.match_info.get("filename")))
    filepath = os.path.join(ATTACHMENTS_DIR, token, filename)
    if os.path.exists(filepath):
        return web.FileResponse(filepath)
    return web.Response(status=404, text="Файл не найден.")

async def cleanup_storage():
    while True:
        now = time.time()
        try:
            conn = sqlite3.connect("mailbot.db")
            c = conn.cursor()
            c.execute("SELECT token FROM messages WHERE expires_at < ?", (now,))
            for (token,) in c.fetchall():
                shutil.rmtree(os.path.join(ATTACHMENTS_DIR, token), ignore_errors=True)
                c.execute("DELETE FROM messages WHERE token = ?", (token,))
            conn.commit()
            conn.close()
        except: pass
        await asyncio.sleep(60)

async def main():
    init_env()
    session = AiohttpSession(proxy=PROXY_URL, timeout=300.0)
    bot = Bot(token=BOT_TOKEN, session=session)
    asyncio.create_task(cleanup_storage())

    app = web.Application()
    app.add_routes([
        web.get("/mail", handle_static_page),                # Отдача HTML-интерфейса
        web.get("/api/mail/{token}", handle_api_mail),       # API (Выдает JSON)
        web.get("/download/{token}/{filename}", handle_download), # Скачивание файлов
    ])
    runner = web.AppRunner(app)
    await runner.setup()

    try:
        site = web.TCPSite(runner, "0.0.0.0", WEB_PORT)
        await site.start()
        print(f"🌐 Веб-сервер запущен на порту {WEB_PORT}")
    except OSError:
        print(f"❌ ОШИБКА: Порт {WEB_PORT} занят.")
        return

    handler = MailHandler(bot)
    loop = asyncio.get_running_loop()
    await loop.create_server(lambda: SMTPServer(handler), host="0.0.0.0", port=25)
    print("📧 SMTP Сервер запущен на 25 порту.")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
