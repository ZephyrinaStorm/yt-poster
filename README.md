# YouTube Alerts Discord Bot

A simple slash-command bot that posts YouTube uploads and livestream alerts into your Discord server. Python 3.11 or newer; tested with Python 3.12 and discord.py 2.7.1.

## Included

- Public upload alerts, including Shorts returned by YouTube's uploads playlist.
- Live alerts, including scheduled streams when they become live.
- Embeds with title, thumbnail, channel name, and a Watch on YouTube button.
- Optional notification-role pings. No automatic @everyone pings.
- Multiple YouTube channels and Discord destinations (10 unique YouTube channels per bot).
- SQLite subscription and notification history that survives restarts when the database is retained.
- One-page embedded help, permission checks, delivery retries, and a status command.
- Render Blueprint for a background worker with a persistent disk.

## 1. Create your Discord bot

1. Open https://discord.com/developers/applications and create an application.
2. Open **Bot**, create/reset its token, and keep that token private.
3. Under **OAuth2 → URL Generator**, select `bot` and `applications.commands`.
4. Select **View Channels**, **Send Messages**, and **Embed Links**, then open the generated invite URL and choose your server.
5. Privileged intents (Message Content, Members, Presence) are not needed. Administrator permission is not needed.

The bot only uses slash commands. Server members need **Manage Server** to configure or test alerts.

## 2. Create a YouTube API key

1. Open https://console.cloud.google.com/ and create or select a project.
2. In **APIs & Services → Library**, enable **YouTube Data API v3**.
3. In **Credentials**, create an API key.
4. Restrict its API access to **YouTube Data API v3**. This bot runs on a server, so a browser HTTP-referrer restriction will not work.
5. Set the key as `YOUTUBE_API_KEY`. A YouTube account login or OAuth callback URL is not required for public-channel checks.

Never upload your real `.env` to GitHub or paste tokens into Discord.

## 3. Run on your computer

Extract the ZIP and open a terminal inside the `youtube-alert-bot` folder.

```sh
python -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in:

```dotenv
DISCORD_TOKEN=your_real_discord_bot_token
YOUTUBE_API_KEY=your_real_youtube_api_key
DISCORD_GUILD_ID=your_discord_server_id
DATABASE_PATH=data/alerts.sqlite3
POLL_SECONDS=300
LIVE_SEARCH_SECONDS=1800
```

`DISCORD_GUILD_ID` is optional. Use it for immediate command registration in one server. To copy a server ID, enable Discord Developer Mode and right-click the server. Without it, commands register globally and may take time to appear. Keep registration mode consistent to avoid temporarily seeing both guild and global command versions.

Start:

```sh
python bot.py
```

Keep the computer and process running for alerts. Press Ctrl+C to stop. Keep the `data` folder when updating your code; it holds your subscriptions and delivery history. Run only one instance of this bot against a database.

## 4. Configure alerts in Discord

```text
/youtube add youtube:@YourChannel destination:#youtube-alerts
```

Or add a role:

```text
/youtube add youtube:@YourChannel destination:#youtube-alerts role:@YouTube Notifications
```

Discord shows these as slash-command fields. Select a role that is mentionable, or give the bot the relevant mention permission in the destination. A role that becomes unmentionable later will block that subscription's alerts until you fix it or remove/re-add the subscription without the role.

You can also use `https://www.youtube.com/@YourChannel` or a `UC...` channel ID. Legacy `/c/` URLs, video URLs, and channel-tab URLs are not accepted; use the channel's root @handle URL.

| Command | Purpose |
| --- | --- |
| `/youtube add` | Add YouTube channel, Discord destination, optional role |
| `/youtube list` | Show subscriptions and their IDs |
| `/youtube remove subscription_id:1` | Delete a subscription in your server |
| `/youtube test destination:#youtube-alerts` | Send a sample embed without pinging anyone |
| `/youtube status` | Show monitor timing, running state, and latest error |
| `/youtube help` | Show every command in one embed |

To change the destination or role, remove the subscription and add it again. Deleting a subscription deletes its history. Adding it again starts a new baseline.

## 5. Host on Render

The included Blueprint creates a **paid background worker and persistent disk**. Check the displayed cost in Render before deploying. This package does not deploy anything by itself.

1. Create a GitHub repository and upload the **contents** of `youtube-alert-bot`, so `bot.py`, `requirements.txt`, and `render.yaml` are at the repository root. Do not upload the ZIP itself or your `.env`.
2. In Render, choose **New → Blueprint**, connect that repository, and use `render.yaml`.
3. Enter `DISCORD_TOKEN` and `YOUTUBE_API_KEY` when prompted. Review the resources/cost and deploy.
4. Optionally add `DISCORD_GUILD_ID` in the worker's Environment settings, then save and deploy.
5. Confirm the logs show `Connected as ...`, then use `/youtube add` in Discord.

