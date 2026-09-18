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
    migration = "preserve_existing_reply_notes_v1"
    if not conn.execute("SELECT 1 FROM schema_migrations WHERE name=?", (migration,)).fetchone():
        conn.execute("""
            INSERT INTO moment_notes(moment_id,author_id,text,created_at,delivery_status)
            SELECT id,recipient_id,reply_note,responded_at,'legacy'
            FROM moments WHERE reply_note IS NOT NULL AND reply_note != ''
        """)
        conn.execute("INSERT INTO schema_migrations VALUES(?)", (migration,))
    album_schema(conn)
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


def album_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS album_name_prompts (
        user_id INTEGER NOT NULL, message_id INTEGER NOT NULL, album_id INTEGER NOT NULL,
        created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(user_id,message_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS albums (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_low INTEGER NOT NULL,
        user_high INTEGER NOT NULL, link_session TEXT NOT NULL,
        tag_key TEXT NOT NULL, display_name TEXT NOT NULL,
        UNIQUE(user_low,user_high,link_session,tag_key))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS album_moments (
        album_id INTEGER NOT NULL, moment_id INTEGER NOT NULL,
        PRIMARY KEY(album_id,moment_id))''')
    conn.execute('CREATE INDEX IF NOT EXISTS album_moment_lookup ON album_moments(moment_id)')


def extract_album_tags(text):
    import re
    import unicodedata
    labels = []
    seen = set()
    for match in re.finditer(r'(?<![\w#])#(\w+)', unicodedata.normalize('NFC', text or '')):
        label = match.group(1)
        key = label.casefold()
        if len(label) <= 32 and key not in seen:
            labels.append((key, label))
            seen.add(key)
        if len(labels) == 10:
            break
    return labels


def album_moment(conn, user_id, moment_id, edit=False):
    row = conn.execute('''SELECT id,sender_id,recipient_id,content_type,file_id,text,
        created_at,reaction,reply_note,delivery_status,link_session FROM moments
        WHERE id=? AND (sender_id=? OR recipient_id=?)
        AND delivery_status IN ('delivered','legacy')''', (moment_id,user_id,user_id)).fetchone()
    if not row:
        return None
    if edit and (not row[10] or not moment_link_still_valid(row[1],row[2],row[10])):
        return None
    return row


def moment_album_labels(conn, moment):
    return conn.execute('''SELECT a.id,a.display_name FROM albums a
        JOIN album_moments am ON am.album_id=a.id WHERE am.moment_id=?
        AND a.user_low=? AND a.user_high=? AND a.link_session=? ORDER BY a.tag_key''',
        (moment[0],min(moment[1],moment[2]),max(moment[1],moment[2]),moment[10])).fetchall()


def apply_album_tags(user_id, moment_id, tags, remove=False):
    with db() as conn:
        moment = album_moment(conn,user_id,moment_id,edit=True)
        if not moment:
            return False
        scope=(min(moment[1],moment[2]),max(moment[1],moment[2]),moment[10])
        for key,label in tags:
            if not remove:
                conn.execute('INSERT OR IGNORE INTO albums(user_low,user_high,link_session,tag_key,display_name) VALUES(?,?,?,?,?)',(*scope,key,label))
            row=conn.execute('SELECT id FROM albums WHERE user_low=? AND user_high=? AND link_session=? AND tag_key=?',(*scope,key)).fetchone()
            if row:
                if remove:
                    conn.execute('DELETE FROM album_moments WHERE album_id=? AND moment_id=?',(row[0],moment_id))
                else:
                    conn.execute('INSERT OR IGNORE INTO album_moments VALUES(?,?)',(row[0],moment_id))
        conn.commit()
        return True


def album_keyboard(rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(label,callback_data=data) for label,data in row] for row in rows])


async def albums_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_private(update):
        return
    await album_panel(context,update.effective_user.id,'home',0,0)


async def album_name_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_private(update):
        return
    import unicodedata
    try:
        album_id=int(context.args[0])
        name=' '.join(context.args[1:]).strip()
    except (IndexError,ValueError):
        name=''
    await save_album_name(update,context,album_id if name else 0,name)


async def save_album_name(update,context,album_id,name):
    import unicodedata
    if not name or len(name)>64 or any(unicodedata.category(c) in ('Cc','Cs') for c in name):
        await update.message.reply_text('Use /albumname <album ID> <name with optional emoji>, up to 64 characters. Open an album → Customise name to find its ID.')
        return False
    user_id=update.effective_user.id
    with db() as conn:
        album=conn.execute('SELECT user_low,user_high,link_session,tag_key FROM albums WHERE id=? AND (user_low=? OR user_high=?)',(album_id,user_id,user_id)).fetchone()
        if not album or not moment_link_still_valid(*album[:3]):
            await update.message.reply_text('That album is unavailable for editing. Only your current pairing’s albums can be renamed.')
            return False
        conn.execute('UPDATE albums SET display_name=? WHERE id=?',(name,album_id))
        conn.commit()
    await update.message.reply_text(f'Album name saved: {name}\nKeep using #{album[3]} to add moments. Your partner will see the name when browsing; no notification is sent.')
    return True


def album_button_name(key,name):
    return '#'+name if name.casefold()==key else f'{name} · #{key}'


async def tags_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_private(update):
        return
    user_id=update.effective_user.id
    args=context.args
    try:
        moment_id=int(args[0])
    except (IndexError,ValueError):
        await update.message.reply_text('Open /albums → All moments → Manage tags. Or use /tags <moment ID> add #Bangkok #Food (or remove).')
        return
    if len(args)==1:
        await album_panel(context,user_id,'edit',moment_id,0)
        return
    if len(args)<3 or args[1].lower() not in ('add','remove'):
        await update.message.reply_text(f'Use /tags {moment_id} add #Bangkok #Food or /tags {moment_id} remove #Food.')
        return
    raw=' '.join(args[2:])
    tags=extract_album_tags(raw)
    # Commands are strict; never silently truncate a requested edit.
    if not tags or len(args[2:])>10 or any(not x.startswith('#') or extract_album_tags(x)!=[(x[1:].casefold(),x[1:])] for x in args[2:]):
        await update.message.reply_text('Use 1–10 hashtags, each up to 32 letters, numbers or underscores. Example: #Bangkok #Raya2026')
        return
    if not apply_album_tags(user_id,moment_id,tags,args[1].lower()=='remove'):
        await update.message.reply_text('That moment cannot be edited. Only moments in your active pairing session can be changed.')
        return
    await album_panel(context,user_id,'edit',moment_id,0)


async def album_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query
    if not is_private_chat(update):
        await query.answer('Open /albums in your private chat with the bot.')
        return
    try:
        _,action,item,page=query.data.split(':')
        item,page=int(item),int(page)
        if action not in ('home','earlier','scope','open','all','edit','add','remove','name') or item<0 or page<0:
            raise ValueError()
    except (ValueError,TypeError):
        await query.answer('This button is unavailable.')
        return
    await query.answer()
    user_id=update.effective_user.id
    if action in ('add','remove'):
        # item is moment ID; page is album ID. Both scope and membership are checked.
        with db() as conn:
            moment=album_moment(conn,user_id,item,edit=True)
            album=conn.execute('SELECT user_low,user_high,link_session,tag_key,display_name FROM albums WHERE id=?',(page,)).fetchone()
        if not moment or not album or tuple(album[:3])!=(min(moment[1],moment[2]),max(moment[1],moment[2]),moment[10]):
            await context.bot.send_message(chat_id=user_id,text='That album or moment is unavailable for editing.')
            return
        apply_album_tags(user_id,item,[tuple(album[3:])],action=='remove')
        await album_panel(context,user_id,'edit',item,0)
        return
    await album_panel(context,user_id,action,item,page)


def album_display_content(content,tag_keys):
    """Hide current organizational tags in browsing only; stored content stays intact."""
    import re
    import unicodedata
    def replace(match):
        key=unicodedata.normalize('NFC',match.group(1)).casefold()
        return '' if key in tag_keys else match.group(0)
    cleaned=re.sub(r'(?<![\w#])#(\w+)',replace,content or '')
    return '\n'.join(re.sub(r'[ \t]+',' ',line).strip() for line in cleaned.splitlines()).strip()


async def album_panel(context,user_id,action,item,page):
    # Read authorization is checked on every callback, not just on the first screen.
    # A scope is identified by an accessible moment ID; no session string is trusted from clients.
    rows=[]
    moment=None
    page_size=8
    with db() as conn:
        if action=='home':
            link=get_partner_id(user_id)
            current=None
            if link and moment_link_still_valid(user_id,link[0],session_token(link[2])):
                current=conn.execute("""SELECT MAX(id) FROM moments
                    WHERE MIN(sender_id,recipient_id)=? AND MAX(sender_id,recipient_id)=?
                    AND link_session=? AND delivery_status IN ('delivered','legacy')""",
                    (min(user_id,link[0]),max(user_id,link[0]),session_token(link[2]))).fetchone()[0]
            if current:
                # Resolve the current session server-side; retain authorization in scope.
                await album_panel(context,user_id,'scope',current,0)
                return
            text='📚 Your shared albums\nNo moments in your current pairing yet. Send a photo or note whenever you like.' if link else '📚 Your shared albums\nYou are not currently paired.'
            rows.append([('Earlier albums','alb:earlier:0:0')])
        elif action=='earlier':
            scopes=conn.execute("""SELECT MAX(id),sender_id,recipient_id,link_session,MAX(created_at)
                FROM moments WHERE (sender_id=? OR recipient_id=?)
                AND delivery_status IN ('delivered','legacy') AND link_session IS NOT NULL AND link_session!=''
                GROUP BY MIN(sender_id,recipient_id),MAX(sender_id,recipient_id),link_session
                ORDER BY MAX(created_at) DESC""",(user_id,user_id)).fetchall()
            scopes=[r for r in scopes if not moment_link_still_valid(r[1],r[2],r[3])]
            visible=scopes[page*page_size:(page+1)*page_size]
            text='📚 Earlier albums\nBrowsing is private. Earlier pairings are read-only.'
            if not visible:
                text+='\nNo earlier albums here. Older unassigned memories remain in /memory.'
            for mid,a,b,session,stamp in visible:
                rows.append([(f'Earlier pairing · {friendly_timestamp(stamp)}',f'alb:scope:{mid}:0')])
            if page: rows.append([('← Previous',f'alb:earlier:0:{page-1}')])
            if len(scopes)>(page+1)*page_size: rows.append([('Next →',f'alb:earlier:0:{page+1}')])
            rows.append([('Current albums','alb:home:0:0')])
        elif action in ('scope','all','edit'):
            ref=album_moment(conn,user_id,item,edit=action=='edit')
            if not ref or not ref[10]:
                await context.bot.send_message(chat_id=user_id,text='That moment or pairing session is unavailable. Older memories without a known session remain in /memory.')
                return
            scope=(min(ref[1],ref[2]),max(ref[1],ref[2]),ref[10])
            if action=='all':
                matches=conn.execute('''SELECT id FROM moments WHERE MIN(sender_id,recipient_id)=? AND MAX(sender_id,recipient_id)=?
                    AND link_session=? AND delivery_status IN ('delivered','legacy') ORDER BY id DESC LIMIT 2 OFFSET ?''',(*scope,page)).fetchall()
                if not matches:
                    text='No moment on this page.'
                else:
                    moment=album_moment(conn,user_id,matches[0][0])
                    text='All moments'
                    if page: rows.append([('← Previous',f'alb:all:{item}:{page-1}')])
                    if len(matches)>1: rows.append([('Next →',f'alb:all:{item}:{page+1}')])
                rows.append([('Back to albums',f'alb:scope:{item}:0')])
            else:
                albums=conn.execute('''SELECT id,tag_key,display_name FROM albums WHERE user_low=? AND user_high=? AND link_session=?
                    ORDER BY tag_key LIMIT ? OFFSET ?''',(*scope,page_size+1,page*page_size)).fetchall()
                if action=='edit':
                    attached=moment_album_labels(conn,ref)
                    attached_ids={x[0] for x in attached}
                    text=f'Manage tags · moment {item}\n'+(', '.join(x[1] for x in attached) or 'No tags yet')
                    text+=f'\n\nCreate/add: /tags {item} add #Bangkok #Food\nRemove: /tags {item} remove #Food\nRemoving tags keeps the moment and all its notes.'
                    for aid,key,name in albums[:page_size]:
                        remove=aid in attached_ids
                        rows.append([(f'{"− Remove" if remove else "+ Add"} {album_button_name(key,name)}',f'alb:{"remove" if remove else "add"}:{item}:{aid}')])
                else:
                    text='📚 Albums for this pairing\nTags are optional. Open All moments to tag an older photo.'
                    rows.append([('All moments (including untagged)',f'alb:all:{item}:0')])
                    for aid,key,name in albums[:page_size]:
                        rows.append([(album_button_name(key,name),f'alb:open:{aid}:0')])
                if page: rows.append([('← Previous',f'alb:{action}:{item}:{page-1}')])
                if len(albums)>page_size: rows.append([('Next →',f'alb:{action}:{item}:{page+1}')])
                rows.append([('Earlier albums','alb:earlier:0:0')])
                rows.append([('Current albums','alb:home:0:0')])
        elif action=='name':
            album=conn.execute('SELECT user_low,user_high,link_session,tag_key,display_name FROM albums WHERE id=? AND (user_low=? OR user_high=?)',(item,user_id,user_id)).fetchone()
            if not album or not moment_link_still_valid(*album[:3]):
                await context.bot.send_message(chat_id=user_id,text='That album is unavailable for editing.')
                return
            prompt=await context.bot.send_message(chat_id=user_id,
                text=f'Rename “{album[4]}” (#{album[3]})\nReply to this message with the new name, emoji welcome (up to 64 characters).\nThis prompt expires in 15 minutes. /cancel cancels it. The hashtag stays the same.',
                reply_markup=ForceReply(selective=True,input_field_placeholder='New album name'))
            conn.execute('UPDATE album_name_prompts SET active=0 WHERE user_id=?',(user_id,))
            conn.execute('INSERT INTO album_name_prompts VALUES(?,?,?,?,1)',(user_id,prompt.message_id,item,now_iso()))
            conn.commit()
            return
        elif action=='open':
            album=conn.execute('SELECT user_low,user_high,link_session,display_name,tag_key FROM albums WHERE id=? AND (user_low=? OR user_high=?)',(item,user_id,user_id)).fetchone()
            if not album:
                await context.bot.send_message(chat_id=user_id,text='That album is unavailable.')
                return
            matches=conn.execute('''SELECT m.id FROM moments m JOIN album_moments am ON am.moment_id=m.id
                WHERE am.album_id=? AND MIN(m.sender_id,m.recipient_id)=? AND MAX(m.sender_id,m.recipient_id)=?
                AND m.link_session=? AND m.delivery_status IN ('delivered','legacy') ORDER BY m.id DESC LIMIT 2 OFFSET ?''',(item,*album[:3],page)).fetchall()
            text='📚 '+album[3]+'\nAdd moments with: #'+album[4]
            if moment_link_still_valid(*album[:3]):
                rows.append([('Customise name',f'alb:name:{item}:0')])
            if matches:
                moment=album_moment(conn,user_id,matches[0][0])
                if page: rows.append([('← Previous',f'alb:open:{item}:{page-1}')])
                if len(matches)>1: rows.append([('Next →',f'alb:open:{item}:{page+1}')])
                rows.append([('Back to albums',f'alb:scope:{moment[0]}:0')])
            else:
                text+='\nNo moments in this album on this page.'
                rows.append([('Earlier albums','alb:earlier:0:0')])
                rows.append([('Current albums','alb:home:0:0')])
        else:
            return
        if moment:
            labels=moment_album_labels(conn,moment)
            other_labels=[x[1] for x in labels if action!='open' or x[0]!=item]
            if other_labels:
                text+='\n'+('Also in: ' if action=='open' else 'Albums: ')+', '.join(other_labels)
            tag_keys={r[0] for r in conn.execute("""SELECT a.tag_key FROM albums a
                JOIN album_moments am ON am.album_id=a.id WHERE am.moment_id=?
                AND a.user_low=? AND a.user_high=? AND a.link_session=?""",
                (moment[0],min(moment[1],moment[2]),max(moment[1],moment[2]),moment[10])).fetchall()}
            if moment[10] and moment_link_still_valid(moment[1],moment[2],moment[10]):
                rows.append([('Manage tags',f'alb:edit:{moment[0]}:0')])
    keyboard=album_keyboard(rows)
    if moment:
        mid,sender,recipient,kind,file_id,content,stamp,reaction,note,status,session=moment
        if kind=='photo':
            delivered=await send_safe(context,user_id,photo_file_id=file_id)
            if not delivered:
                await context.bot.send_message(chat_id=user_id,text='Could not load the photo. You can still browse its details.')
        content=album_display_content(content,tag_keys)
        if content:
            await send_memory_text(context,user_id,content)
        text+='\n\n'+memory_details(mid,user_id,sender,stamp,reaction,note,status,heading='📖')
    await send_memory_text(context,user_id,text,keyboard)



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
                        photo_file_id: str = None, reply_markup=None, moment_id=None, reply_to=None) -> str:
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
        if photo_file_id:
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
        remember_message(chat_id, sent.message_id, moment_id, "received")
    return "delivered"


def remember_message(chat_id, message_id, moment_id, kind):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO moment_messages VALUES(?,?,?,?,?)",
                     (chat_id, message_id, moment_id, kind, now_iso()))
        conn.commit()


def original_message_id(moment_id, chat_id):
    with db() as conn:
        row = conn.execute("SELECT message_id FROM moment_messages WHERE moment_id=? AND chat_id=? AND kind='original' LIMIT 1",
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
        rows = conn.execute("SELECT author_id,text,created_at,delivery_status FROM moment_notes WHERE moment_id=? ORDER BY id", (moment_id,)).fetchall()
    if not rows:
        return ["Notes", old_note] if old_note else []
    moment_day = local_time(moment[0]).date() if moment and moment[0] else None
    lines = []
    last_author = None
    for author, text, stamp, status in rows:
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
        lines.append(f"{label} · {text}")
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
            "You're already linked. Send a photo or a little note any time — "
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

    await update.message.reply_text("You're linked! Send a photo or a little note whenever — it'll go straight to them.")
    await send_safe(context, code_owner_id, "You're linked! Send a photo or a little note whenever — it'll go straight to them.")


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
        conn.execute("DELETE FROM moment_messages WHERE chat_id IN (?,?) AND kind='prompt'", (requester_id, partner_id))
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
        renamed = conn.execute("UPDATE album_name_prompts SET active=0 WHERE user_id=? AND active=1", (user_id,)).rowcount
        cursor = conn.execute("DELETE FROM pending_notes WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM moment_messages WHERE chat_id=? AND kind='prompt'", (user_id,))
        conn.commit()
        deleted = cursor.rowcount
    if renamed:
        await update.message.reply_text("Album renaming cancelled." + (" Pending note cancelled too." if deleted else ""))
    elif deleted:
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

    replied = getattr(message, 'reply_to_message', None)
    if replied:
        with db() as conn:
            rename=conn.execute('SELECT album_id,created_at,active FROM album_name_prompts WHERE user_id=? AND message_id=?',(user_id,replied.message_id)).fetchone()
        if rename:
            if not rename[2] or minutes_since(rename[1])>15:
                await message.reply_text('That rename prompt is no longer active. Open the album and tap Customise name again. Nothing was sent.')
                return
            if not message.text:
                await message.reply_text('Reply with a text name, with optional emoji, or /cancel.')
                return
            if await save_album_name(update,context,rename[0],message.text.strip()):
                with db() as conn:
                    conn.execute('UPDATE album_name_prompts SET active=0 WHERE user_id=? AND message_id=?',(user_id,replied.message_id))
                    conn.commit()
            return

    # Redelivered updates must not turn an already-saved button-mode note
    # into a new moment after its pending state has been consumed.
    with db() as conn:
        saved = conn.execute("SELECT 1 FROM moment_notes WHERE author_id=? AND source_message_id=?",
                             (user_id, message.message_id)).fetchone()
    if saved:
        await message.reply_text("Note already saved.")
        return

    replied = getattr(message, "reply_to_message", None)
    if replied is not None:
        with db() as conn:
            target = conn.execute("SELECT moment_id,kind,created_at FROM moment_messages WHERE chat_id=? AND message_id=?",
                                  (user_id, replied.message_id)).fetchone()
            if target and target[1] in ("received", "prompt"):
                conn.execute("DELETE FROM pending_notes WHERE user_id=?", (user_id,))
                conn.commit()
        if not target or target[1] not in ("received", "prompt"):
            await message.reply_text("I couldn't match that reply to a received moment. Use its Add a note button, or send without replying for a new moment.")
            return
        if target[1] == "prompt" and minutes_since(target[2]) > PENDING_NOTE_TTL_MINUTES:
            await message.reply_text("That note prompt expired. Tap Add a note again; nothing was sent.")
            return
        if not message.text:
            await message.reply_text("Notes are text-only for now. Reply with text, or send your photo without replying as a new moment.")
            return
        await save_note(update, context, target[0], message.text)
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

    if pending and message.text:
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
        else:
            await save_note(update, context, moment_id, message.text)
            return

    row = get_partner_id(user_id)
    if not row:
        await message.reply_text("You're not linked yet — send /start to get a pairing code.")
        return

    partner_id, _, my_linked_at = row

    if message.photo:
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
            "INSERT INTO moments (sender_id, recipient_id, content_type, file_id, text, created_at, delivery_status, link_session) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (user_id, partner_id, content_type, file_id, text, now_iso(), session_token(my_linked_at)),
        )
        moment_id = cursor.lastrowid
        conn.commit()

    remember_message(user_id, message.message_id, moment_id, "original")
    keyboard = reaction_keyboard(moment_id)
    if content_type == "photo":
        status = await send_tracked(context, partner_id, text=text, photo_file_id=file_id, reply_markup=keyboard, moment_id=moment_id)
    else:
        status = await send_tracked(context, partner_id, text=text, reply_markup=keyboard, moment_id=moment_id)

    with db() as conn:
        conn.execute("UPDATE moments SET delivery_status = ? WHERE id = ?", (status, moment_id))
        conn.commit()

    if status == "delivered":
        await message.reply_text("Sent 💌")
        # Optional organization happens after successful delivery and acknowledgment.
        tags = extract_album_tags(message.caption if message.photo else message.text)
        if tags:
            try:
                apply_album_tags(user_id, moment_id, tags)
            except Exception as e:
                print(f"Album tagging failed for moment {moment_id}: {type(e).__name__}")
                await message.reply_text(f"Your moment was sent, but its tags could not be saved. Retry with /tags {moment_id} add " + " ".join('#'+label for _,label in tags))
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
        if query.from_user.id != recipient_id:
            await query.answer("This one wasn't sent to you.", show_alert=True)
            return

        if not moment_link_still_valid(sender_id, recipient_id, link_session):
            await query.answer("This connection isn't active anymore.", show_alert=True)
            return

    await query.answer()
    # The callback message is the actual moment, including old messages
    # delivered before message IDs were recorded.
    target_id = query.message.message_id
    remember_message(recipient_id, target_id, moment_id, "received")
    preview = truncate(content or ("Photo" if kind == "photo" else "Moment"), 100)
    prompt = await context.bot.send_message(
        chat_id=recipient_id,
        text=f"Add a note · {friendly_timestamp(created_at)}\n{preview}\nReply here within {PENDING_NOTE_TTL_MINUTES} minutes, or /cancel.",
        reply_parameters=ReplyParameters(target_id, allow_sending_without_reply=True),
        reply_markup=ForceReply(selective=True, input_field_placeholder="Your note (optional)"),
    )
    with db() as conn:
        conn.execute("DELETE FROM moment_messages WHERE chat_id=? AND kind='prompt'", (recipient_id,))
        conn.execute("INSERT INTO pending_notes VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET moment_id=excluded.moment_id,created_at=excluded.created_at",
                     (recipient_id, moment_id, now_iso()))
        conn.execute("INSERT INTO moment_messages VALUES(?,?,?,?,?)", (recipient_id, prompt.message_id, moment_id, "prompt", now_iso()))
        conn.commit()


async def save_note(update: Update, context: ContextTypes.DEFAULT_TYPE, moment_id: int, note_text: str):
    if not await require_private(update):
        return
    if len(note_text) > MAX_TEXT_LENGTH:
        await update.message.reply_text(f"Please keep each note under {MAX_TEXT_LENGTH} characters; nothing was saved or sent.")
        return

    with db() as conn:
        row = conn.execute("SELECT sender_id, recipient_id, link_session FROM moments WHERE id = ?", (moment_id,)).fetchone()

        if not row:
            await update.message.reply_text("That moment isn't available anymore.")
            return

        sender_id, recipient_id, link_session = row
        if update.effective_user.id != recipient_id:
            await update.message.reply_text("That moment wasn't sent to you.")
            return

        if not moment_link_still_valid(sender_id, recipient_id, link_session):
            await update.message.reply_text("This connection isn't active anymore, so that note wasn't sent.")
            return

        source_id = update.message.message_id
        if conn.execute("SELECT 1 FROM moment_notes WHERE author_id=? AND source_message_id=?", (recipient_id, source_id)).fetchone():
            await update.message.reply_text("Note already saved.")
            return
        stamp = now_iso()
        cursor = conn.execute("INSERT INTO moment_notes(moment_id,author_id,text,created_at,source_message_id,delivery_status) VALUES(?,?,?,?,?,'pending')",
                              (moment_id, recipient_id, note_text, stamp, source_id))
        note_id = cursor.lastrowid
        conn.execute("UPDATE moments SET responded_at=? WHERE id=?", (stamp, moment_id))
        conn.commit()

    status = "paused" if is_paused(sender_id) else await send_tracked(
        context, sender_id, f"💬 {note_text}",
        reply_to=original_message_id(moment_id, sender_id))
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
            if content_type == "photo":
                # Photo captions have a hard 1024-char Telegram limit, and
                # `text` here is already capped to that on its own — but
                # combining it with the metadata line could still blow past
                # it, so the metadata goes as a separate follow-up message
                # instead of getting appended to the caption.
                await context.bot.send_photo(chat_id=update.effective_chat.id, photo=file_id, caption=text or None)
                await send_memory_text(context, user_id, meta)
            else:
                await send_memory_text(context, user_id, f"{text}\n\n{meta}")
            with db() as conn:
                editable = album_moment(conn, user_id, m_id, edit=True)
            if editable:
                await context.bot.send_message(chat_id=user_id, text="Organize this moment", reply_markup=album_keyboard([[('Manage tags',f'alb:edit:{m_id}:0')]]))
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
    buttons = [[InlineKeyboardButton("🎲 Another memory", callback_data=f"memory:{moment_id}")]]
    with db() as conn:
        if album_moment(conn, user_id, moment_id, edit=True):
            buttons.append([InlineKeyboardButton("Manage tags", callback_data=f"alb:edit:{moment_id}:0")])
    keyboard = InlineKeyboardMarkup(buttons)
    meta = memory_details(moment_id, user_id, sender_id, created_at, reaction, note, status)
    try:
        if kind == "photo":
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
# Help
# ---------------------------------------------------------------------------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_private(update):
        return
    msg = (
        "*A little bit of my day*\n\n"
        "Send a photo or a short note any time — it goes straight to your partner, no approval step.\n\n"
        "They can tap a reaction, reply to a received moment with a text note, or just leave it — nothing is required. Multiple notes are kept together.\n\n"
        "`/start` — get a pairing code (or begin if you already have one)\n"
        "`/link <code>` — connect using a code your partner sent you\n"
        "`/unlink` — disconnect (asks to confirm)\n"
        "`/pause` — stop moments from reaching you for now, silently\n"
        "`/resume` — turn deliveries back on\n"
        "`/cancel` — cancel a note you tapped 'Add a note' for but haven't sent yet\n"
        "`/reactions` — view or customize your pair's reactions\n"
        "`/albums` — browse shared albums and manage tags\n"
        "`/albumname ID name` — customise an album name (emoji welcome)\n"
        "Add optional #Bangkok #Food to photo captions or new text moments.\n"
        "`/tags <id> add #tag` or `/tags <id> remove #tag` — edit tags\n"
        "`/memory` — revisit a random memory, just for you\n"
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
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("albums", albums_command))
    app.add_handler(CommandHandler("tags", tags_command))
    app.add_handler(CommandHandler("albumname", album_name_command))
    app.add_handler(CallbackQueryHandler(album_callback, pattern="^alb:"))

    app.add_handler(CallbackQueryHandler(unlink_callback, pattern="^unlink(confirm|cancel):"))
    app.add_handler(CallbackQueryHandler(react_callback, pattern="^(react|choice):"))
    app.add_handler(CallbackQueryHandler(note_callback, pattern="^note:"))
    app.add_handler(CallbackQueryHandler(random_memory, pattern="^memory:"))

    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & (filters.PHOTO | (filters.TEXT & ~filters.COMMAND)),
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
