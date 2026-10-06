"""Miniature YouTube style « clic » : vrai visage détouré + décor IA + gros texte."""
import base64, json, os, subprocess
from pathlib import Path
import requests
import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

AN_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OA_KEY = os.environ.get("OPENAI_API_KEY", "")
OA_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1")
FONT = "/usr/share/fonts/anton/Anton-Regular.ttf"
W, H = 1280, 720
_RB = None

def _font(sz):
    try: return ImageFont.truetype(FONT, sz)
    except Exception: return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", sz)

def frames(raw, off, dur, work, n=8):
    out = []
    for k in range(n):
        t = off + dur * (k + 0.5) / n
        p = work / f"cand{k}.jpg"
        subprocess.run(["ffmpeg", "-y", "-ss", f"{t:.2f}", "-i", str(raw), "-frames:v", "1", "-vf", "scale=1280:-2", "-q:v", "2", str(p)],
                       capture_output=True, timeout=60)
        if p.exists() and p.stat().st_size > 0: out.append(p)
    return out

def pick(fr, titre, source, errs):
    if not AN_KEY or not fr: return 0, 0.5, 0.25
    content = []
    for i, p in enumerate(fr):
        content += [{"type": "text", "text": f"Image {i}"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(p.read_bytes()).decode()}}]
    content.append({"type": "text", "text": f"Extrait : « {titre} » ({source}). Choisis l'image la plus forte pour une miniature YouTube : "
        "la personne principale (celle qui parle) en gros plan, visage net et bien éclairé, expression intense (bouche ouverte, sourcils froncés, "
        "indignation, surprise, colère), yeux ouverts, pas de plan large, le moins de texte incrusté possible. Donne la position horizontale du centre "
        "de son visage (0 = bord gauche, 1 = bord droit) et la largeur de sa tête en fraction de la largeur de l'image. "
        'Réponds UNIQUEMENT en JSON : {"image":0,"x":0.5,"largeur":0.2}'})
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=120,
                          headers={"x-api-key": AN_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                          json={"model": "claude-sonnet-4-5", "max_tokens": 200, "messages": [{"role": "user", "content": content}]})
        txt = r.json()["content"][0]["text"]; j = json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
        return max(0, min(len(fr) - 1, int(j["image"]))), float(j.get("x", 0.5)), float(j.get("largeur", 0.25))
    except Exception as e:
        errs.append(f"choix image: {str(e)[:150]}"); return 0, 0.5, 0.25

def cutout(img, x, w, errs):
    global _RB
    try:
        from rembg import remove, new_session
        if _RB is None: _RB = new_session("u2net_human_seg")
        Wi, Hi = img.size
        half = max(0.2, min(0.38, w * 1.7))
        l = int(max(0, (x - half) * Wi)); r = int(min(Wi, (x + half) * Wi))
        cut = remove(img.crop((l, 0, r, Hi)), session=_RB)
        a = np.array(cut.getchannel("A"))
        if (a > 40).mean() < 0.06: errs.append("détourage trop petit"); return None
        bb = Image.fromarray(((a > 40) * 255).astype("uint8")).getbbox()
        return cut.crop(bb) if bb else None
    except Exception as e:
        errs.append(f"détourage: {str(e)[:150]}"); return None

def decor(prompt, out, errs):
    if not OA_KEY or not prompt: return None
    try:
        r = requests.post("https://api.openai.com/v1/images/generations", timeout=240,
                          headers={"Authorization": f"Bearer {OA_KEY}", "Content-Type": "application/json"},
                          json={"model": OA_MODEL, "n": 1, "size": "1536x1024", "quality": "medium",
                                "prompt": prompt + ". Cinematic, dramatic lighting, high contrast, saturated colors, background for a YouTube thumbnail, "
                                          "no people, no faces, no text, no letters, no logos, no blood, no weapons."})
        if r.status_code != 200: errs.append(f"décor IA {r.status_code}: {r.text[:200]}"); return None
        out.write_bytes(base64.b64decode(r.json()["data"][0]["b64_json"]))
        return Image.open(out).convert("RGB")
    except Exception as e:
        errs.append(f"décor IA: {str(e)[:150]}"); return None

