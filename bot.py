import logging
import os
import random

import discord
import wavelink
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("emusic")
PREFIXES = ("e!", "E!")
STATUS_MINUTES = 10
CHILL_QUOTES = (
    "🎧 slow down, the music isn't going anywhere",
    "🌙 good vibes and lo-fi nights",
    "☕ coffee, headphones, no rush",
    "🌊 let the beat carry the stress away",
    "🍃 breathe in, press play, breathe out",
    "✨ one more song, then one more",
    "🌅 sunsets sound better with music",
    "🛋️ comfy seat, soft beats, zero worries",
    "🌧️ rainy days are for playlists",
    "🎶 turn it up, tune it out",
    "💜 some days just need a soundtrack",
    "🌌 vibing somewhere between the stars",
    "🕯️ low lights, good music, calm mind",
    "🚗 windows down, volume up",
    "🌸 soft songs for loud days",
    "😌 no plans, just music",
    "🎵 music says what words can't",
    "🌙 late nights and good tunes",
    "🍵 sip slow, listen slower",
    "🌿 stay calm and press play",
)


def chill_status(previous: str | None = None) -> discord.CustomActivity:
    """A random chill quote as the bot's custom status, never the same one twice in a row."""
    return discord.CustomActivity(name=random.choice([q for q in CHILL_QUOTES if q != previous]))


class EMusicBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()  # includes guilds + voice_states
        intents.message_content = True  # needed to read "e!" commands
        super().__init__(
            command_prefix=commands.when_mentioned_or(*PREFIXES),
            case_insensitive=True,
            help_command=None,  # replaced by the e!help command in cogs/music.py
            intents=intents,
            activity=chill_status(),
        )

    @tasks.loop(minutes=STATUS_MINUTES)
    async def rotate_status(self) -> None:
        if self.rotate_status.current_loop == 0:
            return  # the first quote was already set at login
        previous = self.activity.name if self.activity else None
        activity = chill_status(previous)
        await self.change_presence(activity=activity)
        self.activity = activity  # remembered so the next quote differs from this one

    @rotate_status.before_loop
    async def before_rotate_status(self) -> None:
        await self.wait_until_ready()

    async def setup_hook(self) -> None:
        node = wavelink.Node(
            identifier="main",
            uri=os.getenv("LAVALINK_URI", "http://127.0.0.1:2333"),
            password=os.getenv("LAVALINK_PASSWORD", "youshallnotpass"),
            inactive_player_timeout=int(os.getenv("IDLE_TIMEOUT", "300")),
            # Lavalink keeps the music playing while the bot reconnects after a restart.
            resume_timeout=60,
        )
        await wavelink.Pool.connect(nodes=[node], client=self, cache_capacity=500)
        await self.load_extension("cogs.music")
        await self.load_extension("cogs.settings")
        self.rotate_status.start()

        dev_guild = os.getenv("DEV_GUILD_ID")
        if dev_guild:
            guild = discord.Object(int(dev_guild))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("Synced %d slash commands", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (%s)", self.user, self.user.id)

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingRequiredArgument):
            text = f"Missing `{error.param.name}`. Usage: `e!{ctx.command.qualified_name} {ctx.command.signature}`"
        elif isinstance(error, (commands.BadArgument, commands.BadLiteralArgument, commands.RangeError)):
            text = f"{error} Usage: `e!{ctx.command.qualified_name} {ctx.command.signature}`"
        elif isinstance(error, commands.NoPrivateMessage):
            text = "Music commands only work in a server."
        elif isinstance(error, commands.MissingPermissions):
            text = "🔒 You need the **Manage Server** permission for that."
        elif isinstance(error, commands.CheckFailure):  # /setup restrictions; the message says why
            text = str(error)
        else:
            log.exception("Command %s failed", ctx.command, exc_info=error)
            text = "Something went wrong running that command."
        if ctx.interaction:
            await ctx.send(text, ephemeral=True)
        else:  # e! commands can't be private, so don't leave the notice cluttering the channel
            await ctx.send(text, delete_after=15)

    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload) -> None:
        log.info("Lavalink node '%s' ready (resumed=%s)", payload.node.identifier, payload.resumed)


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN is missing. Copy .env.example to .env and fill it in.")
    discord.utils.setup_logging(level=logging.INFO)
    EMusicBot().run(token, log_handler=None)


if __name__ == "__main__":
    main()