Manual worker settings, if you prefer creating the service yourself:

| Setting | Value |
| --- | --- |
| Service type | Background Worker |
| Runtime | Python |
| Build command | `pip install -r requirements.txt` |
| Start command | `python bot.py` |
| Disk mount path | `/var/data` |
| Disk size | 1 GB |
| `DATABASE_PATH` | `/var/data/alerts.sqlite3` |
| Other environment variables | `DISCORD_TOKEN`, `YOUTUBE_API_KEY`; optional `DISCORD_GUILD_ID` |

Do not choose Static Site. The bot uses an outbound Discord connection and has no HTTP port. Keep the persistent disk attached: an ephemeral database would lose subscriptions on redeployment. Disk-backed services run one instance and briefly stop during redeploys.

## Timing and limits

- **Uploads:** checked every 5 minutes by default, plus any delay before YouTube exposes the video through its API. This is polling, not an instant-notification guarantee.
- **Known scheduled/live videos:** rechecked every upload cycle. Streams already live when you subscribe can be announced on the next cycle.
- **Additional live discovery:** one `search.list` request every 30 minutes across the entire bot, rotating through unique YouTube channels. With one channel this is about 30 minutes; with two, about an hour per channel; with ten, about five hours. Short streams absent from the uploads playlist can be missed between live searches.
- **API quota:** default discovery uses at most approximately 48 search requests per day. The schedule is persisted to avoid reset-related bursts. Google quotas vary by project; check your Cloud Console. Upload and video-detail requests also consume quota. Other apps using the same key/project share those limits. Larger channel lists, accumulated scheduled videos, or lowering polling intervals can increase use.
- **First subscription:** the latest 50 uploads are marked as history to avoid posting old uploads. Subsequent checks inspect the latest 50 playlist entries plus known pending/retry videos. More than 50 new entries between checks or a long outage can cause missed uploads.
- **Public API coverage:** private, members-only, unlisted content not discoverable through these endpoints, and YouTube Community text/image posts are unsupported. Shorts are ordinary upload alerts; no separate Shorts filter is claimed. Premieres can look like live broadcasts in YouTube's API and may generate the same live alert.
- **Delivery history:** successful sends are recorded after Discord accepts them. A crash in the tiny gap between sending and saving can duplicate one notification. Normal restarts retain history. Deleted Discord messages are not automatically reposted.
- **Availability:** quota errors, API delays, network outages, and missing Discord permissions delay alerts. Check `/youtube status` for errors. The next polling cycle retries failed sends; YouTube transient errors get short bounded retries.
- **Data:** SQLite stores server/channel/role IDs, YouTube channel details, observed video IDs, and delivery flags. No message content, passwords, or YouTube account credentials are collected. Keep the database private. Stop the bot before copying the database and its associated files for backup.

## Troubleshooting

- **No commands:** re-invite with `applications.commands`; check the server ID and command visibility permissions. Global commands may take time to appear.
- **No alert immediately after adding:** old uploads are deliberately skipped. Use `/youtube test`, then wait for a new public upload or livestream.
- **403 from YouTube:** check that Data API v3 is enabled, key restrictions allow server use, and the project has quota available.
- **No role ping or delivery blocked:** check role mentionability and the bot's channel permissions.
- **Monitor stopped:** restart the worker, then inspect `/youtube status` and configuration.
- **Missing file on Render:** upload the extracted files at the repo root and use `python bot.py` as the start command. If you kept an outer folder, set Render's Root Directory to that folder.

## Tests

```sh
python -m unittest discover -s tests -v
```

Tests simulate YouTube and Discord data without credentials. They cover old-upload suppression, upcoming → live → archived transitions, retry behavior, independent destinations, persisted history, scoped removal, URL validation, command permissions, and monitor isolation. A real end-to-end Discord post still requires your credentials and server.

## Official references

- Discord bot setup: https://discord.com/developers/docs/quick-start/getting-started
- YouTube API setup: https://developers.google.com/youtube/v3/getting-started
- Live metadata: https://developers.google.com/youtube/v3/docs/videos
- Live searches: https://developers.google.com/youtube/v3/docs/search/list
- Quotas: https://developers.google.com/youtube/v3/determine_quota_cost
- Render workers: https://render.com/docs/background-workers
- Render disks: https://render.com/docs/disks
