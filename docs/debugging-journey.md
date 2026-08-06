# D&D Bot Setup Journey: What Happened and What We Learned

A record of the path from "brand new to all of this" to a working voice-recording
Discord bot — including every wrong turn, so future-you (or anyone else setting
this up) has the map.

## The short version

The bot's core feature — joining voice chat and recording each speaker
separately — was blocked for weeks not by anything in your setup, but by a
genuinely new industry-wide problem: Discord rolled out mandatory end-to-end
voice encryption ("DAVE") in March 2026, and the library this bot is built on
(py-cord) hadn't finished supporting it yet. Every "network" symptom we chased
was actually a downstream effect of that one root cause. The fix was installing
an in-progress development version of the library that added the missing
support, plus a couple of small code adjustments to match its updated behavior.

## Timeline of issues, in the order we hit them

### 1. Windows: wrong Python version (`av` build failure)
- **Symptom:** `pip install -r requirements.txt` failed trying to compile a
  package called `av`, with an error about needing "Microsoft Visual C++ 14.0."
- **Cause:** Python 3.14 was too new — several dependencies didn't have
  ready-made installers for it yet, forcing pip to try compiling from source.
- **Fix:** Installed Python 3.12 alongside 3.14, recreated the virtual
  environment specifically with `py -3.12 -m venv venv`.

### 2. Missing `requests` library
- **Symptom:** `ModuleNotFoundError: No module named 'requests'` when starting
  the bot, coming from inside the `faster-whisper` library.
- **Cause:** An incomplete `requirements.txt` — this dependency was missing
  from the original file.
- **Fix:** `pip install requests`, then added it to `requirements.txt`
  permanently.

### 3. `anthropic` / `httpx` version mismatch
- **Symptom:** `TypeError: Client.__init__() got an unexpected keyword
  argument 'proxies'` when the bot tried to load the summarization module.
- **Cause:** An older pinned version of the `anthropic` library was
  incompatible with a newer version of one of its own dependencies (`httpx`)
  that got installed alongside it.
- **Fix:** `pip install --upgrade anthropic`, then updated the pinned version
  in `requirements.txt`.

### 4. First voice connection failures ("couldn't establish audio connection")
- **Symptom:** The bot would join the voice channel visually, then leave a
  few seconds later with a message about the connection never finishing.
- **What we suspected (in order):** Windows Firewall → ruled out (same
  failure with it fully disabled) → a VPN → ruled out (no VPN on the actual
  PC) → the home router (a Rogers "Gen 2" gateway) blocking/mishandling voice
  traffic — this looked very promising, since that model is well-documented
  as locking down exactly the kind of setting (SIP ALG) that affects this
  kind of traffic, with no user-facing way to change it.
- **What we did:** Moved the whole bot to a cloud server (Oracle Cloud Free
  Tier), reasoning that a data center network wouldn't have consumer-router
  quirks.
- **Result:** The exact same failure happened again, on a completely
  different network — which turned out to be the key clue that this was
  never actually a network/firewall problem at all.

### 5. Oracle Cloud setup detours (not bugs, just process)
- Hit "Out of capacity" errors creating the free-tier server multiple times
  (Oracle's free ARM instances are popular and often oversubscribed) —
  resolved by simply retrying.
- Recreated the instance more than once while sorting out SSH key mismatches
  and confirming the public IP was attached.
- Accidentally ended up on Ubuntu 20.04 instead of the intended 22.04 at one
  point (an older Python version, 3.8, that couldn't install some packages
  either) — resolved by recreating the instance and explicitly selecting the
  22.04 image.

### 6. Ruling out every remaining network-side suspect
- Checked the server's own Linux firewall (`iptables`) — fully open for
  outbound-initiated traffic, not the cause.
- Checked Oracle's cloud-level firewall ("Security List") — egress fully open
  to all protocols/ports, not the cause.
- Tried a Discord **Stage Channel** instead of a normal voice channel (Stage
  Channels are explicitly exempt from DAVE encryption per Discord's own
  documentation) — same failure occurred, which was the final piece of
  evidence that this wasn't network- or channel-type-related at all.

### 7. Finding the actual root cause: Discord's DAVE encryption
- A web search on the exact failure pattern turned up the real explanation:
  **Discord made end-to-end voice encryption mandatory starting March 2,
  2026**, and py-cord's officially released version (2.6.1, what we started
  with) had not yet implemented support for it. Every symptom above — the
  connection reaching Discord's servers but never completing, regardless of
  network — was this same underlying cause the whole time.
- This was confirmed by the fact that a paid third-party bot (Crit Scribbler)
  was doing the same thing successfully — meaning it was solvable, just not
  yet solved in the library we were using.

### 8. Fixing it: upgrading to an in-progress development version
- Upgrading to the latest *officially released* py-cord (2.8.1) got the
  connection itself working, but recording specifically still failed with a
  library-level warning: "Voice reception is currently broken due to
  Discord's DAVE... " — confirming recording support specifically was still
  incomplete even in the newest stable release.
- Found an active, not-yet-merged development branch
  (`fix/voice-rec-2`) where another user had specifically tested and
  confirmed voice recording working with DAVE encryption.
- Installed directly from that branch:
  ```
  pip install "py-cord[voice] @ git+https://github.com/Pycord-Development/pycord.git@fix/voice-rec-2"
  ```
  This got the bot successfully joining, recording, and processing audio for
  the first time.

### 9. Two small code bugs surfaced by the new library version
- **Bug:** Saving character data crashed with `sqlite3.InterfaceError: Error
  binding parameter 1 - probably unsupported type`.
  - **Cause:** The new development branch returns full Discord `Member`
    objects as dictionary keys for each recorded speaker, instead of plain
    numeric IDs like the previous version did.
  - **Fix:** Updated the code to read `member.id` and `member.display_name`
    directly from those `Member` objects instead of expecting a raw ID.
- After that fix: **first fully successful end-to-end run** — bot joined,
  recorded, transcribed, summarized, and posted a complete recap with a
  Recap section, Key Events, NPCs & Locations, Loot & Mechanics, Open
  Threads, and Memorable Moments, plus an attached transcript file.

## Key lessons worth remembering

- **A symptom that follows you across completely different environments
  (different PC, different network, different continent-scale cloud
  provider) is a strong signal the cause isn't environmental at all** — it's
  worth searching for the exact error text early once you've ruled out one
  or two "obvious" local causes, rather than continuing to chase
  configuration settings.
- **Bleeding-edge software (a library installed from an active development
  branch rather than an official release) can fix a real problem, but comes
  with real trade-offs**: it may have its own undiscovered bugs, and a
  routine `pip install --upgrade` later could silently revert you back to
  the broken official version. That's why `requirements.txt` now pins the
  exact working source with a comment explaining why.
- **Version mismatches between related libraries** (Python itself, or
  companion packages like `anthropic`/`httpx`) caused several of the
  earlier errors — when something fails immediately after a fresh install
  with no code changes on your end, a version mismatch is often the first
  thing worth suspecting.
