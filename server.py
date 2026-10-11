#!/usr/bin/env python3
"""ห้องคาราโอเกะ YouTube — เซิร์ฟเวอร์คิวกลาง

รัน:  python server.py          (พอร์ตเริ่มต้น 8000)
      python server.py 9000     (เลือกพอร์ตเอง)

จอหลัก:      http://localhost:<port>/
มือถือขอเพลง: http://<IP-ในวง-Wi-Fi>:<port>/request   (สแกน QR บนจอหลัก)

ใช้แค่ Python มาตรฐาน ไม่ต้องติดตั้งอะไรเพิ่ม
"""
import json
import os
import random
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, "karaoke_data.json")
ID_RE = re.compile(r"^[\w-]{11}$")
LOCK = threading.Lock()

PAGES = {
    "/": "karaoke.html",
    "/index.html": "karaoke.html",
    "/karaoke.html": "karaoke.html",
    "/request": "request.html",
    "/r": "request.html",
    "/request.html": "request.html",
    "/tv": "tv.html",
    "/tv.html": "tv.html",
}
TV_TIMEOUT = 4  # seconds without a heartbeat before a TV screen counts as gone


# ---------------------------------------------------------------- network helpers
def _ssl_context():
    try:
        import certifi  # optional; fixes missing CA certs on some macOS Python installs
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL_CTX = _ssl_context()


def http_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (yt-karaoke)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"error": {"message": f"HTTP {e.code}"}}
    except ssl.SSLError as e:
        return {"error": {"message": "ใบรับรอง SSL ของ Python ไม่พร้อม (macOS: เปิด 'Install Certificates.command' ในโฟลเดอร์ Python)", "detail": str(e)}}
    except Exception as e:
        return {"error": {"message": f"เชื่อมต่ออินเทอร์เน็ตไม่ได้: {e}"}}


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def parse_id(text):
    text = (text or "").strip()
    if ID_RE.match(text):
        return text
    m = re.search(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/|/live/|/v/)([\w-]{11})", text)
    return m.group(1) if m else None


def parse_list(text):
    m = re.search(r"[?&]list=([\w-]+)", text or "")
    return m.group(1) if m else None


def fetch_meta(vid):
    watch = urllib.parse.quote(f"https://www.youtube.com/watch?v={vid}", safe="")
    for url in (f"https://www.youtube.com/oembed?format=json&url={watch}",
                f"https://noembed.com/embed?url={watch}"):
        j = http_json(url, timeout=6)
        if isinstance(j, dict) and j.get("title"):
            return j["title"], j.get("author_name", "")
    return f"วิดีโอ {vid}", ""


def fetch_playlist(list_id, key, limit=200):
    out, token = [], ""
    while len(out) < limit:
        q = urllib.parse.urlencode({"part": "snippet", "maxResults": 50, "playlistId": list_id,
                                    "key": key, **({"pageToken": token} if token else {})})
        j = http_json(f"https://www.googleapis.com/youtube/v3/playlistItems?{q}")
        if "error" in j:
            return out, api_error(j, "ดึงเพลย์ลิสต์ไม่ได้")
        for it in j.get("items", []):
            s = it.get("snippet", {})
            vid = (s.get("resourceId") or {}).get("videoId")
            if vid and s.get("title") not in ("Private video", "Deleted video"):
                out.append((vid, s.get("title", ""), s.get("videoOwnerChannelTitle", "")))
        token = j.get("nextPageToken")
        if not token:
            break
    return out, None


QUOTA_MSG = "โควตาค้นหา YouTube ของวันนี้หมดแล้ว (รีเซ็ตประมาณบ่ายสองโมง) ระหว่างนี้วางลิงก์ YouTube แทนได้ ไม่ใช้โควตา"


def api_error(j, fallback):
    msg = (j.get("error") or {}).get("message", fallback)
    return QUOTA_MSG if "quota" in msg.lower() else msg


def yt_search(q, key, max_results=15):
    params = urllib.parse.urlencode({"part": "snippet", "type": "video", "videoEmbeddable": "true",
                                     "maxResults": max_results, "q": q, "key": key})
    j = http_json(f"https://www.googleapis.com/youtube/v3/search?{params}")
    if "error" in j:
        return None, api_error(j, "ค้นหาไม่สำเร็จ")
    import html
    res = [{"id": it["id"]["videoId"],
            "title": html.unescape(it["snippet"]["title"]),
            "channel": html.unescape(it["snippet"]["channelTitle"])}
           for it in j.get("items", []) if it.get("id", {}).get("videoId")]
    return res, None


