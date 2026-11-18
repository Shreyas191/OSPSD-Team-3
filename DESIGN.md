# Team 3 Design Document

**By:** John Ashik, Shreyas Kaldate, Zesan Rahman, Jing Qian, Victor Wang
**Vertical:** Document Storage (provider: Dropbox)
**Reviewers:** HW3 Milestone 1: Victor Wang (Team 3, whole chapter), Chanyoung Kang (Team 4, integration design). HW2: Victor Wang (Team 3, whole design), Darsh (Team 6, contract-facing parts)
**Last updated:** 11/18/2026
**Status:** HW3 Milestone 1 (composing the services) is in [the first chapter](#hw3-milestone-1-composing-the-services); it was reviewed within the team and by Team 4 on Nov 18 (see [HW3 review](#hw3-review)). HW2: implemented on `hw2` and released as [`v0.2.0`](https://github.com/Shreyas191/OSPSD-Team-3/releases/tag/v0.2.0), deployed to both the test and demo services by the pipeline. The design review in [Review](#review) is not complete. Sections written on Nov 4 as a plan now describe what was built. [What changed during implementation, and why](#what-changed-during-implementation-and-why) lists every place where the build differs from the Nov 4 plan.

**Links:**

- Shared contract: [Dayana20/ospsd26-cloud-storage](https://github.com/Dayana20/ospsd26-cloud-storage), latest tag [`0.3.1`](https://github.com/Dayana20/ospsd26-cloud-storage/tree/0.3.1). Its package `cloud_storage_api` defines the shared models and error table.
- Shared conformance suite: Team 6's commit [`b77617c`](https://github.com/prateeks99/ospsd26-cloud-storage/tree/b77617c218d345c8cde28dbf08cf4d1a5cbaef38), pinned in CI until the contract repository merges it (its PR #6).
- Decision memo: <https://docs.google.com/document/d/1K9U8uO-FVbeu0PGlWpr_frJ6YQmoHXdz/edit?usp=sharing&ouid=104071874439967208816&rtpof=true&sd=true>
- Team 3 repository: <https://github.com/Shreyas191/OSPSD-Team-3> (branch `hw2`)
- Operating the deployed service (what runs, which version, health, logs, rollback): [docs/operations.md](docs/operations.md)

## HW3 Milestone 1: Composing the services

This chapter describes the HW3 Milestone 1 composition. One API operation, `POST /workflows/bug-report`, completes a concrete workflow across our cloud storage and other teams' issue tracker and calendar. It runs fixed steps, uses no language model, and works with one test account per provider. The chapters after this one describe the HW2 storage service it extends. Usage, request and response examples are in [docs/composed-workflow.md](docs/composed-workflow.md).

**Status (Nov 17):**
- Implemented on `hw2` ([#71](https://github.com/Shreyas191/OSPSD-Team-3/pull/71), [#73](https://github.com/Shreyas191/OSPSD-Team-3/pull/73), [#74](https://github.com/Shreyas191/OSPSD-Team-3/pull/74), [#76](https://github.com/Shreyas191/OSPSD-Team-3/pull/76)), tested with controlled implementations, and deployed through the HW2 pipeline as release `rel-3d16d2f`.
- **The test service runs the real implementations:** `ISSUE_TRACKER_IMPL=team4` and `CALENDAR_IMPL=team5`, with their credentials in Secret Manager. The demo service runs the same release with `fake` for both.
- **Deployed run against all three real providers** on Nov 17: `201` in 2.2 s, and each record was confirmed with its provider (details in [how we verify it](#hw3-how-we-verify-it)):
  - the report file `/bug-reports/f49644be535b40c8b458a688aef5f666.md` in Dropbox;
  - Trello card `6afce1b0f7149de4bd44659e`;
  - Google Calendar event `f2c9v7313ton9afoq0blku2b4k`, Nov 18 10:00–10:30 ET.
- **Deployed failure run** on Nov 17 with `CALENDAR_IMPL=failing`: `503 provider_unavailable`, `"step": "calendar"`, with the issue and the file undone and confirmed gone from Trello and Dropbox ([evidence](docs/evidence/hw3-failure-run.md)).
- **Deployed swap run** on Nov 17: the calendar implementation was changed from `team5` to our controlled `fake` by configuration alone, and the workflow returned `201` ([evidence](docs/evidence/hw3-swap-run.md)). The swap is not to a second *team's* implementation: neither other calendar team's package installs alongside Team 5's (see [H1](#hw3-open-questions)).
- Reviewed on Nov 18 within the team (Victor Wang) and by another team (Team 4). Every comment and its disposition is recorded in [HW3 review](#hw3-review).

### HW3: how the system fits together

```mermaid
flowchart LR
  caller([Caller]) -->|"POST /workflows/bug-report<br/>{title, description, reporter}"| route
  subgraph svc["Cloud Run: cloud-storage-service-&lt;env&gt; (stateless, no database)"]
    route["workflow_routes.py<br/>validates input, builds the response"] --> wf["workflow.py<br/>fixed steps, undo on failure"]
    wf -->|"1 · DocumentStorage<br/>(cloud_storage_api 0.3.1)"| ds["DropboxDocumentStorage<br/>ours, v0.3.0"]
    wf -->|"2 · IssueProvider<br/>(issue-tracker-contract v1.1.0)"| it["ISSUE_TRACKER_IMPL<br/>team4 · fake · failing"]
    wf -->|"3 · CalendarService<br/>(calendar-contract v0.1.0)"| cal["CALENDAR_IMPL<br/>team5 · fake · failing"]
  end
  ds --> dbx[("Dropbox app folder<br/>/bug-reports/")]
  it -->|"Team 4's TrelloIssueProvider"| trello[("Trello test board")]
  cal -->|"Team 5's team5-calendar"| gcal[("Google Calendar<br/>shared test account")]
  sm[["Secret Manager"]] -.->|"read when an instance starts"| svc
```

The three verticals run in one process. Each box on the right is a package. The workflow sees only the three contract types, and `verticals.py` is the only module that names another team's package.

| Vertical | Shared contract (tag) | Implementation we run | Chosen by | Test account and where its credentials live |
|---|---|---|---|---|
| Cloud storage | `cloud_storage_api` [`0.3.1`](https://github.com/Dayana20/ospsd26-cloud-storage/tree/0.3.1) (Dayana20/ospsd26-cloud-storage) | Ours: `DropboxDocumentStorage` (`v0.3.0`) | Always ours (`app.py`) | One Dropbox app per environment; `dropbox-*-<env>` in Secret Manager; locally `.env` and `.dropbox_token.json` |
| Issue tracker | `issue-tracker-contract` `issue-tracker-contract-v1.1.0` (Tyler-Arciniaga/OSPSD-Team-4, `contract/`) | Team 4: `team-4-issue-tracker-trello` `team-4-issue-tracker-trello-v1.0.0` | `ISSUE_TRACKER_IMPL` | Our Trello test board; `trello-{api-key,token,list-id}-test` in Secret Manager; locally `.env` |
| Calendar | `calendar-contract` `v0.1.0` (Airbus-A330/OSPSD-Calendar) | Team 5: `team5-calendar` `team5-calendar-v0.1.0` | `CALENDAR_IMPL` | Google account `shareddrive1515@gmail.com`, as a test user of our OAuth app; `calendar-token-test` in Secret Manager, mounted as a file; locally `~/.config/ospsd-team3/token.json` |

Every package is pinned to a tag in `src/cloud_storage_service/pyproject.toml`, and `uv.lock` records each tag's exact commit. Secrets are created empty by OpenTofu (`infra/composition.tf`), filled by a person with `gcloud secrets versions add`, and mounted only on an environment that runs the implementation needing them. Nothing secret is in the repository, the image, the OpenTofu state or the logs.

**Storage the workflow uses.** None of its own: the service stays stateless. The records live in the three providers, linked by IDs. The file name is the `report_id`, the issue title carries its first 8 characters, and the event title carries the issue ID.

#### A request, traced

Observed locally on Nov 17 with real Dropbox and Team 4's Trello implementation on our test board. The calendar was the in-memory fake, until the Google token is deployed.

1. **Input.** `POST /workflows/bug-report` with `{"title": "Login fails after password reset", "description": "Reset the password, log in with the new one: 500.", "reporter": "victor"}`.
   - `BugReportRequest` checks the lengths, that no field is blank, and that there are no extra fields. Any failure is `400 invalid_request` before any provider is called.
   - The workflow draws `report_id = c34bddd9271c4919b0d120ccb9184e64`.
2. **Storage.** `DocumentStorage.create("/bug-reports/c34bddd9….md", b"# Login fails after password reset\n\n- Report: c34bddd9…\n- Reporter: victor\n- Created: 2026-11-17T…Z\n\n…")` returns a `FileInfo`.
   - Intermediate state: the file exists. The undo list is `[delete file]`.
3. **Issue tracker.** `IssueProvider.create_issue("[bug c34bddd9] Login fails after password reset")` returns `Issue(id="<card id>", …)`, the ID Trello assigned.
   - Intermediate state: the file and the card exist. The undo list is `[delete file, delete issue]`.
4. **Calendar.** `CalendarService.create(CreateEvent(title="Triage: Login fails after password reset (issue <card id>)", start="2026-11-18T15:00:00+00:00", end="2026-11-18T15:30:00+00:00"))` returns an `Event`. The contract model validates the times and the title.
5. **Result.** `201` with `{report_id, file, issue, event}`. The file and the card were then confirmed with each provider.

The same request with `CALENDAR_IMPL=failing` stops at step 4 with `CalendarError(provider_unavailable)`:
- The workflow deletes the issue, then the file.
- It returns `503 {"error": {"code": "provider_unavailable", "message": "The calendar is unreachable; try again later", "step": "calendar", "report_id": "2e91d2fa…", "undone": ["issue 6afbbdfb…", "file /bug-reports/2e91d2fa….md"], "left_behind": []}}`.
- Afterwards Trello answered `not_found` for the card and Dropbox had no file.

### HW3: what decisions make it work

#### Decision H1: on a partial failure, undo the finished steps, then report

- **Constraint.**
  - The service is stateless, and Milestone 1 has no database.
  - None of the three contracts offers an idempotent create:
    - the calendar contract says "creation is not idempotent";
    - `IssueProvider.create_issue(title)` has no key;
    - `DocumentStorage.create` refuses an existing path.
  - The handout asks that a retry not duplicate finished work, and that unfinished work never be reported as done.
- **Approach.** The steps that succeeded are undone newest first: the issue, then the file. The error names the failed `step` and lists what was `undone` and anything possibly `left_behind`. A retry starts over with a new `report_id`. Undoing is possible because each contract has a delete for what we created.
- **Alternative.** Keep the finished steps and resume. The caller would send an idempotency key, and the service would store which steps finished for that key, then skip them on retry.
- **Tradeoff we accept.** A retry redoes the earlier steps: three provider writes instead of one. A short window exists where the records exist before they're undone. In return we need no database and no key protocol, and a failed run tries to undo the completed steps and reports anything that may be left behind. Resuming would need durable state (a database, or a store keyed by request) and a way to find a record created by a previous attempt. The issue-tracker contract can't do the latter: Team 4's `find_tagged_issue` exists only on their package, outside the contract.
- **Ordering.** Storage comes first because it's the only step whose record we can find without its ID: the path comes from the `report_id`. So an uncertain upload can be cleaned up.
- **Revisit when:**
  - Milestone 2 adds the persisted database the product needs anyway, so step state can be stored;
  - a step has a side effect that can't be undone (for example, invitations sent to people);
  - the contracts add idempotency keys or a find-by-tag operation.

#### Decision H2: other teams' implementations run in our process as tagged packages, chosen per environment by configuration

- **Constraint.**
  - The spec requires tagged shared contracts, another team's implementation for two verticals, "one test account" with credentials outside the repository, and swapping teams with no code change.
  - Team 4's README says other teams should use their package, not their deployed service.
  - Team 5 publishes `team5-calendar` for the same purpose.
- **Approach.**
  - `verticals.py` builds each vertical from one setting (`ISSUE_TRACKER_IMPL`, `CALENDAR_IMPL`), with `fake` and `failing` controlled implementations alongside the real ones.
  - OpenTofu sets the value per environment (`vertical_implementations`) and mounts only the credentials of the chosen implementation.
  - A wrong value or a missing credential stops startup with a message naming the setting. The previous revision keeps serving.
  - Our service checks this itself: Team 4's `TrelloIssueProvider()` reads its settings only when it makes a call, so without our check a missing setting would surface only at the first request. Settings that are present but rejected by Trello still surface at the first request, as `502 provider_error`.
- **Alternative.** Call each team's deployed HTTP service with a key they issue.
- **Tradeoff we accept.**
  - We own the test accounts and their upkeep. The Google token of an OAuth app in Testing expires every 7 days and must be renewed (see [failures](#hw3-what-could-go-wrong)).
  - Every implementation must install alongside ours. `team5-calendar` requires Python 3.14, so the service moved to 3.14 ([#70](https://github.com/Shreyas191/OSPSD-Team-3/pull/70)). Our published packages still support 3.12.
  - A swap candidate that caps Python below 3.14 (Teams 2 and 7 today) can't be used until it lifts the cap.

  In return, records land in accounts we can inspect, nobody else's quota or availability is involved, and the swap is one setting.
- **Revisit when:** a team offers only a deployed service, or a later level needs per-user provider accounts. Then implementations must take credentials per request rather than from the environment.

#### Boundaries and rules that hold when a dependency fails

- **Inputs.**
  - The HTTP layer validates the request: lengths, no blank fields, no extra fields.
  - The contract models validate what crosses each boundary: `CreateEvent` checks the RFC 3339 times and the title.
  - The issue title is cut to the issue-tracker contract's 256 characters.
- **Selection.**
  - `app.py` is the only place naming Dropbox.
  - `verticals.py` is the only place naming Team 4's or Team 5's package.
  - Routes get each vertical through a FastAPI dependency, which tests override.
- **Credentials.** They come from the environment or a mounted file, filled from Secret Manager. Startup errors name the missing setting, never its value.
- **Rules:**
  1. `201` only when all three records exist.
  2. Never delete a record this run didn't create. A storage `409 conflict` deletes nothing.
  3. Never retry a write automatically. Each contract warns that a failed write may have been applied.
  4. Report every record that may remain (`left_behind`), never hide it.
  5. Provider messages go to the log, not to the caller. 5xx messages are fixed.
  6. `failing` implementations are refused on the demo service.

### HW3: what could go wrong

| Failure | Trigger | State left behind | What the caller sees | Recovery | Test |
|---|---|---|---|---|---|
| **Calendar fails after the issue is created (partial failure)** | Google Calendar down, or the token expired | None: the issue, then the file, are deleted | `503 provider_unavailable` (or `502`), `step: calendar`, `undone: [issue, file]` | Retry; it creates new records once the calendar works | `test_calendar_unavailable_undoes_the_issue_then_the_file`, `test_retry_after_a_calendar_failure_leaves_exactly_one_of_each`; live with `CALENDAR_IMPL=failing` |
| Google token expired (7 days in Testing) | Token not renewed | As above, on every request | As above | Run `team5_calendar.authorize` again, then `gcloud secrets versions add calendar-token-test` (docs/infrastructure.md) | Covered by the row above; renewal is in the runbook |
| Issue tracker fails | Trello down, or credentials rejected | None: the file is deleted | `502 provider_error`, `step: issue_tracker`, `undone: [file]` | Depends on the cause. Trello down: retry. Credentials rejected: fix the Trello secrets first, then retry. If `left_behind` names a possible card (for example after a timeout), check the board and delete it before retrying (next row) | `test_issue_tracker_failure_undoes_the_file` |
| Uncertain write (issue or event) | The request reached the provider but the reply was lost | Possibly one card or event | The step's error, with `left_behind: ["possibly an issue titled '[bug <id8>] …'"]` | Find it by the `report_id` prefix and delete it, then retry | `test_uncertain_issue_write_…`, `test_uncertain_calendar_write_…` |
| An undo fails | The provider is down when we delete | The record named in `left_behind` | The original step's error; `left_behind: ["issue <id>"]` | Delete it by ID | `test_an_undo_that_fails_is_listed_as_left_behind` |
| Storage reply lost | The upload reached Dropbox, but no reply came | None: the file is deleted by its known path. If that delete fails too, `left_behind` names the path | `503`, `step: storage` | Retry | `test_storage_provider_failure_removes_a_file_the_lost_reply_hid`, `test_storage_cleanup_that_fails_is_reported` |
| Storage path taken | A file already exists at the path | Nothing changed; the existing file is kept | `409 conflict`, `step: storage` | Retry; it draws a new `report_id` | `test_storage_conflict_is_reported_and_the_existing_file_is_kept` |
| Misconfiguration | Unknown `*_IMPL` value, a missing Trello setting or token file, or `failing` on demo | The new revision doesn't start; the previous one keeps serving | Nothing: the previous release answers | Fix the setting and apply again | `test_verticals.py` |
| Another team's implementation diverges from its contract | A behaviour the contract doesn't pin down | Depends on the case | Our error format, mapped from the contract error | Report it to the owning team with a reproduction (none needed so far) | Calendar fakes pass the contract's own conformance suite; real implementations are checked live |

**The handout's example, in our order:** "if an event is created but saving its summary fails". Our workflow saves first, so the matching case is a later step failing after earlier ones succeeded. Nothing finished is kept, so a retry can't duplicate it, and a failed run can't be mistaken for a finished one: the response is an error that lists what was undone.

#### HW3: how we verify it

- **With controlled implementations** (CI, every commit):
  - 23 HTTP tests run the endpoint against the in-memory storage behind our real `DropboxDocumentStorage`, and the fake and failing issue tracker and calendar. They cover success, a failure at each step, uncertain writes, a failed undo, retry, and validation.
  - The calendar fakes pass `calendar_contract`'s conformance suite.
  - `test_verticals.py` covers selection and startup.
- **With real providers:**
  - **Composition:** on the deployed test service with `team4` and `team5`, check the file in Dropbox, the card on the Trello board and the event in the test account's calendar. Success is the `201` body matching the records each provider shows.
  - **Failure:** on test with `CALENDAR_IMPL=failing`, the caller gets the documented error naming `calendar`, and both providers show no leftover records.
  - **Swap:** change `CALENDAR_IMPL` (or `ISSUE_TRACKER_IMPL`) to a second team's package and repeat the composition check, with no code change.
  - **Recorded so far:** composition on the deployed test service, Nov 17, revision `cloud-storage-service-test-00035-z4g` (the configuration now serving as `rel-3d16d2f`), `report_id` `f49644be535b40c8b458a688aef5f666`:
    - Dropbox: the file's metadata and contents were read back through the deployed API ("# Login fails after password reset", 257 bytes); its ID, size, time and etag match the `201` response ([screenshot](docs/evidence/hw3-composition-dropbox-file.webp));
    - Trello: the card was read directly from Trello with Team 4's provider;
    - Trello: the card was also seen on the board in the **Incoming** list ([screenshot](docs/evidence/hw3-composition-trello-card.webp)).
    - Google Calendar: the event "Triage: Login fails after password reset (issue 6afce1b0f7149de4bd44659e)" is in the test account's calendar on Wed Nov 18, 10:00–10:30 (GMT-5), with the issue ID matching the Trello card ([screenshot](docs/evidence/hw3-composition-calendar-event.png)).
  - **Failure, recorded Nov 17** on test (revision `cloud-storage-service-test-00037-rgn`, `CALENDAR_IMPL=failing`, issue tracker real): `503 provider_unavailable`, `"step": "calendar"`, `undone` listed the issue and the file, and `left_behind` was empty. Afterwards Dropbox answered `404` for the file and Trello `not_found` for the card. Request, response and checks: [docs/evidence/hw3-failure-run.md](docs/evidence/hw3-failure-run.md).
  - **Swap, recorded Nov 17** on test (revision `cloud-storage-service-test-00039-7kp`): only `CALENDAR_IMPL` changed, from `team5` to `fake`, giving `201` with the event now from the in-memory implementation (UUID ID, UTC times) while storage and the issue tracker stayed real. [docs/evidence/hw3-swap-run.md](docs/evidence/hw3-swap-run.md).

### HW3 review

| Review | Reviewer | Scope | Status |
|---|---|---|---|
| Within the team | Victor Wang (Team 3) | This chapter: architecture, decisions, failure handling and the test evidence | [Done, Nov 18](https://github.com/Shreyas191/OSPSD-Team-3/pull/77#pullrequestreview-5959797884) |
| Another team | Chanyoung Kang (Team 4, the issue-tracker implementation we use) | Integration design: H1, H2 and the issue-tracker error mapping, checked against the shared issue-tracker contract and Team 4's Trello package | [Done, Nov 18](https://github.com/Shreyas191/OSPSD-Team-3/pull/77#issuecomment-6968245306) |

| # | Comment and reviewer | Disposition | Link |
|---|---|---|---|
| 1 | **H1 (undo):** agrees with undoing completed steps with `delete_issue(id)`, which Team 4's Trello and Linear providers both support, and with not using the Trello-only tagging helpers. Checked the calendar-failure evidence. Suggests H1's tradeoff say the workflow *tries* to undo and reports what may be left behind, instead of "leaves a clean state", to match the failure table. (Chanyoung Kang, Team 4) | Accepted: H1's tradeoff now says the run tries to undo the completed steps and reports anything that may be left behind. | [comment](https://github.com/Shreyas191/OSPSD-Team-3/pull/77#issuecomment-6968245306) |
| 2 | **Error mapping:** confirms `may_have_applied=True` on `create_issue` means a card may exist, and that mapping every issue-step error to `502 provider_error` is reasonable. Asks that the "Retry" advice for the issue-tracker failure distinguish rejected credentials (fix first) from a timeout (check the board first, to avoid a duplicate). (Chanyoung Kang, Team 4) | Accepted: that row's recovery now depends on the cause: Trello down, credentials rejected, or a possible card named in `left_behind`. | [comment](https://github.com/Shreyas191/OSPSD-Team-3/pull/77#issuecomment-6968245306) |
| 3 | **H2 (in-process package):** confirms installing the package against our own Trello list is the intended use. Notes `TrelloIssueProvider()` reads its settings only when it makes a call, so a startup check must live in our service. (Chanyoung Kang, Team 4) | Answered, already the case: `verticals.py` checks the three `TRELLO_*` settings before building the provider, so a missing one stops startup (`test_verticals.py`). H2 now says so, and that settings Trello rejects still surface at the first request. | [comment](https://github.com/Shreyas191/OSPSD-Team-3/pull/77#issuecomment-6968245306) |
| 4 | **Whole chapter:** reviewed the architecture, the decisions, the failure handling and the test evidence; found them consistent with the intended workflow, with no blocking issues. (Victor Wang, Team 3) | No change needed: no issues raised. | [review](https://github.com/Shreyas191/OSPSD-Team-3/pull/77#pullrequestreview-5959797884) |

### HW3 open questions

| # | Question | Status | Owner |
|---|---|---|---|
| H1 | Which second team's implementation do we swap to? | **Decided for Milestone 1: the controlled `fake`.** Teams 7 (`baskd`) and 2 are the only other calendar teams. Team 7's package caps Python at `<3.13` (pip refuses it on 3.14) and pins the contract to `v0.1.0-rc.1`, which conflicts with Team 5's `v0.1.0`. Team 2's implementation isn't an installable package. We switch to a second team's tagged release once one installs alongside Team 5's. | Shreyas |
| H2 | Which steps will need judgment later, and which stay fixed? | **Proposed:** an agent would choose the issue title and summary, the triage time (from free calendar time) and whether a meeting is needed at all. Saving the report, linking the IDs, the undo and the error reporting stay fixed code. | Team |
| H3 | What if Team 4 or Team 5 is unavailable? | **Proposed:** switch the vertical to `fake` (one setting) so the rest of the workflow keeps working, and report the outage to the owning team. The swap candidate in H1 is the real fallback. | Team |
| H4 | Contract mismatches found | None so far in the implementations we use (Team 4, Team 5). A mismatch would be reported to the owning team with a reproduction, not worked around. | Team |
| H5 | Renewing the Google token every 7 days | **Accepted operational task** until the OAuth app is verified or per-user accounts arrive (Milestone 2). The steps are in docs/infrastructure.md. | John |

## Introduction

The Document Storage vertical provides one HTTP contract for consumers that need to store and manage files across the Team 1, Team 3 and Team 6 services. Consumers should be able to use the same request formats and interpret responses the same way, even though each service uses a different storage provider.

### Context

The three services support seven overlapping operations: upload, replace, move, list, download, metadata and delete. Before this work, their APIs differed in ways that forced consumers to write provider-specific code. For example, uploading a file was `POST /files` with `file` and `path` on Team 1, `POST /files/upload` with `file` and `remote_path` on Team 3, and `POST /files/{key}` on Team 6. Error bodies and metadata fields also differed. Some differences changed behaviour: Team 3 deleted a folder and its contents, while Team 1 rejected folder deletion. A consumer that switched services had to change its requests and could get different behaviour for the same operation. A consumer that used several services had to track how each one worked.

### Goals and non-goals

Goals, in no particular order:

- Give each common operation one agreed route, HTTP method and request shape across all participating services.
- Return a consistent file information object from upload, metadata lookup, list and move.
- Use one shared error response format, with consistent meanings for HTTP statuses and error codes, so consumers can handle errors without relying on provider-specific messages.
- Make operations whose effects could differ, such as creating versus replacing a file or deleting a folder, behave as one documented contract says.
- Let a consumer's common workflow run against each participating service with the same requests and outcome handling, changing only the base URL, credentials and provider-specific identifiers.
- Follow RESTful practice in every route, so consumers can predict them.

Non-goals, which a reader might expect but we are not pursuing:

- Migrating existing files, folders or identifiers between providers.
- Exposing every capability of each storage provider through the shared contract.
- Switching storage providers during an in-progress request.
- Standardizing authentication and credential provisioning. Each service keeps its own credentials and setup, and consumers provide what the service they call requires.

## What changed during implementation, and why

The Nov 4 version of this document was a plan. This table records each place where the implementation differs from it, as of release `v0.2.0` (Nov 12). Every other section has been updated to match the code.

| # | Nov 4 plan | What was built | Why | Where |
|---|---|---|---|---|
| 1 | Replace every HW1 route; no aliases (Decision 2) | `POST /files/upload`, `POST /files/move`, `POST /files/rename`, `POST /files/copy`, `POST /folders/create` and `GET /files/search` stay as Team 3 extensions. Only `GET /folders/list` was removed. Bad input is `400` on every route, old and new. | Removing upload and move would have broken our own tests, tutorial and e2e checks in the same PRs that added the shared routes. Keeping them costs one handler each, and they share the new routes' validation, error body and logs. The worry behind Decision 2, that an alias would answer the same mistake two ways, didn't happen: the app-level validation handler gives every route `400 invalid_request`. | `edbdd8a`, `f670154` |
| 2 | `PATCH /files` moves files and folders, and refuses case-only renames | `PATCH /files` moves **files only**. A folder, or a `new_path` equal to `path` ignoring case, is `422 invalid_operation`. `POST /files/move` still moves folders. | The shared conformance suite expects these rules. Applying them needs the source's metadata first, so a `PATCH` makes one extra Dropbox call. | `2f09cf7` |
| 3 | Optional `etag` (planned); uploads over 150 MiB are `400 invalid_request` | Every file response has `etag`, set to the Dropbox `rev` (`null` for folders). An upload Dropbox refuses as too large is `413 file_too_large`. | Contract `0.3.1` makes `etag` part of `FileInfo` and adds `413 file_too_large`. Dropbox documents its limit as 150 MB. | `d5a5c32` |
| 4 | Not anticipated | On Cloud Run, uploads above about 32 MiB never reach the service. Google's front end answers `413` with an HTML page, not the shared error body. | Measured on Nov 11: a 31 MiB upload returned `201`, and a 33 MiB upload returned `413 text/html`. Cloud Run limits HTTP/1 request bodies. | [Failure behavior](#failure-behavior), open question 13 |
| 5 | Depend on the shared Python package once it is tagged; remove `src/cloud_storage_client_api/` | The service still uses our local `cloud_storage_client_api` package. Contract `0.3.1` exists but is not pinned. | `0.3.1` defines a different interface (`DocumentStorage` with `create`, `replace`, `list_files` and `move`), not a drop-in for our `Client`. The HTTP behaviour already matches the contract and passes the conformance suite. Switching the Python dependency is a separate change. | Open question 1 |
| 6 | Python 3.11 → 3.12 once the contract is tagged | Done: Python 3.12 everywhere, including the container image and CI. | The contract package needs 3.12. | `pyproject.toml`, `.python-version` |
| 7 | Telemetry: OpenTelemetry to Grafana Cloud; alerts to Slack | Google Cloud Logging, Trace and Monitoring in the same project, with one dashboard and one alert, emailed to the team. | Item 3.1 sent the data to Google Cloud's own services. Grafana would need a second account and a Google service-account key to read them, plus one more thing to own and tear down, for the same charts. The handout leaves the backend to the team. | [Dashboard](docs/dashboard.md) |
| 8 | Conformance suite owned by Team 6, run before each release | It isn't merged in the contract repository yet, so CI pins Team 6's commit `b77617c` and runs it on every push against our real app on an in-memory client. Every deploy also runs the shared consumer workflow against the new revision, with real Dropbox, before it gets traffic. | It was the only available version of the suite. | [Testing](docs/testing.md) |
| 9 | Not planned | Fault injection (`FAULT_INJECTION`): off by default, it wraps the client so the provider is slow, failing, timing out or losing replies. The service refuses to start if it is set on demo. | The HW2 extension "Fault injection": the shared conformance suite was run under each fault to see which guarantees hold. | [Fault Injection](docs/fault-injection.md) |
| 10 | Not planned | An external synthetic probe: Cloud Monitoring uptime checks call `GET /files/metadata` on test and demo every 5 minutes from three regions, reported as an SLI of its own. | The HW2 extension "External synthetic probe": the server-side SLI can't see requests that never reach the service, or wrong answers with a good status. Both were observed. | [Synthetic Probe](docs/synthetic-probe.md) |
| 11 | Deploy to test and demo | Every push to `hw2` deploys test, then demo, and demo only after the same release passed its smoke check on test. `main` keeps the same pipeline but isn't updated for HW2. | `hw2` is the branch the team submits for HW2, so it ships to both environments. | [Release Pipeline](docs/release-pipeline.md) |
| 12 | Open questions 2, 3, 5, 9, 10 and 11 | Settled: see [Open questions](#open-questions). Stale replace and occupied destinations are `409 conflict`. Codes follow the contract's table. A partial folder delete is `502 provider_error`. Dropbox's stale-rev error was observed live. | Contract `0.3.1` and a live test (Nov 8). | — |

## Architecture as built

**Figure 1. How a consumer's request reaches Dropbox, and which package owns each step.** Solid arrows are calls at request time. The `◄──` arrow means "implements": `dropbox_client_impl` implements the `Client` contract. The service never imports `dropbox`.

```text
                    ┌──────────────────────────┐
                    │   HTTP client / caller   │
                    └────────────┬─────────────┘
                                 │ HTTPS (REST, ?path=)
                                 ▼
                    ┌──────────────────────────┐
                    │ Cloud Run (us-central1)  │  test: every push to hw2
                    │ ≤ 20 requests/instance,  │  demo: after test passes
                    │ ≤ 2 instances            │
                    └────────────┬─────────────┘
                                 ▼
┌────────────────────────────────────────────────────────────────────┐
│ cloud_storage_service                                              │
│   ├── RequestTelemetryMiddleware  (one JSON log line + trace span) │
│   ├── ConcurrencyLimitMiddleware  (≤ 10 in flight, else 503)       │
│   ├── App-level exception handlers (400, 404, 405, 500 bodies)     │
│   ├── FastAPI routes               (HTTP -> Client contract calls) │
│   └── SharedErrorRoute             (exception -> status and code)  │
└────────────┬──────────────────────────────────┬────────────────────┘
             │ calls the abstract contract      │ builds at startup
             ▼                                  ▼  (wrapped by faults.py
┌──────────────────────────────────┐   ┌──────────────────────┐ on test only,
│ cloud_storage_client_api         │◄──│ dropbox_client_impl  │ when FAULT_INJECTION
│   (Client, File, get_client())   │   │   (DropboxClient)    │ is set)
└──────────────────────────────────┘   └──────────┬───────────┘
                                                  │ SDK calls (traced)
                                                  ▼
                                       ┌──────────────────────┐
                                       │ Dropbox API          │
                                       │ (one app per env.)   │
                                       └──────────────────────┘
```

**Responsibilities and dependency direction.**

| Component | Owns | Depends on |
|---|---|---|
| `cloud_storage_service` | HTTP routes, request validation, overload protection, the shared error body, request logs and traces | The `Client` contract only |
| `cloud_storage_client_api` (local; see change 5) | The abstract `Client` and `File` classes, `FileTooLargeError` and `get_client()` | Nothing |
| `dropbox_client_impl` | `DropboxClient`, `DropboxFile`, OAuth in `auth.py`, and the translation of Dropbox errors to built-in exceptions in `errors.py` | The contract and the Dropbox SDK |
| `infra/` (OpenTofu) | Cloud Run services, Artifact Registry, secrets, service accounts, CI sign-in, log-based metrics, dashboard and alert | Google Cloud |

**Who creates and closes the provider client.** The app's `lifespan` calls `build_storage_client()` once at startup, wraps it in fault injection if `FAULT_INJECTION` is set, and stores it in `app.state.storage_client`. Startup fails, with one sanitized line, if required configuration is missing. Routes get the client through the `get_storage_client` dependency, so tests can swap in `FakeStorageClient`. The client lives as long as the process and is not closed explicitly; the Dropbox SDK's HTTP session ends when the process exits.

### Guarantees each request gets

- **Paths:** a path may omit the leading `/`; the service adds it. Empty segments, a trailing `/`, and `.` or `..` segments are `400 invalid_request`. Deleting the root (`?path=/`) is `422 invalid_operation`. Dropbox compares paths ignoring case.
- **Status boundaries:**
  - `400 invalid_request`: malformed input (a bad path, or a missing field or body).
  - `422 invalid_operation`: well-formed but not allowed, such as moving a folder with `PATCH`, downloading a folder, listing a file, or deleting the root.
  - `404 not_found`: the target doesn't exist. An unknown route is `404 route_not_found`, so a typo is never read as a missing file.
  - `409 conflict`: the destination is occupied, or the file changed during a replace.
  - `413 file_too_large`, `403 permission_denied`, `502 provider_error`, and `503 provider_unavailable` / `service_busy`.
- **File info:** `id`, `name`, `path`, `size` (`null` for folders), `modified_at` (UTC, ending in `Z`; `null` for folders) and `etag` (`null` for folders). List entries add `type` (`file` or `folder`).
- **Health:** `GET /health` answers without calling Dropbox, so Cloud Run's liveness probe never restarts the service because Dropbox is down. `GET /health/provider` makes one `users_get_current_account()` call.
- **Telemetry:** one JSON log line per request (operation, status, outcome, duration, release, trace ID) and a trace with a child span for each Dropbox HTTP call. File paths, query strings, bodies and headers are never logged; quoted text in error reasons is redacted.

### Request flow

Both traces below are real: requests to the test service on Nov 11, 2026, release `7d6b6ff`.

**A successful request: `GET /files/metadata?path=/ospsd-tutorial/Docs/report.txt`**

1. `RequestTelemetryMiddleware` starts a span and a timer. `ConcurrencyLimitMiddleware` admits the request (fewer than 10 in flight).
2. FastAPI matches `get_file_metadata` and validates `path` with `clean_path`.
3. The `StorageClient` dependency returns `app.state.storage_client`.
4. The route calls `client.get_metadata("/ospsd-tutorial/Docs/report.txt")`.
5. `DropboxClient` calls `files_get_metadata`, which is traced as a child span, and wraps the `FileMetadata` in a `DropboxFile`.
6. `FileResponse.from_file(file)` builds the body, and FastAPI answers `200`:

   ```json
   {"id": "id:KzOOg5ouvzUAAAAAAAABlQ", "name": "report.txt", "path": "/ospsd-tutorial/Docs/report.txt",
    "size": 25, "modified_at": "2026-11-11T22:39:15Z", "etag": "0166007dd5f13c300000003923a8b33"}
   ```

7. The middleware writes one log line: `{"operation": "get_file_metadata", "status": 200, "outcome": "ok", "duration_ms": 143.8, "release": "7d6b6ff…", "trace_id": "e79f34c1a428b59b39e4df6db476e7b2", …}`.

**An expected failure: `GET /files/metadata?path=/ospsd-tutorial/Docs/missing.txt` → 404**

1. Steps 1 to 4 as above.
2. `files_get_metadata` raises `ApiError` with `path/not_found`.
3. `dropbox_client_impl.errors.translate` raises the built-in `FileNotFoundError("No file or folder at '…'")`. No Dropbox type leaves the adapter.
4. `SharedErrorRoute` maps `FileNotFoundError` to `404 not_found` and records the failure for the request log.
5. Response:

   ```json
   {"error": {"code": "not_found", "message": "No file or folder at '/ospsd-tutorial/Docs/missing.txt'"}}
   ```

6. Log line: `"outcome": "not_found"`, `"provider_failure": false`, `"reason": "No file or folder at '…'"`, `"duration_ms": 118.1`. The path is redacted.

## API and data model

**Contract source.** Routes, request shapes, the file-info fields and the error table follow contract tag `0.3.1` and the shared conformance suite at `b77617c`. The suite passes: 55 tests in CI on every push, and against the deployed test service by hand on Nov 11 ([Testing](docs/testing.md)).

### Shared operations

| Operation | Route | Request | Success |
|---|---|---|---|
| Health | `GET /health`, `GET /health/provider` | none | `200 {"status":"ok"}` |
| Create | `POST /files` | multipart: `file`, `path` | `201` file info |
| Replace | `PUT /files` | multipart: `file`, `path` | `200` file info |
| Metadata | `GET /files/metadata?path=` | `path` | `200` file info |
| Download | `GET /files/download?path=` | `path` | `200` raw bytes |
| List | `GET /files?path=` | folder `path` (default `/`) | `200` array, sorted by path, each with `type` |
| Move or rename a file | `PATCH /files?path=<from>` | JSON `{"new_path": "<to>"}` | `200` file info |
| Delete | `DELETE /files?path=` | `path` | `204` |

### Team 3 extensions

These aren't part of the shared contract, and other teams' services may not have them. They use the same validation, error body and logs as the shared routes.

| Route | Request | Success | Note |
|---|---|---|---|
| `POST /files/upload` | multipart: `file`, `remote_path`, `overwrite` | `200` file info | HW1's upload. With `overwrite=true` it creates or overwrites in one Dropbox call, with no revision check. |
| `POST /files/move` | JSON `from_path`, `to_path` | `200` file info | Moves folders too, unlike `PATCH /files` |
| `POST /files/rename` | JSON `path`, `new_name` | `200` file info | Stays in the same folder |
| `POST /files/copy` | JSON `from_path`, `to_path` | `200` file info | The copy gets a new `id` |
| `POST /folders/create` | JSON `remote_path` | `200` file info | |
| `GET /files/search?q=&max_results=` | query | `200` array | Matches names across the whole app folder |

### File-info object

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Opaque Dropbox ID. It stays the same across move, rename and replace; a copy gets a new one. Unique only within one Dropbox app folder. |
| `name` | string | The last path segment |
| `path` | string | Canonical path starting with `/` |
| `size` | integer or null | Bytes; `null` for folders |
| `modified_at` | string or null | ISO 8601 UTC ending in `Z`; `null` for folders |
| `etag` | string or null | The Dropbox `rev`. It changes whenever the file changes, including on a move (observed). `null` for folders. |
| `type` | `file` or `folder` | List entries only |

### Behaviour

- **Create** never overwrites anything, not even identical bytes (Dropbox add mode with `strict_conflict`).
- **Replace** returns `404` for a missing file and never creates one. It is refused with `409` if the file changes between our two Dropbox calls (Decision 1).
- **Move** (`PATCH`) moves only files and never overwrites an existing destination.
- **Delete** also removes a folder's contents. Deleting again returns `404`.
- **List** is one level deep. The service fetches every Dropbox page, so consumers never handle cursors.

### Errors

All errors use `{"error": {"code", "message"}}`. Consumers should branch on the status and code, not on the message.

| Status | `code` | When |
|---|---|---|
| `400` | `invalid_request` | Malformed request: bad path, or a missing field or body |
| `403` | `permission_denied` | Dropbox refused the operation on this entry |
| `404` | `not_found` | Nothing at the path |
| `404` | `route_not_found` | No such route |
| `405` | `method_not_allowed` | The route exists, but not with this method (the `Allow` header is kept) |
| `409` | `conflict` | Destination occupied, or the file changed during a replace |
| `413` | `file_too_large` | Dropbox refused the upload's size (see [Failure behavior](#failure-behavior) for Cloud Run's lower limit) |
| `422` | `invalid_operation` | Not allowed: folder moved with `PATCH`, folder downloaded, file listed, root deleted, same path ignoring case |
| `500` | `internal_error` | A bug in the service |
| `502` | `provider_error` | Dropbox failed, including a partial folder delete |
| `503` | `provider_unavailable` | Dropbox unreachable or rate limiting us |
| `503` | `service_busy` | More than 10 requests in flight on this instance |

After a `502` or `503` on a write, the outcome is uncertain: check metadata before retrying.

### Examples (observed on the test service, Nov 11, 2026, release `7d6b6ff`)

```http
POST /files            (multipart: file=report.txt, path=/ospsd-tutorial/Docs/report.txt)

201 Created
{"id":"id:KzOOg5ouvzUAAAAAAAABlQ","name":"report.txt","path":"/ospsd-tutorial/Docs/report.txt","size":19,"modified_at":"2026-11-11T22:39:13Z","etag":"0166007dd41306800000003923a8b33"}
```

```http
PATCH /files?path=/ospsd-tutorial/Docs/report.txt
{"new_path": "/ospsd-tutorial/Archive/summary.txt"}

200 OK
{"id":"id:KzOOg5ouvzUAAAAAAAABlQ","name":"summary.txt","path":"/ospsd-tutorial/Archive/summary.txt","size":25,"modified_at":"2026-11-11T22:39:21Z","etag":"0166007ddc12d4e00000003923a8b33"}
```

```http
PATCH /files?path=/ospsd-tutorial/Archive
{"new_path": "/ospsd-tutorial/Old"}

422 Unprocessable Content
{"error":{"code":"invalid_operation","message":"Cannot move '/ospsd-tutorial/Archive': it is a folder, and only files can be moved"}}
```

```http
GET /folders/list?path=/ospsd-tutorial

404 Not Found
{"error":{"code":"route_not_found","message":"No route /folders/list"}}
```

The full run, with every shared operation and its failures, is in [`TUTORIAL.md`](TUTORIAL.md).

### What changed from HW1 for callers

- **Upload:** `POST /files` (create, `201`) and `PUT /files` (replace, `200`) are the shared routes. `POST /files/upload` still works.
- **Move:** `PATCH /files?path=` with `{"new_path"}` is the shared route, for files only. `POST /files/move` still works and still moves folders.
- **List:** `GET /folders/list` is gone, and calling it returns `404 route_not_found`. Use `GET /files?path=`, which adds `type` and sorts by path.
- **File info:** `is_folder` is dropped, `modified` is renamed `modified_at` (UTC with `Z`), and `etag` is added.
- **Errors:** validation failures are `400 invalid_request`, not `422 invalid_input`. Provider failures are `502 provider_error`, not `provider_failure`. Unknown routes, wrong methods and unexpected errors use the shared body.
- **New:** `GET /health` and `GET /health/provider`.

**Information lost:** metadata no longer says whether a path is a folder (a list entry's `type` does). Replace takes 2 Dropbox calls, not 1, and `PATCH` takes 2. Case-only renames through `PATCH` are refused.

## Alternatives

Three decisions, each compared with its alternatives. Decision 1 matters most to consumers, because the wrong choice loses data without an error. Each now has an **Outcome**.

### Decision 1: How `PUT /files` replaces a file

The memo says replace returns `404 not_found` for a missing file and never creates one. It also plans a revision check, so that a concurrent change is refused instead of overwritten.

| Option | Dropbox calls | Missing file | File changed by someone else |
|---|---|---|---|
| A. `files_upload` with overwrite | 1 | Created, which breaks the 404 rule | Overwritten without an error |
| B. `files_get_metadata`, then `files_upload` with overwrite | 2 | `404 not_found` | Overwritten if it changes between our two calls |
| C. `files_get_metadata`, then `files_upload` with `update(rev)` (**chosen**) | 2 | `404 not_found` | Refused with `409` if it changes between our two calls |
| D. The consumer sends the revision it last read, in an `If-Match` header | 1 | `404 not_found` | Refused if it changed since the consumer read it |

**Evidence.** Dropbox's [upload documentation](https://www.dropbox.com/developers/documentation/http/documentation#files-upload) says update mode overwrites only "if the given rev matches the existing file's rev". The 404 rule already needs the metadata call, so passing its rev to the upload adds no call.

**Outcome: implemented.** The live test `test_replace_against_stale_revision_is_a_conflict` (Nov 8) observed Dropbox's answer to a stale rev with `autorename=False`: `ApiError` with `path/conflict/file`. The file keeps the newer content, and replace raises `FileExistsError`, which is `409 conflict`. `test_replace_missing_file_raises_and_creates_nothing` checks the 404 rule. One gap remains, an accepted risk: a file deleted by someone else *between* our two calls is recreated, because Dropbox always writes to a path with no file. We have not observed this window. Option D is now possible, since `etag` is in the file info, but no consumer sends `If-Match` yet.

### Decision 2: Replace the HW1 routes, or keep them

Three HW1 routes changed: `POST /files/upload`, `POST /files/move` and `GET /folders/list`. The plan was to remove them, with no aliases.

**Outcome: changed during implementation** (change 1 above). Upload and move stay as extensions beside the shared routes, and `GET /folders/list` is removed. The plan's objection to aliases was that an alias keeping HW1's `422` would answer the same mistake two ways. That doesn't apply: one app-level handler makes every validation failure `400`, on old and new routes alike. A HW1 caller that checks for `422` on bad input still has to change. Upload and move now behave as Team 3 extensions, not as the shared operations: `POST /files/upload` with `overwrite=true` has no revision check, and `POST /files/move` moves folders.

### Decision 3: Where the shared error body is produced

`SharedErrorRoute` built `{"error": {"code", "message"}}` only for requests that reached a route. FastAPI answered every other request with its own body. On Nov 3, `GET /no-such-route` returned `404 {"detail": "Not Found"}`, which a consumer could read as a missing file.

| Option | What changes | Consequence |
|---|---|---|
| A. Keep `SharedErrorRoute` only | Validation status 422 becomes 400 | Unknown routes and wrong methods keep FastAPI's body |
| B. Replace it with app-level exception handlers | The error mapping and its logging move to new handlers | Every response uses the shared body |
| C. Keep `SharedErrorRoute`, and add app-level handlers for 404, 405 and 500 only (**chosen**) | One status change, plus three handlers | Every response uses the shared body; route errors keep their mapping and logs |

**Outcome: implemented** (`f670154`). Observed on the test service: `GET /folders/list` → `404 route_not_found`, `POST /files/metadata` → `405 method_not_allowed`. `test_unknown_route_returns_404_route_not_found` checks the body. The only responses that still don't use the shared body come from Cloud Run's front end, before the request reaches the service (see the 32 MiB limit below).

### Possible revisit reasons

| Decision | We revisit it if | Owner |
|---|---|---|
| 1 | A consumer needs protection against changes made since *it* read the file; we then accept `If-Match` with the `etag` (option D) | Zesan, with Team 1's create and replace clause owners (Annie, Shini) |
| 2 | The vertical asks services to expose only the shared routes; we then remove the extensions in a release with notes | Shreyas |
| 3 | We adopt the shared Python package's error classes (open question 1); we then map those instead of built-in exceptions | John, Victor |

## Failure behavior

FastAPI validates requests before a route calls the provider-independent `Client`. The client translates Dropbox errors into built-in exceptions; `SharedErrorRoute` maps those to the shared status and code; app-level handlers cover validation, unknown routes, wrong methods and unexpected exceptions.

| Outcome | Exception | HTTP response |
|---|---|---|
| Invalid request or path | `RequestValidationError` | `400 invalid_request` |
| Upload over Dropbox's limit | `FileTooLargeError` | `413 file_too_large` |
| Not allowed (folder moved with `PATCH`, folder downloaded, root deleted, …) | `ValueError`, `IsADirectoryError` | `422 invalid_operation` |
| Nothing at the path | `FileNotFoundError` | `404 not_found` |
| Destination exists, or the file changed during replace | `FileExistsError` | `409 conflict` |
| Permission denied | `PermissionError` | `403 permission_denied` |
| Dropbox unreachable, timed out or rate limiting | `ConnectionError` | `503 provider_unavailable` |
| Other Dropbox failure | `OSError` | `502 provider_error` |
| More than 10 requests in flight | — (middleware) | `503 service_busy` |
| Anything else | any other exception | `500 internal_error` |

The messages for 502, 503 and 500 are fixed, so Dropbox's own error text reaches only the logs. Other messages may contain the caller's paths.

**No automatic retries.** A provider failure can happen after Dropbox has completed a write, so a 502 or 503 doesn't prove that nothing changed. Callers should check metadata before retrying an uncertain write.

**Upload size on Cloud Run.** Cloud Run limits HTTP/1 request bodies. On Nov 11, a 31 MiB upload to the test service returned `201`. A 33 MiB upload returned `413` with an HTML page from Google's front end, so the service never saw it. The deployed service's real limit is therefore about 32 MiB, and above it the caller gets neither the shared body nor `file_too_large`. Locally, the limit is Dropbox's 150 MB. Open question 13 tracks this.

### Configuration, credentials, and data exposure

The service builds its storage client in `build_storage_client()` at startup. Missing Dropbox credentials stop the service from starting, with one sanitized line that names the missing setting but never its value. On Cloud Run, the three Dropbox credentials come from Secret Manager. They are never in the image, the repository, requests or logs. `MAX_IN_FLIGHT_REQUESTS` (10) sets the per-instance limit; excess requests receive `503 service_busy` straight away instead of waiting in a queue.

The service never logs file contents, paths, query strings or headers. Quoted text in failure reasons is replaced with `…`. Cloud Run's own request log records full URLs, query strings included, and so contains paths; it is readable only by project members. Download responses return the file's bytes, and metadata responses include IDs and paths, by design.

The service doesn't authenticate its callers: both Cloud Run services allow unauthenticated requests, and anyone with the URL can use the environment's Dropbox app folder. Each environment has its own Dropbox app, so test can't reach demo's files. This is accepted for coursework (see [Review](#review)).

## Deployment as built

The Nov 4 plan proposed these tools; all were built as planned except telemetry (change 7). Details: [Infrastructure](docs/infrastructure.md), [Release Pipeline](docs/release-pipeline.md), [Operations](docs/operations.md).

| Need | Tool | As built |
|---|---|---|
| Artifact | Docker image from `uv.lock` (`uv sync --locked`) on Python 3.14 | Tagged with the full commit SHA, never `latest`, and deployed by digest. The image sets `RELEASE` to the commit SHA. Images are about 53–70 MB. |
| Registry | Google Artifact Registry, `us-central1` | A cleanup policy keeps the 5 most recent versions. 300 MB stored on Nov 11. |
| Hosting | Cloud Run: `cloud-storage-service-test` and `cloud-storage-service-demo` | 1 vCPU, 512 MiB, scale to zero, at most 2 instances, 20 concurrent requests per instance against our limit of 10. Startup and liveness probes call `GET /health` only. |
| Secrets | Secret Manager: `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, `DROPBOX_REFRESH_TOKEN` per environment | Each runtime service account can read only its own environment's three secrets. |
| Infrastructure as code | OpenTofu with the Google provider; state in a versioned Cloud Storage bucket | Applied Nov 11 by the project owner. CI runs `tofu fmt -check` and `tofu validate` on every push. |
| Pipeline | CircleCI, signing in to Google Cloud through workload identity federation (OIDC) | No Google key is stored anywhere. Only pipelines on `hw2` and `main` can get the deployer identity. Each release is deployed with no traffic, smoke-checked against real Dropbox, then given 100% of traffic. |
| Telemetry | Cloud Logging, Cloud Trace, Cloud Monitoring | One JSON log line per request; traces with Dropbox spans; three log-based metrics; one dashboard; one 5xx alert emailed to the team; an uptime check per environment (the synthetic probe). |

**Identities.** CircleCI's `storage-deployer` service account can push images and deploy the two services. It can't read secret values, change IAM, or create or delete resources. Each service runs as its own runtime account.

**Capacity.** The overload check ([Overload Protection](docs/overload-protection.md)) confirmed that requests beyond 10 per instance get `503 service_busy` in the shared body, not a Cloud Run error. At most 2 instances bound both cost and the rate of calls to Dropbox.

### Cost, ownership and teardown

Measured from the project's own usage, Nov 5–11, 2026 (test was deployed from Nov 11; demo served only the placeholder until its first deploy, late on Nov 11). Free tiers are from Google Cloud's pricing pages.

| Item | Measured use | Free tier | Cost |
|---|---|---|---|
| Cloud Logging ingestion (request logs, our JSON lines, Cloud Run system logs) | 2.9 MB in 7 days | 50 GiB per project per month; then $0.50/GiB | $0 |
| Cloud Monitoring: our 3 log-based metrics (charged by bytes ingested) | ≈ 0.015 MiB in 6 days (487 points) | 150 MiB per billing account per month; then $0.258/MiB | $0 |
| Cloud Monitoring: Cloud Run's own metrics | — | Google Cloud metrics aren't charged | $0 |
| Cloud Trace | 297 spans in 7 days | 2.5 million spans per month; then $0.20/million | $0 |
| Alerting (1 policy) | — | Charges for alerting policies start Sept 1, 2027, after teardown | $0 |
| Uptime checks (the synthetic probe, 2) | about 52,000 runs a month for both (3 checkers every 5 minutes on each) | 1 million runs per project per month; then $0.30 per 1,000 | $0 |
| Cloud Run | 492 s of billable instance time on test, 24 s on demo | 180,000 vCPU-seconds, 360,000 GiB-seconds and 2 million requests per month | $0 |
| Artifact Registry | 300 MB stored | 0.5 GB per month | $0 |
| Secret Manager | 6 secret versions | 6 active versions per month | $0 unless a rotation leaves old versions enabled |
| OpenTofu state bucket | a few KB | 5 GB-months in `us-central1` | $0 |

**Total so far: $0.** Telemetry ingestion is the item that grows with traffic: at about 10 times the measured rate, a full month is still under 0.5% of the logging free tier. The main risks are a load test that runs for hours, or a rotation that leaves old secret versions enabled. A **$5 budget alert** emails the owner either way.

| Owner | Responsibility |
|---|---|
| Shreyas Kaldate | Google Cloud project `ospsd-team3` and its billing account (his own card); the budget alert; `tofu apply` and `tofu destroy` |
| Team 3 | The Dropbox apps for test and demo, which are outside OpenTofu |

**Teardown: Dec 18, 2026**, after HW2 grades are returned, following [Teardown](docs/teardown.md): `tofu destroy`, delete the state bucket, delete the Dropbox apps by hand, then delete the project. The CircleCI project stays, because HW1 uses it.

**Alternatives considered** (unchanged from Nov 4): Render's free plan stops after 15 minutes idle and takes about a minute to wake, which would fail smoke checks and distort the latency SLI. GitHub Actions could also sign in without a key, but moving our CircleCI jobs adds work without adding a capability.

## Migration

The Nov 4 work breakdown, with its outcomes.

| Task | Owner(s) | Outcome on Nov 11 |
|---|---|---|
| Workspace and Python 3.12 | Shreyas | Python 3.12 done. The shared Python package is not pinned yet (change 5, open question 1). |
| Dropbox adapter: replace with `update(rev)`, path rule, `check_health()`, new metadata model, upload limit | Zesan, Jing | Done. The stale-rev behaviour is verified live, and the upload limit is `413 file_too_large`. |
| Shared HTTP routes and error handlers (Decision 3) | John (routes), Victor (error handlers) | Done: all shared routes, the 400/404/405/500 handlers, and root delete `422`. 293 service unit tests. |
| Conformance suite and docs | Shreyas | The suite runs in CI (55 passed). `README.md` and `TUTORIAL.md` were updated for the shared routes on Nov 11, with the tutorial run against the deployed test service. |

**Early end-to-end testing**, as planned: `scripts/consumer_workflow.py` runs the vertical's shared workflow against every new revision before it gets traffic. The steps are upload, metadata, download and compare bytes, list, move, metadata at the new path, delete, then a `404`. Two faulty releases were stopped this way ([Rollback Drill](docs/rollback-drill.md)).

**Incompatible contract changes.** If a defect in the shared contract is found:

1. Affected consumers are Team 1 (Dropbox) and Team 6 (S3/OCI).
2. Whoever finds it doesn't patch the contract alone. They open an issue on `Dayana20/ospsd26-cloud-storage` and start an async review in `#document-storage`. Approval comes from the coordinator (Dayana, Team 1) and the implementation leads (Yashwanth, Team 1; Jing Qian, Team 3; Debasmith Mandal, Team 6).
3. The coordinator tags a new version and posts the migration diff. Each team updates its pin and adapts its service.

## Testing

| Layer | What it checks | Where | Count (Nov 11) |
|---|---|---|---|
| Fast unit tests | Routes, error mapping, telemetry, overload and faults, with `FakeStorageClient`; Dropbox error translation, with a mocked SDK | `src/*/tests/` | 586 (293 in the service); coverage at least 85% |
| Live (real Dropbox) | Provider behaviour we depend on, in uniquely named scratch folders deleted afterwards. Skipped without credentials. | `tests/integration/` | 23 |
| Conformance | Our HTTP behaviour matches the vertical's | CI job `conformance` (in-memory client), and by hand against the deployed test service | 55 passed in both |
| Release smoke check | The shared workflow against each new revision, with real Dropbox, before it gets traffic | `scripts/deploy.sh` | Every deploy |

### Guarantees and how we check them

| Guarantee | Check |
|---|---|
| Create never overwrites, even identical bytes | Live: `test_upload_onto_existing_file_without_overwrite_is_409`, `test_repeating_an_identical_upload_is_409` |
| Replace returns `404` for a missing file and refuses a concurrent change | Live: `test_replace_missing_file_raises_and_creates_nothing`, `test_replace_against_stale_revision_is_a_conflict` |
| `id` stays the same across move and replace | Live: `test_rename_then_move_round_trip`, `test_replace_existing_file_changes_content_and_keeps_id` |
| Move never overwrites; a folder can't be moved into itself | Live: `test_missing_source_and_conflict`, `test_folder_moved_into_itself_is_invalid_operation` |
| List is sorted by path | Fast: `test_list_folder_is_sorted_by_path`; live: `test_list_folder_contents_and_errors`; conformance |
| `modified_at` is UTC with `Z` | Fast: `test_from_file_converts_modified_time_to_utc`, `test_modified_is_timezone_aware_utc` |
| Delete removes folder contents; deleting again is `404`; the root is never deleted | Live: `test_delete_folder_removes_contents`, `test_delete_file_then_repeat_returns_404`; fast: `test_deleting_the_root_returns_422_and_changes_nothing`, `test_delete_root_is_rejected_without_calling_dropbox` |
| Every failure uses the shared body | Fast: failure tests in each route test file, `test_unknown_route_returns_404_route_not_found` |
| Provider health reports Dropbox being down, and `/health` never calls it | Fast: `test_provider_health_is_503_when_dropbox_fails`, `test_health_returns_200_even_when_provider_is_down`; live: `test_check_health_succeeds_against_real_dropbox` |

### Concerns and gaps

- **Accepted: the 150 MB Dropbox limit isn't tested against real Dropbox**, because uploading such a file in every run is too slow. Our size mapping is tested with a mock. On Cloud Run the effective limit is about 32 MiB anyway (open question 13).
- **Accepted: a folder delete that fails partway** can only be simulated with a mock.
- **Accepted: the replace window** (Decision 1) isn't tested, because it needs another client to delete the file between our two calls.
- **Gap: the conformance suite runs against the deployed service only by hand**, not in the pipeline. The pipeline runs the shared workflow instead.

## Open questions and review

"Memo Q" numbers refer to the open questions in the Oct 28 memo.

### Open questions

| # | Question | Status on Nov 11 | Owner |
|---|---|---|---|
| 1 | Depend on the shared contract package (memo Q11) | **Done in HW3.** `cloud_storage_api` `0.3.1` is pinned, `DropboxDocumentStorage` implements it (release `v0.3.0`), and the composed workflow saves through it. The HTTP routes still use our `Client`. | Shreyas |
| 2 | 409 or 422 for an occupied destination or a stale replace (memo Q3) | **Settled: `409 conflict`**, as in contract `0.3.1` | Darsh (Team 6) |
| 3 | One code name per status | **Settled:** contract `0.3.1`'s error table, which `errors.py` follows | Darsh; John and Victor implemented |
| 4 | 404 means only a missing file; a 502/503 after a write means the outcome is unknown (memo Q8) | **Implemented** (`route_not_found`; no retries) | Darsh |
| 5 | Status for a folder delete that fails partway (memo Q6) | **Settled: `502 provider_error`**, as in contract `0.3.1` | John |
| 6 | What deleting the root returns | **Implemented: `422 invalid_operation`**, observed on the test service. The shared contract doesn't state it. | John, with Laya (Team 1) |
| 7 | Replace can recreate a file deleted between its two calls | **Accepted risk**, still unobserved (Decision 1) | Zesan |
| 8 | Dropbox's maximum path length | **Open.** Not measured. | Victor |
| 9 | Upload limit unit and status (memo Q9) | **Settled: `413 file_too_large`**, at Dropbox's 150 MB. See 13 for Cloud Run. | Zesan |
| 10 | `modified_at` in UTC with `Z` | **Settled**, in contract `0.3.1` and implemented | Jing |
| 11 | What Dropbox returns for a stale rev with `autorename=False` | **Settled:** `path/conflict/file`, observed live on Nov 8 | Zesan |
| 12 | Keep `POST /files/rename` as an extension | **Settled: kept**, with the other extensions (change 1) | Team 3 |
| 13 | Cloud Run refuses uploads above about 32 MiB with an HTML `413`, before the service sees them | **Open, new.** Options: document 32 MiB as our deployed limit and tell Teams 1 and 6; or check size in the service below 32 MiB so callers get `413 file_too_large` in the shared body, for requests that declare their length; or enable HTTP/2 end to end, which Cloud Run doesn't limit the same way (unverified). | Zesan |

### Review

| Review | Reviewer | Scope | Status on Nov 11 |
|---|---|---|---|
| Whole design | Victor Wang (Team 3) | Every section | Not recorded yet |
| Contract-facing parts | Darsh (Team 6) | API and data model, failure behavior and open questions | Not recorded yet |

Each review states what the reviewer checked, for example "compared every route and status in this document with contract `0.3.1` and the observed responses". Reviewers comment on the pull request that updates `DESIGN.md`. Each substantive comment gets a disposition below: a change with a link, an answer, or a reasoned decision to keep the approach. Resolving a thread alone is not a disposition.

| # | Comment and reviewer | Disposition | Link |
|---|---|---|---|
| — | No comments recorded yet | — | — |

**Risks we propose to accept** (reviewers confirm or reject each one):

- Replace can recreate a file deleted between its two Dropbox calls (Decision 1, question 7).
- The 150 MB limit isn't tested against real Dropbox, and a partial folder delete is simulated only with a mock.
- Dropbox treats paths that differ only in letter case as one file. The contract documents this, and we don't work around it.
- The deployed services don't authenticate callers. Anyone with a URL can read and write that environment's Dropbox app folder until teardown on Dec 18.

**Scope of approval.** Approval accepts this design at the reviewed revision. It doesn't show that the implementation is correct; the conformance suite, the live tests and the release smoke check do that.

## Conclusion

Team 3's service now implements the vertical's shared HTTP contract: one route per shared operation, one file-info object, and one error body with the contract's status codes. It passes the shared conformance suite, and it is deployed to the test environment through a pipeline that checks each release against real Dropbox before it serves traffic. During implementation we kept the HW1 upload and move routes as extensions, restricted `PATCH` to files to match the suite, added `etag` and `413`, and moved telemetry to Google Cloud. Three things remain: pinning the shared Python package, deciding how to handle Cloud Run's 32 MiB upload limit, and the first deployment of demo from `main`.
