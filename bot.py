import os
import re
import json
import html
import random
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

load_dotenv()

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
raw_chat_id = os.environ.get("TELEGRAM_CHAT_ID")

if not BOT_TOKEN or not raw_chat_id:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in environment variables or .env!")

DEFAULT_CHAT_ID = int(raw_chat_id)

# Topic names must match the "topic" values in questions.json
ALL_TOPICS = ["OS", "Networking", "Cloud", "Java", "Spring", "OOPs", "DBMS", "SQL", "AI"]

# Telegram hard limit for a single message
TELEGRAM_MAX_LEN = 4096

# Quiet hours: no scheduled drills from QUIET_START (inclusive) to QUIET_END (exclusive)
TZ = ZoneInfo(os.environ.get("BOT_TZ", "Asia/Kolkata"))
QUIET_START = 2
QUIET_END = 6

user_preferences = {
    DEFAULT_CHAT_ID: set(ALL_TOPICS)
}

# Domain badges for the question header
TOPIC_BADGES = {
    "Java": "☕ JAVA",
    "Spring": "🍃 SPRING BOOT",
    "OS": "💻 OPERATING SYSTEMS",
    "Networking": "🌐 COMPUTER NETWORKS",
    "DBMS": "🗄️ DATABASE INTERNALS",
    "SQL": "📊 SQL QUERYING",
    "Cloud": "☁️ CLOUD & DISTRIBUTED",
    "OOPs": "🧱 OOP PRINCIPLES",
    "AI": "🤖 AI & EMBEDDINGS",
}

# Short icons used in the topic picker
TOPIC_ICONS = {
    "OS": "💻",
    "Networking": "🌐",
    "Cloud": "☁️",
    "Java": "☕",
    "Spring": "🍃",
    "OOPs": "🧱",
    "DBMS": "🗄️",
    "SQL": "📊",
    "AI": "🤖",
}


# ---------------------------------------------------------------------------
# Quiet hours helpers
# ---------------------------------------------------------------------------
def in_quiet_hours(dt: datetime) -> bool:
    return QUIET_START <= dt.hour < QUIET_END


def next_run_time() -> datetime:
    """Random 45-90 min from now; if that lands in quiet hours, push to 6:00-6:30."""
    candidate = datetime.now(TZ) + timedelta(seconds=random.randint(2700, 5400))
    if in_quiet_hours(candidate):
        candidate = candidate.replace(
            hour=QUIET_END, minute=random.randint(0, 30), second=0, microsecond=0
        )
    return candidate


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def load_questions():
    try:
        with open("questions.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading questions: {e}")
        return []


# ---------------------------------------------------------------------------
# Topic picker UI
# ---------------------------------------------------------------------------
def build_topic_text(chat_id, footer):
    selected = user_preferences.get(chat_id, set(ALL_TOPICS))
    total = len(ALL_TOPICS)
    count = len(selected)

    bar = "🟩" * count + "⬜" * (total - count)

    if count:
        # keep ALL_TOPICS order for a stable display
        chips = "  ".join(
            f"{TOPIC_ICONS[t]} {t}" for t in ALL_TOPICS if t in selected
        )
    else:
        chips = "⚠️ <i>Nothing selected — no questions will be sent</i>"

    return (
        f"🎛 <b>TOPIC CONTROL PANEL</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📡 <b>Active:</b> {count}/{total}\n"
        f"{bar}\n\n"
        f"{chips}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{footer}"
    )


def get_topic_keyboard(chat_id):
    selected_set = user_preferences.get(chat_id, set(ALL_TOPICS))

    layout = [
        ["OS", "Networking", "Cloud"],
        ["Java", "Spring", "OOPs"],
        ["DBMS", "SQL", "AI"],
    ]

    keyboard = []
    for row in layout:
        row_buttons = []
        for topic in row:
            mark = "✅" if topic in selected_set else "▫️"
            row_buttons.append(
                InlineKeyboardButton(
                    f"{mark} {TOPIC_ICONS[topic]} {topic}",
                    callback_data=f"TOGGLE_{topic}",
                )
            )
        keyboard.append(row_buttons)

    keyboard.append([
        InlineKeyboardButton("✨ Select All", callback_data="ACTION_ALL"),
        InlineKeyboardButton("🧹 Clear All", callback_data="ACTION_CLEAR"),
    ])
    keyboard.append([
        InlineKeyboardButton("💾 Save & Close", callback_data="ACTION_DONE"),
    ])

    return InlineKeyboardMarkup(keyboard)


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------
def clean_answer_markup(raw_text: str) -> str:
    """
    Escapes HTML, then converts `backticks` into <b>bold</b>.
    <code> is NOT used because Telegram cannot apply a spoiler to code entities,
    which left those terms visible.
    """
    if not raw_text:
        return ""

    escaped = html.escape(raw_text, quote=False)
    formatted = re.sub(r"`([^`]+)`", r"<b>\1</b>", escaped)
    return formatted.strip()


def format_question_message(item):
    topic_name = item.get("topic", "General")
    badge = TOPIC_BADGES.get(topic_name, f"🎯 {topic_name.upper()}")
    q_id = item.get("id", "")
    id_tag = f" <b>#{q_id}</b>" if q_id else ""

    raw_question = html.escape(item.get("question", "").strip(), quote=False)
    formatted_answer = clean_answer_markup(item.get("answer", ""))

    return (
        f"┏ 🏷️ <b>{badge}</b>{id_tag}\n"
        f"┃\n"
        f"┗ ❓ <b>QUESTION:</b>\n"
        f"<b>{raw_question}</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💡 <b>TAP BELOW TO REVEAL ANSWER</b> 👇\n\n"
        f"<blockquote expandable>"
        f"<tg-spoiler>{formatted_answer}</tg-spoiler>"
        f"</blockquote>"
    )


