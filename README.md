# D&D Session Note-Taker (Discord Bot)

Joins your D&D session's voice channel, records each speaker separately,
transcribes locally with Whisper, and posts an AI-written recap (key events,
NPCs, loot, open threads, funny moments) to a channel of your choosing when
you're done. Distinguishes the Dungeon Master from regular players so the
recap correctly reads their lines as narration/NPC voices rather than a
single character's dialogue.

**Status:** actively running in production for a real weekly campaign. See
[`docs/debugging-journey.md`](docs/debugging-journey.md) and
[`docs/debugging-journey-part-2.md`](docs/debugging-journey-part-2.md) for
the full story of getting this working, including a real bug found in
Discord's still-maturing end-to-end voice encryption support.

## How it works

1. `/session start` — bot joins your current voice channel and starts recording, keeping each speaker's audio separate.
2. `/session pause` / `/session resume` — for breaks, without losing the transcript-so-far.
3. `/session end` — bot leaves, transcribes everything locally (faster-whisper), asks Claude to write a clean recap, and posts it + the full transcript to your configured channel.
4. `/character link` — map each Discord user to their PC name, so the recap reads "Aria (Sarah): ..." instead of raw usernames.
5. `/character set-dm` — mark the Dungeon Master separately, so the recap correctly treats their audio as narration/multiple NPCs instead of one character's dialogue.
6. `/notes recent` and `/notes search` — browse or search past recaps later.
7. `/session force-leave` — emergency recovery if the bot ever gets stuck in a voice channel.

## 1. Requirements

- Python 3.10+
- `ffmpeg` installed on the system (`sudo apt install ffmpeg` on Ubuntu/Debian) — needed for voice
- A Discord bot application + token
- An Anthropic API key (for writing the recap — the transcription itself is free/local)

**Important:** as of Aug 2026, this project depends on an **unreleased development
branch** of py-cord (`fix/voice-rec-2`), not the official release — see the
debugging journey docs for why. `requirements.txt` is already pinned to it,
but building this branch from source takes a bit longer than a normal
`pip install`.

## 2. Discord setup

1. Go to https://discord.com/developers/applications → **New Application**.
2. **Bot** tab → Add Bot → copy the token (this goes in `.env` as `DISCORD_TOKEN`).
3. Still on the **Bot** tab, enable these **Privileged Gateway Intents**:
   - Server Members Intent
   - Message Content Intent
4. **OAuth2 → URL Generator**: check `bot` and `applications.commands` scopes. Under Bot Permissions check: `View Channels`, `Send Messages`, `Connect`, `Speak`, `Use Voice Activity`. Use the generated URL to invite the bot to your server.
5. (Recommended) Set `DISCORD_GUILD_ID` in `.env` to your server's ID so slash commands sync **instantly** rather than relying on Discord's global sync, which can take up to an hour to propagate new/changed commands. To find your server ID: enable Developer Mode (User Settings → Advanced), then right-click your server icon → Copy Server ID.

## 3. Local install

```bash
git clone <this project>
cd dnd-notetaker
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env with your DISCORD_TOKEN and ANTHROPIC_API_KEY
python bot.py
```

The first time a recording is transcribed, `faster-whisper` will download the
model weights (a few hundred MB for the "small" model) — this happens once
and is cached.

## 4. Free hosting: Oracle Cloud "Always Free" tier

Oracle's Always Free tier includes an ARM-based VM that never expires and is
genuinely free — this is enough to run the bot plus a local Whisper "small"
model comfortably.

**Sizing matters:** stick to **1 OCPU / 6GB** if you want it running 24/7 with
zero billing risk. Oracle's free allowance is ~1,500 OCPU-hours and 9,000
GB-hours per month; running a 4 OCPU / 24GB instance continuously would
exceed that. The instance creation wizard can default to a larger size than
intended (this happened during initial setup) — double check the "Shape
configuration" on the instance's detail page after creating it.

1. Sign up at https://www.oracle.com/cloud/free/ (a card is required for identity verification but the Always Free resources are not billed).
2. Create a Compute Instance: **Ampere A1** shape, **1 OCPU / 6GB**, Ubuntu 22.04 image. (Free-tier capacity for this shape is often oversubscribed — if you hit "Out of capacity," just retry periodically.)
3. Open port access if you plan to expose anything (not required for this bot — it only makes outbound connections).
4. SSH in, install Python 3.10+, `ffmpeg`, and `git`:
   ```bash
   sudo apt update && sudo apt install -y python3-pip python3-venv ffmpeg git
   ```
