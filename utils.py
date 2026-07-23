import re
import time
import logging
from collections import OrderedDict
import httpx

logger = logging.getLogger("utils")


class BoundedTTLCache:
    """A tiny dict-like cache with a max size (LRU eviction) and per-entry TTL.

    Prevents unbounded memory growth from caches keyed on user-controlled
    input (search queries, chat_id/message_id pairs, etc).
    """

    def __init__(self, maxsize: int = 1000, ttl: float = 1800):
        self.maxsize = maxsize
        self.ttl = ttl
        self._data = OrderedDict()

    def get(self, key):
        entry = self._data.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.time() - ts > self.ttl:
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def get_with_age(self, key):
        """Return (timestamp, value) without TTL enforcement, or None."""
        entry = self._data.get(key)
        if entry is None:
            return None
        self._data.move_to_end(key)
        return entry

    def set(self, key, value, timestamp=None):
        self._data[key] = (timestamp if timestamp is not None else time.time(), value)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)

    def __contains__(self, key):
        return self.get(key) is not None

    def __len__(self):
        return len(self._data)

def format_size(bytes_size: int) -> str:
    if not bytes_size:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while bytes_size >= 1024 and i < len(units) - 1:
        bytes_size /= 1024.0
        i += 1
    return f"{bytes_size:.2f} {units[i]}"

# Normalize common numbers and terminology for reliable matching
_NORM_MAP = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
    "uno": "1", "dos": "2", "tres": "3", "cuatro": "4", "cinco": "5",
    "seis": "6", "siete": "7", "ocho": "8", "nueve": "9", "diez": "10",
    "temporada": "season", "temp": "season", "capitulo": "episode", 
    "capítulo": "episode", "cap": "episode", "ep": "episode", "ch": "episode", "chapter": "episode"
}

def _normalize_filename(text: str) -> str:
    if not text:
        return ""
    t = text.lower()
    t = re.sub(r'\.[a-z0-9]{2,5}$', '', t)  # strip file extension
    t = re.sub(r'[._\-]', ' ', t)
    
    words = t.split()
    new_words = []
    for w in words:
        if w in _NORM_MAP:
            w = _NORM_MAP[w]
        new_words.append(w)
    return " ".join(new_words)

# Regex lists for identifying seasons/episodes in both orders
_SEASON_EPISODE_PATTERNS = [
    re.compile(r'\bs\s*(?P<s>\d{1,2})\s*[.\-_ ]?\s*e\s*(?P<e>\d{1,3})\b', re.IGNORECASE),
    re.compile(r'\bseason\s*(?P<s>\d{1,2})\D{0,10}?episode\s*(?P<e>\d{1,3})\b', re.IGNORECASE),
    re.compile(r'\bt\s*(?P<s>\d{1,2})\s*[.\-_ ]?\s*c\s*(?P<e>\d{1,3})\b', re.IGNORECASE),
    re.compile(r'(?<!\d)(?P<s>\d{1,2})\s*[xX]\s*(?P<e>\d{1,3})(?!\d)'),
]

_EPISODE_SEASON_PATTERNS = [
    re.compile(r'\be\s*(?P<e>\d{1,3})\s*[.\-_ xX]?\s*s\s*(?P<s>\d{1,2})\b', re.IGNORECASE),
    re.compile(r'\bepisode\s*(?P<e>\d{1,3})\D{0,10}?season\s*(?P<s>\d{1,2})\b', re.IGNORECASE),
    re.compile(r'\bc\s*(?P<e>\d{1,3})\s*[.\-_ ]?\s*t\s*(?P<s>\d{1,2})\b', re.IGNORECASE),
]

_STANDALONE_EPISODE_PATTERNS = [
    re.compile(r'\bep(?:isode)?\s*[.\-_ ]?\s*(?P<e>\d{1,3})\b', re.IGNORECASE),
    re.compile(r'\bcap(?:itulo|ítulo)?\s*[.\-_ ]?\s*(?P<e>\d{1,3})\b', re.IGNORECASE),
    re.compile(r'\[(?P<e>\d{2,3})\]'),
    re.compile(r'(?:^|[\s\-_])[-–]\s*(?P<e>\d{2,3})\s*(?:[-–]|$|\.)'),
]

def parse_season_episode(filename: str) -> tuple:
    if not filename:
        return None, None
    
    fn = _normalize_filename(filename)
    
    for pat in _SEASON_EPISODE_PATTERNS:
        m = pat.search(fn)
        if m:
            try:
                s = int(m.group('s'))
                e = int(m.group('e'))
                return s, e
            except (ValueError, KeyError, IndexError):
                pass
                
    for pat in _EPISODE_SEASON_PATTERNS:
        m = pat.search(fn)
        if m:
            try:
                s = int(m.group('s'))
                e = int(m.group('e'))
                return s, e
            except (ValueError, KeyError, IndexError):
                pass
                
    for pat in _STANDALONE_EPISODE_PATTERNS:
        m = pat.search(fn)
        if m:
            try:
                e = int(m.group('e'))
                return 1, e
            except (ValueError, KeyError, IndexError):
                pass
                
    return None, None