def format_question_plain(item):
    """Plain-text fallback used if Telegram rejects the HTML formatting."""
    topic_name = item.get("topic", "General")
    badge = TOPIC_BADGES.get(topic_name, topic_name.upper())
    q_id = item.get("id", "")
    id_tag = f" #{q_id}" if q_id else ""
    return (
        f"{badge}{id_tag}\n\n"
        f"QUESTION:\n{item.get('question', '').strip()}\n\n"
        f"ANSWER:\n{item.get('answer', '').strip()}"
    )[:TELEGRAM_MAX_LEN]


async def send_question(send_func, item):
    msg = format_question_message(item)
    try:
        if len(msg) > TELEGRAM_MAX_LEN:
            raise BadRequest("Message too long for HTML formatting")
        await send_func(msg, parse_mode="HTML")
    except BadRequest as e:
        print(f"HTML send failed for question #{item.get('id')}: {e}. Falling back to plain text.")
        await send_func(format_question_plain(item))


def pick_question(chat_id):
    questions = load_questions()
    if not questions:
        return None

    active_topics = user_preferences.get(chat_id, set(ALL_TOPICS))
    if not active_topics:
        return None

    normalized_active = {t.lower() for t in active_topics}
    filtered = [q for q in questions if q.get("topic", "").lower() in normalized_active]

    if filtered:
        return random.choice(filtered)
    return None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in user_preferences:
        user_preferences[chat_id] = set(ALL_TOPICS)

    text = (
        "👋 <b>Interview Drill Bot Active</b>\n\n"
        + build_topic_text(chat_id, "Tap topics to toggle, then <b>Save &amp; Close</b>.")
    )
    await update.message.reply_text(
        text, reply_markup=get_topic_keyboard(chat_id), parse_mode="HTML"
    )


async def topic_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in user_preferences:
        user_preferences[chat_id] = set(ALL_TOPICS)

    await update.message.reply_text(
        build_topic_text(chat_id, "Tap topics to toggle, then <b>Save &amp; Close</b>."),
        reply_markup=get_topic_keyboard(chat_id),
        parse_mode="HTML",
    )


async def handle_topic_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id
    data = query.data

    if chat_id not in user_preferences:
        user_preferences[chat_id] = set(ALL_TOPICS)

    current_set = user_preferences[chat_id]

    if data.startswith("TOGGLE_"):
        topic = data.replace("TOGGLE_", "", 1)
        if topic in current_set:
            current_set.remove(topic)
            await query.answer(f"Removed {topic}")
        else:
            current_set.add(topic)
            await query.answer(f"Added {topic}")

    elif data == "ACTION_ALL":
        user_preferences[chat_id] = set(ALL_TOPICS)
        current_set = user_preferences[chat_id]
        await query.answer("All topics selected")

    elif data == "ACTION_CLEAR":
        user_preferences[chat_id] = set()
        current_set = user_preferences[chat_id]
        await query.answer("Cleared all selections")

    elif data == "ACTION_DONE":
        await query.answer("Saved!")
        if current_set:
            chips = "  ".join(f"{TOPIC_ICONS[t]} {t}" for t in ALL_TOPICS if t in current_set)
        else:
            chips = "⚠️ <i>None</i>"
        try:
            await query.edit_message_text(
                f"✅ <b>SAVED</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📋 <b>Focus ({len(current_set)}/{len(ALL_TOPICS)}):</b>\n{chips}\n\n"
                f"🕒 Drills arrive every 45-90 min (quiet 2 AM - 6 AM).\n"
                f"Use /ask for an instant question or /topic to edit again.",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    try:
        await query.edit_message_text(
            build_topic_text(chat_id, "Tap topics to toggle, then <b>Save &amp; Close</b>."),
            reply_markup=get_topic_keyboard(chat_id),
            parse_mode="HTML",
        )
    except Exception:
        pass


async def ask_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    question_data = pick_question(chat_id)

    if not question_data:
        active = user_preferences.get(chat_id, set())
        if not active:
            await update.message.reply_text(
                "⚠️ No topics are currently selected! Run /topic and select at least one."
            )
        else:
            await update.message.reply_text(
                f"⚠️ No questions found for your selected topics: ({', '.join(sorted(active))})."
            )
        return

    await send_question(update.message.reply_text, question_data)


async def send_random_drill_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = DEFAULT_CHAT_ID

    # Skip sending during quiet hours (e.g. startup at 3 AM)
    if not in_quiet_hours(datetime.now(TZ)):
        question_data = pick_question(chat_id)
        if question_data:
            async def _send(text, **kwargs):
                return await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)

            try:
                await send_question(_send, question_data)
            except Exception as e:
                print(f"Failed to send scheduled drill: {e}")

    context.job_queue.run_once(send_random_drill_job, when=next_run_time())


# ---------------------------------------------------------------------------
# Health server (Render)
# ---------------------------------------------------------------------------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return


def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


def main():
    threading.Thread(target=run_health_server, daemon=True).start()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("topic", topic_command))
    app.add_handler(CommandHandler("ask", ask_now))
    app.add_handler(CallbackQueryHandler(handle_topic_callback))

    # First drill 10 seconds after startup (skipped automatically in quiet hours)
    app.job_queue.run_once(send_random_drill_job, when=10)

    print("🚀 Bot is live with multi-topic tuple filtering...")
    app.run_polling()


if __name__ == "__main__":
    main()