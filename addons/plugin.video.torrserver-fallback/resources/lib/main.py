#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TorrServer Fallback v2.6.0 — Kodi addon
Real-Debrid fails → Jackett search → WATCH NOW (TorrServer) or DOWNLOAD (qBittorrent)
Six entry points: Videos menu, context menu, auto-fallback RunPlugin, service trigger,
right-click context, and standalone search.
"""
from __future__ import annotations

import sys, os, re, xbmc, xbmcplugin, xbmcaddon, xbmcgui, xbmcaddon
from urllib.parse import urlencode, urljoin, parse_qs, quote
import xbmcvfs, traceback, json

try:
    import requests
    REQUESTS_OK = True
except Exception:
    REQUESTS_OK = False

# ── helpers ──────────────────────────────────────────────────────────────────

def cfg(key: str, default=""):
    try:
        return ADDON.getSetting(key).strip() or default
    except Exception:
        return default

def d0() -> str:
    return cfg("d0_host", "159.65.10.49")

def ts_url(path="") -> str:
    port = cfg("ts_port", "8092")
    user = cfg("ts_user")
    pw = cfg("ts_pass")
    host = d0()
    if user and pw:
        return f"http://{user}:{pw}@{host}:{port}{path}"
    return f"http://{host}:{port}{path}"

def qb_url() -> str:
    return cfg("qb_url", "http://127.0.0.1:9090")

def qb_auth() -> tuple:
    return cfg("qb_user", "admin"), cfg("qb_pass", "d0t0rr3nt2026")

def jackett_headers() -> dict:
    return {"apikey": cfg("jackett_api")}

BASE_URL = "plugin://plugin.video.torrserver-fallback/"
HANDLE = -1

# ── Kodi-compatible requests (no dependency on external requests lib) ────────

def _kodi_urlopen(url, data=None, headers=None):
    """Use xbmcvfs or urllib on Python stdlib — safe for all Kodi versions."""
    import urllib.request, urllib.parse, urllib.error
    if headers is None:
        headers = {}
    if data is not None:
        encoded_data = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data
    else:
        encoded_data = None
    req = urllib.request.Request(url, data=encoded_data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        xbmc.log(f"[TSFB] urlopen error {url}: {e}", xbmc.LOGWARNING)
        return ""

def ts_get(path) -> dict:
    if REQUESTS_OK:
        try:
            r = requests.get(ts_url(path), timeout=15)
            return r.json()
        except Exception:
            pass
    data = _kodi_urlopen(ts_url(path))
    if data:
        try:
            return json.loads(data)
        except Exception:
            pass
    return {}

def ts_post(action, **kwargs) -> dict:
    payload = {"action": action}
    payload.update(kwargs)
    if REQUESTS_OK:
        try:
            r = requests.post(ts_url("/torrents"), json=payload, timeout=15)
            return r.json()
        except Exception:
            pass
    import urllib.request, urllib.parse
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(ts_url("/torrents"), data=data,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return {}

def ts_stream_url(magnet_or_url, file_index=0):
    """Return a plugin:// URL that plays via TorrServer stream endpoint."""
    import urllib.parse
    # Encode the source (magnet or .torrent URL)
    encoded = urllib.parse.quote(magnet_or_url, safe="")
    stream = ts_url(f"/stream/video?link={encoded}&index={file_index}&play&preload")
    return stream

# ── Jackett search ───────────────────────────────────────────────────────────

