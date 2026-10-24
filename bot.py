import asyncio
import difflib
import hashlib
import html
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter, deque
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("music-bot")


def setup_ffmpeg() -> str:
    sys_ff = shutil.which("ffmpeg")
    if sys_ff:
        return sys_ff
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        bin_dir = "/tmp/ffbin"
        os.makedirs(bin_dir, exist_ok=True)
        target = os.path.join(bin_dir, "ffmpeg")
        if not os.path.exists(target):
            try:
                os.symlink(exe, target)
            except Exception:
                shutil.copy2(exe, target)
                os.chmod(target, 0o755)
        os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"
        return target
    except Exception:
        return "ffmpeg"


FFMPEG_BIN = setup_ffmpeg()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
FORCE_INVITE_LINK = os.environ.get("FORCE_INVITE_LINK", "https://t.me/+LjoTJ6A-AIdkMTg1").strip()
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
ADMIN_ID_ENV = os.environ.get("ADMIN_ID", "").strip()

STATE_FILE = "/tmp/music_bot_state.json"
RUNTIME = {
    "channel_id": os.environ.get("CHANNEL_ID", "").strip(),
    "admin_id": int(ADMIN_ID_ENV) if ADMIN_ID_ENV.isdigit() else 0,
    "bot_username": "Music92827bot",
    "songs_sent": 0,
    "channel_shares": 0,
}

KNOWN_USERS: set[int] = set()
USER_FAVS: dict[int, list[dict]] = {}
TRENDING_COUNTER: Counter = Counter()
TRENDING_META: dict[str, dict] = {}

if os.path.exists(STATE_FILE):
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as sf:
            saved = json.load(sf)
            if not RUNTIME["channel_id"]:
                RUNTIME["channel_id"] = saved.get("channel_id", "")
            if not RUNTIME["admin_id"]:
                RUNTIME["admin_id"] = int(saved.get("admin_id", 0) or 0)
            KNOWN_USERS.update(int(u) for u in saved.get("users", []))
    except Exception:
        pass

processed_updates = deque(maxlen=600)
job_semaphore = asyncio.Semaphore(2)

USER_MODE: dict[int, str] = {}
SEARCH_CACHE: dict[str, dict] = {}
BATCH_CACHE: dict[str, list[dict]] = {}
SHARE_CACHE: dict[str, dict] = {}
LYRICS_CACHE: dict[str, str] = {}


def persist_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as sf:
            json.dump(
                {
                    "channel_id": RUNTIME["channel_id"],
                    "admin_id": RUNTIME["admin_id"],
                    "users": list(KNOWN_USERS)[-5000:],
                },
                sf,
            )
    except Exception:
        pass


def save_channel_id(cid: str | int, admin_uid: int = 0):
    cid_str = str(cid).strip()
    if not cid_str.startswith("-100"):
        return
    RUNTIME["channel_id"] = cid_str
    if admin_uid and not RUNTIME["admin_id"]:
        RUNTIME["admin_id"] = int(admin_uid)
    persist_state()
    logger.info(f"Saved Force-Join CHANNEL_ID = {cid_str}")


async def tg_api(method: str, data: dict = None, files: dict = None, timeout: float = 120.0):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        if files:
            resp = await client.post(url, data=data or {}, files=files)
        else:
            resp = await client.post(url, json=data or {})
        return resp.json()


async def send_text(
    chat_id: int | str,
    text: str,
    reply_to: int = None,
    reply_markup: dict = None,
    parse_mode: str = None,
):
    payload: dict = {"chat_id": chat_id, "text": text}
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    res = await tg_api("sendMessage", payload)
    return res.get("result", {}).get("message_id")


async def edit_text(
    chat_id: int | str,
    message_id: int,
    text: str,
    reply_markup: dict = None,
    parse_mode: str = None,
):
    if not message_id:
        return
    payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        await tg_api("editMessageText", payload)
    except Exception:
        pass


def join_gate_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": " Channel ကို အရင် Join ရန် နှိပ်ပါ", "url": FORCE_INVITE_LINK}],
            [{"text": " Join ပြီးပါပြီ (စစ်ဆေးမည်)", "callback_data": "verify_join"}],
        ]
    }


def main_menu_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": " အဆိုတော်နာမည်ဖြင့် ရှာမည်", "callback_data": "mode:artist"}],
            [{"text": " သီချင်းနာမည်ဖြင့် ရှာမည်", "callback_data": "mode:song"}],
            [
                {"text": " လူကြိုက်အများဆုံး", "callback_data": "menu:trending"},
                {"text": " သိမ်းထားသော သီချင်းများ", "callback_data": "menu:favs"},
            ],
        ]
    }


def switch_mode_row() -> list[dict]:
    return [
        {"text": " အဆိုတော်ဖြင့်ရှာ", "callback_data": "mode:artist"},
        {"text": " သီချင်းဖြင့်ရှာ", "callback_data": "mode:song"},
    ]


JOIN_PROMPT_TEXT = (
    " သီချင်း Bot ကို အသုံးပြုဖို့ အောက်က Channel လေးကို အရင် Join ပေးပါနော်!\n\n"
    "1  Channel ကို အရင် Join ရန် နှိပ်ပါ ကိုနှိပ်ပြီး Join ပါ။\n"
    "2 ပြီးရင်  Join ပြီးပါပြီ ကို နှိပ်လိုက်မှ ရှေ့ဆက်အသုံးပြုလို့ ရပါမယ်! "
)

MAIN_MENU_TEXT = (
    " VIP Music Studio Bot မှ ကြိုဆိုပါတယ်!\n\n"
    " Album Cover ပုံ + သီချင်းစာသား (Lyrics) + Channel သို့ ပြန်လည်မျှဝေနိုင်သည့် စနစ်အပြည့်အစုံ ပါဝင်ပါသည်!\n"
    "အောက်က ခလုတ်များမှ စိတ်ကြိုက်ရွေးချယ်ပါ "
)


async def is_user_joined(user_id: int) -> bool:
    cid = RUNTIME.get("channel_id", "").strip()
    if not cid:
        return False
    try:
        res = await tg_api("getChatMember", {"chat_id": cid, "user_id": user_id}, timeout=15.0)
        if not res.get("ok"):
            return False
        member = res.get("result", {})
        status = member.get("status", "")
        if status in ("creator", "administrator", "member"):
            return True
        if status == "restricted" and member.get("is_member") is True:
            return True
        return False
    except Exception:
        return False


