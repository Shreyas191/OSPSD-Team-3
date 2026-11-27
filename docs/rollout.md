# Rollout, Game Day and Backup Restore

HW3 Milestone 2, Level 5 (#113; task doc 7.5 and 7.7). This page plans how Course Autopilot reaches its users and how we take it back when something goes wrong, then records the drills that check those plans. The launch it serves and its objectives are in the [readiness record](readiness.md); the failures it prepares for are in the [failure analysis](failure-analysis.md).

**Status (Nov 27):** plan written and reviewed by Shreyas ([#120](https://github.com/Shreyas191/OSPSD-Team-3/pull/120)). The stage dates, the sister team, the daily backup and the execution switch are agreed. The drills in sections 4 to 6 have not run yet; each says what it needs first. The coverage days (section 9) are for the team to confirm.

## 1. What a release is

A release is one commit on `hw2`, built once into one image and deployed by digest to all three services of an environment: the API, the agent and the interface ([Release Pipeline](release-pipeline.md)). Everything that changes behaviour is versioned with it, or recorded next to it:

| Part | Where it lives | Versioned by |
|---|---|---|
| Code, the agent's prompt and its tool contracts | the image (`course_agent_service/loop.py`, `tools.py`) | the commit |
| Database schema | migrations in `course_autopilot_store`, applied by the API service when it starts | the commit; the database records the applied revision |
| Model | fixed model version in the agent's configuration, recorded in every run record | the commit, and each run's record |
| Configuration (limits, `MODEL_PROVIDER`, service settings) | `infra/` (OpenTofu) | the commit that changed it; applied by hand |
| Secrets (database URL, model key, provider tokens) | Secret Manager | secret versions; values never in git |

**Rules this implies:**

- **Every migration must work with the previous release.** The candidate revision migrates the database while the previous one is still serving, and a rollback restores code, not the schema. So a release may add tables and nullable columns, but removing or renaming one takes two releases: stop using it, then drop it.
- **The API must keep serving the previous agent and interface.** Each deploy job releases the API service first, then the agent, then the interface. If a later check fails, the services before it are one release ahead until the next deploy. Likewise the agent must keep serving the previous interface.
- **Configuration and secrets are not rolled back with the code.** A rollback after a configuration change needs that change reverted too.

## 2. Rollout stages

Launch week starts on Dec 2 for every team at once, so the stages before it must finish by Dec 1.

| Stage | Who | Where, when | Go on when | Stop and roll back when |
|---|---|---|---|---|
| 0. Every merge | Pipeline | `test`, on each merge to `hw2` | Smoke check passes on both services | The smoke check fails: the pipeline keeps the old revision |
| 1. Our team | The five of us, each with our own Google, Dropbox and Trello accounts | `demo`, Nov 30 | All five complete S1 with no help; every date correct; no stuck proposal; no alert | Any safety violation (a change without approval, another user's data), or a wrong date we can't explain |
| 2. One sister team | Two people from Team 5, who already use our storage package | `demo`, Dec 1 | S1 complete for both, without coaching (task 5.10); feedback filed as issues | As stage 1 |
| 3. Everyone | Assigned teams and staff | `demo`, Dec 2–6 | Not a gate: we watch the measures below | As stage 1, or a measure stays past its threshold for 30 minutes |

Stage 1 needs the proposals and execution (#116), the agent loop, sign-in, provider connections, the OAuth clients and the S1 pages (#95) all on `demo` by Nov 30. **If that slips, both stages run on Dec 1:** our team in the morning, Team 5 in the afternoon.

**What we watch** (objectives from the readiness record; agent dashboard and alert from #112):

| Measure | Healthy | Act when |
|---|---|---|
| Task success: turns ending `replied` or `proposal_ready` | most turns | the agent alert opens (more than 3 failed turns in 5 minutes) |
| Full-turn latency p95 | ≤ 45 s | above 45 s for 30 minutes |
| API errors (5xx) | < 1% | the 5xx alert opens |
| Execution | no change left `uncertain`, no proposal silent in `executing` | any; reconcile it with the student (section 8) |
| Safety | 0 changes without approval, 0 cross-user reads | any, at once |
| Gemini daily use | below the free-tier allowance | above 80% of it before the evening: lower the per-user daily cap (#102) for the rest of the day |

**Stop actions, fastest first:**

1. **Turn the assistant off** when it misbehaves (wrong proposals, a prompt injection that gets through): set `MODEL_PROVIDER=disabled` on the agent service. Turns answer `503 assistant_disabled`; students can still see their plans, approve and execute, because those go to the API. It takes effect with the next revision (about a minute):

    ```bash
    gcloud run services update course-agent-demo --region=us-central1 --update-env-vars=MODEL_PROVIDER=disabled
    ```

    `tofu apply` would turn it back on, so to keep it off, set `agent_model_provider = { demo = "disabled" }` too.
2. **Stop executions** when the fault is in execution (wrong writes, or writes failing): set `EXECUTION=disabled` on the API service and move traffic to the new revision, as [Pausing executions](deployment.md#pausing-executions) shows (setting the variable alone creates a revision that serves nothing). Execute then answers `503`, while proposals and approvals are still recorded, so students can review and approve and run them later. This avoids rolling back a release that is otherwise fine. Shreyas builds the switch with #102. Until then, the only lever is rolling back the API.
3. **Roll back a release** (section 4) when errors start with a new revision.
4. **Restore data** (section 6) when a release or a bug damaged the database. Neon can restore the last 6 hours; the daily backup covers older damage.

## 3. Production smoke check

What the pipeline checks before a revision gets traffic today, and what #113 asks for:

| Check | API service | Agent service | Interface |
|---|---|---|---|
| Liveness (`/health`) | yes | yes (the only check today) | yes |
| Our page, naming the release being deployed (`GET /`) | n/a | n/a | yes |
| Database reached and migrated (`/health/database`) | yes | n/a | n/a |
| Provider access | Dropbox only, with our own account (`/health/provider`) | n/a | n/a |
| A storage round trip (`scripts/consumer_workflow.py`) | yes | n/a | n/a |
| Sign-in | **missing**: needs #96 | **missing** | **missing** |
| S1 | **missing** | **missing** | **missing** |

**Plan, once sign-in (#96) and provider connections (#98–#101) exist,** the same on `test` and `demo`. There is no shortcut sign-in, since dev sign-in is never on `demo`:

- **A smoke user** with its own Google, Dropbox and Trello test accounts signs in once through the interface, the real way.
- **Its session token** goes into Secret Manager as `smoke-session-<env>`, readable only by the deployer.
- **A session ends after 24 hours idle or 7 days in all.** So whoever is on duty keeps it alive with one call at the start of the day (`GET /auth/me` with it). That step goes into the runbook along with the smoke check.
- **On Dec 1,** someone signs in as the smoke user again and reconnects its Calendar, which covers launch week. The Google OAuth app is in Testing mode, where refresh tokens expire after 7 days, so the Calendar connection lapses weekly too.

After each deploy, before traffic moves, the smoke check:

1. checks that the stored session is accepted (`GET /auth/me`), and that a request without one is refused (`401`). It never signs out, which would end the stored session;
2. checks each provider connection reads as `connected`;
3. sends one turn, "List my courses", and expects `replied` naming the smoke course. That is one or two model calls per deploy, which matters at 500 a day;
4. proposes and executes one calendar event in the smoke user's calendar, checks the read-back, then removes it.

**An expired smoke session, or a Calendar connection reading `needs_attention`, is a warning, not a failure.** The check skips the steps that need it and prints what to renew. So a quiet weekend can't block a fix being released. Every other result fails the check.

Steps 3 and 4 cover S1's path end to end without a full setup's model cost. Until then, S1 is checked by hand in stage 1.

## 4. Release rollback

Each service rolls back on its own: every release is a tagged revision (`rel-<sha>`) on all three.

```bash
# Which releases are there, and which serves?
gcloud run services describe course-agent-demo --region=us-central1 \
  --format="table(status.traffic[].tag,status.traffic[].percent)"
# Send traffic back to the previous good release (interface, agent, API, or several).
gcloud run services update-traffic course-web-demo --region=us-central1 --to-tags=rel-<good sha>=100
gcloud run services update-traffic course-agent-demo --region=us-central1 --to-tags=rel-<good sha>=100
gcloud run services update-traffic cloud-storage-service-demo --region=us-central1 --to-tags=rel-<good sha>=100
```

Then run the smoke check against the service URL. Roll back the service whose errors started with the new revision. When rolling back more than one, go in reverse deploy order: interface, then agent, then API. That way, at every step, each service only calls services of its own release or newer, and section 1 requires those to keep serving it. If the new release included a migration, check that the old code works with the new schema (section 1) before rolling the API back.

**What rollback can't undo:** the schema, configuration and secrets; and changes a faulty release already made in the database or in students' Dropbox, Trello or Calendar. Database changes can be restored within 6 hours (section 6); provider changes have to be found through the proposals' outcomes and removed with the student.

**Drill (pending):** repeat the HW2 [rollback drill](rollback-drill.md) for the agent service: a deliberately broken agent release deployed with no traffic must fail its check, and a rollback must restore turns. It needs an account with deploy rights on the project and the agent loop deployed (#92).

## 5. Game day (pending)

A planned failure on `test`, recovered by a teammate who didn't write the runbook, using only the runbook and the dashboards.

- **When:** Nov 30, after the agent loop (#92) is deployed and the agent alert (#119) is applied.
- **Who:** John injects the failure and observes; the responder is a teammate who didn't write the runbook (Zesan, Jing or Victor). The responder isn't told the cause.
- **The failure:** the model key is rejected on the agent service. It is a realistic failure (a revoked key, or the project's billing changing), it is recoverable without touching data, and it fires the agent alert, so the same exercise is the alert's test from firing to clearing. It is injected with the [agent alert drill](runbook.md#agent-alert-drill) steps, on a revision that serves the test service.
- **What we record:** time to detect (alert email), time to diagnose (cause named), time to recover (turns succeed again), every place the runbook was unclear or wrong, and the fix for each.

## 6. Backup restore (pending)

**What Neon keeps on our plan** (Neon docs, read Nov 27, 2026): the free plan can restore a branch to any point in its **last 6 hours**, up to 1 GB of changes; a project may have **10 branches**; computes suspend after **5 minutes** idle and can't be kept on.

**What that means:** damage noticed within 6 hours can be undone from Neon; damage noticed the next morning can't. During launch week that is a real gap.

**Decided: a daily `pg_dump` of the `demo` database, automated.** It is to be in place by Dec 2 (#113).
- **How:** a scheduled Cloud Run job, about 40 lines of `infra/`, writes it to a private Cloud Storage bucket that deletes dumps after 14 days. It is small, from kilobytes to a few megabytes, and makes recovery possible beyond 6 hours.
- **The bucket stays private.** The dump holds students' conversation text.
- **No key rotation during launch week.** Provider tokens in the dump stay encrypted, but a restore can only read them with the same `token-encryption-key-demo`.

Restoring a dump goes into a new Neon branch first, as in the drill below.

**The drill**, restoring into a throwaway branch so production is never touched:

1. Note the time `T` (UTC) and count the rows of a few tables on the production branch (`users`, `proposals`, `change_outcomes`).
2. Create a branch `restore-drill` in the same project.
3. Restore `restore-drill` from the production branch's history at `T`: in the console, open `restore-drill` → Backup & Restore → Restore from history, with the production branch as the source; or `neon branches restore restore-drill <production branch>@<T>`.
4. Connect to `restore-drill` and compare the row counts with step 1. Note how long the restore took.
5. Delete `restore-drill` and the `restore-drill_old_…` branch the restore leaves behind (the plan allows 10 branches).

It needs access to the team's Neon project (Shreyas owns it).

## 7. Data loss and recovery time

How much work a failure can lose, and how long until students can use the product again. The times are targets: the measured ones come from HW2's rollback drill; the others are checked by the drills above.

| Failure | Data lost | Back in service within | Basis |
|---|---|---|---|
| Bad release, caught by the smoke check | none | 0: it never gets traffic | the pipeline's gate (HW2 [rollback drill](rollback-drill.md), evidence 1) |
| Bad release that reached users | none in our database; changes it made are kept, and those in students' accounts need finding | **5 minutes** from deciding to roll back: the traffic switch took 4 s, then the smoke check | measured in HW2 for the traffic switch; the rest is assumed |
| Assistant misbehaving | none | **5 minutes**: one setting, one new revision | assumed; the game day measures a similar change |
| Database damaged (a bad migration, a bug that overwrites rows), **noticed within 6 hours** | none: restore to just before the damage | **1 hour**: find the time, restore, check | assumed until the restore drill (section 6) |
| The same, **noticed later** | everything since the last daily backup, up to **24 hours** | **2 hours** to restore from the dump | assumed until the daily dump exists and one is restored |
| Neon or Gemini down | none; turns and executions in progress stop with a fixed message (failure analysis) | when the provider is back | not in our control |

**What a database loss costs students:** their courses, conversations, proposals and outcomes live in our database. The events, cards and documents we made live in their own accounts, so those survive. But a lost `external_records` row means we no longer know a record was ours, and later changes to it are refused as "not created by Course Autopilot". The student can still edit or delete it themselves.

## 8. Reconciling partial external actions

An execution can end with some changes made and others not. With execution as in [#116](https://github.com/Shreyas191/OSPSD-Team-3/pull/116) (P3), each change is saved as `started` just before its write and gets its outcome after it. So the proposal itself says what happened, and nobody edits its status by hand:

| Outcome | Meaning | What we do |
|---|---|---|
| `completed` | made and read back | nothing; it is never made again |
| `failed` | not made (refused before writing) | the student runs the proposal again; only `failed` and not-yet-run changes run |
| `uncertain` | the write may have happened, but its reply was lost or the read-back differed | never retried automatically. The student checks their calendar or board and settles it on the proposal's page (`POST /proposals/{id}/changes/{index}/resolve`): "it's there", giving the event's or card's ID, which is read back and recorded as ours; or "it isn't", which lets the next run make it |
| `started`, proposal still `executing` | the run stopped mid-change (a redeploy, a crash, a timeout) | once the proposal has been silent for 10 minutes (`proposals.heartbeat_at`), running it again takes it over: the `started` change becomes `uncertain`, and the rest run. The student settles the uncertain one as above |

**Once a day during launch week**, the on-duty person runs these read-only queries. They follow up on anything found with its student, who either runs the silent proposal again or settles the uncertain change:

```sql
-- Proposals whose run stopped and that nobody has run again
SELECT id, created_at, heartbeat_at FROM proposals
WHERE status = 'executing' AND heartbeat_at < now() - interval '10 minutes';
-- Changes waiting for the student to settle
SELECT proposal_id, change_index, recorded_at FROM change_outcomes WHERE status = 'uncertain';
```

The queries use a read-only database role (section 9). Each run also writes one `proposal_execution` log line (`final_status` and the count of completed, failed and uncertain changes). A dashboard panel and an alert on uncertain changes come from that line (#112), and once they exist they show partial executions as they happen.

## 9. Launch-week coverage, escalation and support

**Proposed for the team to confirm:**

| Day (Dec) | On duty | Backup |
|---|---|---|
| Tue 2 | John | Shreyas |
| Wed 3 | Shreyas | Zesan |
| Thu 4 | Zesan | Jing |
| Fri 5 | Jing | Victor |
| Sat 6 | Victor | John |

- **Hours:** 9:00–21:00 Eastern. Outside them, alerts still email the team and are handled the next morning. If something unsafe happens overnight, whoever sees it first turns the assistant off (section 2), which needs no other decision.
- **The on-duty person:**
  - checks the agent and API dashboards at the start, middle and end of the day, and Gemini's daily use in AI Studio;
  - handles alert emails within 30 minutes during hours, using the runbook;
  - renews the smoke user's session (section 3);
  - runs the reconciliation queries (section 8);
  - answers support requests within 2 hours.
- **Database access for whoever is on duty:** today only Shreyas can reach the `demo` database.
  - Before Dec 2, he creates a **read-only Neon role** that can only `SELECT` the product tables.
  - Its connection string goes into Secret Manager as `database-readonly-url-demo`, readable by the five of us.
  - That is enough for section 8's queries. Nobody on duty gets write access to the database.
- **Escalation**, in this order:
  1. the day's backup;
  2. **Shreyas or John** for anything that needs owner access: the Google Cloud project, Neon, the Gemini key's project, or Secret Manager. Shreyas gives John owner access for launch week, so escalation doesn't run through one person;
  3. **course staff** for a safety violation that affected another team's users (a change without approval, or one user reading another's data). We tell them the same day, after turning the assistant off.
- **Support channel:** a GitHub issue in this repository with a `launch-support` label, linked from the interface's footer, so every report has a record and an owner.
  - **Issues are public.** So the issue template says not to include personal details: no email addresses, grades, syllabus contents, or screenshots of a calendar or board.
  - **Private reports**, and students without a GitHub account, use the team's contact address. The template and the interface both list it.
  - The label, the template and the footer link are to be added with #95.

## 10. Results

| Drill | Date | Result | Findings and fixes |
|---|---|---|---|
| Agent release rollback | pending | | |
| Game day (model key rejected) | pending | | |
| Neon restore into a throwaway branch | pending | | |
| Stage 1: our team on `demo` | pending | | |
| Stage 2: sister team on `demo` | pending | | |
