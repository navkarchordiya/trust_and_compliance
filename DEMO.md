# Demo and finals playbook

## 2-minute demo video (record by 5:30 PM)

| Time | Show | Say |
|---|---|---|
| 0:00–0:15 | README top | "Compliance teams are paid to decide whom to trust before money moves. Vendor Check does that whole job: a new vendor in, a sourced verdict out, and a person approves." |
| 0:15–0:50 | `python -m vendorcheck serve`, start a check on a real vendor | Verdict, the reasons, who runs it (confirmed on their own profile), how long it has existed, every fact with its field, record date and retrieval time. Click "stored response" on one fact. |
| 0:50–1:10 | A lookalike: real firm's name on another website | "The website is unknown, the name belongs to a firm on another domain: lookalike. We never borrow the real firm's records." |
| 1:10–1:30 | `./scripts/kill_demo.sh` | Killed mid-run with `kill -9`, resumed, ledger shows no call paid twice, approve twice: done once. |
| 1:30–1:50 | Cost card + `python -m vendorcheck costs` | "4–8 credits per check, a duplicate costs 0. We charge ₹499; an outsourced check is about ₹2,150 and takes days." |
| 1:50–2:00 | Approval form | "Nothing irreversible without a named person; overruling needs a written reason." |

## Finals: 3-minute live run (includes the kill)

1. `python -m vendorcheck serve` on a fresh database: `python -m vendorcheck --db data/finals.db serve`.
2. Paste the unseen request into the form (name, website, GSTIN if given). If it is a list, use
   `python -m vendorcheck --db data/finals.db batch request.json`.
3. When the judge says "kill": `Ctrl+C` or `kill -9` the server. Start it again with the same command: it
   prints "resuming 1 unfinished check" and carries on. Open the cost ledger: completed calls show once.
4. Approve. Press Approve again: "already approved … nothing done again". Show `outbox`.

## Live fix (5 minutes): where things are

| Likely change request | Where | How long |
|---|---|---|
| New output field / remove a field | `config.toml` `[output] fields`, value in `rules.py` `answer` | 1–2 min |
| Lower cost or time limit | `config.toml` `[limits]` (news search is kept first under a tight budget) | 30 s |
| New red flag / threshold | `rules.py` (one `strong.append` / `weak.append`), threshold in `config.toml` | 2–3 min |
| New adverse word | `config.toml` `[media] adverse_terms` | 30 s |
| Approver must be from a list | `actions.decide_approval`: check `by` against a config list | 2 min |
| New source field | `pipeline.s_enrich`: one `f("claim", "json.path")` line; it is cited automatically | 1 min |

Run `python -m unittest discover -s tests` after the change (about 2 s).

## System design questions

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

## Ethics questions (25 points)

- *Personal data:* only founders and up to 3 senior leaders, from public professional profiles, and only their
  current employer and title. No protected attributes. Raw responses are purged after 30 days; facts stay.
- *Namesakes:* an article counts against a person only if it also names the vendor; a clearance based on
  absence is labelled weak; an unreadable page is "cannot verify", never a clearance.
- *Harmful actions:* clearing and rejecting both wait for a named person; overruling needs a reason; nothing is
  sent to the vendor or anyone outside the client's team.
- *Explaining decisions:* every verdict lists its reasons by type, and every fact links to the field or passage.
- *Contestability:* the vendor can be re-checked (`rescreen`) and a person can overrule with a recorded reason.

## Finance questions (25 points)

See [FINANCE.md](FINANCE.md). One line: "₹499 per decision with an evidence file, in about a minute, against
~₹2,150 and days for an outsourced check; our measured cost is 4–8 credits plus at most ₹5 for the GST lookup;
*cannot verify* is free, so the client never pays for a guess; monitoring at ₹49 per vendor per month."
