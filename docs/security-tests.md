# Security Tests

HW3 Milestone 2, Level 4.1 (#108). Each threat in DESIGN.md's threat table has an automated test with two users, and each refusal has a **control** next to it: the legitimate request that succeeds. So a system that refused everything couldn't pass. All of them run in CI (`pytest src/ tests/`), with no live model and no real provider.

**The suite** is [`tests/test_security.py`](https://github.com/Shreyas191/OSPSD-Team-3/blob/hw2/tests/test_security.py). Alice and Bob are signed in with real sessions, and each has their own Dropbox, Trello and Google Calendar connection, stored encrypted. Their provider accounts are in memory behind fake connectors, so a test can see whose account a read or write reached. The API service, the agent service (holding only delegated agent tokens) and the interface are the real apps; only the model is scripted. The suite also checks, after every test, that our own test accounts were never touched by a signed-in person.

| Threat | In the suite | Deeper tests |
|---|---|---|
| **T1** Another user's courses and plan | `test_t1_bobs_courses_are_invisible_to_alice`: Bob's documents 404, his plan empty, not in Alice's list (control: Alice's own documents) | `test_course_routes.py`; `test_connections.py::test_reads_reach_only_the_persons_own_dropbox` |
| **T2** Another user's proposal | `test_t2_bobs_proposal_cant_be_approved_or_run_by_alice`: 404, nothing written (control: Bob approves) | `test_proposal_routes.py` (read, approve, reject, execute); `test_agent_with_api.py::test_another_users_conversation_stays_hidden` |
| **T3** A proposal edited after approval | `test_t3_a_proposal_edited_after_approval_doesnt_run`: content changed directly in the database, without its stored hash → 409, nothing runs (control: an unedited proposal runs) | `test_proposal_routes.py` (approving with another hash) |
| **T4** Replayed approve or execute | `test_t4_a_replayed_approval_or_execution_writes_nothing_more`: both 409, one write | `test_proposal_routes.py` (retry re-runs only failed changes; a run taken over is never repeated) |
| **T5** Instructions in a document | `test_t5_a_poisoned_syllabus_cant_delete_anything`: the scripted model reads a poisoned syllabus and insists on deleting an event we didn't create: no proposal, event intact (control: a clean proposal is stored). The **real model** on poisoned documents is #104 | `test_agent_with_api.py::test_a_forced_delete_of_a_record_we_didnt_create_is_refused` |
| **T6** Another user's IDs in a tool argument | `test_t6_bobs_ids_in_a_tool_argument_reach_nothing`: no tool has a user field; Bob's card ID from Alice's agent is refused (control: Bob's agent may propose it) | `test_agent_with_api.py::test_a_forced_change_to_another_users_record_is_refused`; `test_proposal_routes.py` |
| **T7** A stolen session | `test_t7_a_session_ends_on_sign_out_and_everywhere`: 401 after sign-out; sign out everywhere ends the other device (control: Bob unaffected) | `test_sign_in.py` (idle and total expiry, agent tokens ending with their session) |
| **T8** Cross-site form posts | `test_t8_a_cross_site_form_without_the_csrf_value_does_nothing`: the interface, wired to the real API, refuses a form without its CSRF value and never calls the API (control: the genuine form signs out) | `course_web/tests/test_sign_in.py` (cookie flags, missing cookie, wrong value) |
| **T9** A forged or reused OAuth callback | `test_t9_a_connection_finished_by_someone_else_connects_nothing`: Bob finishing Alice's attempt fails and uses it up (control: Alice connects) | `test_connections.py` (replay, other provider, expiry); `test_sign_in.py` (binding, replay) |
| **T10** Tokens in logs, responses or the model's context | `test_t10_no_token_reaches_a_log_a_response_or_the_model`: a whole S1, searching logs, responses and every model request for each session and provider token (control: the run record and the syllabus are there) | `test_connections.py::test_no_token_reaches_a_response_or_a_log_line`; `test_agent.py` (credentials screened from input and output) |
| **T11** Paths outside the course folder | `test_t11_reads_stay_inside_the_course_folder`: `..`, nested and absolute names, and a slash in the course → refused, nothing read (control: the syllabus) | `test_course_routes.py`, `test_path_validation.py` |
| **T12** Model budget and anonymous turns | `test_t12_over_the_daily_cap_is_429_and_no_session_is_401`: 429 and nothing saved; 401 with no model call (control: Bob, under his cap) | `test_usage.py` (per minute, Pacific day, 20 callers racing for 15 calls); `test_agent.py` (admission, queue) |
| **T13** A revoked connection | `test_t13_a_revoked_connection_never_falls_back`: reconnect message, nothing written to Alice's, Bob's or our accounts; **revoke, then reconnect** runs it (control) | `test_connections.py` (needs attention, refused refresh, secret copied between rows) |
| **T14** A compromised agent service | `test_t14_the_agents_token_cant_approve_or_execute`: 403 on approve and execute; execute without approval 409 (control: Alice approves) | `test_sign_in.py`, `test_connections.py` (every connection route), `test_agent_with_api.py` |

**Also asked for by #108:**

- **Restarting both services:** `test_restarting_both_services_keeps_sessions_connections_and_proposals`. With nothing in memory, only the database and the key, sessions, encrypted connections and a pending proposal all carry on, and it runs once.
- **Token refresh:** `test_a_refreshed_token_is_saved_and_the_old_one_isnt_used_again`. An expired token is refreshed once, stored encrypted, and the new one is used from then on.
- **Revoke, then reconnect:** the end of the T13 test.

## What the suite found

- **T3 held only for edits that also changed the stored hash.** Execute compared the approval with the proposal's stored `changes_hash` column, so content changed after approval by a bug or a direct database write, without the column, would have run. Execute now recomputes the hash from the content it is about to run and requires all three to match (`execution.py`, `_claim`).
- **T11's "course keys are compared case-folded" isn't implemented yet.** It's not a security hole: every path is built inside the course folder, and records are filtered by user. But `CS-GY 6083` and `cs-gy 6083` are treated as two courses in the course list and the plan. Tracked separately.
