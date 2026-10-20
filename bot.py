
import os
import asyncio
import sys
from pathlib import Path

from aiohttp import web
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)
from pyngrok import ngrok

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
NGROK_TOKEN = os.environ["NGROK_AUTHTOKEN"]

BASE = Path("/tmp/my_python_project")
BASE.mkdir(parents=True, exist_ok=True)

script_path = None
project_process = None
public_url = None
busy = False


def is_owner(update):
    return update.effective_user and update.effective_user.id == OWNER_ID


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    await update.message.reply_text(
        "PRABHANJAN EXPLOITS\n\n"
        "Upload a .py file and optionally requirements.txt.\n\n"
        "/status - Check project\n"
        "/run - Install requirements and start\n"
        "/stop - Stop project and tunnel\n"
        "/url - Show public URL"
    )


async def receive_file(update: Update,
                       context: ContextTypes.DEFAULT_TYPE):
    global script_path

    if not is_owner(update):
        return

    doc = update.message.document
    name = Path(doc.file_name or "").name

    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        await update.message.reply_text("File limit: 5 MB.")
        return

    if name not in ("main.py", "app.py", "requirements.txt"):
        await update.message.reply_text(
            "Allowed files: main.py, app.py, requirements.txt"
        )
        return

    # Do not replace a running project.
    if project_process and project_process.returncode is None:
        await update.message.reply_text(
            "Stop the current project before uploading new files."
        )
        return

    target = BASE / name
    telegram_file = await doc.get_file()
    await telegram_file.download_to_drive(custom_path=str(target))

    if name in ("main.py", "app.py"):
        script_path = target

    await update.message.reply_text(f"Uploaded: {name}")


async def run_project(update: Update,
                      context: ContextTypes.DEFAULT_TYPE):
    global project_process, public_url, busy

    if not is_owner(update):
        return

    if busy:
        await update.message.reply_text("A task is already running.")
        return

    if project_process and project_process.returncode is None:
        await update.message.reply_text("Project is already running.")
        return

    if script_path is None or not script_path.exists():
        await update.message.reply_text(
            "Upload main.py or app.py first."
        )
        return

    busy = True
    await update.message.reply_text("Preparing project...")

    try:
        req = BASE / "requirements.txt"

        if req.exists():
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install",
                "--disable-pip-version-check", "-r", str(req),
                cwd=str(BASE),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            try:
                output, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=180
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                await update.message.reply_text(
                    "Dependency installation timed out."
                )
                return

            if proc.returncode != 0:
                await update.message.reply_text(
                    "Dependency installation failed. Check package names."
                )
                return

        env = os.environ.copy()
        env["PORT"] = "8000"
        env["HOST"] = "0.0.0.0"

        project_process = await asyncio.create_subprocess_exec(
            sys.executable, str(script_path),
            cwd=str(BASE), env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )

        await asyncio.sleep(3)

        if project_process.returncode is not None:
            await update.message.reply_text(
                "Project exited early. Check its entry point and logs."
            )
            return

        def open_tunnel():
            ngrok.set_auth_token(NGROK_TOKEN)
            return ngrok.connect(8000, "http").public_url

        public_url = await asyncio.to_thread(open_tunnel)

        await update.message.reply_text(
            f"Project started.\nPublic URL: {public_url}\n"
            "Your app must listen on 0.0.0.0:8000."
        )

    except Exception as exc:
        await update.message.reply_text(
            f"Could not start project: {type(exc).__name__}"
        )
    finally:
        busy = False


async def stop_project(update: Update,
                       context: ContextTypes.DEFAULT_TYPE):
    global project_process, public_url

    if not is_owner(update):
        return

    try:
        await asyncio.to_thread(ngrok.kill)
    except Exception:
        pass

    if project_process and project_process.returncode is None:
        project_process.terminate()
        try:
            await asyncio.wait_for(project_process.wait(), timeout=5)
        except asyncio.TimeoutError:
            project_process.kill()
            await project_process.wait()

    project_process = None
    public_url = None
    await update.message.reply_text("Project and tunnel stopped.")


async def show_url(update: Update,
                   context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    await update.message.reply_text(
        public_url or "No active public URL."
    )


async def status(update: Update,
                 context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return

    running = project_process and project_process.returncode is None
    await update.message.reply_text(
        f"Project: {'RUNNING' if running else 'STOPPED'}\n"
        f"Tunnel: {'ACTIVE' if public_url else 'INACTIVE'}"
    )


async def health(request):
    return web.json_response({"status": "ok"})


async def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("run", run_project))
    app.add_handler(CommandHandler("stop", stop_project))
    app.add_handler(CommandHandler("url", show_url))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(MessageHandler(
        filters.Document.ALL, receive_file
    ))

    # Render Web Service health endpoint
    health_app = web.Application()
    health_app.router.add_get("/", health)
    runner = web.AppRunner(health_app)
    await runner.setup()

    port = int(os.environ.get("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    async with app:
        await app.start()
        await app.updater.start_polling()
        try:
            await asyncio.Event().wait()
        finally:
            await app.updater.stop()
            await app.stop()
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
