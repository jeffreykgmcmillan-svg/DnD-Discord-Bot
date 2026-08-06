# D&D Bot Debugging Journey, Part 2: From "It Works" to "It Works Reliably"

A continuation of [`debugging-journey.md`](debugging-journey.md), picking up
right after the first successful end-to-end test. Getting a demo to work
once turned out to be a different problem than making it work *every time*.

## New feature: distinguishing the Dungeon Master

The original design treated every linked player identically — one Discord
user, one character name. That breaks down for the DM, who isn't playing a
single character: they narrate the environment, adjudicate rules, and voice
every NPC in the game.

Added:
- An `is_dm` flag on the character record (with a safe `ALTER TABLE`
  migration so it wouldn't break the database from earlier testing)
- `/character set-dm @user ["Title"]`, which also clears DM status from
  anyone previously marked (only one DM at a time)
- Updated transcript labeling so the DM's lines are tagged distinctly
  (`"Marcus (Dungeon Master -- narrating/voicing NPCs)"` instead of being
  treated like a single character's dialogue)
- Updated the Claude summarization prompt to explicitly explain this
  distinction, so it infers which specific NPC is speaking from context
  rather than lumping all DM dialogue together generically

## Bug: slash commands not appearing after updates

After adding `/character set-dm`, it didn't show up in Discord at all when
typing the command.

- **Cause:** the bot was relying on Discord's *global* command sync, which
  can take up to an hour to propagate new or changed commands — the bot
  actually already had older commands cached from before, which is why this
  hadn't been noticed until adding something brand new.
- **Fix:** added an optional `DISCORD_GUILD_ID` setting that, when present,
  tells py-cord to sync commands to that specific server instead
  (`debug_guilds` parameter) — this syncs near-instantly and is the
  recommended approach for a bot that only runs in one server anyway.

## Bug: bot getting stuck in voice after `/session end`

This was the big one — the bot would successfully record, transcribe, and
post a full recap, but then just sit in the voice channel indefinitely
afterward, sometimes for 3+ minutes, sometimes not resolving on its own at
all.

**First attempt (partial fix):** added a `/session force-leave` recovery
command that talks directly to Discord's authoritative voice-connection
reference rather than the bot's own possibly-stale internal tracking. This
worked when manually triggered, but didn't explain *why* the automatic
disconnect kept failing in the first place.

**First theory (wrong, but reasonable at the time):** suspected Discord's
own servers were just slow/inconsistent about tearing down DAVE-encrypted
voice sessions, since the exact same disconnect code sometimes worked
instantly and sometimes didn't, with zero errors logged either way.

**Getting pushed to look harder:** that "must be Discord's fault" explanation
was accepted too quickly. Re-examining the code order in `/session end`
revealed the real issue: **`faster-whisper` transcription and the Anthropic
API call were both synchronous, blocking calls, called directly inside
`async` functions with no `await`.**

In an asyncio-based bot, everything -- including the invisible background
heartbeat that keeps the connection to Discord's gateway alive -- runs on a
single event loop. A long-running synchronous call (like transcribing a
50-second audio clip, which took ~25 seconds of pure CPU time) blocks that
entire loop, including the heartbeat. Discord's gateway can then consider
the connection unresponsive and needs to silently recover in the
background -- which produces exactly the kind of unpredictable,
duration-dependent hang that was being observed, but caused by the bot going
quiet, not by Discord's servers being flaky.

**Real fix:** moved both blocking calls onto a background thread using
`asyncio.get_event_loop().run_in_executor()`, so the event loop stays free
to keep servicing Discord's gateway the entire time, regardless of how long
transcription or summarization take.

**Result:** confirmed via full logging (see below) -- a subsequent test
completed the entire flow, including a clean disconnect, in about 30 seconds
with zero manual intervention needed.

## Adding comprehensive logging

Once things were working, the logs were nearly silent by design (only
warnings/errors were printed), which made it hard to actually see what was
happening or verify fixes with confidence. Added structured logging
(Python's `logging` module, configured once in `bot.py`) covering:

- Every slash command, logged on invocation with who ran it and key
  parameters
- Every operational milestone in a session: voice connect, recording
  start/stop per take, per-speaker transcription start/finish (with line
  counts), disconnect attempts (success *and* failure, not just failure),
  Claude summarization start/finish, and final recap posting

This immediately paid off -- the very next test's logs showed the entire
`/session end` -> disconnect -> transcription -> summarization ->
recap-posted sequence completing cleanly in under a minute, which was the
concrete confirmation that the event-loop fix had actually solved the root
cause rather than just moved the symptom around.

## Cost/infrastructure review

Before relying on this for real ongoing use, walked through whether any part
of the stack could result in unexpected charges:

- **Anthropic API**: pay-as-you-go, no auto-reload enabled, so it can't
  overshoot a set budget -- worst case it fails gracefully once credit runs
  out. Cost for a 3-hour session's summarization call: roughly $0.10-$0.30.
- **Oracle Cloud compute**: billed by usage-hours, not a simple "must be off
  sometimes" limit -- confirmed the actual free monthly allowance (1,500
  OCPU-hours / 9,000 GB-hours) comfortably covers 24/7 uptime **at the
  correct instance size**.
- **Caught a real risk**: the running instance turned out to actually be
  provisioned at 4 OCPU / 24GB (double the free allowance if run
  continuously) rather than the intended 1 OCPU / 6GB -- likely a default
  from the instance-creation wizard. Resized it down before this became an
  actual bill.
- **Caught and fixed unbounded disk growth**: raw per-speaker WAV recordings
  were never being deleted after transcription, which would have
  accumulated several GB per session indefinitely. Added automatic cleanup
  of both the audio files and their now-empty directories immediately after
  successful transcription.

## Takeaways from this phase

- **A plausible-sounding explanation isn't the same as a confirmed one.**
  "Discord's servers are just flaky" fit the observed symptoms well enough
  to feel satisfying, but the actual cause was entirely within this
  codebase's control. Worth being pushed on causes that can't easily be
  disproven.
- **Silence in logs is not the same as confirmation nothing is happening** --
  it can equally mean nothing is being logged at all. Comprehensive logging
  turned "I think that's fixed?" into "here's the exact 30-second timeline
  proving it's fixed."
- **Blocking calls inside async code are an easy trap** -- nothing about
  calling a synchronous function from inside an `async def` raises an error
  or warning; it just silently degrades the whole program's responsiveness
  under load, in ways that look like an unrelated, external problem.
