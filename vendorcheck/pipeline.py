"""The service, as six stages from the organizers' guide:

  Take the request -> Plan -> Gather the facts -> Decide -> Act, or hand over -> Show its work

Gathering is split into resumable sub-stages. After each stage its output is saved and the
job advances in one transaction, so a killed run restarts at the stage it was in.
"""
import json
import time
import uuid
from datetime import datetime, timezone

from . import names
from .crustdata import Crustdata, rdap_domain
from .evidence import Evidence, html_to_text, snippet_around
from .sources import FOUND, NOT_FOUND, SourceClient, urllib_transport
from .store import Store, now_iso

STAGES = ["take_request", "plan", "identify", "enrich", "domain_age", "leaders", "media",
          "decide", "hand_over", "done"]


def new_job(store: Store, request: dict) -> str:
    job_id = "job_" + datetime.now().strftime("%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]
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


class Pipeline:
    def __init__(self, store: Store, cfg: dict, transport=urllib_transport, sleep=time.sleep, log=print):
        self.store, self.cfg, self.transport, self.sleep, self.log = store, cfg, transport, sleep, log

    # ------------------------------------------------------------------ driver
    def run(self, job_id: str) -> dict:
        job = self.store.job(job_id)
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
            self.store.clear_facts(job_id, stage)       # a re-run stage rebuilds its own facts
            out = getattr(self, "s_" + stage)()
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
        out = {"name": r.get("name"), "domain": domain, "notes": notes}
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
            return {"status": res.status, "by": by, "error": res.error,
                    "missing": f"Company Identify by {by} returned {res.status}"}

        matches = res.data[0]["matches"]
        cands = [{"i": i, "confidence": m.get("confidence_score"),
                  "id": m["company_data"].get("crustdata_company_id"),
                  "name": m["company_data"].get("basic_info", {}).get("name"),
                  "domain": m["company_data"].get("basic_info", {}).get("primary_domain")}
                 for i, m in enumerate(matches)]
        top = cands[0]
        icfg = self.cfg["identity"]
        exact_domain = bool(req["domain"]) and (top["domain"] or "").lower() == req["domain"]
        close = [c for c in cands[1:] if top["confidence"] - (c["confidence"] or 0) < icfg["ambiguity_gap"]]
        resolved = (top["confidence"] or 0) >= icfg["min_confidence"] and (exact_domain or not close)

        base = f"[0].matches[{top['i']}].company_data"
        self.ev.field("identify", "Company record matched to the request", res, f"{base}.basic_info.name")
        self.ev.field("identify", "Primary domain on the company record", res, f"{base}.basic_info.primary_domain")
        self.ev.field("identify", "Match confidence", res, f"[0].matches[{top['i']}].confidence_score")

        sim = names.similarity(req["name"], top["name"]) if req["name"] else None
        keys = [f"cd:{top['id']}", req["domain"] or top["domain"]]
        dup_reg = self.store.register_lookup(keys)
        dup_job = self.store.earlier_job_for(self.job_id, keys)
        return {
            "status": "RESOLVED" if resolved else "AMBIGUOUS", "by": by, "top": top,
            "candidates": cands[:5], "exact_domain": exact_domain, "name_similarity": sim,
            "company_keys": keys,
            "duplicate_of": (dict(dup_reg) if dup_reg else None),
            "earlier_job": ({"id": dup_job["id"], "verdict": dup_job["verdict"]} if dup_job else None),
        }

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
                    self.ev.field("enrich", f"{pname}'s own profile lists the vendor as a current employer",
                                  res, f"{ppath}.{hit[0]}", source_url=person["profile_url"] or res.url)
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
            f("Most recent news article on the company record", "news[0].article_title")
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
                date = self.ev.field("domain_age", f"Domain {domain} registration date", res, f"events[{i}].eventDate")
                d = datetime.fromisoformat(date.replace("Z", "+00:00"))
                return {"status": "FOUND", "domain": domain, "registered": date,
                        "age_days": (datetime.now(timezone.utc) - d).days}
        return {"status": "NOT_FOUND", "missing": f"RDAP record for {domain} has no registration date"}

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
                self.ev.field("leaders", f"{p['name']}'s own profile lists the vendor as a current employer",
                              res, f"{base}.{hit[0]}", source_url=p["profile_url"])
                checked.append({**p, "confirmed": True,
                                "current_title": hit[1].get("employee_title") or hit[1].get("title")})
            else:
                checked.append({**p, "confirmed": False,
                                "current_employers": [j[1].get("employer_name") or j[1].get("name") for j in jobs]})
        return {"leaders": checked}

    # ------------------------------------------------------------------ 3. gather: adverse media (5.2)
    def s_media(self) -> dict:
        mcfg = self.cfg["media"]
        terms = [t.lower() for t in mcfg["adverse_terms"]]
        enr, ident, req = self.out("enrich") or {}, self.out("identify") or {}, self.out("take_request")
        company = enr.get("name") or (ident.get("top") or {}).get("name") or req.get("name")
        domain = req.get("domain") or (ident.get("top") or {}).get("domain")
        if not company:
            return {"status": "SKIPPED", "reason": "no company name to search"}
        leaders = [l["name"] for l in (self.out("leaders") or {}).get("leaders", [])]
        subjects = [("company", company)] + [("person", n) for n in leaders]
        core = names.core_name(company)

        findings, fetched, searches = [], 0, []
        for kind, subject in subjects:
            q = f"\"{subject}\" " + " OR ".join(["fraud", "scam", "arrested", "raid", "\"shell company\""])
            res = self.cd.web_search(q, sources=("news",), location=mcfg.get("country"))
            searches.append({"subject": subject, "kind": kind, "status": res.status, "error": res.error})
            if res.status != FOUND:
                continue
            for r in (res.data.get("results") or [])[: mcfg["max_results_per_query"]]:
                blob = f"{r.get('title', '')} {r.get('snippet', '')}".lower()
                hit_terms = [t for t in terms if t in blob]
                if not hit_terms or subject.lower().split()[0] not in blob:
                    continue
                item = {"subject": subject, "kind": kind, "title": r.get("title"), "url": r.get("url"),
                        "terms": hit_terms}
                if fetched >= mcfg["max_articles_fetched"]:
                    item.update(link="UNRESOLVED", why="article not read: fetch limit reached")
                    findings.append(item)
                    continue
                fetched += 1
                page = self.cd.web_fetch(r["url"])
                if page.status != FOUND:
                    item.update(link="UNRESOLVED", why=f"article could not be fetched ({page.status})")
                    findings.append(item)
                    continue
                text = html_to_text((page.data[0] or {}).get("content") or "")
                mentions_vendor = (core and core in names.core_name(text)) or (domain and domain in text.lower())
                if kind == "company":
                    linked = mentions_vendor and names.core_name(subject) == core
                else:
                    linked = mentions_vendor
                passage = snippet_around(text, subject) or snippet_around(text, hit_terms[0])
                if linked:
                    item.update(link="LINKED", why=f"article names {subject} and the vendor ({company})")
                    self.ev.passage("media", f"Adverse news linked to {subject}", item["title"], page, text,
                                    passage, r["url"])
                else:
                    item.update(link="NAMESAKE_CLEARED",
                                why=f"article names a '{subject}' but never mentions {company} or {domain}; "
                                    "treated as a different person or firm")
                    self.ev.passage("media", f"News about a namesake of {subject}, not linked to the vendor",
                                    item["title"], page, text, passage, r["url"])
                findings.append(item)
        return {"status": "DONE", "searches": searches, "findings": findings}

    # ------------------------------------------------------------------ 4. decide
    def s_decide(self) -> dict:
        from .rules import decide
        return decide(self.cfg, {s: self.out(s) for s in STAGES[:7]})

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
