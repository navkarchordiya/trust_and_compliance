"""Facts with sources. A fact is only recorded if its quote really is in the stored response.

API facts:  locator = JSON path inside the stored response, quote = the value at that path.
Web facts:  locator = "passage", quote = a passage that appears verbatim in the page text.
The LLM (if any) never supplies a fact; it can only point at text we already fetched.
"""
import json
import re
from html.parser import HTMLParser

from .sources import Result
from .store import Store


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self._skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    try:
        p.feed(html or "")
    except Exception:
        return norm_ws(re.sub(r"<[^>]+>", " ", html or ""))
    return norm_ws(" ".join(p.parts))


def norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def get_path(obj, path: str):
    """get_path(data, '[0].matches[0].company_data.basic_info.year_founded')"""
    for part in re.findall(r"\[(\d+)\]|\.?([^.\[\]]+)", path):
        idx, key = part
        if idx:
            obj = obj[int(idx)]
        else:
            obj = obj[key]
    return obj


class Evidence:
    def __init__(self, store: Store, job_id: str):
        self.store, self.job_id = store, job_id

    def field(self, stage: str, claim: str, res: Result, path: str, source_url: str | None = None):
        """Record a fact read from a JSON field. Returns the value, or None if the field is absent."""
        try:
            value = get_path(res.data, path)
        except (KeyError, IndexError, TypeError):
            return None
        if value in (None, "", [], {}):
            return None
        self.store.add_fact(job_id=self.job_id, stage=stage, claim=claim, value_json=json.dumps(value),
                            source_url=source_url or res.url, locator=path, quote=json.dumps(value)[:500],
                            retrieved_at=res.retrieved_at, call_id=res.call_id)
        return value

    def passage(self, stage: str, claim: str, value, res: Result, page_text: str, passage: str,
                source_url: str) -> bool:
        """Record a fact backed by a passage. Refuses if the passage is not verbatim in the page."""
        passage = norm_ws(passage)
        if not passage or passage.lower() not in page_text.lower():
            return False
        self.store.add_fact(job_id=self.job_id, stage=stage, claim=claim, value_json=json.dumps(value),
                            source_url=source_url, locator="passage", quote=passage[:500],
                            retrieved_at=res.retrieved_at, call_id=res.call_id)
        return True


def snippet_around(text: str, needle: str, width: int = 160) -> str:
    i = text.lower().find(needle.lower())
    if i < 0:
        return ""
    start, end = max(0, i - width), min(len(text), i + len(needle) + width)
    # cut on word boundaries so the passage is still verbatim
    while start > 0 and text[start - 1] != " ":
        start += 1
        if start >= i:
            break
    while end < len(text) and text[end] != " ":
        end -= 1
        if end <= i + len(needle):
            end = i + len(needle)
            break
    return text[start:end].strip()
