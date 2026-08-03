# D&D Session Note-Taker (Discord Bot)

Joins your D&D session's voice channel, records each speaker separately,
transcribes locally with Whisper, and posts an AI-written recap (key events,
NPCs, loot, open threads, funny moments) to a channel of your choosing when
you're done.

## How it works

1. `/session start` — bot joins your current voice channel and starts recording, keeping each speaker's audio separate.
2. `/session pause` / `/session resume` — for breaks, without losing the transcript-so-far.
3. `/session end` — bot leaves, transcribes everything locally (faster-whisper), asks Claude to write a clean recap, and posts it + the full transcript to your configured channel.
4. `/character link` — map each Discord user to their PC name, so the recap reads "Aria (Sarah): ..." instead of raw usernames.
5. `/notes recent` and `/notes search` — browse or search past recaps later.

## 1. Requirements

- Python 3.10+
- `ffmpeg` installed on the system (`sudo apt install ffmpeg` on Ubuntu/Debian) — needed for voice
- A Discord bot application + token
- An Anthropic API key (for writing the recap — the transcription itself is free/local)

## 2. Discord setup

1. Go to https://discord.com/developers/applications → **New Application**.
2. **Bot** tab → Add Bot → copy the token (this goes in `.env` as `DISCORD_TOKEN`).
3. Still on the **Bot** tab, enable these **Privileged Gateway Intents**:
   - Server Members Intent
   - Message Content Intent
4. **OAuth2 → URL Generator**: check `bot` and `applications.commands` scopes. Under Bot Permissions check: `View Channels`, `Send Messages`, `Connect`, `Speak`, `Use Voice Activity`. Use the generated URL to invite the bot to your server.

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

Oracle's Always Free tier includes an ARM-based VM (4 OCPUs, 24GB RAM) that
never expires and is genuinely free — this is enough to run the bot plus a
local Whisper "small" model comfortably.

1. Sign up at https://www.oracle.com/cloud/free/ (a card is required for identity verification but the Always Free resources are not billed).
2. Create a Compute Instance: **Ampere A1** shape, choose your free allocation (e.g. 4 OCPU / 24GB RAM), Ubuntu 22.04 image.
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
   Restart=always
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
| `/character link @user "Name"` | Map a Discord user to their character |
| `/character list` | Show current mappings |
| `/summary-channel-set #channel` | Set where recaps get posted (requires Manage Server) |
| `/notes recent [count]` | Show recent recaps |
| `/notes search <term>` | Search past recaps by keyword |

## Known limitations & good next steps

- **Consent/etiquette**: the bot announces itself when it joins, but you should make sure everyone at the table is comfortable being recorded. Consider adding a `/session status` command that shows who's currently being recorded.
- **Long sessions & memory**: audio for each "take" is buffered in memory until pause/end. For very long sessions (4+ hours), consider adding an auto-pause/resume timer every ~30–45 minutes to flush audio to disk periodically.
- **Crosstalk**: Whisper transcribes each speaker's own audio channel independently, so simultaneous talking won't get garbled together — but background noise bleeding into a mic (e.g. a laptop speaker picking up another player) can add noise to that user's transcript.
- **Model size**: "small" balances speed/accuracy on CPU. If your Oracle box handles it fine, try `medium` in `.env` for better accuracy at the cost of slower transcription.
- **Scaling to many servers**: this scaffold uses SQLite, which is fine for one or a handful of Discord servers. For many concurrent campaigns, consider Postgres.
- **Cost**: transcription is free/local; only the final Claude summarization call costs anything, and it's one call per session (usually cents).

## Suggested feature ideas not yet built

- Reaction-based "flag this moment" during the session for the bot to call out specially in the recap
- Auto-growing NPC/location glossary/wiki extracted from recaps over time
- Export recap to PDF/Notion/Google Docs
- Per-player DM delivery of secret/private information revealed mid-session

# DnD-Discord-Bot
Homebrew discord companion bot for homebrew D&amp;D sessions