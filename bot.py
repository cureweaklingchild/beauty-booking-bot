import asyncio
import logging
import os
import re
from datetime import datetime

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_PATH = os.getenv("DATABASE_PATH", "booking.sqlite3")
MINI_APP_URL = os.getenv("MINI_APP_URL", "").strip().rstrip("/")
WEBAPP_PORT = int(os.getenv("PORT", os.getenv("WEBAPP_PORT", "8000")))
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}

router = Router()


class SlotForm(StatesGroup):
    service = State()
    date = State()
    time = State()


class MasterForm(StatesGroup):
    name = State()


class ServiceForm(StatesGroup):
    name = State()
    duration = State()
    price = State()


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Записаться", callback_data="client:masters")],
            [InlineKeyboardButton(text="Мои записи", callback_data="client:bookings")],
            [InlineKeyboardButton(text="Кабинет мастера", callback_data="master:menu")],
        ]
    )


async def db_execute(query: str, parameters: tuple = (), *, fetchone=False, fetchall=False):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, parameters)
        await db.commit()
        if fetchone:
            return await cursor.fetchone()
        if fetchall:
            return await cursor.fetchall()
        return cursor.lastrowid


async def init_db() -> None:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS masters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                master_id INTEGER NOT NULL REFERENCES masters(id),
                name TEXT NOT NULL,
                duration_minutes INTEGER NOT NULL DEFAULT 60,
                price_rubles INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(master_id, name)
            );
            CREATE TABLE IF NOT EXISTS slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                master_id INTEGER NOT NULL REFERENCES masters(id),
                service_id INTEGER REFERENCES services(id),
                starts_at TEXT NOT NULL,
                client_id INTEGER,
                client_name TEXT,
                client_username TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(master_id, starts_at)
            );
            CREATE INDEX IF NOT EXISTS idx_slots_starts_at ON slots(starts_at);
            """
        )
        columns = await db.execute_fetchall("PRAGMA table_info(slots)")
        if not any(column["name"] == "service_id" for column in columns):
            await db.execute("ALTER TABLE slots ADD COLUMN service_id INTEGER REFERENCES services(id)")
        masters = await db.execute_fetchall("SELECT id FROM masters")
        for master in masters:
            await db.execute(
                "INSERT OR IGNORE INTO services (master_id, name) VALUES (?, ?)",
                (master["id"], "Общая запись"),
            )
        await db.execute(
            """
            UPDATE slots
            SET service_id = (
                SELECT services.id FROM services
                WHERE services.master_id = slots.master_id AND services.name = 'Общая запись'
            )
            WHERE service_id IS NULL
            """
        )
        await db.commit()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def get_master(telegram_id: int):
    return await db_execute(
        "SELECT * FROM masters WHERE telegram_id = ?",
        (telegram_id,),
        fetchone=True,
    )


async def show_masters(message: Message) -> None:
    masters = await db_execute("SELECT * FROM masters ORDER BY name", fetchall=True)
    if not masters:
        await message.answer("Пока нет зарегистрированных мастеров.")
        return
    buttons = [
        [InlineKeyboardButton(text=master["name"], callback_data=f"client:master:{master['id']}")]
        for master in masters
    ]
    await message.answer(
        "Выберите мастера:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


async def show_services(message: Message, master_id: int) -> None:
    services = await db_execute(
        "SELECT id, name, duration_minutes, price_rubles FROM services WHERE master_id = ? ORDER BY name",
        (master_id,),
        fetchall=True,
    )
    if not services:
        await message.answer("У этого мастера пока нет услуг.")
        return
    buttons = []
    for service in services:
        price = f", {service['price_rubles']} руб." if service["price_rubles"] else ""
        buttons.append([
            InlineKeyboardButton(
                text=f"{service['name']} ({service['duration_minutes']} мин{price})",
                callback_data=f"client:service:{master_id}:{service['id']}",
            )
        ])
    await message.answer(
        "Выберите услугу:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


async def show_slots(message: Message, master_id: int, service_id: int) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    slots = await db_execute(
        """
                SELECT slots.id, slots.starts_at, masters.name, services.name AS service_name
                FROM slots
                JOIN masters ON masters.id = slots.master_id
                JOIN services ON services.id = slots.service_id
                WHERE slots.master_id = ? AND slots.service_id = ?
                    AND slots.client_id IS NULL AND slots.starts_at >= ?
        ORDER BY slots.starts_at
        LIMIT 50
        """,
        (master_id, service_id, now),
        fetchall=True,
    )
    if not slots:
        await message.answer("У этого мастера пока нет свободных окон.")
        return
    buttons = []
    for slot in slots:
        date_text = datetime.strptime(slot["starts_at"], "%Y-%m-%d %H:%M").strftime("%d.%m %H:%M")
        buttons.append([
            InlineKeyboardButton(
                text=date_text,
                callback_data=f"client:book:{slot['id']}",
            )
        ])
    await message.answer(
        f"Свободные окна для услуги «{slots[0]['service_name']}» у мастера {slots[0]['name']}:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer(
        "Добро пожаловать в бот записи. Выберите действие:",
        reply_markup=main_menu(),
    )


@router.message(Command("id"))
async def id_handler(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@router.callback_query(F.data == "client:masters")
async def client_masters_handler(callback: CallbackQuery) -> None:
    await callback.answer()
    await show_masters(callback.message)


@router.callback_query(F.data.startswith("client:master:"))
async def client_slots_handler(callback: CallbackQuery) -> None:
    await callback.answer()
    master_id = int(callback.data.rsplit(":", 1)[1])
    await show_services(callback.message, master_id)


@router.callback_query(F.data.startswith("client:service:"))
async def client_service_handler(callback: CallbackQuery) -> None:
    await callback.answer()
    _, _, master_id, service_id = callback.data.split(":")
    await show_slots(callback.message, int(master_id), int(service_id))


@router.callback_query(F.data.startswith("client:book:"))
async def book_handler(callback: CallbackQuery, bot: Bot) -> None:
    slot_id = int(callback.data.rsplit(":", 1)[1])
    client = callback.from_user
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            UPDATE slots
            SET client_id = ?, client_name = ?, client_username = ?
            WHERE id = ? AND client_id IS NULL
            """,
            (client.id, client.full_name, client.username, slot_id),
        )
        await db.commit()
        if cursor.rowcount == 0:
            await callback.answer("Это окно уже заняли.", show_alert=True)
            return
        slot_cursor = await db.execute(
            """
                 SELECT slots.starts_at, masters.name, masters.telegram_id,
                     services.name AS service_name, services.price_rubles
                 FROM slots
                 JOIN masters ON masters.id = slots.master_id
                 JOIN services ON services.id = slots.service_id
            WHERE slots.id = ?
            """,
            (slot_id,),
        )
        slot = await slot_cursor.fetchone()
    await callback.answer("Вы записаны!")
    formatted_date = datetime.strptime(slot["starts_at"], "%Y-%m-%d %H:%M").strftime("%d.%m.%Y в %H:%M")
    price_text = f", стоимость {slot['price_rubles']} руб." if slot["price_rubles"] else ""
    await callback.message.answer(
        f"Готово! Вы записались к {slot['name']} на услугу «{slot['service_name']}» "
        f"на {formatted_date}{price_text}."
    )
    await bot.send_message(
        slot["telegram_id"],
        f"Новая запись: {client.full_name} (@{client.username or 'нет username'}) - "
        f"{slot['service_name']}, {formatted_date}{price_text}.",
    )


