# Clip Factory

Plug in your hard drive → point Clip Factory at a folder → it finds the action-packed
moments in every video, cuts them into clips, and waits for you. Nothing is posted until
you press **Approve & post** on a clip.

```
HDD folder ──scan──► every video ──analyse──► best moments ──cut──► clips
                                                                       │
                                     you review in the dashboard ◄─────┘
                                        │ approve            │ reject
                                        ▼                    ▼
                          YouTube Short / video,         kept aside
                          Instagram Reel
```

## Start it

Double-click **`Start Clip Factory.command`** (or run `.venv/bin/python -m clipfactory`).
The dashboard opens at <http://127.0.0.1:5055>. Keep that Terminal window open while it works.

Requires `ffmpeg` (`brew install ffmpeg`) and Python 3.10+.

## Using it

1. **Drives & videos** → either **drop video files** into the upload box (no drive needed —
   they're copied into `uploads/`), or click your drive under *Connected drives* (or drag a folder from
   Finder into the box) → **Add & scan**. Subfolders are included. The drive is only read.
2. Each video is analysed and cut. Progress shows in the table and the header.
3. **Review** → play each clip (toggle 9:16 / Original), edit the title, description and tags,
   tick where it should go, then **Approve & post** or **Reject**.
   *Adjust timing* lets you nudge the start/length and re-cut.
4. **Posted** lists everything that went out, with links.

### How moments are picked (no AI, free)

One pass over each video measures, second by second:
- **loudness** compared with the rest of the video (crowds, explosions, hype),
- **sudden jumps** in loudness (the instant something happens),
- **scene-cut density** (fast action, replays, camera switches).

The highest-scoring, non-overlapping peaks become clips, placed so the peak lands about
two-thirds of the way in (build-up, then payoff). Tune it in **Settings → Clip cutting**:
clip length, clips per minutes of footage, max per video, and pickiness.

### Output

Each clip is rendered as
- **vertical** 1080×1920 (Shorts / Reels) — full frame over a blurred background, or center-cropped;
- **original** framing (regular YouTube video).

Files go to `clips/` (change in Settings).

## Connect YouTube (one-time, ~10 min)

1. Go to <https://console.cloud.google.com/> → create a project.
2. **APIs & Services → Library** → enable **YouTube Data API v3**.
3. **OAuth consent screen** → External → add your Google account under *Test users*.
4. **Credentials → Create credentials → OAuth client ID → Desktop app** → download the JSON.
5. In Clip Factory **Settings → YouTube**: upload that JSON, then **Sign in with Google**.

> ⚠️ Google rule: videos uploaded through a brand-new, *unaudited* API project are locked to
> **private** until the project passes Google's free
> [API compliance audit](https://support.google.com/youtube/contact/yt_api_form). Until then,
> uploads land as private and you can flip them to public in YouTube Studio.
> Default quota allows about **6 uploads per day**; request more in the same audit form.

## Connect Instagram (one-time)

Instagram only allows posting through its official API for **Business or Creator** accounts
(switch for free in the Instagram app: *Settings → Account type and tools*).

1. Create an app at <https://developers.facebook.com/> → add the **Instagram** product
   (*API setup with Instagram login*).
2. Add your Instagram account as a tester / generate a token with the permissions
   `instagram_business_basic` and `instagram_business_content_publish`.
3. Copy the **Instagram user ID** and the **access token** into
   **Settings → Instagram** (host: `graph.instagram.com`) → **Save & test**.

If you set it up through a Facebook Page instead, choose host `graph.facebook.com` and use a
token with `instagram_content_publish`.

Tokens expire (long-lived ones after ~60 days) — paste a fresh one when posting starts failing.
Instagram also caps how many posts an account can publish through the API per 24 hours.

## Where things live

| Path | What |
|---|---|
| `clipfactory/` | the app (Python + one HTML page) |
| `data/clipfactory.db` | list of drives, videos and clips |
| `data/settings.json`, `data/youtube_*.json` | settings and account logins — keep private |
| `uploads/` | videos you uploaded through the dashboard |
| `clips/` | rendered clips and thumbnails |

If the app is closed mid-way, it picks up where it left off next time.
