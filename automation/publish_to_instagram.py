#!/usr/bin/env python3
"""Publish pending jobs/*.json to Instagram via graph.instagram.com (IG-login flavour).
Runs inside GitHub Actions. Media must be committed to this repo (served by Pages).
Job schema: {"type":"reel|image|carousel|dry_run","media":"media/reels/x.mp4",
             "cover":"media/reels/x_cover.png","caption":"...","share_to_feed":true,
             "children":["media/images/1.png", ...]}
Result written to results/<jobname>.json — never fails the workflow; errors go in the result.
"""
import json, os, sys, time, glob, urllib.request, urllib.parse, urllib.error

API = "https://graph.instagram.com/v23.0"
TOKEN = os.environ["IG_TOKEN"].strip()
IGID = os.environ["IG_USER_ID"].strip()
BASE = os.environ.get("PAGES_BASE", "").strip().rstrip("/") + "/"

def api(path, params=None, method="GET"):
    params = dict(params or {})
    data = None
    url = f"{API}/{path}"
    if method == "POST":
        data = urllib.parse.urlencode(params).encode()
    elif params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try: body = json.loads(e.read().decode())
        except Exception: body = {"raw": "unreadable"}
        return {"_http_error": e.code, "error": body.get("error", body)}

def wait_for_pages(rel_path, expect_size, timeout_s=420):
    """Poll the public Pages URL until the file is served with the right size."""
    url = BASE + rel_path
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=20) as r:
                size = int(r.headers.get("Content-Length") or -1)
                if r.status == 200 and (expect_size <= 0 or size == expect_size):
                    return url
        except Exception:
            pass
        time.sleep(12)
    raise RuntimeError(f"Pages never served {url} within {timeout_s}s")

def wait_container(cid, timeout_s=600):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        st = api(cid, {"fields": "status_code,status"})
        code = st.get("status_code")
        if code == "FINISHED":
            return
        if code == "ERROR" or "_http_error" in st:
            raise RuntimeError(f"container error: {st}")
        time.sleep(10)
    raise RuntimeError("container processing timed out")

def make_container(params):
    res = api(f"{IGID}/media", params, "POST")
    if "id" not in res:
        # retry once without cover_url if that param was rejected
        if "cover_url" in params and "_http_error" in res:
            p2 = {k: v for k, v in params.items() if k != "cover_url"}
            res2 = api(f"{IGID}/media", p2, "POST")
            if "id" in res2:
                res2["_note"] = "cover_url rejected; published without custom cover"
                return res2
        raise RuntimeError(f"container create failed: {res}")
    return res

def publish(cid):
    res = api(f"{IGID}/media_publish", {"creation_id": cid}, "POST")
    if "id" not in res:
        raise RuntimeError(f"media_publish failed: {res}")
    return res["id"]

def handle(job, jobname):
    out = {"job": jobname, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    quota = api(f"{IGID}/content_publishing_limit", {"fields": "quota_usage"})
    out["quota"] = quota
    me = api("me", {"fields": "id,username"})
    out["account"] = me
    if "_http_error" in me:
        out["status"] = "error"
        out["error"] = f"token check failed — regenerate IG token + update the IG_TOKEN secret: {me}"
        return out
    jtype = job.get("type", "dry_run")
    if jtype == "diag":
        import hashlib
        out["token_len"] = len(TOKEN)
        out["token_sha8"] = hashlib.sha256(TOKEN.encode()).hexdigest()[:8]
        out["igid_val"] = IGID
        out["refresh_probe"] = api("refresh_access_token",
            {"grant_type": "ig_refresh_token", "access_token": TOKEN})
        out["status"] = "diag_done"
        return out
    if jtype == "dry_run":
        out["status"] = "dry_run_ok"
        return out
    try:
        caption = job.get("caption", "")
        share = "true" if job.get("share_to_feed", True) else "false"
        if jtype == "reel":
            size = os.path.getsize(job["media"])
            video_url = wait_for_pages(job["media"], size)
            params = {"media_type": "REELS", "video_url": video_url,
                      "caption": caption, "share_to_feed": share}
            if job.get("cover") and os.path.exists(job["cover"]):
                params["cover_url"] = wait_for_pages(job["cover"], os.path.getsize(job["cover"]))
            c = make_container(params)
            if c.get("_note"): out["note"] = c["_note"]
            wait_container(c["id"])
            mid = publish(c["id"])
        elif jtype == "image":
            image_url = wait_for_pages(job["media"], os.path.getsize(job["media"]))
            c = make_container({"image_url": image_url, "caption": caption})
            wait_container(c["id"], 240)
            mid = publish(c["id"])
        elif jtype == "carousel":
            kids = []
            for child in job["children"]:
                curl_ = wait_for_pages(child, os.path.getsize(child))
                cc = make_container({"image_url": curl_, "is_carousel_item": "true"})
                kids.append(cc["id"])
            for k in kids: wait_container(k, 240)
            c = make_container({"media_type": "CAROUSEL", "children": ",".join(kids),
                                "caption": caption})
            wait_container(c["id"])
            mid = publish(c["id"])
        else:
            raise RuntimeError(f"unknown job type {jtype}")
        perma = api(mid, {"fields": "permalink,media_type"})
        out.update({"status": "published", "media_id": mid,
                    "permalink": perma.get("permalink", "")})
    except Exception as e:
        out["status"] = "error"
        out["error"] = str(e)[:900]
    out["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return out

def main():
    pending = []
    for jf in sorted(glob.glob("jobs/*.json")):
        name = os.path.splitext(os.path.basename(jf))[0]
        if not os.path.exists(f"results/{name}.json"):
            pending.append((name, jf))
    if not pending:
        print("no pending jobs"); return
    for name, jf in pending:
        print(f"processing {name}")
        try:
            job = json.load(open(jf))
        except Exception as e:
            res = {"job": name, "status": "error", "error": f"bad job json: {e}"}
        else:
            res = handle(job, name)
        os.makedirs("results", exist_ok=True)
        json.dump(res, open(f"results/{name}.json", "w"), indent=1)
        print(json.dumps({k: v for k, v in res.items() if k != 'caption'}, indent=1))

if __name__ == "__main__":
    main()