@router.callback_query(F.data == "client:bookings")
async def client_bookings_handler(callback: CallbackQuery) -> None:
    await callback.answer()
    bookings = await db_execute(
        """
        SELECT slots.id, slots.starts_at, masters.name, services.name AS service_name
        FROM slots
        JOIN masters ON masters.id = slots.master_id
        JOIN services ON services.id = slots.service_id
        WHERE slots.client_id = ? AND slots.starts_at >= ?
        ORDER BY slots.starts_at
        """,
        (callback.from_user.id, datetime.now().strftime("%Y-%m-%d %H:%M")),
        fetchall=True,
    )
    if not bookings:
        await callback.message.answer("У вас нет будущих записей.")
        return
    buttons = []
    for booking in bookings:
        date_text = datetime.strptime(booking["starts_at"], "%Y-%m-%d %H:%M").strftime("%d.%m.%Y %H:%M")
        buttons.append([
            InlineKeyboardButton(
                text=f"{date_text} - {booking['service_name']}",
                callback_data=f"client:cancel:{booking['id']}",
            )
        ])
    await callback.message.answer(
        "Ваши записи. Нажмите на запись, чтобы отменить её:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("client:cancel:"))
async def cancel_booking_handler(callback: CallbackQuery, bot: Bot) -> None:
    slot_id = int(callback.data.rsplit(":", 1)[1])
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        slot_cursor = await db.execute(
            """
            SELECT slots.starts_at, masters.name, masters.telegram_id, services.name AS service_name
            FROM slots
            JOIN masters ON masters.id = slots.master_id
            JOIN services ON services.id = slots.service_id
            WHERE slots.id = ? AND slots.client_id = ?
            """,
            (slot_id, callback.from_user.id),
        )
        slot = await slot_cursor.fetchone()
        if not slot:
            await callback.answer("Запись уже отменена или не найдена.", show_alert=True)
            return
        await db.execute(
            """
            UPDATE slots
            SET client_id = NULL, client_name = NULL, client_username = NULL
            WHERE id = ? AND client_id = ?
            """,
            (slot_id, callback.from_user.id),
        )
        await db.commit()
    await callback.answer("Запись отменена")
    formatted_date = datetime.strptime(slot["starts_at"], "%Y-%m-%d %H:%M").strftime("%d.%m.%Y в %H:%M")
    await callback.message.answer(
        f"Запись к {slot['name']} на услугу «{slot['service_name']}» на {formatted_date} отменена."
    )
    await bot.send_message(
        slot["telegram_id"],
        f"Клиент {callback.from_user.full_name} отменил запись на "
        f"услугу «{slot['service_name']}» на {formatted_date}.",
    )


@router.callback_query(F.data == "master:menu")
async def master_menu_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not is_admin(callback.from_user.id):
        await callback.message.answer("Доступ мастера пока не подключён для вашего аккаунта.")
        await callback.message.answer("Попросите владельца бота добавить ваш Telegram ID в ADMIN_IDS.")
        return
    master = await get_master(callback.from_user.id)
    if not master:
        await callback.message.answer("Как вас показывать клиентам? Напишите имя или название кабинета.")
        await state.set_state(MasterForm.name)
        return
    await callback.message.answer(
        f"Кабинет мастера: {master['name']}",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                *([[InlineKeyboardButton(text="Открыть календарь", web_app=WebAppInfo(url=MINI_APP_URL))]] if MINI_APP_URL else []),
                [InlineKeyboardButton(text="Мои услуги", callback_data="master:services")],
                [InlineKeyboardButton(text="Добавить свободное окно", callback_data="master:add")],
                [InlineKeyboardButton(text="Мои записи", callback_data="master:bookings")],
            ]
        ),
    )