def jackett_search(title, media="movie", year="", season="", episode="") -> list:
    """
    Returns list of dicts: {name, guid, size, peers, seeds, torrent_url, magnet}
    """
    host = d0()
    api_key = cfg("jackett_api")
    if not api_key:
        notify("Jackett API key not set", 5000); return []

    t = "movie" if media == "movie" else "tvsearch"
    params = {"apikey": api_key, "t": t, "q": title}
    if year:
        params["year"] = year
    if media == "tv" and season:
        params["season"] = season
        params["ep"] = episode

    url = f"http://{host}:9117/api/v2.0/indexers/all/results/torznab/api?" + urlencode(params)
    if REQUESTS_OK:
        try:
            r = requests.get(url, timeout=30, headers={"Accept": "application/json"})
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            xbmc.log(f"[TSFB] Jackett error: {e}", xbmc.LOGWARNING)
            return []
    else:
        raw = _kodi_urlopen(url)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except Exception:
            return []

    results = []
    items = (data.get("channel", {}) or {}).get("item", [])
    if not isinstance(items, list):
        items = [items]

    for item in items:
        try:
            guid = (item.get("guid") or {}).get("#text", "") if isinstance(item.get("guid"), dict) else str(item.get("guid", ""))
            size_str = str(item.get("size", 0))
            size = int(size_str) if size_str.isdigit() else 0

            enclosure = item.get("enclosure") or {}
            torrent_url = enclosure.get("url", "") if isinstance(enclosure, dict) else ""

            # Extract magnet from guid or torznab:stream
            magnet = ""
            if "magnet:" in guid.lower():
                magnet = guid
            elif "magnet:" in str(item.get("description", "")).lower():
                magnet = re.search(r"magnet:\?[^<\s]+", str(item.get("description", "")), re.I).group(0) if re.search(r"magnet:\?[^<\s]+", str(item.get("description", "")), re.I) else ""

            # Jackett provides .torrent URL via enclosure type application/x-bittorrent
            if not torrent_url:
                for link in (item.get("link") or []):
                    if isinstance(link, dict) and "application/x-bittorrent" in str(link.get("type", "")):
                        torrent_url = str(link.get("url", ""))
                        break

            seeds = 0; peers = 0
            for attr in (item.get("torznab:attr") or []):
                if isinstance(attr, dict):
                    name = str(attr.get("name", ""))
                    val = int(str(attr.get("value", 0)))
                    if name == "seeders": seeds = val
                    if name == "peers": peers = val

            if not torrent_url and not magnet:
                continue

            results.append({
                "name": str(item.get("title", "")),
                "guid": guid,
                "size": size,
                "seeds": seeds,
                "peers": peers,
                "torrent_url": torrent_url,
                "magnet": magnet,
            })
        except Exception as e:
            xbmc.log(f"[TSFB] Jackett item parse error: {e}", xbmc.LOGDEBUG)
            continue

    # Sort by seeds descending
    results.sort(key=lambda x: x.get("seeds", 0), reverse=True)
    return results

# ── Quality picking ─────────────────────────────────────────────────────────

QUALITY_PATTERNS = [
    ("4K",      re.compile(r"\b(2160p|4K|UHD)\b", re.I)),
    ("1080p",   re.compile(r"\b(1080p|FHD)\b", re.I)),
    ("720p",    re.compile(r"\b(720p|HD\b)\b", re.I)),
    ("480p",    re.compile(r"\b(480p|SD\b)\b", re.I)),
    ("CAM",     re.compile(r"\b(CAM|TS|HDTC|HDTS)\b", re.I)),
]

EXCLUDE_PATTERNS = re.compile(
    r"\b(sample|proof|trailer|extra|bonus|scenes?|deleted|making|behind)\b",
    re.I
)

def pick_best(files: list) -> str:
    """
    Pick best video file from a list of {path, size} dicts.
    Returns the path of the best file, or empty string.
    """
    if not files:
        return ""
    video_files = []
    for f in files:
        path = f.get("path", "")
        if EXCLUDE_PATTERNS.search(path):
            continue
        # Skip playlists and non-video
        if re.search(r"\.(m3u8|pls|jpg|png|nfo|srt|sub|idx)\b", path, re.I):
            continue
        # Score by quality
        score = 0
        for rank, (label, pattern) in enumerate(QUALITY_PATTERNS):
            if pattern.search(path):
                score = len(QUALITY_PATTERNS) - rank
                break
        # Prefer larger files within same quality
        size = int(f.get("length", 0) or 0)
        video_files.append((~score, -size, path))

    if not video_files:
        return ""
    video_files.sort()
    return video_files[0][2]

# ── TorrServer operations ───────────────────────────────────────────────────

def ts_add(link: str, title: str = "", save=True) -> str:
    """Add a torrent/magnet to TorrServer. Returns hash or ''."""
    data = ts_post("add", link=link, title=title, save_to_db=save)
    if isinstance(data, dict):
        return str(data.get("hash", "") or data.get("result", ""))
    return str(data) if data else ""

def ts_files(hash_id: str) -> list:
    data = ts_post("get", hash=hash_id)
    if isinstance(data, dict):
        stats = data.get("file_stats", []) or data.get("torrent", {}).get("file_stats", [])
        return [{"id": f.get("id", i), "path": f.get("path", ""), "size": f.get("length", 0)}
                for i, f in enumerate(stats)]
    return []

