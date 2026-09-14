import os
import random
import string
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from http.server import HTTPServer, BaseHTTPRequestHandler

import libsql_experimental as libsql
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters, ForceReply
from telegram.error import BadRequest, TimedOut, NetworkError
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Health check server (reused from vault bot, for Render)
# ---------------------------------------------------------------------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()


def run_health_check_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


threading.Thread(target=run_health_check_server, daemon=True).start()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TOKEN = os.getenv("BOT_TOKEN")
TURSO_URL = os.getenv("TURSO_DATABASE_URL")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN")

REACTIONS = ["❤️", "😂", "😮", "🥺", "👍"]

PENDING_CODE_TTL_MINUTES = 15
PENDING_NOTE_TTL_MINUTES = 15
PENDING_STUCK_MINUTES = 5  # how long a 'pending' delivery can sit before we assume the send outcome was lost (e.g. a crash mid-send)
MAX_TEXT_LENGTH = 2000       # Telegram text messages allow up to 4096; we cap lower for readability
MAX_CAPTION_LENGTH = 1024    # Telegram's actual hard limit for photo captions

# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------
def get_db():
    return libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)


def db():
    """Context-managed connection: ``with db() as conn:`` closes the
    connection deterministically, even on an early return or an exception
    partway through a handler. Implemented with a small nested class
    rather than ``@contextlib.contextmanager`` so the test harness — which
    exec's this module's top-level functions in a minimal namespace — can
    run it without extra imports."""
    conn = get_db()

    class _Handle:
        def __enter__(self):
            return conn

        def __exit__(self, *exc):
            conn.close()
            return False

    return _Handle()


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS links (
            user_id INTEGER PRIMARY KEY,
            partner_id INTEGER NOT NULL,
            paused INTEGER NOT NULL DEFAULT 0,
            linked_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_links (
            code TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS moments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            recipient_id INTEGER NOT NULL,
            content_type TEXT NOT NULL,
            file_id TEXT,
            text TEXT,
            created_at TEXT NOT NULL,
            reaction TEXT,
            reply_note TEXT,
            responded_at TEXT,
            delivery_status TEXT NOT NULL DEFAULT 'pending',
            link_session TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY
        )
    """)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(moments)").fetchall()}
    if "voice_duration" not in columns:
        conn.execute("ALTER TABLE moments ADD COLUMN voice_duration INTEGER")
        conn.commit()

    # Migration path for a DB created before delivery_status/link_session
    # existed. Rows from before that point predate delivery tracking
    # entirely — the old code couldn't guarantee they were delivered, so we
    # don't claim 'delivered' for them, and we deliberately don't run them
    # through recover_stuck_pending()'s 'pending' path either (that's for
    # rows genuinely interrupted mid-send, not this whole prior era). They
    # get their own status, 'legacy', so the policy is explicit rather than
    # accidental: still shown in /onthisday (hiding someone's whole existing
    # archive on upgrade would be worse than the ambiguity), but old buttons
    # remain disabled because their pairing session cannot be verified.
    # See moment_link_still_valid() and on_this_day().
    #
    # Gated on a marker so an ordinary boot runs no failing DDL. The
    # try/except stays as a safety net for a DB that already has the
    # columns but predates this marker: it throws "duplicate column" once,
    # we swallow it, and the marker is recorded so it never runs again.
    ddl_migration = "add_moments_delivery_columns_v1"
    if not conn.execute("SELECT 1 FROM schema_migrations WHERE name = ?", (ddl_migration,)).fetchone():
        for ddl in (
            "ALTER TABLE moments ADD COLUMN delivery_status TEXT NOT NULL DEFAULT 'legacy'",
            "ALTER TABLE moments ADD COLUMN link_session TEXT",
        ):
            try:
                conn.execute(ddl)
                conn.commit()
            except Exception as e:
                msg = str(e).lower()
                if "duplicate column" in msg or "already exists" in msg:
                    pass  # column already present (fresh CREATE TABLE, or a pre-marker DB)
                else:
                    print(f"Migration DDL failed unexpectedly ({ddl!r}): {type(e).__name__}: {e}")
                    raise
        conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (ddl_migration,))
        conn.commit()

    # Earlier revisions added delivery_status with a pending default, then
    # recovery changed old rows to uncertain. Their NULL link_session is
    # the distinguishing marker: tracked sends always write a session
    # string (even when linked_at is missing). Do not match empty strings
    # or touch genuine tracked failures/uncertain sends.
    migration = "repair_pre_tracking_moment_status_v1"
    applied = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE name = ?", (migration,)
    ).fetchone()
    if not applied:
        conn.execute("""
            UPDATE moments SET delivery_status = 'legacy'
            WHERE link_session IS NULL
              AND delivery_status IN ('pending', 'uncertain')
        """)
        conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (migration,))
    # The repair and its marker are committed together below.

    conn.execute("""
        CREATE TABLE IF NOT EXISTS pair_reactions (
            user_low INTEGER NOT NULL,
            user_high INTEGER NOT NULL,
            link_session TEXT NOT NULL,
            position INTEGER NOT NULL,
            label TEXT NOT NULL,
            PRIMARY KEY (user_low, user_high, link_session, position)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS moment_reactions (
            moment_id INTEGER NOT NULL,
            position INTEGER NOT NULL,
            label TEXT NOT NULL,
            PRIMARY KEY (moment_id, position)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_notes (
            user_id INTEGER PRIMARY KEY,
            moment_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS moment_messages (
            chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
            moment_id INTEGER NOT NULL, kind TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(chat_id, message_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS moment_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, moment_id INTEGER NOT NULL,
            author_id INTEGER NOT NULL, text TEXT NOT NULL, created_at TEXT,
            source_message_id INTEGER, delivery_status TEXT NOT NULL,
            UNIQUE(author_id, source_message_id)
        )
    """)
    # Notes can now be voice as well as text (Phase 3.5): 'text' stays the
    # default so every pre-existing row keeps its current meaning, and
    # `file_id` is only populated for voice rows. Additive-only, so no
    # table rebuild is needed.
    note_columns = {row[1] for row in conn.execute("PRAGMA table_info(moment_notes)").fetchall()}
    if "content_type" not in note_columns:
        conn.execute("ALTER TABLE moment_notes ADD COLUMN content_type TEXT NOT NULL DEFAULT 'text'")
        conn.commit()
    if "file_id" not in note_columns:
        conn.execute("ALTER TABLE moment_notes ADD COLUMN file_id TEXT")
        conn.commit()

    migration = "preserve_existing_reply_notes_v1"
    if not conn.execute("SELECT 1 FROM schema_migrations WHERE name=?", (migration,)).fetchone():
        conn.execute("""
            INSERT INTO moment_notes(moment_id,author_id,text,created_at,delivery_status)
            SELECT id,recipient_id,reply_note,responded_at,'legacy'
            FROM moments WHERE reply_note IS NOT NULL AND reply_note != ''
        """)
        conn.execute("INSERT INTO schema_migrations VALUES(?)", (migration,))
    conn.commit()
    conn.close()


async def periodic_recovery(context: ContextTypes.DEFAULT_TYPE):
    """job_queue callback — see recover_stuck_pending(). Running this only
    once at startup misses rows that get stuck *between* restarts (e.g. a
    crash one minute after boot, followed by weeks of uptime), so this runs
    on a recurring timer instead of relying on the next restart to catch up."""
    recover_stuck_pending()


def recover_stuck_pending():
    """
    A moment can be inserted with delivery_status='pending' and then the
    process dies before the send attempt's outcome is recorded (crash,
    deploy restart, etc). We can't know whether Telegram actually received
    it, so mark anything still 'pending' past a grace window as 'uncertain'
    rather than leaving it looking indistinguishable from a real delivery.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=PENDING_STUCK_MINUTES)).isoformat()
    with db() as conn:
        conn.execute(
            "UPDATE moments SET delivery_status = 'uncertain' WHERE delivery_status = 'pending' AND created_at < ?",
            (cutoff,),
        )
        conn.execute("UPDATE moment_notes SET delivery_status='uncertain' WHERE delivery_status='pending' AND created_at < ?", (cutoff,))
        conn.commit()


init_db()
recover_stuck_pending()

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def minutes_since(iso_str: str) -> float:
    then = datetime.fromisoformat(iso_str)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 60


def session_token(linked_at: str) -> str:
    """Callback data and stored link_session both use this normalized,
    colon-free form of linked_at so it's safe inside colon-delimited
    callback_data strings and simple to compare for equality."""
    return (linked_at or "").replace(":", "").replace(".", "").replace("+", "")


def get_partner_id(user_id: int):
    with db() as conn:
        row = conn.execute("SELECT partner_id, paused, linked_at FROM links WHERE user_id = ?", (user_id,)).fetchone()
    return row  # (partner_id, paused, linked_at) or None


def is_paused(user_id: int) -> bool:
    """True if messages to this user are currently paused (by this user, on their own side)."""
    row = get_partner_id(user_id)
    return bool(row and row[1])


def currently_linked_to(user_a: int, user_b: int) -> bool:
    """True only if both sides still list each other as partner right now."""
    row_a = get_partner_id(user_a)
    row_b = get_partner_id(user_b)
    return bool(row_a and row_b and row_a[0] == user_b and row_b[0] == user_a)


def moment_link_still_valid(sender_id: int, recipient_id: int, stored_session: str) -> bool:
    """
    Guards old reaction/note buttons and stale unlink confirmations: the
    pairing must still be active AND must be the *same* pairing session
    that existed when the button was created — not a different session
    that happens to involve the same two user IDs after an unlink+relink.

    Moments without a stored session remain viewable in the archive,
    but cannot authorize new reactions or notes. Being linked to the same
    person today does not establish which session an old moment belongs to.
    """
    if not currently_linked_to(sender_id, recipient_id):
        return False
    if not stored_session:
        return False
    row = get_partner_id(recipient_id)
    if not row:
        return False
    return session_token(row[2]) == stored_session


def gen_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def is_private_chat(update: Update) -> bool:
    return update.effective_chat is not None and update.effective_chat.type == "private"


async def require_private(update: Update) -> bool:
    """Returns True if OK to proceed. Replies and returns False otherwise.
    This bot is designed for exactly two people talking to it 1:1 — running
    any command in a group could expose one person's archive to a crowd."""
    if is_private_chat(update):
        return True
    if update.message:
        await update.message.reply_text("This only works in a private chat with the bot — please message it directly.")
    return False


async def send_safe(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str = None,
                     photo_file_id: str = None, reply_markup=None, reply_to=None) -> bool:
    """
    Best-effort send for notifications that aren't part of the delivery-
    tracked archive (e.g. 'your partner reacted'). Losing one of these isn't
    great but doesn't corrupt any stored record, so a simple bool is enough.
    Use send_tracked() instead for the actual moment content.
    """
    options = {"reply_parameters": ReplyParameters(reply_to, allow_sending_without_reply=True)} if reply_to else {}
    try:
        if photo_file_id:
            await context.bot.send_photo(chat_id=chat_id, photo=photo_file_id, caption=text,
                                          reply_markup=reply_markup, **options)
        else:
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, **options)
        return True
    except Exception as e:
        print(f"send_safe failed (chat_id={chat_id}): {type(e).__name__}: {e}")
        return False


async def send_tracked(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str = None,
                        photo_file_id: str = None, reply_markup=None, moment_id=None, reply_to=None,
                        voice_file_id=None, kind: str = "received") -> str:
    """
    Delivery-tracked send used for actual moment content. Returns one of:
    'delivered' — Telegram confirmed the send.
    'uncertain' — a timeout/network error means we genuinely don't know if
                  it went through; do NOT treat as failed or delivered.
    'failed'    — a definite failure (blocked bot, bad chat, rejected
                  content, etc).

    BadRequest is checked before (TimedOut, NetworkError): in PTB's
    hierarchy BadRequest is a NetworkError subclass, so catching
    NetworkError first would misclassify a rejected caption or similar
    as a mere connection hiccup instead of a real, permanent failure.
    """
    options = {"reply_parameters": ReplyParameters(reply_to, allow_sending_without_reply=True)} if reply_to else {}
    try:
        if voice_file_id:
            sent = await context.bot.send_voice(chat_id=chat_id, voice=voice_file_id, caption=text,
                                               reply_markup=reply_markup, **options)
        elif photo_file_id:
            sent = await context.bot.send_photo(chat_id=chat_id, photo=photo_file_id, caption=text,
                                          reply_markup=reply_markup, **options)
        else:
            sent = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, **options)
    except BadRequest as e:
        print(f"send_tracked: failed (bad request, chat_id={chat_id}): {type(e).__name__}: {e}")
        return "failed"
    except (TimedOut, NetworkError) as e:
        print(f"send_tracked: uncertain outcome (chat_id={chat_id}): {type(e).__name__}: {e}")
        return "uncertain"
    except Exception as e:
        print(f"send_tracked: failed (chat_id={chat_id}): {type(e).__name__}: {e}")
        return "failed"

    if moment_id is not None:
        remember_message(chat_id, sent.message_id, moment_id, kind)
    return "delivered"