def _wrap(txt, maxw, maxlines=3):
    words = txt.split()
    for sz in range(168, 60, -6):
        f = _font(sz); lines = [""]
        for w_ in words:
            t = (lines[-1] + " " + w_).strip()
            if f.getlength(t) <= maxw or not lines[-1]: lines[-1] = t
            else: lines.append(w_)
        if len(lines) <= maxlines and all(f.getlength(l) <= maxw for l in lines): return f, lines, sz
    f = _font(60); return f, [txt], 60

def compose(frame_img, bg, cut, texte, label, out):
    if bg is None:
        bg = frame_img.filter(ImageFilter.GaussianBlur(18))
        bg = ImageEnhance.Brightness(bg).enhance(0.7)
    bg = ImageOps.fit(bg.convert("RGB"), (W, H))
    bg = ImageEnhance.Color(ImageEnhance.Contrast(bg).enhance(1.15)).enhance(1.3)
    if cut is None:  # pas de détourage : image réelle en plein cadre
        bg = ImageEnhance.Color(ImageEnhance.Contrast(ImageOps.fit(frame_img.convert("RGB"), (W, H))).enhance(1.15)).enhance(1.3)
    canvas = bg.convert("RGBA")
    # assombrir la gauche pour le texte
    xs = np.linspace(1, 0, W); al = np.clip((xs - 0.25) * 1.6, 0, 0.85)
    shade = Image.fromarray((np.tile(al, (H, 1)) * 255).astype("uint8"))
    canvas = Image.composite(Image.new("RGBA", (W, H), (0, 0, 0, 255)), canvas, shade)
    if cut is not None:
        s = H * 1.08 / cut.height
        if cut.width * s > W * 0.62: s = W * 0.62 / cut.width
        cut = cut.resize((int(cut.width * s), int(cut.height * s)), Image.LANCZOS)
        rgb = ImageEnhance.Sharpness(ImageEnhance.Color(ImageEnhance.Contrast(cut.convert("RGB")).enhance(1.12)).enhance(1.2)).enhance(1.4)
        cut = Image.merge("RGBA", (*rgb.split(), cut.getchannel("A")))
        x0 = W - cut.width + int(cut.width * 0.04); y0 = H - cut.height + int(H * 0.06)
        a = cut.getchannel("A")
        glow = a.filter(ImageFilter.MaxFilter(17)).filter(ImageFilter.GaussianBlur(2))
        shadow = a.filter(ImageFilter.GaussianBlur(25))
        canvas.paste(Image.new("RGBA", cut.size, (0, 0, 0, 200)), (x0 - 12, y0 + 6), shadow)
        canvas.paste(Image.new("RGBA", cut.size, (255, 255, 255, 255)), (x0, y0), glow)
        canvas.paste(cut, (x0, y0), a)
    d = ImageDraw.Draw(canvas)
    maxw = int(W * (0.56 if cut is not None else 0.9))
    f, lines, sz = _wrap(texte.upper(), maxw)
    lh = int(sz * 1.05); y = (H - lh * len(lines)) // 2 + 30
    for k, l in enumerate(lines):
        col = (255, 230, 0) if k % 2 == 0 else (255, 255, 255)
        d.text((48 + 6, y + 8), l, font=f, fill=(0, 0, 0, 160))
        d.text((48, y), l, font=f, fill=col, stroke_width=max(6, sz // 13), stroke_fill=(0, 0, 0))
        y += lh
    if label:
        lf = _font(38); tw = int(lf.getlength(label))
        d.rectangle([40, 34, 40 + tw + 28, 34 + 58], fill=(224, 0, 46))
        d.text((54, 38), label, font=lf, fill=(255, 255, 255))
    canvas.convert("RGB").save(out, "JPEG", quality=90)
    return out
