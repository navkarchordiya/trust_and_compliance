# Business case

## Who pays today, and what they pay

The target customer is a mid-size Indian third-party logistics (3PL) company that onboards carriers, warehouse operators and suppliers
continuously. Before money moves, someone must decide whether each new vendor is real. Today that is
bought in one of three ways:

| Option today | Price (₹, public list prices) | What you get | Source |
|---|---|---|---|
| Company data report (MCA, GST, legal) | ₹69 – ₹1,500 per company; director legal report ₹299 per director | Data, **no decision**; a person still reads it | [InstaFinancials pricing](https://instafinancials.com/products/instascreener.html), [Director Legal](https://www.instafinancials.com/products/DirectorLegal.html) |
| Outsourced due-diligence firm | ~₹2,150 per company due-diligence check; ₹2,000/hour for bespoke work; Big-4 counterparty due diligence is a managed service (KPMG India runs 8,000+ reports a year) | A decision, in days | [IndiaMART listings](https://m.indiamart.com/city/gurgaon/background-verification.html), [IndiaMART due diligence](https://m.indiamart.com/impcat/due-diligence-service.html), [KPMG India](https://kpmg.com/in/en/services/advisory/consulting/risk-and-integrity-advisory/forensic-managed-services/counterparty-due-diligence.html) |
| In-house analyst | 30–60 minutes per vendor just for collection, validation and entry; full onboarding effort was ~72 hours per supplier before automation in one Forrester study | Slow, inconsistent, no evidence trail | [Stampli benchmarks](https://www.stampli.com/resources/vendor-onboarding-cycle-time-benchmarks/), [AZ Big Media / Forrester](https://azbigmedia.com/business/from-bottlenecks-to-benchmarks-workflow-automation-strategies-for-safer-vendor-onboarding) |

**Buyer:** the client's procurement / vendor-onboarding team, with compliance signing off. Budget line:
vendor onboarding / third-party risk.

## Our price (proposal)

| Product | Price | Charged when |
|---|---|---|
| Vendor check (verdict + evidence file) | **₹499 per vendor** | A verdict is delivered. *Cannot verify* is free: the client pays for answers, not for silence. |
| Ongoing monitoring of approved vendors (`rescreen`) | **₹49 per vendor per month** | Each monthly re-screen |

₹499 sits between a raw data report (no decision) and an outsourced check (~₹2,150, days). It is a
decision with an evidence file, in about a minute.

## Measured cost per job

The service records the cost of every call (`python -m vendorcheck ledger JOB`) and summarises it across
runs (`python -m vendorcheck costs`). Credits per job, from the published credit prices
([Crustdata pricing docs](https://docs.crustdata.com/general/pricing)):

| Call | Credits | Notes |
|---|---|---|
| Company Identify | 0.2 | the docs list it as free, but the live ledger (`X-Credits-Used`) charged 0.2 per matched lookup on 22 Nov |
| Company Enrich (basic, headcount, people, news, metadata) | 2 | founders' employment history comes embedded: no Person Enrich needed |
| Vendor homepage fetch (GSTIN) | 1 | skipped when the GSTIN is in the request |
| One combined news search (company + leaders) | 1 | |
| Article fetches | 0 – 4 | only for articles that name a leader next to an adverse word |
| Job Search (newest 3 postings: is it active now) | 0.09 | 0.03 per listing returned; 0 when there are none |
| RDAP domain registration | 0 | public; 1 credit only if rdap.org fails and the record is read through Web Fetch |
| GST registry lookup | ₹0.70 – ₹5 | e.g. ₹3,500 per 5,000 calls ([GSTZen](https://gstzen.in/gst-validator-api-pricing-details)); ₹1–5 at volume ([KYB vendor survey](https://javajack.github.io/b2b-lending-india/04-integrations/kyc-kyb-vendors/)) |
| **Total** | **3.3 – 8.3 credits + ≤ ₹5** | a duplicate stops after Identify: **0.2 credits** |

Crustdata does not publish a rupee or dollar price per credit (it is set by plan), so the cost per job is
shown for a range of credit prices. Set `[cost] inr_per_credit` in `config.toml` to the real rate and
`costs` reports the measured figure.

| ₹ per credit | Cost per job (worst case: 8.3 credits + ₹5 GST) | Gross margin at ₹499 |
|---|---|---|
| ₹4 | ₹38 | 92% |
| ₹8 | ₹71 | 86% |
| ₹20 | ₹171 | 66% |

No LLM is called today, so there is no model cost per job.

## Would a firm pay?

- **Cheaper and faster than the alternative that gives a decision** (₹499 in a minute vs ~₹2,150 in days),
  and it returns an evidence file a compliance auditor can follow field by field.
- **It removes the expensive mistake:** paying a front company. One avoided fraudulent payment covers
  thousands of checks.
- **Honest about uncertainty:** *cannot verify* is free and names what is missing, so the client never pays
  for a guess.
- **Recurring revenue:** monitoring approved vendors (adverse news, GST status) at ₹49/vendor/month.

## Scale

At 100× volume the cost per job does not change (no per-seat or fixed cost per job). Identify and Enrich
results can be cached across clients by Crustdata company ID with a freshness window, which lowers the
cost per job further for common vendors (large carriers are vendors of many 3PLs).
