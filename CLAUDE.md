# CLAUDE.md

Context file for any AI assistant working in this repository. Read this
before proposing or making changes. When suggesting code changes, explicitly
confirm the changes adhere to the AI COMPLETION CHECKLIST at the bottom
before presenting them.

## PROJECT OVERVIEW

A Discord bot that joins a D&D group's voice channel, records each speaker
separately, transcribes locally with Whisper, and posts an AI-written recap
to a text channel. Single private Discord server (one friend group), not a
public/multi-tenant bot. Runs 24/7 on a free-tier Oracle Cloud instance
constrained to **1 OCPU / 6GB RAM** — this sizing is deliberate (see
ANTI-PATTERNS) and should not be casually treated as a bottleneck to "fix"
by recommending more compute without flagging the real cost tradeoff.

## TECH STACK & DEPENDENCIES

- Python 3.10+
- `py-cord` — Discord library
- `faster-whisper` — local speech-to-text
- Anthropic API (Claude) — recap summarization
- SQLite via `aiosqlite`
- Host: Oracle Cloud "Always Free," Ubuntu 22.04, ARM (Ampere)
- Process manager: `systemd` (`dnd-bot.service`)

**CRITICAL — py-cord version constraint:** `requirements.txt` pins py-cord
to an **unreleased development branch** (`fix/voice-rec-2`), not a normal
PyPI release. Do not "resolve" this to a standard version pin. Discord's
DAVE (mandatory end-to-end voice encryption, since March 2026) broke voice
*receiving* in the official py-cord release; sending/playing audio is
unaffected, but this bot only records/receives. Before changing this pin,
verify https://github.com/Pycord-Development/pycord/issues/3139 is closed
with a real release that includes the fix — do not assume it's resolved.

## ARCHITECTURE & FILE MAP

- `bot.py` — entry point; loads cogs; configures `logging` once (all other
  files call `logging.getLogger("name")` and inherit this config)
- `config.py` — sole owner of environment variable reads; no other file
  should call `os.environ`/`os.getenv` directly
- `database.py` — sole owner of SQL; other files call plain async functions
  only
- `audio/transcriber.py` — wraps faster-whisper, lazy-loads the model once
- `summarizer.py` — wraps the Anthropic API call; contains the recap system
  prompt
- `cogs/session.py` — voice connection lifecycle, recording, the "take"
  system (one session = 1+ takes, split by pause/resume/unexpected
  disconnect), auto-reconnect logic. Highest-risk file for regressions —
  see STRICT RULES below before editing.
- `cogs/characters.py` — player/character/DM linking commands
- `cogs/notes.py` — browsing/searching past recaps

## COMMAND INTERFACE

All user interactions are Discord **Slash Commands** (py-cord
`SlashCommandGroup` / `@command`). Do not write legacy prefix-based
(`!command`) commands — none exist in this codebase and none should be
added.

## ENVIRONMENT VARIABLES & SECRETS

