"""The evidence file as a web page: verdict, reasons, who runs it, and every fact with its source.

Used by the review app (python -m vendorcheck serve) and by `python -m vendorcheck report JOB --html`.
Everything that came from outside (names, titles, quotes) is HTML-escaped before it is shown.
"""
import json
from html import escape

VERDICT_STYLE = {
    "REAL_AND_TRADING": ("#0f7b3f", "Real and trading"),
    "HOLD_FOR_REVIEW": ("#b26b00", "Hold for review"),
    "FRONT_SUSPECTED": ("#b42318", "Front company suspected"),
    "CANNOT_VERIFY": ("#475467", "Cannot verify"),
}

ACTION_LABEL = {
    "add_to_approved_vendors": "Add to approved vendors",
    "reject_vendor": "Reject vendor",
    "manual_review": "Manual review (choose an action)",
}

CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0;
       background: #f6f7f9; color: #1d2939; }
header { background: #101828; color: white; padding: 14px 28px; display: flex; align-items: center;
         justify-content: space-between; }
header a { color: white; text-decoration: none; font-weight: 600; }
main { max-width: 1100px; margin: 24px auto; padding: 0 20px; }
.card { background: white; border: 1px solid #e4e7ec; border-radius: 10px; padding: 18px 22px;
        margin-bottom: 16px; }
