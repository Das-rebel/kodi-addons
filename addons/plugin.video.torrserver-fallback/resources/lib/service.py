#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TorrServer Fallback — Kodi service (startup)
Monitors Kodi's GUI state to detect Real-Debrid failure.
Uses TRANSITION-EDGE detection (rising/falling) instead of interval math.
Flow: busy_dialog_START + Fen + >0.8s → arm → busy_dialog_END → fire.
"""
from __future__ import annotations

import xbmc, xbmcplugin, xbmcgui, xbmcaddon, time
from urllib.parse import urlencode, parse_qs

# ── Config ───────────────────────────────────────────────────────────────────

DETECTION_DELAY = 3.0   # seconds to wait after busy dialog closes before firing
POPUP_TIMEOUT  = 20    # seconds before auto-dismiss
GRACE_ARM      = 2.5    # seconds after busy closes to still consider it a "failure"
MIN_BUSY       = 0.8    # minimum busy duration to count as real scrape

ADDON = xbmcaddon.Addon()
BASE  = "plugin://plugin.video.torrserver-fallback/"

def cfg(key, default=""):
    try:
        return ADDON.getSetting(key).strip() or default
    except Exception:
        return default

def d0() -> str:
    return cfg("d0_host", "159.65.10.49")

# ── Monitor class ────────────────────────────────────────────────────────────

class RDDetector(xbmc.Monitor):
    def __init__(self):
        super().__init__()
        self.captured   = None   # metadata from the rising edge
        self.busy_seen_at = 0.0
        self.arm_until  = 0.0   # 0 = not armed
        self.last_fire  = -999.0  # very old → first fire always allowed
        self.was_busy   = False # previous iteration state

    def onNotification(self, sender, method, data):
        # Could be used for direct Kodi events if needed
        pass

    def arm(self, now: float, busy_duration: float):
        """Arm the fallback trigger once the busy dialog closes."""
        if busy_duration >= MIN_BUSY:
            self.arm_until = now + GRACE_ARM
            xbmc.log(f"[TSFB] RD fallback armed (busy={busy_duration:.1f}s)", xbmc.LOGINFO)
        else:
            xbmc.log(f"[TSFB] Busy too short ({busy_duration:.1f}s) — skipped", xbmc.LOGDEBUG)
            self.captured = None

    def should_fire(self, now: float) -> bool:
        if not self.arm_until or now < self.arm_until:
            return False
        if now - self.last_fire < 60:
            xbmc.log("[TSFB] Rate-limited", xbmc.LOGDEBUG)
            return False
        # Must NOT be playing and should be in Fen
        try:
            if xbmc.Player().isPlaying():
                xbmc.log("[TSFB] Player active — skip", xbmc.LOGDEBUG)
                return False
        except Exception:
            pass
        if not self._in_fen():
            xbmc.log("[TSFB] Not in Fen — skip", xbmc.LOGDEBUG)
            return False
        # Skip if sources window is visible (RD success browsing)
        if self._sources_visible():
            xbmc.log("[TSFB] Sources window visible — skip", xbmc.LOGDEBUG)
            return False
        return True

    def _in_fen(self) -> bool:
        try:
            win = xbmcgui.Window(xbmcgui.getCurrentWindowId())
            pn = win.getProperty("Container.PluginName") or ""
            return "fenlight" in pn.lower() or "plugin.video.fenlight" in pn.lower()
        except Exception:
            pass
        try:
            pn = xbmc.getInfoLabel("Container.PluginName")
            return "fenlight" in pn.lower() or "plugin.video.fenlight" in pn.lower()
        except Exception:
            return False

    def _sources_visible(self) -> bool:
        """Return True if Fen's sources/results window is displayed."""
        # Fen Light sources window window ID (verified against Fen source)
        try:
            return xbmcgui.Window(10502).isActive()
        except Exception:
            pass
        try:
            # Fallback: check window labels
            return xbmc.getCondVisibility("Window.IsActive(10502)")
        except Exception:
            return False

    def capture_meta(self) -> dict:
        """Grab title/year/media_type from the current list item."""
        try:
            title = xbmc.getInfoLabel("ListItem.Title") or xbmc.getInfoLabel("ListItem.Label") or ""
            # Try tvshowtitle for series
            label2 = xbmc.getInfoLabel("ListItem.TVShowTitle") or ""
            if label2:
                title = label2
            year  = xbmc.getInfoLabel("ListItem.Year") or ""
            mtype = xbmc.getInfoLabel("ListItem.DBType") or ""
            # Determine media type
            media = "movie"
            if mtype in ("episode", "tvshow", "season"):
                media = "tv"
            # Season / episode
            season  = xbmc.getInfoLabel("ListItem.Season") or ""
            episode = xbmc.getInfoLabel("ListItem.Episode") or ""
            return {
                "title":   title.strip(),
                "year":    year.strip(),
                "media":   media,
                "season":  season.strip(),
                "episode": episode.strip(),
            }
        except Exception as e:
            xbmc.log(f"[TSFB] capture_meta error: {e}", xbmc.LOGWARNING)
            return {"title": "", "year": "", "media": "movie", "season": "", "episode": ""}

    def show_popup(self, now: float):
        """Show Yes/No dialog and launch fallback if confirmed."""
        meta = self.captured
        if not meta or not meta.get("title"):
            xbmc.log("[TSFB] No metadata captured", xbmc.LOGWARNING)
            return
        title = meta["title"]
        line2 = f"Search Jackett for [B]{title[:50]}[/B]?"
        dialog = xbmcgui.Dialog()
        ret = dialog.yesno(
            "TorrServer Fallback",
            f"Real-Debrid found no sources.\n{line2}",
            nolabel="Cancel", yeslabel="Search Now",
            autoclose=POPUP_TIMEOUT * 1000
        )
        if ret:
            params = urlencode({
                "action":  "auto_fallback",
                "title":   title,
                "year":    meta.get("year", ""),
                "media":   meta.get("media", "movie"),
                "season":  meta.get("season", ""),
                "episode": meta.get("episode", ""),
            })
            url = f"{BASE}?{params}"
            xbmc.log(f"[TSFB] Launching fallback: {url}", xbmc.LOGINFO)
            xbmc.executebuiltin(f"RunPlugin({url})")
        self.last_fire = now


