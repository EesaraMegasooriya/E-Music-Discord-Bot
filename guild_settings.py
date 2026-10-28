"""Per-server restrictions: which text channels, voice channels and roles can use the bot.

An empty list means "no restriction". Channel restrictions apply to everyone, admins included.
Members with Manage Server skip only the role check, and /setup itself is never restricted,
so admins can never lock themselves out.
"""

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

import discord

DATA_FILE = Path(__file__).parent / "data" / "guild_settings.json"


@dataclass
class GuildSettings:
    text_channels: list[int] = field(default_factory=list)
    voice_channels: list[int] = field(default_factory=list)
    roles: list[int] = field(default_factory=list)


class SettingsStore:
    """Settings for every server, kept in memory and saved to a JSON file on each change."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[int, GuildSettings] = {}
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            self._data = {int(gid): GuildSettings(**values) for gid, values in raw.items()}

    def get(self, guild_id: int) -> GuildSettings:
        current = self._data.get(guild_id, GuildSettings())
        return GuildSettings(list(current.text_channels), list(current.voice_channels), list(current.roles))

    def set(self, guild_id: int, settings: GuildSettings) -> None:
        if settings == GuildSettings():
            self._data.pop(guild_id, None)
        else:
            self._data[guild_id] = settings
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        payload = {str(gid): asdict(s) for gid, s in self._data.items()}
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)  # atomic: a crash mid-save never leaves a half-written file


store = SettingsStore(DATA_FILE)


def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.manage_guild


def _mentions(ids: list[int], prefix: str) -> str:
    return ", ".join(f"<{prefix}{i}>" for i in ids)


def text_channel_error(channel: discord.abc.GuildChannel | discord.Thread, settings: GuildSettings) -> str | None:
    if not settings.text_channels:
        return None
    channel_id = channel.parent_id if isinstance(channel, discord.Thread) else channel.id
    if channel_id in settings.text_channels:
        return None
    return f"🔒 Music commands only work in {_mentions(settings.text_channels, '#')}."


def voice_channel_error(channel: discord.abc.GuildChannel, settings: GuildSettings) -> str | None:
    if not settings.voice_channels or channel.id in settings.voice_channels:
        return None
    return f"🔒 I can only play music in {_mentions(settings.voice_channels, '#')}."


def role_error(member: discord.Member, settings: GuildSettings) -> str | None:
    if not settings.roles or is_admin(member) or any(role.id in settings.roles for role in member.roles):
        return None
    return f"🔒 You need one of these roles to use music: {_mentions(settings.roles, '@&')}."


def access_error(
    member: discord.Member, channel: discord.abc.GuildChannel | discord.Thread | None
) -> str | None:
    """Why this member can't use music commands here, or None if they can."""
    settings = store.get(member.guild.id)
    return role_error(member, settings) or (text_channel_error(channel, settings) if channel else None)
