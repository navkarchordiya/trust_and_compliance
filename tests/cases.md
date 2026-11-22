# Test cases (real vendors, events dated after 1 September 2026)

Fill these in before trusting the service. Each needs the request and the correct finished result.

| # | Case | Request (name + website) | Correct result | Why |
|---|---|---|---|---|
| 1 | Honest, established firm | | REAL_AND_TRADING | |
| 2 | Honest small firm (few employees) | | REAL_AND_TRADING or CANNOT_VERIFY, never FRONT | One weak signal must not fail it |
| 3 | Front / shell company (from news after 1 Sep 2026) | | FRONT_SUSPECTED or HOLD_FOR_REVIEW | |
| 4 | Lookalike name (name of a known firm, different website) | | HOLD_FOR_REVIEW / FRONT_SUSPECTED | Name mismatch |
| 5 | Director with a namesake in the news | | REAL_AND_TRADING, namesake cleared | |
| 6 | Director genuinely in adverse news | | HOLD_FOR_REVIEW | |
| 7 | Same vendor submitted twice | | HOLD_FOR_REVIEW (duplicate) | |
| 8 | Tiny firm with no records anywhere | | CANNOT_VERIFY, names what is missing | |
| 9 | Source timeout (simulated) | | CANNOT_VERIFY | Covered offline in `test_offline.py` |

Tip: find fronts with Crustdata Web Search, e.g. news about "shell companies" or "fake GST invoices"
busted after 1 September 2026, then take a named company and its website.
