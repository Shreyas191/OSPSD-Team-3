# HW3 swap run: the calendar implementation changed by configuration only

Recorded Nov 17, 2026 (19:08 ET) on the deployed **test** service, release image `rel-3d16d2f`. The only change was the OpenTofu setting for test, `vertical_implementations.test.calendar`, from `"team5"` to `"fake"`, which sets `CALENDAR_IMPL`. No code changed. The resulting revision, `cloud-storage-service-test-00039-7kp`, was reached through its no-traffic tag URL; live traffic stayed on the release. Test was then switched back to `team5`.

**Why `fake` rather than a second team's package:** the only other calendar teams are Team 7 (`baskd`) and Team 2, and neither publishes a package that installs next to Team 5's on Python 3.14. See DESIGN.md, HW3 open question H1. `fake` (`InMemoryCalendar`) is our controlled implementation of the same shared `CalendarService` contract, and it passes the contract's conformance suite.

## Request

```console
$ curl -s -X POST "https://hw3-swap---cloud-storage-service-test-gyesjhieva-uc.a.run.app/workflows/bug-report" \
    -H 'Content-Type: application/json' \
    -d '{"title":"Swap drill: calendar implementation from configuration","description":"HW3 Milestone 1 swap evidence: CALENDAR_IMPL changed from team5 to fake, no code change.","reporter":"shreyas"}'
```

## Response: `201`

```json
{"report_id":"4c29e775c78a477391fede40dd609f2e",
 "file":{"id":"id:KzOOg5ouvzUAAAAAAAACIw","name":"4c29e775c78a477391fede40dd609f2e.md","path":"/bug-reports/4c29e775c78a477391fede40dd609f2e.md","size":255,"modified_at":"2026-11-18T00:08:35Z","etag":"0166081cff08ea700000003923a8b33"},
 "issue":{"id":"6afcec84cbabeecf2e9d4896","title":"[bug 4c29e775] Swap drill: calendar implementation from configuration"},
 "event":{"id":"b7c017cebf8f4f969ab80ba62c52b3f8","title":"Triage: Swap drill: calendar implementation from configuration (issue 6afcec84cbabeecf2e9d4896)","start":"2026-11-19T15:00:00+00:00","end":"2026-11-19T15:30:00+00:00"}}
```

## How the swap shows in the result

| | Before (`CALENDAR_IMPL=team5`, [composition run](https://github.com/Shreyas191/OSPSD-Team-3/blob/hw2/DESIGN.md#hw3-how-we-verify-it)) | After (`CALENDAR_IMPL=fake`) |
|---|---|---|
| Event ID | `f2c9v7313ton9afoq0blku2b4k`: Google Calendar's format | `b7c017cebf8f4f969ab80ba62c52b3f8`: the fake's UUID |
| Times returned | `10:00:00-05:00`: Google answers in the calendar's time zone | `15:00:00+00:00`: the fake returns what it was given |
| Storage and issue tracker | real Dropbox and Trello | real Dropbox and Trello (the file and card were deleted after this run) |

The event is for Nov 19 because the run happened after midnight UTC on Nov 18, and the triage meeting is booked for the next UTC day.