@router.message(MasterForm.name)
async def master_name_handler(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    name = message.text.strip()
    if not 2 <= len(name) <= 80:
        await message.answer("Имя должно быть от 2 до 80 символов. Попробуйте ещё раз.")
        return
    master_id = await db_execute(
        "INSERT INTO masters (telegram_id, name) VALUES (?, ?)",
        (message.from_user.id, name),
    )
    await db_execute(
        "INSERT INTO services (master_id, name) VALUES (?, ?)",
        (master_id, "Общая запись"),
    )
    await state.clear()
    await message.answer("Профиль мастера создан. Нажмите /start и откройте кабинет мастера.")


@router.callback_query(F.data == "master:services")
async def master_services_handler(callback: CallbackQuery) -> None:
    await callback.answer()
    master = await get_master(callback.from_user.id)
    if not master:
        await callback.message.answer("Сначала создайте профиль мастера через кабинет.")
        return
    services = await db_execute(
        "SELECT name, duration_minutes, price_rubles FROM services WHERE master_id = ? ORDER BY name",
        (master["id"],),
        fetchall=True,
    )
    lines = ["Ваши услуги:"]
    for service in services:
        price = f", {service['price_rubles']} руб." if service["price_rubles"] else ""
        lines.append(f"- {service['name']}: {service['duration_minutes']} мин{price}")
    await callback.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Добавить услугу", callback_data="master:add_service")],
            ]
        ),
    )


