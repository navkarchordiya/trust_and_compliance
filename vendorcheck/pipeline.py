"""The service, as six stages from the organizers' guide:

  Take the request -> Plan -> Gather the facts -> Decide -> Act, or hand over -> Show its work

Gathering is split into resumable sub-stages. After each stage its output is saved and the
job advances in one transaction, so a killed run restarts at the stage it was in.
"""
import json
import re
import time
import uuid
from datetime import datetime, timezone

from . import names
from .crustdata import Crustdata, rdap_domain
from .evidence import Evidence, html_to_text, snippet_around
from .sources import FOUND, NOT_FOUND, SourceClient, urllib_transport
from .store import Store, now_iso

STAGES = ["take_request", "plan", "identify", "enrich", "domain_age", "registry", "leaders", "media",
          "decide", "hand_over", "done"]
GATHER_STAGES = STAGES[: STAGES.index("decide")]


def request_keys(req: dict) -> list[str]:
    """Keys that identify the vendor from the request alone: website, GSTIN, and the core name.
    Used for duplicates against the client's vendor list and other open jobs."""
    keys = [req.get("domain")]
    if req.get("gstin"):
        keys.append(f"gstin:{req['gstin']}")
    if names.core_name(req.get("name")):
        keys.append(f"name:{names.core_name(req.get('name'))}")
    return [k for k in keys if k]


