"""Parse a music player's own window title into (song, artist).

Windows publishes the title-bar text; for the known local players that text is
``歌名 - 歌手``.  Per-application capture has no media-session identity (Windows
does not map a session to a PID), so this title is the only *per-PID* song
evidence available.  It stays a hint: it names a playback and can signal a
probable track change, it never proves which process produced the PCM.

No screen pixels, page content or keystrokes are read here: only the title bar
string Windows already exposes.
"""
from __future__ import annotations

MAX_SONG = 120
MAX_ARTIST = 80

# The title bar appends the player's own name; that suffix is not a song.
APP_SUFFIXES = (
    '网易云音乐', 'QQ音乐', '酷狗音乐', '酷我音乐', '咪咕音乐', '汽水音乐',
    'Spotify Premium', 'Spotify', 'Apple Music', 'Amazon Music',
    'YouTube Music', 'MusicBee', 'foobar2000', 'AIMP', 'Media Player',
)

# Titles that are only the application name or a generic placeholder.
PLACEHOLDER = {
    '网易云音乐', 'qq音乐', '酷狗音乐', '酷我音乐', '咪咕音乐', '汽水音乐',
    'spotify', 'spotify premium', 'apple music', 'amazon music',
    'youtube music', 'musicbee', 'foobar2000', 'aimp', 'media player',
    '音乐', 'music', '未知歌曲', '未命名歌曲', 'cloudmusic',
}

# Separators players put between song and artist.
SEPARATORS = (' - ', ' – ', ' — ', ' • ', ' | ')

_SEP_AND_SUFFIX = tuple((sep, name) for name in APP_SUFFIXES
                        for sep in (' - ', ' – ', ' — ', ' | '))


def _strip_app_suffix(text: str) -> str:
    low = text.casefold()
    for sep, name in _SEP_AND_SUFFIX:
        tail = (sep + name).casefold()
        if low.endswith(tail) and len(text) > len(tail):
            return text[:len(text) - len(tail)].strip()
    return text.strip()


def parse_player_window_title(text) -> tuple[str, str] | None:
    """Return (song, artist) or None when the title is not a song title.

    Conservative on purpose: a title without a separator is left alone because
    it may be a page name, a playlist name or a dialog.  A wrong song name is
    worse than an unknown one.
    """
    raw = ' '.join(str(text or '').split())
    if not raw or len(raw) > 300:
        return None
    cleaned = _strip_app_suffix(raw)
    if not cleaned or cleaned.casefold() in PLACEHOLDER:
        return None
    for sep in SEPARATORS:
        if sep not in cleaned:
            continue
        left, _, right = cleaned.partition(sep)
        left, right = left.strip(), right.strip()
        if not left or not right:
            return None
        if left.casefold() in PLACEHOLDER or right.casefold() in PLACEHOLDER:
            return None
        # A trailing " - X" that is still a player name means the title had an
        # extra suffix we could not strip; do not guess which half is the song.
        if any(right.casefold() == name.casefold() for name in APP_SUFFIXES):
            return None
        if any(right.casefold().endswith(name.casefold())
               for name in APP_SUFFIXES):
            return None
        return left[:MAX_SONG], right[:MAX_ARTIST]
    return None