# ---------------------------------------------------------------- state
STATE = {"now": None, "queue": [], "history": [], "saved": {}, "apiKey": "", "autoNext": True, "partyName": "", "mode": "karaoke", "ver": 1}
MODES = ("karaoke", "music")  # karaoke: sing-along with countdowns; music: songs to listen to back to back
# live, not saved to disk: TV heartbeat, what the TV player is doing, remote commands for it
RUNTIME = {"tvSeen": 0.0, "player": {}, "cmd": {"id": 0, "cmd": None}}


def load_state():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            STATE.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        print("อ่านไฟล์ข้อมูลเดิมไม่ได้ เริ่มคิวใหม่:", e)


def save_state():
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STATE, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DATA_FILE)


def bump():
    STATE["ver"] += 1
    STATE["history"] = STATE["history"][:100]
    save_state()


def norm_list(v):
    """Saved playlists: {"type": "karaoke"|"music", "items": [...]}; older saves were a bare list (karaoke)."""
    if isinstance(v, list):
        return {"type": "karaoke", "items": [x for x in v if isinstance(x, dict)]}
    if isinstance(v, dict):
        t = v.get("type") if v.get("type") in MODES else "karaoke"
        return {"type": t, "items": [x for x in (v.get("items") or []) if isinstance(x, dict)]}
    return {"type": "karaoke", "items": []}


def tv_online():
    return time.time() - RUNTIME["tvSeen"] < TV_TIMEOUT


def public_state():
    online = tv_online()
    return {"now": STATE["now"], "queue": STATE["queue"], "history": STATE["history"][:50],
            "saved": {k: norm_list(v) for k, v in STATE["saved"].items()}, "hasKey": bool(STATE["apiKey"]), "autoNext": STATE.get("autoNext", True),
            "partyName": STATE.get("partyName", ""), "mode": STATE.get("mode", "karaoke"),
            "ver": STATE["ver"], "tvOnline": online, "player": RUNTIME["player"] if online else {},
            "cmd": RUNTIME["cmd"]}


def make_item(vid, title, channel, singer, by):
    return {"uid": uuid.uuid4().hex[:10], "id": vid, "title": (title or f"วิดีโอ {vid}")[:200],
            "channel": (channel or "")[:100], "singer": (singer or "")[:40], "by": (by or "")[:40],
            "at": int(time.time())}


def find(lst, uid):
    for i, it in enumerate(lst):
        if it["uid"] == uid:
            return i
    return -1


# ---------------------------------------------------------------- handlers
KARAOKE_RE = re.compile(r"karaoke|คาราโอเกะ", re.I)
MAX_NAME_SEARCHES = 50  # each name search costs 100 of the 10,000 daily YouTube API units


def resolve_text(text, kind, singer, client):
    """Turn pasted text into queue items. Each line is either YouTube links (videos or playlists)
    or a song name, which is searched and its top result taken (karaoke suffix for karaoke lists)."""
    items, errors, not_found = [], [], []
    key = STATE["apiKey"]
    searches = 0
    for line in str(text or "").splitlines()[:200]:
        line = line.strip()
        if not line:
            continue
        toks = [t for t in re.split(r"[\s,]+", line) if t]
        links = [t for t in toks if parse_id(t) or parse_list(t)]
        if links:
            for tok in links:
                vid, lid = parse_id(tok), parse_list(tok)
                if vid:
                    title, ch = fetch_meta(vid)
                    items.append(make_item(vid, title, ch, singer, client))
                elif not key:
                    errors.append("การเพิ่มทั้งเพลย์ลิสต์ต้องตั้ง YouTube API key ที่จอหลักก่อน")
                else:
                    found, err = fetch_playlist(lid, key)
                    if err:
                        errors.append(err)
                    items += [make_item(v, t, c, singer, client) for v, t, c in found]
            continue
        # a song name
        if not key:
            errors.append("การค้นจากชื่อเพลงต้องตั้ง YouTube API key ที่จอหลักก่อน (หรือวางเป็นลิงก์แทน)")
            not_found.append(line)
            continue
        if searches >= MAX_NAME_SEARCHES:
            errors.append(f"ค้นได้ครั้งละไม่เกิน {MAX_NAME_SEARCHES} ชื่อ ที่เหลือวางใหม่อีกรอบ")
            not_found.append(line)
            continue
        q = f"{line} คาราโอเกะ" if kind == "karaoke" and not KARAOKE_RE.search(line) else line
        res, err = yt_search(q, key, max_results=1)
        searches += 1
        if err:
            errors.append(err)
            not_found.append(line)
        elif res:
            items.append(make_item(res[0]["id"], res[0]["title"], res[0]["channel"], singer, client))
        else:
            not_found.append(line)
    return items, list(dict.fromkeys(errors)), not_found


