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
from video_fetch import MOODS

load_dotenv(os.path.join(BASE, ".env"))
TOKEN = (os.getenv("BOT_TOKEN") or "").strip()

JOBS = {}
LOCK = asyncio.Lock()

WELCOME = (
    "рџЋ¬ <b>Edit-Р±РѕС‚ Pro</b>\n\n"
    "РџСЂРёС€Р»Рё С‚СЂРµРє (mp3, m4a, ogg, wav РёР»Рё РіРѕР»РѕСЃРѕРІРѕРµ) вЂ” СЏ СЃРѕР±РµСЂСѓ РїРѕРґ РЅРµРіРѕ "
    "РїСЂРѕС„РµСЃСЃРёРѕРЅР°Р»СЊРЅС‹Р№ РІРµСЂС‚РёРєР°Р»СЊРЅС‹Р№ VIDEO-edit: РЅР°СЂРµР·РєР° РєР°РґСЂРѕРІ СЃС‚СЂРѕРіРѕ "
    "РїРѕ Р±РёС‚Р°Рј Рё СѓРґР°СЂР°Рј, РґРёРЅР°РјРёС‡РЅС‹Рµ РїРµСЂРµС…РѕРґС‹ СЃРѕ РІСЃРїС‹С€РєР°РјРё, РєРёРЅРµРјР°С‚РёС‡РЅС‹Р№ "
    "С†РІРµС‚, РїРѕ Р¶РµР»Р°РЅРёСЋ вЂ” С‚РµРєСЃС‚ РїРµСЃРЅРё kinetic-СЃСѓР±С‚РёС‚СЂР°РјРё.\n\n"
    "Р”Р°Р»СЊС€Рµ: РІС‹Р±РµСЂРё РЅР°СЃС‚СЂРѕРµРЅРёРµ в†’ В«РЎРѕР±СЂР°С‚СЊВ».\n"
    "Р›РёРјРёС‚ С„Р°Р№Р»Р°: 20 РњР‘, РґР»РёРЅР° РґРѕ 11 РјРёРЅСѓС‚."
)


def kb(uid):
    job = JOBS.get(uid, {})
    mood = job.get("mood", "dark")
    lyr = job.get("lyrics", False)
    keys = list(MOODS.items())
    rows = [
        [InlineKeyboardButton(text=("вњ… " if mood == k else "") + v["label"],
                              callback_data=f"mood:{k}") for k, v in keys[:2]],
        [InlineKeyboardButton(text=("вњ… " if mood == k else "") + v["label"],
                              callback_data=f"mood:{k}") for k, v in keys[2:4]],
        [InlineKeyboardButton(text=("рџ“ќ РўРµРєСЃС‚: " + ("РІРєР»" if lyr else "РІС‹РєР»")),
                              callback_data="lyr")],
        [InlineKeyboardButton(text="рџЋ¬ РЎРѕР±СЂР°С‚СЊ", callback_data="go")],
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
                safe_edit(prog, f"вЏі {max(p, 0)}% вЂ” {stage}"), loop)

    try:
        async with LOCK:
            res = await asyncio.to_thread(
                editor.build, snap["path"], snap["dir"], snap["mood"],
                snap["lyrics"], progress,
                os.path.join(BASE, "_pin_cache", snap["mood"]))
        state["done"] = True
        await prog.edit_text(
            f"вњ… Р“РѕС‚РѕРІРѕ В· {res['tempo']:.0f} BPM В· {int(res['duration'])} СЃРµРє")
        cap = f"рџЋ¬ {snap['title'] or 'edit'} В· {res['tempo']:.0f} BPM"
        if snap["lyrics"]:
            cap += " В· СЃ С‚РµРєСЃС‚РѕРј"
        outs = res.get("outs") or [res["out"]]
        if len(outs) == 1:
            await prog.answer_video(FSInputFile(outs[0]), caption=cap[:250])
        else:
            await prog.answer(
                f"рџ“¦ Р Р°Р·Р±РёР» РЅР° {len(outs)} С‡Р°СЃС‚РµР№ (РїРѕ в‰¤48 РњР‘):")
            for i, p in enumerate(outs, 1):
                await prog.answer_video(
                    FSInputFile(p),
                    caption=f"{cap[:200]} В· С‡Р°СЃС‚СЊ {i}/{len(outs)}")
    except Exception as e:
        state["done"] = True
        traceback.print_exc()
        try:
            await prog.edit_text(f"вќЊ РћС€РёР±РєР°: {e}")
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
        await message.reply("Р¤Р°Р№Р» Р±РѕР»СЊС€Рµ 19 РњР‘ вЂ” Telegram РЅРµ РґР°СЃС‚ РµРіРѕ СЃРєР°С‡Р°С‚СЊ. "
                            "РЎРѕР¶РјРё С‚СЂРµРє Рё РїСЂРёС€Р»Рё СЃРЅРѕРІР°.")
        return

    uid = message.from_user.id
    prev = JOBS.get(uid)
    if prev and prev.get("busy"):
        await message.reply("Р•С‰С‘ СЃРѕР±РёСЂР°СЋ С‚РІРѕР№ РїСЂРµРґС‹РґСѓС‰РёР№ РєР»РёРї, РґРѕР¶РґРёСЃСЊ РѕРєРѕРЅС‡Р°РЅРёСЏ.")
        return
    if prev and prev.get("dir"):
        shutil.rmtree(prev["dir"], ignore_errors=True)
    jobdir = os.path.join(BASE, "jobs", f"{uid}_{uuid.uuid4().hex[:6]}")
    os.makedirs(jobdir, exist_ok=True)
    ext = guess_ext(file, getattr(file, "mime_type", None))
    path = os.path.join(jobdir, f"track.{ext}")

    try:
        tg_file = await bot.get_file(file.file_id)
        await bot.download_file(tg_file.file_path, destination=path)
    except Exception as e:
        shutil.rmtree(jobdir, ignore_errors=True)
        await message.reply(f"РќРµ СЃРјРѕРі СЃРєР°С‡Р°С‚СЊ С„Р°Р№Р»: {e}")
        return

    title = None
    if message.audio:
        title = message.audio.title or message.audio.file_name
    elif getattr(file, "file_name", None):
        title = file.file_name

    JOBS[uid] = {"dir": jobdir, "path": path, "mood": "dark",
                 "lyrics": False, "title": title, "busy": False}
    await message.reply("РўСЂРµРє РїСЂРёРЅСЏС‚. Р’С‹Р±РёСЂР°Р№ РЅР°СЃС‚СЂРѕРµРЅРёРµ рџ‘‡", reply_markup=kb(uid))


