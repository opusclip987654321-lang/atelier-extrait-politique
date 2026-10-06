"""Atelier vidéo Impacteur - yt-dlp + Deepgram + ffmpeg, piloté par n8n (réseau Docker interne)."""
import hmac, hashlib, json, os, random, re, string, subprocess, time, glob, threading
from pathlib import Path
import requests
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import FileResponse
from pydantic import BaseModel

DATA = Path(os.environ.get("DATA_DIR", "/data")); DATA.mkdir(parents=True, exist_ok=True)
TOKEN = os.environ.get("ATELIER_TOKEN", "")
PROXY = os.environ.get("PROXY_URL", "")
DG_KEY = os.environ.get("DEEPGRAM_API_KEY", "")
EL_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
EL_VOICE = os.environ.get("ELEVEN_VOICE_ID", "")
EL_MODEL = os.environ.get("ELEVEN_MODEL", "eleven_multilingual_v2")
app = FastAPI(title="Atelier vidéo")
_lock = threading.Lock()

def auth(tok):
    if TOKEN and tok != TOKEN: raise HTTPException(401, "token invalide")

def sticky_proxy():
    if not PROXY: return None
    sid = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return re.sub(r"^(https?://[^:]+:[^@]+)@", rf"\1_session-{sid}_lifetime-10m@", PROXY)

def ytdlp(args, tries=3):
    last = ""
    for i in range(tries):
        cmd = ["yt-dlp", "--no-warnings", "--no-playlist"]
        p = sticky_proxy()
        if p: cmd += ["--proxy", p]
        r = subprocess.run(cmd + args, capture_output=True, text=True, timeout=1200)
        if r.returncode == 0: return r.stdout
        last = (r.stderr or r.stdout)[-600:]
        time.sleep(3)
    raise HTTPException(502, f"yt-dlp: {last}")

def _has_video(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return "video" in r.stdout

def cleanup():
    lim = time.time() - 48 * 3600
    for f in DATA.glob("*"):
        if f.is_file() and f.stat().st_mtime < lim: f.unlink(missing_ok=True)

def _subs_transcript(vid, tj):
    """Sous-titres (auto) français de YouTube → même format que Deepgram. Retourne False si indisponibles."""
    base = DATA / f"{vid}.subs"
    try:
        ytdlp(["--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", "fr,fr-FR,fr-orig", "--sub-format", "json3",
               "-o", str(base), f"https://www.youtube.com/watch?v={vid}"], tries=2)
    except HTTPException:
        return False
    files = sorted(DATA.glob(f"{vid}.subs*.json3"))
    if not files: return False
    words = []
    for ev in json.loads(files[0].read_text()).get("events", []):
        t0 = ev.get("tStartMs", 0) / 1000; segs = ev.get("segs") or []
        dur = ev.get("dDurationMs", 0) / 1000
        toks = [(t0 + sg.get("tOffsetMs", 0) / 1000, sg.get("utf8", "").strip()) for sg in segs if sg.get("utf8", "").strip()]
        for i, (ts, w) in enumerate(toks):
            te = toks[i + 1][0] if i + 1 < len(toks) else ts + min(0.6, max(0.2, dur / max(1, len(toks))))
            for k, piece in enumerate(w.split()):
                words.append({"word": piece.lower(), "punctuated_word": piece, "start": round(ts, 2), "end": round(max(te, ts + 0.15), 2)})
    for f in files: f.unlink(missing_ok=True)
    if len(words) < 20: return False
    tj.write_text(json.dumps({"source": "youtube_subs", "metadata": {"duration": words[-1]["end"]},
                              "results": {"channels": [{"alternatives": [{"words": words}]}]}}))
    return True

class VideoReq(BaseModel):
    video_id: str
    precis: bool = True   # True = Deepgram (Shorts) ; False = sous-titres YouTube d'abord (zapping, économise le proxy)

class ClipReq(BaseModel):
    video_id: str
    start: float
    end: float
    titre: str = ""
    source: str = ""
    vertical: bool = True
    sous_titres: bool = True

@app.get("/health")
def health():
    v = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True).stdout.strip()
    return {"ok": True, "yt_dlp": v, "proxy": bool(PROXY), "deepgram": bool(DG_KEY)}

