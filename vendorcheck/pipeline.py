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
from .evidence import Evidence, get_path, html_to_text, snippet_around
from .sources import FOUND, NOT_FOUND, Result, SourceClient, urllib_transport
from .store import Store, now_iso

STAGES = ["take_request", "plan", "identify", "enrich", "activity", "domain_age", "registry", "leaders", "media",
          "decide", "hand_over", "done"]
GATHER_STAGES = STAGES[: STAGES.index("decide")]
# A re-screen of an approved vendor reuses these from the original check (who the company is, its
# profile and leaders, its domain age) and only re-runs what changes: the GST registry and the news.
CARRIED_STAGES = ("identify", "enrich", "domain_age", "leaders")


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
    """Cite the employer name, title and start date of a current job, not the whole record."""
    key_emp = "employer_name" if "employer_name" in job else "name"
    key_title = "employee_title" if "employee_title" in job else "title"
    key_start = "employee_start_date" if "employee_start_date" in job else "start_date"
    ev.field(stage, f"{person_name}'s own profile: start date in the current job", res, f"{job_path}.{key_start}",
             source_url=profile_url)
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
            try:
                if self.request.get("rescreen_of") and stage in CARRIED_STAGES:
                    out = self._carry_over(stage)
                else:
                    out = getattr(self, "s_" + stage)()
            except (KeyError, IndexError, TypeError, AttributeError, ValueError) as e:
                if stage in ("decide", "hand_over", "take_request", "plan"):
                    raise                                   # our own logic: a real bug must be seen
                # A source answered in a shape we did not expect. Never guess: this check becomes
                # "cannot verify", the run carries on, and the job never gets stuck on it.
                self.store.clear_facts(job_id, stage)
                self.store.log_event(job_id, "stage_error", stage=stage, error=f"{type(e).__name__}: {e}")
                out = {"status": "ERROR", "stage_error": True,
                       "missing": f"{stage}: the source answered in an unexpected format ({type(e).__name__})"}
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
        name_from_text = False
        if r.get("text") and not (r.get("name") or r.get("website")):
            parsed = names.parse_free_text(r["text"])
            r.update({k: v for k, v in parsed.items() if v})
            name_from_text = bool(parsed.get("name"))
        domain = names.domain_of(r.get("website"))
        notes = []
        if not domain:
            notes.append("No website given: identifying by name only, so a lookalike name is more likely.")
        if not r.get("name"):
            notes.append("No vendor name given: cannot compare the claimed name with the record.")
        gstin = (r.get("gstin") or "").strip().upper() or None
        if not gstin and r.get("text"):
            found = names.find_gstins(r["text"])
            gstin = found[0] if found else None
        cin = (r.get("cin") or "").strip().upper() or None
        if not cin and r.get("text"):
            found = names.find_cins(r["text"])
            cin = found[0] if found else None
        out = {"name": r.get("name"), "domain": domain, "gstin": gstin, "notes": notes,
               "name_from_text": name_from_text}
        # Optional extras: only added when given, so a request without them is handled exactly as before.
        if cin:
            out["cin"] = cin
            if not names.decode_cin(cin):
                notes.append(f"'{cin}' is not a well-formed CIN (company registration number).")
        directors = names.parse_people_list(r.get("directors")) or names.directors_in_text(r.get("text"))
        if directors:
            out["directors"] = directors
        if r.get("website") and not domain:
            notes.append(f"'{r['website']}' is not a usable website; treated as no website.")
        if r.get("rescreen_of"):
            out["rescreen_of"] = r["rescreen_of"]
            if not gstin:      # reuse the GSTIN found in the original check: no need to read the website again
                found = [g["gstin"] for g in (self.store.stage_output(r["rescreen_of"], "registry") or {})
                         .get("gstins", []) if g.get("checksum_ok")]
                out["gstin"] = found[0] if found else None
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
            ("latest dated job postings (is it active now)", est.get("/job/search", 0)),
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
        if res is not None and res.status == NOT_FOUND:
            self.ev.absence("identify", f"Company Identify has no company record for the website {req['domain']}", res)
        if (res is None or res.status == NOT_FOUND) and req["name"]:
            res, by = self.cd.identify(name=req["name"]), "name"
        if res.status != FOUND:
            # Even with no company record, the same website asked twice is still a duplicate.
            return self._with_duplicates({"status": res.status, "by": by, "error": res.error,
                                          "missing": f"Company Identify by {by} returned {res.status}"},
                                         request_keys(req))

        matches = res.data[0]["matches"]
        if not matches:
            raise ValueError("Identify FOUND with no matches")
        cands = []
        for i, m in enumerate(matches):
            info = m["company_data"].get("basic_info", {}) or {}
            domains = {names.domain_of(d) for d in (info.get("all_domains") or [])}
            domains |= {names.domain_of(info.get("primary_domain")), names.domain_of(info.get("website"))}
            domains = {d for d in domains if d and not names.is_hosting(d)}     # storefront pages are not websites
            primary = names.domain_of(info.get("primary_domain"))
            cands.append({"i": i, "confidence": m.get("confidence_score"),
                          "id": m["company_data"].get("crustdata_company_id"),
                          "name": info.get("name"),
                          "domain": None if names.is_hosting(primary) else primary,
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
                # A lookalike accusation needs the name to point clearly at ONE known firm: near-exact
                # name matches that all share one website. Seen live: "Sharma Trading Co" loosely
                # matches four different "Sharma ... Trading" firms; that is "cannot verify", not a lookalike.
                strict = [c for c in group if (c["name_similarity"] or 0) >= icfg.get("name_only_min_similarity", 0.85)]
                strict_sites = {c["domain"] for c in strict}
                top = same_site[0] if same_site else max(strict or group, key=rank)
                # Or the requested name carries the whole BRAND of known firms that have their own website
                # (seen live: "Xpressbees Logistics" on a website that does not exist).
                brand_firms = [c for c in cands if c["domain"] and names.carries_brand(req["name"], c["name"])]
                brand_sites = {c["domain"] for c in brand_firms}
                if same_site:
                    status = "RESOLVED"
                elif strict and len(strict_sites) == 1 and top["domain"]:
                    # The NAME clearly belongs to a firm with another website: the lookalike pattern.
                    # Never borrow the real firm's evidence for this vendor.
                    status = "DOMAIN_MISMATCH"
                elif len(brand_sites) == 1:
                    top = max(brand_firms, key=rank)
                    status = "DOMAIN_MISMATCH"
                else:
                    status = "NAME_UNCONFIRMED"  # no single firm clearly carries this name
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
            if not exact and top["all_domains"]:
                # Identify by website returned a firm whose record does not list that website (fuzzy
                # match or redirect). Do not borrow its evidence: a person must look.
                status = "AMBIGUOUS"
            else:
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

    def _carry_over(self, stage: str) -> dict:
        """Re-screen: copy a stage's output and its facts (with their original sources and dates)
        from the original check. No outside call, no cost."""
        original = self.request["rescreen_of"]
        out = dict(self.store.stage_output(original, stage) or {"status": "SKIPPED"})
        out.pop("_stop", None)
        if stage == "identify":
            out.update(duplicate_of=None, earlier_job=None)    # it is the same vendor, by definition
        out["carried_over_from"] = original
        for fact in self.store.facts(original):
            if fact["stage"] == stage:
                self.store.add_fact(job_id=self.job_id, stage=stage, claim=fact["claim"] + " (from the original check)",
                                    value_json=fact["value_json"], source_url=fact["source_url"],
                                    locator=fact["locator"], quote=fact["quote"], retrieved_at=fact["retrieved_at"],
                                    call_id=fact["call_id"], record_date=fact["record_date"])
        return out

    def _with_duplicates(self, out: dict, keys: list[str]) -> dict:
        """Duplicate check on the normalised domain and the Crustdata company ID: against the vendor
        register (already approved or rejected) and against other open jobs."""
        keys = [k for k in keys if k]
        hard = [k for k in keys if not k.startswith("name:")]        # Crustdata ID, website, GSTIN
        soft = [k for k in keys if k.startswith("name:")]            # the normalised name
        req = self.out("take_request") or {}
        dup_reg = self.store.register_lookup(hard)
        dup_job = self.store.earlier_job_for(self.job_id, hard)
        same_name = None
        if not (dup_reg or dup_job) and soft:
            by_name = self.store.register_lookup(soft)
            job_by_name = self.store.earlier_job_for(self.job_id, soft)
            other_site = (by_name or {}).get("domain") if by_name else None
            if by_name and (not req.get("domain") or not other_site):
                dup_reg = by_name             # nothing else tells them apart: treat as the same vendor
            elif job_by_name and not req.get("domain"):
                dup_job = job_by_name
            elif by_name or job_by_name:
                # Same name, different website: maybe a duplicate, maybe a lookalike. Flag it and keep checking.
                same_name = {"job_id": (by_name or {}).get("job_id") or job_by_name["id"],
                             "name": (by_name or {}).get("name"), "domain": other_site, "matched_on": soft[0]}
        out.update(company_keys=keys, duplicate_of=dup_reg, same_name_as=same_name,
                   earlier_job={"id": dup_job["id"], "verdict": dup_job["verdict"]} if dup_job else None)
        if dup_reg or dup_job:
            out["_stop"] = True     # a duplicate is held for a person; no need to pay for the rest
        return out

    # ------------------------------------------------------------------ 3. gather: enrich
    def s_enrich(self) -> dict:
        ident = self.out("identify")
        if ident.get("status") not in ("RESOLVED", "AMBIGUOUS"):
            return {"status": "SKIPPED", "reason": "company not identified"}
        if ident.get("status") == "AMBIGUOUS" and (ident.get("by") == "name" or not ident.get("exact_domain")):
            # Seen live: "RK Enterprises" by name matches 10+ different firms on different websites.
            # We do not know which one is meant, so do not pay to profile a guess; a person picks.
            return {"status": "SKIPPED", "reason": "several different firms carry this name; a person must pick one"}
        res = self.cd.enrich_company(ident["top"]["id"])
        if res.status != FOUND:
            return {"status": res.status, "error": res.error,
                    "missing": f"Company Enrich returned {res.status}"}
        b = "[0].matches[0].company_data"
        meta = (res.data[0]["matches"][0]["company_data"].get("metadata") or {})
        record_date = meta.get("updated_at") or meta.get("indexed_at")     # refresh date, when given
        f = lambda claim, path: self.ev.field("enrich", claim, res, f"{b}.{path}", record_date=record_date)
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
        # Seen live: Blue Dart's only listed "founder" is a self-declared profile with 0 connections, and
        # its CXO list has three "CEO"s with 1 connection. Junk profiles are dropped before anyone is
        # reported as running the company (founders get a lower bar: a real small-firm founder may
        # have a small network).
        min_founder_conn = self.cfg["people"].get("min_founder_connections", 0)
        for group in ("founders", "cxos"):
            members = list(enumerate(((cd.get("people") or {}).get(group)) or []))
            floor = min_founder_conn if group == "founders" else min_conn
            members = [(i, p) for i, p in members
                       if isinstance(p, dict) and ((p.get("professional_network") or {}).get("connections") or 0) >= floor]
            if group == "cxos":
                # Seen live: TCI Express's "CXO" list includes a "Senior Territory Manager". Only a leadership
                # title counts as someone who runs the company.
                members = [(i, p) for i, p in members
                           if names.is_leadership_title((p.get("basic_profile") or {}).get("current_title"))]
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

    # ------------------------------------------------------------------ 3. gather: is it active now
    def s_activity(self) -> dict:
        """Dated proof that the firm is active NOW: its most recent job postings (Crustdata Job Search,
        0.03 credits per listing). A headcount or an "active" flag can be years old; a job posted last week
        cannot."""
        acfg = self.cfg.get("activity", {})
        if not acfg.get("enabled", True):
            return {"status": "SKIPPED", "reason": "activity check turned off in config"}
        ident = self.out("identify") or {}
        if ident.get("status") != "RESOLVED" or not (ident.get("top") or {}).get("id"):
            return {"status": "SKIPPED", "reason": "company not identified"}
        res = self.cd.job_search(ident["top"]["id"], acfg.get("max_listings", 3))
        if res.status == NOT_FOUND:
            return {"status": "NOT_FOUND", "postings": [], "total": 0}
        if res.status != FOUND or not isinstance(res.data, dict):
            return {"status": res.status, "error": res.error,
                    "missing": f"Job Search (dated hiring activity) returned {res.status}"}
        postings = []
        for i, job in enumerate((res.data.get("job_listings") or [])[: acfg.get("max_listings", 3)]):
            if not isinstance(job, dict):
                continue
            # The API nests fields (job_details.title, metadata.date_added); the connector flattens them.
            nested = isinstance(job.get("job_details"), dict)
            title_path = f"job_listings[{i}]." + ("job_details.title" if nested else "title")
            date_path = f"job_listings[{i}]." + ("metadata.date_added" if nested else "date_added")
            url = (job.get("job_details") or {}).get("url") if nested else job.get("url")
            try:
                date = get_path(res.data, date_path)
            except (KeyError, IndexError, TypeError):
                date = None
            day = str(date or "")[:10]
            try:
                datetime.strptime(day, "%Y-%m-%d")         # only an ISO date counts; anything else is skipped
            except ValueError:
                continue
            try:
                title = get_path(res.data, title_path)
            except (KeyError, IndexError, TypeError):
                title = None
            # The date is the fact the decision rests on, so the date field is what is cited.
            cited = self.ev.field("activity", f"Date a job posting{f' ({title!r})' if title else ''} was added "
                                  "for the company", res, date_path, record_date=str(date))
            if not cited:
                continue
            postings.append({"title": title or "untitled posting", "date": day, "url": url})
        postings.sort(key=lambda p: p["date"], reverse=True)
        total = res.data.get("total_count")
        return {"status": "FOUND" if postings else "NOT_FOUND", "postings": postings,
                "total": total if isinstance(total, int) else len(postings)}

    # ------------------------------------------------------------------ 3. gather: domain age
    def s_domain_age(self) -> dict:
        ident = self.out("identify") or {}
        # Only the vendor's own website, or the website of a company we resolved it to; never the website
        # of one candidate among several (seen live: "RK Enterprises" -> a random rkenterprises.lk).
        domain = self.out("take_request")["domain"] or \
            ((ident.get("top") or {}).get("domain") if ident.get("status") == "RESOLVED" else None)
        if not domain:
            return {"status": "SKIPPED", "reason": "no website to date"}
        res = rdap_domain(self.client, domain)
        via, fetched = "", None
        if res.status not in (FOUND, NOT_FOUND) and self.cfg.get("domain", {}).get("fallback_via_web_fetch", True):
            # A source fails: the same public RDAP record, read through Crustdata Web Fetch (1 credit).
            # Seen at the venue: rdap.org unreachable from one network while the fetch route worked.
            fallback = self._rdap_via_web_fetch(domain)
            if fallback:
                res, fetched = fallback
                via = " (read through Crustdata Web Fetch)"

        def cite(claim: str, path: str, record_date=None):
            """A field of the RDAP record. Read directly, cite the JSON field; read through Web Fetch, the
            stored response is the fetched page, so cite the verbatim passage of it that holds the value."""
            try:
                value = get_path(res.data, path)
            except (KeyError, IndexError, TypeError):
                return None
            if value in (None, ""):
                return None
            if not fetched:
                return self.ev.field("domain_age", claim, res, path, record_date=record_date)
            page, content = fetched
            ok = self.ev.passage("domain_age", claim, value, page, content,
                                 snippet_around(content, json.dumps(value)[1:-1] if isinstance(value, str) else str(value), 60),
                                 res.url, record_date=record_date)
            return value if ok else None
        if res.status == NOT_FOUND:
            # "Not registered" only when the registry says so in words, and that sentence is cited.
            desc = (res.data.get("description") or []) if isinstance(res.data, dict) else []
            idx = next((i for i, d in enumerate(desc) if isinstance(d, str) and "available for registration" in d.lower()),
                       None)
            unregistered = idx is not None and bool(cite(f"Domain registry answer for {domain}{via}", f"description[{idx}]"))
            return {"status": NOT_FOUND, "domain": domain, "unregistered": unregistered,
                    "missing": f"Domain registry (RDAP) has no record of {domain}"}
        if res.status != FOUND or not isinstance(res.data, dict):
            return {"status": res.status, "error": res.error, "missing": f"Domain registry (RDAP) for {domain}"}
        for i, e in enumerate(res.data.get("events") or []):
            if e.get("eventAction") == "registration":
                date = cite(f"Domain {domain} registration date{via}", f"events[{i}].eventDate",
                            record_date=e.get("eventDate"))
                try:
                    d = datetime.fromisoformat(str(date).replace("Z", "+00:00"))
                except ValueError:
                    return {"status": "ERROR", "missing": f"RDAP registration date for {domain} is not a date"}
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                out = {"status": "FOUND", "domain": domain, "registered": date,
                       "age_days": (datetime.now(timezone.utc) - d).days}
                # Who the domain is registered to, when the registry shows it (.in records often name the
                # registrant organisation; .com records are usually redacted). Seen live: ecomexpress.in is
                # registered to "Delhivery Limited", which bought Ecom Express.
                entities = res.data.get("entities")
                for j, ent in enumerate(entities if isinstance(entities, list) else []):
                    if not isinstance(ent, dict) or "registrant" not in (ent.get("roles") or []):
                        continue
                    vcard = ent.get("vcardArray")
                    card = vcard[1] if isinstance(vcard, list) and len(vcard) > 1 and isinstance(vcard[1], list) else []
                    for k, item in enumerate(card):
                        if isinstance(item, list) and len(item) >= 4 and item[0] == "org" and str(item[3]).strip():
                            org = cite(f"Organisation the domain {domain} is registered to{via}",
                                       f"entities[{j}].vcardArray[1][{k}][3]")
                            if org:
                                out["registrant_org"] = org
                return out
        return {"status": "NOT_FOUND", "missing": f"RDAP record for {domain} has no registration date"}

    def _rdap_via_web_fetch(self, domain: str):
        """The RDAP record for a domain, read through Crustdata Web Fetch. Returns (a Result whose data is the
        RDAP JSON, (the fetch Result, the fetched text)), or None if that route failed too."""
        url = f"https://rdap.org/domain/{domain}"
        page = self.cd.web_fetch(url)
        entry = (page.data or [{}])[0] if isinstance(page.data, list) and page.data else {}
        if page.status != FOUND or not isinstance(entry, dict) or not entry.get("content"):
            return None
        try:
            record = json.loads(entry["content"])
        except ValueError:
            return None
        if not isinstance(record, dict):
            return None
        status = NOT_FOUND if entry.get("http_status") == 404 or record.get("errorCode") == 404 else FOUND
        return Result(status, record, page.call_id, url, page.retrieved_at), (page, entry["content"])

    # ------------------------------------------------------------------ 3. gather: registry (GST)
    def s_registry(self) -> dict:
        """GSTINs shown on the vendor's own website, checked with the GSTIN checksum and then
        against the GST registry. The website is a claim; the registry is the record."""
        from .crustdata import gst_verify
        ident, req, rcfg = self.out("identify") or {}, self.out("take_request"), self.cfg.get("registry", {})
        domain = req.get("domain") or ((ident.get("top") or {}).get("domain")
                                       if ident.get("status") == "RESOLVED" else None)
        pages, gstins, origin = [], [], {}
        cins, cin_origin = ([req["cin"]], {req["cin"]: "request"}) if req.get("cin") else ([], {})
        website_cins: list[str] = []
        if req.get("gstin"):
            gstins.append(req["gstin"])          # given on the vendor's onboarding form (a claim, like the website)
            origin[req["gstin"]] = "request"
        def with_cins(out: dict) -> dict:
            if cins:
                out["cins"] = [{"cin": c, "from": cin_origin.get(c, "website"), "decoded": names.decode_cin(c),
                                "on_website": c in website_cins} for c in cins]
            return out

        if not domain and not gstins:
            return with_cins({"status": "SKIPPED", "reason": "no website or GSTIN given"})

        # If the run's budget is tight (e.g. a lowered cost limit), keep credits for the news search,
        # which matters more than reading a GSTIN off the homepage.
        reserve = self.cfg["limits"].get("reserve_credits_for_news", 1)
        remaining = self.cfg["limits"]["max_credits_per_run"] - self.store.credits_spent(self.job_id)
        if not gstins and domain and remaining - self.cfg["credit_estimates"]["/web/enrich/live"] < reserve:
            return with_cins({"status": "SKIPPED_BUDGET", "gstins": [],
                              "missing": "the homepage was not read, to keep the run's credit budget for the news check"})
        hosts = [domain]
        if rcfg.get("try_www_first", True) and domain and domain.count(".") == 1:
            hosts = [f"www.{domain}", domain]           # a bare-domain fallback only if www fails
        for path in ([] if gstins or not domain else rcfg.get("website_paths", [""])[: rcfg.get("max_pages", 1)]):
            for host in hosts:
                url = f"https://{host}/{path}".rstrip("/")
                page = self.cd.web_fetch(url)
                entry = ((page.data or [{}])[0] or {}) if isinstance(page.data, list) else {}
                blocked = blocked_reason(entry) if page.status == FOUND else ""
                pages.append({"url": url, "status": "BLOCKED" if blocked else page.status,
                              "error": blocked or page.error or entry.get("message") or ""})
                if page.status == FOUND and not blocked:
                    break
            if pages[-1]["status"] != FOUND:
                continue
            text = html_to_text(entry.get("content") or "")
            for cin in names.find_cins(text)[:2]:
                website_cins.append(cin)
                if cin not in cins:
                    cins.append(cin)
                    cin_origin[cin] = "website"
                self.ev.passage("registry", "Company registration number (CIN) shown on the vendor's own website",
                                cin, page, text, snippet_around(text, cin, 80), url)
            for gstin in names.find_gstins(text):
                if gstin in gstins:
                    continue
                gstins.append(gstin)
                origin[gstin] = "website"
                self.ev.passage("registry", "GSTIN shown on the vendor's own website", gstin, page, text,
                                snippet_around(text, gstin, 80), url)
            if gstins:
                break
        if not gstins and not any(p["status"] == FOUND for p in pages):
            why = "; ".join(f"{p['url']}: {p['error'] or p['status']}" for p in pages)
            return with_cins({"status": "UNAVAILABLE", "pages": pages, "gstins": [],
                              "missing": f"the vendor's website could not be read ({why})"})

        checked = []
        for gstin in gstins[:2]:
            item = {"gstin": gstin, "from": origin.get(gstin, "website"),
                    "checksum_ok": names.gstin_checksum_ok(gstin),
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
                valid = g(f"GST registry: is {gstin} a valid registration", "valid") if "valid" in rec else None
                item.update(
                    registry="FOUND",
                    valid=valid if valid is not None else rec.get("valid"),
                    status=g(f"GST registration status of {gstin}", "gst_in_status"),
                    legal_name=g(f"Legal name on GST registration {gstin}", "legal_name_of_business"),
                    trade_name=g(f"Trade name on GST registration {gstin}", "trade_name_of_business"),
                    registered=g(f"Date of GST registration {gstin}", "date_of_registration"),
                    constitution=g(f"Constitution of business on GST registration {gstin}",
                                   "constitution_of_business"),
                )
            checked.append(item)
        return with_cins({"status": "DONE", "pages": pages, "gstins": checked})

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
        # Directors named on the vendor's onboarding form are screened in the news too. They are a claim:
        # a director also found among the company record's leaders is marked so; the others are not
        # confirmed by any source here (the MCA register of directors is not read).
        on_record = enr.get("people") or []
        for declared in self.out("take_request").get("directors") or []:
            match = next((l for l in checked if names.same_person(l["name"], declared)), None)
            if match:
                match["declared"] = True
                continue
            record = next((p for p in on_record if names.same_person(p.get("name"), declared)), None)
            checked.append({"name": declared, "group": "declared", "declared": True, "confirmed": None,
                            "on_record_as": (record or {}).get("current_title") or (record or {}).get("title"),
                            "why": "named on the vendor's form; not on the company record" if not record else
                                   "named on the vendor's form; also listed on the company record"})
        return {"leaders": checked}

    # ------------------------------------------------------------------ 3. gather: adverse media (5.2)
    def s_media(self) -> dict:
        mcfg = self.cfg["media"]
        term_patterns = [(term.strip(), re.compile(r"\b" + re.escape(term.strip().lower()) + r"\b"))
                         for term in mcfg["adverse_terms"]]
        term_patterns += [(term, re.compile(re.escape(term))) for term in mcfg.get("adverse_terms_hi", [])]
        victim_markers = [marker.lower() for marker in mcfg.get("victim_context", [])]
        enr, ident, req = self.out("enrich") or {}, self.out("identify") or {}, self.out("take_request")
        if ident.get("status") not in ("RESOLVED", "AMBIGUOUS") or \
                (ident.get("status") == "AMBIGUOUS" and (ident.get("by") == "name" or not ident.get("exact_domain"))):
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
            is_victim = any(marker in blob.lower() for marker in victim_markers) or \
                re.search(r"\bfake\s+(?:\w+\s+){0,2}" + re.escape(names.search_name(company).lower()), blob.lower())
            for kind, subject in subjects:
                # The adverse word must sit in the SAME sentence as the name, as a whole word:
                # "by default", or "fraud" two sentences away from the name, is not news about them.
                naming = [s for s in sentences if sentence_names(kind, subject, s)]
                hit_terms = sorted({term for s in naming for term, rx in term_patterns if rx.search(s.lower())})
                ambiguous = {t.lower() for t in mcfg.get("ambiguous_terms", [])}
                if not hit_terms or all(t.lower() in ambiguous for t in hit_terms):
                    continue        # no clear accusation word next to the name
                item = {"subject": subject, "kind": kind, "title": (r.get("title") or "")[:200],
                        "url": r.get("url"), "terms": hit_terms, "published": published}
                if is_victim:
                    item.update(link="NOT_ADVERSE", clearance="weak",
                                why="the vendor appears as the victim or is being impersonated, not accused")
                    findings.append(item)
                    continue
                if kind == "company":
                    # The headline or snippet names the vendor next to an adverse term: that is the link.
                    # Cite the field that actually contains that sentence. (Namesake clearing is for
                    # people; a person reviews company-name collisions.)
                    in_title = any(sentence_names(kind, subject, s) and any(rx.search(s.lower()) for _, rx in term_patterns)
                                   for s in re.split(r"(?<=[.!?])\s+|\n", r.get("title") or ""))
                    field = "title" if in_title else "snippet"
                    self.ev.field("media", f"News {field} naming {subject} with an adverse term", res,
                                  f"results[{ri}].{field}", source_url=r.get("url"), record_date=published)
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
                mentions_vendor = names.mentions_business(company, text) or \
                    (bool(req.get("name")) and names.mentions_business(req["name"], text)) or \
                    bool(domain and domain in text.lower())
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
        if self.request.get("rescreen_of"):
            return self._hand_over_rescreen(d)
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
            # Never reopen a job a person already closed (a kill after this commit, then a restart).
            db.execute("UPDATE jobs SET status='pending_approval', verdict=? WHERE id=? AND status='running'",
                       (d["verdict"], self.job_id))
            result = {"recommended_action": action, "approval": "pending"}
            self._finish_in_transaction(result)
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        return result

    def _finish_in_transaction(self, result: dict) -> None:
        """Save the hand-over output and mark the job done inside the caller's open transaction, so a kill
        can never leave the approval written but the job still at stage hand_over."""
        self.store.db.execute("INSERT OR REPLACE INTO stage_outputs (job_id, stage, output_json) VALUES (?,?,?)",
                              (self.job_id, "hand_over", json.dumps(result)))
        self.store.db.execute("UPDATE jobs SET stage='done', updated_at=? WHERE id=?", (time.time(), self.job_id))

    def _hand_over_rescreen(self, d: dict) -> dict:
        """Monitoring: an approved vendor that still looks real needs nothing. Anything else goes to a
        person (who may reject it, which updates the register) and alerts the team, exactly once."""
        original = self.request["rescreen_of"]
        db = self.store.db
        db.execute("BEGIN IMMEDIATE")
        try:
            if d["verdict"] == "REAL_AND_TRADING":
                db.execute("UPDATE jobs SET status='closed', verdict=? WHERE id=?", (d["verdict"], self.job_id))
                outcome = {"monitoring": "no change", "approval": "not needed"}
            elif d["verdict"] == "CANNOT_VERIFY" and not d.get("no_recent_activity"):
                # A source did not answer this time: not an alert about the vendor. Logged; retry next run.
                db.execute("UPDATE jobs SET status='closed', verdict=? WHERE id=?", (d["verdict"], self.job_id))
                outcome = {"monitoring": "incomplete: a source did not answer; retry", "approval": "not needed"}
            else:
                db.execute("INSERT OR IGNORE INTO approvals (job_id, recommended_action, reason, status) "
                           "VALUES (?, 'manual_review', ?, 'pending')", (self.job_id, d["summary"]))
                db.execute("INSERT OR IGNORE INTO outbox (idempotency_key, to_team, body, created_at) VALUES (?,?,?,?)",
                           (f"{self.job_id}:monitoring_alert", "compliance-approvers",
                            f"Monitoring alert for an approved vendor (first checked in {original}): now "
                            f"{d['verdict']}. {d['summary']}", now_iso()))
                db.execute("UPDATE jobs SET status='pending_approval', verdict=? WHERE id=? AND status='running'",
                           (d["verdict"], self.job_id))
                outcome = {"monitoring": "changed", "approval": "pending", "recommended_action": "manual_review"}
            self._finish_in_transaction(outcome)
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        return outcome
