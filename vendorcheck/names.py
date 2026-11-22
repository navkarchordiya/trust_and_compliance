import re
from difflib import SequenceMatcher
from urllib.parse import urlparse

SUFFIXES = {"private", "pvt", "limited", "ltd", "llp", "inc", "corp", "corporation", "co", "company",
            "the", "india", "opc", "plc", "llc", "gmbh", "pte"}


def core_name(name: str | None) -> str:
    words = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).split()
    return " ".join(w for w in words if w not in SUFFIXES)


def similarity(a: str | None, b: str | None) -> float:
    a, b = core_name(a), core_name(b)
    if not a or not b:
        return 0.0
    return round(SequenceMatcher(None, a, b).ratio(), 3)


def domain_of(website: str | None) -> str | None:
    if not website:
        return None
    w = website.strip()
    if "://" not in w:
        w = "http://" + w
    host = (urlparse(w).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host or None


URL_RE = re.compile(r"(https?://[^\s,;]+|\b(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b)", re.I)


def parse_free_text(text: str) -> dict:
    """Best-effort: pull a website and a name out of a request written in plain words."""
    m = URL_RE.search(text or "")
    website = m.group(0) if m else None
    name = None
    q = re.search(r"[\"“']([^\"”']{2,80})[\"”']", text or "")
    if q:
        name = q.group(1)
    else:
        n = re.search(r"(?:vendor|company|name)\s*(?:is|:)\s*([A-Z][\w&.\- ]{1,80}?)(?:[,.;]|\s+(?:and|with|website)\b|$)",
                      text or "")
        if n:
            name = n.group(1).strip()
    return {"name": name, "website": website}
