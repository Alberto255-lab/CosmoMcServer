import asyncio
import os
import re
import shlex
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ===================== CONFIGURAZIONE =====================
TOKEN = os.environ.get("TELEGRAM_TOKEN", "INSERISCI_QUI_IL_TOKEN_DEL_BOT")

# ID Telegram degli admin (solo loro possono usare i comandi)
ADMIN_IDS = [
    123456789,  # <-- sostituisci con il tuo ID (usa @userinfobot per scoprirlo)
]

# Nome del Codespace (lo trovi con: gh codespace list)
CODESPACE_NAME = os.environ.get("CODESPACE_NAME", "INSERISCI_NOME_CODESPACE")

# Cartella DENTRO il Codespace dove si trova paper-1.21.8-60.jar
SERVER_DIR = os.environ.get("SERVER_DIR", "/workspaces/NOME-REPO")

SERVER_CMD = "java -Xmx4G -Xms4G -jar paper-1.21.8-60.jar nogui"
PLAYITD_CMD = f'playitd --secret "{os.environ.get("PLAYIT_SECRET", "")}"'
PLAYIT_CMD = "playit"

POLL_SECONDS = 5  # ogni quanto controllo lo stato del server
# ==========================================================
# NB: sulla macchina del bot serve la variabile d'ambiente GH_TOKEN
# (token GitHub con permesso "codespace") e la GitHub CLI installata.

HELP_TEXT = (
    "🤖 *Bot Minecraft pronto!*\n\n"
    "Comandi disponibili (solo admin):\n"
    "/startserver - Avvia il server Minecraft (ti avviso quando è pronto)\n"
    "/stopserver - Spegne il server (invia `stop`)\n"
    "/startplayit - Avvia `playitd` con il secret\n"
    "/playit - Avvia `playit` (ti inoltro eventuali link)\n"
    "/status - Mostra quali sessioni sono attive\n"
    "/help - Mostra questo messaggio\n\n"
    "ℹ️ Ogni volta che il server si spegne, va riavviato con /startserver."
)

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
URL_RE = re.compile(r"https?://[^\s\"'<>]+")
# Paper stampa: Done (12.345s)! For help, type "help"
DONE_RE = re.compile(r"Done(\s*\([\d.,]+s\))?!")

NO_SESSION = "__NOSESSION__"


