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


# Pages on these hosts are storefronts or profiles, not a company's own website.
HOSTING_DOMAINS = {"business.site", "wixsite.com", "blogspot.com", "facebook.com", "indiamart.com", "google.com",
                   "g.co", "linkedin.com", "instagram.com", "justdial.com", "sulekha.com", "wordpress.com"}


def is_hosting(domain: str | None) -> bool:
    return bool(domain) and any(domain == h or domain.endswith("." + h) for h in HOSTING_DOMAINS)


EMPTY_VALUES = {"", "-", "--", "n/a", "na", "none", "null", "nil", "not available", "nan"}
LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
# Words that look like domains in free text but are not websites ("Pvt.Ltd", "Pune.Website").
NOT_A_TLD = {"ltd", "pvt", "inc", "co", "website", "site", "llp", "corp"}


def domain_of(website: str | None) -> str | None:
    """Normalised domain of a website value, or None when it is not a usable website
    (seen in CSV input: "N/A", "-", a bare word, a trailing dot)."""
    if not website or website.strip().lower() in EMPTY_VALUES:
        return None
    w = website.strip()
    if "://" not in w:
        w = "http://" + w
    try:
        host = (urlparse(w).hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    if len(labels) < 2 or not all(LABEL_RE.match(label) for label in labels) or not labels[-1].isalpha():
        return None
    return host


URL_RE = re.compile(r"(https?://[^\s,;]+|\b(?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b)", re.I)
WEBSITE_HINT = re.compile(r"(?:website|site|web|url|domain)\s*(?:is|:|-)?\s*(\S+)", re.I)


def _looks_like_website(token: str) -> bool:
    domain = domain_of(token.rstrip(".,;)"))
    return bool(domain) and domain.split(".")[-1] not in NOT_A_TLD and \
        not any(label in NOT_A_TLD and len(domain.split(".")) == 2 for label in domain.split(".")[:1])


def parse_free_text(text: str) -> dict:
    """Best-effort: pull a website and a name out of a request written in plain words.
    Prefers a website named as such ("website acme.in"), then an http/www link, then the last
    domain-like token; never a company suffix like "Pvt.Ltd" or "Pune.Website"."""
    text = text or ""
    website = None
    hinted = WEBSITE_HINT.search(text)
    if hinted and _looks_like_website(hinted.group(1)):
        website = hinted.group(1).rstrip(".,;)")
    else:
        candidates = [m.group(0) for m in URL_RE.finditer(text) if _looks_like_website(m.group(0))]
        linked = [c for c in candidates if c.lower().startswith(("http", "www."))]
        website = (linked or candidates or [None])[-1]
    name = None
    # A quoted name: the quote must not sit inside a word ("vendor's" is not a quote).
    q = re.search(r"(?<!\w)[\"“'‘]([^\"”'’]{2,80})[\"”'’](?!\w)", text)
    if q:
        name = q.group(1).strip()
    else:
        n = re.search(r"(?i:vendor|company|name)\s*(?:is|:)\s*([A-Z][\w&.\- ]{1,80}?)(?:[,;]|\.\s|\s+(?:and|with|website)\b|$)",
                      text)
        if n:
            name = n.group(1).strip()
    return {"name": name, "website": website}


_TITLE = r"(?:(?i:mr|mrs|ms|dr|shri|smt)\.?\s+)?"
_PERSON = _TITLE + r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+"
DIRECTORS_RE = re.compile(r"\b(?i:directors?|promoters?|proprietor)\b\s*(?i:are|is|:|-)?\s*"
                          r"(" + _PERSON + r"(?:\s*(?:,|;|&|\band\b)\s*" + _PERSON + r")*)")


def directors_in_text(text: str | None) -> list[str]:
    """Names given after "director(s)", "promoter(s)" or "proprietor" in a plain-words request, e.g.
    "directors: Ravi Menon and Suresh Patil". Each name needs at least two capitalised words."""
    found = []
    for m in DIRECTORS_RE.finditer(text or ""):
        for part in re.split(r"\s*(?:,|;|&|\band\b)\s*", m.group(1)):
            part = re.sub(r"^(mr|mrs|ms|dr|shri|smt)\.?\s+", "", part.strip(), flags=re.I)
            if len(part.split()) >= 2 and part not in found:
                found.append(part)
    return found[:8]


def name_variants(name: str | None) -> list[str]:
    """A record name like "Xpressbees (BusyBees Logistics Solutions Pvt. Ltd.)" is a brand plus a legal
    name; each counts as a name of the same business."""
    name = name or ""
    variants = [name]
    if "(" in name:
        variants.append(name.split("(")[0].strip())
        variants += [part.strip() for part in re.findall(r"\(([^)]+)\)", name)]
    return [v for v in dict.fromkeys(variants) if v]


def names_match(requested: str | None, record: str | None, threshold: float) -> bool:
    """Same business by name: close overall, or every distinctive word of one name is in the other,
    for any brand / legal variant of either."""
    for a in name_variants(requested):
        for b in name_variants(record):
            if similarity(a, b) >= threshold or names_same_business(a, b) or names_same_business(b, a):
                return True
    return False


def carries_brand(requested: str | None, record_name: str | None) -> bool:
    """The requested name contains the whole brand of a record (e.g. "Xpressbees Logistics" carries the
    brand of "Xpressbees (BusyBees Logistics Solutions Pvt. Ltd.)")."""
    return any(names_same_business(variant, requested) for variant in name_variants(record_name))


LEADER_TITLE = re.compile(r"\b(chief|ceo|cfo|coo|cto|cio|cmo|chro|cxo|founder|co-?founder|president|chairman|"
                          r"chairperson|managing director|director|md|owner|partner|proprietor|head)\b", re.I)


def is_leadership_title(title: str | None) -> bool:
    return bool(title) and LEADER_TITLE.search(title) is not None


def search_name(name: str | None) -> str:
    """Name to search the news with: legal suffixes dropped, and trailing generic words dropped
    while at least two words remain ('Shakti Bhog Food' -> 'Shakti Bhog', 'Acme Freight' stays)."""
    name = (name or "").split("(")[0].strip() or (name or "")     # the brand, not the bracketed legal name
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


# ---------------------------------------------------------------------------- company registration (CIN)
# Corporate Identity Number issued by the Registrar of Companies (MCA), e.g. L62200TG2008PLC061781:
#   L/U listed or unlisted | 5-digit industry (NIC) code | 2-letter state | year of incorporation |
#   3-letter company type | 6-digit registration number.
CIN_RE = re.compile(r"\b([LU])(\d{5})([A-Z]{2})(\d{4})([A-Z]{3})(\d{6})\b")
CIN_STATES = {"AN": "Andaman and Nicobar", "AP": "Andhra Pradesh", "AR": "Arunachal Pradesh", "AS": "Assam",
              "BR": "Bihar", "CH": "Chandigarh", "CT": "Chhattisgarh", "DL": "Delhi", "GA": "Goa",
              "GJ": "Gujarat", "HP": "Himachal Pradesh", "HR": "Haryana", "JH": "Jharkhand",
              "JK": "Jammu and Kashmir", "KA": "Karnataka", "KL": "Kerala", "MH": "Maharashtra",
              "ML": "Meghalaya", "MN": "Manipur", "MP": "Madhya Pradesh", "MZ": "Mizoram", "NL": "Nagaland",
              "OR": "Odisha", "PB": "Punjab", "PY": "Puducherry", "RJ": "Rajasthan", "SK": "Sikkim",
              "TG": "Telangana", "TN": "Tamil Nadu", "TR": "Tripura", "UP": "Uttar Pradesh",
              "UR": "Uttarakhand", "UT": "Uttarakhand", "WB": "West Bengal", "DN": "Dadra and Nagar Haveli",
              "DD": "Daman and Diu", "LD": "Lakshadweep"}
CIN_TYPES = {"PLC": "public limited company", "PTC": "private limited company", "OPC": "one person company",
             "GOI": "central government company", "SGC": "state government company",
             "NPL": "not-for-profit (section 8) company", "FLC": "public company (financial lease)",
             "FTC": "private company (subsidiary of a foreign company)", "GAP": "public company limited by guarantee",
             "GAT": "private company limited by guarantee", "ULL": "public unlimited company",
             "ULT": "private unlimited company"}


def find_cins(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(0) for m in CIN_RE.finditer((text or "").upper())))


