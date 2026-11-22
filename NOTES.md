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

## Next

- Run on real vendors with the Crustdata key; check the real shape of `people` in Company Enrich.
- Gather real test cases dated after 1 Sep 2026 (see `tests/cases.md`).
- LLM step to read articles: extract the person's employer/city/role so namesakes are cleared on
  positive evidence, not just absence of the vendor's name.
- Registry source for incorporation date and directors (MCA via a KYC API sandbox).
