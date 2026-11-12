"""Run with: python bot.py (Python 3.11+)."""
import asyncio
import html
import logging
import os
import signal
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from core import Store, YouTube, YouTubeError, deliver

load_dotenv()
log = logging.getLogger('youtube-alerts')


def setting(name, default, minimum):
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise ValueError(f'{name} must be an integer.') from None
    if value < minimum:
        raise ValueError(f'{name} must be at least {minimum}.')
    return value


class AlertBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents(guilds=True))
        self.tree = app_commands.CommandTree(self)
        self.store = Store(os.getenv('DATABASE_PATH', 'data/alerts.sqlite3'))
        self.lock = asyncio.Lock()
        self.poll_seconds = setting('POLL_SECONDS', 300, 120)
        self.search_seconds = setting('LIVE_SEARCH_SECONDS', 1800, 1800)
        self.last_poll = 0
        self.last_error = 'None'
        self.session = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.youtube = YouTube(self.session, os.environ['YOUTUBE_API_KEY'])
        self.tree.add_command(YouTubeCommands(self))
        guild_id = os.getenv('DISCORD_GUILD_ID', '').strip()
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.monitor.change_interval(seconds=self.poll_seconds)
        self.monitor.start()

    async def on_ready(self):
        log.info('Connected as %s. Watching %d subscriptions.', self.user, len(self.store.subscriptions()))
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name='YouTube • /youtube help'))

    async def close(self):
        if self.monitor.is_running():
            task = self.monitor.get_task()
            self.monitor.cancel()
            if task and task is not asyncio.current_task():
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self.session:
            await self.session.close()
        self.store.db.close()
        await super().close()

    async def send_alert(self, sub, video, event):
        channel = self.get_channel(int(sub['destination'])) or await self.fetch_channel(int(sub['destination']))
        if not isinstance(channel, discord.TextChannel) or str(channel.guild.id) != sub['guild']:
            raise ValueError('Destination channel is unavailable or belongs to another server.')
        permissions = channel.permissions_for(channel.guild.me)
        if not (permissions.view_channel and permissions.send_messages and permissions.embed_links):
            raise ValueError('Bot needs View Channel, Send Messages, and Embed Links in destination.')
        snippet = video['snippet']
        label = '🔴 Live now' if event == 'live' else '🎬 New upload'
        url = f'https://www.youtube.com/watch?v={video["id"]}'
        embed = discord.Embed(title=html.unescape(snippet['title'])[:256], url=url,
                              description=f'{label} from **{discord.utils.escape_markdown(html.unescape(sub["title"]))[:200]}**',
                              color=0xFF3040 if event == 'live' else 0x5865F2)
        thumbnails = snippet.get('thumbnails', {})
        thumbnail = thumbnails.get('maxres') or thumbnails.get('high') or thumbnails.get('medium')
        if thumbnail:
            embed.set_image(url=thumbnail['url'])
        embed.set_footer(text='YouTube Alerts')
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label='Watch on YouTube', url=url))
        role = channel.guild.get_role(int(sub['role'])) if sub['role'] else None
        if role and not (role.mentionable or permissions.mention_everyone):
            raise ValueError('Selected notification role is no longer mentionable. Make it mentionable or re-add without a role.')
        await channel.send(content=role.mention if role else None, embed=embed, view=view,
                           allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=[role] if role else [], replied_user=False))

    @tasks.loop(seconds=300)
    async def monitor(self):
        async with self.lock:
            subscriptions = self.store.subscriptions()
            groups = {}
            for sub in subscriptions:
                groups.setdefault(sub['youtube'], []).append(sub)
            # A persisted global schedule prevents restarts or many subscriptions from
            # multiplying search usage. One search per 30 minutes, round-robin.
            discovered = {}
            errors = []
            now = time.time()
            if groups and now >= float(self.store.meta('next_search')):
                channels = sorted(groups)
                cursor = int(self.store.meta('search_cursor')) % len(channels)
                channel = channels[cursor]
                self.store.set_meta('next_search', now + self.search_seconds)
                self.store.set_meta('search_cursor', cursor + 1)
                try:
                    discovered[channel] = await self.youtube.live(channel)
                except YouTubeError as exc:
                    errors.append(str(exc))
            for channel, subs in groups.items():
                try:
                    ids = await self.youtube.uploads(subs[0]['playlist'])
                    ids += self.store.tracked(channel) + discovered.get(channel, [])
                    videos = await self.youtube.videos(ids)
                except YouTubeError as exc:
                    errors.append(str(exc))
                    continue
                # A broken Discord destination must not block other subscriptions.
                for sub in subs:
                    try:
                        await deliver(self.store, sub, videos, self.send_alert)
                    except (discord.HTTPException, ValueError) as exc:
                        # Discord exception text contains no YouTube request URL/key.
                        errors.append(f'Subscription {sub["id"]}: {type(exc).__name__}')
                        log.warning('Delivery failed for subscription %s (%s); check channel/role permissions.', sub['id'], type(exc).__name__)
            self.last_poll = time.time()
            self.last_error = '; '.join(errors)[:800] if errors else 'None'
            if errors:
                log.warning('Monitoring encountered errors: %s', self.last_error)

    @monitor.before_loop
    async def before_monitor(self):
        await self.wait_until_ready()

    @monitor.error
    async def monitor_error(self, error):
        self.last_error = f'Unexpected {type(error).__name__}; restart the bot and check configuration.'
        log.error(self.last_error)