def ts_stream_torrent(torrent_url: str, title: str) -> str:
    """Add .torrent URL to TorrServer and return stream URL."""
    # First try as direct .torrent URL
    if torrent_url.startswith("http") and ".torrent" in torrent_url:
        # Download .torrent content
        try:
            if REQUESTS_OK:
                r = requests.get(torrent_url, timeout=20)
                files = {"file": ("torrent", r.content, "application/x-bittorrent")}
                resp = requests.post(ts_url("/torrent/upload"), files=files, timeout=30)
                if resp.status_code == 200:
                    result = resp.json()
                    if result:
                        hash_id = str(result.get("hash", "") or result.get("infohash", "") or result)
                        if hash_id:
                            fs = ts_files(hash_id)
                            best = pick_best([{"id": f["id"], "path": f["path"], "size": f.get("size", 0)} for f in fs])
                            if not best and fs:
                                best = str(fs[0]["id"])
                            return ts_stream_url(f"magnet:?xt=urn:btih:{hash_id}", int(best) if best.isdigit() else 0)
            else:
                raw = _kodi_urlopen(torrent_url, headers={"Accept": "application/x-bittorrent"})
                if raw:
                    import urllib.request
                    data_bytes = raw.encode() if isinstance(raw, str) else raw
                    import urllib.parse
                    boundary = "----FormBoundary" + os.urandom(8).hex()
                    body = (f"--{boundary}\r\n"
                            f"Content-Disposition: form-data; name=\"file\"; filename=\"torrent\"\r\n"
                            f"Content-Type: application/x-bittorrent\r\n\r\n").encode()
                    body += data_bytes + f"\r\n--{boundary}--\r\n".encode()
                    req = urllib.request.Request(
                        ts_url("/torrent/upload"),
                        data=body,
                        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
                    )
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        result = json.loads(resp.read().decode())
                        if result:
                            hash_id = str(result.get("hash", "") or result)
                            if hash_id and len(hash_id) == 40:
                                fs = ts_files(hash_id)
                                best_id = fs[0]["id"] if fs else 0
                                return ts_stream_url(f"magnet:?xt=urn:btih:{hash_id}", best_id)
        except Exception as e:
            xbmc.log(f"[TSFB] torrent upload error: {e}", xbmc.LOGWARNING)

    # Fallback: add as magnet
    hash_id = ts_add(torrent_url, title=title)
    if hash_id and len(hash_id) == 40:
        fs = ts_files(hash_id)
        best_id = fs[0]["id"] if fs else 0
        return ts_stream_url(f"magnet:?xt=urn:btih:{hash_id}", best_id)
    return ""

# ── qBittorrent operations ──────────────────────────────────────────────────

def qb_api(endpoint, data=None, method="GET"):
    user, pw = qb_auth()
    if not user or not pw:
        return {} if method == "GET" else False
    import base64
    creds = f"{user}:{pw}"
    token = base64.b64encode(creds.encode()).decode()
    headers = {"Authorization": f"Basic {token}",
               "Content-Type": "application/x-www-form-urlencoded"}
    url = qb_url() + endpoint
    try:
        if method == "GET":
            r = requests.get(url, headers=headers, timeout=15) if REQUESTS_OK else None
            if r:
                return r.json()
        else:
            r = requests.post(url, data=data, headers=headers, timeout=15) if REQUESTS_OK else None
            if r and r.status_code in (200, 201):
                return True
    except Exception as e:
        xbmc.log(f"[TSFB] qB API error {endpoint}: {e}", xbmc.LOGWARNING)
    return {} if method == "GET" else False

def qb_check_free_space(torrent_size_bytes: int) -> bool:
    """Return True if volume has room for the torrent."""
    try:
        data = qb_api("/api/v2/sync/maindata?rid=0")
        free = data.get("server_state", {}).get("free_space_on_disk", 0)
        # Require at least 2GB buffer
        return int(free or 0) >= (torrent_size_bytes + 2 * 1024**3)
    except Exception:
        return True  # allow on error