@app.post("/update-ytdlp")
def update(x_token: str = Header("")):
    auth(x_token)
    r = subprocess.run(["pip", "install", "-U", "--no-cache-dir", "yt-dlp[default]"], capture_output=True, text=True)
    return {"ok": r.returncode == 0, "log": r.stdout[-300:]}

@app.post("/transcribe")
def transcribe(req: VideoReq, x_token: str = Header("")):
    """Télécharge l'audio, le transcrit avec Deepgram (fr), renvoie les phrases horodatées."""
    auth(x_token); cleanup()
    vid = re.sub(r"[^A-Za-z0-9_-]", "", req.video_id)
    tj = DATA / f"{vid}.transcript.json"
    if tj.exists() and req.precis and json.loads(tj.read_text()).get("source") == "youtube_subs":
        tj.unlink()
    if not tj.exists():
        if req.precis or not _subs_transcript(vid, tj):
            if not list(DATA.glob(f"{vid}.audio.*")):
                ytdlp(["-f", "bestaudio[ext=m4a]/bestaudio", "-o", str(DATA / f"{vid}.audio.%(ext)s"), f"https://www.youtube.com/watch?v={vid}"])
            audio = sorted(DATA.glob(f"{vid}.audio.*"))[0]
            with open(audio, "rb") as f:
                r = requests.post("https://api.deepgram.com/v1/listen",
                    params={"model": "nova-2", "language": "fr", "smart_format": "true", "punctuate": "true", "utterances": "true"},
                    headers={"Authorization": f"Token {DG_KEY}", "Content-Type": "audio/mp4"}, data=f, timeout=1800)
            if r.status_code != 200: raise HTTPException(502, f"Deepgram {r.status_code}: {r.text[:300]}")
            tj.write_text(r.text)
    d = json.loads(tj.read_text())
    words = d["results"]["channels"][0]["alternatives"][0].get("words", [])
    utts, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        txt = w.get("punctuated_word", w["word"])
        gap = (words[i + 1]["start"] - w["end"]) if i + 1 < len(words) else 9
        if txt[-1:] in ".?!" or gap > 0.8 or (cur[-1]["end"] - cur[0]["start"]) > 12:
            utts.append({"start": round(cur[0]["start"], 2), "end": round(cur[-1]["end"], 2),
                         "texte": " ".join(x.get("punctuated_word", x["word"]) for x in cur)})
            cur = []
    duree = d.get("metadata", {}).get("duration", 0)
    return {"video_id": vid, "duree_s": duree, "phrases": utts}

def ass_time(t):
    t = max(0, t); h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"

def esc_ass(s):
    s = s.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")
    s = re.sub(r"\s+([?!:;»])", r"\\h\1", s)
    return re.sub(r"«\s+", r"«\\h", s)

def build_ass(vid, start, end, titre, source, W, H):
    words = []
    tj = DATA / f"{vid}.transcript.json"
    if tj.exists():
        d = json.loads(tj.read_text())
        alt = d["results"]["channels"][0]["alternatives"][0]
        words = [w for w in alt.get("words", []) if w["start"] >= start - 0.05 and w["end"] <= end + 0.3]
    fs = int(H * 0.042)
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,DejaVu Sans,{fs},&H00FFFFFF,&H0000FFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{max(3,fs//12)},2,{8 if H > W else 2},60,60,{int(H*0.685) if H > W else int(H*0.08)},1
Style: Titre,DejaVu Sans,{int(fs*0.95)},&H00FFFFFF,&H00FFFFFF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,3,14,0,8,50,50,{int(H*0.07)},1
Style: Source,DejaVu Sans,{int(fs*0.55)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,40,40,{int(H*0.04)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dur = end - start
    ev = []
    if titre: ev.append(f"Dialogue: 1,{ass_time(0)},{ass_time(dur)},Titre,,0,0,0,,{esc_ass(titre)}")
    if source: ev.append(f"Dialogue: 1,{ass_time(0)},{ass_time(dur)},Source,,0,0,0,,Source : {esc_ass(source)}")
    # groupes de 3-4 mots
    grp = []
    for w in words:
        grp.append(w)
        txt = w.get("punctuated_word", w["word"])
        if len(grp) >= 4 or txt[-1:] in ".?!,":
            a = grp[0]["start"] - start; b = grp[-1]["end"] - start
            ev.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b + 0.05)},Sub,,0,0,0,,{esc_ass(' '.join(x.get('punctuated_word', x['word']) for x in grp)).upper()}")
            grp = []
    if grp:
        a = grp[0]["start"] - start; b = grp[-1]["end"] - start
        ev.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b + 0.05)},Sub,,0,0,0,,{esc_ass(' '.join(x.get('punctuated_word', x['word']) for x in grp)).upper()}")
    p = DATA / f"{vid}_{int(start)}_{int(end)}.ass"
    p.write_text(head + "\n".join(ev) + "\n")
    return p