def remember_message(chat_id, message_id, moment_id, kind):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO moment_messages VALUES(?,?,?,?,?)",
                     (chat_id, message_id, moment_id, kind, now_iso()))
        conn.commit()


def original_message_id(moment_id, chat_id):
    """The message that represents this moment in `chat_id`'s own chat —
    'original' for the sender's copy, 'received' for the recipient's. A
    chat only ever has one of the two for a given moment, so this is
    unambiguous regardless of which side is asking."""
    with db() as conn:
        row = conn.execute(
            "SELECT message_id FROM moment_messages WHERE moment_id=? AND chat_id=? AND kind IN ('original','received') LIMIT 1",
            (moment_id, chat_id)).fetchone()
    return row[0] if row else None


def local_now():
    return datetime.now(ZoneInfo("Asia/Singapore"))


def local_time(stamp):
    value = datetime.fromisoformat(stamp)
    # Earlier records without an offset were written using UTC as well.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo("Asia/Singapore"))


def friendly_date(value, today=None):
    today = today or local_now().date()
    day = value.date()
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    label = f"{value.day} {value.strftime('%b')}"
    return label if value.year == today.year else f"{label} {value.year}"


def friendly_clock(value):
    return f"{value.hour % 12 or 12}:{value.minute:02d} {'am' if value.hour < 12 else 'pm'}"


def friendly_timestamp(stamp):
    value = local_time(stamp)
    return f"{friendly_date(value)} · {friendly_clock(value)}"


