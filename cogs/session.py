"""
Recording model: a "session" can be made of multiple "takes" (start -> pause,
resume -> pause, ..., -> end, or an unexpected disconnect -> auto-reconnect).
Each take is recorded to its own set of per-user WAV files via py-cord's sink
API, transcribed independently, then all takes' transcript lines are merged
in chronological order (with a running time offset) when the session ends.
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

TRANSCRIPTION_TIMEOUT_SECONDS = 90 * 60
MAX_AUTO_RECONNECT_ATTEMPTS = 3


class GuildSessionState:
    def __init__(self, session_id: int, voice_client: discord.VoiceClient):
        self.session_id = session_id
        self.voice_client = voice_client
        self.channel_id = voice_client.channel.id
        self.take_index = 0
        self.elapsed_before_current_take = 0.0
        self.take_started_at: datetime.datetime | None = None
        self.all_lines: list[TranscriptLine] = []
        self.take_finished_event = asyncio.Event()
        self.session_start = datetime.datetime.utcnow()
        self.intentional_disconnect = False
        self.reconnect_attempts = 0


class SessionCog(commands.Cog):
    def __init__(self, bot: discord.Bot):
        self.bot = bot
        self.states: dict[int, GuildSessionState] = {}

    session = discord.SlashCommandGroup("session", "Manage D&D session recording")

    def _take_dir(self, guild_id: int, session_id: int, take_index: int) -> str:
        path = os.path.join(RECORDINGS_DIR, str(guild_id), str(session_id), f"take_{take_index}")
        os.makedirs(path, exist_ok=True)
        return path

    async def _start_take(self, guild_id: int, state: GuildSessionState):
        sink = discord.sinks.WaveSink()
        state.take_finished_event.clear()
        state.take_started_at = datetime.datetime.utcnow()
        state.voice_client.start_recording(sink, self._on_take_finished, guild_id, state)

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

            try:
                os.remove(wav_path)
            except OSError:
                pass

        state.elapsed_before_current_take += take_duration
        state.take_index += 1
        state.take_finished_event.set()
        logger.info(f"take_finished_event set for guild {guild_id}, take {state.take_index - 1}")

        try:
            shutil.rmtree(take_dir, ignore_errors=True)
        except OSError:
            pass

    async def _stop_recording_and_wait(self, state: GuildSessionState) -> bool:
        try:
            state.voice_client.stop_recording()
        except Exception as e:
            logger.warning(f"stop_recording() raised (may already be stopped): {e!r}")

        try:
            await asyncio.wait_for(
                state.take_finished_event.wait(), timeout=TRANSCRIPTION_TIMEOUT_SECONDS
            )
            logger.info("Take finished transcribing")
            return True
        except asyncio.TimeoutError:
            logger.error(
                f"Timed out after {TRANSCRIPTION_TIMEOUT_SECONDS}s waiting for take to finish "
                f"transcribing (session {state.session_id}) -- treating as stuck"
            )
            return False

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ):
        if member.id != self.bot.user.id:
            return
        if before.channel is None or after.channel is not None:
            return

        guild_id = before.channel.guild.id
        state = self.states.get(guild_id)
        if not state:
            return

        if state.intentional_disconnect:
            logger.info(f"Voice state update: intentional disconnect for guild {guild_id}, ignoring")
            return

        logger.warning(
            f"Unexpected voice disconnect detected for guild {guild_id} "
            f"(session {state.session_id}) -- attempting automatic recovery"
        )
        await self._handle_unexpected_disconnect(guild_id, before.channel, state)

    async def _handle_unexpected_disconnect(
        self, guild_id: int, channel: discord.VoiceChannel, state: GuildSessionState
    ):
        state.reconnect_attempts += 1
        if state.reconnect_attempts > MAX_AUTO_RECONNECT_ATTEMPTS:
            logger.error(
                f"Guild {guild_id}: exceeded {MAX_AUTO_RECONNECT_ATTEMPTS} automatic reconnect "
                f"attempts for session {state.session_id} -- giving up, needs manual attention"
            )
            await self._notify_recap_channel(
                guild_id,
                "⚠️ Lost the voice connection multiple times in a row and couldn't stay connected. "
                "I've stopped trying to auto-reconnect. Whatever was captured so far is safely "
                "preserved -- run `/session end` to wrap it up with what's recorded, or `/session "
                "force-leave` first if needed, then investigate before starting again.",
            )
            return

        logger.info(f"Finalizing dropped take for guild {guild_id} before reconnecting...")
        finished = await self._stop_recording_and_wait(state)
        if not finished:
            logger.error(
                f"Guild {guild_id}: timed out finalizing the dropped take -- attempting to "
                f"reconnect anyway, but this take's data may be incomplete"
            )

        try:
            vc = await channel.connect()
        except Exception as e:
            logger.error(f"Guild {guild_id}: failed to reconnect to voice: {e!r}")
            await self._notify_recap_channel(
                guild_id,
                f"⚠️ Lost the voice connection and the automatic attempt to rejoin **{channel.name}** "
                f"failed. Whatever was captured so far is safely preserved -- try `/session start` "
                f"again, or `/session end` to wrap up with what's recorded.",
            )
            return

        for _ in range(60):
            if vc.is_connected():
                break
            await asyncio.sleep(0.5)
        else:
            logger.error(f"Guild {guild_id}: reconnect attempt did not become ready after 30s")
            await self._notify_recap_channel(
                guild_id,
                f"⚠️ Lost the voice connection and couldn't fully re-establish audio after rejoining "
                f"**{channel.name}**. Whatever was captured so far is safely preserved -- try `/session "
                f"start` again, or `/session end` to wrap up with what's recorded.",
            )
            return

        state.voice_client = vc
        state.channel_id = channel.id
        await self._start_take(guild_id, state)
        logger.info(
            f"Guild {guild_id}: reconnected to '{channel.name}' and resumed recording "
            f"(take {state.take_index}, attempt {state.reconnect_attempts}/{MAX_AUTO_RECONNECT_ATTEMPTS})"
        )
        await self._notify_recap_channel(
            guild_id,
            f"🔄 Lost the voice connection for a moment but reconnected automatically and resumed "
            f"recording in **{channel.name}**. Nothing captured before the drop was lost.",
        )

    async def _notify_recap_channel(self, guild_id: int, message: str):
        summary_channel_id = await db.get_summary_channel(guild_id)
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        channel = guild.get_channel(summary_channel_id) if summary_channel_id else None
        if channel:
            try:
                await channel.send(message)
            except Exception as e:
                logger.warning(f"Failed to post reconnect notification: {e!r}")
        else:
            logger.info(f"(No summary channel configured to notify for guild {guild_id}: {message})")

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
            try:
                await ctx.guild.me.edit(suppress=False)
                logger.info("Un-suppressed self in Stage Channel")
            except discord.HTTPException:
                pass

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

        await self._start_take(ctx.guild.id, state)
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

        finished = await self._stop_recording_and_wait(state)
        if not finished:
            await ctx.respond(
                "⚠️ Pausing is taking unusually long -- transcription seems stuck rather than just "
                "slow. Recording has been stopped, but I can't confirm the transcript is complete. "
                "**Please don't restart the bot yet** -- completed data may still be recoverable from "
                "memory. Check the logs, or reach out for help troubleshooting this before continuing."
            )
            return

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
        await self._start_take(ctx.guild.id, state)
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

        logger.info("Stopping recording (if any) and waiting for final transcription...")
        finished = await self._stop_recording_and_wait(state)
        if not finished:
            await ctx.followup.send(
                "⚠️ This session seems stuck finishing up -- transcription has been running far "
                "longer than expected and I can't confirm it's complete. **Please don't restart the "
                "bot yet** -- any completed transcription is likely still safely sitting in memory "
                "and recoverable. This needs a human to check the logs before deciding next steps "
                "(running `/session end` again shortly may simply work, once whatever's stuck clears)."
            )
            return
        logger.info("Recording stopped, final take transcribed")

        state.intentional_disconnect = True
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

        await target_channel.send(file=discord.File(transcript_path, filename=f"session_{state.session_id}_transcript.txt"))
        logger.info(f"Recap posted to #{target_channel.name}, session {state.session_id} complete")

        del self.states[ctx.guild.id]
        await ctx.followup.send(f"✅ Recap posted in {target_channel.mention}.")

    async def _fully_disconnect(self, guild: discord.Guild):
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
        state = self.states.get(ctx.guild.id)
        if state:
            state.intentional_disconnect = True
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
