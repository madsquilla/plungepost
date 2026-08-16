# PlungePost

Multi-account social post studio. Generates professional, on-brand posts and
branded graphics for each connected business's Facebook Page, **with a human
review gate**. Add a new account from the dashboard: give it a name and
website, and PlungePost reads the site and auto-builds the brand voice,
content themes, and logo.

It is a **run-once-and-exit** program for generation: it starts, generates
posts, and exits. It is meant to be triggered periodically on Unraid, not run
as a 24/7 service. The dashboard (a separate long-running service) is where
you review, approve, and hand off finished posts.

There is **no automated publishing to Facebook** -- see "Publishing" below
for why, and how posting actually works now.

---

## How it works (the review gate)

Three JSON files in `data/` act as a simple pipeline:

| File            | Meaning                                              |
| --------------- | ---------------------------------------------------- |
| `pending.json`  | Generated posts **awaiting your review**.            |
| `approved.json` | Posts **you have eyeballed** and moved here.         |
| `history.json`  | Posts **already published** (used for 30-day dedup). |

Day to day:

1. The app generates posts into `pending.json` (`--mode stage` or `generate-batch`).
2. **You** review them and cut/paste the good ones into `approved.json`.
3. **You** post them yourself through Meta Business Suite's Planner: open the
   dashboard's Ready/Scheduled tab, click Download image and Copy caption for
   each, paste into Business Suite, then click "Mark as posted" in the
   dashboard to move it to `history.json`.

The app does not publish to Facebook automatically. Content posted through
this app's own API access gets its reach throttled by Facebook unless the
app completes Advanced Access review (Business Verification + App Review) --
see the "Publishing" section below.

The generator reads recent themes/hooks from history + the queues so it does
not repeat a theme within 30 days.

---

## Publishing

