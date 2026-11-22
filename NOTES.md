# Build notes

What we tried, what works, and what comes next. (The guide: "They are the heart of your demo.")

## Approaches we considered

1. **One LLM agent with tools** (Crustdata MCP + web search), asked to write the verdict.
   Fast to build, but facts can come from model memory, verdicts drift between runs, and a
   timeout looks like "no results". Hard to prove "each action happens once".
2. **Fixed pipeline, rules decide, LLM only reads text** (chosen). Every fact is a field in a
   stored API response or a verbatim passage from a fetched page. The verdict is plain rules in
   `config.toml`, so it is repeatable and explainable, and a requirement change is a config edit.
3. **Registry-first** (MCA / GST portals). The strongest legal source, but the public portals
   sit behind CAPTCHAs, and a front can carry genuine papers anyway, so registry presence alone
   cannot prove a business is trading. Kept as a future source.

## Decisions

- Four outcomes per call (FOUND / NOT_FOUND / UNAVAILABLE / ERROR), because "a slow source looks
  like a missing one". Only NOT_FOUND counts as an answer.
- Never join records on name alone ("records share no common key"): Identify by domain first,
  and flag a name mismatch between the request and the record as a lookalike signal.
- One weak signal never fails a firm; FRONT_SUSPECTED needs 2 independent strong signals.
  A wrong answer costs more than "cannot verify", and honest firms must pass.
- Namesakes: an article only counts against a person if it also names the vendor (or its
  domain). Otherwise it is cleared, with the reason shown.
- Rejecting a vendor is irreversible too, so it waits for a person like clearing does.
- Domain registration date (RDAP) is an independent clock: a firm "founded 1998" whose website
  appeared last month is a strong signal.

## Log

- 11:40 Repo created, plan locked: theme 5.1 + 5.2, Python stdlib only.
- 11:55 Pipeline, store, source wrapper, rules, approvals, offline tests (9 passing).
- 12:10 Checked a real Company Enrich response (delhivery.com) through the Crustdata plugin:
  - Plain `people` returns ~300 decision makers: **5.5 MB** per call. Nested paths
    (`people.founders`, `people.cxos`, `headcount.total`, ...) cut it to ~350 KB for the same 2 credits.
  - Each person is a full profile (`basic_profile.name`, `social_handles...profile_url`) with
    employment history embedded, so "who runs it" needs no Person Enrich call (saves ~3 credits per vendor).
  - `employer_company_id` is a list of strings (`["5798"]`); our first version compared integers and
    would never have confirmed anyone. Fixed, with a regression test on the real (slimmed) response.
  - CXO lists contain junk self-reported titles ("reus zxz, Ceo" at Delhivery, 0 connections).
    Real executives had 850+ connections; CXOs below `min_connections = 50` are skipped.
  - `basic_info.status` ("active") is on the record and is now used as a fact.
- 12:25 Live hard cases:
  - Lookalike ("Blue Dart Express" + delhivery.com): HOLD_FOR_REVIEW, name mismatch flagged. Correct.
  - Shakti Bhog Foods (real company, CMD arrested, ED case) first came back FRONT_SUSPECTED. Wrong.
    Causes and fixes:
    - Name-only Identify returns 25 candidates all at confidence 0.6 (incl. "Tyson Foods"), so the
      score means nothing. Now only candidates with every distinctive word of the name count, and
      records sharing one website are one business (we take the one with the most staff).
    - Adverse news counted as a front signal. Now: front signals (fake business) and adverse news
      (real but risky) are separate. Adverse news sends the vendor to a person; it never makes a front.
    - Web Fetch returned `success: true` for a CloudFront 403, a bot-check page and a Google redirect
      page. The old code read those as the article and "cleared" a namesake. Now a block or redirect
      page, or a page that does not even contain the person's name, is "cannot verify", never a clearance.
    - News search now uses the distinctive name ("Shakti Bhog", not "Shakti Bhog Food").
  - Not found yet: a named shell company with a website in news after 1 Sep 2026; GST-fraud reports
    rarely name the shell firms.