@app.post("/clip")
def clip(req: ClipReq, x_token: str = Header("")):
    """Télécharge uniquement le segment [start, end], le met en 9:16 avec sous-titres et titre."""
    auth(x_token); cleanup()
    vid = re.sub(r"[^A-Za-z0-9_-]", "", req.video_id)
    if req.end <= req.start or req.end - req.start > 600: raise HTTPException(400, "intervalle invalide")
    pad = 1.0
    s0 = max(0, req.start - pad)
    raw = DATA / f"{vid}_{int(req.start)}_{int(req.end)}.raw.mp4"
    if raw.exists() and not _has_video(raw): raw.unlink()  # fichier en cache sans image : on le retélécharge
    if not raw.exists():
        # parfois YouTube (via le proxy) ne livre que le son : on vérifie l'image et on retente avec un format plus large
        for fmt in ("bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b", "bv*[height<=1080]+ba/b[vcodec!=none]"):
            for f in DATA.glob(f"{vid}_{int(req.start)}_{int(req.end)}.raw.*"): f.unlink()
            ytdlp(["-f", fmt, "--download-sections", f"*{s0:.2f}-{req.end + pad:.2f}", "--force-keyframes-at-cuts",
                   "--merge-output-format", "mp4", "-o", str(raw), f"https://www.youtube.com/watch?v={vid}"])
            if raw.exists() and _has_video(raw): break
        else:
            if raw.exists(): raw.unlink()
            raise HTTPException(502, "source sans image : YouTube n'a livré que le son pour cette vidéo (2 essais)")
    W, H = (1080, 1920) if req.vertical else (1920, 1080)
    ass = build_ass(vid, req.start, req.end, req.titre, req.source, W, H) if req.sous_titres or req.titre else None
    out = DATA / f"{vid}_{int(req.start)}_{int(req.end)}_{'v' if req.vertical else 'h'}.mp4"
    off = req.start - s0
    dur = req.end - req.start
    if req.vertical:
        vf = (f"[0:v]split=2[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=25:2,eq=brightness=-0.08[bg];"
              f"[b]scale={W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
    else:
        vf = f"[0:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2"
    if ass: vf += f",subtitles={ass}"
    vf += "[v]"
    cmd = ["ffmpeg", "-y", "-ss", f"{off:.2f}", "-i", str(raw), "-t", f"{dur:.2f}", "-filter_complex", vf,
           "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)]
    with _lock:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode != 0: raise HTTPException(500, f"ffmpeg: {r.stderr[-600:]}")
    return {"fichier": out.name, "taille_mo": round(out.stat().st_size / 1e6, 2), "duree_s": round(dur, 1)}

@app.get("/files/{name}")
def files(name: str, x_token: str = Header("")):
    auth(x_token)
    p = DATA / Path(name).name
    if not p.exists(): raise HTTPException(404, "introuvable")
    return FileResponse(p, media_type="video/mp4" if p.suffix == ".mp4" else "application/octet-stream", filename=p.name)


PUBLIC_BASE = os.environ.get("PUBLIC_BASE", "https://n8n.actualitevideo.fr/media")

def _sig(name, exp):
    return hmac.new(TOKEN.encode(), f"{name}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]

@app.get("/sign/{name}")
def sign(name: str, hours: int = 24, x_token: str = Header("")):
    """URL publique temporaire (pour Instagram/Facebook qui téléchargent la vidéo eux-mêmes)."""
    auth(x_token)
    name = Path(name).name
    if not (DATA / name).exists(): raise HTTPException(404, "introuvable")
    exp = int(time.time()) + hours * 3600
    return {"url": f"{PUBLIC_BASE}/{name}?exp={exp}&sig={_sig(name, exp)}"}

@app.get("/pub/{name}")
def pub(name: str, exp: int = 0, sig: str = ""):
    name = Path(name).name
    if exp < time.time() or not hmac.compare_digest(sig, _sig(name, exp)): raise HTTPException(403, "lien expiré ou invalide")
    p = DATA / name
    if not p.exists(): raise HTTPException(404, "introuvable")
    return FileResponse(p, media_type="image/jpeg" if p.suffix == ".jpg" else "video/mp4")


# ======================= ZAPPING QUOTIDIEN (16:9) =======================
import uuid
JOBS = DATA / "jobs"; JOBS.mkdir(exist_ok=True)
W16, H16 = 1920, 1080

class Segment(BaseModel):
    video_id: str
    start: float
    end: float
    titre: str = ""
    contexte: str = ""
    source: str = ""
    voix: str = ""

class ZapReq(BaseModel):
    nom: str
    date_label: str
    accroche: str = ""
    voix_intro: str = ""
    voix_outro: str = ""
    voice_id: str = ""
    miniature_texte: str = ""
    decor_prompt: str = ""
    vedette: int = 0
    segments: list[Segment]

def _dur(path):
    try:
        return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                                    capture_output=True, text=True).stdout.strip() or 0)
    except Exception:
        return 0.0

def _tts(text, out, voice_id, errs):
    """Voix off ElevenLabs -> mp3. Retourne la durée (0 si pas de voix)."""
    text = (text or "").strip()
    vid = voice_id or EL_VOICE
    if not text or not EL_KEY or not vid: return 0.0
    try:
        r = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{vid}?output_format=mp3_44100_128",
                          headers={"xi-api-key": EL_KEY, "Content-Type": "application/json"},
                          json={"text": text, "model_id": EL_MODEL, "language_code": "fr",
                                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "style": 0.2, "speed": float(os.environ.get("ELEVEN_SPEED", "1.1"))}},
                          timeout=120)
        if r.status_code != 200:
            errs.append(f"{r.status_code}: {r.text[:200]}"); return 0.0
        out.write_bytes(r.content)
        return _dur(out)
    except Exception as e:
        errs.append(str(e)[:200]); return 0.0

