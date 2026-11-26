# course-autopilot-store

Course Autopilot's persisted state (HW3 Milestone 2): the tables, their Alembic migrations,
and `Database`, the small class the services use to reach Postgres.

| Table | Holds |
|---|---|
| `users` | One account per person, keyed by their sign-in provider's stable ID |
| `provider_connections` | A user's Dropbox, Trello or Google Calendar access, encrypted |
| `conversations`, `messages` | The agent conversation, turn by turn |
| `proposals`, `approvals` | A change set the agent proposed, and the approval of its exact content (by hash) |
| `change_outcomes` | What happened to each change: completed, failed or uncertain |
| `external_records` | Every provider record the product created, so it only ever changes its own |

`Database.upgrade()` applies pending migrations; the API service calls it at startup. To
change the schema, change `models.py` and add a migration in `migrations/versions/`; the test
`test_the_migration_matches_the_models` fails if the two disagree.
