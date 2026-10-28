import asyncio
import logging
import os
from typing import Literal, cast

import aiohttp
import discord
import wavelink
from discord import app_commands
from discord.ext import commands

import guild_settings
import spotify_links

log = logging.getLogger("emusic.music")
BRAND = discord.Color.from_str("#FF3CAC")
PAUSED = discord.Color.from_str("#6B6F80")
# 100 = the source's original loudness (no gain applied). Opus sources can then even pass through untouched.
DEFAULT_VOLUME = int(os.getenv("DEFAULT_VOLUME", "100"))
PANEL_REFRESH_SECONDS = 10  # live progress bar; much faster edits would hit Discord's rate limits
SEEK_STEP_MS = 10_000
VOLUME_STEP = 10
MAX_VOLUME = 150
LOOP_LABELS = {
    wavelink.QueueMode.normal: "Off",
    wavelink.QueueMode.loop: "Track",
    wavelink.QueueMode.loop_all: "Queue",
}
SOURCES = {
    "spotify": "🟢 Spotify",
    "youtube": "🔴 YouTube",
    "soundcloud": "🟠 SoundCloud",
    "twitch": "🟣 Twitch",
    "bandcamp": "🔵 Bandcamp",
}


class MusicRestricted(commands.CheckFailure):
    """Raised when /setup restrictions block a command; the message says why."""


class EPlayer(wavelink.Player):
    """Player that remembers where its control panel lives."""

    home: discord.abc.Messageable | None = None
    np_message: discord.Message | None = None
    panel_task: asyncio.Task | None = None
    stuck_retry_for: str | None = None  # encoded track already reloaded once after stalling


# ---------- formatting ----------


def fmt_time(ms: int) -> str:
    s = ms // 1000
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def parse_time(text: str) -> int | None:
    parts = text.strip().split(":")
    if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        return None
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + int(p)
    return seconds * 1000


def progress_bar(position: int, length: int, size: int = 18) -> str:
    filled = min(size - 1, int(size * position / length)) if length > 0 else 0
    return "━" * filled + "●" + "─" * (size - filled - 1)


def short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def track_line(track: wavelink.Playable, limit: int = 60) -> str:
    length = "LIVE" if track.is_stream else fmt_time(track.length)
    return f"[{discord.utils.escape_markdown(short(track.title, limit))}]({track.uri}) `{length}`"


def requester_mention(track: wavelink.Playable) -> str:
    uid = getattr(track.extras, "requester", None)
    return f"<@{uid}>" if uid else "Autoplay"


def panel_embed(player: EPlayer) -> discord.Embed:
    """The now-playing card: artwork, live progress, settings, and what's up next."""
    track = player.current
    if track is None:
        return discord.Embed(color=PAUSED, description="⏹️ **Queue finished.** Use `/play` to add more music.")

    if track.is_stream:
        progress = "🔴 **LIVE**"
    else:
        position = min(player.position, track.length)
        progress = f"`{fmt_time(position)}` {progress_bar(position, track.length)} `{fmt_time(track.length)}`"

    embed = discord.Embed(
        color=PAUSED if player.paused else BRAND,
        title=short(track.title, 256),
        url=track.uri,
        description=f"by **{discord.utils.escape_markdown(track.author or 'Unknown')}**\n\n{progress}",
    )
    icon = player.client.user.display_avatar.url if player.client.user else None
    embed.set_author(name="⏸  Paused" if player.paused else "♫  Now Playing", icon_url=icon)

    embed.add_field(name="👤 Requested by", value=requester_mention(track))
    embed.add_field(name="🔊 Volume", value=f"{player.volume}%")
    embed.add_field(name="🔁 Loop", value=LOOP_LABELS[player.queue.mode])

    upcoming = player.queue[:3]
    if upcoming:
        lines = [f"`{i}.` {track_line(t, 45)}" for i, t in enumerate(upcoming, 1)]
        if len(player.queue) > 3:
            lines.append(f"*…and {len(player.queue) - 3} more*")
        embed.add_field(name="📃 Up Next", value="\n".join(lines), inline=False)

    if track.artwork:
        embed.set_image(url=track.artwork)
    embed.set_footer(text=f"{SOURCES.get(track.source, '🎵 ' + track.source.title())}  •  E Music")
    return embed


