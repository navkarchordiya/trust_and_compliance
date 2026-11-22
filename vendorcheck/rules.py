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
import re
from datetime import datetime

from . import names

UNANSWERED = ("UNAVAILABLE", "ERROR", "SKIPPED_BUDGET")


def decide(cfg: dict, o: dict) -> dict:
    req, ident, enr = o.get("take_request") or {}, o.get("identify") or {}, o.get("enrich") or {}
    dom, lead, media = o.get("domain_age") or {}, o.get("leaders") or {}, o.get("media") or {}
    reg = o.get("registry") or {}
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
    elif status == "AMBIGUOUS" and ident.get("by") == "domain" and not ident.get("exact_domain") \
            and (ident.get("top") or {}).get("all_domains"):
        top = ident.get("top") or {}
        review.append(f"Identity unclear: the company record found for {req.get('domain')} is '{top.get('name')}', "
                      f"whose record lists other websites ({', '.join(top.get('all_domains', [])[:3])}). Its records "
                      "were NOT used for this vendor; a person must confirm who the vendor is.")
    elif status == "AMBIGUOUS":
        names_ = ", ".join(f"{c['name']} ({c['domain']})" for c in ident.get("candidates", [])[:3])
        review.append(f"Ambiguous identity: several records match closely ({names_}). A person must pick one.")
    elif status == "DOMAIN_MISMATCH":
        top = ident.get("top") or {}
        strong.append(f"Lookalike: no company record has the website {req.get('domain')}, but the name "
                      f"'{req.get('name')}' carries the name of {top.get('name')}, whose own website is "
                      f"{top.get('domain')}. The real firm's records were NOT used for this vendor.")
        cannot.append(f"Company identity: no company record for the website {req.get('domain')}.")
    elif status == "NAME_UNCONFIRMED":
        top = ident.get("top") or {}
        if req.get("domain"):
            cannot.append(f"Company identity: no company record has the website {req['domain']}, and no single "
                          f"firm clearly carries the name '{req.get('name')}' (closest: '{top.get('name')}', "
                          f"{top.get('domain') or 'no website'}).")
        else:
            cannot.append(f"Company identity: no website given, and no single firm clearly carries the name "
                          f"'{req.get('name')}' (closest: '{top.get('name')}', {top.get('domain') or 'no website'}). "
                          "Send the vendor's website.")
    sim = ident.get("name_similarity")
    top_rec = ident.get("top") or {}
    if sim is not None and status == "RESOLVED" and \
            not names.names_match(req.get("name"), top_rec.get("name"), cfg["identity"]["min_name_similarity"]):
        mismatch = (f"Name mismatch: the request says '{req.get('name')}' but the website belongs to "
                    f"'{top_rec.get('name')}' (similarity {sim}).")
        if req.get("domain") and top_rec.get("domain") and req["domain"] != top_rec.get("domain"):
            # The website is a SECONDARY domain of that record (seen live: gati.com is listed on Allcargo
            # Logistics after the merger): most likely a rebrand or merger, not an impersonation.
            weak.append(mismatch + f" {req['domain']} is listed on that record as a secondary website of "
                        f"{top_rec.get('domain')}: probably a rebrand or merger; confirm the legal entity to pay.")
        elif req.get("name_from_text"):
            # The name was read out of free text by us; a parsing slip must not become an accusation.
            weak.append(mismatch + " The name was read from the request's text: confirm it.")
        else:
            strong.append(mismatch + " Possible lookalike or impersonation.")
    if ident.get("similar_records"):
        weak.append("Identified by name only; other records with similar names exist: "
                    + ", ".join(ident["similar_records"][:3]) + ". Confirm the vendor's website.")
    if ident.get("duplicate_of"):
        d = ident["duplicate_of"]
        if d.get("job_id") == "client-register":
            weak.append(f"Duplicate: already on the client's vendor list as '{d.get('name')}' "
                        f"({d.get('domain') or 'no website'}), matched on {d.get('matched_on')}.")
        else:
            weak.append(f"Duplicate: this company was already {d['decision']} in job {d['job_id']} "
                        f"(matched on {d.get('matched_on')}).")
        review.append("Possible duplicate of an existing vendor; the rest of the check was not repeated.")
    if ident.get("same_name_as"):
        n = ident["same_name_as"]
        weak.append(f"Same name as a vendor in job {n['job_id']} ({n.get('domain') or 'other website'}), but a "
                    "different website: check it is not a duplicate or a lookalike.")
    if not ident.get("duplicate_of") and ident.get("earlier_job"):
        weak.append(f"Duplicate request: the same company is already being checked in job "
                    f"{ident['earlier_job']['id']} (verdict {ident['earlier_job']['verdict'] or 'pending'}).")
        review.append("Possible duplicate request; the rest of the check was not repeated.")

    # ---- how long it has existed
    year = enr.get("year_founded")
    age_days = dom.get("age_days")
    if dom.get("unregistered"):
        weak.append(f"The website given ({dom.get('domain')}) is not a registered domain: the domain registry "
                    "says it is available for registration.")
    elif dom.get("status") in UNANSWERED or dom.get("status") == "NOT_FOUND":
        cannot.append(f"Domain age: {dom.get('missing') or dom.get('error')}.")
    if age_days is not None:
        if age_days < cfg["age"]["young_domain_days"]:
            weak.append(f"Young website: domain registered {dom['registered'][:10]} ({age_days} days ago).")
        if year:
            reg_year = int(dom["registered"][:4])
            if reg_year - int(year) > cfg["age"]["age_mismatch_years"] and age_days < 3 * 365:
                strong.append(f"Age mismatch: the record says founded {year}, but its domain was only "
                              f"registered on {dom['registered'][:10]}.")
    org = dom.get("registrant_org")
    vendor_names = [n for n in (req.get("name"), enr.get("name"), (ident.get("top") or {}).get("name")) if n]
    if org and vendor_names:
        if any(names.names_match(n, org, cfg["identity"]["min_name_similarity"]) or names.carries_brand(org, n)
               for n in vendor_names):
            positive.append(f"Website {dom['domain']} is registered to {org} (domain registry).")
        else:
            weak.append(f"Website {dom['domain']} is registered to '{org}', not to {vendor_names[0]}: a parent "
                        "company, an acquirer, or someone else. Confirm who you will pay.")
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
            positive.append(f"{len(enr['news'])} news article{'s' if len(enr['news']) != 1 else ''} on the company record, latest "
                            f"{(enr['news'][0].get('date') or '')[:10]}.")

    # ---- is it active NOW: the newest dated record (a job posting, a news article on the record)
    acfg = cfg.get("activity", {})
    act = o.get("activity") or {}
    as_of = datetime.fromisoformat(acfg["as_of"]) if acfg.get("as_of") else datetime.now()
    dated = [(p["date"], f"job posting '{p['title']}'") for p in act.get("postings") or [] if p.get("date")]
    # A news item on the company's record counts only if its title names the company. Seen live: the newest
    # item on Ecom Express's record is about Delhivery, which bought it.
    company_names = [n for n in (enr.get("name"), req.get("name")) if n]
    dated += [((n.get("date") or "")[:10], f"news article '{n.get('title')}'")
              for n in (enr.get("news") or [])
              if n.get("date") and any(names.mentions_business(c, n.get("title") or "") for c in company_names)][:1]
    dated = [(d, what) for d, what in dated if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d)]
    latest_activity = None
    if act.get("status") in UNANSWERED:
        cannot.append(f"Hiring activity: Job Search did not answer ({act.get('error') or act['status']}).")
    if act.get("postings"):
        newest = act["postings"][0]
        if (as_of - datetime.fromisoformat(newest["date"])).days <= acfg.get("recent_days", 180):
            positive.append(f"Hiring: {act.get('total')} job listing{'s' if act.get('total') != 1 else ''} on "
                            f"record; latest '{newest['title']}', posted {newest['date']}.")
    if dated:
        date, what = max(dated)
        days = (as_of - datetime.fromisoformat(date)).days
        latest_activity = {"date": date, "what": what, "days_before_check": days}
        if days <= acfg.get("recent_days", 180):
            positive.append(f"Active now: newest dated record is a {what} from {date} ({days} days before this check).")
        else:
            cannot.append(f"Recent activity: the newest dated record is a {what} from {date}, more than "
                          f"{acfg.get('recent_days', 180)} days ago. Ask the vendor for recent invoices.")
    elif enr.get("status") == "FOUND":
        cannot.append("Recent activity: no dated job posting or news article on record.")
    recently_active = latest_activity is not None and latest_activity["days_before_check"] <= acfg.get("recent_days", 180)

    # ---- registry (GST)
    reg_summary = []
    if reg.get("status") in UNANSWERED:
        cannot.append(f"GST registration: {reg.get('missing') or 'the website could not be read'}.")
        pages = reg.get("pages") or []
        if pages and all("cert_date_invalid" in (p.get("error") or "").lower() for p in pages):
            weak.append(f"The website {req.get('domain')} has an expired security certificate: it may no longer "
                        "be maintained (seen on brands absorbed into other companies).")
        if pages and all("host not found" in (p.get("error") or "").lower() for p in pages) \
                and not dom.get("unregistered"):          # the same fact, already reported from the registry
            weak.append(f"The website given ({req.get('domain')}) does not exist: no DNS record "
                        f"(checked {', '.join(p['url'] for p in pages)}).")
    elif reg.get("status") == "DONE" and not reg.get("gstins"):
        cannot.append("GST registration: no GSTIN found on the vendor's website; ask the vendor for it.")
    claimed_name = enr.get("name") or req.get("name")
    rcfg = cfg.get("registry", {})
    for g in reg.get("gstins") or []:
        gid = g["gstin"]
        where = "given in the request" if g.get("from") == "request" else "shown on the vendor's website"
        if not g.get("checksum_ok"):
            if g.get("from") == "request":      # most likely a typo on the form: ask, do not accuse
                review.append(f"GSTIN {gid} {where} fails the GSTIN check-digit test; ask the vendor to confirm it.")
            else:
                strong.append(f"Invalid GSTIN {where}: {gid} fails the GSTIN check-digit test.")
            continue
        if claimed_name and names.claims_company(claimed_name) and g.get("pan_holder_type") not in (None, "company"):
            weak.append(f"GSTIN {gid} belongs to a PAN of type '{g['pan_holder_type']}', but the name "
                        f"'{claimed_name}' says it is a company.")
        if g.get("registry") == "FOUND":
            st = (g.get("status") or "").lower()
            if g.get("valid") is False:
                strong.append(f"GSTIN {gid} {where} is not in the GST registry.")
            elif st and st != "active":
                strong.append(f"GST registration {gid} is '{g['status']}', not active.")
            elif st == "active":
                positive.append(f"GST registration {gid} is active (registered {g.get('registered') or '?'}).")
            best = max(names.similarity(claimed_name, g.get("legal_name")),
                       names.similarity(claimed_name, g.get("trade_name")))
            if claimed_name and (g.get("legal_name") or g.get("trade_name")) and \
                    best < rcfg.get("min_legal_name_similarity", 0.6):
                weak.append(f"GST registration {gid} is held by '{g.get('legal_name')}' (trade name "
                            f"'{g.get('trade_name')}'), which does not match '{claimed_name}'.")
            reg_year = str(g.get("registered") or "")[-4:] if "/" in str(g.get("registered") or "") \
                else str(g.get("registered") or "")[:4]
            if year and reg_year.isdigit() and int(reg_year) >= 2018 and \
                    int(reg_year) - int(year) > cfg["age"]["age_mismatch_years"]:
                weak.append(f"GST registration {gid} dates from {g.get('registered')}, years after the "
                            f"claimed founding year {year}.")
        elif g.get("registry") in ("NOT_CONFIGURED",) + UNANSWERED:
            cannot.append(f"GST registration {gid}: registry not checked ({g.get('why') or g['registry']}).")
        reg_summary.append({k: g.get(k) for k in ("gstin", "from", "checksum_ok", "pan_holder_type", "registry",
                                                    "valid", "status", "legal_name", "trade_name", "registered")})

    # ---- company registration number (CIN): what the identifier itself states
    cin_summary = []
    cins = reg.get("cins") or []
    for c in cins:
        d = c.get("decoded")
        where = "the vendor's form" if c.get("from") == "request" else "the vendor's own website"
        if not d:
            review.append(f"The CIN '{c['cin']}' on {where} is not a well-formed company registration number.")
            continue
        cin_summary.append({"cin": d["cin"], "from": c.get("from"), "year_incorporated": d["year_incorporated"],
                            "state": d["state"], "company_type": d["company_type"], "listed": d["listed"]})
        if as_of.year - d["year_incorporated"] <= cfg["age"].get("young_company_years", 1):
            weak.append(f"Recently incorporated: CIN {d['cin']} ({where}) shows incorporation in "
                        f"{d['year_incorporated']}.")
    if len({c["cin"] for c in cins}) > 1 and {c.get("from") for c in cins} == {"request", "website"}:
        review.append("The CIN on the vendor's form differs from the CIN on its website: "
                      + ", ".join(f"{c['cin']} ({c.get('from')})" for c in cins) + ". Confirm which entity you will pay.")
    company_pans = [g for g in reg.get("gstins") or [] if g.get("pan_holder_type")]
    if cin_summary and company_pans and all(g["pan_holder_type"] != "company" for g in company_pans):
        weak.append(f"The website shows a company registration (CIN {cin_summary[0]['cin']}), but its GSTIN "
                    f"{company_pans[0]['gstin']} belongs to a {company_pans[0]['pan_holder_type']}: the invoicing "
                    "entity may not be the company.")

    # ---- who runs it
    operating = list(positive)        # evidence the firm trades; a confirmed leader alone is not that
    if acfg.get("require_recent_activity") and not recently_active:
        operating = []                # "real and ACTIVE": without a recent dated record, it is not proven
    for c in cin_summary:
        positive.append(f"Registered company: CIN {c['cin']} ({'website' if c['from'] == 'website' else 'form'}), "
                        f"a {'listed' if c['listed'] else 'unlisted'} {c['company_type']} incorporated in "
                        f"{c['year_incorporated']}, registered in {c['state']}.")
    leaders = lead.get("leaders") or []
    confirmed = [l for l in leaders if l.get("confirmed")]
    refuted = [l for l in leaders if l.get("confirmed") is False]
    def shown_title(leader):
        return leader.get("current_title") or leader.get("title")

    # A "CXO" whose own job entry at the vendor is not a leadership role (seen live: a headline of
    # "Chief Administrative Officer" over a TCI Express job of "Senior Territory Manager") works there,
    # but is not reported as running it.
    runs_it = [l for l in confirmed if l.get("group") == "founders" or names.is_leadership_title(shown_title(l))]
    staff = [l for l in confirmed if l not in runs_it]
    if runs_it:
        positive.append("Run by: " + ", ".join(f"{l['name']} ({shown_title(l)})" for l in runs_it)
                        + ", confirmed on their own profiles.")
    if staff:
        positive.append("Also works there: " + ", ".join(f"{l['name']} ({shown_title(l)})" for l in staff)
                        + ", confirmed on their own profiles.")
    if not confirmed and enr.get("status") == "FOUND":
        cannot.append("Who runs it: no leader could be confirmed from their own profile.")
    if refuted and not confirmed:
        weak.append("Listed leaders do not show the vendor as a current employer: "
                    + ", ".join(l["name"] for l in refuted) + ".")
    declared = [l for l in leaders if l.get("declared")]
    if declared:
        def where(l):
            if l.get("confirmed"):
                return "confirmed on their own profile"
            if l.get("on_record_as"):
                return f"on the company record as {l['on_record_as']}"
            return "not on the company record"
        positive.append("Directors named by the vendor: " + ", ".join(f"{l['name']} ({where(l)})" for l in declared)
                        + "; each was screened in the news.")
        unconfirmed = [l["name"] for l in declared if not l.get("confirmed") and not l.get("on_record_as")]
        if unconfirmed:
            cannot.append("Director role: " + ", ".join(unconfirmed) + " named on the vendor's form only; no source "
                          "here confirms the role (check the MCA register of directors).")

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

    # ---- a source that answered in an unexpected format
    stage_errors = [name for name, out in o.items() if isinstance(out, dict) and out.get("stage_error")]
    for name in stage_errors:
        cannot.append(f"{o[name].get('missing')}; this check could not be completed.")

    # ---- verdict
    n_front = cfg["verdict"]["strong_signals_for_front"]
    identity_ok = status == "RESOLVED"
    if len(strong) >= n_front:
        verdict = "FRONT_SUSPECTED"
    elif strong or adverse or review or ident.get("duplicate_of") or ident.get("earlier_job"):
        verdict = "HOLD_FOR_REVIEW"
    elif not identity_ok:
        verdict = "CANNOT_VERIFY"
    elif confirmed and operating and len(weak) <= 1 and not unresolved and not failed and not stage_errors:
        verdict = "REAL_AND_TRADING"
        if ident.get("by") == "name":
            # Never approve on a name alone: a close name can belong to a different firm.
            verdict = "HOLD_FOR_REVIEW"
            review.append("Identified by name only (no website matched): the evidence looks good, but a person "
                          "must confirm the vendor's website before approving.")
    else:
        verdict = "CANNOT_VERIFY" if cannot else "HOLD_FOR_REVIEW"

    answer = {
        "who_runs_it": [{k: l.get(k) for k in ("name", "title", "current_title", "since", "location",
                                               "confirmed", "declared", "on_record_as", "why", "current_employers")
                         if l.get(k) is not None or k in ("name", "confirmed")} for l in leaders],
        "how_long_it_has_existed": {"year_founded": year, "domain_registered": dom.get("registered"),
                                    "domain_age_days": age_days,
                                    "earliest_founder_start": enr.get("earliest_founder_start"),
                                    **({"year_incorporated": cin_summary[0]["year_incorporated"],
                                        "cin": cin_summary[0]["cin"]} if cin_summary else {})},
        "latest_activity": latest_activity,
        "is_trading": positive,
        "namesakes_cleared": [f"{f['subject']}: {f['title']} ({f['url']}) — {f['why']}"
                              for f in cleared + [f for f in media.get("findings", []) if f["link"] == "NOT_ADVERSE"]],
        "registry": reg_summary,
    }
    return _result(verdict, cannot, strong, weak, positive, answer, req, adverse, review)


def _result(verdict, cannot, strong, weak, positive, answer, req, adverse=(), review=()):
    adverse, review = list(adverse), list(review)
    who = req.get("name") or req.get("domain")
    summary = {
        "REAL_AND_TRADING": f"{who} looks like a real, trading business.",
        "FRONT_SUSPECTED": f"{who} shows {len(strong)} independent red flags of a front company.",
        "HOLD_FOR_REVIEW": f"{who} needs a person to review: "
                           + "; ".join(x.rstrip(".") for x in (strong + adverse + review + weak)[:2]) + ".",
        "CANNOT_VERIFY": f"Cannot verify {who}: " + "; ".join(x.rstrip(".") for x in cannot[:2]) + ".",
    }[verdict]
    return {"verdict": verdict, "summary": summary,
            "looks_off": {"strong": strong, "adverse_news": adverse, "needs_review": review, "weak": weak},
            "cannot_verify": cannot, **answer, "decided_at": datetime.now().isoformat(timespec="seconds")}