# ===================== UTILITY =====================
def is_admin(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id in ADMIN_IDS


async def remote(cmd: str, timeout: int = 180):
    """Esegue un comando nel Codespace via `gh codespace ssh`. Ritorna (returncode, output)."""
    proc = await asyncio.create_subprocess_exec(
        "gh", "codespace", "ssh", "-c", CODESPACE_NAME, "--", cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timeout"
    return proc.returncode, ANSI_RE.sub("", out.decode(errors="ignore"))


async def session_exists(name: str) -> bool:
    rc, out = await remote(f"tmux has-session -t {name} 2>/dev/null && echo YES || echo NO")
    return rc == 0 and "YES" in out


async def start_session(name: str, command: str, cwd: str):
    """Crea una sessione tmux detached (= una 'bash' separata)."""
    rc, out = await remote(
        f"tmux new-session -d -s {name} -c {shlex.quote(cwd)} {shlex.quote(command)} && echo STARTED"
    )
    return rc == 0 and "STARTED" in out, out


# ===================== WATCHERS =====================
async def watch_server(app: Application, chat_id: int):
    """Controlla la sessione 'server': avvisa al 'Done!' e quando si spegne."""
    notified = False
    await asyncio.sleep(3)
    while True:
        rc, out = await remote(
            f"tmux has-session -t server 2>/dev/null && "
            f"tmux capture-pane -p -t server -S -300 || echo {NO_SESSION}"
        )
        if rc != 0:  # errore di connessione SSH: riprovo al giro dopo
            await asyncio.sleep(POLL_SECONDS)
            continue
        if NO_SESSION in out:
            await app.bot.send_message(
                chat_id, "🛑 Il server si è spento. Usa /startserver per riavviarlo."
            )
            return
        if not notified and DONE_RE.search(out):
            notified = True
            await app.bot.send_message(chat_id, "✅ Il server Minecraft è acceso e pronto!")
        await asyncio.sleep(POLL_SECONDS)


async def send_links(app: Application, chat_id: int, session: str, delay: int = 8):
    """Dopo qualche secondo legge la sessione e inoltra eventuali link (es. claim di playit)."""
    await asyncio.sleep(delay)
    rc, out = await remote(f"tmux capture-pane -p -t {session} -S -100 2>/dev/null")
    urls = list(dict.fromkeys(URL_RE.findall(out)))
    if urls:
        await app.bot.send_message(chat_id, f"🔗 [{session}]\n" + "\n".join(urls))


# ===================== COMANDI =====================
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Non sei autorizzato.")
        return
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def cmd_startserver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Non sei autorizzato.")
        return
    await update.message.reply_text("⏳ Mi collego al Codespace (se è spento ci vuole un po')...")
    if await session_exists("server"):
        await update.message.reply_text("⚠️ Il server è già in esecuzione.")
        return
    ok, out = await start_session("server", SERVER_CMD, SERVER_DIR)
    if not ok:
        await update.message.reply_text(f"❌ Errore nell'avvio:\n{out[-800:]}")
        return
    await update.message.reply_text("🚀 Server in avvio... ti avviso quando è pronto.")
    asyncio.create_task(watch_server(context.application, update.effective_chat.id))


async def cmd_stopserver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Non sei autorizzato.")
        return
    if not await session_exists("server"):
        await update.message.reply_text("⚠️ Il server non è in esecuzione.")
        return
    await remote("tmux send-keys -t server stop Enter")
    await update.message.reply_text("📤 Comando `stop` inviato al server...", parse_mode="Markdown")


async def cmd_startplayit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Non sei autorizzato.")
        return
    if await session_exists("playitd"):
        await update.message.reply_text("⚠️ playitd è già in esecuzione.")
        return
    ok, out = await start_session("playitd", PLAYITD_CMD, SERVER_DIR)
    if ok:
        await update.message.reply_text("🚀 playitd avviato.")
    else:
        await update.message.reply_text(f"❌ Errore nell'avvio:\n{out[-800:]}")


async def cmd_playit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Non sei autorizzato.")
        return
    if await session_exists("playit"):
        await update.message.reply_text("⚠️ playit è già in esecuzione.")
        return
    ok, out = await start_session("playit", PLAYIT_CMD, SERVER_DIR)
    if ok:
        await update.message.reply_text("🚀 playit avviato.")
        asyncio.create_task(send_links(context.application, update.effective_chat.id, "playit"))
    else:
        await update.message.reply_text(f"❌ Errore nell'avvio:\n{out[-800:]}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("⛔ Non sei autorizzato.")
        return
    rc, out = await remote("tmux ls 2>/dev/null || echo 'nessuna sessione attiva'")
    await update.message.reply_text(f"📊 Sessioni nel Codespace:\n{out.strip()[-800:]}")


async def on_startup(app: Application):
    """All'avvio del bot manda le istruzioni a tutti gli admin."""
    for admin_id in ADMIN_IDS:
        try:
            await app.bot.send_message(admin_id, HELP_TEXT, parse_mode="Markdown")
        except Exception as e:
            print(f"Impossibile scrivere all'admin {admin_id}: {e} (scrivi prima /start al bot)")



class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def start_health_server():
    """Render richiede che un Web Service ascolti sulla porta $PORT."""
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def main():
    start_health_server()
    app = Application.builder().token(TOKEN).post_init(on_startup).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("startserver", cmd_startserver))
    app.add_handler(CommandHandler("stopserver", cmd_stopserver))
    app.add_handler(CommandHandler("startplayit", cmd_startplayit))
    app.add_handler(CommandHandler("playit", cmd_playit))
    app.add_handler(CommandHandler("status", cmd_status))
    print("Bot avviato.")
    app.run_polling()


if __name__ == "__main__":
    main()
