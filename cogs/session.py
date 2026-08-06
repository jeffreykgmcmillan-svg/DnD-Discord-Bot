"""
Recording model: a "session" can be made of multiple "takes" (start -> pause,
resume -> pause, ..., -> end). Each take is recorded to its own set of per-user
WAV files via py-cord's sink API, transcribed independently, then all takes'
transcript lines are merged in chronological order (with a running time offset)
when the session ends.
"""
import asyncio
import datetime
import logging
import os
import shutil

import discord
from discord.ext import commands

import database as db
from config import RECORDINGS_DIR
from audio.transcriber import transcribe_speaker_file, TranscriptLine
from summarizer import summarize_session

logger = logging.getLogger("session")


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

        logger.info(f"Take {state.take_index} finished for guild {guild_id} -- transcribing {len(sink.audio_data)} speaker(s)")

        for member, audio in sink.audio_data.items():
            user_id = member.id
            info = await db.get_character_info(guild_id, user_id)
            discord_name = member.display_name

            if info and info["is_dm"]:
                speaker_label = f"{discord_name} ({info['character_name']} -- narrating/voicing NPCs)"
            elif info:
                speaker_label = f"{info['character_name']} ({discord_name})"
            else:
                speaker_label = discord_name

            wav_path = os.path.join(take_dir, f"{user_id}.wav")
            with open(wav_path, "wb") as f:
                f.write(audio.file.read())

            logger.info(f"Transcribing audio for {speaker_label}...")
            loop = asyncio.get_event_loop()
            lines = await loop.run_in_executor(None, transcribe_speaker_file, wav_path, speaker_label)
            logger.info(f"Transcribed {len(lines)} line(s) for {speaker_label}")
            for line in lines:
                line.start += state.elapsed_before_current_take
                state.all_lines.append(line)

            # The raw audio is only needed long enough to transcribe it --
            # keeping it afterward would let disk usage grow unbounded across
            # sessions for no benefit, since the text transcript is what
            # actually gets used/kept.
            try:
                os.remove(wav_path)
            except OSError:
                pass  # non-fatal -- worst case a leftover file, not a crash

        state.elapsed_before_current_take += take_duration
        state.take_index += 1
        state.take_finished_event.set()

        # Clean up the now-empty take directory too.
        try:
            shutil.rmtree(take_dir, ignore_errors=True)
        except OSError:
            pass

    @session.command(name="start", description="Join your voice channel and start taking notes")
    async def start(self, ctx: discord.ApplicationContext):
        logger.info(f"/session start invoked by {ctx.author.display_name} in guild {ctx.guild.id}")
        await ctx.defer()

        if ctx.author.voice is None or ctx.author.voice.channel is None:
            logger.info("Rejected: command author not in a voice channel")
            await ctx.respond("You need to be in a voice channel for me to join.")
            return

        existing = await db.get_active_session(ctx.guild.id)
        if existing:
            if ctx.guild.id in self.states:
                logger.info("Rejected: a session is already active")
                await ctx.respond("A session is already active. Use `/session end` to wrap it up first.")
                return
            else:
                logger.info(f"Clearing stale session {existing['id']} from before a bot restart")
                await db.update_session_status(existing["id"], "ended")

        voice_channel = ctx.author.voice.channel
        vc = await voice_channel.connect()
        logger.info(f"Connected to voice channel '{voice_channel.name}', waiting for audio link to be ready...")

        if isinstance(voice_channel, discord.StageChannel):
            # Bots often join Stage Channels as suppressed "audience" members
            # by default, which would prevent audio from flowing at all.
            try:
                await ctx.guild.me.edit(suppress=False)
                logger.info("Un-suppressed self in Stage Channel")
            except discord.HTTPException:
                pass  # Non-fatal -- worst case the connection check below will catch it

        # The text-level connection can report success slightly before the
        # underlying voice/UDP link is actually ready to record. Give it a
        # generous window, and fail with a clear message (rather than a
        # confusing crash) if it never comes up.
        for _ in range(60):
            if vc.is_connected():
                break
            await asyncio.sleep(0.5)
        else:
            logger.warning("Voice connection never became ready after 30s -- giving up")
            await vc.disconnect(force=True)
            await ctx.respond(
                "I joined the voice channel but couldn't establish the audio connection "
                "after 30 seconds. This usually means voice traffic is being blocked "
                "somewhere between this server and Discord -- let's dig into the network "
                "setup further."
            )
            return

        started_at = datetime.datetime.utcnow().isoformat()
        session_id = await db.create_session(ctx.guild.id, voice_channel.id, started_at)
        state = GuildSessionState(session_id, vc)
        self.states[ctx.guild.id] = state

        await self._start_take(ctx, state)
        logger.info(f"Session {session_id} started, recording (take 0)")

        await ctx.respond(
            f"🎙️ Joined **{voice_channel.name}** and started recording notes.\n"
            f"Heads up to the table: this session is being recorded for note-taking. "
            f"Use `/session pause`, `/session resume`, or `/session end` to manage it."
        )

    @session.command(name="pause", description="Pause recording without ending the session")
    async def pause(self, ctx: discord.ApplicationContext):
        logger.info(f"/session pause invoked by {ctx.author.display_name} in guild {ctx.guild.id}")
        state = self.states.get(ctx.guild.id)
        if not state:
            logger.info("Rejected: no active session")
            await ctx.respond("There's no active session in this server.")
            return
        await ctx.defer()
        state.voice_client.stop_recording()
        await state.take_finished_event.wait()
        await db.update_session_status(state.session_id, "paused")
        logger.info(f"Session {state.session_id} paused")
        await ctx.respond("⏸️ Recording paused. Use `/session resume` when you're back.")

    @session.command(name="resume", description="Resume a paused session")
    async def resume(self, ctx: discord.ApplicationContext):
        logger.info(f"/session resume invoked by {ctx.author.display_name} in guild {ctx.guild.id}")
        state = self.states.get(ctx.guild.id)
        if not state:
            logger.info("Rejected: no paused session")
            await ctx.respond("There's no paused session in this server. Use `/session start` instead.")
            return
        await ctx.defer()
        await self._start_take(ctx, state)
        await db.update_session_status(state.session_id, "active")
        logger.info(f"Session {state.session_id} resumed (take {state.take_index})")
        await ctx.respond("▶️ Recording resumed.")

    @session.command(name="end", description="Stop recording, generate a summary, and post it")
    async def end(self, ctx: discord.ApplicationContext):
        logger.info(f"/session end invoked by {ctx.author.display_name} in guild {ctx.guild.id}")
        state = self.states.get(ctx.guild.id)
        if not state:
            logger.info("Rejected: no active session")
            await ctx.respond("There's no active session in this server.")
            return

        await ctx.respond("🧠 Wrapping up... transcribing and writing the recap. This can take a few minutes.")

        current_status = (await db.get_active_session(ctx.guild.id) or {}).get("status")
        if current_status == "active":
            logger.info("Stopping active recording...")
            state.voice_client.stop_recording()
            await state.take_finished_event.wait()
            logger.info("Recording stopped, final take transcribed")

        logger.info("Disconnecting from voice...")
        await self._fully_disconnect(ctx.guild)
        logger.info("Disconnect step complete")

        characters = await db.get_characters(ctx.guild.id)
        roster_lines = []
        for c in characters:
            if c.get("is_dm"):
                roster_lines.append(
                    f"{c['discord_name']} is the {c['character_name']} -- they narrate the "
                    f"environment, describe outcomes, and voice all NPCs (not a single character)"
                )
            else:
                roster_lines.append(f"{c['discord_name']} plays {c['character_name']}")
        roster = "\n".join(roster_lines) or "No character mappings set."

        state.all_lines.sort(key=lambda l: l.start)
        full_transcript = "\n".join(
            f"[{self._fmt_ts(l.start)}] {l.speaker_label}: {l.text}" for l in state.all_lines
        )
        logger.info(f"Full transcript assembled: {len(state.all_lines)} lines across {state.take_index} take(s)")

        session_dir = os.path.join(RECORDINGS_DIR, str(ctx.guild.id), str(state.session_id))
        transcript_path = os.path.join(session_dir, "transcript.txt")
        with open(transcript_path, "w") as f:
            f.write(full_transcript)

        if full_transcript.strip():
            logger.info("Sending transcript to Claude for summarization...")
            loop = asyncio.get_event_loop()
            summary = await loop.run_in_executor(None, summarize_session, full_transcript, roster)
            logger.info(f"Summary received ({len(summary)} chars)")
        else:
            logger.info("Transcript was empty -- skipping summarization")
            summary = "No speech was detected/transcribed for this session."

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
        logger.info(f"Recap posted to #{target_channel.name}, session {state.session_id} complete")

        del self.states[ctx.guild.id]
        await ctx.followup.send(f"✅ Recap posted in {target_channel.mention}.")

    async def _fully_disconnect(self, guild: discord.Guild):
        """
        Disconnects from voice as reliably as possible. This dev build of
        py-cord's higher-level VoiceClient teardown can hang (still-maturing
        DAVE/E2EE support) -- so instead we primarily rely on directly telling
        Discord's gateway "I've left voice" over the bot's main connection
        (which stays healthy even when the voice-specific handshake doesn't),
        then also attempt the higher-level cleanup for good measure.
        """
        try:
            await guild.change_voice_state(channel=None)
            logger.info("change_voice_state(None) succeeded")
        except Exception as e:
            logger.warning(f"change_voice_state failed: {e!r}")

        found_vc = False
        for vc in list(self.bot.voice_clients):
            if vc.guild.id == guild.id:
                found_vc = True
                try:
                    await asyncio.wait_for(vc.disconnect(force=True), timeout=10)
                    logger.info("vc.disconnect(force=True) succeeded")
                except Exception as e:
                    logger.warning(f"vc.disconnect failed: {e!r}")
                try:
                    vc.cleanup()
                    logger.info("vc.cleanup() succeeded")
                except Exception as e:
                    logger.warning(f"vc.cleanup failed: {e!r}")
        if not found_vc:
            logger.info("No lingering VoiceClient found for this guild (already clean)")

    @session.command(name="force-leave", description="Emergency: force the bot out of voice if it gets stuck")
    async def force_leave(self, ctx: discord.ApplicationContext):
        logger.info(f"/session force-leave invoked by {ctx.author.display_name} in guild {ctx.guild.id}")
        await ctx.defer()
        await self._fully_disconnect(ctx.guild)
        if ctx.guild.id in self.states:
            del self.states[ctx.guild.id]
        existing = await db.get_active_session(ctx.guild.id)
        if existing:
            await db.update_session_status(existing["id"], "ended")
        logger.info("force-leave complete")
        await ctx.respond("🔧 Forced the bot out of voice and cleared any stuck session state.")

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
