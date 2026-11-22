import re
from difflib import SequenceMatcher
from urllib.parse import urlparse

SUFFIXES = {"private", "pvt", "limited", "ltd", "llp", "inc", "corp", "corporation", "co", "company",
            "the", "india", "opc", "plc", "llc", "gmbh", "pte"}


def core_name(name: str | None) -> str:
    words = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).split()
    return " ".join(w for w in words if w not in SUFFIXES)


GENERIC = {"food", "foods", "logistics", "logistic", "services", "service", "solutions", "industries",
           "enterprises", "enterprise", "traders", "trading", "group", "international", "global",
           "express", "transport", "transports", "carriers", "carrier", "cargo", "freight", "supply",
           "chain", "technologies", "technology", "tech", "ventures", "holdings", "exports", "imports",
           "and", "of"}


def distinctive_tokens(name: str | None) -> set[str]:
    return {w for w in core_name(name).split() if w not in GENERIC and len(w) > 1}


def names_same_business(requested: str | None, candidate: str | None) -> bool:
    """Every distinctive word of the requested name appears in the candidate's name.
    'Shakti Bhog Foods Limited' matches 'Shakti Bhog Food' but not 'Hari Bhog Foods' or 'Shakti Foods'."""
    want = distinctive_tokens(requested)
    have = set(core_name(candidate).split())
    return bool(want) and want <= have


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


def search_name(name: str | None) -> str:
    """Name to search the news with: legal suffixes dropped, and trailing generic words dropped
    while at least two words remain ('Shakti Bhog Food' -> 'Shakti Bhog', 'Acme Freight' stays)."""
    words = [w for w in re.sub(r"[^A-Za-z0-9& ]+", " ", name or "").split() if w.lower() not in SUFFIXES]
    while len(words) > 2 and words[-1].lower() in GENERIC:
        words.pop()
    return " ".join(words) or (name or "")


def mentions_business(name: str | None, text: str | None) -> bool:
    """Every word of the search name appears as a word in the text."""
    want = set(core_name(search_name(name)).split())
    return bool(want) and want <= set(core_name(text).split())


def headcount_rank(employee_count_range: str | None) -> int:
    """'201-500' -> 500, '10001+' -> 10001, None -> 0."""
    nums = re.findall(r"\d+", employee_count_range or "")
    return int(nums[-1]) if nums else 0
# ---------------------------------------------------------------- GSTIN (Indian GST registration number)
# Layout: 2-digit state code, 10-char PAN, entity number, 'Z', check character.
GSTIN_RE = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b")
_GST_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# 4th character of the PAN inside the GSTIN says what kind of taxpayer holds it.
PAN_HOLDER_TYPES = {"C": "company", "F": "firm / LLP", "P": "individual", "H": "Hindu undivided family",
                    "A": "association of persons", "T": "trust", "B": "body of individuals",
                    "G": "government", "J": "artificial juridical person", "L": "local authority"}


def gstin_checksum_ok(gstin: str) -> bool:
    """The official GSTIN check-character algorithm (mod 36, alternating weights 1 and 2)."""
    gstin = (gstin or "").upper()
    if not GSTIN_RE.fullmatch(gstin):
        return False
    total = 0
    for position, char in enumerate(gstin[:14]):
        weight = 1 if position % 2 == 0 else 2
        product = _GST_CHARS.index(char) * weight
        total += product // 36 + product % 36
    check_char = _GST_CHARS[(36 - total % 36) % 36]
    return gstin[14] == check_char


def find_gstins(text: str) -> list[str]:
    """Every distinct GSTIN-shaped string in the text, in order of appearance."""
    seen, found = set(), []
    for match in GSTIN_RE.finditer((text or "").upper()):
        gstin = match.group(1)
        if gstin not in seen:
            seen.add(gstin)
            found.append(gstin)
    return found


def pan_holder_type(gstin: str) -> str | None:
    return PAN_HOLDER_TYPES.get((gstin or "   ")[5:6].upper()) if gstin and len(gstin) >= 6 else None


def claims_company(name: str | None) -> bool:
    """True when the name itself says it is a company (Pvt Ltd, Limited, ...)."""
    words = set(re.sub(r"[^a-z ]+", " ", (name or "").lower()).split())
    return bool(words & {"pvt", "private", "limited", "ltd"})
