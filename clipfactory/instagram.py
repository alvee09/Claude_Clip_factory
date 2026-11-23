"""Instagram Reels via the official Instagram Graph API (Business/Creator account).

Uses the resumable upload flow, so clips go straight from this Mac to Instagram:
  1. create a REELS container  2. upload the file bytes  3. wait until processed  4. publish
"""
import os
import time

import requests

from . import config


def _base(s):
    return f"https://{s['ig_api_host']}/{s['ig_api_version']}"


def _check(resp):
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code >= 400 or "error" in data:
        err = data.get("error", {})
        msg = err.get("error_user_msg") or err.get("message") or resp.text[:300]
        raise RuntimeError(f"Instagram: {msg}")
    return data


def _auth(s):
    if not (s["ig_token"] and s["ig_user_id"]):
        raise RuntimeError("Instagram is not connected")
    return {"Authorization": f"Bearer {s['ig_token']}"}


def account_name():
    s = config.load()
    if not (s["ig_token"] and s["ig_user_id"]):
        return None
    r = requests.get(f"{_base(s)}/{s['ig_user_id']}", params={"fields": "username"},
                     headers=_auth(s), timeout=30)
    return "@" + _check(r).get("username", "?")


def upload_reel(path, caption, on_progress=lambda p: None):
    s = config.load()
    headers = _auth(s)
    base = _base(s)

    container = _check(requests.post(
        f"{base}/{s['ig_user_id']}/media", headers=headers, timeout=60,
        data={"media_type": "REELS", "upload_type": "resumable",
              "caption": caption[:2200], "share_to_feed": "true"}))
    cid = container["id"]
    upload_uri = container.get("uri") or \
        f"https://rupload.facebook.com/ig-api-upload/{s['ig_api_version']}/{cid}"

    size = os.path.getsize(path)
    with open(path, "rb") as f:
        _check(requests.post(upload_uri, data=f, timeout=900, headers={
            "Authorization": f"OAuth {s['ig_token']}",
            "offset": "0", "file_size": str(size)}))
    on_progress(0.5)

    deadline = time.time() + 15 * 60
    while True:
        st = _check(requests.get(f"{base}/{cid}", params={"fields": "status_code,status"},
                                 headers=headers, timeout=30))
        code = st.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram couldn't process the video: {st.get('status')}")
        if time.time() > deadline:
            raise RuntimeError("Instagram took too long to process the video")
        time.sleep(5)
    on_progress(0.9)

    media = _check(requests.post(f"{base}/{s['ig_user_id']}/media_publish",
                                 headers=headers, data={"creation_id": cid}, timeout=60))
    info = _check(requests.get(f"{base}/{media['id']}", params={"fields": "permalink"},
                               headers=headers, timeout=30))
    return info.get("permalink") or f"instagram media {media['id']}"