async def register_webhook(base_url: str) -> dict:
    if not BOT_TOKEN or not base_url:
        return {"ok": False}
    try:
        me = await tg_api("getMe", timeout=10.0)
        uname = me.get("result", {}).get("username")
        if uname:
            RUNTIME["bot_username"] = uname
    except Exception:
        pass
    clean_base = base_url.rstrip("/")
    if clean_base.startswith("http://") and "localhost" not in clean_base and "127.0.0.1" not in clean_base:
        clean_base = "https://" + clean_base[len("http://") :]
    webhook_url = f"{clean_base}/webhook"
    res = await tg_api(
        "setWebhook",
        {
            "url": webhook_url,
            "drop_pending_updates": False,
            "allowed_updates": ["message", "callback_query", "channel_post", "my_chat_member"],
        },
    )
    return res


@asynccontextmanager
async def lifespan(app: FastAPI):
    if RENDER_EXTERNAL_URL and BOT_TOKEN:
        try:
            await register_webhook(RENDER_EXTERNAL_URL)
        except Exception as e:
            logger.error(f"Startup webhook error: {e}")
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
@app.head("/")
async def index(request: Request):
    base = RENDER_EXTERNAL_URL or str(request.base_url)
    wh_ok = False
    if BOT_TOKEN:
        try:
            r = await register_webhook(base)
            wh_ok = bool(r.get("ok"))
        except Exception:
            wh_ok = False
    cid_badge = "FORCE-JOIN ACTIVE" if RUNTIME["channel_id"] else "AWAITING CHANNEL ADMIN"
    bname = RUNTIME["bot_username"]
    html_page = f"""<!DOCTYPE html>
<html lang="my"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VIP Music Workflow  @{bname}</title>
<style>
  body {{ background: radial-gradient(circle at 20% 10%, #0d2b26 0%, #060911 60%); color: #f8fafc; font-family: sans-serif; margin: 0; padding: 24px 16px 50px; }}
  .wrap {{ max-width: 720px; margin: 0 auto; }}
  .top {{ background: rgba(16,28,44,0.85); border: 1px solid rgba(16,185,129,0.35); border-radius: 18px; padding: 18px 20px; display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 10px; }}
  .pill {{ font-size: 11px; font-weight: 800; padding: 6px 12px; border-radius: 999px; background: rgba(16,185,129,0.16); color: #34d399; border: 1px solid rgba(16,185,129,0.4); }}
  h1 {{ text-align: center; font-size: clamp(22px, 4.5vw, 32px); background: linear-gradient(90deg, #34d399, #38bdf8, #a78bfa); -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin: 24px 0 8px; }}
  .sub {{ text-align: center; color: #94a3b8; font-size: 13px; margin-bottom: 24px; }}
  .node {{ background: rgba(15,23,42,0.88); border: 1px solid rgba(56,189,248,0.28); border-radius: 16px; padding: 16px 18px; display: flex; align-items: center; gap: 14px; }}
  .ico {{ width: 46px; height: 46px; border-radius: 12px; background: rgba(16,185,129,0.18); display: flex; align-items: center; justify-content: center; font-size: 22px; flex-shrink: 0; }}
  .wire {{ width: 3px; height: 24px; background: linear-gradient(180deg, #10b981, #38bdf8); margin: 0 auto; }}
  .btn {{ display: inline-block; margin-top: 24px; padding: 12px 24px; border-radius: 999px; background: linear-gradient(90deg, #10b981, #06b6d4); color: #041311; font-weight: 800; text-decoration: none; }}
</style></head>
<body><div class="wrap">
  <div class="top">
    <div><b> VIP MUSIC WORKFLOW ENGINE</b><div style="font-size:12px;color:#94a3b8;">@{bname}  {len(KNOWN_USERS)} Users  {RUNTIME["songs_sent"]} MP3s  {RUNTIME["channel_shares"]} Shares</div></div>
    <div style="display:flex;gap:8px;"><span class="pill">{"WEBHOOK READY" if wh_ok else "CONNECTING"}</span><span class="pill">{cid_badge}</span></div>
  </div>
  <h1>Force-Join &amp; Social Music Pipeline</h1>
  <div class="sub">Channel Gate  Artist/Song Discovery  Cover Art + Lyrics  Anonymous/Public Channel Share</div>
  <div class="node"><div class="ico"></div><div><b>NODE 01  Force-Join Channel Gate</b><div style="font-size:13px;color:#94a3b8;">Channel မဝင်မချင်း ရှေ့ဆက်ခွင့်ပိတ်ထားပြီး Member ဖြစ်မှ သီချင်းရှာခွင့်ပြုခြင်း</div></div></div>
  <div class="wire"></div>
  <div class="node"><div class="ico"></div><div><b>NODE 02  Artist Catalog &amp; Exact Song Matcher</b><div style="font-size:13px;color:#94a3b8;">အဆိုတော်အလိုက် သီချင်းအားလုံးရွေးယူနိုင်ခြင်း နှင့် သီချင်းနာမည်မှန်ပါက MP3 တန်းပို့ပေးခြင်း</div></div></div>
  <div class="wire"></div>
  <div class="node"><div class="ico"></div><div><b>NODE 03  Album Cover Art + Live Lyrics</b><div style="font-size:13px;color:#94a3b8;">MP3 တွင် မူရင်း Cover ပုံ၊ အဆိုတော်အမည် ထည့်သွင်းပြီး သီချင်းစာသား ကြည့်ရှုနိုင်ခြင်း</div></div></div>
  <div class="wire"></div>
  <div class="node"><div class="ico"></div><div><b>NODE 04  Channel Community Share (Profile On/Off)</b><div style="font-size:13px;color:#94a3b8;">နားထောင်ခဲ့သော သီချင်းကို Profile ဖော်၍ဖြစ်စေ၊ Profile ဖျောက်၍ဖြစ်စေ Channel ပေါ် တန်းတင်နိုင်ခြင်း</div></div></div>
  <div style="text-align:center;"><a class="btn" href="https://t.me/{bname}" target="_blank"> Open @{bname}</a></div>
</div></body></html>"""
    return HTMLResponse(content=html_page)


