# Our Little Diary — voice + diary experiment

Separate copy of the current workspace bot, with voice messages as new moments
and a private `/diary` for browsing the shared archive. The original bot.py
was not changed.

## New behavior — voice moments

- Record a Telegram voice message in the bot's private chat to share it.
- The partner receives a playable voice message with custom reactions and Add a note.
- File ID, duration in seconds, timestamps, delivery state, and message IDs are saved.
- Text replies and reactions link to the original recording.
- /memory and /onthisday replay recordings with notes and Singapore-time metadata.
- Pause, unlink, session checks, and delivery uncertainty handling still apply.
- Voice reply notes are not supported. Recordings sent as replies or while a note
  prompt is active are rejected without forwarding; use /cancel and send without
  replying to create a new moment.
- No transcription or AI. Music/audio-file uploads and video notes are not included.

## New behavior — /diary

- `/diary` opens your newest delivered (or legacy) moment: photo, voice, or text,
  with its reaction and notes, same formatting as /memory.
- ⬅️ Older / Newer ➡️ step one moment at a time; omitted at either end of the archive.
- 📅 Months lists every month that has moments, with a count each; picking one jumps
  to that month's newest moment. ⬅️ Back returns to where you were.
- Navigating (or re-running /diary) deletes the previous diary message(s) first, so
  at most one moment is ever visible in the chat instead of piling one up per tap.
- An "— earlier connection —" line appears automatically when paging back crosses
  into an earlier pairing session (including into/out of pre-tracking legacy moments).
- Read-only: no partner notifications, and it works while paused.
- Excludes failed/uncertain/pending sends, same filter as /memory and /onthisday.
- Out of scope for this pass: search, jump-to-date, favorites, export, deleting or
  editing from the diary, reacting or noting from the diary.

## New Render service

Use a separate Telegram bot token and separate Turso database for this experiment.
Do not run this service using the existing bot's token or production database.

Upload this directory's contents to a new repository, or set Render's Root Directory
  to `voice-test` if deploying it from the same repository as the existing bot.

- Runtime: Python
- Build command: `pip install -r requirements.txt`
- Start command: `python bot.py`
- Python: `.python-version` selects 3.13; if setting PYTHON_VERSION in Render instead,
  use the known working full version `3.13.5` (the environment variable overrides the file).
- Environment: BOT_TOKEN, TURSO_DATABASE_URL, TURSO_AUTH_TOKEN for the new bot/database.
- PORT is supplied by Render. The existing health-check server is retained.

Tables are created at startup. The voice duration column is added automatically.
For a new bot, create new test moments; do not import Telegram file IDs from the
production bot, as file IDs are bot-specific.

## Live check

1. Pair two test accounts with /start and /link.
2. Record and send a short voice message in each direction; play both recordings.
3. React, add two text notes, and check the notifications reply to the original recording.
4. Try /memory; verify playback, notes, and the Another memory button.
5. Pause the recipient and send a recording: it must not arrive. Resume and send a new one.
6. Try a voice recording while replying or after Add a note: nothing should be forwarded.
7. Send a few more moments (mix of photo, voice, text) so there's something to browse,
   ideally spanning at least two calendar months (back-date a couple of test sends if
   needed, or just send some and wait).
8. Try /diary: it should open the newest moment. Step through with Older/Newer to the
   start of the archive and back. Confirm the buttons disappear at each end.
9. Tap 📅 Months: confirm every month you sent something in is listed with the right
   count, tapping one jumps to that month's newest moment, and Back returns you to
   where you tapped Months from.
10. Unlink, relink (same or different partner), send a new moment, then open /diary
    and page back past the relink point — the "— earlier connection —" line should
    appear exactly once, right before the older moment(s), and old buttons on
    pre-relink moments must still refuse new contact.
11. Confirm /diary does nothing to the partner (no notification) and still opens
    normally while paused.
12. Check Render logs for exceptions.

Automated tests: `python -m unittest discover -s tests -v`
41 tests passed against temporary SQLite and mocked Telegram calls (28 covering the
existing bot + voice, 13 covering /diary); live Telegram and Turso testing is still
required. No deployment or external messages were performed.