async def cb_mood(cb: CallbackQuery):
    uid = cb.from_user.id
    job = JOBS.get(uid)
    if not job:
        await cb.answer("РЎРЅР°С‡Р°Р»Р° РїСЂРёС€Р»Рё С‚СЂРµРє", show_alert=True)
        return
    if job.get("busy"):
        await cb.answer("РЈР¶Рµ СЃРѕР±РёСЂР°СЋ СЌС‚РѕС‚ С‚СЂРµРє", show_alert=True)
        return
    job["mood"] = cb.data.split(":", 1)[1]
    await cb.answer("РћРє")
    try:
        await cb.message.edit_reply_markup(reply_markup=kb(uid))
    except Exception:
        pass


async def cb_lyr(cb: CallbackQuery):
    uid = cb.from_user.id
    job = JOBS.get(uid)
    if not job:
        await cb.answer("РЎРЅР°С‡Р°Р»Р° РїСЂРёС€Р»Рё С‚СЂРµРє", show_alert=True)
        return
    if job.get("busy"):
        await cb.answer("РЈР¶Рµ СЃРѕР±РёСЂР°СЋ СЌС‚РѕС‚ С‚СЂРµРє", show_alert=True)
        return
    job["lyrics"] = not job["lyrics"]
    await cb.answer("РўРµРєСЃС‚: " + ("РІРєР»" if job["lyrics"] else "РІС‹РєР»"))
    try:
        await cb.message.edit_reply_markup(reply_markup=kb(uid))
    except Exception:
        pass


async def cb_go(cb: CallbackQuery):
    uid = cb.from_user.id
    job = JOBS.get(uid)
    if not job:
        await cb.answer("РЎРЅР°С‡Р°Р»Р° РїСЂРёС€Р»Рё С‚СЂРµРє", show_alert=True)
        return
    if job.get("busy"):
        await cb.answer("РЈР¶Рµ СЃРѕР±РёСЂР°СЋ, РїРѕРґРѕР¶РґРё", show_alert=True)
        return
    job["busy"] = True
    await cb.answer()
    snap = dict(job)
    try:
        prog = await cb.message.edit_text(
            "вЏі 0% вЂ” Р“РѕС‚РѕРІР»СЋ Р°СѓРґРёРѕвЂ¦", reply_markup=None)
    except Exception:
        prog = await cb.message.answer("вЏі 0% вЂ” Р“РѕС‚РѕРІР»СЋ Р°СѓРґРёРѕвЂ¦")
    asyncio.create_task(run_job(uid, snap, prog))


def main():
    if not TOKEN:
        print("РќРµС‚ BOT_TOKEN. РЎРѕР·РґР°Р№ Р±РѕС‚Р° Сѓ @BotFather Рё Р·Р°РїРёС€Рё С‚РѕРєРµРЅ РІ С„Р°Р№Р» .env:")
        print(f"  {os.path.join(BASE, '.env')}")
        print("  BOT_TOKEN=123456:ABC-DEF...")
        sys.exit(1)

    while True:
        try:
            bot = Bot(TOKEN)
            dp = Dispatcher()
            dp.message.register(start, CommandStart())
            dp.message.register(start, Command("help"))
            dp.message.register(on_audio, F.audio | F.voice | F.document)
            dp.callback_query.register(cb_mood, F.data.startswith("mood:"))
            dp.callback_query.register(cb_lyr, F.data == "lyr")
            dp.callback_query.register(cb_go, F.data == "go")

            print("tg_edit_bot Р·Р°РїСѓС‰РµРЅ (polling)вЂ¦", flush=True)
            asyncio.run(dp.start_polling(bot))
            break
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            msg = str(e)
            print("polling error:", msg, flush=True)
            if "Conflict" in msg or "terminated by other" in msg:
                print("Р”СЂСѓРіРѕР№ РёРЅСЃС‚Р°РЅСЃ Р±РѕС‚Р° СѓР¶Рµ Р·Р°РїСѓС‰РµРЅ вЂ” Р·Р°РІРµСЂС€Р°СЋСЃСЊ.")
                sys.exit(0)
            try:
                asyncio.run(asyncio.sleep(10))
            except RuntimeError:
                time.sleep(10)


if __name__ == "__main__":
    main()