def qb_add_torrent(torrent_url: str, title: str) -> bool:
    """Add a .torrent URL to qBittorrent via API."""
    user, pw = qb_auth()
    if not user or not pw:
        notify("qBittorrent credentials not set", 5000)
        return False
    import base64
    creds = f"{user}:{pw}"
    token = base64.b64encode(creds.encode()).decode()
    headers = {"Authorization": f"Basic {token}"}

    # Determine if it's a magnet or http .torrent URL
    is_magnet = torrent_url.startswith("magnet:")
    is_http = torrent_url.startswith("http") and ".torrent" in torrent_url

    if is_magnet:
        # qBittorrent magnet add
        form = {"urls": torrent_url}
    elif is_http:
        # Download .torrent file and add as multipart
        try:
            if REQUESTS_OK:
                r = requests.get(torrent_url, timeout=20)
                files = {"torrentfile": ("torrent", r.content, "application/x-bittorrent")}
                resp = requests.post(
                    qb_url() + "/api/v2/torrents/add",
                    files=files,
                    data={"savepath": cfg("library_path", "/mnt/media/completed")},
                    headers={"Authorization": f"Basic {token}"},
                    timeout=30
                )
                if resp.status_code in (200, 201) and "Ok." in resp.text:
                    return True
            else:
                raw = _kodi_urlopen(torrent_url, headers={"Accept": "application/x-bittorrent"})
                if raw:
                    import urllib.request
                    data_bytes = raw.encode() if isinstance(raw, str) else raw
                    boundary = "----qbtorrent" + os.urandom(8).hex()
                    body = (f"--{boundary}\r\n"
                            f"Content-Disposition: form-data; name=\"torrentfile\"; filename=\"torrent\"\r\n"
                            f"Content-Type: application/x-bittorrent\r\n\r\n").encode()
                    body += data_bytes + f"\r\n--{boundary}\r\n"
                    body += f"Content-Disposition: form-data; name=\"savepath\"\r\n\r\n{cfg('library_path', '/mnt/media/completed')}\r\n--{boundary}--\r\n"
                    req = urllib.request.Request(
                        qb_url() + "/api/v2/torrents/add",
                        data=body,
                        headers={"Authorization": f"Basic {token}",
                                 "Content-Type": f"multipart/form-data; boundary={boundary}"}
                    )
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        return "Ok." in resp.read().decode()
        except Exception as e:
            xbmc.log(f"[TSFB] qB torrent add error: {e}", xbmc.LOGWARNING)
        return False
    else:
        return False

    # Magnet via URL form
    try:
        if REQUESTS_OK:
            resp = requests.post(
                qb_url() + "/api/v2/torrents/add",
                data=form,
                headers={"Authorization": f"Basic {token}", "Content-Type": "application/x-www-form-urlencoded"},
                timeout=15
            )
            return resp.status_code in (200, 201) and "Ok." in resp.text
    except Exception as e:
        xbmc.log(f"[TSFB] qB magnet add error: {e}", xbmc.LOGWARNING)
    return False

def qb_status() -> str:
    try:
        data = qb_api("/api/v2/sync/maindata?rid=0")
        dl = data.get("server_state", {}).get("dl_info_speed", 0)
        up = data.get("server_state", {}).get("up_info_speed", 0)
        return f"↓ {dl/1024:.0f}KB/s  ↑ {up/1024:.0f}KB/s"
    except Exception:
        return "qB offline"

# ── UI helpers ───────────────────────────────────────────────────────────────

def notify(msg: str, ms: int = 3000, icon=ADDON.getAddonInfo("icon")):
    xbmc.executebuiltin(f"Notification({ADDON.getAddonInfo('name')},{msg},{ms},{icon})")

def dialog_choice(options: list, heading="TorrServer Fallback") -> int:
    d = xbmcgui.Dialog()
    return d.select(heading, options)

# ── Router ──────────────────────────────────────────────────────────────────

def router(qs: str):
    """Parse ?key=val&... query string and dispatch."""
    global HANDLE
    params = {}
    if qs:
        for k, v in parse_qs(qs).items():
            params[k] = v[0] if v else ""
    action = params.get("action", "")
    if not action:
        list_router()
    elif action == "search":
        search_dialog()
    elif action == "list":
        browse_library()
    elif action == "play":
        smart_play(params.get("link", ""), params.get("title", "TorrServer"),
                   params.get("turl", ""), media=params.get("media", "movie"))
    elif action == "context_search":
        context_search()
    elif action == "context_gdrive":
        context_gdrive()
    elif action == "gdrive_browse":
        gdrive_browse(params.get("path", ""))
    elif action == "qb_status":
        notify(qb_status(), 8000)
    elif action == "auto_fallback":
        run_search_and_play(
            params.get("title", ""),
            params.get("media", "movie"),
            year=params.get("year", ""),
            season=params.get("season", ""),
            episode=params.get("episode", "")
        )
    else:
        list_router()

