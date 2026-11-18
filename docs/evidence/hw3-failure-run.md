# HW3 failure run: the calendar vertical fails on purpose

Recorded Nov 17, 2026 on the deployed **test** service. Test was switched with OpenTofu to `ISSUE_TRACKER_IMPL=team4` (Team 4's Trello implementation, real) and `CALENDAR_IMPL=failing` (our controlled failing `CalendarService`). The resulting revision, `cloud-storage-service-test-00037-rgn`, was reached through its no-traffic tag URL; live traffic stayed on release `rel-3d16d2f`. Test was then switched back to `team5`.

## Request

```console
$ curl -s -i -X POST "https://hw3-failure---cloud-storage-service-test-gyesjhieva-uc.a.run.app/workflows/bug-report" \
    -H 'Content-Type: application/json' \
    -d '{"title":"Calendar outage drill","description":"HW3 Milestone 1 failure evidence: the calendar vertical is the controlled failing implementation.","reporter":"shreyas"}'
```

## Response: our documented error, naming the failed step

```
HTTP/2 503
content-type: application/json

{"error":{"code":"provider_unavailable","message":"The calendar is unreachable; try again later","step":"calendar","report_id":"83da2944d9bc42b7b0f7a36bdbd7684f","undone":["issue 6afcebc2a9d55942fff7113a","file /bug-reports/83da2944d9bc42b7b0f7a36bdbd7684f.md"],"left_behind":[]}}
```

Steps 1 (storage) and 2 (issue tracker) succeeded, step 3 (calendar) failed. The workflow undid step 2, then step 1, and reports both in `undone`. `left_behind` is empty.

## Checked with the providers afterwards

| Provider | Check | Result |
|---|---|---|
| Dropbox (test) | `GET /files/metadata?path=/bug-reports/83da2944d9bc42b7b0f7a36bdbd7684f.md` | `404`: the file is gone |
| Trello (our test board) | `TrelloIssueProvider().get_issue("6afcebc2a9d55942fff7113a", fresh=True)` | `ServiceError` `not_found`: the card is gone |
