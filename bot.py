# bot.py — aiogram v3
# Клиент платит 19 Kč; админ создаёт/редактирует объявления (ID) с режимом выдачи LINK/TEXT

import os
import sqlite3
import json
import asyncio
import logging
from typing import Optional, Dict, List, Tuple

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, StateFilter
from aiogram.filters.command import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery, InputMediaPhoto
)
from dotenv import load_dotenv

# ── LOGGING ─────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)

# ── ENV ─────────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
PROVIDER_TOKEN = os.getenv("PROVIDER_TOKEN", "TEST")
ADMIN_ID       = int(os.getenv("ADMIN_ID", "0"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").lstrip("@")
PRICE_HAL      = int(os.getenv("PRICE_HAL", "1900"))   # 19 Kč = 1900 геллеров
CHANNEL_RAW    = os.getenv("CHANNEL_ID", "").strip()   # @username или -100...

if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN не задан в .env")
if not CHANNEL_RAW:
    raise SystemExit("❌ CHANNEL_ID не задан в .env")

CHANNEL_ID = CHANNEL_RAW if CHANNEL_RAW.startswith("@") else int(CHANNEL_RAW)

bot = Bot(BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

# два раздельных роутера
r_public = Router(name="public")
r_admin  = Router(name="admin")

# ВСЁ, что пишет админ, идёт в этот роутер
r_admin.message.filter(F.from_user.id == ADMIN_ID)
r_admin.callback_query.filter(F.from_user.id == ADMIN_ID)

# подключаем роутеры
dp.include_router(r_admin)
dp.include_router(r_public)

BOT_USERNAME: str = ""  # подставим при старте

# ── Состояния админ-флоу ─────────────────────────────────────────────────────
class AddListing(StatesGroup):
    channel_text  = State()   # текст поста для канала (твой шаблон)
    decide_link   = State()   # есть ли публичная ссылка на оригинал?
    post_url      = State()   # если "да" — URL
    orig_text     = State()   # если "нет" — текст оригинала
    contact       = State()   # ссылка для связи (контакт автора)
    photos_choice = State()   # добавить фото?
    photos        = State()   # загрузка фото (до 9)

# ── Черновики в БД ───────────────────────────────────────────────────────────

# ── База (SQLite) ────────────────────────────────────────────────────────────
DB_FILE = "listings.db"

# --- DB helpers -------------------------------------------------

def db_init():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS listings (
            id           TEXT PRIMARY KEY,
            text         TEXT NOT NULL,
            link         TEXT NOT NULL,
            post_url     TEXT DEFAULT '',
            deliver_mode TEXT DEFAULT 'TEXT',
            orig_text    TEXT DEFAULT '',
            photos       TEXT DEFAULT '[]',     -- JSON: [file_id, ...]
            status       TEXT DEFAULT 'DRAFT'   -- DRAFT | PUBLISHED
        )
    """)
    # авто-миграции для старых БД
    cur.execute("PRAGMA table_info(listings)")
    cols = [row[1] for row in cur.fetchall()]
    if "photos" not in cols:
        cur.execute("ALTER TABLE listings ADD COLUMN photos TEXT DEFAULT '[]'")
    if "status" not in cols:
        cur.execute("ALTER TABLE listings ADD COLUMN status TEXT DEFAULT 'DRAFT'")
    conn.commit()
    conn.close()

def db_upsert(listing_id: str, channel_text: str, link: str,
              post_url: str, deliver_mode: str, orig_text: str,
              photos: List[str], status: str = "DRAFT") -> None:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO listings (id, text, link, post_url, deliver_mode, orig_text, photos, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            text=excluded.text,
            link=excluded.link,
            post_url=excluded.post_url,
            deliver_mode=excluded.deliver_mode,
            orig_text=excluded.orig_text,
            photos=excluded.photos,
            status=excluded.status
    """, (
        listing_id, channel_text, link, post_url, deliver_mode, orig_text,
        json.dumps(photos, ensure_ascii=False), status
    ))
    conn.commit()
    conn.close()

def db_get(listing_id: str) -> Optional[Tuple]:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        SELECT text, link, post_url, deliver_mode, orig_text, photos, status
        FROM listings WHERE id = ?
    """, (listing_id,))
    row = cur.fetchone()
    conn.close()
    return row

def db_delete(listing_id: str) -> bool:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM listings WHERE id = ?", (listing_id,))
    ok = cur.rowcount > 0
    conn.commit()
    conn.close()
    return ok

def db_set_status(listing_id: str, status: str) -> None:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("UPDATE listings SET status=? WHERE id=?", (status, listing_id))
    conn.commit()
    conn.close()

# ── Клавиатуры (клиент) ──────────────────────────────────────────────────────
def kb_main() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="🔎 Получить контакт", callback_data="get_contact")]]
    if ADMIN_USERNAME:
        rows.append([InlineKeyboardButton(text="🗣️ Поддержка", url=f"https://t.me/{ADMIN_USERNAME}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_confirm(listing_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Да, это оно", callback_data=f"confirm:{listing_id}"),
        InlineKeyboardButton(text="❌ Нет, другой ID", callback_data="get_contact"),
    ]])

def kb_pay(listing_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💳 Оплатить 19 Kč", callback_data=f"pay:{listing_id}")
    ]])

def kb_support(listing_id: str = "") -> InlineKeyboardMarkup:
    rows = []
    if listing_id:
        rows.append([InlineKeyboardButton(text="🔁 Повторить оплату", callback_data=f"pay:{listing_id}")])
    if ADMIN_USERNAME:
        rows.append([InlineKeyboardButton(text="🗣️ Поддержка", url=f"https://t.me/{ADMIN_USERNAME}")])
    return InlineKeyboardMarkup(inline_keyboard=rows or [[]])

def kb_deeplink(listing_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🔓 Получить контакт",
                url=f"https://t.me/{BOT_USERNAME}?start={listing_id}"
            )
        ]]
    )

# ── Клавиатуры (админ) ───────────────────────────────────────────────────────
CANCEL_KB = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_add")]]
)

def kb_admin_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список объявлений", callback_data="adm:list")],
        [InlineKeyboardButton(text="➕ Как создать /add",  callback_data="adm:add_hint")],
        [InlineKeyboardButton(text="🗑 Как удалить /delete", callback_data="adm:del_hint")],
        [InlineKeyboardButton(text="🆔 Мой ID",           callback_data="adm:whoami")],
    ])

def kb_yes_no_link() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, есть публичная ссылка", callback_data="haslink:yes")],
        [InlineKeyboardButton(text="❌ Нет, пришлю текст оригинала", callback_data="haslink:no")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_add")]
    ])

def kb_channel_text_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👍 Всё верно", callback_data="chantext:ok")],
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="chantext:edit")],
        [InlineKeyboardButton(text="❌ Отмена",   callback_data="cancel_add")],
    ])

def kb_photos_choice() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖼 Добавить фото", callback_data="photos:yes")],
        [InlineKeyboardButton(text="⏭ Без фото — к предпросмотру", callback_data="photos:no")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_add")]
    ])

def kb_finish_preview() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сформировать предпросмотр", callback_data="finish_add")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_add")]
    ])

def kb_preview(listing_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"publish:{listing_id}"),
         InlineKeyboardButton(text="🔄 Начать сначала", callback_data="restart")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_add")]
    ])

# ── Клиент: /start + deeplink ────────────────────────────────────────────────
@r_public.message(Command("start"))
async def cmd_start(m: Message, command: CommandObject):
    text = (
        "👋 Привет! Я помогу тебе быстро получить прямые контакты владельцев квартир и комнат.\n\n"
        "📌 Как это работает:\n"
        "1. В канале публикуются объявления с уникальным **ID** (например: `A123`).\n"
        "2. Ты выбираешь интересующее объявление и нажимаешь «🔓 Получить контакт».\n"
        "3. Оплата символическая — всего *19 Kč*. Это меньше чашки кофе ☕.\n"
        "4. Сразу после оплаты я отправлю тебе ссылку на оригинальное объявление с прямым контактом владельца.\n\n"
        "✨ Почему это честно:\n"
        "Мы тратим время, чтобы найти объявления из разных источников, перевести их, оформить и опубликовать. "
        "Риелторы берут целые комиссии 💸, а ты платишь всего 19 Kč и связываешься напрямую с хозяином!\n\n"
        "⏳ Обрати внимание:\n"
        "Все публикации в канале актуальны на момент выхода. Мы стараемся размещать их не позднее чем через 8 часов "
        "после появления объявления. Чтобы не упустить вариант, лучше как можно раньше получить контакт и написать владельцу.\n\n"
        "➡️ Жми кнопку ниже, чтобы начать."
    )
    await m.answer(text, reply_markup=kb_main())

    # Автоподхват ID, если человек пришёл по deep-link: t.me/<bot>?start=A123
    if command.args:
        listing_id = command.args.strip().upper()
        row = db_get(listing_id)
        if row:
            channel_text, _, post_url, deliver, _, *_ = row
            hint = "ℹ️ После оплаты получишь оригинальный текст и контакт автора; если есть ссылка на оригинал — пришлю её тоже."
            await m.answer(
                f"📋 Проверь объявление (ID {listing_id}):\n\n{channel_text}\n\n{hint}",
                reply_markup=kb_confirm(listing_id)
            )
        else:
            await m.answer("⚠️ Такого ID нет. Проверь пост в канале или напиши администратору.", reply_markup=kb_support())

# ── Клиент: ввод ID → подтверждение → оплата → выдача ────────────────────────
@r_public.callback_query(F.data == "get_contact")
async def ask_id(call: CallbackQuery):
    await call.message.answer("✍️ Напиши **ID** объявления (пример: `A101`).")
    await call.answer()

@r_public.message(F.text.regexp(r"^[A-Za-z]\d+$"))
async def on_id(m: Message):
    listing_id = m.text.strip().upper()
    row = db_get(listing_id)
    if not row:
        return await m.answer("⚠️ Такого ID нет. Проверь в канале или напиши администратору.", reply_markup=kb_support())
    channel_text, _, post_url, deliver, _, *_ = row
    hint = (
    "ℹ️ После оплаты я пришлю текст оригинального объявления и прямой контакт автора. "
)
    await m.answer(f"📋 Проверь объявление (ID {listing_id}):\n\n{channel_text}\n\n{hint}", reply_markup=kb_confirm(listing_id))

@r_public.callback_query(F.data.startswith("confirm:"))
async def on_confirm(call: CallbackQuery):
    listing_id = call.data.split(":")[1]
    if not db_get(listing_id):
        await call.message.answer("❌ Объявление не найдено.", reply_markup=kb_support())
        return await call.answer()

    text = (
        "💳 Чтобы получить контакт владельца, необходимо оплатить *19 Kč*.\n\n"
        "✅ Сразу после успешной оплаты я отправлю\n"
        "прямой контакт владельца 📲"
    )

    pay_kb = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="💳 Оплатить 19 Kč", callback_data=f"pay:{listing_id}")
    ]])

    await call.message.answer(text, reply_markup=pay_kb, parse_mode="Markdown")
    await call.answer()

# ───────Клиент: оплата 
# ───────Клиент: оплата / выдача доступа
async def _deliver_access(user_id: int, listing_id: str):
    row = db_get(listing_id)
    if not row:
        await bot.send_message(
            user_id,
            "❌ Объявление не найдено. Напиши администратору.",
            reply_markup=kb_support()
        )
        return

    channel_text, contact_link, post_url, _deliver, orig_text, *_ = row
    final_text = (orig_text or "").strip() or channel_text

    await bot.send_message(user_id, "✅ Оплата получена.\nВот данные по объявлению:")
    await bot.send_message(user_id, f"📝 Оригинальный текст:\n\n{final_text}")
    await bot.send_message(user_id, f"📞 Контакт для связи:\n{contact_link}", reply_markup=kb_support(listing_id))
    if post_url:
        await bot.send_message(user_id, f"🔗 Ссылка на оригинал:\n{post_url}")

@r_public.callback_query(F.data.startswith("pay:"))
async def on_pay(call: CallbackQuery):
    _, listing_id = call.data.split(":")
    if not db_get(listing_id):
        await call.message.answer("❌ Объявление не найдено.", reply_markup=kb_support())
        return await call.answer()

    # ДЕМО: без инвойса — сразу выдаём доступ
    if not PROVIDER_TOKEN or PROVIDER_TOKEN.upper() == "TEST":
        await call.message.answer("🧪 Демо-режим: платежи не настроены. Выдаю доступ без списания средств.")
        await _deliver_access(call.from_user.id, listing_id)
        return await call.answer()

    # ПРОД: реальный счёт (один раз)
    try:
        price = LabeledPrice(label=f"Доступ к объявлению {listing_id}", amount=PRICE_HAL)
        await bot.send_invoice(
            call.from_user.id,
            title="Доступ к объявлению",
            description=f"Разовый доступ к данным по ID {listing_id}. Стоимость: 19 Kč.",
            provider_token=PROVIDER_TOKEN,
            currency="CZK",
            prices=[price],
            start_parameter="pay_contact",
            payload=listing_id
        )
    except Exception as e:
        logging.exception("send_invoice failed")
        await call.message.answer(f"⚠️ Не удалось создать счёт: {e}", reply_markup=kb_support(listing_id))
    finally:
        await call.answer()

    # === Реальная оплата через Telegram Payments ===
    try:
        price = LabeledPrice(label=f"Доступ к объявлению {listing_id}", amount=PRICE_HAL)  # 19 Kč = 1900 геллеров
        await bot.send_invoice(
            call.from_user.id,
            title="Доступ к объявлению",
            description=f"Разовый доступ к данным по ID {listing_id}. Стоимость: 19 Kč.",
            provider_token=PROVIDER_TOKEN,
            currency="CZK",
            prices=[price],
            start_parameter="pay_contact",
            payload=listing_id
        )
    except Exception as e:
        # покажем причину, чтобы сразу увидеть, что не так с токеном/настройкой
        logging.exception("send_invoice failed")
        await call.message.answer(f"⚠️ Не удалось создать счёт: {e}\n\nПроверь PROVIDER_TOKEN в .env или попроси помощь.", reply_markup=kb_support(listing_id))
    finally:
        await call.answer()

@r_public.pre_checkout_query()
async def on_pre_checkout(q: PreCheckoutQuery):
    ok = db_get(q.invoice_payload) is not None
    await bot.answer_pre_checkout_query(
        q.id, ok=ok,
        error_message="Объявление не найдено. Деньги не списаны. Обратитесь к администратору."
    )

@r_public.message(F.successful_payment)
async def on_success(m: Message):
    listing_id = m.successful_payment.invoice_payload
    await _deliver_access(m.chat.id, listing_id)

# ── Пользовательская помощь
@r_public.message(Command("help"))
async def help_cmd(m: Message):
    user_help = (
        "ℹ️ Помощь\n\n"
        "• Введи ID объявления из канала (например, A123), затем подтверди и оплати 19 Kč.\n"
        "• После оплаты я отправлю контакт автора оригинального объявления. Все посты в канале актуальны и опубликованы у нас не позднее 8-и часов после публикации оригинального объявления.\n\n"
        "Команды:\n"
        "/start — начать\n"
        "/help — помощь\n"
    )
    if m.from_user.id == ADMIN_ID:
        admin_help = (
            "\n— — — — — — — — —\n"
            "👑 Админ-команды:\n"
            "/admin — панель админа (кнопки)\n"
            "/add <ID> — создать/редактировать объявление\n"
            "/listings — список всех ID в базе\n"
            "/delete <ID> — удалить объявление из базы\n"
            "/whoami — показать твой numeric ID\n"
        )
        await m.answer(user_help + admin_help)
    else:
        await m.answer(user_help)

@r_public.message(Command("whoami"))
async def whoami(message: Message):
    await message.answer(f"🆔 Твой ID: `{message.from_user.id}`", parse_mode="Markdown")


# ========== АДМИН-ФЛОУ: СОЗДАНИЕ/ПУБЛИКАЦИЯ/СПИСОК/УДАЛЕНИЕ ==========

# ── Админ-панель (/admin) и её кнопки
@r_admin.message(Command("admin"))
async def admin_panel_cmd(m: Message):
    await m.answer("🔧 Панель администратора:", reply_markup=kb_admin_panel())

@r_admin.callback_query(F.data == "adm:list")
async def adm_list(call: CallbackQuery):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT id, text, status FROM listings ORDER BY id COLLATE NOCASE")
    rows = cur.fetchall()
    conn.close()

    if not rows:
        await call.message.answer("📭 База объявлений пуста.")
    else:
        lines = ["📋 Список объявлений:\n"]
        for lid, txt, st in rows:
            short = (txt[:60] + "…") if len(txt) > 60 else txt
            lines.append(f"🔹 {lid} — {st} — {short}")
        await call.message.answer("\n".join(lines))
    await call.answer()

@r_admin.callback_query(F.data == "adm:add_hint")
async def adm_add_hint(call: CallbackQuery):
    txt = (
        "➕ Как создать объявление:\n"
        "1) /add <ID>  — например: /add A101\n"
        "2) Вставь текст поста (твой шаблон для канала)\n"
        "3) Ответь, есть ли публичная ссылка на оригинал\n"
        "4) Пришли URL ИЛИ текст оригинала\n"
        "5) Пришли ссылку для связи (контакт автора)\n"
        "6) Выбери режим выдачи: LINK (ссылка) или TEXT (текст+контакт)\n"
        "7) (Опционально) добавь до 9 фото\n"
        "8) «Сформировать предпросмотр» → «Опубликовать»\n"
    )
    await call.message.answer(txt)
    await call.answer()

@r_admin.callback_query(F.data == "adm:del_hint")
async def adm_del_hint(call: CallbackQuery):
    await call.message.answer("🗑 Удаление: отправь `/delete <ID>`  — например: `/delete A101`", parse_mode="Markdown")
    await call.answer()

@r_admin.callback_query(F.data == "adm:whoami")
async def adm_whoami(call: CallbackQuery):
    await call.message.answer(f"🆔 Твой ID: `{call.from_user.id}`", parse_mode="Markdown")
    await call.answer()

# ── /listings — список всех ID (текстом)
@r_admin.message(Command("listings"))
async def list_listings(message: Message):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT id, text FROM listings ORDER BY id COLLATE NOCASE")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return await message.answer("📭 База объявлений пуста.")
    lines = ["📋 Список объявлений:\n"]
    for lid, txt in rows:
        short = (txt[:60] + "…") if len(txt) > 60 else txt
        lines.append(f"🔹 {lid} — {short}")
    await message.answer("\n".join(lines))

# ── /add <ID> — старт создания/редактирования
@r_admin.message(Command("add"))
async def add_listing_cmd(message: Message, state: FSMContext):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.answer("⚠️ Укажи ID: /add A101")

    listing_id = parts[1].strip().upper()

    await state.clear()
    await state.update_data(
        listing_id=listing_id,
        channel_text="",
        orig_text="",
        post_url="",
        link="",
        deliver_mode="",
        photos=[]
    )
    await state.set_state(AddListing.channel_text)
    await message.answer("✍️ Отправь **текст поста для канала** (готовый шаблон).")

# ── 1) Текст поста для канала
@r_admin.message(StateFilter(AddListing.channel_text), F.text)
async def add_channel_text(message: Message, state: FSMContext):
    channel = (message.text or "").strip()
    if not channel:
        return await message.answer("⚠️ Пришли, пожалуйста, **текст** поста для канала.")
    await state.update_data(channel_text=channel)
    data = await state.get_data()
    listing_id = data.get("listing_id", "—")
    await message.answer(
        f"🔎 Предпросмотр текста для канала по ID {listing_id}:\n\n{channel}",
        reply_markup=kb_channel_text_confirm()
    )
    # остаёмся в AddListing.channel_text до нажатия кнопки

@r_admin.callback_query(F.data == "chantext:ok", StateFilter(AddListing.channel_text))
async def chantext_ok(call: CallbackQuery, state: FSMContext):
    await call.message.answer("❓ Есть **публичная ссылка на оригинальный пост**?", reply_markup=kb_yes_no_link())
    await state.set_state(AddListing.decide_link)
    await call.answer()

@r_admin.callback_query(F.data == "chantext:edit", StateFilter(AddListing.channel_text))
async def chantext_edit(call: CallbackQuery, state: FSMContext):
    await call.message.answer("✍️ Ок, пришли исправленный **текст поста для канала**.")
    await call.answer()

# ── 2a) «Да, есть ссылка» → попросить URL
@r_admin.callback_query(F.data == "haslink:yes", StateFilter(AddListing.decide_link))
async def has_link_yes(call: CallbackQuery, state: FSMContext):
    await call.message.answer("🔗 Пришли URL оригинального поста.", reply_markup=CANCEL_KB)
    await state.set_state(AddListing.post_url)
    await call.answer()

@r_admin.message(StateFilter(AddListing.post_url), F.text)
async def set_post_url(message: Message, state: FSMContext):
    await state.update_data(post_url=message.text.strip())
    await message.answer("📎 Теперь пришли ссылку для связи.", reply_markup=CANCEL_KB)
    await state.set_state(AddListing.contact)

# ── 2b) «Нет ссылки» → попросить текст оригинала
@r_admin.callback_query(F.data == "haslink:no", StateFilter(AddListing.decide_link))
async def has_link_no(call: CallbackQuery, state: FSMContext):
    await call.message.answer("📝 Тогда пришли текст оригинального поста.", reply_markup=CANCEL_KB)
    await state.set_state(AddListing.orig_text)
    await call.answer()

@r_admin.message(StateFilter(AddListing.orig_text), F.text)
async def set_orig_text(message: Message, state: FSMContext):
    await state.update_data(orig_text=message.text.strip())
    await message.answer(
        "📎 Теперь пришли **ссылку для связи** (контакт автора).", reply_markup=CANCEL_KB)
    await state.set_state(AddListing.contact)

# ── 3) Ссылка для связи → выбор режима выдачи и фото
@r_admin.message(StateFilter(AddListing.contact), F.text)
async def set_contact_link(message: Message, state: FSMContext):
    await state.update_data(link=message.text.strip())
    await state.update_data(deliver_mode="TEXT")  # фиксируем всегда TEXT
    await message.answer("📸 Добавить фото к посту?", reply_markup=kb_photos_choice())
    await state.set_state(AddListing.photos_choice)

# ── 5) Фото: да/нет
@r_admin.callback_query(F.data == "photos:yes", StateFilter(AddListing.photos_choice))
async def photos_yes(call: CallbackQuery, state: FSMContext):
    await call.message.answer(
        "Ок! Пришли до **9** фото (несколькими сообщениями).\n"
        "Когда закончишь — нажми «Сформировать предпросмотр» или отправь /done.",
        reply_markup=kb_finish_preview()
    )
    await state.set_state(AddListing.photos)
    await call.answer()

@r_admin.callback_query(F.data == "photos:no", StateFilter(AddListing.photos_choice))
async def photos_no(call: CallbackQuery, state: FSMContext):
    await build_preview(call.message, state)
    await call.answer()

@r_admin.message(StateFilter(AddListing.photos), F.photo)
async def add_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    photos: List[str] = data.get("photos", [])
    if len(photos) >= 9:
        return await message.answer("⚠️ Лимит 9 фото. Жми «Сформировать предпросмотр».", reply_markup=kb_finish_preview())
    photos.append(message.photo[-1].file_id)
    await state.update_data(photos=photos)
    await message.answer(f"✅ Фото сохранено ({len(photos)}/9). Ещё? Или «Сформировать предпросмотр».", reply_markup=kb_finish_preview())

@r_admin.callback_query(F.data == "finish_add", StateFilter(AddListing.photos))
async def finish_add_cb(call: CallbackQuery, state: FSMContext):
    await build_preview(call.message, state)
    await call.answer()

@r_admin.message(Command("done"), StateFilter(AddListing.photos))
async def finish_add_cmd(message: Message, state: FSMContext):
    await build_preview(message, state)

# ── Отмена добавления
@r_admin.callback_query(F.data == "cancel_add")
async def cancel_add(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("❌ Добавление отменено.")
    await call.answer()

# ── Сбор предпросмотра
# ── Сбор предпросмотра
async def build_preview(message: Message, state: FSMContext):
    data = await state.get_data()
    listing_id   = data.get("listing_id")
    channel_text = data.get("channel_text", "")
    orig_text    = data.get("orig_text", "")
    link         = data.get("link", "")
    post_url     = data.get("post_url", "")
    deliver      = data.get("deliver_mode", "TEXT")
    photos: List[str] = data.get("photos", []) or []

    if not (listing_id and channel_text and link and deliver):
        return await message.answer("⚠️ Не хватает данных (ID/текст/контакт/режим). Начни заново: /add A101")

    # сохраняем/обновляем как DRAFT
    db_upsert(
        listing_id=listing_id,
        channel_text=channel_text,
        link=link,
        post_url=post_url,
        deliver_mode=deliver,
        orig_text=orig_text,
        photos=photos,
        status="DRAFT"
    )

    # показываем предпросмотр медиа + текста
    if photos:
        media = []
        for i, p in enumerate(photos):
            if i == 0 and len(channel_text) <= 900:
                media.append(InputMediaPhoto(media=p, caption=channel_text))
            else:
                media.append(InputMediaPhoto(media=p))
        await bot.send_media_group(chat_id=message.chat.id, media=media)
        if len(channel_text) > 900:
            await message.answer(channel_text)
    else:
        await message.answer(channel_text)

    # сводка перед публикацией
    await message.answer(
        f"ID: {listing_id}\n"
        f"Что получит покупатель: текст оригинала + контакт{(' + ссылка на оригинал' if post_url else '')}\n"
        f"Контакт: {link}\n"
        f"{'Оригинал: ' + post_url if post_url else 'Оригинал: —'}",
        reply_markup=kb_preview(listing_id)
    )

    await state.clear()

# ── Публикация в канал
@r_admin.callback_query(F.data.startswith("publish:"))
async def publish_listing(call: CallbackQuery):
    listing_id = call.data.split(":", 1)[1]
    row = db_get(listing_id)
    if not row:
        await call.message.answer("⚠️ Объявление не найдено в БД.")
        return await call.answer()

    channel_text, _link, _post_url, _deliver, _orig_text, photos_json, _status = row
    try:
        photos: List[str] = json.loads(photos_json) if photos_json else []
    except Exception:
        photos = []

    btn = kb_deeplink(listing_id)
    caption_text = channel_text or ""

    # ── Отправка в канал
    try:
        if photos:
            # ====== С ФОТО ======
            if len(photos) == 1:
                # 1 фото → можно прикрепить клавиатуру сразу к фото,
                # если подпись не длиннее лимита Telegram (1024)
                if len(caption_text) <= 1024:
                    await bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=photos[0],
                        caption=caption_text,
                        reply_markup=btn
                    )
                else:
                    # Длинная подпись → отправляем текст с кнопкой отдельным сообщением,
                    # а фото — без кнопки.
                    await bot.send_message(chat_id=CHANNEL_ID, text=caption_text, reply_markup=btn)
                    await bot.send_photo(chat_id=CHANNEL_ID, photo=photos[0])
            else:
                # Альбом: send_media_group НЕ поддерживает клавиатуры.
                # Сначала отправляем все фото…
                media = []
                # можно положить короткую подпись на 1-е фото (если влазит), но кнопка всё равно отдельно
                first_caption = caption_text if len(caption_text) <= 1024 else ""
                media.append(InputMediaPhoto(media=photos[0], caption=first_caption))
                media += [InputMediaPhoto(media=p) for p in photos[1:]]
                await bot.send_media_group(chat_id=CHANNEL_ID, media=media)

                # …а затем отдельным сообщением отправляем полный текст и кнопку
                # (так кнопка точно появится под постом в канале).
                await bot.send_message(chat_id=CHANNEL_ID, text=caption_text, reply_markup=btn)
        else:
            # ====== БЕЗ ФОТО ======
            await bot.send_message(chat_id=CHANNEL_ID, text=caption_text, reply_markup=btn)

        # статус: опубликовано
        db_set_status(listing_id, "PUBLISHED")
        await call.message.answer(f"✅ Объявление {listing_id} опубликовано.")
    except Exception as e:
        logging.exception("publish_listing failed")
        await call.message.answer(f"⚠️ Не удалось опубликовать: {e}")

    await call.answer()

@r_admin.callback_query(F.data == "restart")
async def restart_add(call: CallbackQuery):
    await call.message.answer("🔄 Начнём сначала. Укажи новый ID: /add A102")
    await call.answer()

@r_admin.message(Command("publish"))
async def publish_cmd(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.answer("Укажи ID: /publish A101")
    fake_call = type("C", (), {"data": f"publish:{parts[1].strip().upper()}", "message": message})
    await publish_listing(fake_call)  # type: ignore

# ── Удаление с подтверждением
async def _ask_delete_confirmation(chat_id: int, listing_id: str, preview_text: str):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"confirm_del:{listing_id}"),
         InlineKeyboardButton(text="❌ Отмена",  callback_data="cancel_del")]
    ])
    short = (preview_text[:300] + "…") if len(preview_text) > 300 else preview_text
    await bot.send_message(chat_id, f"Удалить ID **{listing_id}**?\n\n_{short}_", reply_markup=kb, parse_mode="Markdown")

@r_admin.message(Command("delete"))
async def delete_cmd(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.answer("⚠️ Укажи ID после команды: `/delete A101`", parse_mode="Markdown")
    listing_id = parts[1].strip().upper()
    row = db_get(listing_id)
    if not row:
        return await message.answer(f"⚠️ Объявление {listing_id} не найдено.")
    channel_text, *_ = row
    await _ask_delete_confirmation(message.chat.id, listing_id, channel_text)

@r_admin.message(Command("del"))
async def del_alias(message: Message):
    await delete_cmd(message)

@r_admin.callback_query(F.data.startswith("confirm_del:"))
async def confirm_delete(call: CallbackQuery):
    listing_id = call.data.split(":", 1)[1]
    ok = db_delete(listing_id)
    if ok:
        try:
            await call.message.edit_text(f"🗑 Объявление {listing_id} удалено.")
        except Exception:
            await call.message.answer(f"🗑 Объявление {listing_id} удалено.")
    else:
        try:
            await call.message.edit_text(f"⚠️ Объявление {listing_id} не найдено (возможно, уже удалено).")
        except Exception:
            await call.message.answer(f"⚠️ Объявление {listing_id} не найдено (возможно, уже удалено).")
    await call.answer()

@r_admin.callback_query(F.data == "cancel_del")
async def cancel_delete(call: CallbackQuery):
    await call.answer("Отмена")
    try:
        await call.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

# ── Отладка состояния
@r_admin.message(Command("dbg"))
async def dbg_state(m: Message, state: FSMContext):
    st = await state.get_state()
    data = await state.get_data()
    await m.answer(f"🧪 state = {st}\n\ndata = {data}")

# ── main: запуск поллинга
async def main():
    global BOT_USERNAME
    db_init()
    me = await bot.get_me()
    BOT_USERNAME = me.username
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
# ── main: запуск поллинга
# async def main():
#     global BOT_USERNAME
#     db_init()
#     me = await bot.get_me()
#     BOT_USERNAME = me.username
#     await dp.start_polling(bot)

# if __name__ == "__main__":
#     import asyncio
#     asyncio.run(main())