# Synthetic Probe

This page covers the HW2 optional extension **External synthetic probe**: a request made from outside the service on a schedule, reported as its own SLI, and the cases where it disagrees with the server-side SLI in [Service Level Objectives](slo.md).

## The contract and its limits

**What is probed.** Every 5 minutes, Cloud Monitoring uptime checkers call, on each environment's public URL:

```
GET /files/metadata?path=/synthetic-probe/absent
```

- **It is the operation the SLO covers** (`GET /files/metadata`), so the two SLIs measure the same thing from two places.
- **It is safe.** It is read-only, and the path never exists, so no file has to be kept in Dropbox for it. It still makes the Dropbox call (`files_get_metadata`), so a Dropbox outage or a rejected credential fails it.
- **A run passes only on `404` and `error.code == "not_found"` in the body, within 10 s.** The status alone isn't enough: a placeholder page that answers `200` to everything fails, and so does a `404` from anything other than our service (see the first two results below).

**From where.** Three checkers, chosen outside the service's region (`us-central1` is Iowa), so each request crosses the public internet to Google's front end the way a consumer's does: **Oregon, Virginia and Belgium**. Each runs once per period, so there are about 3 runs every 5 minutes per environment.

**Defined in** [`infra/probe.tf`](https://github.com/Shreyas191/OSPSD-Team-3/blob/hw2/infra/probe.tf), one check per environment. It costs nothing: about 52,000 runs a month for both environments, against Cloud Monitoring's free allotment of 1 million uptime-check runs per project.

## The probe SLI

| | |
|---|---|
| **Eligible** | Every probe run in the window, from every checker |
| **Good** | `404` with `error.code == "not_found"` within 10 s |
| **SLI** | good / eligible |
| **Latency** | Of the good runs, the share answered within 1,000 ms at the checker |

The targets and window are the server-side SLO's (99% available, 95% within 1,000 ms, 7 days), so the two can be compared directly. The probe makes about 6,000 runs a week per environment, so its SLI is never "low traffic", even on a service nobody uses.

**Reporting:**

```bash
uv run python scripts/probe_report.py --project <project-id> --service cloud-storage-service-demo --days 7
```

[`scripts/probe_report.py`](https://github.com/Shreyas191/OSPSD-Team-3/blob/hw2/scripts/probe_report.py) reads two sources for the same requests and prints counts only:

- **the probe's view:** each run's result and latency (Cloud Monitoring's `uptime_check/check_passed` and `uptime_check/request_latency`), per checker;
- **the server's view:** the probe's own requests in Cloud Run's request log, the source of the server-side SLI (user agent `GoogleStackdriverMonitoring-UptimeChecks`).

Exit status 0 means the probe SLO is met, 1 not met, 2 no data. Cloud Monitoring repeats each run's result every 10 s until the next run, so the script groups those points back into runs; counting raw points would count each run about 30 times.

The checks' own charts are in the console under **Monitoring → Uptime checks**.

## Where the two SLIs disagree

All observed on Nov 11, 2026, between 22:34 and 23:02 UTC. The probe was created at 22:34:31.

| Case | Probe SLI | Server-side SLI | Why they differ |
|---|---|---|---|
| 1. Demo runs a placeholder | **0%** (15 of 15 runs failed) | **100%** (15 of 15 requests answered `200`) | The answer was wrong, not the status |
| 2. Requests blocked before the service | **0%** (10 of 10 runs failed) | **No data** (0 requests logged) | The requests never reached Cloud Run |
| 3. Slow outside the service | One run slow (1,059 ms) | The same request fast (921 ms) | Time on the network to Belgium |
| 4. Slow after a change | One run took **6,008 ms** | The same request took **365 ms** | Time before Cloud Run logged the request |
| Normal operation (test, 22:34 to 22:44) | 5 of 5 passed | 5 of 5 `404` | They agree: every run has a matching log entry |

### 1. Demo answers `200`, but with the wrong thing

Demo still runs Cloud Run's placeholder image (the pipeline hasn't deployed our service there yet; see [SLO, which environment](slo.md#which-environment-the-slos-cover)). It answers **every** URL with `200` and an HTML "It's running!" page.

- Cloud Run logged all 15 probe requests as `200`. `scripts/slo_report.py` for demo over the same window: **availability 15 / 15 = 100%, latency 100%**. By its definition (`200` or `404` is good), a service that doesn't exist is perfect.
- The probe failed all 15 runs, from all three checkers: `200` is not `404`, and the body has no `error.code`.

**Why:** the server-side SLI judges the status code alone, because it reads the request log, which has no response body. The probe reads the body. This is the gap the SLO page names under "Wrong answers with a correct status".

### 2. Requests that fail before reaching the service

**The experiment** (agreed beforehand; test only, about 16 minutes):

1. 22:44:29 UTC: `gcloud run services update cloud-storage-service-test --ingress=internal`, so Google's front end refuses requests from the internet before they reach Cloud Run. From outside, the service answered `404` with an empty body.
2. The probe ran unchanged for 16 minutes.
3. 23:00:57 UTC: `--ingress=all` restored public access. The first request after it got the normal `404 not_found` body, and `tofu plan` reported **No changes**.

**Result** (`probe_report.py` and `slo_report.py`, window 22:44:29 to 23:00:57):

- **Probe: 0 / 10 runs passed** (Oregon 4, Virginia 3, Belgium 3). The front end's `404` has the status the check accepts, but not the body, so the body check is what caught it.
- **Server side: 0 requests logged.** Cloud Run's request log has no entry for test in the window, so the server-side SLI reports **NO DATA**. Our own request log lines, the dashboard's traffic charts and the 5xx alert saw nothing either. They were blind to a complete outage for consumers.
- `probe_report.py` reports it directly: **"executions with no matching log entry: 10"**.

**Why:** every server-side measurement starts when Cloud Run receives the request. A request refused at Google's front end, or lost to DNS, TLS or the caller's network, never gets that far. Only a caller outside the service sees it.

**A side effect, and what it showed.** PR #59 merged during the block. The pipeline deployed it as a no-traffic revision (`rel-4e4aef6`), its smoke check couldn't reach test from outside, and `deploy_test` failed at 23:00:41, so **traffic stayed on the previous release** (`rel-7d6b6ff`). The release gate did its job, and the job had to be re-run once access was back. Next time, block the window for merges, or run the experiment on a tagged revision.

### 3. Network time: the probe's latency includes it

At 22:41:18 the Belgium checker measured **1,059 ms**, over the 1,000 ms threshold. Cloud Run logged the same request at **921 ms**, under it. So the probe SLI counts it slow and the server-side SLI counts it fast. The ~140 ms difference is the round trip between Belgium and `us-central1`, which a European consumer also pays. The passing Oregon and Virginia runs, closer to the service, took 170 to 270 ms.

### 4. Time before Cloud Run's clock starts

At 23:01:18, the first Belgium run after access was restored took **6,008 ms** at the checker. Cloud Run logged the same request at **365 ms**, and a new instance had started two seconds earlier (`Starting new instance. Reason: AUTOSCALING`, 23:01:16). About 5.6 s were spent before Cloud Run started measuring the request. The cause isn't established by this data: it came right after the ingress change and the instance start. What it shows is that the server-side latency can't see that time; only the probe can.

## What this establishes, and what it doesn't

- **It establishes** that the probe detects failures the server-side SLI cannot: requests that never reach Cloud Run (case 2), wrong answers with a "good" status (case 1), and time outside Cloud Run (cases 3 and 4). It also establishes the reverse in normal operation: every probe run had a matching Cloud Run log entry with the same status.
- **It is still Google's network.** The checkers run on Google infrastructure, so a failure specific to one consumer's ISP or DNS resolver isn't seen; neither is a Google-wide front-end outage that also takes out the checkers.
- **One operation, one answer.** The probe checks one read of a missing path. It says nothing about uploads, downloads, listings or deletes, and nothing about whether metadata of a file that exists is correct.
- **The probe sends traffic of its own.** About 864 requests a day per environment reach the service and Dropbox, all answered `404`, which the server-side SLI counts as good. On demo, with little real traffic, they would dominate that SLI. `slo_report.py` should leave them out (user agent `GoogleStackdriverMonitoring-UptimeChecks`); that is a follow-up for the SLO owner.
- **No alert on the probe yet.** It is reported as an SLI. A probe-based alert is what would have paged someone during case 2, when the 5xx alert stayed silent; that is the obvious next step, and a decision for the team.
- Uptime checks, their scheduling and their metrics are Google's. What we built is the choice of request, the pass rule, the SLI definition, the report that joins the probe with the server's log, and the experiments above.
