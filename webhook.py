import os
import asyncio
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from fastapi import FastAPI, Request
import uvicorn

from bot import dp, bot   # импортируем бота и диспетчер из bot.py

WEBHOOK_PATH = f"/webhook/{os.getenv('BOT_TOKEN')}"
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL") + WEBHOOK_PATH

app = FastAPI()

@app.on_event("startup")
async def on_startup():
    # Устанавливаем вебхук при запуске
    await bot.set_webhook(WEBHOOK_URL)

@app.post(WEBHOOK_PATH)
async def webhook_handler(request: Request):
    update = await request.json()
    await dp.feed_webhook_update(bot, update)
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run("webhook:app", host="0.0.0.0", port=int(os.getenv("PORT", 10000)))