def new_job(store: Store, request: dict, job_id: str | None = None) -> str:
    """Create a job. A caller-chosen job_id (used by batch runs) makes re-submitting the same
    request after a crash resume the existing job instead of creating a duplicate."""
    job_id = job_id or "job_" + datetime.now().strftime("%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]
    if store.job(job_id) is None:
        store.create_job(job_id, request, STAGES[0])
    return job_id


def current_jobs(person: dict) -> list[tuple[str, dict]] | None:
    """Current jobs as (json path inside the person, job). Handles both shapes Crustdata returns:
    experience.employment_details.current[] (Company Enrich people) and experience.current[]."""
    exp = person.get("experience")
    if not isinstance(exp, dict):
        return None
    if isinstance(exp.get("employment_details"), dict):
        return [(f"experience.employment_details.current[{j}]", job)
                for j, job in enumerate(exp["employment_details"].get("current") or [])]
    if isinstance(exp.get("current"), list):
        return [(f"experience.current[{j}]", job) for j, job in enumerate(exp["current"])]
    return None


def job_matches(job: dict, company_id: str, domain: str, company_name: str | None) -> bool:
    ids = job.get("employer_company_id") or job.get("crustdata_company_id") or []
    ids = [str(x) for x in (ids if isinstance(ids, list) else [ids])]
    sites = [names.domain_of(s) for s in (job.get("employer_website") or [])] if isinstance(
        job.get("employer_website"), list) else [names.domain_of(job.get("employer_website"))]
    if company_id and company_id in ids:
        return True
    if domain and domain in sites:
        return True
    return names.similarity(job.get("employer_name") or job.get("name"), company_name) >= 0.85


def cite_job(ev, stage, person_name, res, job_path, job, profile_url):
    """Cite just the employer name and title of a current job, not the whole record."""
    key_emp = "employer_name" if "employer_name" in job else "name"
    key_title = "employee_title" if "employee_title" in job else "title"
    ev.field(stage, f"{person_name}'s own profile: current employer", res, f"{job_path}.{key_emp}",
             source_url=profile_url)
    ev.field(stage, f"{person_name}'s own profile: current title", res, f"{job_path}.{key_title}",
             source_url=profile_url)


BLOCK_MARKERS = ("checking your browser", "request could not be satisfied", "access denied", "just a moment",
                 "attention required", "enable javascript and cookies", "are you a robot", "captcha")


def blocked_reason(entry: dict) -> str:
    """Web Fetch can report success=true for a 403 or a bot-check page. Detect those."""
    status = entry.get("http_status")
    if isinstance(status, int) and status >= 400:
        return f"HTTP {status}"
    content = entry.get("content") or ""
    if (entry.get("title") or "").strip().lower() == "google" or "url=/goto?" in content[:3000]:
        return "a search-engine redirect page, not the article"
    head = f"{entry.get('title') or ''} {content[:600]}".lower()
    hit = next((m for m in BLOCK_MARKERS if m in head), None)
    return f"block page: '{hit}'" if hit else ""


class Pipeline:
    def __init__(self, store: Store, cfg: dict, transport=urllib_transport, sleep=time.sleep, log=print):
        self.store, self.cfg, self.transport, self.sleep, self.log = store, cfg, transport, sleep, log

    # ------------------------------------------------------------------ driver
    def run(self, job_id: str) -> dict:
        job = self.store.job(job_id)
        self.store.log_event(job_id, "run_start" if job["stage"] == STAGES[0] else "resume",
                             stage=job["stage"])
        client = SourceClient(self.store, job_id, self.cfg, self.transport, self.sleep)
        self.cd = Crustdata(client, self.cfg)
        self.client = client
        self.ev = Evidence(self.store, job_id)
        self.job_id = job_id
        self.request = json.loads(job["request_json"])

        while True:
            stage = self.store.job(job_id)["stage"]
            if stage == "done":
                break
            self.log(f"[{job_id}] stage: {stage}")
            self.store.log_event(job_id, "stage_start", stage=stage)
            t0 = time.time()
            self.store.clear_facts(job_id, stage)       # a re-run stage rebuilds its own facts
            out = getattr(self, "s_" + stage)()
            self.store.log_event(job_id, "stage_done", stage=stage, seconds=round(time.time() - t0, 2),
                                 status=out.get("status"), stopped_early=bool(out.get("_stop")))
            nxt = STAGES[STAGES.index(stage) + 1]
            if out.get("_stop"):                          # e.g. nothing to look up
                nxt = "decide" if STAGES.index(stage) < STAGES.index("decide") else nxt
            self.store.save_stage(job_id, stage, out, nxt)
        return self.out("done") or {}

    def out(self, stage: str) -> dict | None:
        return self.store.stage_output(self.job_id, stage)

    # ------------------------------------------------------------------ 1. take the request
    def s_take_request(self) -> dict:
        r = dict(self.request)
        if r.get("text") and not (r.get("name") or r.get("website")):
            r.update({k: v for k, v in names.parse_free_text(r["text"]).items() if v})
        domain = names.domain_of(r.get("website"))
        notes = []
        if not domain:
            notes.append("No website given: identifying by name only, so a lookalike name is more likely.")
        if not r.get("name"):
            notes.append("No vendor name given: cannot compare the claimed name with the record.")
        gstin = (r.get("gstin") or "").strip().upper() or None
        out = {"name": r.get("name"), "domain": domain, "gstin": gstin, "notes": notes}
        if not domain and not r.get("name"):
            out["_stop"] = True
            out["missing"] = "The request has neither a vendor name nor a website."
        return out

    # ------------------------------------------------------------------ 2. plan
    def s_plan(self) -> dict:
        p = self.cfg["people"]["max_leaders"]
        m = self.cfg["media"]["max_articles_fetched"]
        est = self.cfg["credit_estimates"]
        steps = [
            ("identify company", est["/company/identify"]),
            ("enrich company (founders, headcount, news)", est["/company/enrich"]),
            ("domain registration date (RDAP)", est["rdap"]),
            (f"read GSTIN from up to {self.cfg.get('registry', {}).get('max_pages', 1)} website page(s)",
             self.cfg.get("registry", {}).get("max_pages", 1) * est["/web/enrich/live"]),
            ("GST registry lookup", est.get("/gstin", 0)),
            (f"confirm up to {p} leaders", p * est["/person/enrich"]),
            (f"news search: company + {p} leaders", (1 + p) * est["/web/search/live"]),
            (f"fetch up to {m} articles", m * est["/web/enrich/live"]),
        ]
        total = round(sum(c for _, c in steps), 2)
        limit = self.cfg["limits"]["max_credits_per_run"]
        return {"steps": [{"step": s, "credits_estimate": c} for s, c in steps],
                "credits_estimate": total, "credit_limit": limit,
                "fits_budget": total <= limit}

    # ------------------------------------------------------------------ 3. gather: identify
    def s_identify(self) -> dict:
        req = self.out("take_request")
        res = self.cd.identify(domain=req["domain"]) if req["domain"] else None
        by = "domain"
        if (res is None or res.status == NOT_FOUND) and req["name"]:
            res, by = self.cd.identify(name=req["name"]), "name"
        if res.status != FOUND:
            # Even with no company record, the same website asked twice is still a duplicate.
            return self._with_duplicates({"status": res.status, "by": by, "error": res.error,
                                          "missing": f"Company Identify by {by} returned {res.status}"},
                                         request_keys(req))

        matches = res.data[0]["matches"]
        cands = []
        for i, m in enumerate(matches):
            info = m["company_data"].get("basic_info", {}) or {}
            domains = {names.domain_of(d) for d in (info.get("all_domains") or [])}
            domains |= {names.domain_of(info.get("primary_domain")), names.domain_of(info.get("website"))}
            cands.append({"i": i, "confidence": m.get("confidence_score"),
                          "id": m["company_data"].get("crustdata_company_id"),
                          "name": info.get("name"),
                          "domain": names.domain_of(info.get("primary_domain")),
                          "all_domains": sorted(d for d in domains if d),
                          "employees": info.get("employee_count_range"),
                          "name_similarity": names.similarity(req["name"], info.get("name")) if req["name"] else None})
        icfg = self.cfg["identity"]
        top = cands[0]
        similar_records = []

        if by == "name":
            # Name-only lookups score every candidate the same (0.6 seen live) and pad the answer with
            # unrelated firms (Reliance, ITC, "Tyson Foods" ...). Only candidates carrying every
            # distinctive word of the name, and reasonably similar overall, count as this business.
            group = [c for c in cands if names.names_same_business(req["name"], c["name"])
                     and (c["name_similarity"] or 0) >= icfg["min_name_similarity"]]
            # Several records can share one website (seen: "Shakti Bhog" 201-500 staff, "Shakti Bhog
            # Food" 3 staff); the biggest is the main profile.
            rank = lambda c: (bool(c["domain"]), names.headcount_rank(c.get("employees")), c["name_similarity"] or 0)
            sites = {c["domain"] for c in group if c["domain"]}
            if not group:
                top = max(cands, key=lambda c: c["name_similarity"] or 0)
                status = "NAME_UNCONFIRMED"      # no record really carries this name: cannot verify, no accusation
            elif req["domain"]:
                # Identify by the WEBSITE found nothing, so the request's website is unknown to the source.
                same_site = [c for c in group if req["domain"] in c["all_domains"]]
                top = same_site[0] if same_site else max(group, key=rank)
                if same_site:
                    status = "RESOLVED"
                elif top["all_domains"]:
                    # The NAME matches a firm with another website: the lookalike pattern.
                    # Never borrow the real firm's evidence for this vendor.
                    status = "DOMAIN_MISMATCH"
                else:
                    status = "NAME_UNCONFIRMED"  # the namesake record has no website to compare with
            else:
                top = max(group, key=rank)
                status = "RESOLVED" if len(sites) == 1 else "AMBIGUOUS"
                if status == "RESOLVED":
                    similar_records = [f"{c['name']} ({c['domain'] or 'no website'})" for c in group
                                       if c["id"] != top["id"] and c["domain"] != top["domain"]]
            candidates_shown = (group or cands)[:5]
        else:
            exact = bool(req["domain"]) and req["domain"] in top["all_domains"]
            close = [c for c in cands[1:]
                     if (top["confidence"] or 0) - (c["confidence"] or 0) < icfg["ambiguity_gap"]]
            resolved = (top["confidence"] or 0) >= icfg["min_confidence"] and (exact or not close)
            status = "RESOLVED" if resolved else "AMBIGUOUS"
            candidates_shown = cands[:5]
        sim = top["name_similarity"]
        exact_domain = bool(req["domain"]) and req["domain"] in top["all_domains"]

        base = f"[0].matches[{top['i']}].company_data"
        self.ev.field("identify", "Company record matched to the request", res, f"{base}.basic_info.name")
        self.ev.field("identify", "Primary domain on the company record", res, f"{base}.basic_info.primary_domain")
        self.ev.field("identify", "Match confidence", res, f"[0].matches[{top['i']}].confidence_score")

        out = {"status": status, "by": by, "top": top, "candidates": candidates_shown,
               "exact_domain": exact_domain, "name_similarity": sim, "similar_records": similar_records}
        if status in ("RESOLVED", "AMBIGUOUS"):
            keys = [f"cd:{top['id']}", req["domain"] or top["domain"]] + request_keys(req)
        else:
            keys = request_keys(req)      # only what the request itself says identifies this vendor
        return self._with_duplicates(out, list(dict.fromkeys(keys)))

    def _with_duplicates(self, out: dict, keys: list[str]) -> dict:
        """Duplicate check on the normalised domain and the Crustdata company ID: against the vendor
        register (already approved or rejected) and against other open jobs."""
        keys = [k for k in keys if k]
        dup_reg = self.store.register_lookup(keys)
        dup_job = self.store.earlier_job_for(self.job_id, keys)
        out.update(company_keys=keys,
                   duplicate_of=dup_reg,
                   earlier_job={"id": dup_job["id"], "verdict": dup_job["verdict"]} if dup_job else None)
        if dup_reg or dup_job:
            out["_stop"] = True     # a duplicate is held for a person; no need to pay for the rest
        return out

    # ------------------------------------------------------------------ 3. gather: enrich
    def s_enrich(self) -> dict:
        ident = self.out("identify")
        if ident.get("status") not in ("RESOLVED", "AMBIGUOUS"):
            return {"status": "SKIPPED", "reason": "company not identified"}
        res = self.cd.enrich_company(ident["top"]["id"])
        if res.status != FOUND:
            return {"status": res.status, "error": res.error,
                    "missing": f"Company Enrich returned {res.status}"}
        b = "[0].matches[0].company_data"
        f = lambda claim, path: self.ev.field("enrich", claim, res, f"{b}.{path}")
        out = {
            "status": "FOUND",
            "name": f("Company name", "basic_info.name"),
            "website": f("Website", "basic_info.website"),
            "record_status": f("Company status on the record", "basic_info.status"),
            "year_founded": f("Year founded", "basic_info.year_founded"),
            "company_type": f("Company type", "basic_info.company_type"),
            "headcount": f("Employee headcount", "headcount.total"),
            "headcount_growth_yoy": f("Headcount growth, year on year (%)", "headcount.growth_percent.yoy"),
            "employee_range": f("Employee count range", "basic_info.employee_count_range"),
            "hq": f("Headquarters", "locations.headquarters"),
            "hq_country": f("Headquarters country", "locations.country"),
            "openings": f("Open job postings", "hiring.openings_count"),
        }
        cd = res.data[0]["matches"][0]["company_data"]
        company_id = str(cd.get("crustdata_company_id") or ident["top"]["id"])
        domain = (self.out("take_request").get("domain") or ident["top"].get("domain") or "").lower()
        min_conn = self.cfg["people"].get("min_connections", 0)

        # Founders first, then CXOs ranked by network size (junk self-reported titles have ~0).
        ranked = []
        for group in ("founders", "cxos"):
            members = list(enumerate(((cd.get("people") or {}).get(group)) or []))
            if group == "cxos":
                members = [(i, p) for i, p in members
                           if ((p.get("professional_network") or {}).get("connections") or 0) >= min_conn]
                members.sort(key=lambda ip: -((ip[1].get("professional_network") or {}).get("connections") or 0))
            ranked += [(group, i, p) for i, p in members if isinstance(p, dict)]

        people, seen = [], set()
        for group, i, p in ranked:
            if len(people) >= self.cfg["people"]["max_leaders"]:
                break
            bp = p.get("basic_profile") or {}
            pname = bp.get("name")
            if not pname or pname.lower() in seen:
                continue
            seen.add(pname.lower())
            ppath = f"{b}.people.{group}[{i}]"
            self.ev.field("enrich", f"Listed among the company's {group}", res, f"{ppath}.basic_profile.name")
            person = {"name": pname, "title": bp.get("current_title"), "group": group,
                      "location": (bp.get("location") or {}).get("raw"),
                      "profile_url": ((p.get("social_handles") or {}).get("professional_network_identifier")
                                      or {}).get("profile_url"),
                      "confirmed": None}
            jobs = current_jobs(p)
            if jobs is not None:
                hit = next((j for j in jobs if job_matches(j[1], company_id, domain, out["name"])), None)
                if hit:
                    cite_job(self.ev, "enrich", pname, res, f"{ppath}.{hit[0]}", hit[1],
                             person["profile_url"] or res.url)
                    person.update(confirmed=True, current_title=hit[1].get("employee_title") or hit[1].get("title"),
                                  since=(hit[1].get("employee_start_date") or hit[1].get("start_date") or "")[:10])
                else:
                    person.update(confirmed=False,
                                  current_employers=[j[1].get("employer_name") or j[1].get("name") for j in jobs])
            people.append(person)
        out["people"] = people
        starts = [p["since"] for p in people if p.get("group") == "founders" and p.get("since")]
        out["earliest_founder_start"] = min(starts) if starts else None

        news = (cd.get("news") or [])
        if isinstance(news, dict):
            news = news.get("articles") or []
        out["news"] = [{"title": n.get("article_title"), "url": n.get("article_url"),
                        "date": n.get("article_publish_date")} for n in news[:10] if isinstance(n, dict)]
        if out["news"]:
            self.ev.field("enrich", "Most recent news article on the company record", res,
                          f"{b}.news[0].article_title", record_date=out["news"][0]["date"])
        return out

    # ------------------------------------------------------------------ 3. gather: domain age
    def s_domain_age(self) -> dict:
        domain = self.out("take_request")["domain"] or (self.out("identify").get("top") or {}).get("domain")
        if not domain:
            return {"status": "SKIPPED", "reason": "no domain"}
        res = rdap_domain(self.client, domain)
        if res.status != FOUND or not isinstance(res.data, dict):
            return {"status": res.status, "error": res.error, "missing": f"Domain registry (RDAP) for {domain}"}
        for i, e in enumerate(res.data.get("events") or []):
            if e.get("eventAction") == "registration":
                date = self.ev.field("domain_age", f"Domain {domain} registration date", res,
                                     f"events[{i}].eventDate", record_date=e.get("eventDate"))
                d = datetime.fromisoformat(date.replace("Z", "+00:00"))
                return {"status": "FOUND", "domain": domain, "registered": date,
                        "age_days": (datetime.now(timezone.utc) - d).days}
        return {"status": "NOT_FOUND", "missing": f"RDAP record for {domain} has no registration date"}

    # ------------------------------------------------------------------ 3. gather: registry (GST)
    def s_registry(self) -> dict:
        """GSTINs shown on the vendor's own website, checked with the GSTIN checksum and then
        against the GST registry. The website is a claim; the registry is the record."""
        from .crustdata import gst_verify
        ident, req, rcfg = self.out("identify") or {}, self.out("take_request"), self.cfg.get("registry", {})
        domain = req.get("domain") or ((ident.get("top") or {}).get("domain")
                                       if ident.get("status") == "RESOLVED" else None)
        pages, gstins = [], []
        if req.get("gstin"):
            gstins.append(req["gstin"])          # given on the vendor's onboarding form (a claim, like the website)
        if not domain and not gstins:
            return {"status": "SKIPPED", "reason": "no website or GSTIN given"}

        for path in ([] if gstins or not domain else rcfg.get("website_paths", [""])[: rcfg.get("max_pages", 1)]):
            url = f"https://{domain}/{path}".rstrip("/")
            page = self.cd.web_fetch(url)
            pages.append({"url": url, "status": page.status, "error": page.error})
            if page.status != FOUND:
                continue
            text = html_to_text(((page.data or [{}])[0] or {}).get("content") or "")
            for gstin in names.find_gstins(text):
                if gstin in gstins:
                    continue
                gstins.append(gstin)
                self.ev.passage("registry", "GSTIN shown on the vendor's own website", gstin, page, text,
                                snippet_around(text, gstin, 80), url)
            if gstins:
                break
        if not gstins and not any(p["status"] == FOUND for p in pages):
            return {"status": "UNAVAILABLE", "pages": pages, "gstins": [],
                    "missing": f"the vendor's website ({domain}) could not be fetched"}

        checked = []
        for gstin in gstins[:2]:
            item = {"gstin": gstin, "checksum_ok": names.gstin_checksum_ok(gstin),
                    "state_code": gstin[:2], "pan_holder_type": names.pan_holder_type(gstin)}
            if not item["checksum_ok"]:
                item["registry"] = "NOT_CHECKED"
                checked.append(item)
                continue
            res = gst_verify(self.client, self.cfg, gstin)
            if res is None:
                item.update(registry="NOT_CONFIGURED", why="no GST registry key configured")
            elif res.status != FOUND or not isinstance(res.data, dict):
                item.update(registry=res.status, why=res.error or "registry did not answer")
            else:
                prefix = "data." if isinstance(res.data.get("data"), dict) else ""
                rec = res.data.get("data") if prefix else res.data
                reg_date = rec.get("date_of_registration")
                g = lambda claim, key: self.ev.field("registry", claim, res, prefix + key, record_date=reg_date)
                item.update(
                    registry="FOUND",
                    valid=rec.get("valid"),
                    status=g(f"GST registration status of {gstin}", "gst_in_status"),
                    legal_name=g(f"Legal name on GST registration {gstin}", "legal_name_of_business"),
                    trade_name=g(f"Trade name on GST registration {gstin}", "trade_name_of_business"),
                    registered=g(f"Date of GST registration {gstin}", "date_of_registration"),
                    constitution=g(f"Constitution of business on GST registration {gstin}",
                                   "constitution_of_business"),
                )
            checked.append(item)
        return {"status": "DONE", "pages": pages, "gstins": checked}

    # ------------------------------------------------------------------ 3. gather: who runs it
    def s_leaders(self) -> dict:
        """Company Enrich already carries each leader's employment history. Person Enrich is only
        called for a leader whose history was missing there."""
        enr, ident = self.out("enrich") or {}, self.out("identify") or {}
        company_id = str((ident.get("top") or {}).get("id") or "")
        domain = (self.out("take_request").get("domain") or "").lower()
        checked = []
        for p in (enr.get("people") or [])[: self.cfg["people"]["max_leaders"]]:
            if p.get("confirmed") is not None:
                checked.append(p)
                continue
            if not p.get("profile_url"):
                checked.append({**p, "why": "no employment history and no profile URL on the record"})
                continue
            res = self.cd.enrich_person(p["profile_url"])
            if res.status != FOUND:
                checked.append({**p, "why": f"Person Enrich {res.status} {res.error}".strip()})
                continue
            pd = res.data[0]["matches"][0]["person_data"]
            jobs = current_jobs(pd) or []
            hit = next((j for j in jobs if job_matches(j[1], company_id, domain, enr.get("name"))), None)
            base = "[0].matches[0].person_data"
            if hit:
                cite_job(self.ev, "leaders", p["name"], res, f"{base}.{hit[0]}", hit[1], p["profile_url"])
                checked.append({**p, "confirmed": True,
                                "current_title": hit[1].get("employee_title") or hit[1].get("title")})
            else:
                checked.append({**p, "confirmed": False,
                                "current_employers": [j[1].get("employer_name") or j[1].get("name") for j in jobs]})
        return {"leaders": checked}

    # ------------------------------------------------------------------ 3. gather: adverse media (5.2)
    def s_media(self) -> dict:
        mcfg = self.cfg["media"]
        term_patterns = [(term.strip(), re.compile(r"\b" + re.escape(term.strip().lower()) + r"\b"))
                         for term in mcfg["adverse_terms"]]
        victim_markers = [marker.lower() for marker in mcfg.get("victim_context", [])]
        enr, ident, req = self.out("enrich") or {}, self.out("identify") or {}, self.out("take_request")
        if ident.get("status") not in ("RESOLVED", "AMBIGUOUS"):
            return {"status": "SKIPPED", "reason": "identity not resolved: news about the matched name "
                                                   "could be about a different firm"}
        company = enr.get("name") or (ident.get("top") or {}).get("name") or req.get("name")
        domain = req.get("domain") or (ident.get("top") or {}).get("domain")
        if not company:
            return {"status": "SKIPPED", "reason": "no company name to search"}
        leaders = [l["name"] for l in (self.out("leaders") or {}).get("leaders", [])]
        subjects = [("company", names.search_name(company))] + [("person", n) for n in leaders]
        core = names.core_name(company)

        def sentence_names(kind: str, subject: str, sentence: str) -> bool:
            if kind == "company":
                return names.mentions_business(subject, sentence)
            return re.search(r"\b" + re.escape(subject.lower()) + r"\b", sentence.lower()) is not None

        findings, fetched = [], 0
        # One combined search for the company and every leader (one call instead of 1 + N:
        # live search takes 6-25 s per call). Each result is matched back to whoever it names.
        q = ("(" + " OR ".join(f'"{name}"' for _, name in subjects) + ") ("
             + " OR ".join(mcfg["query_terms"]) + ")")
        res = self.cd.web_search(q, sources=("news",), location=mcfg.get("country"))
        searches = [{"subject": ", ".join(n for _, n in subjects), "query": q,
                     "status": res.status, "error": res.error}]
        results = (res.data.get("results") or []) if res.status == FOUND and isinstance(res.data, dict) else []
        pages: dict[str, tuple] = {}            # url -> (Result, text, blocked reason): each article read once

        for ri, r in enumerate(results[: mcfg["max_results_per_query"]]):
            blob = f"{r.get('title') or ''}. {r.get('snippet') or ''}"
            sentences = [s for s in re.split(r"(?<=[.!?])\s+|\n", blob) if s.strip()]
            published = r.get("date") or r.get("published_date") or r.get("publish_date")   # not given live
            is_victim = any(marker in blob.lower() for marker in victim_markers)
            for kind, subject in subjects:
                # The adverse word must sit in the SAME sentence as the name, as a whole word:
                # "by default", or "fraud" two sentences away from the name, is not news about them.
                naming = [s for s in sentences if sentence_names(kind, subject, s)]
                hit_terms = sorted({term for s in naming for term, rx in term_patterns if rx.search(s.lower())})
                if not hit_terms:
                    continue
                item = {"subject": subject, "kind": kind, "title": (r.get("title") or "")[:200],
                        "url": r.get("url"), "terms": hit_terms, "published": published}
                if is_victim:
                    item.update(link="NOT_ADVERSE", clearance="weak",
                                why="the vendor appears as the victim or is being impersonated, not accused")
                    findings.append(item)
                    continue
                if kind == "company":
                    # The headline itself names the vendor next to an adverse term: that is the link.
                    # (Namesake clearing is for people; a person reviews company-name collisions.)
                    self.ev.field("media", f"News headline naming {subject}", res, f"results[{ri}].title",
                                  source_url=r.get("url"), record_date=published)
                    item.update(link="LINKED", why=f"the headline names {subject}")
                    findings.append(item)
                    continue
                if r["url"] not in pages:
                    if fetched >= mcfg["max_articles_fetched"]:
                        item.update(link="UNRESOLVED", why="article not read: fetch limit reached")
                        findings.append(item)
                        continue
                    fetched += 1
                    page = self.cd.web_fetch(r["url"])
                    entry = ((page.data or [{}])[0] or {}) if page.status == FOUND else {}
                    pages[r["url"]] = (page, html_to_text(entry.get("content") or ""), blocked_reason(entry))
                page, text, blocked = pages[r["url"]]
                if page.status != FOUND:
                    item.update(link="UNRESOLVED", why=f"article could not be fetched ({page.status})")
                    findings.append(item)
                    continue
                if blocked:          # a block page is not the article: never clear a namesake from it
                    item.update(link="UNRESOLVED", why=f"site blocked automated reading ({blocked})")
                    findings.append(item)
                    continue
                if subject.lower() not in text.lower():
                    # We did not read the article (other language, redirect, paywall). Absence of the
                    # vendor's name in text we could not read is not evidence of a namesake.
                    item.update(link="UNRESOLVED", why=f"the fetched page does not contain '{subject}', so the "
                                                        "article was not read (other language, redirect or paywall)")
                    findings.append(item)
                    continue
                mentions_vendor = (core and core in names.core_name(text)) or (domain and domain in text.lower())
                passage = snippet_around(text, subject) or snippet_around(text, hit_terms[0])
                if mentions_vendor:
                    item.update(link="LINKED", why=f"article names {subject} and the vendor ({company})")
                    self.ev.passage("media", f"Adverse news linked to {subject}", item["title"], page, text,
                                    passage, r["url"], record_date=published)
                else:
                    item.update(link="NAMESAKE_CLEARED", clearance="weak",
                                why=f"article names a '{subject}' but never mentions {company} or {domain}; "
                                    "treated as a different person (weak clearance: based on absence, "
                                    "a person should confirm)")
                    self.ev.passage("media", f"News about a namesake of {subject}, not linked to the vendor",
                                    item["title"], page, text, passage, r["url"], record_date=published)
                findings.append(item)
        return {"status": "DONE", "searches": searches, "findings": findings}

    # ------------------------------------------------------------------ 4. decide
    def s_decide(self) -> dict:
        from .rules import decide
        return decide(self.cfg, {s: self.out(s) for s in GATHER_STAGES})

    # ------------------------------------------------------------------ 5. act, or hand over
    def s_hand_over(self) -> dict:
        d = self.out("decide")
        action = {"REAL_AND_TRADING": "add_to_approved_vendors",
                  "FRONT_SUSPECTED": "reject_vendor"}.get(d["verdict"], "manual_review")
        db = self.store.db
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("INSERT OR IGNORE INTO approvals (job_id, recommended_action, reason, status) "
                       "VALUES (?,?,?, 'pending')", (self.job_id, action, d["summary"]))
            db.execute("INSERT OR IGNORE INTO outbox (idempotency_key, to_team, body, created_at) VALUES (?,?,?,?)",
                       (f"{self.job_id}:notify_approver", "compliance-approvers",
                        f"Vendor check {self.job_id}: {d['verdict']}. Recommended: {action}. {d['summary']}",
                        now_iso()))
            db.execute("UPDATE jobs SET status='pending_approval', verdict=? WHERE id=?", (d["verdict"], self.job_id))
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        return {"recommended_action": action, "approval": "pending"}