This app **does not call the Facebook API to publish posts.** It used to, but
Facebook throttles the reach of any content posted by an app that hasn't
completed Advanced Access review (Business Verification + App Review for
`pages_manage_posts`) -- such posts remain technically "published" and
reachable (e.g. via the Page's Photos tab) but never surface in the Page's
normal feed for real visitors. Getting reviewed requires becoming a Meta
"Tech Provider" (irreversible) plus submitting business documents, which is
disproportionate for a small tool serving a couple of Pages.

Instead: the dashboard's Ready and Scheduled tabs give each post a
**Download image** button and a **Copy caption** button. Post the image +
caption yourself through **Meta Business Suite's Planner**
(business.facebook.com) -- native Meta posts always get full normal reach,
no review needed. Once posted there, click **Mark as posted** in the
dashboard to move the item into `history.json` for your own records (this
does not check Facebook; it is just local bookkeeping).

---

## Modes and flags

```
python src/main.py --mode <stage|publish-approved|generate-batch> [--dry-run] [--count N]
```

| Mode               | What it does                                                          |
| ------------------ | -------------------------------------------------------------------- |
| `stage` (default)  | Generate one post, append it to `pending.json`. No publishing.       |
| `publish-approved` | Logs how many approved items are waiting. Does **not** call Facebook -- post them yourself via Meta Business Suite, then click "Mark as posted" in the dashboard. |
| `generate-batch`   | Generate `--count N` posts at once into `pending.json` (default 5).  |

| Flag        | Effect                                                                       |
| ----------- | ---------------------------------------------------------------------------- |
| `--dry-run` | Generate + print to console + write to `data/dry_runs/`, instead of staging. Applies to `stage` / `generate-batch` only. |
| `--count N` | Number of posts for `generate-batch`.                                        |

Exit code is `0` on success and **non-zero on any failure** (generation error)
so a failed scheduled run is visible.

---

## 1. Local setup and a first dry-run (Windows)

From the project folder (`plungepost`):

```powershell
# Create and activate a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt

# Create your .env from the template, then edit it
copy .env.example .env
notepad .env
```

Put at least your **ANTHROPIC_API_KEY** in `.env`. You do **not** need the Meta
token yet for a dry-run.

Now run a dry-run. This calls Anthropic to generate a post, prints it, and
writes it to `data/dry_runs/` -- it does **not** touch Facebook:

```powershell
python src\main.py --mode stage --dry-run
```

You should see the formatted post printed, ending with `Run finished with exit code 0`.

> Tip: if you are not in the venv, use `.\.venv\Scripts\python.exe src\main.py ...`.

---

## 2. Running each mode locally

```powershell
# Generate one post into pending.json (for review)
python src\main.py --mode stage

# Generate a week of posts at once into pending.json
python src\main.py --mode generate-batch --count 7

# Dry-run a batch (prints + files them, stages nothing)
python src\main.py --mode generate-batch --count 7 --dry-run

# Publish the oldest approved post (requires META_* in .env)
python src\main.py --mode publish-approved

# Dry-run a publish (shows exactly what WOULD post, calls nobody)
python src\main.py --mode publish-approved --dry-run
```

---

## 3. Reviewing pending posts and promoting to approved

After a `stage` / `generate-batch` run, open `data/pending.json`. Each entry
looks like:

```json
{
  "id": "20260611-090000-follow-the-sun",
  "generated_at": "2026-06-11T09:00:00+00:00",
  "theme": "follow-the-sun",
  "post_text": "....the full post....",
  "suggested_image_concept": "....",
  "link": "https://skyusa.us",
  "status": "pending"
}
```

To approve a post:

1. Open `data/pending.json` and `data/approved.json` in a text editor.
2. **Move** the entries you like (the whole `{ ... }` object) from the
   `pending.json` array into the `approved.json` array.
3. Edit the `post_text` freely if you want to tweak wording.
4. Save both files. Keep them valid JSON (arrays of objects, commas between
   items, no trailing comma).

`publish-approved` just logs how many items are in `approved.json` and prints
each one -- it does not publish anything (see "Publishing" above). Post them
yourself via Meta Business Suite, then click "Mark as posted" in the
dashboard for each one to move it into `history.json`.

> Posts you delete from `pending.json` are simply discarded -- nothing is
> published unless it is in `approved.json`.

---

## 4. Post images (automatic, branded)

Every generated post gets a **branded graphic built automatically** -- you do
nothing, and you review it before it publishes.

How it works:
- The generator also writes a short image headline, subtext, highlight word,
  and a stock-photo search query for each post.
- `images.py` pulls a topic-relevant professional photo from **Pexels** (needs
  a free `PEXELS_API_KEY`).
- `imagecard.py` lays your navy/green brand overlay, logo, and headline over
  that photo and saves it to `data/cards/<post-id>.png`.
- Publishing becomes a **photo post**: the branded card as the image, your full
  post text as the caption.

**Reviewing the image:** after `stage` / `generate-batch`, each item in
`pending.json` has a `card_path`. Open that PNG (e.g.
`data/cards/<post-id>.png`) to eyeball the graphic alongside the text before you
promote the item to `approved.json`.

**No Pexels key?** The post still gets a clean, on-brand **text-only card** (one
of five rotating styles) instead of a photo. Nothing breaks.

**Want a different image?** Delete `data/cards/<post-id>.png` and re-run, or edit
the item's `image_query` and regenerate. You can also hand-make one from
`template/post-card.html` (a standalone browser template) and point `card_path`
at it.

---

## Web review dashboard (easiest way to review + publish)

Instead of editing JSON by hand, run the browser dashboard. You do not even need
Docker for this -- it runs straight from the venv:

```powershell
.\.venv\Scripts\python.exe src\webapp.py
```

Then open **http://localhost:8080**. From there you can:
- **Create a custom post** — type what you want (holiday greeting, limited offer,
  event, announcement) and it writes an on-brand post + graphic for it. Optionally
  pick a style.
- **Generate** from the theme rotation (set a count, click Generate)
- See each post's **branded image + caption + on-image copy** (large preview)
- **Approve**, **Discard**, or move posts **back to review**
- **Download image** / **Copy caption** for an approved post, to paste into
  Meta Business Suite's Planner yourself, then **Mark as posted** to file it
- **Schedule** a post for a specific date and time -- this just labels it as
  a reminder for when you plan to post it; nothing fires automatically
- **Auto-pilot** — assigns approved posts across your chosen days/times
  automatically, so you have a ready-made posting calendar to work from, but
  you still post each one by hand
- See **recently posted** items, with a Facebook link for older ones that
  were published via the old API path

Scheduling uses the `TZ` time zone from your `.env` (default
`America/Chicago`).

> Security: the dashboard has **no login**. Keep it on localhost or a
> trusted LAN. Do **NOT** port-forward it or expose it to the internet.

In Docker, the dashboard is its own long-running service:

```powershell
docker compose up -d dashboard      # start it -> http://localhost:8080
docker compose logs -f dashboard    # watch its logs
docker compose down                 # stop it
```

---

## 5. Build the Docker image

```powershell
docker build -t plungepost:latest .
```

Test the image with a dry-run (mount data/ so output persists):

```powershell
docker run --rm --env-file .env -v ${PWD}\data:/app/data -v ${PWD}\tenants:/app/tenants -v ${PWD}\logs:/app/logs `
  plungepost:latest --mode stage --dry-run
```

Or via compose:

```powershell
docker compose run --rm poster --mode stage --dry-run
```

The container runs the command and **exits** -- that is by design.

---

## 6. Deploying on Unraid (web UI)

### a) Get the project onto Unraid

Either:

- **Private GitHub repo:** push this folder to a private repo, then on Unraid
  pull it (e.g. into `/mnt/user/appdata/plungepost`), **or**
- **SMB copy:** copy the whole `plungepost` folder to an Unraid share
  (e.g. `\\TOWER\appdata\plungepost`) over the network.

Do **not** copy your `.env` -- secrets go in the container template (step c).

### b) Build / import the image on Unraid

Open the Unraid terminal (or a User Scripts script) and build the image from
the copied folder:

```bash
cd /mnt/user/appdata/plungepost
docker build -t plungepost:latest .
```

This produces a local image named `plungepost:latest` that Unraid can run.

### c) Set environment variables (NOT in the repo)

When you create the container (Docker tab -> Add Container, or via the User
Scripts `docker run` below), set these three variables in the template:

| Variable                 | Value                                            |
| ------------------------ | ------------------------------------------------ |
| `ANTHROPIC_API_KEY`      | Your Anthropic API key                           |
| `META_PAGE_ID`           | Numeric ID of your Facebook Page            |
| `META_PAGE_ACCESS_TOKEN` | Long-lived **Page** access token                 |

### d) Make `data/` and `tenants/` persistent (so accounts/history survive)

Map host paths into the container so the queues, history, and every account's
brand/tokens/cards are not lost when the container is recreated:

| Container path | Host path (example)                              |
| -------------- | ------------------------------------------------ |
| `/app/data`    | `/mnt/user/appdata/plungepost/data`       |
| `/app/tenants` | `/mnt/user/appdata/plungepost/tenants`    |
| `/app/logs`    | `/mnt/user/appdata/plungepost/logs`       |

Without `/app/data`, `history.json` resets every run and dedup stops working.
Without `/app/tenants`, **every account (including its Facebook Page token,
brand, themes, logo, and cards) is wiped when the container is recreated** --
the app would silently re-migrate only the original SkySystems setup on next
start. `tenants/` is gitignored, so `update.sh` never touches it.

### e) Schedule it with the User Scripts plugin

This container is run-once. Use **User Scripts** to run it on a cron schedule
for generation only -- `--mode publish-approved` no longer posts to Facebook
(see "Publishing" above), so a script running it just logs a reminder now.
You can leave such a script running harmlessly or disable it; either way it
will not post anything on its own anymore.

```bash
#!/bin/bash
docker run --rm \
  -e ANTHROPIC_API_KEY="your-key" \
  -v /mnt/user/appdata/plungepost/data:/app/data \
  -v /mnt/user/appdata/plungepost/tenants:/app/tenants \
  -v /mnt/user/appdata/plungepost/logs:/app/logs \
  plungepost:latest --mode generate-batch --count 7
```

Cron line for **once a week**:

```
0 8 * * 1
```

Actual publishing now happens by hand through the dashboard (Download image /
Copy caption) into Meta Business Suite's Planner -- see "Publishing" above.

### f) Read the logs to confirm a post

- **Container logs:** Unraid Docker tab -> click the container -> **Logs**.
  A successful publish prints:
  `Published post id=... (facebook id=...) and moved it to history.json.`
- **File log:** `/mnt/user/appdata/plungepost/logs/poster.log`
  (rotating, kept across runs).
- A non-zero exit and an `ERROR` line mean something failed (API down, empty
  queue, or an expired token -- the message tells you which).

---

## Generating a long-lived Page access token (your separate checklist)

Not required for the normal Download image / Copy caption workflow -- this is
only needed if you use the dashboard's "Verify" button to confirm which Page
a saved token points to, or if you later pursue Meta's Advanced Access review
(see "Publishing" above) to restore real API publishing:

1. In **Meta for Developers**, create an app and add the Pages permissions
   (`pages_manage_posts`, `pages_read_engagement`).
2. In **Graph API Explorer**, select your app and your Facebook Page,
   grant those permissions, and generate a **User** token.
3. Exchange it for a **long-lived user token**, then call `/me/accounts` to get
   the **long-lived Page token** for your Facebook Page.
4. Put that Page token in `META_PAGE_ACCESS_TOKEN`.

---

## Project layout

```
plungepost/
  src/
    main.py        entry point, arg parsing, mode dispatch, logging
    webapp.py      Flask web dashboard (review/approve/publish)
    generate.py    Anthropic call + prompt assembly + JSON parsing
    images.py      Pexels stock-photo fetch
    imagecard.py   branded card rendering (landscape + square + styles)
    cards.py       shared card-builder (photo fetch + render)
    publish.py     Meta Graph API publish call (photo or text)
    store.py       read/write pending/approved/history JSON + dedup
    content.py     brand facts + system prompt assembly
  assets/
    fonts/         Rajdhani + Nunito Sans (brand fonts)
    logo_full.png  packaged fallback logo (accounts store their own)
    logo_mark.png  packaged fallback icon mark
  data/
    themes.json    rotating content themes (16 seeded)
    pending.json   generated, awaiting your approval
    approved.json  you move items here after review
    history.json   already posted, used for dedup
    cards/         rendered post graphics (one PNG per post)
  template/
    post-card.html standalone branded image template
  tools/
    get_page_token.py  convert a user token to a long-lived Page token
    verify_token.py    read-only check that the Page token can publish
  Dockerfile
  docker-compose.yml
  .env.example
  requirements.txt
  README.md
```

## Configuration reference

| Env var                  | Required | Default                       |
| ------------------------ | -------- | ----------------------------- |
| `ANTHROPIC_API_KEY`      | yes      | --                            |
| `META_PAGE_ID`           | to publish | --                          |
| `META_PAGE_ACCESS_TOKEN` | to publish | --                          |
| `PEXELS_API_KEY`         | for photos | -- (free: pexels.com/api)   |
| `ANTHROPIC_MODEL`        | no       | `claude-opus-4-8`             |
| `CANONICAL_URL`          | no       | `https://skyusa.us`           |
