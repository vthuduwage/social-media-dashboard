"""Social post analytics collector.

For each post in posts.json:
  1. Try yt-dlp in metadata-only mode (`yt-dlp -j --skip-download`).
  2. If any of views/likes/comments/shares is missing, fall back to Playwright
     reading the public page (embedded JSON, meta tags, XHR/GraphQL responses).
  3. Write one Firestore document per snapshot to the `snapshots` collection.

Every run ends with a `SUMMARY {...}` JSON log line that the deploy scripts read.

Usage:
  python main.py              # collect and write to Firestore
  python main.py --dry-run    # collect and print only
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

METRICS = ("views", "likes", "comments", "shares")
COLLECTION = os.environ.get("FIRESTORE_COLLECTION", "snapshots")
YTDLP_TIMEOUT = int(os.environ.get("YTDLP_TIMEOUT", "60"))
PAGE_TIMEOUT_MS = int(os.environ.get("PAGE_TIMEOUT_MS", "45000"))

UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("collector")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def parse_count(value) -> int | None:
    """'1,234' -> 1234, '1.2K' -> 1200, '3.4M' -> 3400000, 17 -> 17."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().replace(" ", " ").replace(",", "")
    m = re.match(r"^([\d.]+)\s*([KkMmBb]?)", s)
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    mult = {"": 1, "k": 1e3, "m": 1e6, "b": 1e9}[m.group(2).lower()]
    return int(round(num * mult))


def resolve_url(url: str) -> str:
    """Follow share/short-link redirects to the canonical post URL."""
    try:
        r = requests.get(
            url,
            headers={"User-Agent": UA_DESKTOP, "Accept-Language": "en-US,en;q=0.9"},
            allow_redirects=True,
            timeout=20,
        )
        final = r.url
    except requests.RequestException as e:
        log.warning("redirect resolution failed for %s: %s", url, e)
        return url
    # Facebook/Instagram redirect anonymous users to a login page that carries
    # the real target in ?next= - prefer the original if we landed on login.
    if "/login" in final:
        return url
    return final


