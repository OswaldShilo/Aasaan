"""Practice Curator: real practice for the learner's track, linked out. No model, and no problem statements are ever fetched or reproduced.

  dsa   Codeforces public API: problems tagged for the chapter topic at the learner's level, most-solved first. Every item is a real problem the API lists.
  exam  official sources only (NTA, CBSE, NCERT, JEE Advanced archive), chosen from the goal's own words; each link is checked before it is shown.
Anything that fails simply gives no practice block: a chapter never waits for or depends on it.
"""
from __future__ import annotations

import re
from typing import Optional

from .. import web

TAGS = (("binary search", "binary search"), ("graph", "graphs"), ("tree", "trees"), ("dynamic programming", "dp"), ("dp", "dp"), ("greedy", "greedy"),
        ("sort", "sortings"), ("string", "strings"), ("hash", "hashing"), ("math", "math"), ("two pointer", "two pointers"), ("recursion", "dfs and similar"),
        ("bit", "bitmasks"), ("array", "implementation"), ("stack", "data structures"), ("queue", "data structures"), ("heap", "data structures"), ("linked", "data structures"))
RATING = {"beginner": (800, 1200), "intermediate": (1200, 1700), "advanced": (1700, 2200)}
OFFICIAL = {
    "jee": [("JEE Main: official portal and syllabus (NTA)", "https://jeemain.nta.nic.in/"), ("JEE Advanced: past papers archive", "https://jeeadv.ac.in/archive.html")],
    "neet": [("NEET UG: official portal and syllabus (NTA)", "https://neet.nta.nic.in/")],
    "boards": [("CBSE: academic syllabus and sample papers", "https://cbseacademic.nic.in/"), ("NCERT: official textbooks", "https://ncert.nic.in/textbook.php")],
}
EXAM_WORDS = (("jee", r"\bjee\b|\biit\b"), ("neet", r"\bneet\b"), ("boards", r"\b(cbse|icse|board exam|boards|ncert|class (?:10|12)|grade (?:10|12))\b"))


def tag_for(topic: str) -> Optional[str]:
    t = (topic or "").lower()
    return next((tag for key, tag in TAGS if key in t), None)


def codeforces(topic: str, level: str = "beginner", n: int = 6) -> list[dict]:
    """Problems from the Codeforces API for the topic's tag and the level's rating band. [] when the API or the tag is unavailable."""
    tag = tag_for(topic)
    if not tag:
        return []
    lo, hi = RATING.get(level, RATING["beginner"])

    def fetch():
        data = web._json(web.http_get("https://codeforces.com/api/problemset.problems", params={"tags": tag}), "codeforces problemset")
        if data.get("status") != "OK":
            raise web.ToolFailure("server", "codeforces answered with a failure status", retryable=False)
        return {"problems": data["result"]["problems"], "stats": data["result"]["problemStatistics"]}

    res = web.tool("codeforces", fetch, cache_key=f"cf:{tag}", summary=f"codeforces problems tagged {tag}")
    got = res.value_or(None)
    if not got:
        return []
    solved = {(s["contestId"], s["index"]): s.get("solvedCount", 0) for s in got["stats"]}
    probs = [p for p in got["problems"] if p.get("rating") and lo <= p["rating"] <= hi and p.get("contestId")]
    probs.sort(key=lambda p: -solved.get((p["contestId"], p["index"]), 0))
    return [{"title": p["name"], "url": f"https://codeforces.com/problemset/problem/{p['contestId']}/{p['index']}", "detail": f"Rating {p['rating']} · {', '.join(p.get('tags', [])[:3])}"}
            for p in probs[:n]]


def official(goal: str) -> list[dict]:
    """Official portals for the exam the goal names, each checked live (a dead link is dropped, not shown)."""
    text = (goal or "").lower()
    items: list[dict] = []
    for exam, rx in EXAM_WORDS:
        if re.search(rx, text):
            for title, url in OFFICIAL[exam]:
                c = web.verify_link(web.Candidate(f"o{len(items)}", "link", url, title, url.split('/')[2]))
                if not c.extra.get("drop"):
                    items.append({"title": title, "url": url, "detail": "Official source"})
    return items


def practice_for(track: Optional[str], topic: str, level: str, goal: str = "") -> Optional[dict]:
    """A `practice` block's content, or None. Never raises."""
    try:
        if track == "dsa":
            items = codeforces(topic, level)
            return {"source": "Codeforces", "note": "Real problems; open them on Codeforces. Aasaan never copies a problem statement.", "items": items} if items else None
        if track == "exam":
            items = official(goal)
            return {"source": "Official sources", "note": "Syllabus and past papers from the official bodies.", "items": items} if items else None
    except Exception:  # noqa: BLE001
        return None
    return None
