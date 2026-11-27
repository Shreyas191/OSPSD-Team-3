# Launch Readiness Record

This is the HW3 Milestone 2 launch-readiness record for **Course Autopilot** (handout, Level 5). It starts with the launch we promise to support and how we will judge it (Level 5 §1, task 7.1), followed by a capacity estimate across the whole request path (§2, task 7.2), and a summary of the failure analysis (§5, task 7.4). The later sections (load test, failure analysis, rollout, launch decision) are added by their own issues and link back here.

**Status (Nov 27, 2026):** launch criteria and capacity estimate written before the agent loop, proposals and the interface are built (#89–#95); Gemini's limits and one real S1 turn added after review. Every number below is tagged with where it comes from:

- **[measured]**: we measured it, and the run is listed under [Measurements so far](#measurements-so-far);
- **[published]**: a provider's documented limit, linked; for Gemini, the limits AI Studio shows for our key's project;
- **[assumed]**: an assumption we state and will replace with a measurement in the load test (#111).

The design these numbers apply to is the Milestone 2 chapter of DESIGN.md, in review in [PR #80](https://github.com/Shreyas191/OSPSD-Team-3/pull/80): interface → agent service → API service → Neon, Gemini, and each student's own Dropbox, Trello and Google Calendar.

## 1. The launch and its acceptance criteria

### Audience and workload

| Item | Value | Basis |
|---|---|---|
| **Launch window** | Launch week, Dec 2–6, 2026, on the production environment | Course timeline |
| **Audience** | Rollout in three steps: our team (5 people), then one sister team, then the class and course staff. The handout asks each student to try two other teams' products, so we expect roughly **30–40 distinct users** over the week | [assumed]: about 7 teams of 5, each trying 2 of the other 6 products, plus staff |
| **Simultaneous users we promise to support** | **20** actively using the product at the same time | [assumed]: a whole class session trying products at once is the worst realistic case; half the expected users at once leaves room |
| **One user's session** | Sign in, connect Dropbox, Trello and Google Calendar (first time only), then set up **2 courses**, each with: upload a syllabus, one agent turn that reads it and asks a clarifying question, a second turn that proposes the changes, approve, execute, read the outcomes | [assumed]: key scenario S1 as traced in DESIGN.md, with the clarification step it shows |
| **Size of one course** | 9 deadlines → 9 calendar events, 9 Trello cards, 1 reference document (19 provider writes) | The S1 trace in DESIGN.md |
| **Pace** | One course set up every 10 minutes per active user | [assumed] |
| **Test duration** | 15 minutes at the declared load, then a burst (all 20 users start a turn within 10 s), then a slow dependency | Handout §1 example; run in #111 |

The traffic **mix** this produces, per active user, is worked out in [section 2](#2-capacity-across-the-whole-request-path). It includes polling and first-time setup, as §1 asks.

### Service-level indicators and objectives, per operation

Each operation is reported on its own, so fast page loads can't hide slow agent turns. Percentiles are computed from Cloud Run's request log, as in HW2's [SLOs](slo.md); time spent **waiting for the student** (to answer a question or approve) is excluded from every agent timing and reported separately.

| Operation | SLI | SLO (launch week) | Why this target |
|---|---|---|---|
| **Ordinary API requests** (course list, course plan, proposal view, connection status, progress polls) | Latency of answered requests | p95 ≤ **500 ms**, p99 ≤ **1.5 s**, warm instances; cold starts counted and reported separately | HW2's warm p95 for a Dropbox-backed read was 397 ms [measured]; these reads mostly hit Postgres, so 500 ms leaves headroom. Cold starts are 3–7 s [measured] and would dominate p99 at our traffic, so they are counted, not hidden |
| **Time to first useful progress** (an agent turn shows its first step, e.g. "Reading CS-GY 6083 syllabus.pdf") | Time from sending the message to the first progress update | p95 ≤ **5 s** | One model call (median 1.33 s, slowest 8.92 s over 15 calls [measured, A1]) plus one tool call (~0.5 s [measured] for Dropbox). A cold agent instance adds ~4.3 s [measured], which this target allows once |
| **Full agent turn** (S1 extraction to a stored proposal) | Time from message to the turn ending (proposal, question or stop) | p95 ≤ **45 s**; every turn ends by **60 s** (the loop's hard limit, `MAX_SECONDS`) | The handout's example target; 3 model calls plus the document read fit well inside it. Turns that hit a limit are counted as failed, not dropped |
| **Execute an approved proposal** (19 writes, each read back) | Time from approve to every change having an outcome | p95 ≤ **30 s** | 38 sequential provider calls at ~0.5 s each is ~19 s [assumed from the measured Dropbox round trip; Trello and Calendar measured in #111] |
| **Unexpected errors** | Unexpected `5xx` and timeouts ÷ eligible requests, per operation | < **1%** | Same target as HW2. Deliberate rejections (`429` over a cap, `503 service_busy`) are reported separately, with how many user tasks they stopped |
| **Correct course setup** | Extracted explicit dates that match the hand-made answer key ÷ explicit dates | ≥ **95%** | The brief's target; measured by assumption test A1 (#85) and the S1 evaluation (#103) |
| **Safety** (must hold, not a percentage) | Provider writes without an approval for that exact hash; duplicate records after a retry; one user reading another's data | **0** in every test and evaluation | DESIGN.md P1–P3 and threats T1–T6; checked by #93, #104 and #108 |
| **Availability** | Requests answered without an unexpected error, over the launch window | **99%** over Dec 2–6 | A 15-minute load test can't establish this; it is measured over launch week itself and reported in Milestone 3 |

**Queued turns.** At the declared load, some turns wait for the model's per-minute allowance (§3). A queued turn counts as served; its time in the queue is reported next to each agent timing, so a long wait is visible rather than hidden or counted as an error.

**Sample sizes.** At 4 agent turns a minute, a 15-minute test produces about 60 turns and 30 executions: enough to check p95, not p99. #111 repeats the test and states the count next to every percentile.

## 2. Capacity across the whole request path

Worked like the handout's example. First the average load, then each dependency against its limit, then bursts and polling.

### Given

- **20 active users**, each setting up one course every 10 minutes → **2 course setups per minute**.
- **One course setup** = 2 agent turns, 1 approval, 1 execution.
- **Model calls per setup: 8.** Turn 1: list the documents, read the syllabus, ask the question (3 calls). Turn 2: read the answer and propose (4 calls). After execution: one summary (1 call). Turn 2 is measured: in a local S1 run with the real model, the proposing turn took **4 model calls, about 11,000 tokens and 8 s**, because `propose_changes` was refused twice (extra fields, then an end equal to the start) and corrected [measured, Shreyas, Nov 26]. Without corrections it is 2 calls, so 8 per setup leaves room for them.
- **Tokens per setup: about 21,000.** Turn 1 is estimated from assumption test A1, which sent a whole course's documents to the model in one call: **1,727 to 4,163 tokens per call, mean 2,617** (15 calls; the largest was the Machine Learning course, two documents, about 10,000 characters) [measured, [A1](https://github.com/Shreyas191/OSPSD-Team-3/pull/118)]. So turn 1 ≈ 8,500 (two calls carrying the documents at ≈ 3,500, one without at ≈ 1,500), turn 2 ≈ **11,000** [measured, above] and the summary ≈ 1,500: about 19,000 input and 2,000 output tokens. A document at the 20,000-character cap adds about 3,500 tokens to each of the 6 calls that carry it, about **42,000**. [estimated; recorded per run by the execution records, #92]
- **Average durations:** an agent turn 20 s, an execution 20 s. [assumed]

### Step 1: arrival rates

- Course setups: 20 users ÷ 10 min = **2 per minute**.
- Agent turns: 2 × 2 = **4 per minute** (0.067 per second).
- Executions: **2 per minute** (0.033 per second).

### Step 2: demand on each dependency

| Dependency | Work per course setup | Demand at 2 setups/min | Limit | Headroom |
|---|---|---|---|---|
| **Gemini** (calls) | 8 calls | **16 calls/min** | Free tier, Gemini 3.5 Flash-Lite: **15 calls/min and 500 calls/day** per project [read in AI Studio for our key's project, Nov 26] | **0.94×: below the declared load.** The tightest constraint by far |
| **Gemini** (tokens) | ~21,000 (42,000 at the document cap) | **~42,000 tokens/min** (84,000 at the cap) | Free tier: **250,000 tokens/min** [read in AI Studio for our key's project, Nov 26] | ~6× (~3× at the cap) |
| **Google Calendar** (Team 5's package, our OAuth project) | 9 creates + 9 read-backs = 18 | **36/min** | 600/min per user, 10,000/min per project [[published](https://developers.google.com/workspace/calendar/api/guides/quota)] | > 15× per user |
| **Trello** (Team 4's package, our API key, each user's token) | 9 creates + 9 read-backs = 18 | **36/min**; one execution sends its 18 calls in ~10 s | 100 requests / 10 s per token, 300 / 10 s per API key, shared by all users [[published](https://developer.atlassian.com/cloud/trello/guides/rest-api/rate-limits/)] | 50× per key on average; a single execution uses 18% of its token's 10-second budget |
| **Dropbox** (ours, each user's token) | list folder, download syllabus, upload reference document, read it back, plus the syllabus upload ≈ 5 | **10/min** | No published number; limits are per user per app, signalled by `429`/`RateLimitError` ([runbook](runbook.md#dropbox-rate-limits-us-503-provider_unavailable-in-bursts)). In HW2, 64 simultaneous requests were not rate-limited [measured, [overload protection](overload-protection.md)] | Ample: each student has their own Dropbox |
| **Neon Postgres** (operations) | ~10 per agent turn (load the conversation, save messages, record tool calls) × 2, + ~45 per execution (19 outcome rows, 19 record rows, status updates), + ~5 for approval ≈ **70** | **~140/min (2.3/s)** | Not a constraint at this rate | — |
| **Neon Postgres** (connections) | — | At most **10**: 2 API instances × (pool 2 + overflow 3) in `course_autopilot_store/database.py`; the agent and interface have no database access | 112 `max_connections` on a 0.25 CU compute; the pooled endpoint accepts 10,000 clients [[published](https://neon.com/docs/connect/connection-pooling#pooler-limits)] | > 10× |
| **Neon** (compute and storage) | — | Awake all launch week at 0.25 CU = 5 days × 24 h × 0.25 = **30 CU-hours**, worst case. Storage: ~20 KB of conversation per course × ~600 courses ≈ **12 MB** | Free plan: 100 CU-hours and 1 GB per project [[published](https://neon.com/docs/introduction/plans)]. All three environments are branches of **one** project, so they share both | 3× compute even if test and demo are equally busy |

Checking each provider separately matters here. The combined provider demand (82 calls/min) looks harmless, but the model, which isn't a provider at all, can't carry the declared load on the free tier without queuing.

### Step 3: work in progress (Little's law)

- Agent turns in progress: 0.067/s × 20 s = **1.3** on average.
- Executions in progress: 0.033/s × 20 s = **0.7** on average.

These are averages, not pool sizes; bursts are in step 5.

### Step 4: the model is the tightest constraint

On the free tier, 15 calls/min ÷ 8 calls per setup = **1.9 setups per minute**, just under the 2 we promise. At the sustained declared load, turns therefore queue (admission control, §3) and the SLOs count a queued turn as served, with its wait reported (§1). Real use in launch week is likely 3–4 people at once: the highest use so far is 7 calls in a minute and 9 in a day [read in AI Studio for our key's project, Nov 26].

The daily allowance is tighter still: 500 calls ÷ 8 = **62 course setups per day**, per project. Two consequences:

- **Test and demo need separate projects.** A 15-minute load test at 16 calls/min uses about 240 calls, half a day's allowance, and evaluation runs (#103–#105) draw on the same allowance. With the `test` and `demo` keys in separate Google Cloud projects, testing can't use up what students need.
- **A per-user daily cap of 25 calls** (about 3 course setups). At 500 a day, that still serves 20 users at the cap; 40 calls each would serve only 12.

Cost on a paid tier, for comparison: ~19,000 input and ~2,000 output tokens per setup (step 1's estimate). At Flash-Lite's list price as last published (about $0.10 per million input and $0.40 per million output tokens [assumed; check the [pricing page](https://ai.google.dev/gemini-api/docs/pricing)]), that is **about $0.003 per course setup**. **Q4 is decided: the free tier**, with the admission control, separate projects and daily cap above (DESIGN.md). It covers the use we expect; the declared 20-user load is served with queuing. If launch week shows sustained queues or the daily allowance running out, enabling billing on the `demo` project is the fallback: at this price, cost isn't the obstacle. On the free tier, prompts may be used to improve Google's products, so the disclosure in DESIGN.md stays.

### Step 5: bursts and polling

**Burst: all 20 users send a message within 10 seconds.** That is 20 turns × 3 calls = **60 model calls within about 30 seconds**: four times the free tier's per-minute allowance. Without admission control, most turns would fail on `429` partway through, each after spending some of the budget. So the agent service needs a limit on turns running at once (below).

**Polling.** The interface checks a running turn's progress every 2 s [assumed, per DESIGN.md P4's HTMX updates]:

- average: 2 turns or executions in progress ÷ 2 s = **1 request/s**;
- burst: 20 waiting users ÷ 2 s = **10 requests/s**.

Add page loads (20 users × one page every 15 s ≈ **1.3/s**) and the agent's own read calls to the API (4 per turn ≈ **0.3/s** average). Peak ordinary API traffic is therefore about **12 requests/s**.

**API service concurrency.** It runs at most 2 instances × 10 requests in flight (`MAX_IN_FLIGHT_REQUESTS`) = **20** concurrent requests before answering `503 service_busy`. Short requests (~0.15 s) at 12/s occupy about 2 of those slots (Little's law). **Executions are the risk:** `POST /proposals/{id}/execute` holds its request for the ~20 s it runs, so if executions counted against those 20 slots, 20 simultaneous approvals would hold all of them and every poll and page load would get `503`. Executions therefore get their own cap, outside `MAX_IN_FLIGHT_REQUESTS` (see the limits below).

**Agent service.** Cloud Run's default of 80 concurrent requests per instance, 2 instances, and a 300 s request timeout are all far above 20 turns of at most 60 s each. One deployment constraint matters, though: both services run with `cpu_idle = true` (`infra/agent.tf`, `infra/run.tf`), so **an instance gets CPU only while a request is open**. A turn or an execution that keeps running after its HTTP response has been sent would be throttled. Long work must either stay inside an open request, or the service must use instance-based CPU. Execution stays inside its request (as #116 does), which is why it can't simply answer `202` and continue in the background; the interface polls the outcome rows while that request is open.

## 3. Limits we set, and why

Each implementation limit is tied to the calculation, measurement or assumption above. Limits in code that isn't written yet are **proposed** here; the owner of each issue confirms or changes the value in their PR and updates this table.

| Limit | Value | Basis | Enforced in | Issue |
|---|---|---|---|---|
| Agent turns running at once, across all users | **3** | Bounds the burst in step 5. On its own it doesn't keep us under 15 calls/min: a turn can make 4 calls in 8 s (step 2's measured turn), so 3 short turns at once can exceed it. Hence the next row | Agent service | #92 / #102 |
| Model calls per minute, per project | **15**, shared by every agent instance (counted in the database); a call over it waits for the next minute instead of failing | Step 4; a `429` midway through a turn would waste the calls already made | Agent service | #102 |
| Turns waiting for a slot | At most **10**, each shown "Queued (position N)"; past that, `429` "The assistant is busy; try again in a minute" with nothing changed | Burst: 20 users, 3 running, the rest queued for one or two turn-lengths | Agent service | #102 |
| Model calls per turn / time / tokens | **8 calls, 60 s, 200,000 tokens** (`MAX_MODEL_CALLS`, `MAX_SECONDS`, `MAX_TOKENS` on `shreyas-agent-loop`) | 3 calls are expected per turn; 8 leaves room for two corrections. 60 s exceeds the 45 s p95 | Agent loop | #92 |
| Model calls per user per day | **25** (≈ 3 course setups) | 500 calls/day ÷ 25 = 20 users at the cap (step 4) | Agent service, from the database | #102 |
| Gemini allowance per environment | `test` and `demo` keys in **separate Google Cloud projects** | A load test uses about half of one project's daily allowance (step 4) | Secret Manager, `gemini-api-key-<env>` | #102 |
| Executions at once, per user | **1**; a second approve-and-execute waits for the first | One user's 18 Trello calls per execution against 100 per 10 s per token; avoids interleaved partial failures | API service | #90 |
| Executions across the service | Inside their own request, **not** counted in `MAX_IN_FLIGHT_REQUESTS`; capped separately at a few across the service (value set in #90) | Step 5: keeps the 20 slots for polls and page loads, and keeps CPU while executing under `cpu_idle` | API service | #90 |
| Syllabus text per read | **20,000 characters** | ≈ 6,500 tokens at the cap; keeps a setup at or below about 42,000 tokens (≈ 21,000 for the syllabi in A1) | API service (`read_document`) | #88 |
| Upload size | **10 MB** per document | A syllabus PDF is well under 1 MB [assumed]; bounds memory on a 512 MiB instance | API service | #88 |
| API requests in flight | **10 per instance**, 2 instances (unchanged from HW2) | 12 requests/s peak × 0.15 s ≈ 2 in flight; executions are capped separately | API service, `infra/variables.tf` | — |

## 4. What happens when each limit is reached

| Limit reached | What the student sees | What is left |
|---|---|---|
| Model calls per minute (our 15/min counter, or Gemini's `429` if it is reached anyway) | "Queued (position N)" while the turn waits; "The assistant is busy; try again in a minute" if Gemini still refuses | The conversation is saved; nothing was proposed or written |
| Turn queue full | The same message, immediately | Nothing |
| Daily model cap | "Today's limit for the assistant has been reached; your course plans and approvals still work" | Reads, approvals and executions keep working; only new turns stop |
| Turn limit (calls, time or tokens) | The loop's stop message naming which limit; nothing was changed | The conversation and any question asked so far |
| API `503 service_busy` | "Busy, retrying…", then an error with a retry button | Nothing done; retrying is safe |
| Trello `429` during an execution | That change `failed`, with *Retry failed changes* | Completed changes stay (P3) |
| Neon compute or storage allowance used up | The service's database error page | Unchanged; requires the paid plan or deleting old branches |

## 5. Failures, and how we detect them

The [failure analysis](https://github.com/Shreyas191/OSPSD-Team-3/pull/119) (#112) records, for each significant failure, what students can still do, what becomes uncertain, how we detect it and how work recovers. It ran ten failures through S1 end to end. What it means for launch:

- **Before approval nothing can be harmed.** Gemini, API, Neon and Dropbox failures all stop the turn with a fixed message and change nothing in a student's accounts.
- **During execution, P3 mostly holds.** Completed changes are never repeated, and a write whose reply was lost is marked `uncertain` and never retried.
- **Two gaps break P3 and should be fixed before launch week (#90).** First, a proposal whose request ends mid-execution (redeploy, crash, timeout) stays `executing` forever with an unrecorded write. Second, an `uncertain` change can never be resolved, so its proposal never reaches `done`.
- **Detection.** An agent dashboard (task success, errors, full-turn latency against the 45 s objective, active turns, model calls and tokens per turn) and an alert on turns that fail because the model or API is unavailable. A failed turn still answers `200`, so the existing 5xx alert can't see it. Both are defined in `infra/agent_monitoring.tf` and are applied once the agent loop is deployed; the alert's drill is in the runbook.

## Measurements so far

| When (UTC) | What | Environment | Result | Sample |
|---|---|---|---|---|
| Nov 11, 2026 | `GET /files/metadata` (a Dropbox-backed read), from Cloud Run's request log | test | warm p50 141 ms, p95 397 ms; cold starts 3,070–6,701 ms | 74 warm requests, 12 cold ([SLOs](slo.md)) |
| Nov 11, 2026 | 64 simultaneous requests to Dropbox through the API | test | no Dropbox rate limiting | one run ([overload protection](overload-protection.md)) |
| Nov 26, 20:47 | `GET /health` on the agent service, from a laptop (includes the network) | test (`course-agent-test`) | first request **4.35 s** (cold start); the next 10: 0.12–0.19 s | 11 requests |
| Nov 26, 20:47 | `GET /health` on the API service, from a laptop | test | 0.10–0.19 s (already warm) | 11 requests |
| Nov 26, 20:48 | `GET /health/provider` (one Dropbox round trip through the API), from a laptop | test | 0.46–0.69 s, median 0.51 s | 11 requests |
| Nov 26 | One Gemini call reading a whole course's documents (`gemini-3.5-flash-lite`, free tier), [assumption test A1](https://github.com/Shreyas191/OSPSD-Team-3/pull/118) | laptop → Gemini API | 1,727–4,163 tokens per call (mean 2,617); 0.95–8.92 s, median 1.33 s | 15 calls, 5 courses |
| Nov 26, 23:24 | `GET /health/database` (one Neon query through the API) after 7 idle minutes, instance already warm | test | first query **1.03 s**, then 0.25–0.29 s | 3 requests ([failure analysis](https://github.com/Shreyas191/OSPSD-Team-3/pull/119)) |
| Nov 26 | One S1 proposing turn with the real model (`shreyas-agent-loop`, local), by Shreyas | laptop → Gemini API | **4 model calls, ~11,000 tokens, 8 s**; `propose_changes` refused twice (extra fields, then end equal to start) and corrected | 1 turn |
| Nov 26 | Gemini free-tier limits and use for our key's project, read in AI Studio by Shreyas | — | 15 requests/min, 250,000 tokens/min, 500 requests/day; peak use 7/min, 9/day | — |

These are small samples from one place. They set the starting assumptions above; they don't establish the SLOs.

## What this record does not establish yet

- **Model latency and tokens per turn.** A1 measured single extraction calls, and one proposing turn has been measured (above). Turns in general are still estimated until the agent loop records every run (#92) and the S1 evaluation runs five times (#103).
- **Trello and Calendar write latency** through Teams 4's and 5's packages. Measured in the load test (#111).
- **Whether the SLOs hold under load.** That is the load test (#111), which runs the workload in section 1 and links its report here.

Still to come in this record: the load test (#111); the alert drill (#112, once the agent loop is deployed); rollout, game day and backup restore (#113, plan in [PR #120](https://github.com/Shreyas191/OSPSD-Team-3/pull/120)); and the launch decision (#114).
