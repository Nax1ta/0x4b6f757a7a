"""Shared observation, session and delivery state. No mailbox credentials in clients."""
from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"
CN = timezone(timedelta(hours=8))


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_env(path):
    if Path(path).exists():
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def api_json(url, timeout=8):
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://live.bilibili.com/", "Accept": "application/json"})
    # Bypass Windows system proxies for domestic platform APIs. A stale/global
    # proxy otherwise turns valid rooms into TLS errors on the desktop.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError("HTTP %d" % response.status)
        return json.loads(response.read(4_000_000))


def live_time(value):
    try:
        if isinstance(value, (float, int)) or str(value).isdigit():
            ts = int(value)
            return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts > 1_000_000_000 else None
        value = str(value or "").strip()
        if value.startswith("0000") or not value:
            return None
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=parsed.tzinfo or CN).astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def fetch_room(rid, name=""):
    """Validate status explicitly; absence/errors never mean offline."""
    rid = str(rid)
    if not rid.isdigit() or not 1 <= len(rid) <= 20:
        raise ValueError("房间号须为 1–20 位数字")
    base = "https://api.live.bilibili.com"
    errors = []
    for endpoint, parameter, nested in [
        ("/room/v1/Room/get_info", "room_id", False),
        ("/xlive/web-room/v1/index/getInfoByRoom", "room_id", True),
        ("/room/v1/Room/room_init", "id", False),
    ]:
        try:
            data = api_json(base + endpoint + "?" + urllib.parse.urlencode({parameter: rid}))
            if data.get("code") != 0:
                raise RuntimeError("API code %s" % data.get("code"))
            info = (data.get("data") or {}).get("room_info") if nested else data.get("data")
            if not isinstance(info, dict) or type(info.get("live_status")) is not int or info["live_status"] not in (0, 1, 2):
                raise RuntimeError("缺少有效直播状态")
            status = "live" if info["live_status"] == 1 else "offline"
            started = live_time(info.get("live_start_time") or info.get("live_time"))
            return {"id": rid, "name": name or rid, "title": info.get("title") or "",
                    "cover": info.get("user_cover") or info.get("cover") or info.get("keyframe") or "",
                    "url": "https://live.bilibili.com/" + rid, "status": status,
                    "live_started_at": started if status == "live" else None,
                    "last_live_at": started, "checked_at": now_iso(), "error": None,
                    "source": endpoint, "metadata_partial": not bool(info.get("title"))}
        except Exception as exc:
            errors.append(type(exc).__name__ + ": " + str(exc)[:180])
    raise RuntimeError("；".join(errors))


def observe(rooms):
    result = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, max(1, len(rooms)))) as pool:
        futures = {pool.submit(fetch_room, r["id"], r.get("name", "")): r for r in rooms}
        for future in concurrent.futures.as_completed(futures):
            room = futures[future]
            try:
                result.append(future.result())
            except Exception as exc:
                result.append({"id": str(room["id"]), "name": room.get("name") or str(room["id"]),
                               "status": "unknown", "checked_at": now_iso(), "error": str(exc),
                               "url": "https://live.bilibili.com/" + str(room["id"])})
    order = {str(r["id"]): i for i, r in enumerate(rooms)}
    return sorted(result, key=lambda r: order[r["id"]])


def apply_observation(previous, current):
    """Keep unknown separate; a new start time detects restarts missed between polls."""
    result = dict(previous)
    if current["status"] == "unknown":
        result.update({"id": current["id"], "name": current.get("name") or previous.get("name", current["id"]),
                       "url": current.get("url"), "status": "unknown", "error": current["error"],
                       "attempted_at": current["checked_at"]})
        return result
    result.update(current)
    result["last_success_at"] = current["checked_at"]
    result["last_live_at"] = current.get("last_live_at") or previous.get("last_live_at")
    if not current.get("title"):
        result["title"] = previous.get("title", "")
    if not current.get("cover"):
        result["cover"] = previous.get("cover", "")
    if current["status"] == "live":
        started = current.get("live_started_at")
        if started:
            result["session"] = "start:" + started
            result["last_live_at"] = started
            result["last_live_estimated"] = False
        elif previous.get("observed_live") and previous.get("session"):
            result["session"] = previous["session"]
        else:
            result["session"] = "observed:" + current["checked_at"]
            result["last_live_at"] = current["checked_at"]
            result["last_live_estimated"] = True
        # A later response may enrich an observed session with the actual start.
        if previous.get("observed_live") and str(previous.get("session", "")).startswith("observed:") and started:
            notified = dict(previous.get("notified") or {})
            for key in list(notified):
                prefix = previous["session"] + "|"
                if key.startswith(prefix):
                    notified[result["session"] + key[len(previous["session"]):]] = notified[key]
            result["notified"] = notified
    result["observed_live"] = current["status"] == "live"
    return result


def pending_recipients(room, recipients):
    if room.get("status") != "live":
        return []
    sent = room.get("notified") or {}
    return [r for r in recipients if room["session"] + "|" + r.casefold() not in sent]


def acknowledge(room, recipient, at=None):
    notified = room.setdefault("notified", {})
    notified[room["session"] + "|" + recipient.casefold()] = at or now_iso()
    # Keep bounded history without forgetting the current session.
    room["notified"] = dict(list(notified.items())[-128:])
    room["last_mail_at"] = at or now_iso()
    room.pop("mail_error", None)


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


class StateStore:
    def __init__(self, path, encrypted=False):
        self.path = Path(path)
        self.encrypted = encrypted

    def cipher(self):
        from cryptography.fernet import Fernet
        material = os.getenv("STATE_KEY") or (os.getenv("EMAIL_USER", "") + "\0" + os.getenv("EMAIL_PASSWORD", ""))
        if not os.getenv("STATE_KEY") and not os.getenv("EMAIL_PASSWORD"):
            raise RuntimeError("缺少状态加密密钥")
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(("state-v2\0" + material).encode()).digest()))

    def read(self):
        if not self.path.exists():
            return {"version": 2, "rooms": {}}
        data = self.path.read_bytes()
        if self.encrypted:
            # Never reset corrupt/unreadable state silently; that would duplicate delivery.
            data = self.cipher().decrypt(data)
        result = json.loads(data)
        if result.get("version") != 2 or not isinstance(result.get("rooms"), dict):
            raise RuntimeError("状态版本无效")
        return result

    def write(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(state, ensure_ascii=False).encode()
        if self.encrypted:
            data = self.cipher().encrypt(data)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, self.path)
