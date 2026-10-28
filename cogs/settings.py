import discord
from discord import app_commands
from discord.ext import commands

import guild_settings
from guild_settings import GuildSettings

BRAND = discord.Color.from_str("#FF3CAC")
TEXT_TYPES = [discord.ChannelType.text, discord.ChannelType.news]
VOICE_TYPES = [discord.ChannelType.voice, discord.ChannelType.stage_voice]


def _channel_line(guild: discord.Guild, channel_id: int, needed: dict[str, bool]) -> str:
    """'• #channel', with a warning if the bot is missing a permission it needs there."""
    channel = guild.get_channel(channel_id)
    if channel is None:
        return f"• ~~deleted channel~~ `{channel_id}`"
    perms = channel.permissions_for(guild.me)
    missing = [name for name, attr in needed.items() if not getattr(perms, attr)]
    warning = f" ⚠️ *I can't {', '.join(missing)} here*" if missing else ""
    bitrate = getattr(channel, "bitrate", None)
    if bitrate:  # voice channels: the channel bitrate caps the sound quality everyone hears
        best = min(guild.bitrate_limit, 384_000)
        tip = f" — raise to {best // 1000} for better sound" if bitrate < best else ""
        warning += f" · 🎚️ {bitrate // 1000} kbps{tip}"
    return f"• {channel.mention}{warning}"


def settings_embed(guild: discord.Guild, settings: GuildSettings) -> discord.Embed:
    text_needed = {"see": "view_channel", "send messages": "send_messages", "embed links": "embed_links"}
    voice_needed = {"see": "view_channel", "connect": "connect", "speak": "speak"}

    embed = discord.Embed(
        color=BRAND,
        title="⚙️ E Music — Server Setup",
        description=(
            "Choose from the menus below — changes save instantly.\n"
            "Leave a menu **empty** to allow everything.\n"
            "Channel limits apply to **everyone**, admins too. "
            "Members with **Manage Server** skip the role check and can open `e!setup` anywhere."
        ),
    )
    embed.add_field(
        name="💬 Command channels",
        value="\n".join(_channel_line(guild, i, text_needed) for i in settings.text_channels) or "All channels",
        inline=False,
    )
    embed.add_field(
        name="🔊 Voice channels",
        value="\n".join(_channel_line(guild, i, voice_needed) for i in settings.voice_channels)
        or "All voice channels",
        inline=False,
    )
    embed.add_field(
        name="🎭 Allowed roles",
        value="\n".join(f"• <@&{i}>" for i in settings.roles) or "Everyone",
        inline=False,
    )
    if guild.me.display_avatar:
        embed.set_thumbnail(url=guild.me.display_avatar.url)
    return embed


class SetupView(discord.ui.View):
    """Dropdowns for the server's restrictions. Only the admin who opened it can use it."""

    def __init__(self, owner_id: int, guild: discord.Guild) -> None:
        super().__init__(timeout=600)
        self.owner_id = owner_id
        self.message: discord.Message | None = None
        settings = guild_settings.store.get(guild.id)
        # Pre-select the current choices (skipping channels/roles that were deleted).
        self.text_channels.default_values = [c for i in settings.text_channels if (c := guild.get_channel(i))]
        self.voice_channels.default_values = [c for i in settings.voice_channels if (c := guild.get_channel(i))]
        self.roles.default_values = [r for i in settings.roles if (r := guild.get_role(i))]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the person who opened this can change it.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        if self.message:
            try:
                await self.message.edit(view=None)
            except discord.HTTPException:
                pass

    async def save(self, interaction: discord.Interaction, settings: GuildSettings) -> None:
        guild = interaction.guild
        assert guild is not None
        guild_settings.store.set(guild.id, settings)
        view = SetupView(self.owner_id, guild)
        view.message = self.message
        self.stop()
        await interaction.response.edit_message(embed=settings_embed(guild, settings), view=view)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=TEXT_TYPES,
        placeholder="💬 Command channels (empty = all)",
        min_values=0,
        max_values=25,
        row=0,
    )
    async def text_channels(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect) -> None:
        settings = guild_settings.store.get(interaction.guild_id)  # type: ignore[arg-type]
        settings.text_channels = [c.id for c in select.values]
        await self.save(interaction, settings)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=VOICE_TYPES,
        placeholder="🔊 Voice channels (empty = all)",
        min_values=0,
        max_values=25,
        row=1,
    )
    async def voice_channels(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect) -> None:
        settings = guild_settings.store.get(interaction.guild_id)  # type: ignore[arg-type]
        settings.voice_channels = [c.id for c in select.values]
        await self.save(interaction, settings)

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="🎭 Roles that can use music (empty = everyone)",
        min_values=0,
        max_values=25,
        row=2,
    )
    async def roles(self, interaction: discord.Interaction, select: discord.ui.RoleSelect) -> None:
        settings = guild_settings.store.get(interaction.guild_id)  # type: ignore[arg-type]
        settings.roles = [r.id for r in select.values]
        await self.save(interaction, settings)

    @discord.ui.button(label="Reset all", emoji="♻️", style=discord.ButtonStyle.danger, row=3)
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.save(interaction, GuildSettings())

    @discord.ui.button(label="Done", emoji="✅", style=discord.ButtonStyle.success, row=3)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(view=None)


class Settings(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.hybrid_command(
        name="setup",
        aliases=["settings", "config"],
        description="Choose which channels and roles can use the bot (Manage Server only).",
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    @app_commands.default_permissions(manage_guild=True)  # hides /setup from members who can't use it
    async def setup_panel(self, ctx: commands.Context) -> None:
        assert ctx.guild is not None
        view = SetupView(ctx.author.id, ctx.guild)
        view.message = await ctx.send(
            embed=settings_embed(ctx.guild, guild_settings.store.get(ctx.guild.id)), view=view, ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Settings(bot))
