import asyncio
import os
import shutil
import sys
import time
import traceback
import uuid

from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import editor
from pinterest_fetch import MOODS

load_dotenv(os.path.join(BASE, ".env"))
TOKEN = (os.getenv("BOT_TOKEN") or "").strip()

JOBS = {}
LOCK = asyncio.Lock()

WELCOME = (
    "🎬 <b>Edit-бот</b>\n\n"
    "Пришли трек (mp3, m4a, ogg, wav или голосовое) — я соберу под него "
    "вертикальный клип: нарезка строго по битам, картинки с Pinterest, "
    "вспышки на склейках, можно ещё и текст песни субтитрами.\n\n"
    "Дальше: выбери настроение → «Собрать».\n"
    "Лимит файла: 20 МБ, длина до 11 минут."
)


def kb(uid):
    job = JOBS.get(uid, {})
    mood = job.get("mood", "dark")
    lyr = job.get("lyrics", False)
    keys = list(MOODS.items())
    rows = [
        [InlineKeyboardButton(text=("✅ " if mood == k else "") + v["label"],
                              callback_data=f"mood:{k}") for k, v in keys[:2]],
        [InlineKeyboardButton(text=("✅ " if mood == k else "") + v["label"],
                              callback_data=f"mood:{k}") for k, v in keys[2:4]],
        [InlineKeyboardButton(text=("📝 Текст: " + ("вкл" if lyr else "выкл")),
                              callback_data="lyr")],
        [InlineKeyboardButton(text="🎬 Собрать", callback_data="go")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def guess_ext(file, mime):
    name = getattr(file, "file_name", None) or ""
    e = os.path.splitext(name)[1].lower().lstrip(".")
    if e:
        return e
    return {"audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a",
            "audio/x-m4a": "m4a", "audio/ogg": "ogg", "audio/wav": "wav",
            "audio/x-wav": "wav", "audio/flac": "flac",
            "audio/webm": "weba", "audio/opus": "ogg"}.get(mime or "", "mp3")


async def safe_edit(msg, text):
    try:
        await msg.edit_text(text)
    except TelegramBadRequest:
        pass
    except Exception:
        pass


async def run_job(uid, snap, prog):
    loop = asyncio.get_running_loop()
    state = {"last": 0.0, "pct": -1, "done": False}

    def progress(p, stage):
        if state["done"]:
            return
        if p is None:
            p = state["pct"]
        else:
            state["pct"] = p
        now = time.time()
        if p >= 100 or now - state["last"] >= 1.5:
            state["last"] = now
            asyncio.run_coroutine_threadsafe(
                safe_edit(prog, f"⏳ {max(p, 0)}% — {stage}"), loop)

    try:
        async with LOCK:
            res = await asyncio.to_thread(
                editor.build, snap["path"], snap["dir"], snap["mood"],
                snap["lyrics"], progress,
                os.path.join(BASE, "_pin_cache", snap["mood"]))
        state["done"] = True
        await prog.edit_text(
            f"✅ Готово · {res['tempo']:.0f} BPM · {int(res['duration'])} сек")
        cap = f"🎬 {snap['title'] or 'edit'} · {res['tempo']:.0f} BPM"
        if snap["lyrics"]:
            cap += " · с текстом"
        await prog.answer_video(FSInputFile(res["out"]), caption=cap[:250])
    except Exception as e:
        state["done"] = True
        traceback.print_exc()
        try:
            await prog.edit_text(f"❌ Ошибка: {e}")
        except Exception:
            pass
    finally:
        cur = JOBS.get(uid)
        if cur and cur.get("dir") == snap["dir"]:
            cur["busy"] = False
        shutil.rmtree(snap["dir"], ignore_errors=True)


async def start(message: Message):
    await message.answer(WELCOME, parse_mode="HTML")


async def on_audio(message: Message, bot: Bot):
    file = None
    if message.audio:
        file = message.audio
    elif message.voice:
        file = message.voice
    elif message.document and (message.document.mime_type or "").startswith("audio"):
        file = message.document
    if not file:
        return

    if (file.file_size or 0) > 19 * 1024 * 1024:
        await message.reply("Файл больше 19 МБ — Telegram не даст его скачать. "
                            "Сожми трек и пришли снова.")
        return

    uid = message.from_user.id
    jobdir = os.path.join(BASE, "jobs", f"{uid}_{uuid.uuid4().hex[:6]}")
    os.makedirs(jobdir, exist_ok=True)
    ext = guess_ext(file, getattr(file, "mime_type", None))
    path = os.path.join(jobdir, f"track.{ext}")

    try:
        tg_file = await bot.get_file(file.file_id)
        await bot.download_file(tg_file.file_path, destination=path)
    except Exception as e:
        shutil.rmtree(jobdir, ignore_errors=True)
        await message.reply(f"Не смог скачать файл: {e}")
        return

    title = None
    if message.audio:
        title = message.audio.title or message.audio.file_name
    elif getattr(file, "file_name", None):
        title = file.file_name

    JOBS[uid] = {"dir": jobdir, "path": path, "mood": "dark",
                 "lyrics": False, "title": title, "busy": False}
    await message.reply("Трек принят. Выбирай настроение 👇", reply_markup=kb(uid))


async def cb_mood(cb: CallbackQuery):
    uid = cb.from_user.id
    job = JOBS.get(uid)
    if not job:
        await cb.answer("Сначала пришли трек", show_alert=True)
        return
    if job.get("busy"):
        await cb.answer("Уже собираю этот трек", show_alert=True)
        return
    job["mood"] = cb.data.split(":", 1)[1]
    await cb.answer("Ок")
    try:
        await cb.message.edit_reply_markup(reply_markup=kb(uid))
    except Exception:
        pass


async def cb_lyr(cb: CallbackQuery):
    uid = cb.from_user.id
    job = JOBS.get(uid)
    if not job:
        await cb.answer("Сначала пришли трек", show_alert=True)
        return
    if job.get("busy"):
        await cb.answer("Уже собираю этот трек", show_alert=True)
        return
    job["lyrics"] = not job["lyrics"]
    await cb.answer("Текст: " + ("вкл" if job["lyrics"] else "выкл"))
    try:
        await cb.message.edit_reply_markup(reply_markup=kb(uid))
    except Exception:
        pass


async def cb_go(cb: CallbackQuery):
    uid = cb.from_user.id
    job = JOBS.get(uid)
    if not job:
        await cb.answer("Сначала пришли трек", show_alert=True)
        return
    if job.get("busy"):
        await cb.answer("Уже собираю, подожди", show_alert=True)
        return
    job["busy"] = True
    await cb.answer()
    snap = dict(job)
    try:
        prog = await cb.message.edit_text(
            "⏳ 0% — Готовлю аудио…", reply_markup=None)
    except Exception:
        prog = await cb.message.answer("⏳ 0% — Готовлю аудио…")
    asyncio.create_task(run_job(uid, snap, prog))


def main():
    if not TOKEN:
        print("Нет BOT_TOKEN. Создай бота у @BotFather и запиши токен в файл .env:")
        print(f"  {os.path.join(BASE, '.env')}")
        print("  BOT_TOKEN=123456:ABC-DEF...")
        sys.exit(1)

    bot = Bot(TOKEN)
    dp = Dispatcher()
    dp.message.register(start, CommandStart())
    dp.message.register(start, Command("help"))
    dp.message.register(on_audio, F.audio | F.voice | F.document)
    dp.callback_query.register(cb_mood, F.data.startswith("mood:"))
    dp.callback_query.register(cb_lyr, F.data == "lyr")
    dp.callback_query.register(cb_go, F.data == "go")

    print("tg_edit_bot запущен (polling)…")
    asyncio.run(dp.start_polling(bot))


if __name__ == "__main__":
    main()