@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
class YouTubeCommands(app_commands.Group, name='youtube', description='Manage YouTube upload and livestream alerts'):
    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def interaction_check(self, interaction):
        if not interaction.guild or not interaction.permissions.manage_guild:
            raise app_commands.CheckFailure('You need Manage Server to configure YouTube alerts.')
        return True

    @app_commands.command(name='add', description='Watch a YouTube channel for uploads and livestreams')
    @app_commands.describe(youtube='@handle, YouTube channel URL, or UC channel ID', destination='Discord channel for alerts', role='Optional role to ping (must be mentionable)')
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.guild_id)
    async def add(self, interaction: discord.Interaction, youtube: str, destination: discord.TextChannel, role: discord.Role | None = None):
        await interaction.response.defer(ephemeral=True)
        if destination.guild.id != interaction.guild_id:
            raise ValueError('Choose a channel in this server.')
        perms = destination.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            raise ValueError('Give the bot View Channel, Send Messages, and Embed Links in that channel.')
        if role and (role.is_default() or role.managed or role.guild.id != interaction.guild_id):
            raise ValueError('Choose a normal notification role in this server, not @everyone or a managed role.')
        if role and not (role.mentionable or perms.mention_everyone):
            raise ValueError('Make the selected role mentionable before adding it.')
        async with self.bot.lock:
            channel_id, title, playlist = await self.bot.youtube.resolve(youtube)
            existing = self.bot.store.subscriptions()
            if any(s['guild'] == str(interaction.guild_id) and s['destination'] == str(destination.id) and s['youtube'] == channel_id for s in existing):
                raise ValueError('This channel is already subscribed in that destination. Remove it first to change its role.')
            if channel_id not in {s['youtube'] for s in existing} and len({s['youtube'] for s in existing}) >= 10:
                raise ValueError('This bot supports up to 10 unique YouTube channels. Remove one first.')
            baseline = await self.bot.youtube.videos(await self.bot.youtube.uploads(playlist))
            sid = self.bot.store.add(interaction.guild_id, destination.id, channel_id, title, playlist, role.id if role else None, baseline)
        await interaction.followup.send(f'Added subscription **#{sid}** for **{discord.utils.escape_markdown(title)[:200]}** → {destination.mention}. Old uploads are skipped. Currently live streams can be announced on the next check.', ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name='remove', description='Remove a subscription using its ID from /youtube list')
    async def remove(self, interaction: discord.Interaction, subscription_id: int):
        await interaction.response.defer(ephemeral=True)
        async with self.bot.lock:
            removed = self.bot.store.remove(interaction.guild_id, subscription_id)
        await interaction.followup.send('Subscription removed.' if removed else 'Subscription not found in this server.', ephemeral=True)

    @app_commands.command(name='list', description='List the watched channels in this server')
    async def list_subscriptions(self, interaction: discord.Interaction):
        rows = [s for s in self.bot.store.subscriptions() if s['guild'] == str(interaction.guild_id)]
        await interaction.response.defer(ephemeral=True)
        lines = [f'**#{s["id"]}** [{discord.utils.escape_markdown(s["title"])[:100]}](https://www.youtube.com/channel/{s["youtube"]}) → <#{s["destination"]}>' for s in rows]
        pages, page = [], ''
        for line in lines:
            if len(page) + len(line) > 1800:
                pages.append(page)
                page = ''
            page += line + '\n'
        pages.append(page or 'No subscriptions yet. Use `/youtube add`.')
        for page in pages:
            await interaction.followup.send(page, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name='test', description='Send a sample embed without pinging roles')
    async def test(self, interaction: discord.Interaction, destination: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)
        if destination.guild.id != interaction.guild_id:
            raise ValueError('Choose a channel in this server.')
        embed = discord.Embed(title='YouTube alert test', description='Your upload and livestream alerts will appear here with a thumbnail and watch link.', color=0x5865F2)
        await destination.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        await interaction.followup.send('Test alert sent.', ephemeral=True)

    @app_commands.command(name='status', description='Show monitor status and the latest error')
    async def status(self, interaction: discord.Interaction):
        b = self.bot
        channels = len({s['youtube'] for s in b.store.subscriptions()})
        last = f'<t:{int(b.last_poll)}:R>' if b.last_poll else 'Waiting for first check'
        embed = discord.Embed(title='YouTube Alerts status', color=0x5865F2)
        embed.description = f'Monitor: {"Running" if b.monitor.is_running() else "Stopped"}\nLast check: {last}\nUpload check: {b.poll_seconds}s\nLive discovery: one channel every {b.search_seconds}s, rotating {channels} channel(s)\nLatest error: {b.last_error}'
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name='help', description='Show all YouTube alert commands')
    async def help(self, interaction: discord.Interaction):
        embed = discord.Embed(title='YouTube Alerts commands', color=0x5865F2, description=(
            '**/youtube add** — Select YouTube channel, Discord destination, optional role.\n'
            '**/youtube remove** — Delete a subscription by ID.\n'
            '**/youtube list** — Show subscriptions and IDs.\n'
            '**/youtube test** — Send a sample embed without pings.\n'
            '**/youtube status** — Check monitoring and errors.\n'
            '**/youtube help** — Show this command list.\n\n'
            'Manage Server permission required. New public videos and Shorts are treated as uploads. '
            'Streams and Premieres use live alerts when YouTube reports them live. Community posts are unsupported.'))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def on_error(self, interaction, error):
        original = getattr(error, 'original', error)
        if isinstance(original, (ValueError, YouTubeError, app_commands.CheckFailure, app_commands.CommandOnCooldown)):
            message = str(original)
        elif isinstance(original, discord.Forbidden):
            message = 'Discord blocked this action. Check the bot’s channel permissions.'
        else:
            message = f'Action failed ({type(original).__name__}). Check configuration and try again.'
            log.error('Command failed: %s', type(original).__name__)
        if interaction.response.is_done():
            await interaction.followup.send(message[:1800], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        else:
            await interaction.response.send_message(message[:1800], ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


async def main():
    for key in ('DISCORD_TOKEN', 'YOUTUBE_API_KEY'):
        value = os.getenv(key, '').strip()
        if not value or value.startswith('put_your_'):
            raise ValueError(f'Set {key} in .env or your host environment.')
    bot = AlertBot()
    async with bot:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.close()))
            except NotImplementedError:
                pass  # Windows: asyncio.run handles Ctrl+C.
        await bot.start(os.environ['DISCORD_TOKEN'])


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    try:
        asyncio.run(main())
    except (ValueError, discord.LoginFailure) as exc:
        raise SystemExit(str(exc)) from None
