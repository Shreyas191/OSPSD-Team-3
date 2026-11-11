# Error Budget Policy

This document defines Team 3's Error Budget Policy for HW2 Milestone 3, Level 4 (item 3.6, rubric item 16.4). It sets the rules governing releases and operational priorities when our reliability promises are threatened or violated.

The policy applies to the **demo** deployment (`cloud-storage-service-demo`), which is the persistent environment evaluated by outside consumers and course staff.

## 1. Context and Purpose

As defined in [Service Level Objectives](slo.md), Team 3 promises:
- **Availability SLO:** 99.0% good requests over a rolling 7-day window (permits 10 failures per 1,000 eligible requests).
- **Latency SLO:** 95.0% of good requests served within 1,000 ms over a rolling 7-day window (permits 50 slow requests per 1,000 eligible requests).

The error budget is a shared contract between product velocity and system reliability. It specifies exactly how much unreliability consumers can tolerate before shipping new features becomes irresponsible.

## 2. Budget Health Thresholds

Error budget consumption is measured through `scripts/slo_report.py`. The team assesses budget status during PR reviews and regular checks.

| Remaining Budget | Status | Operational Posture & Release Rules |
|---|---|---|
| **> 50% remaining** | **Healthy (Green)** | Normal operations. Feature development, dependency upgrades, and routine releases proceed normally through the CI/CD pipeline. |
| **25% – 50% remaining** | **Caution (Yellow)** | Heightened scrutiny. New deployments require review from at least two teammates. The team inspects Google Cloud Logging to identify emerging error patterns or latency degradation. |
| **0% – 25% remaining** | **Warning (Orange)** | High risk. Feature releases are restricted to low-risk, verified PRs. Non-essential infrastructure changes are deferred. Engineering investigates all 5xx logs and traces. |
| **≤ 0% (Budget Exhausted)** | **Breach (Red)** | **Immediate feature freeze.** All deployments of new features or cosmetic refactors to `main` and `demo` are blocked. Engineering effort shifts 100% to reliability and root-cause mitigation. |

## 3. Mandatory Steps When Budget is Burned (≤ 0% Remaining)

When an error budget is burned (availability < 99% or latency < 95% over the window, or rapid burn exhausting the remaining budget):

### Step 1: Immediate Release Freeze
- All non-essential merges to `main` and deployments to `cloud-storage-service-demo` are suspended.
- Only changes that directly mitigate the failure, fix bugs, or improve system resiliency may be merged and deployed.

### Step 2: Triage and Rapid Mitigation
The on-duty engineer immediately consults the [Operational Runbook](runbook.md):
- If caused by a faulty deployment: roll back traffic to the previous known-good revision (`gcloud run services update-traffic --to-tags=...`).
- If caused by expired or invalid credentials: rotate the Dropbox refresh token in Google Secret Manager and trigger a new revision.
- If caused by Dropbox provider outages: verify provider status at <https://status.dropbox.com> and report external degradation to affected vertical consumers.
- If caused by service concurrency saturation: verify whether `MAX_IN_FLIGHT_REQUESTS` needs adjustment or abusive callers require throttling.

### Step 3: Blameless Postmortem and Corrective Actions
Within 48 hours of budget exhaustion, the team must complete a blameless postmortem documenting:
- Root cause and contributing triggers.
- Total downtime, error counts, and impact on consumers.
- What helped recovery and what hindered triage.
- Action items with explicit student owners and target completion dates.

### Step 4: Verification and Freeze Lifting
The feature freeze is lifted and normal deployments resume only when all of the following conditions are satisfied:
1. The root-cause fix is deployed and verified via the post-deployment smoke check (`scripts/consumer_workflow.py`).
2. The error condition is fully resolved in Cloud Monitoring.
3. The remaining budget recovers to at least 25% over the 7-day rolling window, or the service demonstrates at least 48 consecutive hours of zero error-budget burn under representative traffic.
4. All immediate preventative action items (such as missing conformance checks or regression unit tests) are implemented and merged.