# ── Entry points ─────────────────────────────────────────────────────────────

def list_router():
    """Videos → Browse Library / Search / qB Status / Settings."""
    items = [
        (ADDON.getLocalizedString(30001) or "Search Jackett",  "search",          ""),
        (ADDON.getLocalizedString(30002) or "Browse Library",    "list",             ""),
        (ADDON.getLocalizedString(30003) or "qBittorrent",     "qb_status",        ""),
    ]
    for label, action, _ in items:
        url = f"{BASE_URL}?action={action}"
        li = xbmcgui.ListItem(label)
        li.setInfo("video", {"title": label})
        xbmcplugin.addDirectoryItem(HANDLE, url, li, False)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)

def search_dialog():
    """Show a keyboard input, then run Jackett + show results."""
    d = xbmcgui.Dialog()
    title = d.input(ADDON.getLocalizedString(30004) or "Search title",
                    type=xbmcgui.INPUT_ALPHANUM)
    if not title:
        return
    media_types = ["movie", "tv"]
    sel = dialog_choice(media_types, "Media type")
    if sel < 0:
        return
    media = media_types[sel]
    year = ""
    if media == "movie":
        y = d.input("Year (optional)", type=xbmcgui.INPUT_NUMERIC)
        year = y if y and len(y) == 4 else ""
    season = ""; episode = ""
    if media == "tv":
        season = d.input("Season", type=xbmcgui.INPUT_NUMERIC)
        episode = d.input("Episode", type=xbmcgui.INPUT_NUMERIC)
    results = jackett_search(title, media=media, year=year, season=season, episode=episode)
    show_results(results, title, media=media, year=year, season=season, episode=episode)

def show_results(results: list, title: str, media="movie", year="", season="", episode=""):
    """Display Jackett results with Watch / Download options."""
    if not results:
        notify("No results found", 4000)
        return
    for item in results:
        seeds = item.get("seeds", 0)
        peers = item.get("peers", 0)
        size_mb = item.get("size", 0) // (1024 * 1024)
        label = f"{item['name']}  [{seeds}⚇ {peers}○]  {size_mb}MB"
        turl = item.get("torrent_url", "")
        magnet = item.get("magnet", "")
        source = magnet if magnet else turl
        if not source:
            continue
        params = urlencode({
            "action": "play", "title": item["name"], "link": source,
            "turl": turl, "media": media
        })
        play_url = f"{BASE_URL}?{params}"
        dl_params = urlencode({
            "action": "download", "title": item["name"],
            "link": source, "turl": turl, "media": media
        })
        dl_url = f"{BASE_URL}?{dl_params}"
        li = xbmcgui.ListItem(label)
        li.setInfo("video", {"title": item["name"], "size": item.get("size", 0)})
        li.addContextMenuItems([
            ("Watch Now", f"RunPlugin({play_url})"),
            ("Download",  f"RunPlugin({dl_url})"),
        ])
        xbmcplugin.addDirectoryItem(HANDLE, play_url, li, False)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)

def smart_play(link: str, title: str, turl: str = "", media="movie"):
    """
    Try WATCH NOW via TorrServer. If link is http .torrent, use ts_stream_torrent.
    Falls back to magnet/btih hash path.  Sets resolved URL or uses Player().play().
    """
    if not link:
        notify("No source available", 4000)
        return

    # If we have a .torrent URL and requests available, try direct stream path
    if turl and turl.startswith("http") and ".torrent" in turl:
        stream_url = ts_stream_torrent(turl, title)
        if stream_url:
            play_url = stream_url
        else:
            # Fall back to magnet
            play_url = ts_stream_url(link) if link.startswith("magnet") else ts_stream_url(turl)
    elif link.startswith("magnet"):
        play_url = ts_stream_url(link)
    elif link.startswith("http"):
        play_url = ts_stream_torrent(link, title)
        if not play_url:
            notify("TorrServer: could not add torrent", 4000)
            return
    else:
        play_url = ts_stream_url(link)

    if not play_url:
        notify("No stream URL", 4000)
        return

    # Play via setResolvedUrl if handle is valid, else via Player().play()
    if HANDLE > 0:
        li = xbmcgui.ListItem(title)
        li.setInfo("video", {"title": title})
        li.setMimeType("application/x-mpegURL" if ".m3u8" in play_url else "video/mp4")
        xbmcplugin.setResolvedUrl(HANDLE, True, li)
    else:
        # Background or context menu: use Player directly
        player = xbmc.Player()
        player.play(play_url)

