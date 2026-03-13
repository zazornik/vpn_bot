import logging
import sqlite3
import uuid
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, PreCheckoutQueryHandler
)
import os
import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_IDS = [int(id) for id in os.getenv('ADMIN_IDS', '').split(',') if id]
CRYPTOBOT_API_KEY = os.getenv('CRYPTOBOT_API_KEY')
STARS_PRICE = int(os.getenv('STARS_PRICE', '50'))

# ========== НАСТРОЙКИ ТАРИФОВ ==========
TARIFFS = {
    'week': {'name': '7 дней', 'stars': 15, 'days': 7},
    'month': {'name': '30 дней', 'stars': 50, 'days': 30},
    'quarter': {'name': '3 месяца', 'stars': 125, 'days': 90},
    'year': {'name': '12 месяцев', 'stars': 400, 'days': 365}
}

# ========== КЛАСС ДЛЯ РАБОТЫ С 3X-UI (закомментировано, раскомментируй позже) ==========
"""
class XUIClient:
    def __init__(self, panel_url, username, password):
        self.panel_url = panel_url
        self.session = requests.Session()
        self.login(username, password)

    def login(self, username, password):
        login_data = {'username': username, 'password': password}
        response = self.session.post(f"{self.panel_url}/login", data=login_data)
        return response.status_code == 200

    def create_client(self, inbound_id, email, days):
        expiry_time = int((datetime.now() + timedelta(days=days)).timestamp() * 1000)
        client_data = {
            "id": inbound_id,
            "settings": json.dumps({
                "clients": [{
                    "id": str(uuid.uuid4()),
                    "email": email,
                    "limitIp": 1,
                    "totalGB": 0,
                    "expiryTime": expiry_time,
                    "enable": True,
                    "tgId": "",
                    "subId": ""
                }]
            })
        }
        response = self.session.post(f"{self.panel_url}/panel/api/inbounds/addClient", json=client_data)
        return response.json()
"""