# ── Busy-dialog polling (transition-edge detection) ──────────────────────────

def busy_active() -> bool:
    """Return True if the Fen busy dialog (including nocancel variant) is showing."""
    try:
        # check if nocancel busy is showing (Fen uses busydialognocancel for scraping)
        if xbmc.getCondVisibility("Window.IsActive(busydialognocancel)"):
            return True
        # also check regular busy dialog
        if xbmc.getCondVisibility("Window.IsActive(busydialog)"):
            return True
        # programmatic check via busydialog state
        state = xbmc.getInfoLabel("System.BusyDialog")
        return state.lower() in ("true", "1", "busy")
    except Exception:
        return False

def run_loop(mon: RDDetector):
    """Main polling loop using TRANSITION-EDGE detection."""
    xbmc.log("[TSFB] Service started — watching for RD failures", xbmc.LOGINFO)
    was_busy = False

    while not mon.abortRequested():
        now = time.time()
        is_busy = busy_active() and mon._in_fen()

        # ── RISING EDGE: busy dialog appeared ──────────────────────────────────
        if is_busy and not was_busy:
            if mon.captured is None:          # only capture if not already captured
                meta = mon.capture_meta()
                if meta.get("title"):
                    mon.captured = meta
                    mon.busy_seen_at = now
                    xbmc.log(f"[TSFB] Captured: {meta['title']}", xbmc.LOGDEBUG)
            else:
                xbmc.log("[TSFB] Already captured — ignoring new busy", xbmc.LOGDEBUG)

        # ── FALLING EDGE: busy dialog closed ──────────────────────────────────
        elif was_busy and not is_busy:
            duration = now - mon.busy_seen_at
            xbmc.log(f"[TSFB] Busy ended after {duration:.1f}s", xbmc.LOGDEBUG)
            mon.arm(now, duration)

        # ── Check fire condition ──────────────────────────────────────────────
        if mon.arm_until and now >= mon.arm_until:
            if mon.should_fire(now):
                mon.show_popup(now)
            mon.captured  = None
            mon.arm_until  = 0.0

        was_busy = is_busy
        mon.waitForAbort(0.5)

    xbmc.log("[TSFB] Service stopped", xbmc.LOGINFO)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mon = RDDetector()
    run_loop(mon)
