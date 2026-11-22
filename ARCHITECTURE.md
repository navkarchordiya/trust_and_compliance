# Architecture

```mermaid
flowchart TD
    REQ[Request: name + website<br/>or plain words] --> TAKE[1. Take the request<br/>parse, flag what is vague]
    TAKE --> PLAN[2. Plan<br/>checks + credit estimate vs limit]
    PLAN --> GATHER

    subgraph GATHER[3. Gather the facts — each sub-stage checkpointed]
        ID[identify<br/>Crustdata Company Identify] --> EN[enrich<br/>Crustdata Company Enrich]
        EN --> AGE[domain_age<br/>RDAP]
        AGE --> LEAD[leaders<br/>Crustdata Person Enrich]
        LEAD --> MEDIA[media<br/>Crustdata Web Search + Fetch<br/>namesake clearing]
    end

    GATHER --> DECIDE[4. Decide<br/>rules in config.toml]
    DECIDE --> HAND[5. Act, or hand over<br/>approval queue]
    HAND -->|person approves| ACT[Action executor<br/>idempotency key, one transaction]
    HAND -->|person declines| CLOSE[closed, no action]
    ACT --> REG[(vendor register)]
    ACT --> OUT[(outbox: internal team only)]
    DECIDE --> SHOW[6. Show its work<br/>verdict, facts + sources, cost]

    subgraph SC[SourceClient — every outside call]
        RETRY[retries + backoff<br/>timeout / 5xx / 429] --> CLASS[FOUND / NOT_FOUND /<br/>UNAVAILABLE / ERROR]
        BUDGET[credit + time limit check] --> RETRY
        CLASS --> CACHE[(calls table:<br/>raw responses = evidence,<br/>credits = cost ledger)]
    end

    ID -.-> SC
    EN -.-> SC
    AGE -.-> SC
    LEAD -.-> SC
    MEDIA -.-> SC

    subgraph DB[SQLite job store]
        JOBS[(jobs + stage)] --- STAGES[(stage_outputs)] --- FACTS[(facts)] --- APPR[(approvals)] --- ACTS[(actions)]
    end
```

## Where things sit

| Concern | Where | How |
|---|---|---|
| Data calls | `vendorcheck/crustdata.py`, `SourceClient.call` | All calls go through one wrapper |
| Retries | `SourceClient.call` | Timeouts, 5xx and 429 retried with backoff (429 waits for `X-RateLimit-Reset`) |
| Silence ≠ answer | `SourceClient.call` → `rules.py` | `UNAVAILABLE` / `ERROR` become *cannot verify*, never a red flag |
| Cost ledger | `calls` table | `x-credits-used` header per call; `python -m vendorcheck ledger JOB` |
| Limits | `SourceClient._over_budget` | Calls past the credit/time limit are skipped and reported |
| Evidence | `vendorcheck/evidence.py` | API facts = JSON path into the stored response; web facts = a passage that must appear verbatim in the fetched page |
| Resume after kill | `Pipeline.run`, `Store.save_stage` | Stage output + stage advance in one transaction; completed calls are cached per job |
| Approvals | `approvals` table, `actions.py` | Clearing and rejecting both wait for a person |
| Exactly once | `actions` table (idempotency key) | Approval, action, register entry and notification commit together |
| Rules and output fields | `config.toml` | Thresholds, limits and output fields change without code changes |

## Scaling notes (for the design question)

- **100× volume:** the stages are already independent and checkpointed; put jobs on a queue
  (one row per job is already the unit of work) and run N workers. Cache Identify/Enrich
  results across jobs by company ID with a freshness window, and share one rate limiter per
  Crustdata endpoint across workers.
- **A second client:** add `client_id` to jobs, approvals and the vendor register, and load a
  `config.toml` per client (their own thresholds, limits and approvers).
