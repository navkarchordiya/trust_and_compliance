# Design notes

The approaches we considered, the decisions behind the design, and what testing on real data changed.

## Approaches we considered

1. **One LLM agent with tools** (Crustdata MCP + web search), asked to write the verdict.
   Fast to build, but facts can come from model memory, verdicts drift between runs, and a
   timeout looks like "no results". Hard to prove "each action happens once".
2. **Fixed pipeline, rules decide, LLM only reads text** (chosen). Every fact is a field in a
   stored API response or a verbatim passage from a fetched page. The verdict is plain rules in
   `config.toml`, so it is repeatable and explainable, and changing a rule is a config edit.
3. **Registry-first** (MCA / GST portals). The strongest legal source, but the public portals
   sit behind CAPTCHAs, and a front can carry genuine papers anyway, so registry presence alone
   cannot prove a business is trading. Kept as a future source.

## Decisions

- Four outcomes per call (FOUND / NOT_FOUND / UNAVAILABLE / ERROR), because a slow source looks
  like a missing one. Only NOT_FOUND counts as an answer.
- Never join records on name alone (records share no common key): identify by domain first,
  and flag a name mismatch between the request and the record as a lookalike signal.
- One weak signal never fails a firm; FRONT_SUSPECTED needs 2 independent strong signals.
  A wrong answer costs more than "cannot verify", and honest firms must pass.
- Front signals (a fake business) and adverse news (a real but risky business) are kept apart.
  Adverse news sends a vendor to a person; it never makes a firm a front.
- Namesakes: an article only counts against a person if it also names the vendor (or its
  domain). Otherwise it is cleared, with the reason shown, and the clearance is marked weak.
- Rejecting a vendor is irreversible too, so it waits for a person like clearing does.
- Domain registration date (RDAP) is an independent clock: a firm "founded 1998" whose website
  appeared last month is a strong signal.
- "Real" is not enough, the vendor must be *active*: approval needs a dated record from the last
  180 days (a job posting, or news naming the firm).
- Sandbox data never decides a real vendor. The GST registry provider is off (`[registry] provider =
  "none"`) until production keys are set; GSTINs still get the offline check-digit test, and the
  registry check reads *cannot verify*.

## What real data taught us

Every lesson below came from running the service on live Crustdata data for Indian logistics firms
(recorded on 22 Nov 2026 and replayed as tests: `python scripts/replay_live.py`, 16/16 pass, 0 wrong).

**The API is not shaped like the docs say.**
- Plain `people` in Company Enrich returns about 300 decision makers: **5.5 MB** per call. Nested paths
  (`people.founders`, `people.cxos`, `headcount.total`, ...) cut it to about 350 KB for the same 2 credits.
- Each person is a full profile with employment history embedded, so "who runs it" needs no Person Enrich
  call (about 3 credits saved per vendor).
- `employer_company_id` is a list of strings (`["5798"]`); comparing integers would never have confirmed
  anyone.
- Identify is documented as free but charged 0.2 credits per match.
- News search results carry no date, so article facts say "record date not stated" and keep the fetch time.

**Identity is the hardest part.**
- Identify by *name* returns padding: "Delhivery Limited" came back with Reliance, ITC and Britannia, and
  "Shakti Bhog Foods" with Tyson Foods and McCain, all at confidence 0.6. Candidates are now ranked by name
  match; a vague name is *cannot verify*, never an accusation.
- Several records can share one website ("Shakti Bhog", 237 staff, and "Shakti Bhog Food", 3 staff): they are
  one business, and the main profile is used.
- "Sharma Trading Co" loosely matched four different firms; calling it a lookalike of one would have been a
  false accusation. A lookalike now needs near-exact name matches pointing at one firm.
- A request with an unknown website fell back to the name, matched the real firm and would have been cleared
  with the real firm's records. It is now a strong "Lookalike" red flag.
- Brands and rebrands: "XpressBees" is filed as "Xpressbees (BusyBees Logistics Solutions Pvt. Ltd.)"; Gati now
  lists Allcargo's website first after the merger, which is reported as a likely rebrand, not a lookalike.
- Ecom Express still shows 13,000 employees, but `ecomexpress.in` is registered to Delhivery Limited, which
  bought it. It is no longer approved: a person confirms which entity gets paid.

**Who runs it.**
- CXO lists contain junk self-reported titles ("reus zxz, Ceo" at Delhivery with 0 connections; Blue Dart's
  only "founder" also has 0). Founders need ≥10 connections and CXOs ≥50 before anyone is reported as running
  the firm, and only leadership titles count.
- Directors named on a vendor's form are screened in the news but not looked up by name: a name search
  returned strangers.

**Adverse news.**
- Shakti Bhog Foods (a real firm whose CMD was arrested) first came back FRONT_SUSPECTED. The fixes: the
  identity fix above, keeping adverse news apart from front signals, and searching the distinctive name
  ("Shakti Bhog", not "Shakti Bhog Food"). It is now held for review with both articles cited.
- Most "Blue Dart" fraud news is about scams *in its name*, often in Hindi ("के नाम पर", "in the name of").
  Victim phrases are not adverse news; Hindi adverse words are matched in mixed headlines.
- Words like "FIR" or "raid" appear when the vendor is the victim (Xpressbees), so they need a second adverse
  word about the vendor.
- Web Fetch returned `success: true` for a CloudFront 403, a bot-check page and a Google redirect page. Such a
  page, or one that does not even contain the person's name, is *cannot verify*, never a clearance.
- Reports on GST-fraud rings rarely name the shell firms, so for fronts the tested property is "never
  approved".

**Speed and cost.**
- One combined news search for the company and all its leaders cut a live check from about 95 s to 17 s.
- Job Search rate-limits bursts (429, retry after 50 s); the client waits for the rate-limit header.
- `https://delhivery.com` fails on its certificate while `www.` works: the homepage read tries `www.` first.

## Independent reviews

Two code reviews by a fresh reviewer with no context found 26 defects (each reproduced first); all are fixed
with regression tests. The ones that could have given a wrong verdict or a wrong action:
- rejecting a lookalike wrote the real firm into the vendor register;
- crime words ("cyber fraud") were treated as signs the vendor was the victim;
- name-only identities could be approved, and "real and trading" needed only one confirmed leader;
- two approvals at the same moment could both act, and a decline was not atomic;
- a call killed in flight was missing from the cost ledger;
- a job-posting date in another format crashed the decision and left the job stuck;
- a posting without a title approved a vendor with no cited fact;
- the review app had no CSRF or Host check.

## Known limits and next steps

- **GST registry:** needs production provider keys; until then it is *cannot verify*.
- **MCA register:** status and directors are not read directly (mirrors block automated reads); the CIN is
  decoded offline for incorporation year, state and company type.
- **Namesakes:** clearance is based on the vendor's name being absent from the article. An LLM step that
  extracts the person's employer, city and role would clear namesakes on positive evidence instead.
- **Scale:** see [ARCHITECTURE.md](ARCHITECTURE.md) for moving from SQLite to a queue with workers.