def _ass_head(W, H, styles):
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{styles}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

def _words(vid, start, end):
    tj = DATA / f"{vid}.transcript.json"
    if not tj.exists(): return []
    d = json.loads(tj.read_text())
    alt = d["results"]["channels"][0]["alternatives"][0]
    return [w for w in alt.get("words", []) if w["start"] >= start - 0.05 and w["end"] <= end + 0.3]

def _ass_segment(seg, path):
    dur = seg.end - seg.start
    st = "\n".join([
        "Style: Sub,DejaVu Sans,46,&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,-1,0,0,0,100,100,0,0,3,10,0,2,200,200,60,1",
        "Style: Bandeau,DejaVu Sans,44,&H00FFFFFF,&H00FFFFFF,&H002E10C8,&H002E10C8,-1,0,0,0,100,100,0,0,3,18,0,7,60,440,50,1",
        "Style: Contexte,DejaVu Sans,32,&H00FFFFFF,&H00FFFFFF,&H00000000,&HB0000000,0,0,0,0,100,100,0,0,3,12,0,7,60,440,130,1",
        "Style: Source,DejaVu Sans,26,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,9,40,40,40,1",
        "Style: Marque,DejaVu Sans,26,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,2,0,1,2,1,9,40,40,80,1",
    ])
    ev = []
    if seg.titre: ev.append(f"Dialogue: 2,{ass_time(0)},{ass_time(min(dur, 9))},Bandeau,,0,0,0,,{esc_ass(seg.titre.upper())}")
    if seg.contexte:
        nl = max(1, -(-len(seg.titre or "") // 50)) if seg.titre else 0
        mv = 50 + nl * 64 + 22 if nl else 50
        ev.append(f"Dialogue: 2,{ass_time(0.4)},{ass_time(min(dur, 9))},Contexte,,0,0,{mv},,{esc_ass(seg.contexte)}")
    if seg.source: ev.append(f"Dialogue: 1,{ass_time(0)},{ass_time(dur)},Source,,0,0,0,,Source : {esc_ass(seg.source)}")
    ev.append(f"Dialogue: 1,{ass_time(0)},{ass_time(dur)},Marque,,0,0,0,,L’EXTRAIT POLITIQUE")
    grp = []
    words = _words(seg.video_id, seg.start, seg.end)
    def flush(g):
        a = g[0]["start"] - seg.start; b = g[-1]["end"] - seg.start
        ev.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b + 0.05)},Sub,,0,0,0,,{esc_ass(' '.join(x.get('punctuated_word', x['word']) for x in g))}")
    for w in words:
        grp.append(w)
        if len(grp) >= 7 or w.get("punctuated_word", w["word"])[-1:] in ".?!":
            flush(grp); grp = []
    if grp: flush(grp)
    path.write_text(_ass_head(W16, H16, st) + "\n".join(ev) + "\n")

