import os
import re
import sys
import asyncio
import logging
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ============ CONFIGURATION ============

# Yahan apna Telegram Bot Token daalo
BOT_TOKEN = "8612093910:AAEUllxHDLp5SC0TqtCYiNK-C9rzCzff234"

# Yahan apna Telegram numeric User ID daalo
OWNER_ID = 6146027296
# Project settings
BASE_DIR = Path("python_projects").resolve()
BASE_DIR.mkdir(parents=True, exist_ok=True)

MAX_FILE_SIZE = 2 * 1024 * 1024  # 2 MB


logging.basicConfig(level=logging.INFO)
log = logging.getLogger("PrabhanjanHost")

# One active project per owner in this starter version.
projects = {}
pending_files = {}
pending_port = {}


def is_owner(update):
    return (
        update.effective_user is not None
        and OWNER_ID != 0
        and update.effective_user.id == OWNER_ID
    )


async def say(update, text, **kwargs):
    if update.effective_message:
        await update.effective_message.reply_text(text, **kwargs)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return await say(update, "Access denied.")

    keyboard = [
        [InlineKeyboardButton("📤 Upload Python", callback_data="upload")],
        [
            InlineKeyboardButton("📊 Status", callback_data="status"),
            InlineKeyboardButton("📄 Logs", callback_data="logs"),
        ],
        [InlineKeyboardButton("🛑 Stop", callback_data="stop")],
    ]

    await say(
        update,
        "🚀 PRABHANJAN EXPLOITS\n"
        "PYTHON HOSTING BOT\n\n"
        "1. Upload a .py file.\n"
        "2. Enter the local web-server port, e.g. 5000.\n"
        "3. Enter 0 for a normal Python script without a tunnel.\n\n"
        "Commands: /status /logs /stop",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def button_handler(update, context):
    query = update.callback_query
    await query.answer()

    if not is_owner(update):
        return await query.edit_message_text("Access denied.")

    if query.data == "upload":
        await query.edit_message_text(
            "📤 Ab apni .py file Telegram par document ke roop mein upload karein."
        )
    elif query.data == "status":
        await status(update, context)
    elif query.data == "logs":
        await logs(update, context)
    elif query.data == "stop":
        await stop(update, context)


async def receive_file(update: Update, context):
    if not is_owner(update):
        return

    doc = update.message.document
    if not doc.file_name or not doc.file_name.lower().endswith(".py"):
        return await say(update, "Sirf .py files upload karein.")

    if doc.file_size and doc.file_size > MAX_FILE_SIZE:
        return await say(update, "File limit 2 MB hai.")

    user_id = update.effective_user.id
    folder = BASE_DIR / str(user_id)
    folder.mkdir(parents=True, exist_ok=True)

    # Use a fixed filename instead of trusting uploaded paths.
    script_path = folder / "uploaded_script.py"
    tg_file = await doc.get_file()
    await tg_file.download_to_drive(custom_path=str(script_path))

    pending_files[user_id] = script_path

    await say(
        update,
        "✅ Python file upload ho gayi.\n\n"
        "Agar Flask/FastAPI ya koi localhost web app hai, "
        "to uska port bhejein (example: 5000).\n\n"
        "Normal script ke liye 0 bhejein.\n"
        "Note: Web server ko 0.0.0.0 ya 127.0.0.1 par "
        "usi port par listen karna chahiye."
    )


async def receive_port(update: Update, context):
    if not is_owner(update):
        return

    user_id = update.effective_user.id
    if user_id not in pending_files:
        return

    try:
        port = int(update.message.text.strip())
        if port != 0 and not 1024 <= port <= 65535:
            raise ValueError
    except ValueError:
        return await say(update, "Port 1024-65535 ke beech dein, ya normal script ke liye 0.")

    # Stop an older project before replacing it.
    await stop_project(user_id)

    script_path = pending_files.pop(user_id)
    folder = script_path.parent
    log_path = folder / "output.log"

    try:
        log_file = open(log_path, "w", encoding="utf-8")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", str(script_path),
            cwd=str(folder),
            stdout=log_file,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception as exc:
        log_file.close()
        return await say(update, f"❌ Python start nahi hua: {exc}")

    project = {
        "proc": proc,
        "log_file": log_file,
        "log_path": log_path,
        "port": port,
        "tunnel": None,
        "url": None,
    }
    projects[user_id] = project

    await say(update, f"⏳ Python script start ho rahi hai. PID: {proc.pid}")

    if port == 0:
        return await say(
            update,
            "✅ Script start kar di.\n"
            "🌐 Public URL: N/A (normal script)\n"
            "Status check: /status\nLogs: /logs"
        )

    # Give the local web server time to start.
    await asyncio.sleep(3)

    if proc.returncode is not None:
        return await say(
            update,
            "❌ Script jaldi exit ho gayi. /logs bhejkar error check karein."
        )

    # Cloudflared must already be installed on this machine.
    try:
        tunnel = await asyncio.create_subprocess_exec(
            "cloudflared", "tunnel",
            "--no-autoupdate",
            "--url", f"http://127.0.0.1:{port}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        project["tunnel"] = tunnel

        async def read_tunnel_output():
            while True:
                line = await tunnel.stdout.readline()
                if not line:
                    break

                text = line.decode(errors="replace").strip()
                log.info("cloudflared: %s", text)

                match = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", text)
                if match and not project["url"]:
                    project["url"] = match.group(0)
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"🌍 Public URL ready:\n{project['url']}\n\n"
                             f"Status: /status\nLogs: /logs\nStop: /stop"
                    )

        project["tunnel_reader"] = asyncio.create_task(read_tunnel_output())

        # Wait briefly for Cloudflare to print its URL.
        for _ in range(20):
            if project["url"]:
                break
            if tunnel.returncode is not None:
                break
            await asyncio.sleep(1)

        if not project["url"]:
            await say(
                update,
                "⚠️ Tunnel start hua, lekin URL abhi nahi mila.\n"
                "Cloudflared installation/network check karein."
            )

    except FileNotFoundError:
        await say(
            update,
            "❌ cloudflared install nahi hai ya PATH mein nahi mila.\n"
            "Cloudflare Tunnel install karke bot dobara chalayein."
        )
    except Exception as exc:
        await say(update, f"❌ Tunnel error: {exc}")


async def status(update, context):
    if not is_owner(update):
        return
    p = projects.get(update.effective_user.id)

    if not p:
        return await say(update, "Koi active project nahi hai.")

    proc = p["proc"]
    running = proc.returncode is None
    message = (
        f"Python: {'RUNNING' if running else 'STOPPED'}\n"
        f"PID: {proc.pid}\nPort: {p['port']}\n"
        f"Tunnel: {'RUNNING' if p['tunnel'] and p['tunnel'].returncode is None else 'OFF'}\n"
        f"Public URL: {p['url'] or 'Not available'}"
    )
    await say(update, message)


async def logs(update, context):
    if not is_owner(update):
        return

    p = projects.get(update.effective_user.id)
    if not p:
        return await say(update, "Koi project nahi hai.")

    try:
        content = p["log_path"].read_text(errors="replace")[-3000:]
    except Exception:
        content = "Logs abhi available nahi hain."

    await say(update, "📄 Latest logs:\n\n" + content[-3800:])


async def stop_project(user_id):
    p = projects.pop(user_id, None)
    if not p:
        return

    tunnel = p.get("tunnel")
    proc = p.get("proc")

    if tunnel and tunnel.returncode is None:
        tunnel.terminate()
        try:
            await asyncio.wait_for(tunnel.wait(), timeout=5)
        except asyncio.TimeoutError:
            tunnel.kill()
            await tunnel.wait()

    reader = p.get("tunnel_reader")
    if reader and not reader.done():
        reader.cancel()

    if proc and proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

    try:
        p["log_file"].close()
    except Exception:
        pass


async def stop(update, context):
    if not is_owner(update):
        return
    await stop_project(update.effective_user.id)
    await say(update, "🛑 Project stop kar diya.")


async def error_handler(update, context):
    log.exception("Telegram handler error", exc_info=context.error)


def main():
    if not BOT_TOKEN or OWNER_ID == 0:
        raise SystemExit(
            "TELEGRAM_BOT_TOKEN aur OWNER_ID environment variables set karein."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("logs", logs))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, receive_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_port))
    app.add_error_handler(error_handler)

    print("Prabhanjan Python Hosting Bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