5. Clone your project there and follow the "Local install" steps above.
6. Keep it running permanently with `systemd`:

   Create `/etc/systemd/system/dnd-bot.service`:
   ```ini
   [Unit]
   Description=D&D Discord Note-Taker
   After=network.target

   [Service]
   WorkingDirectory=/home/ubuntu/dnd-notetaker
   ExecStart=/home/ubuntu/dnd-notetaker/venv/bin/python bot.py
   Environment=PYTHONUNBUFFERED=1
   Restart=always
   RestartSec=5
   User=ubuntu

   [Install]
   WantedBy=multi-user.target
   ```
   Then:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now dnd-bot
   sudo journalctl -u dnd-bot -f   # view logs
   ```

## 5. Command reference

| Command | Purpose |
|---|---|
| `/session start` | Join your voice channel, start recording |
| `/session pause` | Pause recording (keeps transcript so far) |
| `/session resume` | Resume after a pause |
| `/session end` | Stop, transcribe, summarize, post recap + transcript |
| `/session force-leave` | Emergency: force the bot out of voice if it ever gets stuck |
| `/character link @user "Name"` | Map a Discord user to their character |
| `/character set-dm @user ["Title"]` | Mark a user as the Dungeon Master (narration/NPCs, not a single character) |
| `/character list` | Show current mappings, DM marked with 🎲 |
| `/summary-channel-set #channel` | Set where recaps get posted (requires Manage Server) |
| `/notes recent [count]` | Show recent recaps |
| `/notes search <term>` | Search past recaps by keyword |

## 6. Logging

Every command and major operational step (voice connect, per-speaker
transcription, Claude summarization, disconnect) logs to stdout with a
timestamp and module tag, e.g.:
```
00:14:35 [session] /session end invoked by shardik in guild 40*****
00:14:35 [session] Transcribing audio for Barnaby Thundermuzzle (shardik)...
00:15:06 [session] Summary received (447 chars)
```
When running as a `systemd` service, view this with:
```bash
sudo journalctl -u dnd-bot -f
```

## Known limitations & good next steps

- **Consent/etiquette**: the bot announces itself when it joins, but you should make sure everyone at the table is comfortable being recorded.
- **Long sessions & memory**: audio for each "take" is buffered in memory until pause/end. For very long sessions (4+ hours), consider adding an auto-pause/resume timer every ~30–45 minutes to flush audio to disk periodically.
- **Crosstalk**: Whisper transcribes each speaker's own audio channel independently, so simultaneous talking won't get garbled together — but background noise bleeding into a mic (e.g. a laptop speaker picking up another player) can add noise to that user's transcript.
- **Model size**: "small" balances speed/accuracy on CPU. If your Oracle box handles it fine, try `medium` in `.env` for better accuracy at the cost of slower transcription.
- **Scaling to many servers**: this scaffold uses SQLite, which is fine for one or a handful of Discord servers. For many concurrent campaigns, consider Postgres.
- **Cost**: transcription is free/local; only the final Claude summarization call costs anything, and it's one call per session (usually cents).
- **Bleeding-edge dependency**: relies on an unreleased py-cord branch (see requirements.txt comment) since the official release doesn't yet support voice recording under Discord's DAVE encryption. Revisit this pin once [pycord#3139](https://github.com/Pycord-Development/pycord/issues/3139) is resolved and shipped in a real release.
- ~~Disconnecting from voice at session end was unreliable~~ — **resolved**: root cause was that transcription and Claude summarization ran as blocking synchronous calls directly on the bot's event loop, starving Discord's gateway heartbeat. Both now run via `loop.run_in_executor()`. See the debugging journey docs.

## Resolved issues (see debugging journey docs for full detail)

- Windows Python version mismatches, missing dependencies, and library version conflicts during initial setup
- Home network / cloud firewall red herrings while chasing a voice connection failure that was actually Discord's DAVE encryption rollout
- Voice recording support entirely missing from py-cord's official release — fixed by pinning to an in-progress development branch
- Bot getting permanently stuck in voice channels after `/session end` — fixed by identifying and removing event-loop-blocking synchronous calls
- Unbounded disk growth from raw audio recordings — fixed by auto-deleting per-speaker WAV files after successful transcription
- Oracle Cloud instance accidentally provisioned at 4x the intended size (real billing risk) — caught and resized down before it became a cost issue

## Suggested feature ideas not yet built

- Reaction-based "flag this moment" during the session for the bot to call out specially in the recap
- Auto-growing NPC/location glossary/wiki extracted from recaps over time
- Export recap to PDF/Notion/Google Docs
- Per-player DM delivery of secret/private information revealed mid-session
