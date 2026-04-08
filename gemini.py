import asyncio
import sqlite3
import os
import time
import logging
import sys
import io
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import LabeledPrice, PreCheckoutQuery, Message, InputMediaPhoto, InlineKeyboardButton
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from telethon import TelegramClient, functions
from telethon.errors import SessionPasswordNeededError

# --- ИМПОРТ CRYPTOPAY ---
try:
    from aiocryptopay import AioCryptoPay, Networks
except ImportError:
    AioCryptoPay = None

# --- КОНФИГУРАЦИЯ ---
API_TOKEN = '8604631055:AAHOgN_OukMzDoWkpWtiT-O9ZUKqpp2Tqb4'
API_ID = 20652575
API_HASH = 'c0d5c94ec3c668444dca9525940d876d'
ADMIN_ID = 7785932103
CRYPTO_PAY_TOKEN = '540011:AARTDw8jiNvxfbJNrCKkEp4l6l50XTuJOYX'
SUPPORT_URL = "https://t.me/Dutsi18"
STAR_RATE = 0.02
WELCOME_BONUS = 0.1
MIN_RENT_TIME = 10

# ССЫЛКИ НА КАРТИНКИ
IMG_MAIN = "https://ibb.co/d4zm29x6"
IMG_CATALOG = "https://ibb.co/HTm1Cv56"
IMG_BALANCE = "https://ibb.co/WNy38dr2"
IMG_MY_RENT = "https://ibb.co/tTSMycBT"

logging.basicConfig(level=logging.INFO, stream=sys.stdout, format='%(levelname)s:%(name)s:%(message)s')
bot = Bot(token=API_TOKEN)
dp = Dispatcher()

crypto = None
if AioCryptoPay:
    crypto = AioCryptoPay(token=CRYPTO_PAY_TOKEN, network=Networks.MAIN_NET)

# --- БАЗА ДАННЫХ ---
db = sqlite3.connect('bot_data.db', check_same_thread=False)
cur = db.cursor()


