"""YouTube API access and durable notification state."""
import asyncio
import re
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlparse

import aiohttp


def channel_reference(value):
    value = value.strip()
    if value.startswith(('https://', 'http://')):
        url = urlparse(value)
        if url.hostname not in ('youtube.com', 'www.youtube.com', 'm.youtube.com'):
            raise ValueError('Use a youtube.com channel URL, @handle, or UC channel ID.')
        value = unquote(url.path).strip('/')
        if value.startswith('channel/'):
            value = value[8:]
    if re.fullmatch(r'UC[\w-]{22}', value):
        return {'id': value}
    if re.fullmatch(r'@[^/\s?#]{1,100}', value):
        return {'forHandle': value}
    raise ValueError('Use an @handle, youtube.com/@handle URL, or UC channel ID. Video and /c/ URLs are unsupported.')


def video_state(video):
    live = video.get('liveStreamingDetails', {})
    flag = video['snippet'].get('liveBroadcastContent', 'none')
    if live.get('actualEndTime'):
        return 'archive'
    if live.get('actualStartTime') or flag == 'live':
        return 'live'
    if flag == 'upcoming' or live.get('scheduledStartTime'):
        return 'upcoming'
    return 'upload'


class YouTubeError(Exception):
    pass


class YouTube:
    def __init__(self, session, key):
        self.session, self.key = session, key

    async def get(self, endpoint, **params):
        # Never log request URLs: they contain the API key.
        for attempt in range(3):
            try:
                async with self.session.get(
                    f'https://www.googleapis.com/youtube/v3/{endpoint}',
                    params={'key': self.key, **params},
                    timeout=aiohttp.ClientTimeout(total=25),
                ) as response:
                    payload = await response.json(content_type=None)
                    if response.status == 200:
                        return payload.get('items', [])
                    if response.status in (429, 500, 502, 503, 504) and attempt < 2:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    reasons = payload.get('error', {}).get('errors', [])
                    reason = reasons[0].get('reason', 'requestFailed') if reasons else 'requestFailed'
                    raise YouTubeError(f'YouTube HTTP {response.status}: {reason}. Check API key, enabled API, and quota.')
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                if attempt == 2:
                    raise YouTubeError('YouTube connection failed; retry later.') from None
                await asyncio.sleep(2 ** attempt)
        raise YouTubeError('YouTube request failed.')

    async def resolve(self, reference):
        items = await self.get('channels', part='snippet,contentDetails', **channel_reference(reference))
        if not items:
            raise ValueError('YouTube channel not found or not public.')
        item = items[0]
        return item['id'], item['snippet']['title'], item['contentDetails']['relatedPlaylists']['uploads']

    async def uploads(self, playlist):
        items = await self.get('playlistItems', part='contentDetails', playlistId=playlist, maxResults=50)
        return [item['contentDetails']['videoId'] for item in items]

    async def videos(self, ids):
        ids = sorted(set(ids))
        result = []
        for offset in range(0, len(ids), 50):
            result.extend(await self.get('videos', part='snippet,liveStreamingDetails', id=','.join(ids[offset:offset+50])))
        return result

    async def live(self, channel):
        items = await self.get('search', part='id', channelId=channel, type='video', eventType='live', maxResults=50)
        return [item['id']['videoId'] for item in items]


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA foreign_keys=ON;
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS subscriptions(
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild TEXT NOT NULL,
                destination TEXT NOT NULL, youtube TEXT NOT NULL, title TEXT NOT NULL,
                playlist TEXT NOT NULL, role TEXT,
                UNIQUE(guild, destination, youtube));
            CREATE TABLE IF NOT EXISTS observations(
                subscription INTEGER REFERENCES subscriptions(id) ON DELETE CASCADE,
                video TEXT NOT NULL, state TEXT NOT NULL,
                upload_sent INTEGER NOT NULL DEFAULT 0, live_sent INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(subscription, video));
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')

    def subscriptions(self):
        return self.db.execute('SELECT * FROM subscriptions ORDER BY id').fetchall()

    def add(self, guild, destination, youtube, title, playlist, role, baseline):
        with self.db:
            cursor = self.db.execute('INSERT INTO subscriptions(guild,destination,youtube,title,playlist,role) VALUES(?,?,?,?,?,?)',
                                     (str(guild), str(destination), youtube, title, playlist, str(role) if role else None))
            sid = cursor.lastrowid
            for video in baseline:
                state = video_state(video)
                self.db.execute('INSERT INTO observations VALUES(?,?,?,?,?)',
                                (sid, video['id'], state, int(state != 'upcoming'), int(state not in ('live', 'upcoming'))))
        return sid

    def remove(self, guild, sid):
        with self.db:
            return self.db.execute('DELETE FROM subscriptions WHERE guild=? AND id=?', (str(guild), sid)).rowcount

    def tracked(self, youtube):
        return [row[0] for row in self.db.execute('''SELECT DISTINCT o.video FROM observations o
            JOIN subscriptions s ON s.id=o.subscription WHERE s.youtube=?
            AND (o.state IN ('upcoming','live') OR (o.state='upload' AND o.upload_sent=0))''', (youtube,))]

    def observe(self, sid, video):
        state = video_state(video)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO observations(subscription,video,state) VALUES(?,?,?)', (sid, video['id'], state))
            self.db.execute('UPDATE observations SET state=? WHERE subscription=? AND video=?', (state, sid, video['id']))
        return self.db.execute('SELECT * FROM observations WHERE subscription=? AND video=?', (sid, video['id'])).fetchone()

    def mark(self, sid, video_id, event):
        if event not in ('upload', 'live'):
            raise ValueError('Invalid event')
        with self.db:
            self.db.execute(f'UPDATE observations SET {event}_sent=1 WHERE subscription=? AND video=?', (sid, video_id))

    def meta(self, key, default='0'):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        with self.db:
            self.db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))


async def deliver(store, subscription, videos, send):
    """Only acknowledge successful sends. Failed items remain eligible for retry."""
    for video in sorted(videos, key=lambda v: v['snippet'].get('publishedAt', '')):
        if video['snippet'].get('channelId') != subscription['youtube']:
            continue
        row = store.observe(subscription['id'], video)
        state = row['state']
        if state in ('upload', 'live') and not row[f'{state}_sent']:
            await send(subscription, video, state)
            store.mark(subscription['id'], video['id'], state)
