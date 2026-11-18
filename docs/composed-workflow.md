# Composed Workflow: Bug Report

`POST /workflows/bug-report` is the HW3 composed operation. One request runs three fixed steps, each through a shared contract, and returns one combined result:

| Step | Vertical | Contract | Implementation (chosen by configuration) | What it does |
|---|---|---|---|---|
| 1 `storage` | Cloud storage | `cloud_storage_api` 0.3.1 `DocumentStorage` | Ours: `DropboxDocumentStorage` | Saves the report as `/bug-reports/<report_id>.md` |
| 2 `issue_tracker` | Issue tracker | `issue-tracker-contract` v1.1.0 `IssueProvider` | `ISSUE_TRACKER_IMPL`: `team4` (Team 4, Trello), `fake`, `failing` | Creates the issue `[bug <first 8 of report_id>] <title>` |
| 3 `calendar` | Calendar | `calendar-contract` v0.1.0 `CalendarService` | `CALENDAR_IMPL`: `team5` (Team 5, Google Calendar), `fake`, `failing` | Books `Triage: <title> (issue <issue id>)`, tomorrow 15:00–15:30 UTC |

No language model is involved. The settings and the credentials each implementation needs are in [Container and Configuration](deployment.md).

## Request

```console
$ curl -s -X POST "$B/workflows/bug-report" -H 'Content-Type: application/json' -d '{
    "title": "Login fails after password reset",
    "description": "Reset the password, log in with the new one: 500.",
    "reporter": "victor"
  }'
```

| Field | Rule |
|---|---|
| `title` | 1–120 characters, not blank |
| `description` | 1–10,000 characters, not blank |
| `reporter` | 1–100 characters, not blank |

Any other field, or a field that breaks a rule, is `400 invalid_request`, and no provider is called.

## Success: `201`

```json
{
  "report_id": "c34bddd9271c4919b0d120ccb9184e64",
  "file": {"id": "id:…", "name": "c34bddd9271c4919b0d120ccb9184e64.md", "path": "/bug-reports/c34bddd9271c4919b0d120ccb9184e64.md", "size": 214, "modified_at": "2026-11-17T03:10:42Z", "etag": "…"},
  "issue": {"id": "<card id>", "title": "[bug c34bddd9] Login fails after password reset"},
  "event": {"id": "…", "title": "Triage: Login fails after password reset (issue <card id>)", "start": "2026-11-18T15:00:00+00:00", "end": "2026-11-18T15:30:00+00:00"}
}
```

Each record points to the one before it: the issue title carries the report ID, and the event title carries the issue ID.

## Failure: the error names the step

A failed step returns the service's usual error body with four extra fields:

```json
{
  "error": {
    "code": "provider_unavailable",
    "message": "The calendar is unreachable; try again later",
    "step": "calendar",
    "report_id": "2e91d2fa4a2345418e385e72a6a5986d",
    "undone": ["issue 6afbbdfbedd543f0ddc0fd41", "file /bug-reports/2e91d2fa4a2345418e385e72a6a5986d.md"],
    "left_behind": []
  }
}
```

| Failed step | Cause | Status | `code` |
|---|---|---|---|
| any | missing or invalid field | 400 | `invalid_request` |
| `storage` | a shared `cloud_storage_api` error | its `status_code` and `code` (e.g. 409 `conflict`, 503 `provider_unavailable`, 502 `provider_error`) | |
| `issue_tracker` | any `ServiceError` | 502 | `provider_error` |
| `calendar` | `provider_unavailable` | 503 | `provider_unavailable` |
| `calendar` | any other `CalendarError` | 502 | `provider_error` |

Messages are fixed per case, so provider details never reach the caller; they are logged with the request.

## Partial failure: undo, then report

- **Undo.** When a step fails, the steps that already succeeded are undone, newest first: the issue is deleted, then the file. `undone` lists what was removed. A failed run therefore never claims success and leaves nothing half-done.
- **Retry is safe.** A retry starts from nothing with a new `report_id`, so it does not duplicate the earlier run's records. The workflow never retries a write by itself.
- **When something may remain, it says so.** `left_behind` is not empty only when:
  - an undo itself failed (for example, the issue tracker was down when deleting the issue), or
  - the failed step was an *uncertain write*: the request may have reached the provider although no answer came back. These are an issue-tracker failure with `may_have_applied`, and the calendar contract's non-retryable `provider_error`.

  For storage, the report's path is known, so an uncertain upload is removed. A client error such as `409 conflict` never deletes anything. Every record carries the `report_id` (in the file name, the issue title, or through the issue ID in the event title), so a stray record can be found by it.

## Seeing a failure on purpose

On the **test** service, set `CALENDAR_IMPL=failing` (or `ISSUE_TRACKER_IMPL=failing`) and call the endpoint: the response is the error above. `failing` is refused on the demo service.

## Verified against real providers

Run locally on Nov 17 with our Dropbox and Team 4's Trello implementation on our test board (`ISSUE_TRACKER_IMPL=team4`), calendar `fake`:

- `201`: the file existed in Dropbox and the card on the Trello board (both removed afterwards).
- With `CALENDAR_IMPL=failing`: `503 provider_unavailable`, `step: calendar`. Afterwards the card was gone from Trello (`not_found`) and the file gone from Dropbox.