def init_db():
    cur.execute('''CREATE TABLE IF NOT EXISTS accounts 
                   (phone TEXT PRIMARY KEY, owner_id INTEGER, expires INTEGER, 
                    text TEXT DEFAULT 'Привет!', photo_id TEXT, 
                    interval INTEGER DEFAULT 30, chats TEXT DEFAULT '',
                    is_running INTEGER DEFAULT 0, price_per_min REAL DEFAULT 0.10)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS users 
                   (user_id INTEGER PRIMARY KEY, balance REAL DEFAULT 0.0)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS payments 
                   (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL, method TEXT, date TEXT)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS rent_history 
                   (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, phone TEXT, duration INTEGER, cost REAL, date TEXT)''')

    try:
        cur.execute('ALTER TABLE accounts ADD COLUMN is_premium INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute('ALTER TABLE accounts ADD COLUMN notified_10m INTEGER DEFAULT 0')
    except sqlite3.OperationalError:
        pass

    # Таблица для стоп-слов
    cur.execute('''CREATE TABLE IF NOT EXISTS blacklist (word TEXT PRIMARY KEY)''')
    default_words = ['темка', 'чернуха', 'скам', '$кам']
    for w in default_words:
        cur.execute('INSERT OR IGNORE INTO blacklist (word) VALUES (?)', (w,))

    db.commit()


init_db()


class States(StatesGroup):
    waiting_for_phone = State()
    waiting_for_code = State()
    waiting_for_password = State()
    waiting_for_tgp = State()
    waiting_for_rent_time = State()
    edit_text = State()
    edit_chats = State()
    edit_photo = State()
    edit_interval = State()
    top_up_amount = State()


# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def get_balance(user_id):
    cur.execute('SELECT balance FROM users WHERE user_id = ?', (user_id,))
    res = cur.fetchone()
    return round(res[0], 2) if res else None


def add_payment_history(user_id, amount, method):
    date = time.strftime('%Y-%m-%d %H:%M:%S')
    cur.execute('INSERT INTO payments (user_id, amount, method, date) VALUES (?, ?, ?, ?)',
                (user_id, amount, method, date))
    cur.execute('UPDATE users SET balance = balance + ? WHERE user_id = ?', (amount, user_id))
    db.commit()


def contains_bad_words(text):
    cur.execute('SELECT word FROM blacklist')
    words = [row[0] for row in cur.fetchall()]
    text_lower = text.lower()
    for w in words:
        if w in text_lower: return w
    return None


def main_menu():
    kb = ReplyKeyboardBuilder()
    kb.button(text="📂 Каталог аккаунтов")
    kb.button(text="🔑 Моя аренда")
    kb.button(text="💰 Баланс")
    kb.button(text="🛒 Shop")
    kb.button(text="👨‍💻 Support")
    kb.adjust(2, 2, 1)
    return kb.as_markup(resize_keyboard=True)


def back_kb(to="to_main"):
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ Назад", callback_data=to)
    kb.adjust(1)
    return kb


def extract_chat_and_topic(chat_str):
    chat_str = chat_str.strip()
    if "t.me/" in chat_str: chat_str = chat_str.split("t.me/")[1]
    if "/" in chat_str:
        parts = chat_str.split("/")
        if len(parts) == 2 and parts[1].isdigit():
            return parts[0], int(parts[1])
        elif len(parts) == 3 and parts[0] == "c" and parts[2].isdigit():
            return int("-100" + parts[1]), int(parts[2])
    try:
        return int(chat_str), None
    except ValueError:
        return chat_str, None


# --- ФОНОВАЯ ЗАДАЧА УВЕДОМЛЕНИЙ ---
async def check_expirations():
    while True:
        now = int(time.time())
        # Проверка: осталось 10 минут
        cur.execute(
            'SELECT phone, owner_id FROM accounts WHERE owner_id IS NOT NULL AND expires > 0 AND expires - ? <= 600 AND notified_10m = 0',
            (now,))
        for phone, owner_id in cur.fetchall():
            try:
                await bot.send_message(owner_id,
                                       f"⚠️ **Внимание!** До конца аренды аккаунта `{phone}` осталось менее 10 минут.",
                                       parse_mode="Markdown")
            except:
                pass
            cur.execute('UPDATE accounts SET notified_10m = 1 WHERE phone = ?', (phone,))
        db.commit()

        # Проверка: аренда закончилась
        cur.execute('SELECT phone, owner_id FROM accounts WHERE owner_id IS NOT NULL AND expires > 0 AND expires <= ?',
                    (now,))
        for phone, owner_id in cur.fetchall():
            try:
                await bot.send_message(owner_id,
                                       f"🛑 Время аренды аккаунта `{phone}` подошло к концу. Сессия остановлена, аккаунт возвращен в каталог.",
                                       parse_mode="Markdown")
            except:
                pass
            cur.execute(
                'UPDATE accounts SET owner_id = NULL, expires = 0, is_running = 0, notified_10m = 0 WHERE phone = ?',
                (phone,))
        db.commit()
        await asyncio.sleep(60)


# --- БАЗОВЫЕ КОМАНДЫ ПОМОЩИ ---
@dp.message(Command("help"))
async def cmd_help(message: Message):
    text = ("🤖 **Добро пожаловать в сервис аренды Telegram-аккаунтов!**\n\n"
            "Здесь вы можете:\n"
            "• 📂 В **Каталоге аккаунтов** выбрать и арендовать номер для работы.\n"
            "• 🔑 В разделе **Моя аренда** управлять аккаунтом: менять текст, фото, чаты рассылки, интервалы и запускать/останавливать работу.\n"
            "• 🛒 В **Shop** приобрести дополнительные товары.\n\n"
            "Бот автоматически делает рассылку ваших сообщений по заданным чатам (группам или топикам). "
            "Пожалуйста, соблюдайте правила сервиса: некоторые слова запрещены к использованию в рассылке.")
    await message.answer(text, parse_mode="Markdown")


@dp.message(Command("ahelp"))
async def cmd_ahelp(message: Message):
    if message.from_user.id != ADMIN_ID: return
    text = ("🛠 **Команды администратора:**\n"
            "`/addacc` - Добавить новый аккаунт в базу\n"
            "`/delacc +7999...` - Удалить аккаунт\n"
            "`/stats ID` - Полная статистика пользователя\n"
            "`/givebal ID СУММА` - Выдать баланс (USD)\n"
            "`/delbal ID СУММА` - Списать баланс (USD)\n"
            "`/setprice +7999... 0.15` - Установить цену за 1 мин аренды\n"
            "`/blacklist слово` - Добавить слово в стоп-лист")
    await message.answer(text, parse_mode="Markdown")


# --- АДМИН КОМАНДЫ ---
@dp.message(Command("blacklist"))
async def adm_blacklist(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: return await message.answer("⚠️ Формат: `/blacklist слово`", parse_mode="Markdown")
    word = command.args.strip().lower()
    try:
        cur.execute('INSERT INTO blacklist (word) VALUES (?)', (word,))
        db.commit()
        await message.answer(f"✅ Слово `{word}` успешно добавлено в стоп-лист рассылки.", parse_mode="Markdown")
    except sqlite3.IntegrityError:
        await message.answer(f"⚠️ Слово `{word}` уже присутствует в стоп-листе.", parse_mode="Markdown")


@dp.message(Command("stats"))
async def adm_stats(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: return await message.answer("⚠️ Формат: `/stats ID`", parse_mode="Markdown")
    try:
        uid = int(command.args.strip())
        bal = get_balance(uid)
        if bal is None: return await message.answer("❌ Пользователь не найден.")

        cur.execute('SELECT phone, expires FROM accounts WHERE owner_id = ? AND expires > ?', (uid, int(time.time())))
        active_rents = cur.fetchall()
        active_list = "\n".join([f"• `{r[0]}` (до {time.strftime('%H:%M %d.%m', time.localtime(r[1]))})" for r in
                                 active_rents]) or "Нет активных"

        cur.execute('SELECT phone, duration, cost, date FROM rent_history WHERE user_id = ? ORDER BY id DESC LIMIT 5',
                    (uid,))
        h_rents = cur.fetchall()
        history_rent_list = "\n".join(
            [f"• `{h[0]}` | {h[1]} мин | ${h[2]} ({h[3]})" for h in h_rents]) or "История пуста"

        cur.execute('SELECT amount, method, date FROM payments WHERE user_id = ? ORDER BY id DESC LIMIT 5', (uid,))
        h_pays = cur.fetchall()
        history_pay_list = "\n".join([f"• +${p[0]} ({p[1]}) - {p[2]}" for p in h_pays]) or "Пополнений нет"

        report = (f"👤 **Статистика пользователя `{uid}`**\n\n💳 **Баланс:** `${bal}`\n\n"
                  f"🔑 **Активная аренда:**\n{active_list}\n\n"
                  f"📜 **Последние аренды:**\n{history_rent_list}\n\n"
                  f"📊 **Последние пополнения:**\n{history_pay_list}")
        await message.answer(report, parse_mode="Markdown")
    except:
        await message.answer("❌ Ошибка в ID.")


@dp.message(Command("givebal"))
async def adm_give(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    try:
        uid, amt = command.args.split()
        amt = float(amt.replace(",", "."))
        add_payment_history(int(uid), amt, "Admin Add")
        await message.answer(f"✅ Зачислено **${amt}** пользователю `{uid}`", parse_mode="Markdown")
    except:
        await message.answer("Ошибка. Формат: `/givebal ID СУММА`")


@dp.message(Command("delbal"))
async def adm_del_bal(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    try:
        uid, amt = command.args.split()
        uid = int(uid)
        amt = float(amt.replace(",", "."))
        cur.execute('UPDATE users SET balance = balance - ? WHERE user_id = ?', (amt, uid))
        db.commit()
        await message.answer(f"✅ Списано **${amt}** у пользователя `{uid}`", parse_mode="Markdown")
    except:
        await message.answer("Ошибка. Формат: `/delbal ID СУММА`")


@dp.message(Command("delacc"))
async def adm_del_acc(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: return await message.answer("⚠️ Формат: `/delacc +79991234567`")
    phone = command.args.strip().replace(" ", "")
    cur.execute('DELETE FROM accounts WHERE phone = ?', (phone,))
    db.commit()
    session_path = f"sessions/{phone}.session"
    if os.path.exists(session_path):
        try:
            os.remove(session_path); await message.answer(f"✅ Аккаунт `{phone}` и файл сессии удалены.")
        except Exception as e:
            await message.answer(f"✅ БД очищена, ошибка удаления файла: {e}")
    else:
        await message.answer(f"✅ Аккаунт `{phone}` удален из базы.")


@dp.message(Command("setprice"))
async def adm_set_price(message: Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    try:
        phone, price = command.args.split()
        price = float(price.replace(",", "."))
        cur.execute('UPDATE accounts SET price_per_min = ? WHERE phone = ?', (price, phone))
        db.commit()
        await message.answer(f"✅ Цена для `{phone}` теперь **${price}/мин**", parse_mode="Markdown")
    except:
        await message.answer("Ошибка. Формат: `/setprice +7... 0.15`")


# --- ОСНОВНЫЕ ОБРАБОТЧИКИ ---

@dp.message(Command("start"))
@dp.callback_query(F.data == "to_main")
async def start_cmd(event: types.Message | types.CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = event.from_user.id
    bonus_text = ""
    if get_balance(user_id) is None:
        cur.execute('INSERT INTO users (user_id, balance) VALUES (?, ?)', (user_id, WELCOME_BONUS))
        db.commit()
        bonus_text = f"\n\n🎁 Вам начислен бонус: **${WELCOME_BONUS}**"

    caption = f"👋 Главное меню. Выберите раздел:{bonus_text}"
    kb = InlineKeyboardBuilder()
    kb.button(text="📂 Каталог", callback_data="catalog_inline")
    kb.adjust(1)

    if isinstance(event, Message):
        await event.answer_photo(photo=IMG_MAIN, caption=caption, reply_markup=main_menu(), parse_mode="Markdown")
    else:
        await event.message.edit_media(media=InputMediaPhoto(media=IMG_MAIN, caption=caption, parse_mode="Markdown"),
                                       reply_markup=kb.as_markup())


@dp.message(F.text == "💰 Баланс")
async def bal_menu(message: Message):
    bal = get_balance(message.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="💎 Stars", callback_data="topup_stars")
    kb.button(text="🔌 CryptoPay", callback_data="topup_crypto")
    kb.button(text="⬅️ Назад", callback_data="to_main")
    await message.answer_photo(photo=IMG_BALANCE, caption=f"💳 Ваш баланс: **${bal}**",
                               reply_markup=kb.adjust(2, 1).as_markup())


@dp.message(F.text == "👨‍💻 Support")
async def support_info(message: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="Написать в Поддержку", url=SUPPORT_URL)
    await message.answer("Связь с администрацией и поддержка:", reply_markup=kb.as_markup())


# --- МАГАЗИН ---
@dp.message(F.text == "🛒 Shop")
@dp.callback_query(F.data == "shop_main")
async def shop_menu(event: Message | types.CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="Код Бота - 20$", callback_data="buy_bot_code")
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="to_main"))
    text = "🛒 **Магазин**\n\nВыберите товар для покупки:"
    if isinstance(event, Message):
        await event.answer(text, reply_markup=kb.as_markup(), parse_mode="Markdown")
    else:
        try:
            await event.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="Markdown")
        except:
            await event.message.edit_caption(caption=text, reply_markup=kb.as_markup(), parse_mode="Markdown")


@dp.callback_query(F.data == "buy_bot_code")
async def buy_bot_code(call: types.CallbackQuery):
    cost = 20.0
    if get_balance(call.from_user.id) < cost:
        return await call.answer("❌ Недостаточно средств на балансе!", show_alert=True)

    cur.execute('UPDATE users SET balance = balance - ? WHERE user_id = ?', (cost, call.from_user.id))
    db.commit()

    # --- ССЫЛКА НА АРХИВ ---
    LINK_TO_ARCHIVE = "https://ВАША_ССЫЛКА_НА_АРХИВ.zip"

    msg = f"✅ **Оплата прошла успешно!**\nС вашего баланса списано ${cost}.\n\n📦 Вы приобрели лот: 'Код Бота'.\n🔗 Ссылка на скачивание архива: {LINK_TO_ARCHIVE}"
    await call.message.edit_text(msg, parse_mode="Markdown")


@dp.message(F.text == "📂 Каталог аккаунтов")
@dp.callback_query(F.data == "catalog_inline")
async def catalog(event: Message | types.CallbackQuery):
    cur.execute('SELECT phone, price_per_min, is_premium FROM accounts WHERE owner_id IS NULL OR expires < ?',
                (int(time.time()),))
    rows = cur.fetchall()
    kb = InlineKeyboardBuilder()
    for r in rows:
        star = "⭐ " if r[2] else ""
        kb.button(text=f"{star}📱 {r[0]} (${r[1]}/мин)", callback_data=f"rent_{r[0]}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="to_main"))
    cap = "🛒 Свободные номера:"
    if isinstance(event, Message):
        await event.answer_photo(photo=IMG_CATALOG, caption=cap, reply_markup=kb.as_markup())
    else:
        await event.message.edit_media(media=InputMediaPhoto(media=IMG_CATALOG, caption=cap),
                                       reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("rent_"))
async def rent_init(call: types.CallbackQuery, state: FSMContext):
    await state.update_data(rent_phone=call.data.split("_")[1])
    await call.message.answer(f"Введите время аренды ({MIN_RENT_TIME} - 600 минут):",
                              reply_markup=back_kb("to_main").as_markup())
    await state.set_state(States.waiting_for_rent_time)


@dp.message(States.waiting_for_rent_time)
async def rent_finish(m: Message, state: FSMContext):
    data = await state.get_data()
    try:
        mins = int(m.text)
        if mins < MIN_RENT_TIME or mins > 600: return await m.answer(f"⚠️ Лимит: от {MIN_RENT_TIME} до 600 минут.")

        cur.execute('SELECT price_per_min FROM accounts WHERE phone = ?', (data['rent_phone'],))
        cost = round(mins * cur.fetchone()[0], 2)
        if get_balance(m.from_user.id) < cost: return await m.answer("❌ Недостаточно средств.")

        cur.execute('UPDATE users SET balance = balance - ? WHERE user_id = ?', (cost, m.from_user.id))
        exp = int(time.time()) + (mins * 60)

        # Обнуляем флаг уведомления 10m при новой аренде
        cur.execute('UPDATE accounts SET owner_id = ?, expires = ?, is_running = 0, notified_10m = 0 WHERE phone = ?',
                    (m.from_user.id, exp, data['rent_phone']))

        cur.execute('INSERT INTO rent_history (user_id, phone, duration, cost, date) VALUES (?, ?, ?, ?, ?)',
                    (m.from_user.id, data['rent_phone'], mins, cost, time.strftime('%Y-%m-%d %H:%M:%S')))

        db.commit()
        await m.answer(f"✅ Аккаунт арендован на {mins} мин!")

        # Уведомление админу
        try:
            await bot.send_message(ADMIN_ID,
                                   f"🔔 **Новая аренда:**\nПользователь `{m.from_user.id}` арендовал `{data['rent_phone']}` на {mins} мин. (Списано ${cost})",
                                   parse_mode="Markdown")
        except:
            pass

        await state.clear()
    except:
        await m.answer("Ошибка ввода.")


@dp.message(F.text == "🔑 Моя аренда")
@dp.callback_query(F.data == "to_my_rents")
async def my_rents(event: Message | types.CallbackQuery):
    uid = event.from_user.id
    cur.execute('SELECT phone, is_premium FROM accounts WHERE owner_id = ? AND expires > ?', (uid, int(time.time())))
    rows = cur.fetchall()
    kb = InlineKeyboardBuilder()
    for r in rows:
        star = "⭐ " if r[1] else ""
        kb.button(text=f"{star}⚙️ {r[0]}", callback_data=f"manage_{r[0]}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="to_main"))
    cap = "🔧 Ваши активные номера:"
    if isinstance(event, Message):
        await event.answer_photo(photo=IMG_MY_RENT, caption=cap, reply_markup=kb.as_markup())
    else:
        await event.message.edit_media(media=InputMediaPhoto(media=IMG_MY_RENT, caption=cap),
                                       reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("manage_"))
async def manage_acc(call: types.CallbackQuery):
    p = call.data.split("_")[1]
    cur.execute('SELECT is_running FROM accounts WHERE phone = ?', (p,))
    res = cur.fetchone()
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Текст", callback_data=f"set_text_{p}")
    kb.button(text="🖼 Фото", callback_data=f"set_photo_{p}")
    kb.button(text="👥 Чаты", callback_data=f"set_chats_{p}")
    kb.button(text="⏳ Сек", callback_data=f"set_int_{p}")
    kb.button(text="🛑 СТОП" if res[0] else "🚀 ПУСК", callback_data=f"{'off' if res[0] else 'on'}_{p}")
    kb.button(text="❌ ОТМЕНИТЬ", callback_data=f"cancel_{p}")
    kb.button(text="⬅️ Назад", callback_data="to_my_rents")
    await call.message.edit_caption(caption=f"📱 `{p}`\nСтатус: {'🔥 РАБОТАЕТ' if res[0] else '💤 ПАУЗА'}",
                                    reply_markup=kb.adjust(2, 2, 2, 1).as_markup(), parse_mode="Markdown")


# --- ОПЛАТА ---
@dp.callback_query(F.data.startswith("topup_"))
async def topup_init(call: types.CallbackQuery, state: FSMContext):
    method = call.data.split("_")[1]
    await state.update_data(method=method)
    if method == 'stars':
        await call.message.answer("Введите количество Stars для покупки:", reply_markup=back_kb().as_markup())
    else:
        await call.message.answer("Введите сумму в USD для пополнения через CryptoPay:",
                                  reply_markup=back_kb().as_markup())
    await state.set_state(States.top_up_amount)


@dp.message(States.top_up_amount)
async def create_pay(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        val = float(message.text.replace(",", "."))
        if val <= 0: raise ValueError
    except:
        return await message.answer("Пожалуйста, введите корректное число.")

    if data['method'] == 'stars':
        stars_count = int(val)
        usd_equivalent = round(stars_count * STAR_RATE, 2)
        await message.answer_invoice(
            title="Пополнение баланса",
            description=f"Покупка {stars_count} Stars (начислим ${usd_equivalent} на баланс)",
            payload=f"paystars_{usd_equivalent}", currency="XTR",
            prices=[LabeledPrice(label="Stars", amount=stars_count)]
        )
    elif crypto:
        inv = await crypto.create_invoice(asset='USDT', amount=val)
        kb = InlineKeyboardBuilder()
        kb.button(text="Оплатить", url=inv.bot_invoice_url)
        kb.button(text="Проверить", callback_data=f"chk_{inv.invoice_id}_{val}")
        await message.answer(f"Счет на ${val} создан:", reply_markup=kb.adjust(1).as_markup())
    await state.clear()


@dp.callback_query(F.data.startswith("chk_"))
async def check_crypto(call: types.CallbackQuery):
    _, iid, amt = call.data.split("_")
    inv = await crypto.get_invoices(invoice_ids=int(iid))
    if inv and inv.status == 'paid':
        add_payment_history(call.from_user.id, float(amt), "CryptoPay")
        await call.message.edit_text("✅ Оплата получена!")
    else:
        await call.answer("Не оплачено", show_alert=True)


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery): await bot.answer_pre_checkout_query(q.id, ok=True)


@dp.message(F.successful_payment)
async def success_pay(m: Message):
    usd_to_add = float(m.successful_payment.invoice_payload.split("_")[1])
    add_payment_history(m.from_user.id, usd_to_add, "Stars")
    await m.answer(f"✅ Успешно! На ваш баланс зачислено ${usd_to_add}")


# --- ТЕЛЕТОН И РАССЫЛКА ---
@dp.message(Command("addacc"))
async def add_acc(m: Message, state: FSMContext):
    if m.from_user.id != ADMIN_ID: return
    await m.answer("Номер:")
    await state.set_state(States.waiting_for_phone)


@dp.message(States.waiting_for_phone)
async def h_phone(m: Message, state: FSMContext):
    p = m.text.strip().replace(" ", "")
    c = TelegramClient(f"sessions/{p}", API_ID, API_HASH)
    await c.connect()
    try:
        req = await c.send_code_request(p)
        await state.update_data(phone=p, hash=req.phone_code_hash)
        await m.answer("Код:")
        await state.set_state(States.waiting_for_code)
    except Exception as e:
        await m.answer(f"Error: {e}");
        await state.clear()
    finally:
        await c.disconnect()


@dp.message(States.waiting_for_code)
async def h_code(m: Message, state: FSMContext):
    d = await state.get_data()
    c = TelegramClient(f"sessions/{d['phone']}", API_ID, API_HASH)
    await c.connect()
    try:
        await c.sign_in(d['phone'], m.text.strip(), phone_code_hash=d['hash'])
        await ask_premium_status(m, state, d['phone'])
    except SessionPasswordNeededError:
        await m.answer("2FA:")
        await state.set_state(States.waiting_for_password)
    except Exception as e:
        await m.answer(f"Error: {e}");
        await state.clear()
    finally:
        await c.disconnect()


@dp.message(States.waiting_for_password)
async def h_2fa(m: Message, state: FSMContext):
    d = await state.get_data()
    c = TelegramClient(f"sessions/{d['phone']}", API_ID, API_HASH)
    await c.connect()
    try:
        await c.sign_in(password=m.text.strip())
        await ask_premium_status(m, state, d['phone'])
    except Exception as e:
        await m.answer(f"Error: {e}");
        await state.clear()
    finally:
        await c.disconnect()


async def ask_premium_status(m: Message, state: FSMContext, phone: str):
    await state.update_data(phone=phone)
    kb = InlineKeyboardBuilder()
    kb.button(text="Да ⭐", callback_data="tgp_yes")
    kb.button(text="Нет", callback_data="tgp_no")
    await m.answer("На этом аккаунте есть Telegram Premium?", reply_markup=kb.adjust(2).as_markup())
    await state.set_state(States.waiting_for_tgp)


@dp.callback_query(States.waiting_for_tgp, F.data.in_(["tgp_yes", "tgp_no"]))
async def process_tgp(call: types.CallbackQuery, state: FSMContext):
    d = await state.get_data()
    is_prem = 1 if call.data == "tgp_yes" else 0
    cur.execute('INSERT OR REPLACE INTO accounts (phone, is_running, is_premium) VALUES (?, 0, ?)',
                (d['phone'], is_prem))
    db.commit()
    await call.message.edit_text(f"✅ Аккаунт `{d['phone']}` добавлен в базу.")
    await state.clear()


async def broadcast_loop(phone):
    client = TelegramClient(f"sessions/{phone}", API_ID, API_HASH)
    try:
        await client.connect()
        while True:
            cur.execute('SELECT is_running, text, interval, chats, expires, photo_id FROM accounts WHERE phone = ?',
                        (phone,))
            res = cur.fetchone()
            if not res or not res[0] or int(time.time()) > res[4]: break
            chats = [c.strip() for c in res[3].split(',') if c.strip()]
            for chat in chats:
                cur.execute('SELECT is_running FROM accounts WHERE phone = ?', (phone,))
                if not cur.fetchone()[0]: break
                try:
                    entity, topic_id = extract_chat_and_topic(chat)
                    if res[5]:
                        f = await bot.get_file(res[5])
                        p_io = await bot.download_file(f.file_path)
                        buf = io.BytesIO(p_io.getvalue());
                        buf.name = "img.jpg"
                        await client.send_file(entity, buf, caption=res[1], reply_to=topic_id)
                    else:
                        await client.send_message(entity, res[1], reply_to=topic_id)
                except Exception as e:
                    logging.error(f"Broadcast error for {chat}: {e}")
                await asyncio.sleep(res[2])
            await asyncio.sleep(10)
    finally:
        await client.disconnect()


@dp.callback_query(F.data.startswith(("on_", "off_")))
async def toggle_r(call: types.CallbackQuery):
    p = call.data.split("_")[1]
    on = 1 if "on" in call.data else 0
    cur.execute('UPDATE accounts SET is_running = ? WHERE phone = ?', (on, p))
    db.commit()
    if on: asyncio.create_task(broadcast_loop(p))
    await manage_acc(call)


@dp.callback_query(F.data.startswith("set_"))
async def set_param_init(call: types.CallbackQuery, state: FSMContext):
    param, p = call.data.split("_")[1], call.data.split("_")[2]
    await state.update_data(target=p)
    st_map = {"text": States.edit_text, "photo": States.edit_photo, "chats": States.edit_chats,
              "int": States.edit_interval}
    await call.message.answer(f"Новое значение {param}:", reply_markup=back_kb(f"manage_{p}").as_markup())
    await state.set_state(st_map[param])


@dp.message(States.edit_text)
async def edit_t(m: Message, state: FSMContext):
    # ПРОВЕРКА НА СТОП-СЛОВА
    bad_word = contains_bad_words(m.text)
    if bad_word:
        return await m.answer(
            f"❌ Текст не сохранен!\nВы использовали запрещенное слово: `{bad_word}`.\nПожалуйста, измените текст.",
            parse_mode="Markdown")

    d = await state.get_data()
    cur.execute('UPDATE accounts SET text = ? WHERE phone = ?', (m.text, d['target']))
    db.commit()
    await m.answer("✅ Текст обновлен.")
    await state.clear()


@dp.message(States.edit_photo)
async def edit_p(m: Message, state: FSMContext):
    d = await state.get_data()
    fid = m.photo[-1].file_id if m.photo else None
    cur.execute('UPDATE accounts SET photo_id = ? WHERE phone = ?', (fid, d['target']))
    db.commit()
    await m.answer("✅ Фото обновлено.")
    await state.clear()


@dp.message(States.edit_chats)
async def edit_c(m: Message, state: FSMContext):
    d = await state.get_data()
    cur.execute('UPDATE accounts SET chats = ? WHERE phone = ?', (m.text, d['target']))
    db.commit()
    await m.answer("✅ Чаты обновлены.")
    await state.clear()


@dp.message(States.edit_interval)
async def edit_i(m: Message, state: FSMContext):
    if m.text.isdigit():
        d = await state.get_data()
        cur.execute('UPDATE accounts SET interval = ? WHERE phone = ?', (int(m.text), d['target']))
        db.commit()
        await m.answer("✅ Интервал обновлен.")
        await state.clear()


@dp.callback_query(F.data.startswith("cancel_"))
async def cancel_rent(call: types.CallbackQuery):
    p = call.data.split("_")[1]
    cur.execute('UPDATE accounts SET owner_id = NULL, expires = 0, is_running = 0, notified_10m = 0 WHERE phone = ?',
                (p,))
    db.commit()
    await call.answer("Отменено")
    await my_rents(call)


async def main():
    if not os.path.exists('sessions'): os.makedirs('sessions')

    # Запуск фоновой задачи для проверки аренды
    asyncio.create_task(check_expirations())

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())