def queue_embed(player: EPlayer, page: int = 1) -> discord.Embed:
    per_page = 10
    tracks = list(player.queue)
    pages = max(1, -(-len(tracks) // per_page))
    page = min(page, pages)
    start = (page - 1) * per_page
    lines = [f"`{i}.` {track_line(t)}" for i, t in enumerate(tracks[start : start + per_page], start + 1)]

    embed = discord.Embed(color=BRAND, title="📃 Queue")
    if player.current:
        embed.add_field(name="♫ Now Playing", value=track_line(player.current), inline=False)
    embed.add_field(name="Up Next", value="\n".join(lines) or "Nothing queued.", inline=False)
    total = sum(t.length for t in tracks if not t.is_stream)
    embed.set_footer(
        text=f"Page {page}/{pages} • {len(tracks)} tracks • {fmt_time(total)} • Loop: {LOOP_LABELS[player.queue.mode]}"
    )
    return embed


def same_channel_error(member: discord.Member, player: EPlayer | None) -> str | None:
    if player is None or not player.connected:
        return "I'm not playing anything right now."
    voice = member.voice
    if not voice or voice.channel != player.channel:
        return f"Join {player.channel.mention} to control the music."
    return None


# ---------- control panel ----------


class Controls(discord.ui.View):
    """Buttons under the now-playing card. Built from the player's state so they show what's active."""

    def __init__(self, player: EPlayer) -> None:
        super().__init__(timeout=None)
        track = player.current
        seekable = track is not None and not track.is_stream

        self.previous.disabled = len(player.queue.history or []) < 2
        self.back.disabled = self.forward.disabled = self.replay.disabled = not seekable
        if player.paused:
            self.pause_resume.emoji = "▶️"
            self.pause_resume.style = discord.ButtonStyle.success
        self.volume_down.disabled = player.volume <= 0
        self.volume_up.disabled = player.volume >= MAX_VOLUME
        if player.queue.mode is wavelink.QueueMode.loop:
            self.loop.emoji = "🔂"
        if player.queue.mode is not wavelink.QueueMode.normal:
            self.loop.style = discord.ButtonStyle.success
        self.shuffle.disabled = len(player.queue) < 2

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        member = cast(discord.Member, interaction.user)
        if error := guild_settings.access_error(member, None) or same_channel_error(member, self.player(interaction)):
            await interaction.response.send_message(error, ephemeral=True)
            return False
        return True

    @staticmethod
    def player(interaction: discord.Interaction) -> EPlayer:
        return cast(EPlayer, interaction.guild.voice_client)  # type: ignore[union-attr]

    @staticmethod
    async def redraw(interaction: discord.Interaction, player: EPlayer) -> None:
        await interaction.response.edit_message(embed=panel_embed(player), view=Controls(player))

    # Row 1: playback

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, row=0)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self.player(interaction)
        history = player.queue.history
        if history is None or len(history) < 2:
            return await interaction.response.send_message("No previous track.", ephemeral=True)
        # History ends with [..., previous, current]; both get re-added when played again.
        previous = history[-2]
        del history[-2:]
        if player.current:
            player.queue.put_at(0, player.current)
        await interaction.response.defer()
        await player.play(previous)

    @discord.ui.button(emoji="⏪", style=discord.ButtonStyle.secondary, row=0)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self.player(interaction)
        await player.seek(max(0, player.position - SEEK_STEP_MS))
        await self.redraw(interaction, player)

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.primary, row=0)
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self.player(interaction)
        await player.pause(not player.paused)
        await self.redraw(interaction, player)

    @discord.ui.button(emoji="⏩", style=discord.ButtonStyle.secondary, row=0)
    async def forward(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self.player(interaction)
        if player.current:
            await player.seek(min(player.current.length - 1000, player.position + SEEK_STEP_MS))
        await self.redraw(interaction, player)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        await self.player(interaction).skip(force=True)

    # Row 2: settings

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, row=1)
    async def volume_down(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self.player(interaction)
        await player.set_volume(max(0, player.volume - VOLUME_STEP))
        await self.redraw(interaction, player)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, row=1)
    async def volume_up(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self.player(interaction)
        await player.set_volume(min(MAX_VOLUME, player.volume + VOLUME_STEP))
        await self.redraw(interaction, player)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, row=1)
    async def loop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self.player(interaction)
        order = list(LOOP_LABELS)
        player.queue.mode = order[(order.index(player.queue.mode) + 1) % len(order)]
        await self.redraw(interaction, player)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, row=1)
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self.player(interaction)
        player.queue.shuffle()
        await self.redraw(interaction, player)

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, row=1)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        await leave(self.player(interaction))

    # Row 3: extras

    @discord.ui.button(emoji="↩️", label="Replay", style=discord.ButtonStyle.secondary, row=2)
    async def replay(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        player = self.player(interaction)
        await player.seek(0)
        await self.redraw(interaction, player)

    @discord.ui.button(emoji="📃", label="Queue", style=discord.ButtonStyle.secondary, row=2)
    async def show_queue(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(embed=queue_embed(self.player(interaction)), ephemeral=True)


async def _panel_updater(player: EPlayer) -> None:
    """Keep the progress bar moving while a track plays."""
    while True:
        await asyncio.sleep(PANEL_REFRESH_SECONDS)
        message = player.np_message
        if message is None or not player.connected:
            return
        if player.current is None:  # queue ran out
            await close_panel(player)
            return
        if player.paused:
            continue
        try:
            await message.edit(embed=panel_embed(player))
        except discord.NotFound:
            player.np_message = None
            return
        except discord.HTTPException:
            pass


async def send_panel(player: EPlayer, ctx: commands.Context | None = None) -> None:
    """Post a fresh panel (as the command's reply if given) and remove the old one."""
    old = player.np_message
    embed, view = panel_embed(player), Controls(player)
    if ctx:
        player.np_message = await ctx.send(embed=embed, view=view)
    elif player.home:
        player.np_message = await player.home.send(embed=embed, view=view)
    if old and old.id != getattr(player.np_message, "id", None):
        try:
            await old.delete()
        except discord.HTTPException:
            pass
    if player.panel_task is None or player.panel_task.done():
        player.panel_task = asyncio.create_task(_panel_updater(player))


async def refresh_panel(player: EPlayer) -> None:
    """Redraw the panel after a slash command changed something."""
    if player.np_message and player.current:
        try:
            await player.np_message.edit(embed=panel_embed(player), view=Controls(player))
        except discord.HTTPException:
            pass


async def close_panel(player: EPlayer, text: str | None = None) -> None:
    """Turn the panel into a final 'stopped' card without buttons."""
    if player.panel_task and player.panel_task is not asyncio.current_task():
        player.panel_task.cancel()
    player.panel_task = None
    if player.np_message:
        embed = panel_embed(player) if text is None else discord.Embed(color=PAUSED, description=text)
        try:
            await player.np_message.edit(embed=embed, view=None)
        except discord.HTTPException:
            pass
        player.np_message = None


async def leave(player: EPlayer, text: str = "⏹️ **Stopped.** Use `/play` to start again.") -> None:
    """Clear the queue, close the panel, and disconnect."""
    player.queue.clear()
    await close_panel(player, text)
    await player.disconnect()


class Music(commands.Cog):
    """Every command works as both `e!name` and `/name`."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.background_tasks: set[asyncio.Task] = set()

    async def cog_check(self, ctx: commands.Context) -> bool:
        """Apply the server's /setup restrictions (command channels and roles) to every music command."""
        if ctx.guild is None or ctx.command is None or ctx.command.name == "help":
            return True
        if error := guild_settings.access_error(cast(discord.Member, ctx.author), ctx.channel):  # type: ignore[arg-type]
            raise MusicRestricted(error)
        return True

    # ---------- helpers ----------

    async def queue_in_background(
        self, player: EPlayer, tracks: list[spotify_links.SpotifyTrack], requester: int
    ) -> None:
        """Find the rest of a Spotify collection on YouTube and add it to the queue in order."""
        try:
            found = await spotify_links.find_all_on_youtube(tracks)
            if not player.connected or not found:
                return
            for track in found:
                track.extras = {"requester": requester}
            await player.queue.put_wait(found)
            if player.playing:
                await refresh_panel(player)
            else:  # the first song may have ended (or been skipped) while we were searching
                await player.play(player.queue.get())
        except Exception:
            log.exception("Queueing Spotify tracks failed")

    async def reply(self, ctx: commands.Context, text: str, *, ephemeral: bool = False) -> None:
        await ctx.send(embed=discord.Embed(color=BRAND, description=text), ephemeral=ephemeral)

    async def controlled_player(self, ctx: commands.Context) -> EPlayer | None:
        """Return the guild's player if the user is allowed to control it, otherwise reply with why not."""
        player = cast(EPlayer | None, ctx.voice_client)
        if error := same_channel_error(ctx.author, player):  # type: ignore[arg-type]
            await self.reply(ctx, error, ephemeral=True)
            return None
        return player

    # ---------- wavelink events ----------

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload) -> None:
        player = cast(EPlayer | None, payload.player)
        if player and player.stuck_retry_for != payload.track.encoded:
            player.stuck_retry_for = None  # a different song: it gets its own reload attempt
        if player and player.home:
            await send_panel(player)  # new panel at the bottom of the chat for every song

    @commands.Cog.listener()
    async def on_wavelink_track_stuck(self, payload: wavelink.TrackStuckEventPayload) -> None:
        """A stream stalled (no audio for trackStuckThresholdMs): reload it at the same spot once, then skip."""
        player = cast(EPlayer | None, payload.player)
        track = player.current if player else None
        if player is None or track is None or not player.connected:
            return
        if player.stuck_retry_for == track.encoded:
            log.warning("Track stalled again, skipping: %s", track.title)
            player.stuck_retry_for = None
            await player.skip(force=True)
            return
        log.warning("Track stalled, reloading at %d ms: %s", player.position, track.title)
        player.stuck_retry_for = track.encoded
        await player.play(track, start=player.position, add_history=False)

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload) -> None:
        player = cast(EPlayer | None, payload.player)
        if player and player.home:
            await player.home.send(f"⚠️ Couldn't play **{payload.track.title}**, skipping.", delete_after=15)

    @commands.Cog.listener()
    async def on_wavelink_inactive_player(self, player: EPlayer) -> None:
        await leave(player, "👋 Left the voice channel after being idle.")

    # ---------- commands ----------

    @commands.hybrid_command(
        aliases=["p"], description="Play a song or playlist from YouTube, Spotify, or a search."
    )
    @app_commands.describe(query="Song name, or a YouTube / Spotify link")
    @commands.guild_only()
    async def play(self, ctx: commands.Context, *, query: str) -> None:
        member = cast(discord.Member, ctx.author)
        voice = member.voice
        if not voice or not voice.channel:
            return await self.reply(ctx, "Join a voice channel first.", ephemeral=True)

        player = cast(EPlayer | None, ctx.voice_client)
        if player is None:
            settings = guild_settings.store.get(member.guild.id)
            if error := guild_settings.voice_channel_error(voice.channel, settings):
                return await self.reply(ctx, error, ephemeral=True)
            perms = voice.channel.permissions_for(member.guild.me)
            if not (perms.view_channel and perms.connect and perms.speak):
                return await self.reply(
                    ctx, f"I need **Connect** and **Speak** permission in {voice.channel.mention}.", ephemeral=True
                )
            limit = getattr(voice.channel, "user_limit", 0)
            if limit and len(voice.channel.members) >= limit and not perms.move_members:
                return await self.reply(ctx, f"{voice.channel.mention} is full.", ephemeral=True)

        await ctx.defer()
        if player is None:
            try:
                player = await voice.channel.connect(cls=EPlayer, self_deaf=True)
            except Exception as e:  # noqa: BLE001 - surface any connect failure to the user
                return await self.reply(ctx, f"Couldn't join {voice.channel.mention}: `{e}`")
            player.autoplay = wavelink.AutoPlayMode.partial  # advance through the queue automatically
            player.home = ctx.channel
            await player.set_volume(DEFAULT_VOLUME)
        elif player.channel != voice.channel:
            return await self.reply(ctx, f"I'm already playing in {player.channel.mention}.")

        try:
            collection = await spotify_links.fetch_collection(query)
        except (aiohttp.ClientError, asyncio.TimeoutError, LookupError, KeyError, ValueError) as e:
            return await self.reply(ctx, f"Couldn't read that Spotify link: `{e}`")

        if collection is not None:
            # Spotify album/playlist/artist: start the first song now, find the rest in the background.
            name, pending = collection
            first = None
            while pending and first is None:
                first = await spotify_links.find_on_youtube(pending.pop(0))
            if first is None:
                return await self.reply(ctx, f"Couldn't find any songs from **{name}**.")
            first.extras = {"requester": ctx.author.id}
            await player.queue.put_wait(first)
            await self.reply(ctx, f"📃 Queuing **{len(pending) + 1}** tracks from **{name}**.")
            if pending:
                task = asyncio.create_task(self.queue_in_background(player, pending, ctx.author.id))
                self.background_tasks.add(task)
                task.add_done_callback(self.background_tasks.discard)
        else:
            try:
                # Links (YouTube, Spotify tracks, SoundCloud...) load directly; plain text searches YouTube Music.
                results = await wavelink.Playable.search(query, source=wavelink.TrackSource.YouTubeMusic)
            except wavelink.LavalinkLoadException as e:
                return await self.reply(ctx, f"Search failed: `{e.error}`")
            if not results:
                return await self.reply(ctx, f"No results for **{discord.utils.escape_markdown(query)}**.")

            if isinstance(results, wavelink.Playlist):
                results.track_extras(requester=ctx.author.id)
                added = await player.queue.put_wait(results)
                await self.reply(ctx, f"📃 Queued **{added}** tracks from **{results.name}**.")
            else:
                track = results[0]
                track.extras = {"requester": ctx.author.id}
                await player.queue.put_wait(track)
                if player.playing:
                    await self.reply(ctx, f"➕ Queued {track_line(track)} at position **{len(player.queue)}**.")
                else:
                    await self.reply(ctx, f"🎶 Loading {track_line(track)}")

        if player.playing:
            await refresh_panel(player)  # update the "Up Next" list
        else:
            await player.play(player.queue.get())

    @commands.hybrid_command(aliases=["s", "next"], description="Skip the current song.")
    @commands.guild_only()
    async def skip(self, ctx: commands.Context) -> None:
        if player := await self.controlled_player(ctx):
            skipped = player.current
            await player.skip(force=True)
            await self.reply(ctx, f"⏭️ Skipped **{skipped.title if skipped else 'track'}**.")

    @commands.hybrid_command(description="Pause the music.")
    @commands.guild_only()
    async def pause(self, ctx: commands.Context) -> None:
        if player := await self.controlled_player(ctx):
            await player.pause(True)
            await self.reply(ctx, "⏸️ Paused.")
            await refresh_panel(player)

    @commands.hybrid_command(aliases=["r", "unpause"], description="Resume the music.")
    @commands.guild_only()
    async def resume(self, ctx: commands.Context) -> None:
        if player := await self.controlled_player(ctx):
            await player.pause(False)
            await self.reply(ctx, "▶️ Resumed.")
            await refresh_panel(player)

    @commands.hybrid_command(aliases=["leave", "dc"], description="Stop the music, clear the queue, and leave.")
    @commands.guild_only()
    async def stop(self, ctx: commands.Context) -> None:
        if player := await self.controlled_player(ctx):
            await leave(player)
            await self.reply(ctx, "⏹️ Stopped and left the channel.")

    @commands.hybrid_command(
        name="nowplaying", aliases=["np", "player"], description="Show the player with all controls."
    )
    @commands.guild_only()
    async def now_playing(self, ctx: commands.Context) -> None:
        player = cast(EPlayer | None, ctx.voice_client)
        if not player or not player.current:
            return await self.reply(ctx, "Nothing is playing.", ephemeral=True)
        await send_panel(player, ctx)  # moves the panel to the bottom of the chat

    @commands.hybrid_command(aliases=["q"], description="Show the queue.")
    @app_commands.describe(page="Page number")
    @commands.guild_only()
    async def queue(self, ctx: commands.Context, page: commands.Range[int, 1] = 1) -> None:
        player = cast(EPlayer | None, ctx.voice_client)
        if not player or (not player.current and not player.queue):
            return await self.reply(ctx, "The queue is empty.", ephemeral=True)
        await ctx.send(embed=queue_embed(player, page))

    @commands.hybrid_command(aliases=["vol", "v"], description="Set the volume (0-150).")
    @commands.guild_only()
    async def volume(self, ctx: commands.Context, level: commands.Range[int, 0, 150]) -> None:
        if player := await self.controlled_player(ctx):
            await player.set_volume(level)
            await self.reply(ctx, f"🔊 Volume set to **{level}%**.")
            await refresh_panel(player)

    @commands.hybrid_command(
        aliases=["l", "repeat"], description="Loop the current track, the whole queue, or turn looping off."
    )
    @commands.guild_only()
    async def loop(self, ctx: commands.Context, mode: Literal["off", "track", "queue"]) -> None:
        if player := await self.controlled_player(ctx):
            player.queue.mode = {
                "off": wavelink.QueueMode.normal,
                "track": wavelink.QueueMode.loop,
                "queue": wavelink.QueueMode.loop_all,
            }[mode]
            await self.reply(ctx, f"🔁 Loop: **{mode}**.")
            await refresh_panel(player)

    @commands.hybrid_command(aliases=["mix"], description="Shuffle the queue.")
    @commands.guild_only()
    async def shuffle(self, ctx: commands.Context) -> None:
        if player := await self.controlled_player(ctx):
            player.queue.shuffle()
            await self.reply(ctx, f"🔀 Shuffled **{len(player.queue)}** tracks.")
            await refresh_panel(player)

    @commands.hybrid_command(description="Jump to a time in the current song, e.g. 1:30.")
    @app_commands.describe(time="Time like 90, 1:30 or 1:02:30")
    @commands.guild_only()
    async def seek(self, ctx: commands.Context, time: str) -> None:
        if player := await self.controlled_player(ctx):
            ms = parse_time(time)
            if ms is None or not player.current or player.current.is_stream or ms > player.current.length:
                return await self.reply(ctx, "Can't seek there.", ephemeral=True)
            await player.seek(ms)
            await self.reply(ctx, f"⏩ Jumped to `{fmt_time(ms)}`.")
            await refresh_panel(player)

    @commands.hybrid_command(aliases=["rm"], description="Remove a song from the queue.")
    @app_commands.describe(position="Position shown in the queue")
    @commands.guild_only()
    async def remove(self, ctx: commands.Context, position: commands.Range[int, 1]) -> None:
        if player := await self.controlled_player(ctx):
            if position > len(player.queue):
                return await self.reply(ctx, "There's no track at that position.", ephemeral=True)
            track = player.queue[position - 1]
            player.queue.delete(position - 1)
            await self.reply(ctx, f"🗑️ Removed {track_line(track)}.")
            await refresh_panel(player)

    @commands.hybrid_command(aliases=["c"], description="Clear the queue (keeps the current song playing).")
    @commands.guild_only()
    async def clear(self, ctx: commands.Context) -> None:
        if player := await self.controlled_player(ctx):
            player.queue.clear()
            await self.reply(ctx, "🧹 Queue cleared.")
            await refresh_panel(player)

    @commands.hybrid_command(aliases=["h", "commands"], description="List all commands.")
    async def help(self, ctx: commands.Context) -> None:
        embed = discord.Embed(
            color=BRAND,
            title="🎧 E Music — Commands",
            description="Use the prefix **`e!`** or slash **`/`**, e.g. `e!play` or `/play`.\n"
            "Join a voice channel first, then play something!",
        )
        sections = [
            ("🎵 Music", [
                ("play <song or link>", "p", "Play from YouTube or Spotify"),
                ("pause", None, "Pause the music"),
                ("resume", "r", "Resume the music"),
                ("skip", "s", "Skip the current song"),
                ("stop", "dc", "Stop and leave the channel"),
                ("seek <1:30>", None, "Jump to a time in the song"),
            ]),
            ("📃 Queue", [
                ("queue [page]", "q", "Show the queue"),
                ("nowplaying", "np", "Show the player with buttons"),
                ("shuffle", None, "Shuffle the queue"),
                ("loop <off/track/queue>", "l", "Set the loop mode"),
                ("remove <number>", "rm", "Remove a song from the queue"),
                ("clear", None, "Clear the queue"),
            ]),
            ("⚙️ Settings", [
                ("volume <0-150>", "vol", "Set the volume"),
                ("setup", None, "Pick channels & roles for the bot (admins)"),
                ("help", "h", "Show this list"),
            ]),
        ]
        for title, entries in sections:
            lines = [
                f"`e!{usage}`" + (f" · `e!{alias}`" if alias else "") + f"\n└ {text}"
                for usage, alias, text in entries
            ]
            embed.add_field(name=title, value="\n".join(lines), inline=False)
        if self.bot.user:
            embed.set_thumbnail(url=self.bot.user.display_avatar.url)
        embed.set_footer(text="Tip: the player panel has buttons for everything!")
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