def run_cmd(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)


def fmt_dur(sec_val) -> str:
    try:
        s = int(float(sec_val))
        return f"{s // 60}:{s % 60:02d}"
    except Exception:
        return "MP3"


def cache_track(tr: dict) -> str:
    key = hashlib.md5(f"{tr['url']}".encode()).hexdigest()[:10]
    SEARCH_CACHE[key] = tr
    if len(SEARCH_CACHE) > 2000:
        SEARCH_CACHE.pop(next(iter(SEARCH_CACHE)))
    return key


def cache_share_item(item: dict) -> str:
    skey = hashlib.md5(f"{item['file_id']}:{time.time()}".encode()).hexdigest()[:10]
    SHARE_CACHE[skey] = item
    if len(SHARE_CACHE) > 1500:
        SHARE_CACHE.pop(next(iter(SHARE_CACHE)))
    return skey


async def search_tracks(query: str, max_items: int = 10) -> list[dict]:
    results = []
    seen_titles = set()
    fetch_n = max_items + 5

    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--no-warnings",
        "--dump-json",
        f"scsearch{fetch_n}:{query}",
    ]
    proc = await asyncio.to_thread(run_cmd, cmd, 30)
    for line in proc.stdout.decode(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            url = item.get("url") or item.get("webpage_url")
            title = (item.get("title") or "Unknown Track").strip()
            uploader = (item.get("uploader") or "").strip()
            dur = item.get("duration") or 0
            if dur and float(dur) > 1200 and "mix" not in query.lower():
                continue
            norm_t = re.sub(r"\s+", " ", title.lower())
            if url and norm_t not in seen_titles:
                seen_titles.add(norm_t)
                display_title = title if uploader.lower() in title.lower() or not uploader else f"{title} - {uploader}"
                results.append(
                    {
                        "title": display_title,
                        "raw_title": title,
                        "artist": uploader,
                        "url": url,
                        "dur": fmt_dur(dur),
                    }
                )
        except Exception:
            continue
        if len(results) >= max_items:
            break

    if len(results) < 3:
        cmd_yt = [
            "yt-dlp",
            "--flat-playlist",
            "--no-warnings",
            "--extractor-args",
            "youtube:player_client=android_vr,ios,mweb",
            "--dump-json",
            f"ytsearch{max_items}:{query}",
        ]
        proc2 = await asyncio.to_thread(run_cmd, cmd_yt, 25)
        for line in proc2.stdout.decode(errors="ignore").splitlines():
            try:
                item = json.loads(line.strip())
                vid = item.get("id")
                url = item.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else None)
                title = (item.get("title") or "Unknown Track").strip()
                uploader = (item.get("uploader") or item.get("channel") or "").strip()
                dur = item.get("duration") or 0
                if dur and float(dur) > 1200:
                    continue
                norm_t = re.sub(r"\s+", " ", title.lower())
                if url and norm_t not in seen_titles:
                    seen_titles.add(norm_t)
                    results.append(
                        {
                            "title": title,
                            "raw_title": title,
                            "artist": uploader,
                            "url": url,
                            "dur": fmt_dur(dur),
                        }
                    )
            except Exception:
                continue
            if len(results) >= max_items:
                break

    return results


def is_strong_song_match(query: str, candidate_title: str) -> bool:
    q = re.sub(r"[^\w\u1000-\u109F]+", " ", query.lower()).strip()
    t = re.sub(r"[^\w\u1000-\u109F]+", " ", candidate_title.lower()).strip()
    if not q or not t:
        return False
    if q in t or t in q:
        return True
    q_words = [w for w in q.split() if len(w) > 1]
    if q_words and all(w in t for w in q_words):
        return True
    ratio = difflib.SequenceMatcher(None, q, t[: max(len(q) + 10, len(t))]).ratio()
    return ratio >= 0.62


