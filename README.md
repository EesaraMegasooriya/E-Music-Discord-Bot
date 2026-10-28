<p align="center"><img src="assets/logo.png" width="160" alt="E Music Bot logo"></p>

# E Music Bot

A Discord music bot that plays from **YouTube**, **YouTube Music** and **Spotify** (plus Twitch and direct audio links).

## Why it doesn't lag

The bot never handles audio itself. Playback runs in **[Lavalink](https://lavalink.dev)**, a separate Java audio server:

- Audio is decoded and encoded on Lavalink's own threads, so a busy bot (slow commands, big playlists) can't make the music stutter.
- Lavalink keeps **5 seconds of encoded audio buffered** (`frameBufferDurationMs`), so short network stalls are never heard.
- It runs on **generational ZGC**, which keeps garbage-collection pauses under 1 ms.
- If the bot restarts, Lavalink **keeps the music playing** and the bot picks the session back up (`resume_timeout`).
- Seeking keeps the old audio playing until the new position is ready (`useSeekGhosting`).
- YouTube audio comes from the **IOS** client first. Measured: a full song downloads in about **0.5 s** (thousands of times faster than playback), with the first audio in about 0.2 s, so YouTube throttling can't cause stutter. The signed-in **TV** client (via the remote cipher) is the backup.
- Audio is encoded for Discord at maximum quality (`opusEncodingQuality: 10`, `resamplingQuality: HIGH`).
- The audio server runs at **above-normal CPU priority**, so games or browsers on the same PC can't starve it.
- If a stream ever stalls for 10 s, the bot reloads the song at the same spot; if it stalls again, it skips to the next song instead of going silent.

For the best results, run Lavalink on the same machine (or LAN) as the bot, and on a host with a stable connection close to your Discord voice region.

## Setup

**Requirements:** Python 3.10+ and Java 17+ (you already have both).

### 1. Create the Discord bot
1. Go to the <https://discord.com/developers/applications> page and click **New Application** → name it **E Music**.
2. **General Information** → upload `assets/logo.png` as the app icon.
3. **Bot** → upload `assets/logo.png` as the avatar, click **Reset Token** and copy the token. On the same page, turn on **Message Content Intent** (required for `e!` commands; without it the bot won't start).
4. **OAuth2 → URL Generator** → scopes `bot` + `applications.commands`; bot permissions **View Channels, Send Messages, Embed Links, Connect, Speak**. Open the URL to invite the bot.

### 2. Create a Spotify app (for Spotify links)
1. Open the <https://developer.spotify.com/dashboard> page → **Create app** (any redirect URI, e.g. `http://127.0.0.1:8888/callback`, and tick **Web API**).
2. Copy the **Client ID** and **Client Secret**.

### 3. Configure
Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN`, `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`.

### 4. Run (two terminals)
```powershell
# Terminal 1 — audio server (downloads Lavalink + plugins on first run)
.\lavalink\start-lavalink.ps1

# Terminal 2 — the bot (wait until Lavalink logs "Lavalink is ready to accept connections")
.\start-bot.ps1
```
If PowerShell blocks the scripts, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

### Ubuntu Server (runs automatically at boot)

Copy the project to the server, create `.env`, then run:

```bash
chmod +x deploy/install-ubuntu.sh
./deploy/install-ubuntu.sh
```

The installer installs Java 21 LTS, creates two systemd services, starts Lavalink before the bot, and
automatically restarts either process after a crash. Useful commands:

```bash
sudo systemctl status emusic-lavalink emusic-bot
sudo journalctl -u emusic-lavalink -u emusic-bot -f
sudo systemctl restart emusic-lavalink emusic-bot
```

Lavalink listens only on `127.0.0.1`, so no firewall rule or router port
forwarding is required. Disconnecting SSH does not stop either service.

Slash commands can take up to an hour to appear globally. Set `DEV_GUILD_ID` in `.env` to your server's ID to get them instantly while testing.

## Commands

Every command works with the **`e!`** prefix (any case, e.g. `E!PLAY`) and as a **`/`** slash command.

| Command | Short | What it does |
|---|---|---|
| `e!play <song or link>` | `e!p` | Plays a song or playlist. Accepts YouTube/Spotify links (tracks, albums, playlists, artists) or a search |
| `e!skip` | `e!s` | Skip the current song |
| `e!pause` / `e!resume` | `e!r` | Pause or resume |
| `e!stop` | `e!dc` | Stop, clear the queue, and leave |
| `e!nowplaying` | `e!np` | Bring the player panel to the bottom of the chat |
| `e!queue [page]` | `e!q` | Show the queue |
| `e!volume <0-150>` | `e!vol` | Set the volume |
| `e!loop <off/track/queue>` | `e!l` | Loop mode |
| `e!shuffle` | `e!mix` | Shuffle the queue |
| `e!seek <time>` | | Jump to a time, e.g. `e!seek 1:30` |
| `e!remove <number>` | `e!rm` | Remove a song from the queue |
| `e!clear` | `e!c` | Clear the queue, keep the current song |
| `e!help` | `e!h` | Show all commands |
| `e!setup` | `e!settings` | Choose which channels and roles can use the bot (Manage Server only) |

## Server setup (channels & roles)

Admins run `e!setup` (or `/setup`) to get a panel with three dropdowns. Changes save instantly and survive restarts (stored in `data/guild_settings.json`).

| Setting | Effect when set | When left empty |
|---|---|---|
| 💬 Command channels | Music commands only work in these text channels (and their threads) | Works in every channel |
| 🔊 Voice channels | The bot only joins these voice channels | Joins any voice channel |
| 🎭 Allowed roles | Only members with one of these roles can use commands and the player buttons | Everyone can use it |

- Channel limits apply to **everyone, admins included**. Members with **Manage Server** only skip the role check, and `e!setup` itself works in any channel, so admins can't lock themselves out.
- The panel flags channels where the bot is missing permissions (e.g. can't **Connect**/**Speak**) and deleted channels.
- It also shows each voice channel's bitrate. **Raise voice channels to the highest bitrate your server allows (96 kbps, or more with boosts)**; Discord's default 64 kbps is the biggest limit on sound quality.
- `e!help` works everywhere so people can always find out how to use the bot.

## Player panel

Every song posts a player card at the bottom of the chat (the old one is removed). It shows the big cover art, title, artist, a **live progress bar** (updates every 10 s), requester, volume, loop mode and the next 3 songs. The card turns grey while paused.

| Row | Buttons |
|---|---|
| Playback | ⏮️ previous · ⏪ back 10s · ⏸️/▶️ pause/resume · ⏩ forward 10s · ⏭️ skip |
| Settings | 🔉 volume −10 · 🔊 volume +10 · 🔁/🔂 loop off → track → queue · 🔀 shuffle · ⏹️ stop |
| Extras | ↩️ Replay · 📃 Queue (only you see it) |

Buttons that can't do anything right now are greyed out (e.g. seeking a live stream), and active modes are highlighted green. `/nowplaying` moves the panel to the bottom of the chat. Only people in the bot's voice channel can use the buttons. The bot leaves on its own after `IDLE_TIMEOUT` seconds with nothing playing.

## How Spotify works
Spotify doesn't allow streaming its audio to bots, so every Spotify song plays from YouTube:

- **Track links and `spsearch:`** go through the LavaSrc plugin, which matches the exact recording by ISRC code.
- **Album, playlist and artist links** are handled by [spotify_links.py](spotify_links.py). Since February 2026 Spotify no longer gives new developer apps playlist contents or artist top tracks, and the plugin's album loading uses an endpoint Spotify removed. So the bot reads the song list from Spotify's public embed player and finds each song on YouTube Music, checking the length so it doesn't pick covers or remixes. The first song starts right away; the rest are added in the background (a 50-song playlist takes about 10 s to fill in).
- The embed player lists up to about 50–100 songs, so very long playlists are cut short.

## Troubleshooting
- **SoundCloud links don't play:** SoundCloud changed its audio formats and Lavalink's built-in SoundCloud support can't read them yet (as of Lavalink 4.2.2 / lavaplayer 2.2.7). Nothing to fix in the bot; it will work again once Lavalink updates.
- **Bot says "Couldn't play … skipping" / "Sign in to confirm you're not a bot":** YouTube sign-in is already enabled. If the saved sign-in stops working, delete the `YOUTUBE_REFRESH_TOKEN` value in `.env`, set `skipInitialization: false` in `lavalink/application.yml`, restart Lavalink, and enter the code it prints at <https://www.google.com/device> using a **spare** Google account. Then copy the refresh token Lavalink prints into `.env`.
- **"Must find sig function" errors:** YouTube changed its player code. Update the `youtube-plugin` version in `application.yml` to the newest [release](https://github.com/lavalink-devs/youtube-source/releases) or latest commit hash (with `snapshot: true`), then restart Lavalink.
- **Bot says it can't connect to Lavalink:** start Lavalink first and check that `LAVALINK_PASSWORD` in `.env` matches `application.yml`.
- **Slash commands don't show up:** set `DEV_GUILD_ID`, or wait for global sync.

## Project layout
```
bot.py                     bot startup, Lavalink connection, command sync
cogs/music.py              all music commands, now-playing embed and buttons
lavalink/application.yml   audio server config (YouTube + Spotify plugins, buffering)
lavalink/start-lavalink.ps1
start-bot.ps1
assets/logo.svg / logo.png
```
