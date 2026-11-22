"""Decide. Plain rules over gathered facts, thresholds in config.toml. No LLM here.

Verdicts
  REAL_AND_TRADING  identity resolved, someone confirmed running it, evidence it operates, no strong red flag
  FRONT_SUSPECTED   at least N independent strong red flags, each with a source
  HOLD_FOR_REVIEW   one strong red flag, an adverse-media link, a duplicate, or an ambiguous identity
  CANNOT_VERIFY     a key question has no source (missing record, timeout, budget); names what is missing

Rule of thumb from the brief: a wrong answer costs more than "cannot verify", and honest firms
and innocent namesakes must pass. So one weak signal never fails a firm, and a source that
did not answer is never read as "does not exist".
"""
from datetime import datetime

UNANSWERED = ("UNAVAILABLE", "ERROR", "SKIPPED_BUDGET")


def decide(cfg: dict, o: dict) -> dict:
    req, ident, enr = o.get("take_request") or {}, o.get("identify") or {}, o.get("enrich") or {}
    dom, lead, media = o.get("domain_age") or {}, o.get("leaders") or {}, o.get("media") or {}
    strong, weak, positive, cannot = [], [], [], []   # strong = signs of a FRONT (fake business)
    adverse, review = [], []                              # real-but-risky: these send it to a person

    # ---- request itself
    if req.get("missing"):
        return _result("CANNOT_VERIFY", [req["missing"]], [], [], [], {}, req)

    # ---- identity
    status = ident.get("status")
    if status in UNANSWERED:
        cannot.append(f"Company identity: the source did not answer ({ident.get('error') or status}). "
                      "Retry later or check by hand.")
    elif status == "NOT_FOUND":
        cannot.append(f"Company identity: no company record found for "
                      f"{req.get('domain') or req.get('name')}.")
    elif status == "AMBIGUOUS":
        names_ = ", ".join(f"{c['name']} ({c['domain']})" for c in ident.get("candidates", [])[:3])
        review.append(f"Ambiguous identity: several records match closely ({names_}). Possible lookalike.")
    sim = ident.get("name_similarity")
    if sim is not None and sim < cfg["identity"]["min_name_similarity"] and status == "RESOLVED":
        strong.append(f"Name mismatch: the request says '{req.get('name')}' but the website belongs to "
                      f"'{ident['top']['name']}' (similarity {sim}). Possible lookalike or impersonation.")
    if ident.get("similar_records"):
        weak.append("Identified by name only; other records with similar names exist: "
                    + ", ".join(ident["similar_records"][:3]) + ". Confirm the vendor's website.")
    if ident.get("duplicate_of"):
        d = ident["duplicate_of"]
        weak.append(f"Duplicate: this company was already {d['decision']} in job {d['job_id']}.")
    elif ident.get("earlier_job"):
        weak.append(f"Duplicate request: the same company was checked in job {ident['earlier_job']['id']} "
                    f"(verdict {ident['earlier_job']['verdict']}).")

    # ---- how long it has existed
    year = enr.get("year_founded")
    age_days = dom.get("age_days")
    if dom.get("status") in UNANSWERED or dom.get("status") == "NOT_FOUND":
        cannot.append(f"Domain age: {dom.get('missing') or dom.get('error')}.")
    if age_days is not None:
        if age_days < cfg["age"]["young_domain_days"]:
            weak.append(f"Young website: domain registered {dom['registered'][:10]} ({age_days} days ago).")
        if year:
            reg_year = int(dom["registered"][:4])
            if reg_year - int(year) > cfg["age"]["age_mismatch_years"] and age_days < 3 * 365:
                strong.append(f"Age mismatch: the record says founded {year}, but its domain was only "
                              f"registered on {dom['registered'][:10]}.")
    if not year and enr.get("status") == "FOUND":
        cannot.append("Founding year: the company record has no founding year.")

    # ---- is it trading
    if enr.get("status") in UNANSWERED:
        cannot.append(f"Company profile: the source did not answer ({enr.get('error')}).")
    elif enr.get("status") == "FOUND":
        rs = (enr.get("record_status") or "").lower()
        if rs == "active":
            positive.append("Company record status: active.")
        elif rs:
            strong.append(f"Company record status is '{rs}', not active.")
        hc = enr.get("headcount")
        if hc is not None and hc >= cfg["operating"]["min_headcount"]:
            positive.append(f"{hc} employees visible on the company record.")
        elif hc is not None:
            weak.append(f"Very few visible employees ({hc}).")
        else:
            cannot.append("Headcount: the company record has no employee count.")
        if enr.get("openings"):
            positive.append(f"{enr['openings']} open job postings.")
        if enr.get("news"):
            positive.append(f"{len(enr['news'])} news articles on the company record, latest "
                            f"{(enr['news'][0].get('date') or '')[:10]}.")

    # ---- who runs it
    leaders = lead.get("leaders") or []
    confirmed = [l for l in leaders if l.get("confirmed")]
    refuted = [l for l in leaders if l.get("confirmed") is False]
    if confirmed:
        positive.append("Run by: " + ", ".join(f"{l['name']} ({l.get('current_title') or l.get('title')})"
                                               for l in confirmed) + ", confirmed on their own profiles.")
    elif enr.get("status") == "FOUND":
        cannot.append("Who runs it: no leader could be confirmed from their own profile.")
    if refuted and not confirmed:
        weak.append("Listed leaders do not show the vendor as a current employer: "
                    + ", ".join(l["name"] for l in refuted) + ".")

    # ---- adverse media (5.2)
    linked = [f for f in media.get("findings", []) if f["link"] == "LINKED"]
    unresolved = [f for f in media.get("findings", []) if f["link"] == "UNRESOLVED"]
    cleared = [f for f in media.get("findings", []) if f["link"] == "NAMESAKE_CLEARED"]
    by_article: dict[str, list] = {}           # one article = one signal, however many subjects it names
    for f in linked:
        by_article.setdefault(f["url"], []).append(f)
    for url, fs in by_article.items():
        who = " and ".join(dict.fromkeys(f["subject"] for f in fs))
        adverse.append(f"Adverse news linked to {who}: \"{fs[0]['title']}\" ({', '.join(fs[0]['terms'])}).")
    for f in unresolved:
        cannot.append(f"Adverse news about {f['subject']} could not be checked: {f['why']}.")
    failed = [s for s in media.get("searches", []) if s["status"] in UNANSWERED]
    for s in failed:
        cannot.append(f"News search for {s['subject']} did not answer ({s['error'] or s['status']}).")

    # ---- verdict
    n_front = cfg["verdict"]["strong_signals_for_front"]
    identity_ok = status == "RESOLVED"
    if len(strong) >= n_front:
        verdict = "FRONT_SUSPECTED"
    elif strong or adverse or review or ident.get("duplicate_of") or ident.get("earlier_job"):
        verdict = "HOLD_FOR_REVIEW"
    elif not identity_ok:
        verdict = "CANNOT_VERIFY"
    elif confirmed and positive and len(weak) <= 1 and not unresolved and not failed:
        verdict = "REAL_AND_TRADING"
    else:
        verdict = "CANNOT_VERIFY" if cannot else "HOLD_FOR_REVIEW"

    answer = {
        "who_runs_it": [{k: l.get(k) for k in ("name", "title", "current_title", "since", "location",
                                               "confirmed", "why", "current_employers")} for l in leaders],
        "how_long_it_has_existed": {"year_founded": year, "domain_registered": dom.get("registered"),
                                    "domain_age_days": age_days,
                                    "earliest_founder_start": enr.get("earliest_founder_start")},
        "is_trading": positive,
        "namesakes_cleared": [f"{f['subject']}: {f['title']} ({f['url']}) — {f['why']}" for f in cleared],
    }
    return _result(verdict, cannot, strong, weak, positive, answer, req, adverse, review)


def _result(verdict, cannot, strong, weak, positive, answer, req, adverse=(), review=()):
    adverse, review = list(adverse), list(review)
    who = req.get("name") or req.get("domain")
    summary = {
        "REAL_AND_TRADING": f"{who} looks like a real, trading business.",
        "FRONT_SUSPECTED": f"{who} shows {len(strong)} independent red flags of a front company.",
        "HOLD_FOR_REVIEW": f"{who} needs a person to review: " + "; ".join((strong + adverse + review + weak)[:2]),
        "CANNOT_VERIFY": f"Cannot verify {who}: " + "; ".join(cannot[:2]),
    }[verdict]
    return {"verdict": verdict, "summary": summary,
            "looks_off": {"strong": strong, "adverse_news": adverse, "needs_review": review, "weak": weak},
            "cannot_verify": cannot, **answer, "decided_at": datetime.now().isoformat(timespec="seconds")}