def context_search():
    """Context menu: search for selected item's title."""
    try:
        # Get info from current list item
        label = ""
        try:
            label = xbmc.getInfoLabel("ListItem.Title")
        except Exception:
            pass
        if not label:
            label = xbmc.getInfoLabel("ListItem.Label")
        if not label:
            notify("No title found", 3000)
            return
        results = jackett_search(label)
        show_results(results, label)
    except Exception as e:
        xbmc.log(f"[TSFB] context_search error: {e}", xbmc.LOGERROR)
        notify("Search failed", 3000)

def context_gdrive():
    """Context menu: try GDrive lookup for selected title."""
    try:
        label = ""
        try:
            label = xbmc.getInfoLabel("ListItem.Title") or xbmc.getInfoLabel("ListItem.Label")
        except Exception:
            pass
        if not label:
            notify("No title", 3000)
            return
        notify(f"GDrive: {label[:30]}...", 3000)
        # Placeholder — GDrive integration ready for future use
        notify("GDrive: not yet configured", 4000)
    except Exception:
        notify("GDrive lookup failed", 3000)

def browse_library():
    """Browse completed downloads from /mnt/media/completed via rclone serve."""
    library_path = cfg("library_path", "/mnt/media/completed")
    host = d0()
    rclone_url = f"http://{host}:8093/"
    notify(f"Library: {library_path}", 3000)
    # Show the rclone serve URL as a directory listing
    li = xbmcgui.ListItem("Open Library (rclone serve)")
    li.setInfo("video", {"title": "Library"})
    xbmcplugin.addDirectoryItem(HANDLE, rclone_url, li, False)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)

def gdrive_browse(path: str = ""):
    """Browse GDrive remote via rclone serve."""
    host = d0()
    base = f"http://{host}:8093/"
    url = base + ("gdrive:" + path if path else "gdrive:")
    li = xbmcgui.ListItem("GDrive: " + (path or "root"))
    xbmcplugin.addDirectoryItem(HANDLE, url, li, False)
    xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)

# ── Auto-fallback (triggered by service.py) ──────────────────────────────────

def run_search_and_play(title: str, media="movie", year="", season="", episode=""):
    """Called by service when RD failure detected. Shows Yes/No, then searches."""
    dialog = xbmcgui.Dialog()
    ret = dialog.yesno(
        "TorrServer Fallback",
        f"Real-Debrid found no sources.\nSearch Jackett for [B]{title}[/B]?",
        nolabel="Cancel", yeslabel="Search Now"
    )
    if not ret:
        return
    results = jackett_search(title, media=media, year=year, season=season, episode=episode)
    if not results:
        notify("No Jackett results", 4000)
        return
    # Auto-play best result
    best = results[0]
    link = best.get("torrent_url", "") or best.get("magnet", "")
    turl = best.get("torrent_url", "")
    smart_play(link, best["name"], turl, media=media)

# ── Main ─────────────────────────────────────────────────────────────────────

ADDON = xbmcaddon.Addon()

if __name__ == "__main__":
    argv = sys.argv
    if len(argv) >= 3 and argv[2].startswith("?"):
        # Standard Kodi call: argv[1]=handle, argv[2]="?key=val&..."
        try:
            HANDLE = int(argv[1])
        except Exception:
            HANDLE = -1
        router(argv[2][1:])          # strip leading "?"
    elif len(argv) >= 2 and not argv[1].lstrip("-").isdigit():
        # Backward compat: RunPlugin called with action=... style
        HANDLE = -1
        router(f"action={argv[1]}")
    else:
        # Fallback: list
        HANDLE = int(argv[1]) if len(argv) > 1 else -1
        list_router()