async def resolve_youtube_title_oembed(yt_url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get("https://www.youtube.com/oembed", params={"url": yt_url, "format": "json"})
            if r.status_code == 200:
                j = r.json()
                return f"{j.get('title', '')} {j.get('author_name', '')}".strip()
    except Exception:
        pass
    return ""


async def fetch_song_lyrics(search_title: str) -> str:
    if search_title in LYRICS_CACHE:
        return LYRICS_CACHE[search_title]

    clean_q = re.sub(
        r"\(.*?\)|\[.*?\]|official\s*music\s*video|official\s*audio|lyrics?",
        " ",
        search_title,
        flags=re.I,
    )
    clean_q = re.sub(r"\s+", " ", clean_q).strip()

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            r = await client.get("https://lrclib.net/api/search", params={"q": clean_q})
            if r.status_code == 200:
                items = r.json()
                for it in items:
                    plain = (it.get("plainLyrics") or "").strip()
                    if plain and len(plain) > 40:
                        header = f" {it.get('trackName', '')}  {it.get('artistName', '')}\n\n"
                        full_l = (header + plain)[:3900]
                        LYRICS_CACHE[search_title] = full_l
                        return full_l
    except Exception as e:
        logger.warning(f"LRCLIB lookup warning: {e}")

    return ""


async def download_song_with_cover(target_url: str, fallback_query: str, work_dir: str) -> dict | None:
    out_tmpl = os.path.join(work_dir, "track.%(ext)s")

    attempts = [
        [
            "yt-dlp",
            "--no-playlist",
            "--no-warnings",
            "--write-info-json",
            "--extractor-args",
            "youtube:player_client=android_vr,ios,mweb,tv_downgraded",
            "-f",
            "bestaudio[filesize<45M]/bestaudio/best[filesize<45M]",
            "-o",
            out_tmpl,
            target_url,
        ]
    ]

    is_yt = any(d in target_url.lower() for d in ("youtube.com", "youtu.be"))
    if is_yt:
        yt_title = fallback_query or await resolve_youtube_title_oembed(target_url)
        if yt_title:
            attempts.append(
                [
                    "yt-dlp",
                    "--no-playlist",
                    "--no-warnings",
                    "--write-info-json",
                    "-f",
                    "bestaudio/best",
                    "-o",
                    out_tmpl,
                    f"scsearch1:{yt_title}",
                ]
            )

    downloaded_file = None
    info_data = {}
    for cmd in attempts:
        for fn in os.listdir(work_dir):
            if fn.startswith("track."):
                os.remove(os.path.join(work_dir, fn))
        await asyncio.to_thread(run_cmd, cmd, 120)
        for fn in os.listdir(work_dir):
            if fn.endswith(".info.json"):
                try:
                    with open(os.path.join(work_dir, fn), "r", encoding="utf-8", errors="ignore") as jf:
                        info_data = json.load(jf)
                except Exception:
                    pass
            elif fn.startswith("track.") and not fn.endswith(".part"):
                cand = os.path.join(work_dir, fn)
                if os.path.getsize(cand) > 15_000:
                    downloaded_file = cand
        if downloaded_file:
            break

    if not downloaded_file:
        return None

    raw_title = (info_data.get("track") or info_data.get("title") or fallback_query or "Music Track").strip()
    artist = (
        info_data.get("artist")
        or info_data.get("creator")
        or info_data.get("uploader")
        or info_data.get("channel")
        or ""
    ).strip()

    if " - " in raw_title:
        parts = [p.strip() for p in raw_title.split(" - ", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            artist = parts[0]
            song_title = parts[1]
        else:
            song_title = raw_title
    else:
        song_title = raw_title

    duration_sec = int(float(info_data.get("duration") or 0))
    thumb_url = info_data.get("thumbnail") or ""
    cover_jpg = os.path.join(work_dir, "cover.jpg")
    has_cover = False

    if thumb_url and thumb_url.startswith("http"):
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
                tr = await client.get(thumb_url)
                if tr.status_code == 200 and len(tr.content) > 800:
                    raw_img = os.path.join(work_dir, "raw_thumb.img")
                    with open(raw_img, "wb") as imgf:
                        imgf.write(tr.content)
                    await asyncio.to_thread(
                        run_cmd,
                        [
                            FFMPEG_BIN,
                            "-y",
                            "-i",
                            raw_img,
                            "-vf",
                            "scale=320:320:force_original_aspect_ratio=increase,crop=320:320",
                            "-q:v",
                            "4",
                            cover_jpg,
                        ],
                        20,
                    )
                    has_cover = os.path.exists(cover_jpg) and os.path.getsize(cover_jpg) > 500
        except Exception as e:
            logger.warning(f"Cover art fetch warning: {e}")

    final_mp3 = os.path.join(work_dir, "master_song.mp3")
    ff_args = [FFMPEG_BIN, "-y", "-i", downloaded_file]
    if has_cover:
        ff_args += [
            "-i",
            cover_jpg,
            "-map",
            "0:a:0",
            "-map",
            "1:v:0",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            "-c:v",
            "mjpeg",
            "-id3v2_version",
            "3",
            "-metadata:s:v",
            "title=Album cover",
            "-metadata:s:v",
            "comment=Cover (front)",
        ]
    else:
        ff_args += ["-vn", "-c:a", "libmp3lame", "-b:a", "192k"]

    ff_args += [
        "-metadata",
        f"title={song_title}",
        "-metadata",
        f"artist={artist or 'VIP Music'}",
        final_mp3,
    ]
    await asyncio.to_thread(run_cmd, ff_args, 90)

    if not os.path.exists(final_mp3) or os.path.getsize(final_mp3) < 10_000:
        return None

    return {
        "mp3_path": final_mp3,
        "cover_path": cover_jpg if has_cover else None,
        "title": song_title,
        "artist": artist or "Music Studio",
        "full_label": f"{artist} - {song_title}" if artist and artist.lower() not in song_title.lower() else song_title,
        "duration": duration_sec,
    }


async def send_single_mp3(
    chat_id: int,
    user_id: int,
    url: str,
    title_hint: str,
    reply_to: int = None,
    ask_channel_share: bool = True,
):
    if not await is_user_joined(user_id):
        await send_text(chat_id, JOIN_PROMPT_TEXT, reply_to=reply_to, reply_markup=join_gate_keyboard())
        return

    status_id = await send_text(chat_id, f" Album Cover + MP3 ပြင်ဆင်နေပါပြီ...\n {title_hint or url}", reply_to=reply_to)
    async with job_semaphore:
        work_dir = tempfile.mkdtemp(prefix="music_dl_")
        try:
            meta = await download_song_with_cover(url, title_hint, work_dir)
            if not meta:
                await edit_text(chat_id, status_id, " ဒီသီချင်းဖိုင်ကို ဆွဲလို့ မရပါဘူးနော်။ တခြားသီချင်းတစ်ပုဒ် ရွေးကြည့်ပေးပါ!")
                return

            full_label = meta["full_label"]
            tr_obj = {
                "title": full_label,
                "raw_title": meta["title"],
                "artist": meta["artist"],
                "url": url,
                "dur": fmt_dur(meta["duration"]),
            }
            t_key = cache_track(tr_obj)

            TRENDING_COUNTER[t_key] += 1
            TRENDING_META[t_key] = tr_obj

            await edit_text(chat_id, status_id, f" Telegram ဆီ သီချင်းပို့ပေးနေပါပြီ...\n {full_label}")
            safe_filename = re.sub(r'[\\/:*?"<>|]+', "_", full_label)[:80] + ".mp3"

            song_buttons = {
                "inline_keyboard": [
                    [
                        {"text": " သီချင်းစာသား (Lyrics)", "callback_data": f"lyr:{t_key}"},
                        {"text": " သိမ်းထားမည်", "callback_data": f"fav:{t_key}"},
                    ],
                    switch_mode_row(),
                ]
            }

            form_data = {
                "chat_id": str(chat_id),
                "title": meta["title"][:100],
                "performer": meta["artist"][:80],
                "caption": f" {meta['title']}\n {meta['artist']}",
                "reply_markup": json.dumps(song_buttons),
            }
            if meta["duration"] > 0:
                form_data["duration"] = str(meta["duration"])

            if meta["cover_path"] and os.path.exists(meta["cover_path"]):
                with open(meta["mp3_path"], "rb") as af, open(meta["cover_path"], "rb") as cf:
                    res = await tg_api(
                        "sendAudio",
                        data=form_data,
                        files={
                            "audio": (safe_filename, af, "audio/mpeg"),
                            "thumbnail": ("cover.jpg", cf, "image/jpeg"),
                        },
                        timeout=200.0,
                    )
            else:
                with open(meta["mp3_path"], "rb") as af:
                    res = await tg_api(
                        "sendAudio",
                        data=form_data,
                        files={"audio": (safe_filename, af, "audio/mpeg")},
                        timeout=200.0,
                    )

            if res.get("ok"):
                RUNTIME["songs_sent"] += 1
                try:
                    await tg_api("deleteMessage", {"chat_id": chat_id, "message_id": status_id})
                except Exception:
                    pass

                # Capture Telegram's audio file_id so sharing to Channel takes 0.1s with zero re-upload!
                sent_msg = res.get("result", {})
                audio_file_id = (sent_msg.get("audio") or {}).get("file_id")
                sent_msg_id = sent_msg.get("message_id")

                if audio_file_id and ask_channel_share and RUNTIME.get("channel_id"):
                    skey = cache_share_item(
                        {
                            "file_id": audio_file_id,
                            "title": meta["title"],
                            "artist": meta["artist"],
                            "shared": False,
                        }
                    )
                    share_prompt_kb = {
                        "inline_keyboard": [
                            [{"text": " Profile ဖော်ပြီး Channel မှာတင်မည်", "callback_data": f"shpub:{skey}"}],
                            [{"text": " Profile ဖျောက်ပြီး Channel မှာတင်မည်", "callback_data": f"shanon:{skey}"}],
                            [{"text": " မတင်တော့ပါ", "callback_data": "shno"}],
                        ]
                    }
                    await send_text(
                        chat_id,
                        f" အခုနားထောင်နေတဲ့ {meta['title']} သီချင်းလေးကို Channel ပေါ် ပြန်တင်ပေးချင်ပါသလား?\n"
                        "(Profile ဖော်ပြီးတင်မလား၊ ဖျောက်ပြီးတင်မလား စိတ်ကြိုက်ရွေးနိုင်ပါတယ် )",
                        reply_to=sent_msg_id,
                        reply_markup=share_prompt_kb,
                    )
            else:
                await edit_text(chat_id, status_id, f" သီချင်းပို့ရာတွင် အခက်အခဲရှိပါသည်: {res}")
        except Exception as e:
            logger.exception("Music delivery error")
            await edit_text(chat_id, status_id, f" အမှားတစ်ခု ဖြစ်သွားပါတယ်: {str(e)[:180]}")
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)


async def send_batch_mp3(chat_id: int, user_id: int, tracks: list[dict]):
    if not await is_user_joined(user_id):
        await send_text(chat_id, JOIN_PROMPT_TEXT, reply_markup=join_gate_keyboard())
        return
    info_id = await send_text(
        chat_id,
        f" ရွေးချယ်ထားသော သီချင်း ({len(tracks)}) ပုဒ်လုံးကို Album Cover နှင့်တကွ MP3 ပို့ပေးနေပါပြီ...",
    )
    for idx, tr in enumerate(tracks):
        # Ask channel share on the last song so user doesn't get flooded during batch download
        ask_sh = idx == len(tracks) - 1
        await send_single_mp3(chat_id, user_id, tr["url"], tr["title"], ask_channel_share=ask_sh)
    await edit_text(chat_id, info_id, f" သီချင်း ({len(tracks)}) ပုဒ်လုံး ပို့ပေးပြီးပါပြီ! ")


async def post_song_to_channel(
    skey: str,
    hide_profile: bool,
    from_user: dict,
    user_chat_id: int,
    prompt_msg_id: int,
    cb_id: str,
):
    item = SHARE_CACHE.get(skey)
    cid = RUNTIME.get("channel_id", "").strip()
    if not item or not cid:
        await tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": " သီချင်းအချက်အလက် သက်တမ်းကုန်သွားပါပြီ။", "show_alert": True})
        return
    if item.get("shared"):
        await tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": " ဒီသီချင်းကို Channel မှာ တင်ပြီးသားဖြစ်ပါတယ်!", "show_alert": True})
        return

    uid = from_user.get("id", 0)
    first_name = (from_user.get("first_name") or "").strip()
    last_name = (from_user.get("last_name") or "").strip()
    full_name = f"{first_name} {last_name}".strip() or "Music Lover"
    username = (from_user.get("username") or "").strip()

    safe_title = html.escape(item["title"])
    safe_artist = html.escape(item["artist"])
    bname = RUNTIME.get("bot_username", "Music92827bot")

    if hide_profile:
        sharer_line = " အမည်မဖော်လိုသူ (Profile ဖျောက်ထားသည်)"
    else:
        safe_name = html.escape(full_name)
        if username:
            sharer_line = f' <a href="https://t.me/{username}">{safe_name}</a> (@{html.escape(username)})'
        else:
            sharer_line = f' <a href="tg://user?id={uid}">{safe_name}</a>'

    channel_caption = (
        f" <b>{safe_title}</b>\n"
        f" {safe_artist}\n\n"
        f" မျှဝေသူ  {sharer_line}\n"
        f" သီချင်းရှာရန်  @{bname}"
    )

    ch_kb = {
        "inline_keyboard": [
            [{"text": " ဒီ Bot မှာ ကိုယ်တိုင် သီချင်းရှာမည်", "url": f"https://t.me/{bname}"}]
        ]
    }

    res = await tg_api(
        "sendAudio",
        data={
            "chat_id": cid,
            "audio": item["file_id"],
            "caption": channel_caption,
            "parse_mode": "HTML",
            "reply_markup": ch_kb,
        },
        timeout=30.0,
    )

    if res.get("ok"):
        item["shared"] = True
        RUNTIME["channel_shares"] += 1
        mode_note = " Profile ဖျောက်ပြီး" if hide_profile else " သင့် Profile နှင့်တကွ"
        await tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": " Channel ပေါ် တင်ပြီးပါပြီ!"})
        await edit_text(
            user_chat_id,
            prompt_msg_id,
            f" {item['title']} သီချင်းကို {mode_note} Channel ပေါ် တင်ပေးပြီးပါပြီ! ",
            reply_markup={
                "inline_keyboard": [
                    [{"text": " Channel တွင် သွားကြည့်မည်", "url": FORCE_INVITE_LINK}]
                ]
            },
        )
    else:
        desc = res.get("description", "Post permission လိုအပ်ပါသည်")
        await tg_api(
            "answerCallbackQuery",
            {
                "callback_query_id": cb_id,
                "text": f" Channel ပေါ်တင်မရပါ ({desc[:80]})  Bot ကို Channel တွင် Post Messages ခွင့်ပြုထားကြောင်း စစ်ပေးပါ!",
                "show_alert": True,
            },
        )


async def handle_artist_search(chat_id: int, user_id: int, artist_name: str, reply_to: int):
    status_id = await send_text(chat_id, f" အဆိုတော် {artist_name} ၏ သီချင်းများကို ရှာဖွေနေပါတယ်...", reply_to=reply_to)
    tracks = await search_tracks(artist_name, max_items=10)
    if not tracks:
        await edit_text(
            chat_id,
            status_id,
            f" အဆိုတော် {artist_name} အတွက် သီချင်းရှာမတွေ့ပါဘူးနော်။ နာမည်စာလုံးပေါင်း ပြောင်းရေးကြည့်ပါ!",
            reply_markup={"inline_keyboard": [switch_mode_row()]},
        )
        return

    batch_key = hashlib.md5(f"artist:{artist_name}:{time.time()}".encode()).hexdigest()[:10]
    BATCH_CACHE[batch_key] = tracks
    if len(BATCH_CACHE) > 300:
        BATCH_CACHE.pop(next(iter(BATCH_CACHE)))

    buttons = []
    for idx, tr in enumerate(tracks, start=1):
        k = cache_track(tr)
        buttons.append([{"text": f"{idx}.  {tr['title'][:40]} ({tr['dur']})", "callback_data": f"dl:{k}"}])

    buttons.append([{"text": f" ဒီထဲက သီချင်းအားလုံး ({len(tracks)} ပုဒ်) MP3 ယူမည်", "callback_data": f"dlall:{batch_key}"}])
    buttons.append(switch_mode_row())

    await edit_text(
        chat_id,
        status_id,
        f" အဆိုတော် {artist_name} ၏ သီချင်းခေါင်းစဉ်များ \n"
        "ကြိုက်တဲ့သီချင်းကို တစ်ပုဒ်ချင်းဖြစ်စေ၊ အားလုံးဖြစ်စေ နှိပ်ပြီး MP3 ယူနိုင်ပါတယ် ",
        reply_markup={"inline_keyboard": buttons},
    )


async def handle_song_title_search(chat_id: int, user_id: int, song_query: str, reply_to: int):
    status_id = await send_text(chat_id, f" {song_query} သီချင်းကို ရှာဖွေနေပါတယ်...", reply_to=reply_to)
    tracks = await search_tracks(song_query, max_items=7)
    if not tracks:
        await edit_text(
            chat_id,
            status_id,
            f" {song_query} သီချင်း ရှာမတွေ့ပါဘူးနော်။ စာလုံးပေါင်း ပြန်စစ်ပြီး ထပ်ပို့ကြည့်ပါ!",
            reply_markup={"inline_keyboard": [switch_mode_row()]},
        )
        return

    top_track = tracks[0]
    exact_matched = is_strong_song_match(song_query, top_track["title"]) or is_strong_song_match(
        song_query, top_track.get("raw_title", "")
    )

    extras = tracks[1:] if exact_matched else tracks
    buttons = []
    for tr in extras:
        k = cache_track(tr)
        buttons.append([{"text": f" {tr['title'][:42]} ({tr['dur']})", "callback_data": f"dl:{k}"}])
    buttons.append(switch_mode_row())

    if exact_matched:
        msg_txt = (
            f" {top_track['title']} သီချင်းအမှန်ကို MP3 တန်းပို့ပေးနေပါပြီ!\n\n"
            " အနီးစပ်ဆုံး တခြားသီချင်းများ (ထပ်ယူချင်ရင် နှိပ်ပါ) "
        )
    else:
        msg_txt = (
            f" {song_query} နှင့် အနီးစပ်ဆုံး သီချင်းများ \n"
            f"(အနီးစပ်ဆုံး {top_track['title']} ကိုလည်း MP3 ပို့ပေးနေပါတယ် )"
        )

    await edit_text(chat_id, status_id, msg_txt, reply_markup={"inline_keyboard": buttons})
    await send_single_mp3(chat_id, user_id, top_track["url"], top_track["title"], reply_to=reply_to)


async def broadcast_to_users(admin_chat_id: int, bcast_text: str):
    targets = list(KNOWN_USERS)
    sent = 0
    for uid in targets:
        try:
            r = await tg_api("sendMessage", {"chat_id": uid, "text": bcast_text}, timeout=15.0)
            if r.get("ok"):
                sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await send_text(admin_chat_id, f" Broadcast ပို့ပြီးပါပြီ! (စုစုပေါင်း {sent}/{len(targets)} ဦးထံ ရောက်ရှိပါသည်)")


@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
    except Exception:
        return JSONResponse({"ok": True})

    update_id = update.get("update_id")
    if update_id in processed_updates:
        return JSONResponse({"ok": True})
    if update_id is not None:
        processed_updates.append(update_id)

    # 1. Auto-capture Private Channel ID & Admin ID
    my_cm = update.get("my_chat_member")
    if my_cm:
        ch = my_cm.get("chat", {})
        from_u = my_cm.get("from", {}).get("id", 0)
        if ch.get("type") == "channel" and str(ch.get("id", "")).startswith("-100"):
            save_channel_id(ch["id"], admin_uid=from_u)
            if from_u:
                asyncio.create_task(
                    send_text(
                        from_u,
                        f" သင့် Channel ({ch.get('title', '')}) နဲ့ Force-Join + Channel Share စနစ် အလိုအလျောက် ချိတ်ဆက်ပြီးပါပြီ!\n"
                        f" Channel ID: {ch['id']}\n"
                        f"(Server ပြန်စတိုင်း အမြဲမှတ်မိနေအောင် Render ရဲ့ Environment မှာ Key: CHANNEL_ID / Value: {ch['id']} ထည့်ထားနိုင်ပါတယ်!)",
                    )
                )
        return JSONResponse({"ok": True})

    ch_post = update.get("channel_post")
    if ch_post:
        ch = ch_post.get("chat", {})
        if ch.get("type") == "channel" and str(ch.get("id", "")).startswith("-100"):
            save_channel_id(ch["id"])
        return JSONResponse({"ok": True})

    # 2. Callback Button Clicks
    cb = update.get("callback_query")
    if cb:
        cb_id = cb.get("id")
        from_user = cb.get("from", {})
        user_id = from_user.get("id")
        msg = cb.get("message", {})
        chat_id = msg.get("chat", {}).get("id")
        msg_id = msg.get("message_id")
        data = cb.get("data", "")

        if user_id and user_id not in KNOWN_USERS:
            KNOWN_USERS.add(user_id)
            persist_state()

        if data == "verify_join":
            joined = await is_user_joined(user_id)
            if not joined:
                alert_msg = (
                    " Bot Admin မှ Channel ထဲတွင် @Music92827bot ကို Admin အရင်ခန့်ပေးရန် လိုအပ်ပါသည်!"
                    if not RUNTIME.get("channel_id")
                    else " သင် Channel ကို မ Join ရသေးပါ! အပေါ်က ခလုတ်ကိုနှိပ်ပြီး အရင် Join ပေးပါနော်။"
                )
                await tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": alert_msg, "show_alert": True})
            else:
                await tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": " Join ပြီးကြောင်း အတည်ပြုပြီးပါပြီ!"})
                await edit_text(chat_id, msg_id, MAIN_MENU_TEXT, reply_markup=main_menu_keyboard())
            return JSONResponse({"ok": True})

        if not await is_user_joined(user_id):
            await tg_api(
                "answerCallbackQuery",
                {
                    "callback_query_id": cb_id,
                    "text": " Channel ကို အရင် Join ပေးမှ ဆက်သုံးလို့ရပါမယ်နော်!",
                    "show_alert": True,
                },
            )
            await send_text(chat_id, JOIN_PROMPT_TEXT, reply_markup=join_gate_keyboard())
            return JSONResponse({"ok": True})

        # Channel Share Handlers (Public Profile vs Hidden Profile / Cancel)
        if data.startswith("shpub:"):
            skey = data[6:]
            asyncio.create_task(post_song_to_channel(skey, False, from_user, chat_id, msg_id, cb_id))
            return JSONResponse({"ok": True})

        if data.startswith("shanon:"):
            skey = data[7:]
            asyncio.create_task(post_song_to_channel(skey, True, from_user, chat_id, msg_id, cb_id))
            return JSONResponse({"ok": True})

        if data == "shno":
            await tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": "ဟုတ်ကဲ့၊ Channel မှာ မတင်တော့ပါဘူးနော်!"})
            try:
                await tg_api("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})
            except Exception:
                pass
            return JSONResponse({"ok": True})

        if data == "mode:artist":
            USER_MODE[user_id] = "artist"
            await tg_api("answerCallbackQuery", {"callback_query_id": cb_id})
            await send_text(
                chat_id,
                " ရှာချင်တဲ့ အဆိုတော်နာမည် ကို ရေးပို့ပေးပါ!\n"
                "(ဥပမာ  မျိုးကြီး၊ လေးဖြူ၊ ဝေလ၊ G Fatt၊ Ed Sheeran)",
            )
            return JSONResponse({"ok": True})

        if data == "mode:song":
            USER_MODE[user_id] = "song"
            await tg_api("answerCallbackQuery", {"callback_query_id": cb_id})
            await send_text(
                chat_id,
                " ရှာချင်တဲ့ သီချင်းနာမည် ကို ရေးပို့ပေးပါ!\n"
                "(နာမည်မှန်ရင် Album Cover + MP3 တန်းပို့ပေးပြီး အနီးစပ်ဆုံး သီချင်းတွေပါ ပြပေးပါမယ်!)",
            )
            return JSONResponse({"ok": True})

        if data == "menu:trending":
            await tg_api("answerCallbackQuery", {"callback_query_id": cb_id})
            if not TRENDING_COUNTER:
                asyncio.create_task(handle_artist_search(chat_id, user_id, "Myanmar New Song Hits", msg_id))
            else:
                btns = []
                for k, cnt in TRENDING_COUNTER.most_common(8):
                    tr = TRENDING_META.get(k)
                    if tr:
                        btns.append([{"text": f" {tr['title'][:38]} ({cnt}x)", "callback_data": f"dl:{k}"}])
                btns.append(switch_mode_row())
                await send_text(chat_id, " ယနေ့ လူဒေါင်းအများဆုံး သီချင်းများ ", reply_markup={"inline_keyboard": btns})
            return JSONResponse({"ok": True})

        if data == "menu:favs":
            favs = USER_FAVS.get(user_id, [])
            await tg_api("answerCallbackQuery", {"callback_query_id": cb_id})
            if not favs:
                await send_text(
                    chat_id,
                    " သိမ်းထားသော သီချင်း မရှိသေးပါ!\nသီချင်းဒေါင်းပြီးတိုင်း အောက်က  သိမ်းထားမည် ခလုတ်ကို နှိပ်သိမ်းထားနိုင်ပါတယ်။",
                    reply_markup=main_menu_keyboard(),
                )
            else:
                btns = []
                for tr in favs[-10:]:
                    k = cache_track(tr)
                    btns.append([{"text": f" {tr['title'][:40]} ({tr['dur']})", "callback_data": f"dl:{k}"}])
                btns.append(switch_mode_row())
                await send_text(chat_id, " သင် သိမ်းထားသော အကြိုက်ဆုံး သီချင်းများ ", reply_markup={"inline_keyboard": btns})
            return JSONResponse({"ok": True})

        if data.startswith("fav:"):
            key = data[4:]
            tr = SEARCH_CACHE.get(key)
            if tr:
                fav_list = USER_FAVS.setdefault(user_id, [])
                if not any(x["url"] == tr["url"] for x in fav_list):
                    fav_list.append(tr)
                await tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": " အကြိုက်ဆုံးစာရင်းထဲ သိမ်းပြီးပါပြီ!"})
            else:
                await tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": " ပြန်ရှာပြီးမှ သိမ်းပေးပါနော်။"})
            return JSONResponse({"ok": True})

        if data.startswith("lyr:"):
            key = data[4:]
            tr = SEARCH_CACHE.get(key)
            if not tr:
                await tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": " သီချင်းအချက်အလက် မတွေ့ပါ။", "show_alert": True})
                return JSONResponse({"ok": True})
            await tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": " သီချင်းစာသား ရှာပေးနေပါတယ်..."})

            async def _send_lyrics():
                q_str = f"{tr.get('raw_title', tr['title'])} {tr.get('artist', '')}".strip()
                lyr = await fetch_song_lyrics(q_str) or await fetch_song_lyrics(tr.get("raw_title", tr["title"]))
                if lyr:
                    await send_text(chat_id, lyr)
                else:
                    await send_text(chat_id, f" {tr['title']} အတွက် သီချင်းစာသား (Lyrics) Database ထဲတွင် မရှိသေးပါနော်။")

            asyncio.create_task(_send_lyrics())
            return JSONResponse({"ok": True})

        if data.startswith("dl:"):
            key = data[3:]
            tr = SEARCH_CACHE.get(key)
            await tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": " MP3 ပို့ပေးနေပါပြီ..."})
            if not tr:
                await send_text(chat_id, " စာရင်းသက်တမ်းကုန်သွားလို့ နာမည်ပြန်ရိုက်ရှာပေးပါနော်။")
            else:
                asyncio.create_task(send_single_mp3(chat_id, user_id, tr["url"], tr["title"]))
            return JSONResponse({"ok": True})

        if data.startswith("dlall:"):
            bkey = data[6:]
            tracks = BATCH_CACHE.get(bkey)
            await tg_api("answerCallbackQuery", {"callback_query_id": cb_id, "text": " သီချင်းအားလုံး ပို့ပေးနေပါပြီ..."})
            if not tracks:
                await send_text(chat_id, " စာရင်းသက်တမ်းကုန်သွားလို့ အဆိုတော်နာမည် ပြန်ရိုက်ရှာပေးပါနော်။")
            else:
                asyncio.create_task(send_batch_mp3(chat_id, user_id, tracks))
            return JSONResponse({"ok": True})

        return JSONResponse({"ok": True})

    # 3. Incoming Messages
    msg = update.get("message")
    if not msg:
        return JSONResponse({"ok": True})

    chat_id = msg.get("chat", {}).get("id")
    user_id = msg.get("from", {}).get("id")
    msg_id = msg.get("message_id")

    if user_id and user_id not in KNOWN_USERS:
        KNOWN_USERS.add(user_id)
        persist_state()

    fwd_origin = msg.get("forward_origin") or {}
    fwd_chat = fwd_origin.get("chat") or msg.get("forward_from_chat") or {}
    if fwd_chat.get("type") == "channel" and str(fwd_chat.get("id", "")).startswith("-100"):
        save_channel_id(fwd_chat["id"], admin_uid=user_id)
        asyncio.create_task(
            send_text(
                chat_id,
                f" Channel ({fwd_chat.get('title', '')}) ကို မှတ်သားပြီးပါပြီ!\n"
                f" Channel ID: {fwd_chat['id']}\n သင့်ကိုလည်း Admin အဖြစ် မှတ်သားထားပါပြီ!",
                reply_to=msg_id,
            )
        )
        return JSONResponse({"ok": True})

    text = (msg.get("text") or msg.get("caption") or "").strip()
    if not chat_id or not user_id or not text:
        return JSONResponse({"ok": True})

    if text.startswith("/admin") and (user_id == RUNTIME.get("admin_id") or not RUNTIME.get("admin_id")):
        if not RUNTIME.get("admin_id"):
            RUNTIME["admin_id"] = user_id
            persist_state()
        asyncio.create_task(
            send_text(
                chat_id,
                f" VIP Music Bot Admin Panel\n\n"
                f" စုစုပေါင်း အသုံးပြုသူ: {len(KNOWN_USERS)} ဦး\n"
                f" ပို့ပေးပြီးသော MP3 ပေါင်း: {RUNTIME['songs_sent']} ပုဒ်\n"
                f" Channel ပေါ် ပြန်တင်ထားမှု: {RUNTIME['channel_shares']} ကြိမ်\n"
                f" Force-Join Channel ID: {RUNTIME['channel_id'] or 'မချိတ်ရသေးပါ'}\n\n"
                f" လူအားလုံးထံ စာပို့လိုပါက \n/broadcast မင်္ဂလာပါ သီချင်းအသစ်တွေ ဒေါင်းလို့ရပြီနော်!",
                reply_to=msg_id,
            )
        )
        return JSONResponse({"ok": True})

    if text.startswith("/broadcast ") and user_id == RUNTIME.get("admin_id"):
        b_msg = text[len("/broadcast ") :].strip()
        if b_msg:
            asyncio.create_task(broadcast_to_users(chat_id, b_msg))
        return JSONResponse({"ok": True})

    if not await is_user_joined(user_id):
        asyncio.create_task(send_text(chat_id, JOIN_PROMPT_TEXT, reply_to=msg_id, reply_markup=join_gate_keyboard()))
        return JSONResponse({"ok": True})

    if text.startswith("/start") or text.startswith("/menu"):
        asyncio.create_task(send_text(chat_id, MAIN_MENU_TEXT, reply_to=msg_id, reply_markup=main_menu_keyboard()))
        return JSONResponse({"ok": True})

    url_match = re.search(r"https?://[^\s<>\"']+", text)
    if url_match:
        raw_url = url_match.group(0).rstrip(".,!?;:)")
        asyncio.create_task(send_single_mp3(chat_id, user_id, raw_url, "", reply_to=msg_id))
        return JSONResponse({"ok": True})

    mode = USER_MODE.get(user_id)
    if not mode:
        asyncio.create_task(
            send_text(
                chat_id,
                " အရင်ဆုံး အဆိုတော်နာမည် နဲ့ ရှာမလား၊ သီချင်းနာမည် နဲ့ ရှာမလား ရွေးပေးပါနော် ",
                reply_to=msg_id,
                reply_markup=main_menu_keyboard(),
            )
        )
        return JSONResponse({"ok": True})

    if mode == "artist":
        asyncio.create_task(handle_artist_search(chat_id, user_id, text, reply_to=msg_id))
    else:
        asyncio.create_task(handle_song_title_search(chat_id, user_id, text, reply_to=msg_id))

    return JSONResponse({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