def matches_episode(filename: str, season: int, episode: int) -> bool:
    if season is None or episode is None:
        return True
        
    f_season, f_episode = parse_season_episode(filename)
    if f_season == season and f_episode == episode:
        return True
        
    fn = _normalize_filename(filename)
    
    patterns = [
        rf'\bs\s*{season:02d}\s*[.\-_ ]?\s*e\s*{episode:02d}\b',
        rf'\bs\s*{season}\s*[.\-_ ]?\s*e\s*{episode:02d}\b',
        rf'(?<!\d){season}[xX]{episode:02d}(?!\d)',
        rf'(?<!\d){season}[xX]{episode}(?!\d)',
        rf'\[season\s*0*{season}\].*?\[episode\s*0*{episode}\]',
        rf'season\s*0*{season}\D{{0,20}}?episode\s*0*{episode}(?!\d)',
        rf'\bt\s*{season:02d}\s*c\s*{episode:02d}\b',
        rf'\bt\s*{season}\s*c\s*{episode}\b',
        rf'(?<!\d){season}{episode:02d}(?!\d)',
    ]
    
    # Allow fallback standalone episode checks for Season 1
    has_explicit_season = any(re.search(p, fn, re.IGNORECASE) for p in [r'\bs\d', r'\bseason\s*\d', r'\bt\d', r'\d+[xX]'])
    if season == 1 and not has_explicit_season:
        patterns += [
            rf'\bepisode\s*0*{episode}\b',
            rf'\bcap\s*0*{episode}\b',
            rf'\[0*{episode}\]',
            rf'[-–]\s*0*{episode:02d}\s*(?:[-–]|$)',
        ]
    
    for pat in patterns:
        if re.search(pat, fn, re.IGNORECASE):
            return True
            
    return False

def matches_subtitle(video_filename: str, sub_filename: str) -> bool:
    if not video_filename or not sub_filename:
        return False
        
    v_fn = video_filename.lower()
    s_fn = sub_filename.lower()
    
    v_base = v_fn.rsplit('.', 1)[0]
    s_base = s_fn.rsplit('.', 1)[0]
    
    s_base_clean = re.sub(r'\.(eng|en|english|sub|subtitle|srt|vtt)$', '', s_base)
    
    if s_base_clean in v_base or v_base in s_base_clean:
        return True
        
    return False

def get_search_query_from_filename(filename: str) -> str:
    if not filename:
        return ""
    name = filename.lower()
    name = name.rsplit('.', 1)[0]
    name = re.sub(r'[._\-]', ' ', name)
    
    terms = r'\b(2160p|1080p|720p|480p|360p|4k|8k|10bit|h264|x264|h265|x265|hevc|web[- ]?rip|bluray|brrip|hdrip)\b'
    match = re.search(terms, name)
    if match:
        name = name[:match.start()]
    
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def parse_split_info(filename: str) -> tuple:
    if not filename:
        return None, None
        
    # Match suffix .001, .002 etc.
    m1 = re.search(r'\.(\d{3,4})$', filename)
    if m1:
        part = int(m1.group(1))
        base = filename[:m1.start()]
        return base, part
        
    # Match part1, part01, part_1 etc.
    m2 = re.search(r'[._\- ]part_?(\d+)(?:\.([^.]+))?$', filename, re.IGNORECASE)
    if m2:
        part = int(m2.group(1))
        ext = m2.group(2) or ""
        base = filename[:m2.start()]
        if ext:
            base += f".{ext}"
        return base, part
        
    return None, None

_metadata_cache = BoundedTTLCache(maxsize=2000, ttl=6 * 3600)

async def get_metadata_from_cinemeta(meta_type: str, imdb_id: str) -> dict:
    if not meta_type or not imdb_id:
        return {}
    # Harden against path injection / unexpected upstream requests
    if meta_type not in ("movie", "series") or not re.fullmatch(r"tt\d{5,12}", imdb_id.split(":")[0]):
        logger.warning(f"Rejected invalid Cinemeta lookup: type={meta_type!r} id={imdb_id!r}")
        return {}

    cache_key = f"{meta_type}:{imdb_id}"
    cached = _metadata_cache.get(cache_key)
    if cached is not None:
        return cached

    imdb_clean = imdb_id.split(":")[0]
    url = f"https://v3-cinemeta.strem.io/meta/{meta_type}/{imdb_clean}.json"
    logger.info(f"Fetching metadata from Cinemeta: {url}")
    
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                meta = data.get("meta", {})
                if meta:
                    result = {
                        "name": meta.get("name"),
                        "year": meta.get("year"),
                        "genres": meta.get("genres", []),
                        "poster": meta.get("poster")
                    }
                    _metadata_cache.set(cache_key, result)
                    return result
    except Exception as e:
        logger.error(f"Cinemeta metadata lookup failed: {e}")
        
    return {}

VIDEO_EXTENSIONS = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.ts', '.m4v')

def is_video_file(filename: str) -> bool:
    return filename.lower().endswith(VIDEO_EXTENSIONS)
