import os, re, json, glob, asyncio, subprocess, tempfile, threading, textwrap
from http.server import BaseHTTPRequestHandler, HTTPServer

import yt_dlp
from groq import Groq
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
LLM_MODEL = os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile")
MAX_UPLOAD = 49 * 1024 * 1024
YT_RE = re.compile(r"(https?://(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/\S+)")
NUM_CLIPS = 3


# ---------- Render health server ----------
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass

def start_health_server():
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), Health).serve_forever()


# ---------- Pipeline ----------
def download(url, d):
    opts = {
        "format": "best[height<=720]/bestvideo[height<=720]+bestaudio/best",
        "outtmpl": f"{d}/video.%(ext)s",
        "merge_output_format": "mp4",
        "quiet": True,
        "noplaylist": True,
    }
    if os.environ.get("COOKIES_FILE"):
        opts["cookiefile"] = os.environ["COOKIES_FILE"]
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    return glob.glob(f"{d}/video.*")[0]


def transcribe(video, d):
    audio = f"{d}/audio.mp3"
    subprocess.run(["ffmpeg", "-y", "-i", video, "-vn", "-ac", "1", "-ar", "16000",
                    "-b:a", "32k", audio], check=True, capture_output=True)
    with open(audio, "rb") as f:
        resp = groq_client.audio.transcriptions.create(
            file=("audio.mp3", f.read()),
            model="whisper-large-v3-turbo",
            response_format="verbose_json",
        )
    segs = resp.segments if hasattr(resp, "segments") else resp["segments"]
    g = lambda s, k: s[k] if isinstance(s, dict) else getattr(s, k)
    return [(g(s, "start"), g(s, "end"), g(s, "text").strip()) for s in segs]


VIRAL_PROMPT = """Tum ek expert short-form video editor ho (YouTube Shorts / Reels / TikTok).
Neeche video ka transcript hai (timestamps seconds mein).

Aise {n_cand} clips chuno jinhe sabse zyada log dekhenge aur share karenge.

Viral clip ke rules:
1. HOOK: pehle 3 second mein hi attention pakde (shocking claim, sawal, strong opinion, curiosity). Intro/"hello guys" wala hissa nahi.
2. STANDALONE: bina poora video dekhe samajh aa jaye. Ek complete idea/story/punchline.
3. EMOTION ya VALUE: funny, surprising, controversial, inspiring, ya koi kaam ki tip/secret.
4. CLEAN END: baat poori hone par khatam ho, beech sentence mein nahi.
5. LENGTH: 20-55 second. Clips overlap na karein.

Har clip ke liye score 1-10 do (virality potential) aur kam score wale clips mat rakho.

Sirf JSON array do, aur kuch nahi:
[{{"start": 12.0, "end": 48.5, "score": 9, "hook_title": "catchy title (max 8 words)", "hashtags": "#tag1 #tag2 #tag3", "reason": "ek line mein kyun viral hoga"}}]

Transcript:
{transcript}"""


def pick_clips(segments):
    transcript = "\n".join(f"[{s:.1f}-{e:.1f}] {t}" for s, e, t in segments)
    resp = groq_client.chat.completions.create(
        model=LLM_MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": VIRAL_PROMPT.format(
            n_cand=NUM_CLIPS + 2, transcript=transcript[:30000])}],
    )
    text = resp.choices[0].message.content
    clips = json.loads(re.search(r"\[.*\]", text, re.S).group(0))
    clips.sort(key=lambda c: c.get("score", 0), reverse=True)
    return clips[:NUM_CLIPS]


def snap(clip, segments):
    """Clip ko sentence boundaries par snap karo taaki beech mein na kate."""
    start, end = float(clip["start"]), float(clip["end"])
    for s, e, _ in segments:
        if s <= start < e:
            start = s
        if s < end <= e:
            end = e
    return start, end


def fmt_ts(t):
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02}:{int(m):02}:{int(s):02},{int((s % 1) * 1000):03}"


def make_srt(segments, start, end, path):
    lines, idx = [], 1
    for s, e, text in segments:
        if e <= start or s >= end:
            continue
        a, b = max(s, start) - start, min(e, end) - start
        wrapped = "\n".join(textwrap.wrap(text, 28)[:3])
        lines.append(f"{idx}\n{fmt_ts(a)} --> {fmt_ts(b)}\n{wrapped}\n")
        idx += 1
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def cut_vertical(video, start, end, srt, out):
    style = "FontSize=18,Bold=1,Outline=2,Alignment=2,MarginV=70"
    vf = (f"crop='min(iw,ih*9/16)':ih,scale=720:1280,"
          f"subtitles={srt}:force_style='{style}'")
    subprocess.run(
        ["ffmpeg", "-y", "-ss", str(start), "-i", video, "-t", str(end - start),
         "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
         "-c:a", "aac", "-movflags", "+faststart", out],
        check=True, capture_output=True,
    )


def process(url, d):
    video = download(url, d)
    segments = transcribe(video, d)
    clips = pick_clips(segments)
    results = []
    for i, c in enumerate(clips, 1):
        start, end = snap(c, segments)
        srt, out = f"{d}/c{i}.srt", f"{d}/clip{i}.mp4"
        make_srt(segments, start, end, srt)
        cut_vertical(video, start, end, srt, out)
        caption = (f"🔥 {c.get('hook_title', f'Clip {i}')}\n"
                   f"Score: {c.get('score', '?')}/10\n"
                   f"{c.get('hashtags', '')}\n\n💡 {c.get('reason', '')}")
        results.append((out, caption[:1000]))
    return results


# ---------- Telegram ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Mujhe koi bhi YouTube link bhejo, main uske sabse viral clips "
        "(vertical + captions ke saath) nikal dunga 🎬")


async def handle_link(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = YT_RE.search(update.message.text or "")
    if not m:
        return
    await update.message.reply_text("Video analyze kar raha hoon... 3-6 min lag sakte hain ⏳")
    with tempfile.TemporaryDirectory() as d:
        try:
            results = await asyncio.to_thread(process, m.group(1), d)
        except Exception as e:
            await update.message.reply_text(f"Error: {str(e)[:300]}")
            return
        for path, caption in results:
            if os.path.getsize(path) > MAX_UPLOAD:
                await update.message.reply_text("Ek clip 50MB se bada tha, skip kiya.")
                continue
            with open(path, "rb") as f:
                await update.message.reply_video(f, caption=caption)


def main():
    threading.Thread(target=start_health_server, daemon=True).start()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    app.run_polling()

if __name__ == "__main__":
    main()
