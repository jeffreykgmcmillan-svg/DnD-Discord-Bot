"""
Recording model: a "session" can be made of multiple "takes" (start -> pause,
resume -> pause, ..., -> end). Each take is recorded to its own set of per-user
WAV files via py-cord's sink API, transcribed independently, then all takes'
transcript lines are merged in chronological order (with a running time offset)
when the session ends.
"""
import asyncio
import datetime
import os
import shutil

import discord
from discord.ext import commands

import database as db
from config import RECORDINGS_DIR
from audio.transcriber import transcribe_speaker_file, TranscriptLine
from summarizer import summarize_session


class GuildSessionState:
    def __init__(self, session_id: int, voice_client: discord.VoiceClient):
        self.session_id = session_id
        self.voice_client = voice_client
        self.take_index = 0
        self.elapsed_before_current_take = 0.0  # seconds, from prior takes
        self.take_started_at: datetime.datetime | None = None
        self.all_lines: list[TranscriptLine] = []
        self.take_finished_event = asyncio.Event()
        self.session_start = datetime.datetime.utcnow()


class SessionCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.states: dict[int, GuildSessionState] = {}

    session = discord.SlashCommandGroup("session", "Manage D&D session recording")

    def _take_dir(self, guild_id: int, session_id: int, take_index: int) -> str:
        path = os.path.join(RECORDINGS_DIR, str(guild_id), str(session_id), f"take_{take_index}")
        os.makedirs(path, exist_ok=True)
        return path

    async def _start_take(self, ctx: discord.ApplicationContext, state: GuildSessionState):
        sink = discord.sinks.WaveSink()
        state.take_finished_event.clear()
        state.take_started_at = datetime.datetime.utcnow()
        state.voice_client.start_recording(sink, self._on_take_finished, ctx.guild.id, state)

    async def _on_take_finished(self, sink: discord.sinks.WaveSink, guild_id: int, state: GuildSessionState):
        take_dir = self._take_dir(guild_id, state.session_id, state.take_index)
        take_duration = (datetime.datetime.utcnow() - state.take_started_at).total_seconds()

        for user_id, audio in sink.audio_data.items():
            character_name = await db.get_character_name(guild_id, user_id)
            member = self.bot.get_user(user_id)
            discord_name = member.display_name if member else str(user_id)
            speaker_label = f"{character_name} ({discord_name})" if character_name else discord_name

            wav_path = os.path.join(take_dir, f"{user_id}.wav")
            with open(wav_path, "wb") as f:
                f.write(audio.file.read())

            lines = transcribe_speaker_file(wav_path, speaker_label)
            for line in lines:
                line.start += state.elapsed_before_current_take
                state.all_lines.append(line)

        state.elapsed_before_current_take += take_duration
        state.take_index += 1
        state.take_finished_event.set()

    @session.command(name="start", description="Join your voice channel and start taking notes")
    async def start(self, ctx: discord.ApplicationContext):
        await ctx.defer()

        if ctx.author.voice is None or ctx.author.voice.channel is None:
            await ctx.respond("You need to be in a voice channel for me to join.")
            return

        existing = await db.get_active_session(ctx.guild.id)
        if existing:
            if ctx.guild.id in self.states:
                await ctx.respond("A session is already active. Use `/session end` to wrap it up first.")
                return
            else:
                # No in-memory state for this guild means the bot restarted
                # (e.g. crashed) since this session was started -- the old
                # voice connection is long gone, so it's safe to clear the
                # stale record and start fresh.
                await db.update_session_status(existing["id"], "ended")

        voice_channel = ctx.author.voice.channel
        vc = await voice_channel.connect()

        for _ in range(10):
            if vc.is_connected():
                break
            await asyncio.sleep(0.5)
        else:
            await vc.disconnect(force=True)
            await ctx.respond(
                "I joined the voice channel but couldn't establish the audio connection "
                "after several seconds. This usually means voice traffic (UDP) is being "
                "blocked by a firewall or router on the machine running the bot -- try "
                "temporarily disabling any firewall/VPN and running `/session start` again "
                "to confirm that's the cause."
            )
            return

        started_at = datetime.datetime.utcnow().isoformat()
        session_id = await db.create_session(ctx.guild.id, voice_channel.id, started_at)
        state = GuildSessionState(session_id, vc)
        self.states[ctx.guild.id] = state

        await self._start_take(ctx, state)

        await ctx.respond(
            f"🎙️ Joined **{voice_channel.name}** and started recording notes.\n"
            f"Heads up to the table: this session is being recorded for note-taking. "
            f"Use `/session pause`, `/session resume`, or `/session end` to manage it."
        )

    @session.command(name="pause", description="Pause recording without ending the session")
    async def pause(self, ctx: discord.ApplicationContext):
        state = self.states.get(ctx.guild.id)
        if not state:
            await ctx.respond("There's no active session in this server.")
            return
        await ctx.defer()
        state.voice_client.stop_recording()
        await state.take_finished_event.wait()
        await db.update_session_status(state.session_id, "paused")
        await ctx.respond("⏸️ Recording paused. Use `/session resume` when you're back.")

    @session.command(name="resume", description="Resume a paused session")
    async def resume(self, ctx: discord.ApplicationContext):
        state = self.states.get(ctx.guild.id)
        if not state:
            await ctx.respond("There's no paused session in this server. Use `/session start` instead.")
            return
        await ctx.defer()
        await self._start_take(ctx, state)
        await db.update_session_status(state.session_id, "active")
        await ctx.respond("▶️ Recording resumed.")

    @session.command(name="end", description="Stop recording, generate a summary, and post it")
    async def end(self, ctx: discord.ApplicationContext):
        state = self.states.get(ctx.guild.id)
        if not state:
            await ctx.respond("There's no active session in this server.")
            return

        await ctx.respond("🧠 Wrapping up... transcribing and writing the recap. This can take a few minutes.")

        current_status = (await db.get_active_session(ctx.guild.id) or {}).get("status")
        if current_status == "active":
            state.voice_client.stop_recording()
            await state.take_finished_event.wait()

        await state.voice_client.disconnect()

        characters = await db.get_characters(ctx.guild.id)
        roster = "\n".join(f"{c['discord_name']} plays {c['character_name']}" for c in characters) or "No character mappings set."

        state.all_lines.sort(key=lambda l: l.start)
        full_transcript = "\n".join(
            f"[{self._fmt_ts(l.start)}] {l.speaker_label}: {l.text}" for l in state.all_lines
        )

        session_dir = os.path.join(RECORDINGS_DIR, str(ctx.guild.id), str(state.session_id))
        transcript_path = os.path.join(session_dir, "transcript.txt")
        with open(transcript_path, "w") as f:
            f.write(full_transcript)

        summary = summarize_session(full_transcript, roster) if full_transcript.strip() else (
            "No speech was detected/transcribed for this session."
        )

        ended_at = datetime.datetime.utcnow().isoformat()
        await db.finish_session(state.session_id, ended_at, transcript_path, summary)

        summary_channel_id = await db.get_summary_channel(ctx.guild.id)
        target_channel = ctx.guild.get_channel(summary_channel_id) if summary_channel_id else ctx.channel

        chunks = self._chunk_text(summary, 1900)
        await target_channel.send(f"## 📜 Session Recap ({datetime.date.today().isoformat()})")
        for chunk in chunks:
            await target_channel.send(chunk)

        # Attach full transcript for the record
        await target_channel.send(file=discord.File(transcript_path, filename=f"session_{state.session_id}_transcript.txt"))

        del self.states[ctx.guild.id]
        await ctx.followup.send(f"✅ Recap posted in {target_channel.mention}.")

    @staticmethod
    def _fmt_ts(seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    @staticmethod
    def _chunk_text(text: str, size: int) -> list[str]:
        return [text[i:i + size] for i in range(0, len(text), size)] or ["(empty)"]


def setup(bot):
    bot.add_cog(SessionCog(bot))