def archive_notes(moment_id, viewer_id, old_note=None):
    with db() as conn:
        moment = conn.execute("SELECT created_at FROM moments WHERE id=?", (moment_id,)).fetchone()
        rows = conn.execute("SELECT author_id,text,created_at,delivery_status,content_type FROM moment_notes WHERE moment_id=? ORDER BY id", (moment_id,)).fetchall()
    if not rows:
        return ["Notes", old_note] if old_note else []
    moment_day = local_time(moment[0]).date() if moment and moment[0] else None
    lines = []
    last_author = None
    for author, text, stamp, status, note_kind in rows:
        if author != last_author:
            if lines:
                lines.append("")
            lines.append("Your notes" if author == viewer_id else "Their notes")
            last_author = author
        if stamp:
            value = local_time(stamp)
            label = friendly_clock(value)
            if value.date() != moment_day:
                label = f"{friendly_date(value)} · {label}"
        else:
            label = "Time unavailable"
        # Voice replies aren't replayed inline in this text summary yet
        # (that's next) — say so plainly rather than showing a blank line.
        shown = "🎤 Voice note (not shown here yet)" if note_kind == "voice" else text
        lines.append(f"{label} · {shown}")
        if status != "delivered":
            # Never reveal whether unconfirmed delivery was due to pause.
            lines.append("(Delivery unconfirmed)")
    return lines


def memory_details(moment_id, viewer_id, sender_id, stamp, reaction, note, status, heading=None):
    title = heading or "🎲"
    lines = [f"{title} {friendly_timestamp(stamp)}",
             "You shared this" if sender_id == viewer_id else "They shared this"]
    if status == "legacy":
        lines.append("(Original delivery unverified)")
    if reaction:
        lines.extend(["", reaction])
    notes = archive_notes(moment_id, viewer_id, note)
    if notes:
        lines.extend(["", *notes])
    return "\n".join(lines)


def truncate(text: str, limit: int = MAX_TEXT_LENGTH) -> str:
    """Truncates to at most `limit` characters total, including the
    '… (trimmed)' suffix — the old version appended the suffix on top of
    the limit, so a too-long caption could still exceed it."""
    if not text or len(text) <= limit:
        return text
    suffix = "… (trimmed)"
    if limit <= len(suffix):
        return text[:limit]
    return text[: limit - len(suffix)] + suffix


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_private(update):
        return

    user_id = update.effective_user.id
    args = context.args

    # /start <code> also works as a deep-link style pairing shortcut
    if args:
        await link(update, context)
        return

    existing = get_partner_id(user_id)
    if existing:
        await update.message.reply_text(
            "You're already linked. Send a photo, voice message, or a little note any time — "
            "it'll go straight to your partner. Use /unlink to disconnect."
        )
        return

    code = gen_code()
    with db() as conn:
        conn.execute("DELETE FROM pending_links WHERE user_id = ?", (user_id,))
        conn.execute(
            "INSERT INTO pending_links (code, user_id, created_at) VALUES (?, ?, ?)",
            (code, user_id, now_iso()),
        )
        conn.commit()

    await update.message.reply_text(
        "Welcome! This is a shared little space for you and one other person.\n\n"
        f"Share this code with them: `{code}` (valid for {PENDING_CODE_TTL_MINUTES} minutes)\n"
        f"They should send `/link {code}` to this bot to connect with you.\n\n"
        "Already have a code from them instead? Send `/link <code>`.",
        parse_mode="Markdown",
    )


async def link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_private(update):
        return

    user_id = update.effective_user.id

    if get_partner_id(user_id):
        await update.message.reply_text("You're already linked with someone. Use /unlink first if you want to switch.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /link <code>")
        return

    code = context.args[0].strip().upper()

    with db() as conn:
        row = conn.execute("SELECT user_id, created_at FROM pending_links WHERE code = ?", (code,)).fetchone()

        if not row:
            await update.message.reply_text("That code doesn't look right (or it's expired). Ask your partner to send /start again.")
            return

        code_owner_id, created_at = row

        if minutes_since(created_at) > PENDING_CODE_TTL_MINUTES:
            conn.execute("DELETE FROM pending_links WHERE code = ?", (code,))
            conn.commit()
            await update.message.reply_text("That code has expired. Ask your partner to send /start again for a new one.")
            return

        if code_owner_id == user_id:
            await update.message.reply_text("You can't link with yourself — send this code to your partner instead.")
            return

        # The code owner may have since linked with someone else while this code
        # sat unused — an old code should never be able to silently steal or
        # overwrite an existing partnership.
        if get_partner_id(code_owner_id):
            conn.execute("DELETE FROM pending_links WHERE code = ?", (code,))
            conn.commit()
            await update.message.reply_text("That code is no longer valid — ask them to send /start again for a fresh one.")
            return

        linked_at = now_iso()
        conn.execute(
            "INSERT INTO links (user_id, partner_id, paused, linked_at) VALUES (?, ?, 0, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET partner_id=excluded.partner_id, paused=0, linked_at=excluded.linked_at",
            (user_id, code_owner_id, linked_at),
        )
        conn.execute(
            "INSERT INTO links (user_id, partner_id, paused, linked_at) VALUES (?, ?, 0, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET partner_id=excluded.partner_id, paused=0, linked_at=excluded.linked_at",
            (code_owner_id, user_id, linked_at),
        )
        # Clear this code, plus any other pending code either side is holding.
        conn.execute("DELETE FROM pending_links WHERE code = ?", (code,))
        conn.execute("DELETE FROM pending_links WHERE user_id IN (?, ?)", (user_id, code_owner_id))
        conn.commit()

    await update.message.reply_text("You're linked! Send a photo, voice message, or a little note whenever — it'll go straight to them.")
    await send_safe(context, code_owner_id, "You're linked! Send a photo, voice message, or a little note whenever — it'll go straight to them.")


async def unlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_private(update):
        return

    user_id = update.effective_user.id
    row = get_partner_id(user_id)
    if not row:
        await update.message.reply_text("You're not linked with anyone right now.")
        return

    session = session_token(row[2])
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, unlink", callback_data=f"unlinkconfirm:{user_id}:{session}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"unlinkcancel:{user_id}:{session}"),
    ]])
    await update.message.reply_text(
        "Unlink from your partner? Your shared moments stay in the database but you'll stop being connected.",
        reply_markup=keyboard,
    )


async def unlink_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 3:
        # A button from before session binding was added (2 fields, no
        # session token) — reject cleanly instead of crashing on unpack.
        await query.edit_message_text("This button is from an older version of the bot — please use /unlink again.")
        return

    action, requester_id, button_session = parts
    requester_id = int(requester_id)

    if query.from_user.id != requester_id:
        await query.answer("This isn't your confirmation.", show_alert=True)
        return

    if action == "unlinkcancel":
        await query.edit_message_text("Unlink cancelled.")
        return

    row = get_partner_id(requester_id)
    if not row:
        await query.edit_message_text("You're already not linked with anyone.")
        return

    # This button was generated for a specific pairing session. If the
    # person unlinked and re-paired (with the same or a different partner)
    # since this message was sent, an old "Confirm unlink" tap must not be
    # allowed to sever the *new* connection.
    if session_token(row[2]) != button_session:
        await query.edit_message_text(
            "This confirmation is from an earlier connection and no longer applies. "
            "Use /unlink again if you still want to disconnect."
        )
        return

    partner_id = row[0]
    with db() as conn:
        conn.execute("DELETE FROM links WHERE user_id = ?", (requester_id,))
        conn.execute("DELETE FROM links WHERE user_id = ?", (partner_id,))
        conn.execute("DELETE FROM pending_notes WHERE user_id IN (?, ?)", (requester_id, partner_id))
        conn.execute("DELETE FROM moment_messages WHERE chat_id IN (?,?) AND kind IN ('prompt','diary')", (requester_id, partner_id))
        conn.commit()

    # Deliberately no message to partner_id here — unlink is designed to be
    # silent on the other end, same as pause. They'll find out naturally the
    # next time they try to send something and get "you're not linked yet."
    await query.edit_message_text("Unlinked. Send /start any time to pair again — with them or someone else.")


