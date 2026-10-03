"""Team progress for admins (docs/training-site.md section 3, "Team
progress"): what each person has uploaded, scanned and reviewed, and when
they were last active.

``team_progress`` is a pure function over the learning store's
``activity_by_person`` and the site database's ``activity_by_person``, so it
can be tested without a server. Work by people who are no longer members
stays listed (``member: false``); work recorded without a signed-in person
(local development, the CLI) is one row with ``email: null``.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, Iterable, List, Optional

#: Days of hourly review activity the page can ask for.
DAYS_DEFAULT = 14
DAYS_MAX = 90

#: Audit actions shown as their own counts (the rest are summed as "other").
COUNTED_ACTIONS = ("set_scan_started", "batch_started", "legend_read", "legend_confirmed",
                   "document_deleted")


def _iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    t = _dt.datetime.fromtimestamp(float(epoch), _dt.timezone.utc)
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def since_for(days: int, now: _dt.datetime) -> str:
    """RFC3339 UTC start of the activity window: ``days`` whole UTC days back,
    from midnight, so the first day is complete."""
    start = (now.astimezone(_dt.timezone.utc) - _dt.timedelta(days=days)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _latest(values: Iterable[Optional[str]]) -> Optional[str]:
    values = [v for v in values if v]
    return max(values) if values else None


def team_progress(members: List[Dict[str, Any]], store: Dict[str, Any], site: Dict[str, Any],
                  *, since: str, days: int) -> Dict[str, Any]:
    """``members`` are member dicts (``email``, ``role``, ``added_at``,
    ``has_password``). Returns ``{since, days, people [...], hourly [...]}``;
    people are members first (by email), then former members, then the
    unattributed row."""
    reviews, pages, scans = store["reviews"], store["pages"], store["scans"]
    uploads, seen, actions = site["uploads"], site["last_seen"], site["actions"]
    by_email = {m["email"]: m for m in members}
    others = set(reviews) | set(pages) | set(scans) | set(uploads) | set(actions)
    # Audit actors also include system names (e.g. PINNY_ADMIN_EMAILS); only people have an "@".
    others = sorted(e for e in others if e is not None and "@" in e and e not in by_email)
    order: List[Optional[str]] = sorted(by_email) + others
    if any(None in d for d in (reviews, pages, scans)):
        order.append(None)

    people = []
    for email in order:
        m = by_email.get(email)
        r = reviews.get(email) or {"actions": {}, "pins": 0, "first_at": None, "last_at": None}
        a = r["actions"]
        p = pages.get(email) or {"complete": 0, "last_at": None}
        s = scans.get(email) or {"scans": 0, "pages": 0, "last_at": None}
        u = uploads.get(email) or {"uploads": 0, "last_at": None}
        acts = actions.get(email, {}) if email is not None else {}
        last_seen = _iso(seen.get(email)) if email is not None else None
        people.append({
            "email": email,
            "member": m is not None,
            "role": m["role"] if m else None,
            "added_at": m["added_at"] if m else None,
            "has_password": bool(m and m.get("has_password")),
            "uploads": u["uploads"],
            "scans": s["scans"],
            "pages_scanned": s["pages"],
            "reviews": {
                "approved": a.get("approve", 0),
                "rejected": a.get("reject", 0),
                "added": a.get("add_manual", 0),
                "removed": a.get("remove_manual", 0),
                "actions": sum(a.values()),
                "pins": r["pins"],
                "first_at": r["first_at"],
            },
            "pages_complete": p["complete"],
            "actions": {k: acts.get(k, {}).get("count", 0) for k in COUNTED_ACTIONS},
            "other_actions": sum(v["count"] for k, v in acts.items() if k not in COUNTED_ACTIONS),
            "last_seen_at": last_seen,
            "last_active_at": _latest([r["last_at"], p["last_at"], s["last_at"], u["last_at"], last_seen,
                                       *(v["last_at"] for v in acts.values())]),
        })
    return {"since": since, "days": days, "people": people, "hourly": store["hourly"]}
