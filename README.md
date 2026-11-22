# Vendor Check — Trust and compliance (ContextCon 05)

An agent that does the whole job of a vendor verification firm: given a new vendor's name and
website, it decides whether the vendor is a **real, trading business**, says **who runs it**,
**how long it has existed** and **anything that looks off**, then screens the people who run it
against the news and **clears innocent namesakes** (themes 5.1 Vendor Check + 5.2 Adverse Media Screen).

## The job, and who pays for it today

Before money moves to a new vendor, a compliance team or an outside verification and screening
firm checks that the business is who it says it is. Our client, a mid-size Indian 3PL, onboards
carriers, warehouse operators and suppliers every week and pays for each check. A front company
can carry genuine papers, records share no common key, and a slow source looks like a missing
one, so the check is slow, manual and error-prone.

## Input

```bash
python -m vendorcheck run --name "Acme Freight Pvt Ltd" --website acmefreight.in
python -m vendorcheck run --text "Here is a new vendor: 'Acme Freight', acmefreight.in. Is it real?"
```

## Finished result

One verdict, with every fact linked to its source (the field or passage, and the date retrieved):

| Verdict | Meaning | Recommended action (a person approves) |
|---|---|---|
| `REAL_AND_TRADING` | Identity resolved, someone confirmed running it, evidence it operates, no strong red flag | add to approved vendors |
| `FRONT_SUSPECTED` | ≥ 2 independent strong red flags, each with a source | reject vendor |
| `HOLD_FOR_REVIEW` | One strong red flag, adverse news linked to the vendor, a duplicate, or an ambiguous identity | manual review |
| `CANNOT_VERIFY` | A key question has no source (no record, timeout, budget). The output names what is missing | manual review |

The result also lists: who runs it (confirmed on their own profile or not), founding year and
domain registration date, trading evidence, red flags, namesakes cleared, what could not be
verified, every fact with its source, and the cost of the run.

## Pass mark

- Honest firms and innocent namesakes pass; fronts are caught **with evidence**.
- Every claim has a source; no fact comes from model memory.
- A source that times out is never read as "does not exist": it becomes *cannot verify*.
- Clearing **and rejecting** a vendor both wait for a person's approval, and happen exactly once.
- A run killed at any moment resumes where it stopped without paying twice for a call.
- Each run stays inside its credit and time limit (`config.toml`).

## Data sources

| Source | Used for | Cost |
|---|---|---|
| Crustdata `POST /company/identify` | Resolve the domain or name to one company, with a confidence score | 0.2 credits per match |
| Crustdata `POST /company/enrich` | Founding year, headcount, HQ, hiring, founders/CXOs, news | 2 credits |
| Crustdata `POST /person/enrich` | Confirm each leader's current employer is the vendor | 1+ credit per person |
| Crustdata `POST /web/search/live` (news, IN) | Adverse news about the company and each leader | 1 credit per page |
| Crustdata `POST /web/enrich/live` | Fetch each article; the passage is stored as evidence | 1 credit per page |
| RDAP (`rdap.org`) | Domain registration date, independent of the company's own claims | free |

## Approvals (human in the loop)

```bash
python -m vendorcheck approvals                          # what is waiting
python -m vendorcheck approve JOB --by "Your Name"       # run the recommended action
python -m vendorcheck approve JOB --by "Your Name" --action reject_vendor
python -m vendorcheck decline JOB --by "Your Name"
```

Agents stay in the room: notifications go to an internal `outbox` table for the team, never to
the vendor or anyone outside.

## Robustness

```bash
python -m vendorcheck run --website acmefreight.in   # kill it at any moment (Ctrl+C / kill -9)
python -m vendorcheck resume                         # carries on from the stage it was in
python -m vendorcheck ledger JOB                     # every call, status, attempts, credits
python -m vendorcheck raw CALL_ID                    # the stored raw response behind a fact
```

## Setup

```bash
cp .env.example .env        # add CRUSTDATA_API_KEY
python -m unittest discover -s tests -v
```

Python 3.11+, standard library only.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the components and data flow, and
[NOTES.md](NOTES.md) for the approaches we considered and what we learned.