.verdict { display: inline-block; color: white; border-radius: 6px; padding: 4px 12px; font-weight: 700; }
h1 { font-size: 22px; margin: 0 0 6px; } h2 { font-size: 16px; margin: 0 0 10px; }
.muted { color: #667085; font-size: 13px; }
ul { margin: 6px 0; padding-left: 20px; } li { margin: 3px 0; }
.front li::marker { color: #b42318; } .adverse li::marker { color: #b26b00; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; border-bottom: 1px solid #eaecf0; padding: 7px 8px; vertical-align: top; }
th { background: #f9fafb; font-weight: 600; }
code { background: #f2f4f7; padding: 1px 4px; border-radius: 4px; font-size: 12px; word-break: break-all; }
form.inline { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
input, select, textarea { font: inherit; padding: 7px 9px; border: 1px solid #d0d5dd; border-radius: 6px; }
button { font: inherit; padding: 8px 14px; border: 0; border-radius: 6px; background: #1d4ed8; color: white;
         cursor: pointer; font-weight: 600; }
button.secondary { background: #475467; }
.grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.notice { padding: 10px 14px; border-radius: 8px; background: #eef4ff; border: 1px solid #c7d7fe; }
pre { background: #0b1220; color: #d1e0ff; padding: 14px; border-radius: 8px; overflow: auto; font-size: 12px; }
"""


def page(title: str, body: str, refresh_seconds: int | None = None) -> str:
    refresh = f'<meta http-equiv="refresh" content="{refresh_seconds}">' if refresh_seconds else ""
    return (f"<!DOCTYPE html><html><head><meta charset='utf-8'>{refresh}<title>{escape(title)}</title>"
            f"<style>{CSS}</style></head><body><header><a href='/'>Vendor Check</a>"
            f"<span class='muted' style='color:#98a2b3'>Trust and compliance · every fact sourced · "
            f"a person approves</span></header><main>{body}</main></body></html>")


def verdict_badge(verdict: str | None) -> str:
    colour, label = VERDICT_STYLE.get(verdict or "", ("#98a2b3", verdict or "running"))
    return f"<span class='verdict' style='background:{colour}'>{escape(label)}</span>"


def bullet_list(items, css_class: str = "") -> str:
    if not items:
        return "<p class='muted'>None.</p>"
    return f"<ul class='{css_class}'>" + "".join(f"<li>{escape(str(i))}</li>" for i in items) + "</ul>"


def source_cell(fact: dict, live_links: bool) -> str:
    """Where the fact came from: a clickable page for web facts, the stored API response for API facts."""
    source = fact.get("source") or ""
    if source.startswith("http") and "api.crustdata.com" not in source and "rdap.org" not in source:
        shown = f"<a href='{escape(source)}' target='_blank' rel='noopener'>{escape(source[:70])}</a>"
    else:
        shown = f"<code>{escape(source)}</code>"
    raw = (f"<a href='/call/{fact['call_id']}?path={escape(fact.get('field_or_passage') or '')}'>"
           f"stored response #{fact['call_id']}</a>") if live_links and fact.get("call_id") else \
        escape(fact.get("raw_response") or "")
    return f"{shown}<br><span class='muted'>{raw}</span>"


def render_job(result: dict, live_links: bool = False, message: str = "") -> str:
    r = result
    request = r.get("request") or {}
    vendor = request.get("name") or request.get("website") or request.get("text") or r["job_id"]
    parts = []
    if message:
        parts.append(f"<div class='notice'>{escape(message)}</div><br>")

    # ---- header: verdict and summary
    parts.append(f"<div class='card'><h1>{escape(vendor)}</h1>"
                 f"<p class='muted'>{escape(json.dumps(request, ensure_ascii=False))} · job "
                 f"<code>{escape(r['job_id'])}</code></p>"
                 f"<p>{verdict_badge(r.get('verdict'))}</p><p>{escape(r.get('summary') or '')}</p></div>")

    # ---- approval
    approval = r.get("approval") or {}
    recommended = r.get("recommended_action")
    if approval.get("status") == "pending" and live_links:
        options = "".join(
            f"<option value='{a}' {'selected' if a == recommended else ''}>{escape(ACTION_LABEL[a])}</option>"
            for a in ("add_to_approved_vendors", "reject_vendor"))
        parts.append(
            f"<div class='card'><h2>Decision (waits for a person)</h2>"
            f"<p>Recommended: <b>{escape(ACTION_LABEL.get(recommended, recommended or '-'))}</b>. "
            f"Nothing irreversible happens until you approve. Choosing a different action needs a reason.</p>"
            f"<form class='inline' method='post' action='/job/{escape(r['job_id'])}/decide'>"
            f"<input name='by' placeholder='Your name' required>"
            f"<select name='action'>{options}</select>"
            f"<input name='reason' placeholder='Reason (needed to overrule)' size='34'>"
            f"<button name='decision' value='approve'>Approve</button>"
            f"<button class='secondary' name='decision' value='decline'>Decline</button></form></div>")
    elif approval:
        decided = (f"{escape(approval.get('status') or '')} by {escape(approval.get('decided_by') or '-')}"
                   f" ({escape(approval.get('decided_action') or '-')}) at {escape(approval.get('decided_at') or '-')}")
        reason = f"<br>Reason given: {escape(approval['override_reason'])}" if approval.get("override_reason") else ""
        parts.append(f"<div class='card'><h2>Decision</h2><p>Recommended: "
                     f"{escape(ACTION_LABEL.get(recommended, recommended or '-'))}. Status: {decided}{reason}</p></div>")

    # ---- why: the reasons behind the verdict
    looks_off = r.get("looks_off") or {}
    parts.append("<div class='card'><h2>Why</h2><div class='grid'><div>"
                 "<b>Signs of a front company</b>" + bullet_list(looks_off.get("strong"), "front") +
                 "<b>Adverse news (real but risky)</b>" + bullet_list(looks_off.get("adverse_news"), "adverse") +
                 "<b>Needs a person</b>" + bullet_list(looks_off.get("needs_review")) +
                 "<b>Weak signals</b>" + bullet_list(looks_off.get("weak")) +
                 "</div><div><b>Evidence it is real and trading</b>" + bullet_list(r.get("is_trading")) +
                 "<b>Cannot verify (what is missing)</b>" + bullet_list(r.get("cannot_verify")) +
                 "<b>Namesakes cleared</b>" + bullet_list(r.get("namesakes_cleared")) +
                 "</div></div></div>")

    # ---- who runs it and how long it has existed
    rows = "".join(
        f"<tr><td>{escape(p.get('name') or '')}</td><td>{escape(p.get('current_title') or p.get('title') or '')}</td>"
        f"<td>{escape(p.get('since') or '')}</td>"
        f"<td>{ {True: 'confirmed on own profile', False: 'NOT confirmed', None: 'cannot verify'}[p.get('confirmed')] }"
        f"{(' — ' + escape(p['why'])) if p.get('why') else ''}</td></tr>"
        for p in r.get("who_runs_it") or [])
    age = r.get("how_long_it_has_existed") or {}
    parts.append(
        "<div class='grid'><div class='card'><h2>Who runs it</h2>"
        + (f"<table><tr><th>Name</th><th>Title</th><th>Since</th><th>Check</th></tr>{rows}</table>"
           if rows else "<p class='muted'>No leader found on the record.</p>")
        + "</div><div class='card'><h2>How long it has existed</h2><ul>"
        f"<li>Founded: {escape(str(age.get('year_founded') or '?'))}</li>"
        f"<li>Website registered: {escape(str(age.get('domain_registered') or '?')[:10])}</li>"
        f"<li>Earliest founder start: {escape(str(age.get('earliest_founder_start') or '?'))}</li></ul>"
        + ("<h2>GST registration</h2>" + bullet_list(
            [f"{g['gstin']}: checksum {'ok' if g.get('checksum_ok') else 'FAILED'}, registry {g.get('registry')}"
             + (f", {g.get('status')}, {g.get('legal_name')}" if g.get('registry') == 'FOUND' else '')
             for g in r.get("registry") or []]) if r.get("registry") else "")
        + "</div></div>")

    # ---- sources
    fact_rows = "".join(
        f"<tr><td>{escape(f['claim'])}</td><td>{escape(json.dumps(f['value'], ensure_ascii=False)[:160])}</td>"
        f"<td>{source_cell(f, live_links)}</td>"
        f"<td><code>{escape(f.get('field_or_passage') or '')}</code>"
        + (f"<br><span class='muted'>“{escape(f['quote'][:240])}”</span>" if f.get('field_or_passage') == 'passage' else "")
        + f"</td><td>{escape(f.get('record_date') or 'not stated by source')}</td>"
        f"<td>{escape((f.get('retrieved_at') or '')[:19])}</td></tr>"
        for f in r.get("facts") or [])
    parts.append("<div class='card'><h2>Every fact and its source</h2>"
                 "<table><tr><th>Claim</th><th>Value</th><th>Source</th><th>Field or passage</th>"
                 f"<th>Record date</th><th>Retrieved</th></tr>{fact_rows}</table></div>")

    # ---- cost
    cost = r.get("cost") or {}
    parts.append(f"<div class='card'><h2>Cost of this job</h2><p>{cost.get('crustdata_credits')} Crustdata "
                 f"credits of {cost.get('credit_limit')} allowed · ≈ ₹{cost.get('inr')} "
                 f"(at ₹{cost.get('inr_per_credit_assumed')}/credit) · {cost.get('calls')} calls · "
                 f"{cost.get('elapsed_s')} s</p><p class='muted'>"
                 f"{escape(json.dumps(cost.get('by_endpoint') or {}))}"
                 + (f" · <a href='/ledger/{escape(r['job_id'])}'>full cost ledger</a>" if live_links else "")
                 + "</p></div>")
    return page(f"{vendor} — {r.get('verdict')}", "".join(parts),
                refresh_seconds=3 if r.get("stage") not in ("done", None) else None)
