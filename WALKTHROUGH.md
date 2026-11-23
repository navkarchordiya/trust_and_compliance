# Walkthrough

A guided tour of Vendor Check: what to run, what to look at, and where things live in the code.

## A two-minute tour

| Step | Run / open | What to look at |
|---|---|---|
| 1 | `python -m vendorcheck serve`, start a check on a real vendor | The verdict and its reasons, who runs it (confirmed on their own profile), how long it has existed, and every fact with its field, record date and retrieval time. Open "stored response" on one fact. |
| 2 | A lookalike, then Ecom Express (`docs/real_ecom_express.txt`) | A lookalike borrows a real firm's name on another domain; its records are never used. "Real" is not enough, it must be *active*: Ecom Express still has 13,000 employees on record, but its website is registered to Delhivery, which bought it, and its newest job posting is from 2024. Held for a person. |
| 3 | `./scripts/kill_demo.sh` | The run is killed mid-way with `kill -9` and resumed; the ledger shows no call paid twice; approving twice acts once. |
| 4 | `python -m vendorcheck costs` | Measured cost per check: 3–8 credits, 0.2 for a duplicate. |
| 5 | The approval form | Nothing irreversible happens without a named person; overruling the recommendation needs a written reason. |

## Kill and resume

1. Start the review app on a fresh database: `python -m vendorcheck --db data/demo.db serve`.
2. Start a check from the form (name, website, GSTIN if known), or a list with
   `python -m vendorcheck --db data/demo.db batch requests.json`.
3. Stop the server at any moment (`Ctrl+C` or `kill -9`) and start it again with the same command: it prints
   "resuming 1 unfinished check" and carries on. In the cost ledger, completed calls appear once.
4. Approve. Approve again: "already approved … nothing done again". `python -m vendorcheck outbox` shows each
   internal notification once.

## Where to change things

| Change | Where | Effort |
|---|---|---|
| New output field / remove a field | `config.toml` `[output] fields`, value in `rules.py` `answer` | 1–2 min |
| Lower cost or time limit | `config.toml` `[limits]` (news search is kept first under a tight budget) | 30 s |
| New red flag / threshold | `rules.py` (one `strong.append` / `weak.append`), threshold in `config.toml` | 2–3 min |
| New adverse word | `config.toml` `[media] adverse_terms` | 30 s |
| Approver must be from a list | `actions.decide_approval`: check `by` against a config list | 2 min |
| New source field | `pipeline.s_enrich`: one `f("claim", "json.path")` line; it is cited automatically | 1 min |
| "Active" window (e.g. 90 days) or turn the active check off | `config.toml` `[activity] recent_days` / `require_recent_activity` | 30 s |
| Weak signals that send a firm to a person | `config.toml` `[verdict] weak_signals_for_review` | 30 s |
| New request field (e.g. PAN) | `pipeline.s_take_request` (copy the `cin` lines), `__main__.py` flag, `web.py` form | 3 min |

Run `python -m unittest discover -s tests` after a change.

## Design questions

**100× the volume.** Each job is already the unit of work, checkpointed after every stage. Put job IDs on a
queue and run N workers (the code has no shared in-memory state). Swap SQLite for Postgres
(`SELECT … FOR UPDATE SKIP LOCKED` to claim jobs). One rate limiter per Crustdata endpoint shared by all
workers; 429s already back off using `X-RateLimit-Reset`. Cache Identify/Enrich by Crustdata company ID with a
freshness window: large carriers are vendors of many shippers, so the cost per job falls with volume.
The idempotency keys and the outbox stay as they are: they are what makes "exactly once" hold with many workers.

**A second client.** Add `client_id` to jobs, approvals, the vendor register and the outbox; load a
`config.toml` per client (their thresholds, limits, approvers, output fields). Evidence and raw responses are
never shared across clients; only the public-data cache (by Crustdata company ID) is.

**Why not one LLM agent?** Facts could come from model memory, verdicts drift between runs, and a timeout
looks like "no results". Here every fact is a stored field or a verbatim passage, the verdict is plain rules,
and the same inputs give the same answer, so it can be audited and tested.

## Ethics

- *Personal data:* only founders and up to 3 senior leaders, from public professional profiles, and only their
  current employer and title, plus directors the vendor itself names on its form (used only for the news
  screen). No protected attributes. Raw responses are purged after 30 days; facts stay.
- *Namesakes:* an article counts against a person only if it also names the vendor; a clearance based on
  absence is labelled weak; an unreadable page is "cannot verify", never a clearance.
- *Harmful actions:* clearing and rejecting both wait for a named person; overruling needs a reason; nothing is
  sent to the vendor or anyone outside the client's team.
- *Explaining decisions:* every verdict lists its reasons by type, and every fact links to the field or passage.
- *Contestability:* the vendor can be re-checked (`rescreen`) and a person can overrule with a recorded reason.

## Business case

See [FINANCE.md](FINANCE.md). In one line: ₹499 per decision with an evidence file, in about a minute, against
about ₹2,150 and several days for an outsourced check; the measured cost is 4–8 data credits plus at most ₹5 for
a GST lookup; *cannot verify* is free, so nobody pays for a guess; monitoring is ₹49 per vendor per month.