@router.callback_query(F.data == "master:add_service")
async def add_service_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not await get_master(callback.from_user.id):
        await callback.message.answer("Сначала создайте профиль мастера через кабинет.")
        return
    await state.set_state(ServiceForm.name)
    await callback.message.answer("Введите название услуги, например «Женская стрижка».")


@router.message(ServiceForm.name)
async def service_name_handler(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not 2 <= len(name) <= 80:
        await message.answer("Название должно быть от 2 до 80 символов. Попробуйте ещё раз.")
        return
    await state.update_data(service_name=name)
    await state.set_state(ServiceForm.duration)
    await message.answer("Сколько минут длится услуга? Введите целое число, например 60.")


@router.message(ServiceForm.duration)
async def service_duration_handler(message: Message, state: FSMContext) -> None:
    if not message.text.strip().isdigit() or not 5 <= int(message.text.strip()) <= 1440:
        await message.answer("Введите длительность целым числом от 5 до 1440 минут.")
        return
    await state.update_data(duration=int(message.text.strip()))
    await state.set_state(ServiceForm.price)
    await message.answer("Введите цену в рублях или 0, если цену показывать не нужно.")


@router.message(ServiceForm.price)
async def service_price_handler(message: Message, state: FSMContext) -> None:
    price_text = message.text.strip()
    if not price_text.isdigit() or int(price_text) < 0:
        await message.answer("Введите цену целым числом, например 2500, или 0.")
        return
    data = await state.get_data()
    master = await get_master(message.from_user.id)
    try:
        await db_execute(
            "INSERT INTO services (master_id, name, duration_minutes, price_rubles) VALUES (?, ?, ?, ?)",
            (master["id"], data["service_name"], data["duration"], int(price_text)),
        )
    except aiosqlite.IntegrityError:
        await message.answer("Такая услуга уже есть. Добавьте её под другим названием.")
        await state.clear()
        return
    await state.clear()
    await message.answer("Услуга добавлена. Теперь для неё можно создавать свободные окна.")


@router.callback_query(F.data == "master:add")
async def add_slot_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    master = await get_master(callback.from_user.id)
    if not master:
        await callback.message.answer("Сначала создайте профиль мастера через кабинет.")
        return
    services = await db_execute(
        "SELECT id, name, duration_minutes, price_rubles FROM services WHERE master_id = ? ORDER BY name",
        (master["id"],),
        fetchall=True,
    )
    buttons = []
    for service in services:
        buttons.append([
            InlineKeyboardButton(
                text=f"{service['name']} ({service['duration_minutes']} мин)",
                callback_data=f"master:addslot:{service['id']}",
            )
        ])
    await callback.message.answer(
        "Для какой услуги добавить окно?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("master:addslot:"))
async def select_slot_service_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    service_id = int(callback.data.rsplit(":", 1)[1])
    master = await get_master(callback.from_user.id)
    service = await db_execute(
        "SELECT id FROM services WHERE id = ? AND master_id = ?",
        (service_id, master["id"] if master else 0),
        fetchone=True,
    )
    if not service:
        await callback.message.answer("Эта услуга не принадлежит вашему профилю.")
        return
    await state.update_data(service_id=service_id)
    await state.set_state(SlotForm.date)
    await callback.message.answer("Введите дату окна в формате ДД.ММ.ГГГГ, например 25.12.2026.")


@router.message(SlotForm.date)
async def slot_date_handler(message: Message, state: FSMContext) -> None:
    try:
        parsed_date = datetime.strptime(message.text.strip(), "%d.%m.%Y")
        if parsed_date.date() < datetime.now().date():
            raise ValueError
    except ValueError:
        await message.answer("Не понял дату. Используйте формат ДД.ММ.ГГГГ и укажите будущую дату.")
        return
    await state.update_data(date=parsed_date.strftime("%Y-%m-%d"))
    await state.set_state(SlotForm.time)
    await message.answer("Теперь введите время в формате ЧЧ:ММ, например 14:30.")


@router.message(SlotForm.time)
async def slot_time_handler(message: Message, state: FSMContext) -> None:
    time_text = message.text.strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time_text):
        await message.answer("Не понял время. Используйте формат ЧЧ:ММ, например 14:30.")
        return
    data = await state.get_data()
    starts_at = f"{data['date']} {time_text}"
    master = await get_master(message.from_user.id)
    try:
        await db_execute(
            "INSERT INTO slots (master_id, service_id, starts_at) VALUES (?, ?, ?)",
            (master["id"], data["service_id"], starts_at),
        )
    except aiosqlite.IntegrityError:
        await message.answer("Такое окно уже добавлено. Введите другую дату или время через /start.")
        await state.clear()
        return
    await state.clear()
    await message.answer("Свободное окно добавлено. Клиенты уже могут его увидеть.")


@router.callback_query(F.data == "master:bookings")
async def bookings_handler(callback: CallbackQuery) -> None:
    await callback.answer()
    master = await get_master(callback.from_user.id)
    if not master:
        await callback.message.answer("Профиль мастера ещё не создан.")
        return
    bookings = await db_execute(
        """
         SELECT slots.id, slots.starts_at, slots.client_name, slots.client_username,
             services.name AS service_name
        FROM slots JOIN services ON services.id = slots.service_id
        WHERE slots.master_id = ? AND slots.client_id IS NOT NULL AND slots.starts_at >= ?
        ORDER BY starts_at
        """,
        (master["id"], datetime.now().strftime("%Y-%m-%d %H:%M")),
        fetchall=True,
    )
    if not bookings:
        await callback.message.answer("Будущих записей пока нет.")
        return
    buttons = []
    lines = ["Ваши ближайшие записи:"]
    for booking in bookings:
        date_text = datetime.strptime(booking["starts_at"], "%Y-%m-%d %H:%M").strftime("%d.%m.%Y %H:%M")
        username = f"@{booking['client_username']}" if booking["client_username"] else "без username"
        lines.append(f"{date_text} - {booking['service_name']} - {booking['client_name']} ({username})")
        buttons.append([
            InlineKeyboardButton(
                text=f"Отменить: {date_text} - {booking['client_name']}",
                callback_data=f"master:cancel:{booking['id']}",
            )
        ])
    await callback.message.answer(
        "\n".join(lines) + "\n\nВыберите запись для отмены:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("master:cancel:"))
async def master_cancel_booking_handler(callback: CallbackQuery, bot: Bot) -> None:
    slot_id = int(callback.data.rsplit(":", 1)[1])
    master = await get_master(callback.from_user.id)
    if not master:
        await callback.answer("Профиль мастера не найден.", show_alert=True)
        return
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        slot_cursor = await db.execute(
            """
            SELECT slots.starts_at, slots.client_id, masters.name, services.name AS service_name
            FROM slots
            JOIN masters ON masters.id = slots.master_id
            JOIN services ON services.id = slots.service_id
            WHERE slots.id = ? AND slots.master_id = ? AND slots.client_id IS NOT NULL
            """,
            (slot_id, master["id"]),
        )
        slot = await slot_cursor.fetchone()
        if not slot:
            await callback.answer("Запись уже отменена или не найдена.", show_alert=True)
            return
        await db.execute(
            """
            UPDATE slots
            SET client_id = NULL, client_name = NULL, client_username = NULL
            WHERE id = ? AND master_id = ? AND client_id IS NOT NULL
            """,
            (slot_id, master["id"]),
        )
        await db.commit()
    await callback.answer("Запись отменена")
    formatted_date = datetime.strptime(slot["starts_at"], "%Y-%m-%d %H:%M").strftime("%d.%m.%Y в %H:%M")
    await callback.message.answer(
        f"Запись клиента на услугу «{slot['service_name']}» на {formatted_date} отменена."
    )
    await bot.send_message(
        slot["client_id"],
        f"Мастер {slot['name']} отменил вашу запись на услугу "
        f"«{slot['service_name']}» на {formatted_date}.",
    )


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Не найден BOT_TOKEN. Создайте файл .env по примеру .env.example.")
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    import uvicorn
    from webapp import app as web_app

    web_server = uvicorn.Server(
        uvicorn.Config(web_app, host="0.0.0.0", port=WEBAPP_PORT, log_level="info")
    )
    asyncio.create_task(web_server.serve())
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    logging.info("Бот запущен")
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())