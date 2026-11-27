# Rollout, Game Day and Backup Restore

HW3 Milestone 2, Level 5 (#113; task doc 7.5 and 7.7). This page plans how Course Autopilot reaches its users and how we take it back when something goes wrong, then records the drills that check those plans. The launch it serves and its objectives are in the [readiness record](https://github.com/Shreyas191/OSPSD-Team-3/pull/117); the failures it prepares for are in the [failure analysis](https://github.com/Shreyas191/OSPSD-Team-3/pull/119).

**Status (Nov 27):** plan written. The drills in sections 5 to 7 have not run yet; each says what it needs first.

## 1. What a release is

A release is one commit on `hw2`, built once into one image and deployed by digest to both services of an environment ([Release Pipeline](release-pipeline.md)). Everything that changes behaviour is versioned with it, or recorded next to it:

| Part | Where it lives | Versioned by |
|---|---|---|
| Code, the agent's prompt and its tool contracts | the image (`course_agent_service/loop.py`, `tools.py`) | the commit |
| Database schema | migrations in `course_autopilot_store`, applied by the API service when it starts | the commit; the database records the applied revision |
| Model | fixed model version in the agent's configuration, recorded in every run record | the commit, and each run's record |
| Configuration (limits, `MODEL_PROVIDER`, service settings) | `infra/` (OpenTofu) | the commit that changed it; applied by hand |
| Secrets (database URL, model key, provider tokens) | Secret Manager | secret versions; values never in git |

**Rules this implies:**

- **Every migration must work with the previous release.** The candidate revision migrates the database while the previous one is still serving, and a rollback restores code, not the schema. So a release may add tables and nullable columns, but removing or renaming one takes two releases: stop using it, then drop it.
- **The API must keep serving the previous agent.** Each deploy job releases the API service first, then the agent. If the agent's check fails, the API is one release ahead until the next deploy.
- **Configuration and secrets are not rolled back with the code.** A rollback after a configuration change needs that change reverted too.

## 2. Rollout stages

Launch week starts on Dec 2 for every team at once, so the stages before it must finish by Dec 1.

| Stage | Who | Where, when | Go on when | Stop and roll back when |
|---|---|---|---|---|
| 0. Every merge | Pipeline | `test`, on each merge to `hw2` | Smoke check passes on both services | The smoke check fails: the pipeline keeps the old revision |
| 1. Our team | The five of us, each with our own Google, Dropbox and Trello accounts | `demo`, Nov 30 | All five complete S1 with no help; every date correct; no stuck proposal; no alert | Any safety violation (a change without approval, another user's data), or a wrong date we can't explain |
| 2. One sister team | Two people from Team 5, who already use our storage package | `demo`, Dec 1 | S1 complete for both, without coaching (task 5.10); feedback filed as issues | As stage 1 |
| 3. Everyone | Assigned teams and staff | `demo`, Dec 2–6 | Not a gate: we watch the measures below | As stage 1, or a measure stays past its threshold for 30 minutes |

**What we watch** (objectives from the readiness record; agent dashboard and alert from #112):

| Measure | Healthy | Act when |
|---|---|---|
| Task success: turns ending `replied` or `proposal_ready` | most turns | the agent alert opens (more than 3 failed turns in 5 minutes) |
| Full-turn latency p95 | ≤ 45 s | above 45 s for 30 minutes |
| API errors (5xx) | < 1% | the 5xx alert opens |
| Execution | no change left `uncertain` or proposal stuck in `executing` | any; the student is told, and the runbook's stuck-proposal section applies |
| Safety | 0 changes without approval, 0 cross-user reads | any, at once |
| Gemini daily use | below the free-tier allowance | above 80% of it before the evening |

**Stop actions, fastest first:**

1. **Turn the assistant off** when it misbehaves (wrong proposals, a prompt injection that gets through): set `MODEL_PROVIDER=disabled` on the agent service. Turns answer `503 assistant_disabled`; students can still see their plans, approve and execute, because those go to the API. It takes effect with the next revision (about a minute):

    ```bash
    gcloud run services update course-agent-demo --region=us-central1 --update-env-vars=MODEL_PROVIDER=disabled
    ```

    `tofu apply` would turn it back on, so to keep it off, set `agent_model_provider = { demo = "disabled" }` too.
2. **Roll back a release** (section 4) when errors start with a new revision.
3. **Restore data** (section 6) when a release or a bug damaged the database. This is only possible within the last 6 hours.

Nothing can stop executions on their own yet. If a fault is in execution, the only lever is rolling back the API. An operator switch for execution belongs with the kill switch in #102.

## 3. Production smoke check

What the pipeline checks before a revision gets traffic today, and what #113 asks for:

| Check | API service | Agent service |
|---|---|---|
| Liveness (`/health`) | yes | yes (the only check today) |
| Database reached and migrated (`/health/database`) | yes | n/a |
| Provider access | Dropbox only, with our own account (`/health/provider`) | n/a |
| A storage round trip (`scripts/consumer_workflow.py`) | yes | n/a |
| Sign-in | **missing**: needs #96 | **missing** |
| S1 | **missing** | **missing** |

**Plan, once sign-in (#96) and provider connections (#98–#101) exist:** a smoke user with its own test accounts, signed in without a browser (a session created for the smoke user in `test` only). After each deploy, before traffic moves, the smoke check:

1. checks the smoke user's session is accepted, and refused after sign-out;
2. checks each provider connection reads as connected;
3. sends one turn, "List my courses", and expects `replied` naming the smoke course. That is one or two model calls per deploy, which matters at 500 a day;
4. proposes and executes one calendar event in the smoke user's calendar, checks the read-back, then removes it.

Steps 3 and 4 cover S1's path end to end without a full setup's model cost. Until then, S1 is checked by hand in stage 1.

## 4. Release rollback

Each service rolls back on its own: every release is a tagged revision (`rel-<sha>`) on both.

```bash
# Which releases are there, and which serves?
gcloud run services describe course-agent-demo --region=us-central1 \
  --format="table(status.traffic[].tag,status.traffic[].percent)"
# Send traffic back to the previous good release (agent, API, or both).
gcloud run services update-traffic course-agent-demo --region=us-central1 --to-tags=rel-<good sha>=100
gcloud run services update-traffic cloud-storage-service-demo --region=us-central1 --to-tags=rel-<good sha>=100
```

Then run the smoke check against the service URL. Roll back the service whose errors started with the new revision. If the new release included a migration, check that the old code works with the new schema (section 1) before rolling the API back.

**What rollback can't undo:** the schema, configuration and secrets; and changes a faulty release already made in the database or in students' Dropbox, Trello or Calendar. Database changes can be restored within 6 hours (section 6); provider changes have to be found through the proposals' outcomes and removed with the student.

**Drill (pending):** repeat the HW2 [rollback drill](rollback-drill.md) for the agent service: a deliberately broken agent release deployed with no traffic must fail its check, and a rollback must restore turns. It needs an account with deploy rights on the project and the agent loop deployed (#92).

## 5. Game day (pending)

A planned failure on `test`, recovered by a teammate who didn't write the runbook, using only the runbook and the dashboards.

- **When:** Nov 30, after the agent loop (#92) is deployed and the agent alert (#119) is applied.
- **Who:** John injects the failure and observes; the responder is a teammate who didn't write the runbook (Zesan, Jing or Victor). The responder isn't told the cause.
- **The failure:** the model key is rejected on the agent service. It is a realistic failure (a revoked key, or the project's billing changing), it is recoverable without touching data, and it fires the agent alert, so the same exercise is the alert's test from firing to clearing. It is injected with the [agent alert drill](https://github.com/Shreyas191/OSPSD-Team-3/pull/119) steps, on a revision that serves the test service.
- **What we record:** time to detect (alert email), time to diagnose (cause named), time to recover (turns succeed again), every place the runbook was unclear or wrong, and the fix for each.

## 6. Backup restore (pending)

**What Neon keeps on our plan** (Neon docs, read Nov 27, 2026): the free plan can restore a branch to any point in its **last 6 hours**, up to 1 GB of changes; a project may have **10 branches**; computes suspend after **5 minutes** idle and can't be kept on.

**What that means:** damage noticed within 6 hours can be undone from Neon; damage noticed the next morning can't. During launch week that is a real gap. **Recommendation:** a daily logical backup (`pg_dump`) of the `demo` database during launch week, kept outside Neon. It is small (kilobytes to a few megabytes) and makes recovery possible beyond 6 hours. This is a team decision; recorded here until made.

**The drill**, restoring into a throwaway branch so production is never touched:

1. Note the time `T` (UTC) and count the rows of a few tables on the production branch (`users`, `proposals`, `change_outcomes`).
2. Create a branch `restore-drill` in the same project.
3. Restore `restore-drill` from the production branch's history at `T`: in the console, open `restore-drill` → Backup & Restore → Restore from history, with the production branch as the source; or `neon branches restore restore-drill <production branch>@<T>`.
4. Connect to `restore-drill` and compare the row counts with step 1. Note how long the restore took.
5. Delete `restore-drill` and the `restore-drill_old_…` branch the restore leaves behind (the plan allows 10 branches).

It needs access to the team's Neon project (Shreyas owns it).

## 7. Results

| Drill | Date | Result | Findings and fixes |
|---|---|---|---|
| Agent release rollback | pending | | |
| Game day (model key rejected) | pending | | |
| Neon restore into a throwaway branch | pending | | |
| Stage 1: our team on `demo` | pending | | |
| Stage 2: sister team on `demo` | pending | | |
