"""Read-only analytics dashboard backed by Firestore `snapshots`.

To stay inside Firestore's free tier (50k reads/day) snapshots are cached in
memory and refreshed incrementally: after the first load, each refresh only
reads documents newer than the last one seen.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, render_template, request

COLLECTION = os.environ.get("FIRESTORE_COLLECTION", "snapshots")
HISTORY_DAYS = int(os.environ.get("HISTORY_DAYS", "30"))
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "300"))
METRICS = ("views", "likes", "comments", "shares", "interactions")

app = Flask(__name__)
_lock = threading.Lock()
_cache: dict = {"rows": [], "last_ts": None, "fetched_at": 0.0}
_db = None


def db():
    global _db
    if _db is None:
        from google.cloud import firestore
        _db = firestore.Client()
    return _db


def _refresh():
    now = time.time()
    if now - _cache["fetched_at"] < REFRESH_SECONDS:
        return
    since = _cache["last_ts"] or datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)
    q = (db().collection(COLLECTION)
         .where("timestamp", ">", since)
         .order_by("timestamp"))
    new = []
    for doc in q.stream():
        d = doc.to_dict()
        ts = d.get("timestamp")
        new.append({
            "t": ts.isoformat() if ts else None,
            "run": (d.get("run_ts") or ts).isoformat() if ts else None,
            "platform": d.get("platform"),
            "url": d.get("url"),
            "status": d.get("status"),
            "method": d.get("method"),
            "title": d.get("title"),
            **{m: d.get(m) for m in METRICS},
        })
        _cache["last_ts"] = ts
    cutoff = (datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)).isoformat()
    _cache["rows"] = [r for r in _cache["rows"] + new if r["t"] and r["t"] >= cutoff]
    _cache["fetched_at"] = now


def _latest_good(rows):
    """Latest snapshot per platform, falling back per-metric to the last known value."""
    latest: dict = {}
    for r in rows:  # ascending by time
        p = r["platform"]
        cur = latest.setdefault(p, {"platform": p, "url": r["url"], **{m: None for m in METRICS}})
        cur.update({"t": r["t"], "status": r["status"], "method": r["method"]})
        if r.get("title"):
            cur["title"] = r["title"]
        for m in METRICS:
            if r.get(m) is not None and (m != "interactions" or r["status"] != "error"):
                cur[m] = r[m]
                cur[f"{m}_t"] = r["t"]
    return latest


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/data")
def api_data():
    with _lock:
        try:
            _refresh()
            err = None
        except Exception as e:  # serve stale cache rather than 500
            err = f"{type(e).__name__}: {e}"
        rows = list(_cache["rows"])
    days = min(int(request.args.get("days", HISTORY_DAYS)), HISTORY_DAYS)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    series = [r for r in rows if r["t"] >= cutoff and r["status"] != "error"]
    latest = _latest_good(rows)
    order = ["tiktok", "instagram", "facebook"]
    posts = sorted(latest.values(), key=lambda p: order.index(p["platform"])
                   if p["platform"] in order else 99)
    totals = {m: sum(p.get(m) or 0 for p in posts) for m in METRICS}
    resp = jsonify({
        "posts": posts,
        "totals": totals,
        "series": series,
        "last_updated": rows[-1]["t"] if rows else None,
        "error": err,
    })
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp


@app.get("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