- Real secrets live in a `.env` file on the **server only**
  (`/home/ubuntu/dnd-notetaker/.env`) — never committed to git
  (`.gitignore`'d).
- `.env.example` (committed, no real values) documents every variable with
  a placeholder.
- To add a new secret/config value:
  1. Add it to `.env.example` with a placeholder value and a comment
  2. Read it in `config.py` (via `os.environ[...]` if required, or
     `os.getenv(..., default)` if optional — see existing pattern)
  3. Manually add the real value to the actual `.env` file on the server
     over SSH (e.g. `nano .env`) — this step is not automated
  4. Restart the `dnd-bot` service to pick up the change

## STRICT RULES & KNOWN QUIRKS

**Rule 1 — Never block the asyncio event loop.**
Context: `faster-whisper` transcription and the Anthropic API call are both
synchronous. Calling either directly inside `async` code freezes the entire
event loop — including the Discord gateway heartbeat — for as long as the
call takes, which previously caused the bot to silently disconnect/hang
after `/session end`.
Enforcement: Always wrap blocking calls as
`await loop.run_in_executor(None, fn, *args)`. Applies to any future
CPU-bound or blocking-I/O call, not just the two current ones.

**Rule 2 — Auto-reconnect must stay in place; do not remove without a
replacement.**
Context: Discord/py-cord has a known issue (pre-dates DAVE) where
listen-only voice bots (never transmit their own audio) get silently
disconnected after extended periods, with a clean-looking close code that
implies no error. This is an upstream issue, not fixable in this codebase.
Enforcement: `on_voice_state_update` in `cogs/session.py` detects
unexpected drops (`state.intentional_disconnect` is `False`) and
auto-rejoins + resumes recording as a new take, capped at
`MAX_AUTO_RECONNECT_ATTEMPTS = 3`. Any intentional disconnect path (new or
existing) must set `state.intentional_disconnect = True` first, or the
watchdog will misfire.

**Rule 3 — `/session end` and `/session pause` must remain idempotent.**
Context: A second invocation (e.g. retrying after a network hiccup, or
manual recovery from a stuck session) previously crashed with
`RecordingException: You are not recording`.
Enforcement: Both route through `_stop_recording_and_wait()`, which wraps
`stop_recording()` in try/except and tolerates "already stopped." Any new
command that stops/starts recording must use this same helper, not call
`stop_recording()`/`start_recording()` directly.

**Rule 4 — Transcription is slow; do not assume anything is "stuck" below
90 minutes.**
Context: This server has exactly 1 CPU core. Transcription runs
sequentially, one speaker at a time. A real 5-speaker session has taken 40+
minutes. `TRANSCRIPTION_TIMEOUT_SECONDS = 90 * 60` reflects this — do not
lower it without understanding this constraint, and do not add other
timeouts around transcription shorter than this.

**Rule 5 — Delete raw audio immediately after transcribing it.**
Context: Un-deleted per-speaker WAV files would grow disk usage unboundedly
across sessions (several GB/session at scale).
Enforcement: `_on_take_finished` in `cogs/session.py` deletes each WAV file
right after transcribing it, and removes the resulting empty take
directory. Any new code path that writes audio to disk must clean up the
same way.

**Rule 6 — Database schema changes must not break existing production
data.**
Context: The bot has real, irreplaceable session history in its SQLite DB.
Enforcement: New columns must be added via `ALTER TABLE` wrapped in
`try/except aiosqlite.OperationalError: pass` (see `init_db()` in
`database.py`), so re-running startup on an existing DB doesn't fail.

**Rule 7 — If a session appears stuck, do not restart the bot process as a
first response.**
Context: A stuck-but-not-crashed session may have already-completed,
in-memory transcription data (proven recoverable in a real incident by
manually flipping the session's DB status to bypass a crashing code path
and re-running `/session end`). Restarting the process destroys anything
not yet written to disk.
Enforcement: Investigate via `journalctl` and the database first. Only
restart once data has been confirmed either saved or unrecoverable.

## DEPLOYMENT INSTRUCTIONS

Production does **not** pull from GitHub. GitHub is a backup/showcase,
updated periodically, not the deployment mechanism. Actual deploy flow:

1. `python3 -m py_compile <file>` on every changed file first
2. Transfer to server: full-project `scp` for multi-file changes, or a
   `cat > file << 'EOF' ... EOF` heredoc pasted directly into the SSH
   session for single-file changes (avoid recommending manual `nano` edits
   beyond trivial one-line changes — has caused whitespace/tab corruption
   before)
3. `sudo systemctl stop dnd-bot` before overwriting files
4. `sudo systemctl daemon-reload && sudo systemctl restart dnd-bot`
5. Verify via `sudo journalctl -u dnd-bot -f`
6. Push the same change to GitHub — do not skip this; repo/production
   drift has happened before

## ANTI-PATTERNS (DO NOT IMPLEMENT WITHOUT EXPLICIT DISCUSSION)

- **No rate-limiting/queueing infrastructure.** Single private server,
  trusted users, already naturally throttled (one session active at a
  time; transcription itself is the bottleneck). Do not add Redis, job
  queues, or per-user cooldown systems speculatively.
- **No multi-guild/multi-tenant support.** SQLite schema and session state
  (`self.states: dict[guild_id, ...]`) assume one Discord server.
- **No CI/CD pipeline.** Deployment is intentionally manual/SSH-based at
  this scale.
- **No resizing the Oracle instance above 1 OCPU / 6GB** without explicit
  cost discussion — this server was previously accidentally provisioned at
  4x this size, which would have exceeded the free monthly allowance if
  left running.

## DEFERRED FEATURES (designed, not yet built — do not build unprompted)

- **Multi-campaign support.** Agreed design: explicit `campaign` parameter
  on setup/lookup commands (`/character link`, `/character set-dm`,
  `/notes recent`, `/notes search`). `/session start` takes `campaign`
  explicitly. `/session pause|resume|end|force-leave` do NOT take a
  campaign parameter — they resolve the active session via which voice
  channel the caller is currently in, so two campaigns can record
  concurrently. Rejected alternative: binding campaigns to fixed channels
  (explicitly ruled out by the project owner).
- **AI-generated recap image.** Agreed design: one image per recap, always
  on. A second Claude call turns the recap's "Memorable Moments" section
  into an image-generation prompt; sent to an external image API (OpenAI's
  was the leading candidate, not yet integrated); result attached to the
  recap post the same way the transcript file is.

## AI COMPLETION CHECKLIST

Before presenting any proposed code change, explicitly confirm:
- [ ] Does not introduce a blocking call inside `async` code (Rule 1)
- [ ] Does not bypass or weaken the auto-reconnect watchdog (Rule 2)
- [ ] Any recording start/stop goes through `_stop_recording_and_wait()`
      (Rule 3)
- [ ] No new timeout shorter than 90 minutes around transcription (Rule 4)
- [ ] Any new audio-writing code cleans up its files (Rule 5)
- [ ] Any schema change uses the guarded `ALTER TABLE` pattern (Rule 6)
- [ ] `python3 -m py_compile` run on every changed file
- [ ] Uses Slash Commands only, not prefix commands
- [ ] Does not add anything listed under ANTI-PATTERNS
- [ ] Deployment instructions followed, including the final GitHub push