async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_private(update):
        return

    user_id = update.effective_user.id
    row = get_partner_id(user_id)
    if not row:
        await update.message.reply_text("You're not linked with anyone yet.")
        return
    with db() as conn:
        conn.execute("UPDATE links SET paused = 1 WHERE user_id = ?", (user_id,))
        conn.commit()
    await update.message.reply_text(
        "Paused — anything your partner sends won't reach you until you /resume. "
        "They won't be told you paused; a message will just quietly not go through."
    )


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_private(update):
        return

    user_id = update.effective_user.id
    row = get_partner_id(user_id)
    if not row:
        await update.message.reply_text("You're not linked with anyone yet.")
        return
    with db() as conn:
        conn.execute("UPDATE links SET paused = 0 WHERE user_id = ?", (user_id,))
        conn.commit()
    await update.message.reply_text("Resumed — moments will reach you again.")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clears a pending 'waiting for your note' state without sending anything."""
    if not await require_private(update):
        return
    user_id = update.effective_user.id
    with db() as conn:
        cursor = conn.execute("DELETE FROM pending_notes WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM moment_messages WHERE chat_id=? AND kind='prompt'", (user_id,))
        conn.commit()
        deleted = cursor.rowcount
    if deleted:
        await update.message.reply_text("Cancelled — that note won't be sent.")
    else:
        await update.message.reply_text("Nothing to cancel.")


# ---------------------------------------------------------------------------
# Sending a moment (photo or text)
# ---------------------------------------------------------------------------
def pair_reaction_labels(user_id: int, partner_id: int, session: str):
    with db() as conn:
        rows = conn.execute(
            "SELECT label FROM pair_reactions WHERE user_low=? AND user_high=? "
            "AND link_session=? ORDER BY position",
            (min(user_id, partner_id), max(user_id, partner_id), session),
        ).fetchall()
    return [r[0] for r in rows] or list(REACTIONS)


async def reactions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_private(update):
        return
    user_id = update.effective_user.id
    row = get_partner_id(user_id)
    if not row or not currently_linked_to(user_id, row[0]):
        await update.message.reply_text("Link with your person first using /start.")
        return
    partner_id, _, linked_at = row
    session = session_token(linked_at)
    raw = " ".join(context.args).strip()
    if not raw:
        labels = pair_reaction_labels(user_id, partner_id, session)
        await update.message.reply_text(
            "Your pair's reactions: " + " | ".join(labels) +
            "\n\nSet up to 5 emoji or short labels (20 characters each):\n"
            "/reactions ❤️ | 😂 | very you | wish I was there\n"
            "Either person can change them. New moments use the new set; "
            "older buttons keep their meaning.\n/reactions reset — restore defaults"
        )
        return
    labels = list(REACTIONS) if raw.lower() == "reset" else [x.strip() for x in raw.split("|")]
    if (not 1 <= len(labels) <= 5 or any(not x or len(x) > 20 or
            any(ord(c) < 32 for c in x) for x in labels) or len(set(labels)) != len(labels)):
        await update.message.reply_text(
            "Use 1–5 different labels, each 1–20 characters, separated by |.\n"
            "Example: /reactions ❤️ | very you | wish I was there"
        )
        return
    with db() as conn:
        key = (min(user_id, partner_id), max(user_id, partner_id), session)
        conn.execute("DELETE FROM pair_reactions WHERE user_low=? AND user_high=? AND link_session=?", key)
        for position, label in enumerate(labels):
            conn.execute("INSERT INTO pair_reactions VALUES(?,?,?,?,?)", (*key, position, label))
        conn.commit()
    await update.message.reply_text("Reactions saved for new moments: " + " | ".join(labels))


def reaction_keyboard(moment_id: int):
    # Persist a snapshot so later customization cannot reinterpret old buttons.
    with db() as conn:
        choices = conn.execute("SELECT position,label FROM moment_reactions WHERE moment_id=? ORDER BY position", (moment_id,)).fetchall()
        if not choices:
            moment = conn.execute("SELECT sender_id,recipient_id,link_session FROM moments WHERE id=?", (moment_id,)).fetchone()
            labels = pair_reaction_labels(*moment) if moment else list(REACTIONS)
            choices = list(enumerate(labels))
            for position, label in choices:
                conn.execute("INSERT INTO moment_reactions VALUES(?,?,?)", (moment_id, position, label))
            conn.commit()
    buttons = [InlineKeyboardButton(label, callback_data=f"choice:{moment_id}:{position}")
               for position, label in choices]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("💬 Add a note", callback_data=f"note:{moment_id}")])
    return InlineKeyboardMarkup(rows)


async def handle_incoming(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_private(update):
        return

    message = update.message
    user_id = update.effective_user.id

    # Redelivered updates must not turn an already-saved button-mode note
    # into a new moment after its pending state has been consumed.
    with db() as conn:
        saved = conn.execute("SELECT 1 FROM moment_notes WHERE author_id=? AND source_message_id=?",
                             (user_id, message.message_id)).fetchone()
    if saved:
        await message.reply_text("Note already saved.")
        return

    # A moment's reply thread grows in both directions: 'received'/'prompt'
    # are the original recipient-facing targets, 'reply' is anything
    # delivered afterward to either side as part of that same back-and-forth
    # (see save_note()). Any of the three is a valid thing to reply to.
    replied = getattr(message, "reply_to_message", None)
    if replied is not None:
        with db() as conn:
            target = conn.execute("SELECT moment_id,kind,created_at FROM moment_messages WHERE chat_id=? AND message_id=?",
                                  (user_id, replied.message_id)).fetchone()
            if target and target[1] in ("received", "prompt", "reply"):
                conn.execute("DELETE FROM pending_notes WHERE user_id=?", (user_id,))
                conn.commit()
        if not target or target[1] not in ("received", "prompt", "reply"):
            await message.reply_text("I couldn't match that reply to a received moment. Use its Add a note button, or send without replying for a new moment.")
            return
        if target[1] == "prompt" and minutes_since(target[2]) > PENDING_NOTE_TTL_MINUTES:
            await message.reply_text("That note prompt expired. Tap Add a note again; nothing was sent.")
            return
        if message.text:
            await save_note(update, context, target[0], text=message.text)
        elif getattr(message, "voice", None):
            await save_note(update, context, target[0], voice_file_id=message.voice.file_id)
        else:
            await message.reply_text("Notes support text or voice for now. Reply with one of those, or send your photo without replying as a new moment.")
        return

    # If this user owes a note reply to a specific moment, treat this as
    # that note instead of a new moment. Backed by the DB (not
    # context.user_data) so it survives a restart, and expires rather than
    # lingering forever.
    with db() as conn:
        pending = conn.execute("SELECT moment_id, created_at FROM pending_notes WHERE user_id = ?", (user_id,)).fetchone()
        if pending:
            conn.execute("DELETE FROM pending_notes WHERE user_id = ?", (user_id,))
            conn.commit()

    if pending and (message.text or getattr(message, "voice", None)):
        moment_id, created_at = pending
        if minutes_since(created_at) > PENDING_NOTE_TTL_MINUTES:
            # Expired note prompts must NOT silently become a new moment —
            # that changes what the message means without the person
            # choosing that. Stop and make them explicitly resend/re-tap.
            await message.reply_text(
                "That note prompt expired, so this wasn't sent as a note or as a new message. "
                "Tap 'Add a note' on the moment again if you still want to reply, "
                "or send this again if you meant it as something new."
            )
            return
        elif message.text:
            await save_note(update, context, moment_id, text=message.text)
            return
        else:
            await save_note(update, context, moment_id, voice_file_id=message.voice.file_id)
            return

    row = get_partner_id(user_id)
    if not row:
        await message.reply_text("You're not linked yet — send /start to get a pairing code.")
        return

    partner_id, _, my_linked_at = row

    voice_duration = None
    if getattr(message, "voice", None):
        content_type = "voice"
        file_id = message.voice.file_id
        duration = message.voice.duration
        voice_duration = int(duration.total_seconds() if isinstance(duration, timedelta) else duration)
        text = truncate(message.caption or None, MAX_CAPTION_LENGTH)
    elif message.photo:
        content_type = "photo"
        file_id = message.photo[-1].file_id
        text = truncate(message.caption or None, MAX_CAPTION_LENGTH)
    elif message.text:
        content_type = "text"
        file_id = None
        text = truncate(message.text, MAX_TEXT_LENGTH)
    else:
        return  # unsupported content, silently ignore for this prototype

    if is_paused(partner_id):
        # Deliberately the same message as a genuine delivery failure below —
        # a paused partner's status is never revealed to the sender.
        await message.reply_text("Couldn't deliver that right now — you can try again later.")
        return

    with db() as conn:
        cursor = conn.execute(
            "INSERT INTO moments (sender_id, recipient_id, content_type, file_id, text, created_at, delivery_status, link_session, voice_duration) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (user_id, partner_id, content_type, file_id, text, now_iso(), session_token(my_linked_at), voice_duration),
        )
        moment_id = cursor.lastrowid
        conn.commit()

    remember_message(user_id, message.message_id, moment_id, "original")
    keyboard = reaction_keyboard(moment_id)
    if content_type == "voice":
        status = await send_tracked(context, partner_id, text=text, voice_file_id=file_id, reply_markup=keyboard, moment_id=moment_id)
    elif content_type == "photo":
        status = await send_tracked(context, partner_id, text=text, photo_file_id=file_id, reply_markup=keyboard, moment_id=moment_id)
    else:
        status = await send_tracked(context, partner_id, text=text, reply_markup=keyboard, moment_id=moment_id)

    with db() as conn:
        conn.execute("UPDATE moments SET delivery_status = ? WHERE id = ?", (status, moment_id))
        conn.commit()

    if status == "delivered":
        await message.reply_text("Sent 💌")
    elif status == "uncertain":
        # Deliberately doesn't tell them to watch for a reply as the way to
        # find out — that just reintroduces "wait and see if they respond"
        # as an expectation, which is exactly what this bot is trying to
        # avoid. State the actual uncertainty and the actual tradeoff.
        await message.reply_text(
            "Delivery couldn't be confirmed. Sending again may create a duplicate."
        )
    else:
        await message.reply_text("Couldn't deliver that right now — you can try again later.")


# ---------------------------------------------------------------------------
# Reactions and notes
# ---------------------------------------------------------------------------
async def react_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_private_chat(update):
        await query.answer("Please use the bot in a private chat.", show_alert=True)
        return
    try:
        action, moment_id, value = query.data.split(":", 2)
        moment_id = int(moment_id)
        if action not in ("choice", "react"):
            raise ValueError("unknown reaction action")
    except (ValueError, TypeError):
        await query.answer("That button is no longer available.")
        return

    with db() as conn:
        row = conn.execute(
            "SELECT sender_id, recipient_id, reaction, link_session FROM moments WHERE id = ?", (moment_id,)
        ).fetchone()

        if not row:
            await query.answer("This moment isn't available anymore.", show_alert=True)
            return

        if action == "choice":
            try:
                position = int(value)
            except ValueError:
                position = -1
            choice = conn.execute("SELECT label FROM moment_reactions WHERE moment_id=? AND position=?", (moment_id, position)).fetchone()
            emoji = choice[0] if choice else None
        else:
            # Compatibility with fixed-emoji buttons delivered before customization.
            emoji = value if value in REACTIONS else None
        if emoji is None:
            await query.answer("That reaction is no longer available.")
            return

        sender_id, recipient_id, existing_reaction, link_session = row
        if query.from_user.id != recipient_id:
            await query.answer("This one wasn't sent to you.", show_alert=True)
            return

        # Old buttons must not still work after either side has unlinked, or
        # after they've unlinked and re-paired (even with each other again) —
        # this moment belongs to the earlier session, not the new one.
        if not moment_link_still_valid(sender_id, recipient_id, link_session):
            await query.answer("This connection isn't active anymore.", show_alert=True)
            return

        if existing_reaction == emoji:
            await query.answer("Reaction already saved.")
            return

        is_first_reaction = existing_reaction is None
        conn.execute(
            "UPDATE moments SET reaction = ?, responded_at = ? WHERE id = ?",
            (emoji, now_iso(), moment_id),
        )
        conn.commit()

    # Keep the "Add a note" option available; drop the reaction row so it
    # can't be double-tapped. This is purely cosmetic (the reaction is
    # already saved above), so a failure here must not block the
    # notification below — and we only answer the callback once, after we
    # know whether the notification actually went out.
    try:
        await query.edit_message_reply_markup(
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Add a note", callback_data=f"note:{moment_id}")]])
        )
    except Exception as e:
        print(f"react_callback: keyboard edit failed (non-fatal) for moment {moment_id}: {type(e).__name__}: {e}")

    delivered = False
    if not is_paused(sender_id):
        notif = f"Reacted {emoji}" if is_first_reaction else f"Changed reaction to {emoji}"
        delivered = await send_safe(context, sender_id, notif, reply_to=original_message_id(moment_id, sender_id))

    await query.answer(f"Sent {emoji}" if delivered else "Reaction saved")


async def note_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_private_chat(update):
        await query.answer("Please use the bot in a private chat.")
        return
    _, moment_id = query.data.split(":")
    moment_id = int(moment_id)

    with db() as conn:
        row = conn.execute("SELECT sender_id, recipient_id, link_session, text, content_type, created_at FROM moments WHERE id = ?", (moment_id,)).fetchone()

        if not row:
            await query.answer("This moment isn't available anymore.", show_alert=True)
            return

        sender_id, recipient_id, link_session, content, kind, created_at = row
        replier_id = query.from_user.id
        # Either side of the pairing can now reply — the original "Add a
        # note" tap by the recipient, or a "Reply" tap by whoever the note
        # was just delivered to (see save_note()).
        if replier_id not in (sender_id, recipient_id):
            await query.answer("This one wasn't sent to you.", show_alert=True)
            return

        if not moment_link_still_valid(sender_id, recipient_id, link_session):
            await query.answer("This connection isn't active anymore.", show_alert=True)
            return

    await query.answer()
    anchor_message_id = query.message.message_id
    if replier_id == recipient_id:
        # Backfills tracking for old "Add a note" buttons delivered before
        # message IDs were recorded. Reply-delivery messages are already
        # tracked at send time (see send_tracked's `kind`), so they never
        # need this.
        remember_message(replier_id, anchor_message_id, moment_id, "received")

    # A re-tap (or a tap on a stale button) replaces any prompt already
    # waiting for this person — delete the old prompt message itself, not
    # just its tracking row, so the chat doesn't fill up with look-alike
    # "reply within 15 minutes" bubbles where only the newest one works.
    with db() as conn:
        stale_prompt = conn.execute("SELECT message_id FROM moment_messages WHERE chat_id=? AND kind='prompt'", (replier_id,)).fetchone()
    if stale_prompt:
        try:
            await context.bot.delete_message(chat_id=replier_id, message_id=stale_prompt[0])
        except Exception as e:
            print(f"note_callback: delete_message failed (non-fatal) chat_id={replier_id} message_id={stale_prompt[0]}: {type(e).__name__}: {e}")

    preview = truncate(content or ("Photo" if kind == "photo" else "Voice message" if kind == "voice" else "Moment"), 100)
    prompt = await context.bot.send_message(
        chat_id=replier_id,
        text=f"Reply · {friendly_timestamp(created_at)}\n{preview}\nReply here within {PENDING_NOTE_TTL_MINUTES} minutes, or /cancel.",
        reply_parameters=ReplyParameters(anchor_message_id, allow_sending_without_reply=True),
        reply_markup=ForceReply(selective=True, input_field_placeholder="Your note (optional, text or voice)"),
    )
    with db() as conn:
        conn.execute("DELETE FROM moment_messages WHERE chat_id=? AND kind='prompt'", (replier_id,))
        conn.execute("INSERT INTO pending_notes VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET moment_id=excluded.moment_id,created_at=excluded.created_at",
                     (replier_id, moment_id, now_iso()))
        conn.execute("INSERT INTO moment_messages VALUES(?,?,?,?,?)", (replier_id, prompt.message_id, moment_id, "prompt", now_iso()))
        conn.commit()


async def save_note(update: Update, context: ContextTypes.DEFAULT_TYPE, moment_id: int,
                     text: str = None, voice_file_id: str = None):
    """Records a reply (text or voice) on a moment's thread and forwards it
    to whichever of the two participants didn't write it. Either the
    original sender or recipient may call this — the note is attributed to
    whoever actually sent it, and delivered to the other one."""
    if not await require_private(update):
        return
    if text is not None and len(text) > MAX_TEXT_LENGTH:
        await update.message.reply_text(f"Please keep each note under {MAX_TEXT_LENGTH} characters; nothing was saved or sent.")
        return

    with db() as conn:
        row = conn.execute("SELECT sender_id, recipient_id, link_session FROM moments WHERE id = ?", (moment_id,)).fetchone()

        if not row:
            await update.message.reply_text("That moment isn't available anymore.")
            return

        sender_id, recipient_id, link_session = row
        author_id = update.effective_user.id
        if author_id not in (sender_id, recipient_id):
            await update.message.reply_text("That moment wasn't sent to you.")
            return
        target_id = recipient_id if author_id == sender_id else sender_id

        if not moment_link_still_valid(sender_id, recipient_id, link_session):
            await update.message.reply_text("This connection isn't active anymore, so that note wasn't sent.")
            return

        source_id = update.message.message_id
        if conn.execute("SELECT 1 FROM moment_notes WHERE author_id=? AND source_message_id=?", (author_id, source_id)).fetchone():
            await update.message.reply_text("Note already saved.")
            return
        stamp = now_iso()
        note_content_type = "voice" if voice_file_id else "text"
        cursor = conn.execute(
            "INSERT INTO moment_notes(moment_id,author_id,text,created_at,source_message_id,delivery_status,content_type,file_id) "
            "VALUES(?,?,?,?,?,'pending',?,?)",
            (moment_id, author_id, text or "", stamp, source_id, note_content_type, voice_file_id))
        note_id = cursor.lastrowid
        conn.execute("UPDATE moments SET responded_at=? WHERE id=?", (stamp, moment_id))
        conn.commit()

    reply_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("💬 Reply", callback_data=f"note:{moment_id}")]])
    if is_paused(target_id):
        status = "paused"
    elif voice_file_id:
        status = await send_tracked(context, target_id, voice_file_id=voice_file_id, reply_markup=reply_keyboard,
                                     moment_id=moment_id, reply_to=original_message_id(moment_id, target_id), kind="reply")
    else:
        status = await send_tracked(context, target_id, text=f"💬 {text}", reply_markup=reply_keyboard,
                                     moment_id=moment_id, reply_to=original_message_id(moment_id, target_id), kind="reply")
    with db() as conn:
        conn.execute("UPDATE moment_notes SET delivery_status=? WHERE id=?", (status, note_id))
        conn.commit()
    if status == "delivered":
        await update.message.reply_text("Sent 💌")
    else:
        await update.message.reply_text("Note saved with this moment, but delivery couldn't be confirmed. It won't be resent automatically.")


# ---------------------------------------------------------------------------
# Resurfacing
# ---------------------------------------------------------------------------
async def on_this_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_private(update):
        return

    user_id = update.effective_user.id
    today = local_now()
    month_day = today.strftime("%m-%d")

    with db() as conn:
        rows = conn.execute(
            "SELECT id, sender_id, recipient_id, content_type, file_id, text, created_at, reaction, reply_note, delivery_status "
            "FROM moments WHERE (sender_id = ? OR recipient_id = ?) AND delivery_status IN ('delivered', 'legacy') "
            "AND strftime('%m-%d', created_at, '+8 hours') = ? ORDER BY created_at ASC",
            (user_id, user_id, month_day),
        ).fetchall()

    # Singapore is UTC+8; both the date query and year check use local time.
    rows = [r for r in rows if local_time(r[6]).year < today.year]

    if not rows:
        await update.message.reply_text("Nothing from this day in a previous year yet.")
        return

    await update.message.reply_text(f"📅 On this day — {len(rows)} moment(s):")

    for m_id, sender_id, recipient_id, content_type, file_id, text, created_at_str, reaction, reply_note, delivery_status in rows:
        years_ago = today.year - local_time(created_at_str).year
        heading = f"📅 {years_ago} year{'s' if years_ago != 1 else ''} ago ·"
        meta = memory_details(m_id, user_id, sender_id, created_at_str,
                              reaction, reply_note, delivery_status, heading)

        # One failed resend (e.g. an expired file_id) shouldn't stop the rest
        # of the batch from showing.
        try:
            if content_type == "voice":
                await context.bot.send_voice(chat_id=user_id, voice=file_id, caption=text or None)
                await send_memory_text(context, user_id, meta)
            elif content_type == "photo":
                # Photo captions have a hard 1024-char Telegram limit, and
                # `text` here is already capped to that on its own — but
                # combining it with the metadata line could still blow past
                # it, so the metadata goes as a separate follow-up message
                # instead of getting appended to the caption.
                await context.bot.send_photo(chat_id=update.effective_chat.id, photo=file_id, caption=text or None)
                await send_memory_text(context, user_id, meta)
            else:
                await send_memory_text(context, user_id, f"{text}\n\n{meta}")
        except Exception as e:
            print(f"/onthisday: failed to resend moment {m_id}: {type(e).__name__}: {e}")
            await update.message.reply_text(f"⚠️ Couldn't show one moment from {meta.splitlines()[0]}.")


async def random_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await require_private(update):
        if query:
            await query.answer("Please use the bot in a private chat.")
        return
    previous_id = -1
    if query:
        await query.answer()
        try:
            previous_id = int(query.data.split(":", 1)[1])
        except (ValueError, IndexError):
            pass
    user_id = update.effective_user.id
    with db() as conn:
        # Prefer a different memory on the next tap, but allow a one-item archive.
        row = conn.execute(
            "SELECT id,sender_id,content_type,file_id,text,created_at,reaction,reply_note,delivery_status "
            "FROM moments WHERE (sender_id=? OR recipient_id=?) "
            "AND delivery_status IN ('delivered','legacy') "
            "ORDER BY (id=?) ASC, RANDOM() LIMIT 1",
            (user_id, user_id, previous_id),
        ).fetchone()
    if not row:
        await context.bot.send_message(chat_id=user_id, text="No shared memories yet. Once a moment is delivered, you can revisit it here.")
        return
    moment_id, sender_id, kind, file_id, text, created_at, reaction, note, status = row
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🎲 Another memory", callback_data=f"memory:{moment_id}")]])
    meta = memory_details(moment_id, user_id, sender_id, created_at, reaction, note, status)
    try:
        if kind == "voice":
            await context.bot.send_voice(chat_id=user_id, voice=file_id, caption=text or None)
        elif kind == "photo":
            await context.bot.send_photo(chat_id=user_id, photo=file_id)
            if text:
                await send_memory_text(context, user_id, "Original caption: " + text)
        elif text:
            await send_memory_text(context, user_id, text)
        await send_memory_text(context, user_id, meta, keyboard)
    except Exception as e:
        print(f"random_memory failed for moment {moment_id}: {type(e).__name__}: {e}")
        await send_safe(context, user_id, "Couldn't show that memory right now.", reply_markup=keyboard)


async def send_memory_text(context, user_id, text, keyboard=None):
    # Conservative chunks also accommodate old, uncapped archive entries.
    chunks = [text[i:i + 1800] for i in range(0, len(text), 1800)]
    for index, chunk in enumerate(chunks):
        await context.bot.send_message(chat_id=user_id, text=chunk,
            reply_markup=keyboard if index == len(chunks) - 1 else None)


# ---------------------------------------------------------------------------
# Diary — private moment-by-moment browsing (Phase 3, core)
#
# No stored cursor: every button carries its target moment id, so it's
# restart-safe and immune to a new moment landing mid-browse. Navigating (or
# re-running /diary) deletes the previous diary message(s) first, so at most
# one moment is ever visible in the chat instead of piling one up per tap.
# Read-only — no partner notifications, and it works while paused.
#
# Note: the filter string below is duplicated in three functions rather than
# hoisted to a module constant. The test suite execs this file's top-level
# *functions* in isolation (see tests/test_review_fixes.py) — a module-level
# constant wouldn't be carried over, so every name these functions need has
# to either be a builtin, a parameter, or defined inside the function itself.
# ---------------------------------------------------------------------------
def diary_moment_row(conn, user_id, moment_id):
    filt = "(sender_id=? OR recipient_id=?) AND delivery_status IN ('delivered','legacy') AND created_at IS NOT NULL"
    return conn.execute(
        "SELECT id, sender_id, recipient_id, content_type, file_id, text, created_at, reaction, reply_note, delivery_status, link_session "
        f"FROM moments WHERE id=? AND {filt}",
        (moment_id, user_id, user_id),
    ).fetchone()


def diary_newest_id(conn, user_id):
    filt = "(sender_id=? OR recipient_id=?) AND delivery_status IN ('delivered','legacy') AND created_at IS NOT NULL"
    row = conn.execute(f"SELECT id FROM moments WHERE {filt} ORDER BY id DESC LIMIT 1", (user_id, user_id)).fetchone()
    return row[0] if row else None


def diary_neighbor_id(conn, user_id, anchor_id, older):
    filt = "(sender_id=? OR recipient_id=?) AND delivery_status IN ('delivered','legacy') AND created_at IS NOT NULL"
    comparison = "id < ? ORDER BY id DESC" if older else "id > ? ORDER BY id ASC"
    row = conn.execute(f"SELECT id FROM moments WHERE {filt} AND {comparison} LIMIT 1",
                       (user_id, user_id, anchor_id)).fetchone()
    return row[0] if row else None


def diary_boundary_marker(conn, user_id, moment_id, moment_session):
    """True if the next-newer moment belongs to a different pairing session
    (NULL/legacy counts as its own session) — i.e. moment_id is the newest
    moment of an earlier session, viewed while paging backward."""
    filt = "(sender_id=? OR recipient_id=?) AND delivery_status IN ('delivered','legacy') AND created_at IS NOT NULL"
    row = conn.execute(f"SELECT link_session FROM moments WHERE {filt} AND id > ? ORDER BY id ASC LIMIT 1",
                       (user_id, user_id, moment_id)).fetchone()
    if row is None:
        return False  # nothing newer to compare against
    return row[0] != moment_session


def diary_months(conn, user_id):
    filt = "(sender_id=? OR recipient_id=?) AND delivery_status IN ('delivered','legacy') AND created_at IS NOT NULL"
    rows = conn.execute(
        f"SELECT strftime('%Y-%m', created_at, '+8 hours') AS ym, COUNT(*) FROM moments "
        f"WHERE {filt} GROUP BY ym ORDER BY ym DESC",
        (user_id, user_id),
    ).fetchall()
    return [(ym, count) for ym, count in rows if ym]


def diary_month_newest_id(conn, user_id, year_month):
    filt = "(sender_id=? OR recipient_id=?) AND delivery_status IN ('delivered','legacy') AND created_at IS NOT NULL"
    row = conn.execute(
        f"SELECT id FROM moments WHERE {filt} AND strftime('%Y-%m', created_at, '+8 hours') = ? ORDER BY id DESC LIMIT 1",
        (user_id, user_id, year_month),
    ).fetchone()
    return row[0] if row else None


def diary_month_label(year_month):
    year, month = year_month.split("-")
    return datetime(int(year), int(month), 1).strftime("%B %Y")


def diary_keyboard(moment_id, older_id, newer_id):
    nav = []
    if older_id is not None:
        nav.append(InlineKeyboardButton("⬅️ Older", callback_data=f"diary:at:{older_id}"))
    if newer_id is not None:
        nav.append(InlineKeyboardButton("Newer ➡️", callback_data=f"diary:at:{newer_id}"))
    rows = [nav] if nav else []
    rows.append([InlineKeyboardButton("📅 Months", callback_data=f"diary:months:{moment_id}")])
    return InlineKeyboardMarkup(rows)


def diary_months_keyboard(months, back_id):
    buttons = [InlineKeyboardButton(f"{diary_month_label(ym)} · {count}", callback_data=f"diary:month:{ym}")
               for ym, count in months]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"diary:at:{back_id}")])
    return InlineKeyboardMarkup(rows)


async def diary_send_text(context, user_id, text, keyboard=None):
    # Conservative chunks also accommodate old, uncapped archive entries.
    chunks = [text[i:i + 1800] for i in range(0, len(text), 1800)]
    ids = []
    for index, chunk in enumerate(chunks):
        sent = await context.bot.send_message(chat_id=user_id, text=chunk,
            reply_markup=keyboard if index == len(chunks) - 1 else None)
        ids.append(sent.message_id)
    return ids


async def diary_clear(context, user_id):
    """Deletes whatever diary view is currently shown to this user."""
    with db() as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT message_id FROM moment_messages WHERE chat_id=? AND kind='diary'", (user_id,)
        ).fetchall()]
        conn.execute("DELETE FROM moment_messages WHERE chat_id=? AND kind='diary'", (user_id,))
        conn.commit()
    for message_id in ids:
        try:
            await context.bot.delete_message(chat_id=user_id, message_id=message_id)
        except Exception as e:
            # Telegram only allows deleting a bot's own messages for 48h;
            # past that (or if it's already gone) this is harmless to skip.
            print(f"diary_clear: delete_message failed (non-fatal) chat_id={user_id} message_id={message_id}: {type(e).__name__}: {e}")


async def diary_render(context, user_id, moment_id) -> bool:
    """Sends `moment_id` as the user's diary view. Returns False without
    sending anything if it doesn't belong to this user's diary (wrong
    owner, wrong status, or gone)."""
    with db() as conn:
        row = diary_moment_row(conn, user_id, moment_id)
        if row is None:
            return False
        older_id = diary_neighbor_id(conn, user_id, moment_id, older=True)
        newer_id = diary_neighbor_id(conn, user_id, moment_id, older=False)
        (m_id, sender_id, recipient_id, content_type, file_id, text,
         created_at, reaction, note, status, link_session) = row
        crosses_boundary = diary_boundary_marker(conn, user_id, m_id, link_session)

    keyboard = diary_keyboard(m_id, older_id, newer_id)
    details = memory_details(m_id, user_id, sender_id, created_at, reaction, note, status, heading="📖")
    if crosses_boundary:
        details = f"— earlier connection —\n{details}"

    sent_ids = []
    media_failed = False
    if content_type == "voice":
        try:
            sent = await context.bot.send_voice(chat_id=user_id, voice=file_id, caption=text or None)
            sent_ids.append(sent.message_id)
        except Exception as e:
            print(f"diary_render: voice unavailable for moment {m_id}: {type(e).__name__}: {e}")
            media_failed = True
    elif content_type == "photo":
        try:
            sent = await context.bot.send_photo(chat_id=user_id, photo=file_id, caption=text or None)
            sent_ids.append(sent.message_id)
        except Exception as e:
            print(f"diary_render: photo unavailable for moment {m_id}: {type(e).__name__}: {e}")
            media_failed = True

    if content_type == "text":
        body = f"{text}\n\n{details}" if text else details
    elif media_failed:
        body = f"(media unavailable)\n\n{details}"
    else:
        body = details

    sent_ids.extend(await diary_send_text(context, user_id, body, keyboard))

    with db() as conn:
        for message_id in sent_ids:
            conn.execute("INSERT OR IGNORE INTO moment_messages VALUES(?,?,?,?,?)",
                         (user_id, message_id, m_id, "diary", now_iso()))
        conn.commit()
    return True


async def diary_open(context, user_id, moment_id):
    """Shared entry point for /diary and diary navigation: clears whatever
    diary view is currently shown, then renders `moment_id`."""
    await diary_clear(context, user_id)
    if not await diary_render(context, user_id, moment_id):
        await context.bot.send_message(chat_id=user_id, text="This moment isn't available anymore.")


async def diary_open_months(context, user_id, back_id):
    """Clears whatever diary view is shown and renders the month picker,
    with Back returning to `back_id`."""
    await diary_clear(context, user_id)
    with db() as conn:
        months = diary_months(conn, user_id)
    keyboard = diary_months_keyboard(months, back_id)
    sent = await context.bot.send_message(chat_id=user_id, text="📅 Pick a month:", reply_markup=keyboard)
    with db() as conn:
        # moment_id 0: this message isn't about any one moment (no real
        # moment id is ever 0, since the table's ids start at 1).
        conn.execute("INSERT OR IGNORE INTO moment_messages VALUES(?,?,?,?,?)",
                     (user_id, sent.message_id, 0, "diary", now_iso()))
        conn.commit()


async def diary_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_private(update):
        return
    user_id = update.effective_user.id
    with db() as conn:
        newest_id = diary_newest_id(conn, user_id)
    if newest_id is None:
        await diary_clear(context, user_id)
        await update.message.reply_text("Nothing in the diary yet — moments show up here once they've been delivered.")
        return
    await diary_open(context, user_id, newest_id)


async def diary_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_private_chat(update):
        await query.answer("Please use the bot in a private chat.", show_alert=True)
        return
    parts = query.data.split(":", 2)
    if len(parts) != 3:
        await query.answer("That button is no longer available.")
        return
    action, value = parts[1], parts[2]
    user_id = query.from_user.id

    if action in ("at", "months"):
        try:
            target_id = int(value)
        except ValueError:
            await query.answer("That button is no longer available.")
            return
        await query.answer()
        if action == "at":
            await diary_open(context, user_id, target_id)
        else:
            await diary_open_months(context, user_id, target_id)
    elif action == "month":
        await query.answer()
        with db() as conn:
            moment_id = diary_month_newest_id(conn, user_id, value)
        await diary_open(context, user_id, moment_id)
    else:
        await query.answer("That button is no longer available.")


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_private(update):
        return
    msg = (
        "*A little bit of my day*\n\n"
        "Send a photo, voice message, or a short note any time — it goes straight to your partner, no approval step.\n\n"
        "They can tap a reaction, reply to a received moment with a text or voice note, or just leave it — nothing is required. Either of you can keep replying, and multiple notes are kept together.\n\n"
        "`/start` — get a pairing code (or begin if you already have one)\n"
        "`/link <code>` — connect using a code your partner sent you\n"
        "`/unlink` — disconnect (asks to confirm)\n"
        "`/pause` — stop moments from reaching you for now, silently\n"
        "`/resume` — turn deliveries back on\n"
        "`/cancel` — cancel a note you tapped 'Add a note' for but haven't sent yet\n"
        "`/reactions` — view or customize your pair's reactions\n"
        "`/memory` — revisit a random memory, just for you\n"
        "`/diary` — browse your shared moments, one at a time\n"
        "Dates and times use Singapore time.\n"
        "`/onthisday` — see what you shared on this date in previous years\n"
        "`/help` — show this message"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if not TOKEN or not TURSO_URL or not TURSO_TOKEN:
        print("Error: BOT_TOKEN, TURSO_DATABASE_URL, and TURSO_AUTH_TOKEN must be set.")
        exit(1)

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("link", link))
    app.add_handler(CommandHandler("unlink", unlink))
    app.add_handler(CommandHandler("pause", pause))
    app.add_handler(CommandHandler("resume", resume))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("reactions", reactions_command))
    app.add_handler(CommandHandler("memory", random_memory))
    app.add_handler(CommandHandler("random", random_memory))
    app.add_handler(CommandHandler("onthisday", on_this_day))
    app.add_handler(CommandHandler("diary", diary_command))
    app.add_handler(CommandHandler("help", help_command))

    app.add_handler(CallbackQueryHandler(unlink_callback, pattern="^unlink(confirm|cancel):"))
    app.add_handler(CallbackQueryHandler(react_callback, pattern="^(react|choice):"))
    app.add_handler(CallbackQueryHandler(note_callback, pattern="^note:"))
    app.add_handler(CallbackQueryHandler(random_memory, pattern="^memory:"))
    app.add_handler(CallbackQueryHandler(diary_callback, pattern="^diary:"))

    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (filters.PHOTO | filters.VOICE | (filters.TEXT & ~filters.COMMAND)),
        handle_incoming,
    ))

    # Catch pending->uncertain recovery on a timer, not just at startup —
    # see periodic_recovery(). Requires the python-telegram-bot[job-queue]
    # extra (APScheduler) to be installed.
    if app.job_queue is not None:
        app.job_queue.run_repeating(periodic_recovery, interval=300, first=300)
    else:
        print("job_queue unavailable (install python-telegram-bot[job-queue] for periodic recovery) — "
              "falling back to startup-only recovery.")

    print("Moments bot connected to Turso Cloud DB and running...")
    app.run_polling()
