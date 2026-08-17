# CLAUDE.md

Context for any AI assistant (or human) working on this repository. Read
this before making changes — several things here aren't discoverable from
the code alone, and getting them wrong has cost real debugging time before.

## What this is

A Discord bot that joins a D&D group's voice channel, records each speaker
separately, transcribes locally with Whisper, and posts an AI-written recap
to a text channel. Runs 24/7 on a free-tier Oracle Cloud server for one
specific Discord server (a friend group), not a general-purpose public bot.

## Tech stack

- Python 3.10+, `py-cord` (Discord library), `faster-whisper` (local
  transcription), Anthropic API (Claude, for recap summarization), SQLite
  via `aiosqlite`
- Hosted on Oracle Cloud "Always Free" tier: Ubuntu 22.04, ARM (Ampere),
  **1 OCPU / 6GB RAM** — deliberately sized this small to stay permanently
  free (see incident log below — it was accidentally provisioned 4x too
  large at one point). Do not casually recommend resizing up without
  flagging the cost tradeoff explicitly to the user first.
- Runs as a `systemd` service (`dnd-bot.service`), not a manually-run
  process.

## Critical: the py-cord dependency is NOT the official release

`requirements.txt` pins py-cord to a specific **unreleased development
branch** (`fix/voice-rec-2`), not a normal PyPI version. This is
intentional and required. Discord made end-to-end voice encryption (DAVE)
mandatory in March 2026, and the official py-cord release still doesn't
fully support *recording* audio under it (sending/playing audio works fine
in the official release — receiving/recording doesn't). Do not "fix" an
install by changing this to a plain version pin — that will silently break
voice recording again. Before assuming this is still necessary, check
https://github.com/Pycord-Development/pycord/issues/3139 for whether it's
been resolved upstream and a real release now includes the fix.

Because of this, expect rough edges in voice connection teardown and
occasional library immaturity — see the incident log below for specifics
already worked around.

## Architecture at a glance

- `bot.py` — entry point, loads cogs, configures logging (Python's
  `logging` module, set up once here; every other file just does
  `logging.getLogger("name")` and inherits the format)
- `config.py` — all environment variable reading lives here; nowhere else
  should call `os.environ` directly
- `database.py` — all SQL lives here; other files call plain async
  functions, never write raw SQL themselves. SQLite via `aiosqlite`.
- `audio/transcriber.py` — wraps faster-whisper (lazy-loads the model once)
- `summarizer.py` — wraps the Anthropic API call, contains the recap system
  prompt
- `cogs/session.py` — the most complex file: voice connection lifecycle,
  recording, the "take" system (a session = one or more takes, split by
  pause/resume or an unexpected disconnect), auto-reconnect logic. Read
  this file's docstring and the incident log below before touching it.
- `cogs/characters.py` — player/character/DM linking commands
- `cogs/notes.py` — browsing/searching past recaps

## Hard-won lessons (don't relearn these the expensive way)

1. **Never call a blocking synchronous function directly inside `async`
   code.** `faster-whisper` transcription and the Anthropic API call are
   both synchronous; calling them directly froze the entire bot's event
   loop (including the heartbeat that keeps Discord's connection alive) for
   as long as they ran, which caused real, hard-to-diagnose disconnect
   bugs (see incident log). Both are wrapped in
   `loop.run_in_executor(None, fn, args)` in `cogs/session.py` — keep doing
   this for any future slow/blocking call.
2. **This server has exactly 1 CPU core.** Transcription runs sequentially,
   one speaker at a time. A real session with 5 speakers can take 40+
   minutes just to transcribe after `/session end`. This is a known,
   accepted tradeoff for staying in the free tier, not a bug — but keep it
   in mind before adding anything else CPU-heavy, and don't set aggressive
   timeouts around transcription (see `TRANSCRIPTION_TIMEOUT_SECONDS`).
3. **A plausible-sounding external explanation isn't automatically
   correct.** "Discord's servers must just be flaky" was the wrong
   conclusion at least twice during this project's history before the real,
   in-our-control cause was found each time (see incident log). Prefer
   reproducing and isolating over accepting the first explanation that fits.
4. **Log liberally.** Every command and every major step in
   `cogs/session.py` logs on entry/completion. This directly enabled
   diagnosing several real bugs after the fact from `journalctl` output
   alone, including recovering ~44 minutes of already-transcribed session
   data from a stuck process without losing it. Keep this pattern for any
   new command or long-running operation.
5. **Voice connections can drop unexpectedly, unrelated to anything in this
   codebase** — a known py-cord/Discord issue where "listen-only" bots
   (never transmitting their own audio) occasionally get silently
   disconnected after tens of minutes. The `on_voice_state_update` listener
   in `cogs/session.py` auto-detects this and reconnects + resumes
   recording as a new take, capped at 3 attempts per session (see
   `MAX_AUTO_RECONNECT_ATTEMPTS`). Don't remove this without a replacement
   safety net.
6. **`/session end` and `/session pause` must be idempotent/re-runnable.**
   Both call `_stop_recording_and_wait()`, which tolerates being called
   when recording's already stopped (via try/except around
   `stop_recording()`) rather than crashing. This matters because manual
   recovery from a stuck session sometimes means re-running these commands.

## Incident log (condensed history — real bugs found and fixed)

**Getting voice recording working at all.** The core blocker for most of
early development: Discord's DAVE encryption rollout broke voice *receiving*
in every mainstream library. Router/firewall troubleshooting (both on a
home network and on this Oracle server) was a red herring — the connection
attempt reaching Discord's servers looked identical whether or not it would
ever succeed, because the real failure was a protocol-level handshake gap
in the library, not blocked traffic. Confirmed via a matching, already-known
GitHub issue and fixed by pinning to the in-progress fix branch (see above).

**Stuck-in-voice-channel bug, and the wrong initial theory.** After
`/session end` completed and posted a recap, the bot would sometimes stay
in the voice channel for minutes, or indefinitely. First theory (accepted
too quickly): "Discord's servers are just slow to tear down DAVE-encrypted
sessions." Real cause, found only after being pushed to look harder:
`faster-whisper` and the Anthropic API call were both blocking the entire
asyncio event loop (see lesson #1 above), starving the Discord gateway
heartbeat and causing it to silently need to recover. Fixed via
`run_in_executor`. Confirmed fixed by the same operation completing in
~30 seconds afterward, logged end-to-end.

**Storage risk.** Raw per-speaker WAV recordings were never deleted after
transcription, which would have grown disk usage unboundedly across
sessions (several GB per real session). Fixed: `_on_take_finished` now
deletes each WAV file immediately after transcribing it, and cleans up the
empty take directory.

**Oracle instance accidentally 4x oversized.** At one point the running
instance was actually provisioned at 4 OCPU / 24GB rather than the intended
1 OCPU / 6GB — a real risk of exceeding the free monthly allowance if left
running 24/7 (confirmed via the math: 1,500 free OCPU-hours/month vs. ~2,920
that a 4-OCPU instance running continuously would use). Caught during a
deliberate cost review and resized down before it became a real charge.
Always double-check actual provisioned size on the instance's plain detail
page, not the shape-selection wizard's slider range.

**`/session end` non-idempotent + a genuinely stuck session.** During a
real ~1-hour test with 5 speakers, the bot unexpectedly left voice mid-session
(see next item for why) and the subsequent processing got stuck for 19+
hours with zero errors logged — transcription for all 5 speakers had
actually completed successfully, but the code never proceeded past that
point to build/post the recap, for reasons never fully confirmed. A second
`/session end` attempt crashed (`RecordingException: You are not
recording`) instead of helping. Recovered by manually flipping the
session's DB status flag (`UPDATE sessions SET status='paused'`) to route
around the crashing code path, which let `/session end` pick up the
already-completed in-memory transcription and post the recap successfully
— confirming the data survives in memory even when a session appears
stuck, as long as the bot process itself isn't restarted. This led directly
to lesson #6 above and the `TRANSCRIPTION_TIMEOUT_SECONDS` (90 min) bound,
so a future stuck session surfaces a clear error instead of hanging
silently for hours.

**Root cause of that mid-session disconnect: a known upstream bug.**
Investigation found a pre-existing, previously-reported py-cord issue
(predating DAVE entirely) affecting listen-only voice bots (bots that
receive audio but never transmit their own) — they can get silently
disconnected by Discord after an extended period, with a clean-looking
close code that doesn't indicate an actual error. This is not something
fixable from this codebase's side. Mitigated (not "fixed," since the root
cause is upstream) via the auto-reconnect logic in lesson #5 above, which
detects the drop via `on_voice_state_update` and transparently rejoins +
resumes as a new take.

## Deployment workflow (this matters — it's not `git push`)

The production server does **not** pull from GitHub directly. Changes are
deployed by:
1. Editing files
2. `python3 -m py_compile <file>` to catch syntax errors before deploying
   anything
3. Getting the file(s) onto the server — either a full project zip via
   `scp`, or for single-file changes, a `cat > file << 'EOF' ... EOF`
   heredoc pasted directly into the SSH session (this avoids `nano`
   whitespace/tab corruption issues that have bitten this project before —
   avoid recommending manual `nano` edits for anything beyond a one-line
   change)
4. `sudo systemctl stop dnd-bot` before overwriting files, then
   `sudo systemctl daemon-reload && sudo systemctl restart dnd-bot`
5. Verify with `sudo journalctl -u dnd-bot -f`

GitHub is used purely as a showcase/backup of the code, updated
periodically, not as the deployment mechanism.

## Before considering any change "done"

- [ ] `python3 -m py_compile` on every changed file
- [ ] If it touches `cogs/session.py`, consider whether it affects: the
      idempotency of `/session end` / `/session pause`, the auto-reconnect
      logic, or the event-loop-blocking rule (lesson #1)
- [ ] If it touches the database schema, use an `ALTER TABLE` guarded by
      `try/except aiosqlite.OperationalError` (see `init_db()`) so existing
      production data isn't broken — this bot has real, irreplaceable
      session history in it
- [ ] Consider whether a real, in-progress session could be active on the
      production server when this gets deployed — a restart mid-session
      currently loses any not-yet-finalized take's audio

## Cost model (for context, not a live dashboard — verify current numbers if it matters)

- Oracle compute: fixed-size, always-on, flat allowance-based — not
  metered per-request, so command volume/abuse can't directly increase this
  bill. Only real risk is provisioning too large a shape (see incident log).
- Anthropic API: pay-as-you-go, no auto-reload enabled on the account, so
  it fails gracefully rather than overspending. Roughly $0.10-$0.30 per
  real 3-hour session's summarization call.
- Rate limiting / queueing infrastructure was deliberately not built —
  traffic is a handful of trusted users in one private server, already
  naturally throttled by "only one session active at a time" plus
  transcription being the real bottleneck. Revisit if/when an
  image-generation feature ships, since per-call cost is higher for images.

## Things this project deliberately does NOT have (don't add without discussion)

- No queueing/rate-limiting infrastructure (see cost model above)
- No multi-server (multi-guild-at-scale) support — SQLite and the current
  design assume one Discord server
- No CI/CD pipeline — deployment is manual/SSH-based by design, given the
  small scale
- No multi-campaign support yet (designed but not built — if implementing,
  the agreed design is *explicit* `campaign` parameters on setup/lookup
  commands like `/character link` and `/notes search`, NOT binding
  campaigns to specific channels; `/session start` takes the campaign
  explicitly, but `/session pause|resume|end|force-leave` resolve the
  active session by which voice channel the caller is currently in, so
  two campaigns can record concurrently without extra parameters on those)
- No AI-generated recap image yet (designed but not built — agreed plan:
  one image per recap, always on, via a second Claude call to turn the
  "Memorable Moments" section into an image-generation prompt, sent to an
  external image API — OpenAI's was the leading candidate — with the
  result attached to the recap post the same way the transcript file is)