def _ass_card(lines, path, dur):
    st = "\n".join([
        "Style: Gros,DejaVu Sans,96,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,2,0,1,0,0,5,80,80,0,1",
        "Style: Rouge,DejaVu Sans,56,&H002E10C8,&H002E10C8,&H00000000,&H00000000,-1,0,0,0,100,100,1,0,1,0,0,5,80,80,0,1",
        "Style: Moyen,DejaVu Sans,64,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,2,5,140,140,0,1",
        "Style: Petit,DejaVu Sans,40,&H00D0D0D0,&H00D0D0D0,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,120,120,0,1",
    ])
    ev = []
    for style, txt, y in lines:
        ev.append(f"Dialogue: 0,{ass_time(0)},{ass_time(dur)},{style},,0,0,0,,{{\\pos(960,{y})\\fad(250,250)}}{esc_ass(txt)}")
    path.write_text(_ass_head(W16, H16, st) + "\n".join(ev) + "\n")

ENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p", "-r", "30",
       "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart"]

def _render_card(lines, dur, out, voice=None, bg=None):
    vdur = _dur(voice) if voice else 0
    if vdur: dur = max(dur, vdur + 0.9)
    ass = out.with_suffix(".ass"); _ass_card(lines, ass, dur)
    vin = (["-loop", "1", "-framerate", "30", "-t", f"{dur:.2f}", "-i", str(bg)] if bg else
           ["-f", "lavfi", "-i", f"color=c=0x141112:s={W16}x{H16}:r=30:d={dur:.2f}"])
    ain = ["-i", str(voice)] if vdur else ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    vf = f"scale={W16}:{H16},setsar=1,fps=30,subtitles={ass}"
    af = (f"adelay=350|350,aresample=48000,loudnorm=I=-16:TP=-1.5:LRA=11,apad" if vdur else "anull")
    cmd = ["ffmpeg", "-y"] + vin + ain + ["-t", f"{dur:.2f}", "-vf", vf, "-af", af] + ENC + [str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0: raise RuntimeError("carte: " + r.stderr[-400:])

def _render_segment(seg, idx, workdir):
    vid = re.sub(r"[^A-Za-z0-9_-]", "", seg.video_id)
    pad = 1.0; s0 = max(0, seg.start - pad)
    raw = DATA / f"{vid}_{int(seg.start)}_{int(seg.end)}.h.raw.mp4"
    if not raw.exists():
        ytdlp(["-f", "bv*+ba/b", "-S", "res:720,ext:mp4:m4a",
               "--download-sections", f"*{s0:.2f}-{seg.end + pad:.2f}", "--force-keyframes-at-cuts",
               "--merge-output-format", "mp4", "-o", str(raw), f"https://www.youtube.com/watch?v={vid}"])
    dur = seg.end - seg.start; off = seg.start - s0
    ass = workdir / f"seg{idx:02d}.ass"; _ass_segment(seg, ass)
    out = workdir / f"seg{idx:02d}.mp4"
    fo = max(0, dur - 0.35)
    try:
        wh = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                             "-of", "csv=p=0", str(raw)], capture_output=True, text=True).stdout.strip().split(",")
        ratio = int(wh[0]) / int(wh[1])
    except Exception:
        ratio = 16 / 9
    post = f"setsar=1,fps=30,subtitles={ass},fade=t=in:st=0:d=0.3,fade=t=out:st={fo:.2f}:d=0.35"
    if ratio < 1.5:  # vidéo verticale / carrée : fond flouté plein écran au lieu de bandes noires
        vf = (f"split[a][b];[a]scale={W16}:{H16}:force_original_aspect_ratio=increase,crop={W16}:{H16},boxblur=30:3,eq=brightness=-0.25[bg];"
              f"[b]scale=-2:{H16}[fg];[bg][fg]overlay=(W-w)/2:0," + post)
    else:
        vf = (f"scale={W16}:{H16}:force_original_aspect_ratio=decrease,pad={W16}:{H16}:(ow-iw)/2:(oh-ih)/2:color=black," + post)
    af = f"aresample=48000,loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=in:st=0:d=0.3,afade=t=out:st={fo:.2f}:d=0.35"
    cmd = ["ffmpeg", "-y", "-ss", f"{off:.2f}", "-i", str(raw), "-t", f"{dur:.2f}", "-vf", vf, "-af", af] + ENC + [str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode != 0: raise RuntimeError(f"segment {idx}: " + r.stderr[-400:])
    return out, raw

def _tok(w):
    return re.sub(r"[^\w]+", "", (w or "").lower())

def _find_long(seg, notes):
    """Si l'extrait vient d'un Short vertical, cherche la même séquence dans une vidéo longue (16:9) de la même chaîne."""
    vid = re.sub(r"[^A-Za-z0-9_-]", "", seg.video_id)
    try:
        meta = json.loads(ytdlp(["-j", "--skip-download", f"https://www.youtube.com/watch?v={vid}"], tries=2))
    except Exception:
        return None
    w, h = meta.get("width") or 0, meta.get("height") or 0
    if not w or not h or w / h >= 1.5: return None
    sw = [(_tok(x.get("punctuated_word", x["word"])), x["start"]) for x in _words(vid, seg.start, seg.end)]
    sw = [x for x in sw if x[0]]
    if len(sw) < 8:
        notes.append(f"{seg.source} : Short vertical, pas assez de texte pour retrouver l'original"); return None
    grams = {}
    for k in range(len(sw) - 3):
        grams.setdefault(" ".join(t for t, _ in sw[k:k + 4]), []).append(sw[k][1])
    chan = meta.get("channel_id") or ""
    entries = []
    for tab in ("videos", "streams"):
        try:
            entries += json.loads(ytdlp(["--flat-playlist", "-J", "--playlist-end", "40", f"https://www.youtube.com/channel/{chan}/{tab}"], tries=1)).get("entries") or []
        except Exception:
            pass
    if not entries:
        notes.append(f"{seg.source} : liste des vidéos longues indisponible"); return None
    ttl = set(_tok(x) for x in (meta.get("title") or "").split()) - {""}
    cands = [e for e in entries if e.get("id") and e.get("id") != vid and (e.get("duration") or 0) >= 150]
    cands.sort(key=lambda e: -len(ttl & set(_tok(x) for x in (e.get("title") or "").split())))
    for e in cands[:10]:
        cid = re.sub(r"[^A-Za-z0-9_-]", "", e["id"])
        tj = DATA / f"{cid}.transcript.json"
        if not tj.exists() and not _subs_transcript(cid, tj): continue
        lw = json.loads(tj.read_text())["results"]["channels"][0]["alternatives"][0].get("words", [])
        lt = [(_tok(x.get("punctuated_word", x["word"])), x["start"]) for x in lw]
        lt = [x for x in lt if x[0]]
        votes = {}
        for k in range(len(lt) - 3):
            g = " ".join(t for t, _ in lt[k:k + 4])
            for st in grams.get(g, []):
                off = round(lt[k][1] - st); votes[off] = votes.get(off, 0) + 1
        if not votes: continue
        off, n = max(votes.items(), key=lambda kv: kv[1] + votes.get(kv[0] - 1, 0) + votes.get(kv[0] + 1, 0))
        n = n + votes.get(off - 1, 0) + votes.get(off + 1, 0)
        if n >= max(3, len(grams) * 0.25):
            # affiner : décalage moyen des correspondances autour de off
            fin = [lt[k][1] - st for k in range(len(lt) - 3) for st in grams.get(" ".join(t for t, _ in lt[k:k + 4]), [])
                   if abs(lt[k][1] - st - off) <= 1.5]
            o = sum(fin) / len(fin) if fin else off
            notes.append(f"{seg.source} : Short remplacé par la vidéo longue « {(e.get('title') or '')[:60]} »")
            return Segment(video_id=cid, start=max(0, seg.start + o), end=seg.end + o, titre=seg.titre,
                           contexte=seg.contexte, source=seg.source, voix=seg.voix)
    notes.append(f"{seg.source} : Short vertical, original long introuvable (fond flouté)")
    return None

def _job_write(jid, **kw):
    p = JOBS / f"{jid}.json"
    d = json.loads(p.read_text()) if p.exists() else {}
    d.update(kw); p.write_text(json.dumps(d, ensure_ascii=False))

def _zap_worker(jid, req: ZapReq):
    work = DATA / f"zapwork_{jid}"; work.mkdir(exist_ok=True)
    try:
        parts = []; ok_segs = []; erreurs = []; verr = []; nvoix = 0; longs = []
        intro = work / "intro.mp4"
        v_in = work / "v_intro.mp3"
        has = _tts(req.voix_intro, v_in, req.voice_id, verr) > 0; nvoix += has
        _render_card([("Gros", "L’EXTRAIT POLITIQUE", 430), ("Rouge", f"Le zapping du {req.date_label}", 560),
                      ("Petit", req.accroche[:110], 670)], 4, intro, voice=v_in if has else None)
        parts.append(intro)
        t_cur = _dur(intro)
        for i, seg in enumerate(req.segments):
            _job_write(jid, etat="en_cours", etape=f"segment {i + 1}/{len(req.segments)}")
            rseg = seg
            try:
                rseg = _find_long(seg, longs) or seg
            except Exception as e:
                longs.append(f"{seg.source} : recherche de l'original en échec ({str(e)[-80:]})")
            try:
                try:
                    out, raw = _render_segment(rseg, i, work)
                except Exception:
                    if rseg is seg: raise
                    rseg = seg; out, raw = _render_segment(seg, i, work)
            except Exception as e:
                erreurs.append({"segment": i, "video_id": seg.video_id, "erreur": str(e)[-300:]}); continue
            t_chap = t_cur
            vf = work / f"v{i:02d}.mp3"
            if seg.voix and _tts(seg.voix, vf, req.voice_id, verr) > 0:
                try:
                    bg = work / f"bg{i:02d}.jpg"
                    subprocess.run(["ffmpeg", "-y", "-ss", "1.0", "-i", str(raw), "-frames:v", "1", "-vf",
                                    f"scale={W16}:{H16}:force_original_aspect_ratio=increase,crop={W16}:{H16},boxblur=18:2,eq=brightness=-0.28",
                                    str(bg)], capture_output=True, timeout=60)
                    tr = work / f"tr{i:02d}.mp4"
                    _render_card([("Moyen", seg.titre.upper()[:80], 500), ("Petit", f"Source : {seg.source}"[:90], 640)],
                                 3, tr, voice=vf, bg=bg if bg.exists() else None)
                    parts.append(tr); t_cur += _dur(tr); nvoix += 1
                except Exception as e:
                    verr.append(f"transition {i}: {str(e)[-200:]}")
            parts.append(out); ok_segs.append((i, rseg, raw, t_chap, seg.video_id)); t_cur += _dur(out)
        if not ok_segs: raise RuntimeError("aucun segment n'a pu être monté")
        outro = work / "outro.mp4"
        v_out = work / "v_outro.mp3"
        has = _tts(req.voix_outro, v_out, req.voice_id, verr) > 0; nvoix += has
        _render_card([("Gros", "À DEMAIN, 19H", 450), ("Rouge", "Abonne-toi pour ne rien rater", 580),
                      ("Petit", "L’Extrait politique — le zapping quotidien de la politique française", 680)], 5, outro, voice=v_out if has else None)
        parts.append(outro)
        lst = work / "list.txt"; lst.write_text("".join(f"file '{p}'\n" for p in parts))
        final = DATA / f"{req.nom}.mp4"
        r = subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(final)],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0: raise RuntimeError("concat: " + r.stderr[-400:])
        # chapitres (temps réels)
        chap = [{"t": round(tc), "titre": sg.titre, "video_id": oid, "source": sg.source} for i, sg, raw, tc, oid in ok_segs]
        # miniature 1280x720 à partir du 1er segment
        thumb = DATA / f"{req.nom}.jpg"
        i0, s0g, raw0, _t0, _o0 = ok_segs[0]
        tass = work / "thumb.ass"
        st = "\n".join(["Style: T,DejaVu Sans,78,&H00FFFFFF,&H00FFFFFF,&H00000000,&HC0000000,-1,0,0,0,100,100,0,0,3,16,0,4,50,420,0,1",
                        "Style: B,DejaVu Sans,38,&H00FFFFFF,&H00FFFFFF,&H002E10C8,&H002E10C8,-1,0,0,0,100,100,1,0,3,14,0,7,40,40,36,1"])
        _txt = esc_ass((req.accroche or s0g.titre)[:60]).upper()
        tass.write_text(_ass_head(1280, 720, st) + f"Dialogue: 0,0:00:00.00,0:00:05.00,T,,0,0,0,,{_txt}\nDialogue: 0,0:00:00.00,0:00:05.00,B,,0,0,0,,LE ZAPPING · {esc_ass(req.date_label).upper()}\n")
        mid = 1.0 + (s0g.end - s0g.start) / 2
        terr = []
        try:
            import thumb as TH
            from PIL import Image
            ved = next((x for x in ok_segs if x[0] == req.vedette), ok_segs[0])
            _, vs, vraw, _, _ = ved
            voff = vs.start - max(0, vs.start - 1.0)
            fr = TH.frames(vraw, voff, vs.end - vs.start, work)
            k, fx, fw = TH.pick(fr, vs.titre, vs.source, terr)
            fimg = Image.open(fr[k]).convert("RGB")
            cut = TH.cutout(fimg, fx, fw, terr)
            bg = TH.decor(req.decor_prompt, work / "decor.png", terr)
            TH.compose(fimg, bg, cut, (req.miniature_texte or req.accroche or vs.titre)[:60],
                       f"LE ZAPPING · {req.date_label.upper()}", thumb)
        except Exception as e:
            terr.append(f"miniature: {str(e)[-200:]}")
            subprocess.run(["ffmpeg", "-y", "-ss", f"{mid:.2f}", "-i", str(raw0), "-frames:v", "1",
                            "-vf", f"scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,eq=brightness=-0.12:saturation=1.15,subtitles={tass}",
                            "-q:v", "3", str(thumb)], capture_output=True, text=True, timeout=120)
        dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(final)],
                                   capture_output=True, text=True).stdout.strip() or 0)
        _job_write(jid, etat="termine", fichier=final.name, miniature=thumb.name if thumb.exists() else None,
                   duree_s=round(dur), taille_mo=round(final.stat().st_size / 1e6, 1), chapitres=chap, erreurs=erreurs,
                   miniature_erreurs=terr[:5], originaux_longs=longs[:12], voix={"demandees": bool(req.voix_intro or any(s.voix for s in req.segments)), "generees": int(nvoix), "erreurs": verr[:5]})
    except Exception as e:
        _job_write(jid, etat="erreur", erreur=str(e)[-600:])
    finally:
        subprocess.run(["rm", "-rf", str(work)])

