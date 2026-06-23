# Interpreting the GPT Rollout Health Dashboard

A field guide for reading what's on the screen and turning it into a decision.
Open this next to the dashboard the first few times you use it.

---

## The question this dashboard answers

> *Is the GPT-via-WebSocket path good enough — and consistent enough across brands — for us to enable it for more customers?*

Everything else on the page is in service of that one question.

The dashboard compares two cohorts of brands:

- **Rollout (WebSocket)** — brands where GPT is already enabled on the WebSocket path.
  Default: `470, 257, 466, 416, 38, 221, 411, 301`.
- **Control (Traditional)** — brands still on the legacy GPT path, used as a baseline.
  Default: `367, 311, 371, 519, 246, 45, 259, 454`.

Both lists are editable in the Pinot fetch panel and persist in `localStorage`.

---

## Headline — the cohort comparison

This is the **decisioning view**. Read it first.

You'll see two cards side by side and a one-line verdict below them:

```text
┌──────────────────────────────┐ ┌──────────────────────────────┐
│ ROLLOUT (WEBSOCKET)          │ │ CONTROL (TRADITIONAL)        │
│ 3,210 ms  (first p95)        │ │ 1,950 ms  (first p95)        │
│ 5 brands · 4,213 requests    │ │ 8 brands · 27,580 requests   │
│ follow-up p95 4,870 ms       │ │ follow-up p95 2,210 ms       │
│ GREEN 1 · YELLOW 3 · RED 1   │ │ GREEN 6 · YELLOW 2 · RED 0   │
└──────────────────────────────┘ └──────────────────────────────┘

↑ Rollout (WebSocket) is 1,260 ms slower than control (traditional) at first-utterance p95.
```

### How to read each card

| Field | Meaning |
|---|---|
| Big number | Request-weighted **first-utterance p95 TTFB**, in ms. The "primary SLO" number. |
| `N brands` | How many brands in this cohort had data **above the Min sample size threshold**. |
| `N requests` | Total Pinot rows (first + follow-up) across all brands in the cohort. |
| `follow-up p95` | Same calculation for `conversation_turn != 1` turns. Usually lower than first p95 because the model is warm. |
| Health pills | How those brands distribute across the GREEN / YELLOW / RED tiers (computed per-brand using your health thresholds). |

### How the verdict line is computed

```text
Rollout first p95   = sum(brand_first_p95 × brand_first_N) / sum(brand_first_N)
Control first p95   = sum(brand_first_p95 × brand_first_N) / sum(brand_first_N)
Delta               = Rollout - Control
```

It's **request-weighted**, not a simple average — a single high-traffic brand counts more
than a brand with two requests. This matches "how an average user feels the latency."

> **Caveat.** This is good for **decisioning**, not for hypothesis testing. The number
> is computed from per-brand p95s, not from raw samples. For statistical comparison
> (Mann-Whitney, bootstrap CI, etc.) run a `GROUP BY cohort` aggregate directly in
> Pinot and analyze offline. The verdict line is the "would I be embarrassed by this
> in a leadership review" version of the comparison.

---

## Reading the brand health table

One row per brand that exists in **both** Pinot and MySQL adoption data.

| Column | What it tells you |
|---|---|
| **Brand** | Human-readable name from MySQL. |
| **Cohort** | `rollout` or `control` — only shown when both lists were fetched. |
| **Sessions** | Distinct `audiobot_practice_session.video_id` count for that brand in the window. From MySQL. |
| **Avg utt / session** | Average utterances per session. From MySQL. |
| **N** (bold) | Total Pinot requests in the window (first + follow-up). **Your trust-this-row signal.** |
| **First Requests / Avg / p50 / p95 / p99** | TTFB stats for `conversation_turn = 1` only. The user-facing latency. |
| **Follow-up Requests / Avg / p50 / p95 / p99** | TTFB stats for `conversation_turn != 1`. Lower than first because the model context is already loaded. |
| **Health** | GREEN / YELLOW / RED — the **worst** of the two buckets. A row is RED if *either* first OR follow-up trips the RED threshold. |

### Sort order

Rows are sorted by `first_p95_ttfb_ms` descending — slowest first-utterance at the top.
When both cohorts are present, rollout brands are grouped above control brands.

### What the color tints mean

- **Whole row tint** = the worst-of-buckets health tier:
  - <span style="color:#2ecc71">GREEN row</span>: both first and follow-up are in the green zone.
  - <span style="color:#f1c40f">YELLOW row</span>: at least one bucket is in the yellow zone.
  - <span style="color:#e74c3c">RED row</span>: at least one bucket has tripped the red threshold.
- **Per-cell tint** (only on p95/p99 cells): a single bucket is worse than the row's tier.
  Example: row is YELLOW because follow-up p95 = 7,200 ms, but the first-utterance p95
  cell is also tinted yellow to tell you *which side* drove the row's tier.

---

## The four insight cards (above the cohort comparison)

