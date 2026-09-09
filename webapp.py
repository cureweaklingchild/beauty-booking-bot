import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl

import aiosqlite
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATABASE_PATH = os.getenv("DATABASE_PATH", "booking.sqlite3")
WEBAPP_DIR = Path(__file__).parent / "webapp"
app = FastAPI(title="Booking Mini App")


class SlotRequest(BaseModel):
    date: str
    service_id: int
    times: list[str] = Field(min_length=1, max_length=50)


class ServiceRequest(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    duration_minutes: int = Field(ge=5, le=1440)
    price_rubles: int = Field(ge=0, le=10_000_000)


def telegram_user(init_data: str | None) -> dict:
    if not init_data or not BOT_TOKEN:
        raise HTTPException(status_code=401, detail="Откройте приложение из Telegram")
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", "")
    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        raise HTTPException(status_code=401, detail="Неверные данные Telegram")
    auth_date = int(values.get("auth_date", "0"))
    if time.time() - auth_date > 86_400:
        raise HTTPException(status_code=401, detail="Сессия устарела, откройте приложение заново")
    try:
        return json.loads(values["user"])
    except (KeyError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=401, detail="Не удалось определить пользователя") from error


async def current_master(init_data: str | None):
    user = telegram_user(init_data)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM masters WHERE telegram_id = ?", (user["id"],))
        master = await cursor.fetchone()
    if not master:
        raise HTTPException(status_code=403, detail="У вас нет профиля мастера")
    return user, master


@app.get("/")
async def index():
    return FileResponse(WEBAPP_DIR / "index.html")


@app.get("/static/{filename}")
async def static_file(filename: str):
    file_path = WEBAPP_DIR / filename
    if not file_path.is_file() or file_path.parent != WEBAPP_DIR:
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(file_path)


@app.get("/api/bootstrap")
async def bootstrap(x_telegram_init_data: str | None = Header(default=None)):
    _, master = await current_master(x_telegram_init_data)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        services_cursor = await db.execute(
            "SELECT id, name, duration_minutes, price_rubles FROM services WHERE master_id = ? ORDER BY name",
            (master["id"],),
        )
        services = [dict(row) for row in await services_cursor.fetchall()]
    return {"master": dict(master), "services": services}


@app.get("/api/slots")
async def slots(month: str, x_telegram_init_data: str | None = Header(default=None)):
    _, master = await current_master(x_telegram_init_data)
    try:
        month_start = datetime.strptime(month, "%Y-%m")
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Неверный месяц") from error
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT slots.id, slots.starts_at, slots.client_id, slots.client_name,
                   slots.service_id, services.name AS service_name, services.duration_minutes,
                   services.price_rubles
            FROM slots JOIN services ON services.id = slots.service_id
            WHERE slots.master_id = ? AND slots.starts_at >= ? AND slots.starts_at < ?
            ORDER BY slots.starts_at
            """,
            (master["id"], month_start.strftime("%Y-%m-%d"), next_month.strftime("%Y-%m-%d")),
        )
        result = [dict(row) for row in await cursor.fetchall()]
    return {"slots": result}


@app.post("/api/slots")
async def create_slots(
    request: SlotRequest,
    x_telegram_init_data: str | None = Header(default=None),
):
    _, master = await current_master(x_telegram_init_data)
    try:
        parsed_date = datetime.strptime(request.date, "%Y-%m-%d").date()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Неверная дата") from error
    if parsed_date < datetime.now().date():
        raise HTTPException(status_code=400, detail="Нельзя добавлять прошедшую дату")
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        service_cursor = await db.execute(
            "SELECT * FROM services WHERE id = ? AND master_id = ?",
            (request.service_id, master["id"]),
        )
        service = await service_cursor.fetchone()
        if not service:
            raise HTTPException(status_code=400, detail="Услуга не найдена")
        created = []
        conflicts = []
        for time_text in request.times:
            try:
                starts_at = datetime.strptime(f"{request.date} {time_text}", "%Y-%m-%d %H:%M")
            except ValueError:
                conflicts.append(time_text)
                continue
            ends_at = starts_at + timedelta(minutes=service["duration_minutes"])
            existing_cursor = await db.execute(
                """
                SELECT slots.starts_at, services.duration_minutes
                FROM slots JOIN services ON services.id = slots.service_id
                WHERE slots.master_id = ? AND date(slots.starts_at) = date(?)
                """,
                (master["id"], starts_at.strftime("%Y-%m-%d %H:%M")),
            )
            existing = await existing_cursor.fetchall()
            overlaps = False
            for row in existing:
                old_start = datetime.strptime(row["starts_at"], "%Y-%m-%d %H:%M")
                old_end = old_start + timedelta(minutes=row["duration_minutes"])
                if starts_at < old_end and ends_at > old_start:
                    overlaps = True
                    break
            if overlaps:
                conflicts.append(time_text)
                continue
            try:
                await db.execute(
                    "INSERT INTO slots (master_id, service_id, starts_at) VALUES (?, ?, ?)",
                    (master["id"], request.service_id, starts_at.strftime("%Y-%m-%d %H:%M")),
                )
                created.append(time_text)
            except aiosqlite.IntegrityError:
                conflicts.append(time_text)
        await db.commit()
    return {"created": created, "conflicts": conflicts}


@app.delete("/api/slots/{slot_id}")
async def delete_slot(slot_id: int, x_telegram_init_data: str | None = Header(default=None)):
    _, master = await current_master(x_telegram_init_data)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM slots WHERE id = ? AND master_id = ? AND client_id IS NULL",
            (slot_id, master["id"]),
        )
        await db.commit()
    if cursor.rowcount == 0:
        raise HTTPException(status_code=409, detail="Окно уже занято или не найдено")
    return {"ok": True}


@app.post("/api/services")
async def create_service(
    request: ServiceRequest,
    x_telegram_init_data: str | None = Header(default=None),
):
    _, master = await current_master(x_telegram_init_data)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        try:
            cursor = await db.execute(
                "INSERT INTO services (master_id, name, duration_minutes, price_rubles) VALUES (?, ?, ?, ?)",
                (master["id"], request.name.strip(), request.duration_minutes, request.price_rubles),
            )
            await db.commit()
        except aiosqlite.IntegrityError as error:
            raise HTTPException(status_code=409, detail="Такая услуга уже существует") from error
    return {"id": cursor.lastrowid, "name": request.name.strip()}