# ========== БАЗА ДАННЫХ ==========
class Database:
    def __init__(self):
        self.conn = sqlite3.connect('vpn_bot.db', check_same_thread=False)
        self.create_tables()

    def create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                subscription_end DATE,
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS vpn_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_text TEXT UNIQUE,
                protocol TEXT,
                server_location TEXT,
                is_used BOOLEAN DEFAULT 0,
                is_active BOOLEAN DEFAULT 1,
                used_by INTEGER,
                FOREIGN KEY (used_by) REFERENCES users (user_id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                key_id INTEGER,
                tariff TEXT,
                end_date DATE,
                payment_id TEXT,
                is_active BOOLEAN DEFAULT 1,
                FOREIGN KEY (user_id) REFERENCES users (user_id),
                FOREIGN KEY (key_id) REFERENCES vpn_keys (id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount_stars INTEGER,
                tariff TEXT,
                payment_id TEXT,
                invoice_payload TEXT UNIQUE,
                status TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        self.conn.commit()

    def add_user(self, user_id, username):
        cursor = self.conn.cursor()
        cursor.execute('INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)', (user_id, username))
        self.conn.commit()

    def add_vpn_key(self, key_text, protocol, server_location):
        try:
            cursor = self.conn.cursor()
            cursor.execute('INSERT INTO vpn_keys (key_text, protocol, server_location) VALUES (?, ?, ?)',
                          (key_text, protocol, server_location))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_free_key(self):
        cursor = self.conn.cursor()
        cursor.execute('SELECT id, key_text, protocol, server_location FROM vpn_keys WHERE is_used = 0 AND is_active = 1 LIMIT 1')
        return cursor.fetchone()

    def assign_key_to_user(self, key_id, user_id, tariff, payment_id):
        cursor = self.conn.cursor()
        days = TARIFFS[tariff]['days']
        end_date = datetime.now().date() + timedelta(days=days)
        cursor.execute('UPDATE vpn_keys SET is_used = 1, used_by = ? WHERE id = ?', (user_id, key_id))
        cursor.execute('INSERT INTO subscriptions (user_id, key_id, tariff, end_date, payment_id, is_active) VALUES (?, ?, ?, ?, ?, 1)',
                      (user_id, key_id, tariff, end_date, payment_id))
        cursor.execute('UPDATE users SET subscription_end = ?, is_active = 1 WHERE user_id = ?', (end_date, user_id))
        self.conn.commit()
        return end_date

    def check_subscription(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute('SELECT subscription_end, is_active FROM users WHERE user_id = ?', (user_id,))
        result = cursor.fetchone()
        if not result or not result[0]:
            return False, None, False
        end_date = datetime.strptime(result[0], '%Y-%m-%d').date()
        is_active = bool(result[1])
        return end_date >= datetime.now().date() and is_active, end_date, is_active

    def get_user_key(self, user_id):
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT v.key_text, v.protocol, v.server_location, s.end_date, s.tariff
            FROM subscriptions s
            JOIN vpn_keys v ON s.key_id = v.id
            WHERE s.user_id = ? AND s.is_active = 1
            ORDER BY s.end_date DESC LIMIT 1
        ''', (user_id,))
        return cursor.fetchone()

    def create_payment(self, user_id, amount_stars, tariff):
        cursor = self.conn.cursor()
        invoice_payload = f"vpn_sub_{user_id}_{tariff}_{uuid.uuid4().hex[:8]}"
        cursor.execute('INSERT INTO payments (user_id, amount_stars, tariff, invoice_payload, status) VALUES (?, ?, ?, ?, ?)',
                      (user_id, amount_stars, tariff, invoice_payload, 'pending'))
        self.conn.commit()
        return invoice_payload

    def update_payment_status(self, invoice_payload, status, payment_id=None):
        cursor = self.conn.cursor()
        if payment_id:
            cursor.execute('UPDATE payments SET status = ?, payment_id = ? WHERE invoice_payload = ?',
                          (status, payment_id, invoice_payload))
        else:
            cursor.execute('UPDATE payments SET status = ? WHERE invoice_payload = ?', (status, invoice_payload))
        self.conn.commit()

    def get_payment(self, invoice_payload):
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM payments WHERE invoice_payload = ?', (invoice_payload,))
        return cursor.fetchone()

    def get_all_keys(self):
        cursor = self.conn.cursor()
        cursor.execute('SELECT * FROM vpn_keys')
        return cursor.fetchall()

db = Database()

# ========== КЛАСС ДЛЯ CRYPTOBOT ==========
class CryptoBotAPI:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://pay.crypt.bot/api"
        self.headers = {"Crypto-Pay-API-Token": api_key, "Content-Type": "application/json"}

    def create_invoice(self, amount, user_id, invoice_payload):
        url = f"{self.base_url}/createInvoice"
        unique_payload = f"vpn_{invoice_payload}"
        payload = {
            "asset": "USDT",
            "amount": str(amount),
            "description": f"VPN подписка для пользователя {user_id}",
            "payload": unique_payload,
            "paid_btn_name": "viewItem",
            "paid_btn_url": "https://t.me/your_bot",
            "allow_comments": False,
            "allow_anonymous": False,
            "expires_in": 3600
        }
        try:
            response = requests.post(url, json=payload, headers=self.headers)
            return response.json() if response.status_code == 200 else None
        except Exception as e:
            logger.error(f"CryptoBot request failed: {e}")
            return None

    def get_invoice_status(self, invoice_id):
        url = f"{self.base_url}/getInvoices"
        params = {"invoice_ids": str(invoice_id)}
        try:
            response = requests.get(url, params=params, headers=self.headers)
            if response.status_code == 200:
                data = response.json()
                if data.get('items') and len(data['items']) > 0:
                    return data['items'][0].get('status')
            return None
        except Exception as e:
            logger.error(f"CryptoBot status check failed: {e}")
            return None

crypto_api = CryptoBotAPI(CRYPTOBOT_API_KEY) if CRYPTOBOT_API_KEY else None

# ========== КЛАВИАТУРА ==========
def get_main_keyboard():
    keyboard = [
        [KeyboardButton("💰 Тарифы"), KeyboardButton("🔑 Мой ключ")],
        [KeyboardButton("📊 Статус"), KeyboardButton("📱 Инструкция")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ========== СТАРТ ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.add_user(user.id, user.username)
    has_sub, end_date, is_active = db.check_subscription(user.id)
    if has_sub and is_active:
        days_left = (end_date - datetime.now().date()).days
        status_text = f"✅ **У тебя активна подписка!**\n📅 Действует до: **{end_date}**\n⏳ Осталось дней: **{days_left}**"
    else:
        status_text = f"❌ **У тебя нет активной подписки**"
    await update.message.reply_text(
        f"👋 Привет, {user.first_name}!\n\n{status_text}",
        parse_mode='Markdown',
        reply_markup=get_main_keyboard()
    )

# ========== КНОПКИ ==========
async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "💰 Тарифы":
        await show_tariffs(update, context)
    elif text == "🔑 Мой ключ":
        await mykey(update, context)
    elif text == "📊 Статус":
        await status(update, context)
    elif text == "📱 Инструкция":
        await show_instruction(update, context)
    else:
        await update.message.reply_text(
            "🤔 Я тебя не совсем понял...\nИспользуй кнопки внизу 👇",
            reply_markup=get_main_keyboard()
        )

# ========== ТАРИФЫ ==========
async def show_tariffs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []
    for tariff_id, tariff in TARIFFS.items():
        keyboard.append([InlineKeyboardButton(
            f"{tariff['name']} — {tariff['stars']} ⭐",
            callback_data=f"tariff_{tariff_id}"
        )])
    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")])
    text = "💰 **Наши тарифы**\n\nВыбери подходящий вариант:"
    for t in TARIFFS.values():
        text += f"\n• {t['name']} — {t['stars']} ⭐"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def tariff_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tariff_id = query.data.split('_')[1]
    tariff = TARIFFS[tariff_id]
    context.user_data['selected_tariff'] = tariff_id
    if not db.get_free_key():
        await query.edit_message_text(
            "😓 Ключи временно закончились.\nПопробуй позже.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="show_tariffs")]])
        )
        return
    keyboard = [[InlineKeyboardButton("⭐ Оплатить Stars", callback_data=f"pay_stars_{tariff_id}")]]
    if crypto_api:
        keyboard.append([InlineKeyboardButton("💎 CryptoBot (USDT)", callback_data=f"pay_crypto_{tariff_id}")])
    keyboard.append([InlineKeyboardButton("🔙 К тарифам", callback_data="show_tariffs")])
    await query.edit_message_text(
        f"💳 **Оплата: {tariff['name']}**\n\nСумма: {tariff['stars']} ⭐\n\nВыбери способ оплаты:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ========== ОПЛАТА STARS ==========
async def pay_with_stars(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tariff_id = query.data.split('_')[2]
    tariff = TARIFFS[tariff_id]
    user_id = query.from_user.id
    payload = db.create_payment(user_id, tariff['stars'], tariff_id)
    prices = [LabeledPrice(f"VPN {tariff['name']}", tariff['stars'])]
    await context.bot.send_invoice(
        chat_id=user_id,
        title=f"VPN {tariff['name']}",
        description=f"Подписка на {tariff['name'].lower()}",
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=prices,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Оплатить", pay=True)]])
    )

async def pre_checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user_id = update.effective_user.id
    payload = msg.successful_payment.invoice_payload
    payment_id = msg.successful_payment.telegram_payment_charge_id
    parts = payload.split('_')
    tariff_id = parts[3] if len(parts) > 3 else 'month'
    tariff = TARIFFS.get(tariff_id, TARIFFS['month'])
    db.update_payment_status(payload, 'completed', payment_id)
    key = db.get_free_key()
    if not key:
        await msg.reply_text("❌ Ключи закончились. Администратор вернет деньги.")
        return
    end_date = db.assign_key_to_user(key[0], user_id, tariff_id, payment_id)
    await msg.reply_text(
        f"✅ **Оплата прошла успешно!**\n\n"
        f"📅 Тариф: **{tariff['name']}**\n"
        f"📅 Подписка до: **{end_date}**\n"
        f"🌍 Сервер: **{key[2]}**\n"
        f"📡 Протокол: **{key[3]}**\n\n"
        f"🔑 **Твой ключ:**\n"
        f"`{key[1]}`\n\n"
        f"📱 **Как подключить:**\n"
        f"1️⃣ Нажми кнопку «Инструкция» внизу\n"
        f"2️⃣ Выбери своё устройство",
        parse_mode='Markdown',
        reply_markup=get_main_keyboard()
    )

# ========== ИНСТРУКЦИЯ ==========
async def show_instruction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📱 **Гайд по подключению VPN**\n\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "**📱 ДЛЯ ANDROID**\n"
        "• [HAPP (Google Play)](https://play.google.com/store/apps/details?id=com.hekeki.happ)\n"
        "• [v2rayNG](https://play.google.com/store/apps/details?id=com.v2ray.ang)\n\n"
        "**🍎 ДЛЯ iOS**\n"
        "• [V2Box](https://apps.apple.com/app/v2box-v2ray-client/id6446018936)\n"
        "• [Shadowrocket](https://apps.apple.com/app/shadowrocket/id932747118)\n\n"
        "**💻 ДЛЯ WINDOWS**\n"
        "• [v2rayN](https://github.com/2dust/v2rayN/releases)\n"
        "• [Nekoray](https://github.com/MatsuriDayo/nekoray/releases)\n\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "**🔧 КАК ПОДКЛЮЧИТЬ HAPP (Android)**\n"
        "1️⃣ Скачай HAPP по ссылке\n"
        "2️⃣ Открой → нажми **+**\n"
        "3️⃣ Выбери **«Импорт из буфера обмена»**\n"
        "4️⃣ Вставь ключ → **«Подключиться»**\n\n"
        "❓ **Если не работает:** перезагрузи приложение, проверь ключ"
    )
    keyboard = [
        [InlineKeyboardButton("📱 HAPP", url="https://play.google.com/store/apps/details?id=com.hekeki.happ")],
        [InlineKeyboardButton("🍎 V2Box", url="https://apps.apple.com/app/v2box-v2ray-client/id6446018936")],
        [InlineKeyboardButton("💻 v2rayN", url="https://github.com/2dust/v2rayN/releases")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_menu")]
    ]
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode='Markdown', disable_web_page_preview=True)
        await update.callback_query.message.reply_text("📥 **Скачать приложения:**", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, parse_mode='Markdown', disable_web_page_preview=True)
        await update.message.reply_text("📥 **Скачать приложения:**", reply_markup=InlineKeyboardMarkup(keyboard))

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    has_sub, end_date, is_active = db.check_subscription(query.from_user.id)
    status_text = f"✅ Подписка активна, осталось {(end_date - datetime.now().date()).days} дн." if has_sub else "❌ Подписки нет"
    await query.edit_message_text(f"🏠 **Главное меню**\n\n{status_text}", parse_mode='Markdown')
    await query.message.reply_text("Что будем делать?", reply_markup=get_main_keyboard())

# ========== МОЙ КЛЮЧ ==========
async def mykey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    has_sub, end_date, is_active = db.check_subscription(user_id)
    if not has_sub or not is_active:
        await update.message.reply_text(
            "❌ **Нет активной подписки**\nНажми «💰 Тарифы» чтобы выбрать",
            reply_markup=get_main_keyboard(), parse_mode='Markdown'
        )
        return
    key = db.get_user_key(user_id)
    if key:
        key_text, protocol, location, end_date, tariff = key
        await update.message.reply_text(
            f"🔑 **Твой VPN ключ**\n\n"
            f"📅 Тариф: **{TARIFFS.get(tariff, {}).get('name', tariff)}**\n"
            f"📅 Действует до: **{end_date}**\n"
            f"🌍 Сервер: **{location}**\n"
            f"📡 Протокол: **{protocol}**\n\n"
            f"`{key_text}`",
            parse_mode='Markdown', reply_markup=get_main_keyboard()
        )
    else:
        await update.message.reply_text("❌ Ключ не найден", reply_markup=get_main_keyboard())

# ========== СТАТУС ==========
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    has_sub, end_date, is_active = db.check_subscription(user_id)
    if has_sub and is_active:
        days_left = (end_date - datetime.now().date()).days
        await update.message.reply_text(
            f"✅ **Подписка активна**\n\n📅 До: **{end_date}**\n⏳ Осталось дней: **{days_left}**",
            parse_mode='Markdown', reply_markup=get_main_keyboard()
        )
    else:
        await update.message.reply_text(
            "❌ **Нет активной подписки**\nНажми «💰 Тарифы»",
            reply_markup=get_main_keyboard(), parse_mode='Markdown'
        )

# ========== АДМИНКА ==========
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Доступ запрещен")
        return
    keyboard = [
        [InlineKeyboardButton("➕ Добавить ключи", callback_data="admin_add")],
        [InlineKeyboardButton("📋 Список ключей", callback_data="admin_list")],
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")]
    ]
    await update.message.reply_text("🔐 **Панель администратора**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if update.effective_user.id not in ADMIN_IDS:
        await query.edit_message_text("⛔ Доступ запрещен")
        return
    if query.data == "admin_add":
        await query.edit_message_text(
            "📝 **Отправь ключи** в формате:\n\n`ключ | протокол | сервер`\n\nПример:\n`vless://abc123 | vless | Netherlands`"
        )
        context.user_data['adding_keys'] = True
    elif query.data == "admin_list":
        keys = db.get_all_keys()
        if keys:
            text = "📋 **Список ключей:**\n\n"
            for k in keys:
                status = "✅" if k[4] else "🆓"
                used_by = f" (ID: {k[6]})" if k[6] else ""
                text += f"{status} {k[2]}|{k[3]}: {k[1][:30]}...{used_by}\n"
            await query.edit_message_text(text[:4000], parse_mode='Markdown')
        else:
            await query.edit_message_text("📭 **Ключей нет**")
    elif query.data == "admin_stats":
        cursor = db.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM users WHERE is_active = 1")
        active_users = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM vpn_keys")
        total_keys = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM vpn_keys WHERE is_used = 1")
        used_keys = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM subscriptions WHERE is_active = 1")
        active_subs = cursor.fetchone()[0]
        stats = (
            f"📊 **Статистика:**\n\n"
            f"👥 Всего пользователей: **{total_users}**\n"
            f"✅ Активных: **{active_users}**\n"
            f"🔑 Всего ключей: **{total_keys}**\n"
            f"✅ Использовано: **{used_keys}**\n"
            f"📅 Активных подписок: **{active_subs}**"
        )
        await query.edit_message_text(stats, parse_mode='Markdown')
    elif query.data.startswith("tariff_"):
        await tariff_selected(update, context)
    elif query.data == "show_tariffs":
        await show_tariffs(update, context)
    elif query.data == "back_to_menu":
        await back_to_menu(update, context)

async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('adding_keys'):
        added, failed = 0, 0
        for line in update.message.text.strip().split('\n'):
            parts = [p.strip() for p in line.split('|')]
            if len(parts) == 3 and db.add_vpn_key(parts[0], parts[1], parts[2]):
                added += 1
            else:
                failed += 1
        context.user_data['adding_keys'] = False
        await update.message.reply_text(f"✅ **Добавлено:** {added}\n❌ **Ошибок:** {failed}", parse_mode='Markdown')

# ========== ОСНОВНОЙ ЗАПУСК ==========
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mykey", mykey))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_buttons))
    app.add_handler(CallbackQueryHandler(pay_with_stars, pattern="^pay_stars_"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^tariff_"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^show_tariffs$"))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern="^back_to_menu$"))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))
    print("🚀 Бот с тарифами и инструкциями запущен!")
    app.run_polling()

if __name__ == '__main__':
    main()