def strip_query(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


# --------------------------------------------------------------------------- #
# Method 1: yt-dlp metadata only
# --------------------------------------------------------------------------- #
def via_ytdlp(url: str) -> tuple[dict, str | None]:
    cmd = ["yt-dlp", "-j", "--skip-download", "--no-warnings", "--no-playlist", url]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=YTDLP_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {}, "yt-dlp timeout"
    if p.returncode != 0 or not p.stdout.strip():
        err = (p.stderr.strip().splitlines() or ["unknown error"])[-1]
        return {}, err[:300]
    info = json.loads(p.stdout.strip().splitlines()[-1])
    out = {
        "views": parse_count(info.get("view_count")),
        "likes": parse_count(info.get("like_count")),
        "comments": parse_count(info.get("comment_count")),
        "shares": parse_count(info.get("repost_count")),
        "title": (info.get("title") or info.get("description") or "")[:200],
        "author": info.get("uploader") or info.get("channel") or info.get("uploader_id"),
        "post_id": info.get("id"),
    }
    return out, None


# --------------------------------------------------------------------------- #
# Method 2: Playwright public page
# --------------------------------------------------------------------------- #
# Regexes over the page HTML and any JSON responses the page loads.
# Order matters: first pattern with a match wins for that metric.
PATTERNS = {
    "tiktok": {
        "views": [r'"playCount"\s*:\s*"?(\d+)'],
        "likes": [r'"diggCount"\s*:\s*"?(\d+)'],
        "comments": [r'"commentCount"\s*:\s*"?(\d+)'],
        "shares": [r'"shareCount"\s*:\s*"?(\d+)'],
    },
    "instagram": {
        "views": [r'"play_count"\s*:\s*(\d+)', r'"video_play_count"\s*:\s*(\d+)',
                  r'"ig_play_count"\s*:\s*(\d+)', r'"video_view_count"\s*:\s*(\d+)',
                  r'"view_count"\s*:\s*(\d+)'],
        "likes": [r'"like_count"\s*:\s*(\d+)', r'"edge_media_preview_like"\s*:\s*\{\s*"count"\s*:\s*(\d+)'],
        "comments": [r'"comment_count"\s*:\s*(\d+)', r'"edge_media_to_comment"\s*:\s*\{\s*"count"\s*:\s*(\d+)'],
        "shares": [r'"reshare_count"\s*:\s*(\d+)', r'"share_count"\s*:\s*(\d+)'],
    },
    "facebook": {
        "views": [r'"video_view_count"\s*:\s*(\d+)', r'"play_count"\s*:\s*(\d+)',
                  r'"post_view_count"\s*:\s*(\d+)', r'"view_count"\s*:\s*(\d+)'],
        "likes": [r'"reaction_count"\s*:\s*\{\s*"count"\s*:\s*(\d+)',
                  r'"i18n_reaction_count"\s*:\s*"([\d.,KMB]+)"',
                  r'"likers"\s*:\s*\{\s*"count"\s*:\s*(\d+)'],
        "comments": [r'"comments"\s*:\s*\{\s*"total_count"\s*:\s*(\d+)',
                     r'"comment_count"\s*:\s*\{\s*"total_count"\s*:\s*(\d+)',
                     r'"total_comment_count"\s*:\s*(\d+)'],
        "shares": [r'"share_count"\s*:\s*\{\s*"count"\s*:\s*(\d+)',
                   r'"i18n_share_count"\s*:\s*"([\d.,KMB]+)"'],
    },
}

# Visible-text fallbacks ("1.2K views", "345 likes") used when no JSON matched.
TEXT_PATTERNS = {
    "views": r"([\d.,]+\s*[KMB]?)\s+(?:views|plays)\b",
    "likes": r"([\d.,]+\s*[KMB]?)\s+(?:likes|reactions)\b",
    "comments": r"([\d.,]+\s*[KMB]?)\s+comments?\b",
    "shares": r"([\d.,]+\s*[KMB]?)\s+shares?\b",
}

LOGIN_WALL_MARKERS = ("/accounts/login", "/login/", "login.php", "checkpoint")


def via_playwright(url: str, platform: str) -> tuple[dict, str | None]:
    from playwright.sync_api import sync_playwright

    bodies: list[str] = []

    def on_response(resp):
        try:
            ctype = resp.headers.get("content-type", "")
            if any(k in resp.url for k in ("graphql", "/api/", "item_list", "/item/detail")) \
                    or "json" in ctype:
                txt = resp.text()
                if len(txt) < 5_000_000:
                    bodies.append(txt)
        except Exception:
            pass

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            executable_path=os.environ.get("CHROMIUM_PATH") or None,
        )
        ctx = browser.new_context(
            user_agent=UA_DESKTOP,
            locale="en-US",
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        page.on("response", on_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            final_url = page.url
            html = page.content()
            try:
                text = page.inner_text("body", timeout=5000)
            except Exception:
                text = ""
            og_desc = page.evaluate(
                "() => (document.querySelector('meta[property=\"og:description\"]')||{}).content || ''"
            )
            title = page.title()
        finally:
            browser.close()

    blob = "\n".join([html, *bodies])
    out: dict = {m: None for m in METRICS}
    for metric, pats in PATTERNS.get(platform, {}).items():
        for pat in pats:
            m = re.search(pat, blob)
            if m:
                out[metric] = parse_count(m.group(1))
                break

    # Meta description, e.g. Instagram: "1,234 likes, 56 comments - user on ..."
    for src in (og_desc, text):
        for metric, pat in TEXT_PATTERNS.items():
            if out[metric] is None and src:
                m = re.search(pat, src, flags=re.IGNORECASE)
                if m:
                    out[metric] = parse_count(m.group(1))

    out["title"] = (title or "")[:200]
    err = None
    if all(out[m] is None for m in METRICS):
        if any(k in final_url for k in LOGIN_WALL_MARKERS):
            err = f"login wall ({final_url[:120]})"
        else:
            err = "no counts found on public page"
    return out, err


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def collect_one(post: dict, run_ts: datetime) -> dict:
    platform, url = post["platform"], post["url"]
    t0 = time.time()
    resolved = resolve_url(url)
    target = strip_query(resolved) if platform in ("instagram", "facebook") else resolved
    log.info("[%s] url=%s resolved=%s", platform, url, target)

    result = {m: None for m in METRICS}
    meta: dict = {}
    methods: list[str] = []
    errors: dict = {}

    data, err = via_ytdlp(target)
    if err and target != url:  # retry with the original link
        data, err = via_ytdlp(url)
    if err:
        errors["yt-dlp"] = err
        log.warning("[%s] yt-dlp failed: %s", platform, err)
    else:
        got = [m for m in METRICS if data.get(m) is not None]
        log.info("[%s] yt-dlp returned %s", platform, {m: data.get(m) for m in METRICS})
        if got:
            methods.append("yt-dlp")
            for m in got:
                result[m] = data[m]
        meta.update({k: data.get(k) for k in ("title", "author", "post_id") if data.get(k)})

    missing = [m for m in METRICS if result[m] is None]
    if missing:
        log.info("[%s] missing %s -> trying Playwright", platform, missing)
        try:
            pdata, perr = via_playwright(target, platform)
        except Exception as e:  # browser crash, timeout, etc.
            pdata, perr = {}, f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"[:300]
        if perr:
            errors["playwright"] = perr
            log.warning("[%s] playwright failed: %s", platform, perr)
        filled = [m for m in missing if pdata.get(m) is not None]
        if filled:
            methods.append("playwright")
            for m in filled:
                result[m] = pdata[m]
            log.info("[%s] playwright filled %s", platform, {m: pdata[m] for m in filled})
        if pdata.get("title") and not meta.get("title"):
            meta["title"] = pdata["title"]

    core = ("views", "likes", "comments")
    if all(result[m] is not None for m in core):
        status = "ok"
    elif any(result[m] is not None for m in METRICS):
        status = "partial"
    else:
        status = "error"

    method = "+".join(methods) if methods else "none"
    log.info("[%s] status=%s method=%s values=%s (%.1fs)",
             platform, status, method, result, time.time() - t0)

    return {
        "timestamp": datetime.now(timezone.utc),
        "run_ts": run_ts,
        "platform": platform,
        "url": url,
        "resolved_url": target,
        **result,
        "interactions": sum(result[m] or 0 for m in ("likes", "comments", "shares")),
        "status": status,
        "method": method,
        "errors": errors,
        **meta,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="don't write to Firestore")
    ap.add_argument("--posts", default=str(Path(__file__).with_name("posts.json")))
    args = ap.parse_args()
    dry = args.dry_run or os.environ.get("DRY_RUN") == "1"

    posts = json.loads(Path(args.posts).read_text())
    db = None
    if not dry:
        from google.cloud import firestore
        db = firestore.Client()

    run_ts = datetime.now(timezone.utc)
    summary = []
    for post in posts:
        try:
            snap = collect_one(post, run_ts)
        except Exception as e:
            log.exception("[%s] unexpected failure", post.get("platform"))
            snap = {
                "timestamp": datetime.now(timezone.utc), "run_ts": run_ts, "platform": post["platform"],
                "url": post["url"], **{m: None for m in METRICS}, "interactions": 0,
                "status": "error", "method": "none", "errors": {"collector": str(e)[:300]},
            }
        if db is not None:
            db.collection(COLLECTION).add(snap)
        summary.append({k: snap.get(k) for k in
                        ("platform", "status", "method", *METRICS, "interactions", "errors")})

    totals = {m: sum(s[m] or 0 for s in summary) for m in (*METRICS, "interactions")}
    print("SUMMARY " + json.dumps({"posts": summary, "totals": totals}, default=str), flush=True)
    # Exit non-zero only if *every* post failed, so one blocked platform
    # doesn't mark the whole scheduled execution as failed.
    return 1 if all(s["status"] == "error" for s in summary) else 0


if __name__ == "__main__":
    sys.exit(main())