def decode_cin(cin: str | None) -> dict | None:
    """What a CIN itself states. The format is fixed by the MCA, so these are facts of the identifier,
    not guesses: listed or not, state of registration, year of incorporation, type of company.
    Returns None when the value is not a well-formed CIN."""
    m = CIN_RE.fullmatch((cin or "").strip().upper())
    if not m:
        return None
    listed, nic, state, year, kind, number = m.groups()
    if state not in CIN_STATES or kind not in CIN_TYPES or not 1850 <= int(year) <= 2100:
        return None
    return {"cin": m.group(0), "listed": listed == "L", "industry_code": nic, "state": CIN_STATES[state],
            "year_incorporated": int(year), "company_type": CIN_TYPES[kind], "type_code": kind}


def parse_people_list(value) -> list[str]:
    """Director names given with a request: a list, or one string separated by ';', ',', '|' or newlines."""
    if not value:
        return []
    items = value if isinstance(value, list) else re.split(r"[;,|\n]", str(value))
    cleaned = []
    for item in items:
        name = re.sub(r"^(mr|mrs|ms|dr|shri|smt)\.?\s+", "", str(item).strip(), flags=re.I).strip(" .")
        if len(name) >= 3 and name.lower() not in (c.lower() for c in cleaned):
            cleaned.append(name)
    return cleaned[:8]


def same_person(a: str | None, b: str | None) -> bool:
    """Two spellings of one person's name: same tokens ignoring case, titles, dots and order."""
    def tokens(n):
        n = re.sub(r"^(mr|mrs|ms|dr|shri|smt)\.?\s+", "", (n or "").strip(), flags=re.I)
        return {t for t in re.split(r"[^a-z]+", n.lower()) if len(t) > 1}
    ta, tb = tokens(a), tokens(b)
    return bool(ta) and bool(tb) and (ta == tb or (len(ta) >= 2 and len(tb) >= 2 and (ta <= tb or tb <= ta)))