def list_entry(it):
    return {"id": it["id"], "title": it.get("title", ""), "channel": it.get("channel", ""), "singer": it.get("singer", "")}


def handle_list(body):
    """Edit saved playlists directly, without touching the live queue."""
    if not body.get("host"):
        return {"error": "แก้ไขเพลย์ลิสต์ได้เฉพาะจอหลัก", "status": 403}
    op = body.get("op")
    name = str(body.get("name", "")).strip()[:60]
    kind = body.get("kind") if body.get("kind") in MODES else None
    saved = STATE["saved"]

    if op == "add":
        with LOCK:
            if name not in saved:
                return {"error": "ไม่พบเพลย์ลิสต์นี้", "status": 404}
            list_kind = norm_list(saved[name])["type"]
        items = [make_item(str(it.get("id")), it.get("title"), it.get("channel"), "", "host")
                 for it in (body.get("items") or [])[:50] if ID_RE.match(str(it.get("id", "")))]
        more, errors, not_found = resolve_text(body.get("text", ""), list_kind, "", "host")
        items += more
        with LOCK:
            if name not in saved:
                return {"error": "ไม่พบเพลย์ลิสต์นี้", "status": 404}
            pl = norm_list(saved[name])
            pl["items"].extend(list_entry(it) for it in items)
            saved[name] = pl
            if items:
                bump()
            return {"name": name, "added": len(items), "errors": errors, "notFound": not_found, "state": public_state()}

    with LOCK:
        if op == "create":
            if not name:
                return {"error": "ตั้งชื่อเพลย์ลิสต์ก่อน", "status": 400}
            if name in saved:
                return {"error": "มีเพลย์ลิสต์ชื่อนี้แล้ว ใช้ชื่ออื่น", "status": 400}
            saved[name] = {"type": kind or STATE.get("mode", "karaoke"), "items": []}
        else:
            if name not in saved:
                return {"error": "ไม่พบเพลย์ลิสต์นี้", "status": 404}
            pl = norm_list(saved[name])
            items = pl["items"]
            if op == "rename":
                new = str(body.get("newName", "")).strip()[:60]
                if not new:
                    return {"error": "ชื่อใหม่ว่างอยู่", "status": 400}
                if new != name and new in saved:
                    return {"error": "มีเพลย์ลิสต์ชื่อนี้แล้ว", "status": 400}
                del saved[name]
                name = new
            elif op == "setType":
                if not kind:
                    return {"error": "ไม่รู้จักประเภท", "status": 400}
                pl["type"] = kind
            elif op == "remove":
                i = int(body.get("index", -1))
                if 0 <= i < len(items):
                    items.pop(i)
            elif op == "move":
                i, to = int(body.get("index", -1)), int(body.get("to", -1))
                if 0 <= i < len(items):
                    to = max(0, min(to, len(items) - 1))
                    items.insert(to, items.pop(i))
            else:
                return {"error": "ไม่รู้จักคำสั่ง", "status": 400}
            saved[name] = pl
        bump()
        return {"name": name, "state": public_state()}


def handle_add(body):
    singer = str(body.get("singer", "")).strip()
    client = str(body.get("client", "")).strip()
    items, errors = [], []

    for it in (body.get("items") or [])[:50]:
        vid = str(it.get("id", ""))
        if ID_RE.match(vid):
            items.append(make_item(vid, it.get("title"), it.get("channel"), singer, client))

    more, errors, not_found = resolve_text(body.get("text", ""), STATE.get("mode", "karaoke"), singer, client)
    items += more

    positions = []
    with LOCK:
        if items:
            if body.get("next"):
                STATE["queue"][0:0] = items
            else:
                STATE["queue"].extend(items)
            if STATE["now"] is None:
                STATE["now"] = STATE["queue"].pop(0)
            bump()
        for it in items:
            if STATE["now"] and STATE["now"]["uid"] == it["uid"]:
                positions.append(0)
            else:
                positions.append(find(STATE["queue"], it["uid"]) + 1)
        if not items and not errors:
            errors.append("ไม่พบเพลง ลองพิมพ์ชื่อเพลงให้ชัดขึ้น หรือวางลิงก์ YouTube")
        return {"added": len(items), "positions": positions, "errors": errors, "notFound": not_found, "state": public_state()}