| Card | What it tells you |
|---|---|
| **Health mix** | Total GREEN / YELLOW / RED brand count across the table (above-threshold only). |
| **Slowest tail latency** | The brand with the highest *first-utterance* p95, AND separately the brand with the highest *follow-up* p95 (often different brands). |
| **Most adopted** | Brand with the most `total_sessions` from MySQL. The brand running the most tests. |
| **Healthiest brand** | Brand with the lowest first-utterance p95 among those classified GREEN. |

These all use only **above-threshold rows**, so a 1-sample brand can't be crowned "slowest".

---

## The banners — what they mean and how to react

There can be up to **three** notices around the brand table. Each tells you something
specific.

### Yellow warning banner: "N brand(s) hidden because total requests < ..."

Some brands had Pinot data but fell below the **Min sample size** threshold (default
10). Their percentiles aren't trustworthy enough for decisioning, so they're hidden.
Listed by name with their N.

**What to do:** if you genuinely want to see them, lower the threshold. Otherwise
ignore — these are the "I ran one test, please don't extrapolate" rows.

### Blue info banner: "N Pinot brand(s) had data but aren't in MySQL's adoption table"

Pinot returned latency data for brands that MySQL's adoption query didn't find.
Usually all the **control** brands land here — they're not running `audiobot_practice_session`
records because they're on the traditional path. They're **counted in the Cohort
comparison** above but **not shown in the brand table** (no name, no sessions).

**What to do:** nothing — this is the expected shape of the data when comparing
WebSocket vs traditional. If you see a *rollout* brand here, it means MySQL's window
didn't catch any sessions for it; widening the window usually fixes it.

### Dashed grey card: "No rollout brand had ≥ X GPT WebSocket requests in this window"

Empty state. Nothing in the table meets the threshold and there's nothing to render.
Either:

- The rollout cohort had a quiet window — try widening **Last N days**.
- The threshold is too high — try lowering **Min sample size**.

---

## Decision rubric

Combining the headline verdict with the brand-level table, here's a rough decisioning
guide. Adjust the cutoffs to whatever your SLO doc says.

| Situation | What it usually means | Suggested action |
|---|---|---|
| Rollout first p95 is **within ±200 ms** of control | The WebSocket path is at parity. | Enable more brands. |
| Rollout first p95 is **200–800 ms slower** | Real but small overhead. Could be tolerable. | Investigate biggest contributors (sort the table by first p95). Decide per brand. |
| Rollout first p95 is **> 800 ms slower** | Material regression. | **Pause expansion.** Investigate the slowest rollout brands first — the cell tints + Slowest-tail-latency card point you at the worst. |
| Rollout has **RED brand(s)** but control has none | One or more WebSocket brands are doing significantly worse than any traditional brand. | Investigate those specific brands before enabling more. |
| Rollout follow-up p95 is fine but **first p95 is bad** | Connection setup / first-chunk latency overhead. | Look at the WebSocket handshake path. |
| Rollout first p95 is fine but **follow-up p95 is bad** | Long-tail conversation latency. Backend pressure under load. | Look at downstream model and queueing behavior. |
| Both cohorts have **GREEN 0** | Either a bad window, a too-strict threshold, or genuinely poor latency across the board. | Verify the window with the diagnostic queries in [README.md](README.md) before drawing conclusions. |

---

## Common pitfalls

- **MySQL adoption SQL is filtered to brands with `node_key = 'gpt_test'`.** That's
  almost always the rollout cohort. Control brands won't appear in MySQL even though
  Pinot returns data for them — see the blue info banner above.
- **`conversation_turn = 1`** counts only conversations that **started inside the
  window.** A brand whose conversations all started before the window will have
  `first_total_requests = 0` and only follow-up data. Widen the window if you see
  this.
- **The Pinot bearer token rotates every ~24 hours.** A 401 from the Pinot fetch
  means re-mint the JWT, not that the data is broken.
- **Min sample size = 10** is decisioning-grade but not statistically rigorous.
  Bump to 30 or 100 if you want stronger guarantees; bump to 1 to see all rows
  (including noisy ones) for debugging.
- **The verdict line is request-weighted, not raw.** For statistical confidence
  intervals, run a Pinot query with `PERCENTILETDIGEST(duration, 95) GROUP BY cohort`
  directly.

---

## One-liners you might want to share

When pasting a snapshot in Slack or a doc:

> *"WebSocket rollout is currently **{delta} ms slower** than the traditional path
> at first-utterance p95 over the last {N} days, across {rollout_brand_count}
> rollout brands ({rollout_requests} requests) and {control_brand_count} control
> brands ({control_requests} requests). {GREEN_count} brand(s) are healthy,
> {YELLOW_count} need attention, {RED_count} need investigation."*

Replace the placeholders from the comparison card and the verdict line. That single
sentence is the deck-ready summary.

---

## See also

- [INSTALLATION.md](INSTALLATION.md) — how to start the bridge + open the dashboard.
- [README.md](README.md) — full feature reference, including the fetch SQL and bridge
  endpoint details.
