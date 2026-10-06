"""Publication TikTok (Login Kit + Content Posting API) — page web de l'éditeur."""
import html, json, os, secrets, time, urllib.parse
from pathlib import Path
import requests
from fastapi import APIRouter, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

CK = os.environ.get("TIKTOK_CLIENT_KEY", "")
CS = os.environ.get("TIKTOK_CLIENT_SECRET", "")
PAGE_KEY = os.environ.get("TIKTOK_PAGE_KEY", "")
BASE = "https://n8n.actualitevideo.fr"
REDIRECT = f"{BASE}/webhook/tiktok-callback"
SCOPES = "user.info.basic,video.upload,video.publish"
API = "https://open.tiktokapis.com/v2"

def setup(app, DATA, auth, sign_url):
    KEEP = DATA / "keep"; KEEP.mkdir(exist_ok=True)
    TOK = KEEP / "tiktok_tokens.json"; Q = KEEP / "tiktok_queue.json"; ST = KEEP / "tiktok_states.json"
    r = APIRouter()
    esc = lambda s: html.escape(str(s or ""))

    def jload(p, d):
        try: return json.loads(p.read_text())
        except Exception: return d

    def check_key(k):
        if not PAGE_KEY or not secrets.compare_digest(k or "", PAGE_KEY): raise HTTPException(403, "Accès refusé")

    def token():
        t = jload(TOK, {})
        if not t: return None
        if t.get("expires_at", 0) < time.time() + 120:
            rr = requests.post(f"{API}/oauth/token/", timeout=30, headers={"Content-Type": "application/x-www-form-urlencoded"},
                               data={"client_key": CK, "client_secret": CS, "grant_type": "refresh_token", "refresh_token": t.get("refresh_token", "")}).json()
            if "access_token" not in rr: return None
            t.update(access_token=rr["access_token"], refresh_token=rr.get("refresh_token", t.get("refresh_token")),
                     expires_at=time.time() + rr.get("expires_in", 86400))
            TOK.write_text(json.dumps(t))
        return t

    def tt(path, at, body=None):
        return requests.post(f"{API}{path}", timeout=60, json=body or {},
                             headers={"Authorization": f"Bearer {at}", "Content-Type": "application/json; charset=UTF-8"}).json()

    CSS = """<style>:root{--bg:#f6f6f7;--card:#fff;--ink:#161823;--mut:#6b6b76;--red:#fe2c55;--line:#e3e3e6}
body{margin:0;background:var(--bg);color:var(--ink);font-family:system-ui,-apple-system,sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:16px}.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin:14px 0}
h1{font-size:1.35em;margin:6px 0}h2{font-size:1.05em;margin:0 0 10px}.mut{color:var(--mut);font-size:.9em}
.btn{display:inline-block;border:0;border-radius:8px;padding:12px 16px;font-size:1em;font-weight:600;cursor:pointer;text-decoration:none}
.pri{background:var(--red);color:#fff}.sec{background:#fff;color:var(--ink);border:1px solid var(--line)}
video{width:100%;max-height:520px;background:#000;border-radius:10px}textarea,select{width:100%;box-sizing:border-box;font-size:1em;padding:10px;border:1px solid var(--line);border-radius:8px}
label.row{display:flex;align-items:center;gap:10px;margin:8px 0}.acct{display:flex;align-items:center;gap:12px}.acct img{width:48px;height:48px;border-radius:50%}
.box{background:#fafafa;border:1px dashed var(--line);border-radius:8px;padding:10px;margin-top:8px}.ok{color:#0a7d33}.err{color:#c4002b}</style>"""
    def page(body, title="L'Extrait politique – TikTok"):
        return HTMLResponse(f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{title}</title>{CSS}</head><body><div class='wrap'><h1>L'Extrait politique · Publish to TikTok</h1>{body}<p class='mut'><a href='{BASE}/webhook/confidentialite'>Privacy</a> · <a href='{BASE}/webhook/conditions'>Terms</a></p></div></body></html>")

    @r.post("/tiktok/queue")
    def queue(item: dict, x_token: str = Header("")):
        auth(x_token)
        q = jload(Q, [])
        it = {"id": secrets.token_hex(5), "fichier": Path(item.get("fichier", "")).name, "legende": item.get("legende", "")[:2200],
              "titre": item.get("titre", ""), "ajoute": time.time(), "statut": "à publier"}
        q = [x for x in q if x["fichier"] != it["fichier"]][-29:] + [it]
        Q.write_text(json.dumps(q, ensure_ascii=False))
        return {"id": it["id"], "lien": f"{BASE}/webhook/tiktok?k={PAGE_KEY}&v={it['id']}"}

    @r.get("/webhook/tiktok-connect")
    def connect(k: str = ""):
        check_key(k)
        st = secrets.token_urlsafe(16); s = jload(ST, {}); s[st] = time.time(); ST.write_text(json.dumps(s))
        q = urllib.parse.urlencode({"client_key": CK, "scope": SCOPES, "response_type": "code", "redirect_uri": REDIRECT, "state": st})
        return RedirectResponse(f"https://www.tiktok.com/v2/auth/authorize/?{q}")

    @r.get("/webhook/tiktok-callback")
    def callback(code: str = "", state: str = "", error: str = "", error_description: str = ""):
        s = jload(ST, {})
        if error: return page(f"<div class='card err'>TikTok authorization refused: {esc(error_description or error)}</div>")
        if state not in s: raise HTTPException(400, "state invalide")
        s.pop(state); ST.write_text(json.dumps(s))
        rr = requests.post(f"{API}/oauth/token/", timeout=30, headers={"Content-Type": "application/x-www-form-urlencoded"},
                           data={"client_key": CK, "client_secret": CS, "code": code, "grant_type": "authorization_code", "redirect_uri": REDIRECT}).json()
        if "access_token" not in rr: return page(f"<div class='card err'>Token error: {esc(rr)}</div>")
        t = {"access_token": rr["access_token"], "refresh_token": rr.get("refresh_token"), "open_id": rr.get("open_id"),
             "scope": rr.get("scope"), "expires_at": time.time() + rr.get("expires_in", 86400)}
        try:
            u = requests.get(f"{API}/user/info/?fields=open_id,avatar_url,display_name", timeout=30,
                             headers={"Authorization": f"Bearer {t['access_token']}"}).json().get("data", {}).get("user", {})
            t.update(display_name=u.get("display_name"), avatar_url=u.get("avatar_url"))
        except Exception: pass
        TOK.write_text(json.dumps(t))
        return RedirectResponse(f"{BASE}/webhook/tiktok?k={PAGE_KEY}")

    @r.get("/webhook/tiktok")
    def home(k: str = "", v: str = ""):
        check_key(k)
        t = token()
        if not t:
            return page(f"<div class='card'><h2>Connect your TikTok account</h2><p class='mut'>Authorize L'Extrait politique to publish the videos you choose to your TikTok account.</p><a class='btn pri' href='{BASE}/webhook/tiktok-connect?k={k}'>Continue with TikTok</a></div>")
        ci = tt("/post/publish/creator_info/query/", t["access_token"]).get("data", {})
        acct = f"<div class='card acct'><img src='{esc(ci.get('creator_avatar_url') or t.get('avatar_url'))}' alt=''><div><b>{esc(ci.get('creator_nickname') or t.get('display_name'))}</b><div class='mut'>@{esc(ci.get('creator_username', ''))} · connected</div></div><div style='margin-left:auto'><a class='btn sec' href='{BASE}/webhook/tiktok-connect?k={k}'>Switch</a></div></div>"
        q = list(reversed(jload(Q, [])))
        if v: q = [x for x in q if x["id"] == v] + [x for x in q if x["id"] != v]
        if not q: return page(acct + "<div class='card'>No video waiting.</div>")
        opts = ci.get("privacy_level_options") or ["SELF_ONLY"]
        lab = {"PUBLIC_TO_EVERYONE": "Everyone", "MUTUAL_FOLLOW_FRIENDS": "Friends", "FOLLOWER_OF_CREATOR": "Followers", "SELF_ONLY": "Only me"}
        maxd = ci.get("max_video_post_duration_sec")
        cards = []
        for it in q[:8]:
            f = it["fichier"]
            if not (DATA / f).exists(): continue
            done = it.get("statut") != "à publier"
            dis = lambda key: "disabled" if ci.get(key) else ""
            cards.append(f"""<div class='card'><h2>{esc(it.get('titre') or f)}</h2>
<video controls preload='metadata' playsinline src='{esc(sign_url(f))}'></video>
<p class='mut'>Status: <b>{esc(it.get('statut'))}</b>{(' · max duration allowed: ' + str(maxd) + ' s') if maxd else ''}</p>
<form method='post' action='{BASE}/webhook/tiktok-post' onsubmit="return chk(this,event)">
<input type='hidden' name='k' value='{esc(k)}'><input type='hidden' name='id' value='{esc(it['id'])}'>
<label>Caption</label><textarea name='titre' rows='4' maxlength='2200'>{esc(it.get('legende'))}</textarea>
<label style='display:block;margin-top:10px'>Who can view this video</label>
<select name='privacy' required><option value='' selected disabled>Select privacy level</option>{''.join(f"<option value='{o}'>{lab.get(o, o)}</option>" for o in opts)}</select>
<div style='margin-top:10px'><b>Allow users to</b>
<label class='row'><input type='checkbox' name='comment' {dis('comment_disabled')}> Comment</label>
<label class='row'><input type='checkbox' name='duet' {dis('duet_disabled')}> Duet</label>
<label class='row'><input type='checkbox' name='stitch' {dis('stitch_disabled')}> Stitch</label></div>
<div class='box'><label class='row'><input type='checkbox' name='disclose' onchange="this.form.querySelector('.cc').style.display=this.checked?'block':'none'"> <b>Disclose video content</b></label>
<div class='cc' style='display:none'><p class='mut'>Turn on to disclose that this video promotes goods or services in exchange for something of value.</p>
<label class='row'><input type='checkbox' name='yourbrand'> Your brand <span class='mut'>– labeled “Promotional content”</span></label>
<label class='row'><input type='checkbox' name='branded'> Branded content <span class='mut'>– labeled “Paid partnership”</span></label></div></div>
<p class='mut'>By posting, you agree to TikTok's <a href='https://www.tiktok.com/legal/page/global/music-usage-confirmation/en'>Music Usage Confirmation</a><span class='bc'></span>.</p>
<button class='btn pri' name='mode' value='direct' {'disabled' if done else ''}>Post to TikTok</button>
<button class='btn sec' name='mode' value='draft' {'disabled' if done else ''} formnovalidate>Send to TikTok drafts</button>
</form></div>""")
        js = """<script>function chk(f,e){var m=e&&e.submitter&&e.submitter.value;if(m==='draft')return true;
if(!f.privacy.value){alert('Please select who can view this video.');return false}
if(f.disclose.checked&&!f.yourbrand.checked&&!f.branded.checked){alert('You need to indicate if your content promotes yourself, a third party, or both.');return false}
if(f.branded.checked&&f.privacy.value==='SELF_ONLY'){alert('Branded content visibility cannot be set to private.');return false}return true}</script>"""
        return page(acct + "".join(cards) + js)

    @r.post("/webhook/tiktok-post")
    def post(k: str = Form(""), id: str = Form(""), mode: str = Form("draft"), titre: str = Form(""), privacy: str = Form(""),
             comment: str = Form(None), duet: str = Form(None), stitch: str = Form(None), disclose: str = Form(None),
             yourbrand: str = Form(None), branded: str = Form(None)):
        check_key(k)
        t = token()
        if not t: return RedirectResponse(f"{BASE}/webhook/tiktok?k={k}", status_code=303)
        q = jload(Q, []); it = next((x for x in q if x["id"] == id), None)
        if not it: raise HTTPException(404, "vidéo inconnue")
        if it.get("statut") != "à publier": return page(f"<div class='card'>Already sent ({esc(it.get('statut'))}).</div><a class='btn sec' href='{BASE}/webhook/tiktok?k={k}'>Back</a>")
        p = DATA / it["fichier"]
        if not p.exists(): raise HTTPException(404, "fichier expiré")
        size = p.stat().st_size
        chunk = size if size <= 64 * 1024 * 1024 else 10 * 1024 * 1024
        n = 1 if chunk == size else size // chunk
        src = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": n}
        if mode == "direct":
            body = {"post_info": {"title": titre[:2200], "privacy_level": privacy, "disable_comment": not comment, "disable_duet": not duet,
                                  "disable_stitch": not stitch, "video_cover_timestamp_ms": 1000,
                                  "brand_content_toggle": bool(disclose and branded), "brand_organic_toggle": bool(disclose and yourbrand)},
                    "source_info": src}
            res = tt("/post/publish/video/init/", t["access_token"], body)
        else:
            res = tt("/post/publish/inbox/video/init/", t["access_token"], {"source_info": src})
        d = res.get("data", {}); errc = (res.get("error") or {}).get("code")
        if not d.get("upload_url"):
            return page(f"<div class='card err'>TikTok error: {esc(res.get('error'))}</div><a class='btn sec' href='{BASE}/webhook/tiktok?k={k}'>Back</a>")
        with open(p, "rb") as fh:
            for i in range(n):
                a = i * chunk; b = size - 1 if i == n - 1 else a + chunk - 1
                fh.seek(a); data = fh.read(b - a + 1)
                up = requests.put(d["upload_url"], data=data, timeout=600,
                                  headers={"Content-Type": "video/mp4", "Content-Range": f"bytes {a}-{b}/{size}"})
                if up.status_code not in (200, 201, 206):
                    return page(f"<div class='card err'>Upload error {up.status_code}: {esc(up.text[:300])}</div>")
        it["statut"] = "publication en cours" if mode == "direct" else "envoyé dans les brouillons"
        it["publish_id"] = d.get("publish_id"); Q.write_text(json.dumps(q, ensure_ascii=False))
        msg = ("Your video is being posted. It may take a few minutes to appear on your profile." if mode == "direct"
               else "Your video was sent to your TikTok inbox. Open the TikTok app notification to finish editing and post it.")
        return page(f"<div class='card ok'><b>✓ Sent to TikTok.</b><p>{msg}</p><p class='mut'>publish_id: {esc(d.get('publish_id'))}</p></div><a class='btn sec' href='{BASE}/webhook/tiktok-status?k={k}&id={esc(id)}'>Check status</a> <a class='btn sec' href='{BASE}/webhook/tiktok?k={k}'>Back</a>")

    @r.get("/webhook/tiktok-status")
    def status(k: str = "", id: str = ""):
        check_key(k)
        t = token(); q = jload(Q, []); it = next((x for x in q if x["id"] == id), None)
        if not t or not it or not it.get("publish_id"): raise HTTPException(404, "inconnu")
        res = tt("/post/publish/status/fetch/", t["access_token"], {"publish_id": it["publish_id"]})
        st = (res.get("data") or {}).get("status", "?")
        if st == "PUBLISH_COMPLETE": it["statut"] = "publié"; Q.write_text(json.dumps(q, ensure_ascii=False))
        return page(f"<div class='card'><b>Status:</b> {esc(st)}<p class='mut'>{esc((res.get('data') or {}).get('fail_reason', ''))}</p></div><a class='btn sec' href='{BASE}/webhook/tiktok-status?k={k}&id={esc(id)}'>Refresh</a> <a class='btn sec' href='{BASE}/webhook/tiktok?k={k}'>Back</a>")

    app.include_router(r)