def handle_action(body):
    t = body.get("type")
    is_host = bool(body.get("host"))
    client = str(body.get("client", ""))
    uid = body.get("uid")
    q, h = STATE["queue"], STATE["history"]

    with LOCK:
        if t == "next":
            expect = body.get("expect")
            if expect and (not STATE["now"] or STATE["now"]["uid"] != expect):
                return {"state": public_state()}  # another screen already advanced
            if STATE["now"]:
                h.insert(0, STATE["now"])
            STATE["now"] = q.pop(0) if q else None

        elif t == "remove":
            i = find(q, uid)
            if i < 0:
                return {"state": public_state()}
            if not is_host and q[i].get("by") != client:
                return {"error": "ลบได้เฉพาะเพลงที่คุณขอเอง", "status": 403}
            q.pop(i)

        elif t == "setSinger":
            i = find(q, uid)
            if i < 0 or (not is_host and q[i].get("by") != client):
                return {"error": "แก้ไขไม่ได้", "status": 403}
            q[i]["singer"] = str(body.get("singer", ""))[:40]

        elif not is_host:
            return {"error": "คำสั่งนี้ใช้ได้เฉพาะจอหลัก", "status": 403}

        elif t == "prev":
            if not h:
                return {"state": public_state()}
            if STATE["now"]:
                q.insert(0, STATE["now"])
            STATE["now"] = h.pop(0)

        elif t == "playAt":
            i = find(q, uid)
            if i < 0:
                return {"state": public_state()}
            if STATE["now"]:
                h.insert(0, STATE["now"])
            STATE["now"] = q.pop(i)

        elif t == "move":
            i = find(q, uid)
            if i < 0:
                return {"state": public_state()}
            to = max(0, min(int(body.get("to", 0)), len(q) - 1))
            q.insert(to, q.pop(i))

        elif t == "again":
            i = find(h, uid)
            if i >= 0:
                src = h[i]
                q.append(make_item(src["id"], src["title"], src["channel"], src.get("singer"), "host"))
                if STATE["now"] is None:
                    STATE["now"] = q.pop(0)

        elif t == "cmd":
            cmd = body.get("cmd")
            if cmd not in ("toggle", "play", "pause", "restart"):
                return {"error": "ไม่รู้จักคำสั่ง", "status": 400}
            RUNTIME["cmd"] = {"id": RUNTIME["cmd"]["id"] + 1, "cmd": cmd}
            return {"state": public_state()}  # live only, nothing to save

        elif t == "setMode":
            if body.get("mode") not in MODES:
                return {"error": "ไม่รู้จักโหมด", "status": 400}
            STATE["mode"] = body["mode"]

        elif t == "setPartyName":
            STATE["partyName"] = str(body.get("name", "")).strip()[:60]

        elif t == "setAutoNext":
            STATE["autoNext"] = bool(body.get("value"))

        elif t == "shuffle":
            random.shuffle(q)
        elif t == "clear":
            q.clear()
        elif t == "clearHistory":
            h.clear()

        elif t == "saveList":
            name = str(body.get("name", "")).strip()[:60] or time.strftime("เพลย์ลิสต์ %d/%m/%Y")
            items = [{k: v for k, v in it.items() if k not in ("uid", "by", "at")}
                     for it in ([STATE["now"]] if STATE["now"] else []) + q]
            if not items:
                return {"error": "คิวยังว่าง เพิ่มเพลงก่อนบันทึก", "status": 400}
            kind = body.get("kind") if body.get("kind") in MODES else STATE.get("mode", "karaoke")
            STATE["saved"][name] = {"type": kind, "items": items}
        elif t == "loadList":
            lst = norm_list(STATE["saved"].get(body.get("name"), []))
            src = lst["items"]
            STATE["mode"] = lst["type"]  # loading a playlist switches the room to its type
            if body.get("mode") == "replace":
                q.clear()
                if STATE["now"]:
                    h.insert(0, STATE["now"])
                    STATE["now"] = None
            q.extend(make_item(x["id"], x.get("title"), x.get("channel"), x.get("singer"), "host")
                     for x in src if ID_RE.match(str(x.get("id", ""))))
            if STATE["now"] is None and q:
                STATE["now"] = q.pop(0)
        elif t == "delList":
            STATE["saved"].pop(body.get("name"), None)
        elif t == "importSaved":
            data = body.get("saved") or {}
            if isinstance(data, dict):
                for name, lst in data.items():
                    if isinstance(lst, (list, dict)):
                        STATE["saved"][str(name)[:60]] = norm_list(lst)
        else:
            return {"error": "ไม่รู้จักคำสั่ง", "status": 400}

        bump()
        return {"state": public_state()}