- 12:08 (Vedant) One combined news search for the company and all leaders: live Delhivery run ~95 s → 17 s.
- 12:30 Hard cases and live checks through the Crustdata MCP connector:
  - Identify by **name** returns padding: "Delhivery Limited" came back with Delhivery, "Delhivery courier",
    Reliance, ITC, Britannia ... **all at confidence 0.6**. The old code took row 0, so an unknown vendor
    would have been matched to a random big firm. Now candidates are ranked by name similarity, and below
    0.85 the answer is *cannot verify*, never an accusation.
  - Lookalike bug: a request with an unknown website fell back to name, matched the real firm and would have
    been **cleared with the real firm's records**. Now a strong "Lookalike" red flag; enrich is skipped.
  - Identify for an unknown domain returns `matches: []` (handled as NOT_FOUND).
  - News search results have no date field (`position, title, url, snippet, source`), so article facts
    carry "record date not stated"; the fetch time is kept as `retrieved_at`.
  - Delhivery's homepage has no GSTIN and `/contact-us` is a 404 page that still returns `success: true`.
    So the registry stage reads only the homepage, and a GSTIN can come in the request (`--gstin`).
  - Adverse terms are whole words in the same sentence as the name ("by default" no longer matches), and
    victim context ("posing as", "in the name of", "beware of") is not adverse news.
  - Time limit counted from job creation, so a resume after a long pause skipped every call. Now it counts
    from when the current process picked the job up.
  - Identify is free (docs.crustdata.com/general/pricing); `X-Credits-Used` confirmed as the header.

- 12:45 Merged with Vedant's live fixes (kept his name grouping, adverse vs front split, block-page detection).
  Added: client vendor-list import (duplicates by website / GSTIN / name, even with sources down),
  JSON-lines event log per job, written reason required to overrule the recommendation, retention purge,
  `costs` (measured mean / p90 per job), `scripts/kill_demo.sh` for the kill-on-stage demo. 37 tests.

- 13:10 Review app (`serve`): evidence file, raw response behind each API fact, ledger, approvals; resumes
  unfinished checks on start. Enrich requests `metadata` (refresh date as record date when Crustdata fills it;
  null for Delhivery today). FINANCE.md with sourced ₹ benchmarks. `rescreen` for monitoring: carries the
  profile over, re-runs news + GST (~1–2 credits), alerts once. `eval` accepts `!REAL_AND_TRADING`: news about
  shell firms after 1 Sep 2026 rarely names one with a website, so for fronts the property we test is
  "never approved". 40 tests.

- 13:40 **Live eval on real Crustdata data: 8/8 pass, 0 wrong answers** (`python scripts/replay_live.py`;
  23 real answers recorded through the Crustdata connector, now a regression test). What the real data broke:
  - "Sharma Trading Co" + unknown website loosely matched four different "Sharma ... Trading" firms; we called it
    a **lookalike** of one (a false accusation). Now a lookalike needs near-exact name matches pointing at ONE firm.
  - "RK Enterprises" by name = 10+ different firms: we paid to enrich a random one and dated its website. Now an
    ambiguous name skips enrich, news and domain age (0 credits) and goes to a person.
  - Blue Dart's only "founder" is a self-declared profile with **0 connections**; three "CEO"s have 1. Founders
    now need ≥10 connections, CXOs ≥50, before anyone is reported as running the company.
  - Most "Blue Dart" fraud news is scams **in its name**, often in Hindi ("के नाम पर" = "in the name of"). Victim
    markers expanded (English + Hindi); Hindi adverse words added for mixed headlines (Shakti Bhog "CMD अरेस्ट").
  - `https://delhivery.com` fails (certificate) while `www.` works; Shakti Bhog serves a 403 bot-check page.
    Homepage read now tries `www.` first and treats block pages as unreadable, never as "no GSTIN".
  - Same name + different website was stopped as a "duplicate" (skipping the lookalike's checks). Now flagged
    and the check continues; a name match only counts as a duplicate when nothing else tells them apart.
  - A website with no DNS record is reported (weak signal).

- 13:45 Independent code review (a fresh reviewer with no context) found 15 defects, 6 of which could give a
  wrong verdict; all fixed with regression tests (`tests/test_review_fixes.py`, 57 tests):
  rejecting a lookalike wrote the real firm into the register; crime words ("cyber fraud") were treated as
  victim markers; a leader's article naming the short brand was cleared; name-only and "record does not list
  this website" identities could be approved; "real and trading" needed only a confirmed leader; "N/A" or free
  text produced lookalike accusations; odd response shapes crashed a stage forever; a call killed in flight was
  not in the ledger; two racing approvals both acted; a kill after hand-over could leave a job open; decline
  was not atomic; some flags had no cited field; the review app had no CSRF/Host check.

## Declared before 10:15

TODO before submitting: list anything written before 10:15 (code, prompts, notes), or write "Nothing".

## Next

- Run on real vendors with the Crustdata key in `.env`.
- Gather real test cases dated after 1 Sep 2026 (see `tests/cases.md`).
- LLM step to read articles: extract the person's employer/city/role so namesakes are cleared on
  positive evidence, not just absence of the vendor's name.
- Registry source for incorporation date and directors (MCA via a KYC API sandbox).
