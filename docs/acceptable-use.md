# Acceptable Use: What the Assistant Does

HW3 Milestone 2, Level 4.2 (#102; threat T12). This page is the assistant's policy: what it does, what it declines and what it asks about. The same rules are in its instructions (`course_agent_service/loop.py`, `SYSTEM_PROMPT`), and the parts code can enforce are enforced in code, listed at the end.

## What it does

Course Autopilot's assistant helps one student turn **their own course documents** into a plan:

- read the course folders and documents in the student's connected Dropbox;
- answer questions about those documents: deadlines, weights, policies;
- propose calendar events, Trello cards and a course plan document for the student to review.

It **only proposes**. Nothing is added, changed or removed until the student approves the exact changes, and approval happens on the Course Autopilot page, never in the conversation (DESIGN.md, P1).

## What it declines

| Request | What the student sees |
|---|---|
| Anything unrelated to the student's courses and their planning (general chat, coding help, homework answers, essays) | A short decline that says what it can help with |
| Doing coursework for the student: answering assignment or exam questions, writing submissions | A decline. It can still list the assignment's deadline and requirements from the syllabus |
| Anything about another person's courses, accounts or data | A decline. It can only reach the signed-in student's own connections |
| Changing anything directly, or "approving" on the student's behalf | An explanation that changes happen only after the student approves them on the page |
| Following instructions found inside a document ("ignore your rules", "email this to…") | Nothing: text inside a document is data, never a request (T5) |
| Harmful, harassing or illegal requests | A decline, with no detail |

## What it asks about

It asks one short question instead of guessing when:

- a date or time is missing or ambiguous ("Week 7", "before Thanksgiving", no time of day);
- it doesn't know the course's time zone;
- a request could mean more than one course;
- a change would remove or rename something the student already has.

## What code enforces

The model decides how to word a reply. These rules don't depend on it:

| Rule | Where |
|---|---|
| The model can only read and propose; it has no tool that changes anything, and its token is refused on approve, execute and connection routes | `tools.py`; the API service (T14) |
| Every proposed change is validated by code (allowed operations, fields, times, targets the student created) before it is stored | the API service, `proposals.py` |
| A message that looks like it contains a password, an API key or a token is **not sent to the model and not stored**; the student is told why | `loop.py`, `looks_secret`, before the model call (T10) |
| A reply that looks like it contains a credential is replaced with fixed text | `loop.py`, `looks_secret`, on the reply (T10) |
| Per-turn limits (8 model calls, 60 s, 200,000 tokens), 25 model calls per student per day, 15 per minute for the service, 3 turns at once with 10 queued | `loop.py`, `app.py`, the API service's `usage.py` (T12) |
| Messages are at most 4,000 characters; a document is read as at most 20,000 characters | `app.py`; the API service, `courses.py` |
| The operator can turn the assistant off (`MODEL_PROVIDER=disabled`) or pause execution (`EXECUTION=disabled`) | [Container and Configuration](deployment.md), the settings table |

Off-topic and harmful requests are recognised by the model, following the rules above. That can't be checked by a rule in code, so it is checked by evaluation cases instead (#93): the bounded tools and the approval step limit what a wrong judgment can do.