class Handler(BaseHTTPRequestHandler):
    server_version = "YTKaraoke/1.0"

    def log_message(self, fmt, *args):
        line = str(args[0]) if args else ""
        if "/api/state" not in line and "/api/tv" not in line:
            sys.stderr.write("  %s %s\n" % (self.address_string(), fmt % args))

    def send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_body(self):
        n = min(int(self.headers.get("Content-Length") or 0), 2_000_000)
        try:
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:
            return {}

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path
        if path in PAGES:
            fp = os.path.join(HERE, PAGES[path])
            try:
                with open(fp, "rb") as f:
                    data = f.read()
            except FileNotFoundError:
                return self.send_json({"error": f"ไม่พบไฟล์ {PAGES[path]}"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/state":
            with LOCK:
                self.send_json(public_state())
        elif path == "/api/info":
            port = self.server.server_address[1]
            self.send_json({"requestUrl": f"http://{lan_ip()}:{port}/request", "hostUrl": f"http://{lan_ip()}:{port}/"})
        elif path == "/api/search":
            q = urllib.parse.parse_qs(url.query).get("q", [""])[0].strip()
            if not q:
                return self.send_json({"results": []})
            if not STATE["apiKey"]:
                return self.send_json({"error": "ยังไม่ได้ตั้ง YouTube API key ที่จอหลัก"}, 400)
            res, err = yt_search(q, STATE["apiKey"])
            if err:
                return self.send_json({"error": err, "quota": err == QUOTA_MSG}, 502)
            self.send_json({"results": res})
        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self.send_json({"error": "ไม่พบหน้า"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        body = self.read_body()
        if path == "/api/add":
            self.send_json(handle_add(body))
        elif path == "/api/action":
            res = handle_action(body)
            self.send_json(res, res.pop("status", 200) if "error" in res else 200)
        elif path == "/api/list":
            res = handle_list(body)
            self.send_json(res, res.pop("status", 200) if "error" in res else 200)
        elif path == "/api/tv":
            # heartbeat from a TV screen; only a started TV takes over playback
            with LOCK:
                if body.get("started"):
                    RUNTIME["tvSeen"] = time.time()
                    RUNTIME["player"] = {k: body.get(k) for k in ("uid", "playing", "cur", "dur")}
                self.send_json(public_state())
        elif path == "/api/settings":
            if not body.get("host"):
                return self.send_json({"error": "ตั้งค่าได้เฉพาะจอหลัก"}, 403)
            with LOCK:
                STATE["apiKey"] = str(body.get("apiKey", "")).strip()
                bump()
                self.send_json({"state": public_state()})
        else:
            self.send_json({"error": "ไม่พบหน้า"}, 404)


def main():
    load_state()
    nums = [a for a in sys.argv[1:] if a.isdigit()]
    port = int(nums[0]) if nums else int(os.environ.get("PORT", 8000))
    httpd = None
    for p in range(port, port + 10):
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", p), Handler)
            port = p
            break
        except OSError:
            continue
    if not httpd:
        sys.exit(f"พอร์ต {port}-{port + 9} ถูกใช้อยู่ทั้งหมด ลองระบุพอร์ตอื่น เช่น python server.py 9000")

    ip = lan_ip()
    print("\n  ห้องคาราโอเกะพร้อมแล้ว")
    print(f"  จอหลัก (เปิดบนคอม/ทีวี):  http://localhost:{port}/")
    print(f"  จอทีวี:                  http://localhost:{port}/tv")
    print(f"  มือถือขอเพลง:            http://{ip}:{port}/request")
    print("  มือถือต้องต่อ Wi-Fi วงเดียวกับเครื่องนี้ · กด Ctrl+C เพื่อปิด\n")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(f"http://localhost:{port}/")).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  ปิดห้องแล้ว คิวถูกบันทึกไว้ใน karaoke_data.json")


if __name__ == "__main__":
    main()
