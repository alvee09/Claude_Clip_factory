"""YouTube uploads via the YouTube Data API v3 (OAuth, your own Google Cloud project)."""
import re

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from .config import DATA

CLIENT_SECRET = DATA / "youtube_client_secret.json"
TOKEN = DATA / "youtube_token.json"
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


def _save(creds):
    TOKEN.write_text(creds.to_json())
    TOKEN.chmod(0o600)


def credentials():
    if not TOKEN.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds.valid and creds.refresh_token:
        creds.refresh(Request())
        _save(creds)
    return creds if creds.valid else None


def connect():
    """Opens Google sign-in in the browser; blocks until you finish."""
    if not CLIENT_SECRET.exists():
        raise RuntimeError("Upload your OAuth client JSON first (see README).")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", open_browser=True)
    _save(creds)
    return channel_name()


def disconnect():
    TOKEN.unlink(missing_ok=True)


def channel_name():
    creds = credentials()
    if not creds:
        return None
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    items = yt.channels().list(part="snippet", mine=True).execute().get("items", [])
    return items[0]["snippet"]["title"] if items else "(no channel)"


def upload(path, title, description, tags, privacy, category, short, thumbnail=None, on_progress=lambda p: None):
    """Returns (url, warning). A thumbnail that YouTube refuses doesn't fail the upload."""
    creds = credentials()
    if not creds:
        raise RuntimeError("YouTube is not connected")
    title = re.sub(r"[<>]", "", title).strip()[:100] or "Highlight"
    if short and "#shorts" not in (title + description).lower():
        description = (description + "\n\n#Shorts").strip()
    body = {
        "snippet": {
            "title": title,
            "description": re.sub(r"[<>]", "", description)[:5000],
            "tags": tags[:30],
            "categoryId": category,
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
    req = yt.videos().insert(
        part="snippet,status", body=body,
        media_body=MediaFileUpload(str(path), mimetype="video/mp4",
                                   chunksize=8 * 1024 * 1024, resumable=True))
    resp = None
    while resp is None:
        status, resp = req.next_chunk()
        if status:
            on_progress(status.progress())
    vid = resp["id"]
    url = f"https://youtube.com/shorts/{vid}" if short else f"https://youtu.be/{vid}"
    warning = None
    if thumbnail:
        try:
            yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(thumbnail), mimetype="image/jpeg")).execute()
        except Exception as e:
            # Custom thumbnails need a verified channel (youtube.com/verify)
            warning = f"Posted, but YouTube didn't take the thumbnail: {e}"
    return url, warning