@app.post("/zapping")
def zapping(req: ZapReq, x_token: str = Header("")):
    auth(x_token); cleanup()
    req.nom = re.sub(r"[^A-Za-z0-9_-]", "", req.nom) or "zapping"
    jid = uuid.uuid4().hex[:12]
    _job_write(jid, etat="en_cours", etape="démarrage", nom=req.nom)
    threading.Thread(target=_zap_worker, args=(jid, req), daemon=True).start()
    return {"job": jid}

@app.get("/jobs/{jid}")
def job(jid: str, x_token: str = Header("")):
    auth(x_token)
    p = JOBS / f"{Path(jid).name}.json"
    if not p.exists(): raise HTTPException(404, "job inconnu")
    return json.loads(p.read_text())

@app.get("/files_img/{name}")
def files_img(name: str, x_token: str = Header("")):
    auth(x_token)
    p = DATA / Path(name).name
    if not p.exists(): raise HTTPException(404, "introuvable")
    return FileResponse(p, media_type="image/jpeg")


# ======================= TIKTOK =======================
import tiktok as _TK
_TK.setup(app, DATA, auth, lambda f: (lambda e: f"{PUBLIC_BASE}/{f}?exp={e}&sig={_sig(f, e)}")(int(time.time()) + 6 * 3600))
