# Course Autopilot: product brief

**Team:** Team 3 (John Ashik, Shreyas Kaldate, Zesan Rahman, Jing Qian, Victor Wang)
**Status:** Draft for team review, Nov 24, 2026. Sections marked **[needs evidence]** are filled in after discovery (task 1.2) and the assumption tests (tasks 1.4 and 1.5).
**Tracker:** [Team 3 HW3 product](https://github.com/users/Shreyas191/projects/2)

**Format: Request for Comments.** We state the problem and our proposal, review it as a pull request, and record each decision with its owner. We chose this format because the whole team needs to agree on the product before building it, and because pull-request review is how we already review DESIGN.md.

## Problem

> **[needs evidence]** Replace this illustrative story with a real one from discovery.

A student takes four courses. In week 6, one professor moves the HW4 deadline from Friday to Wednesday in a course announcement, and another uploads a revised syllabus that adds a quiz. The student copied every deadline into their calendar in week 1, so both entries are now wrong. They learn about the change two days before the new deadline, when a classmate mentions it.

Students copy deadlines once, at the start of the semester. After that, a deadline change reaches them as an edited PDF or one line in an announcement, and their calendar is only right if they noticed. Nothing keeps the copy in sync with the source.

## User

University students taking three to five courses, each with a published syllabus and fixed deadlines, who keep their own calendar and to-do list.

**Not for:**
- **Instructors.** They publish deadlines; they don't need to track them.
- **Research or open-ended projects.** Their deadlines are flexible or undefined, and the agent can't verify a date that was never set.
- **Courses without published deadlines.** There is nothing to sync.

**Is this only our team's problem?** **[needs evidence]** Answered from the discovery notes.

## Evidence

**[needs evidence]** Discovery (task 1.2). For each conversation, we record what the person said (quoted where possible), what we observed, and what we are assuming, and we link the notes. The two questions we ask:

1. How do you keep track of deadlines across your courses today?
2. Has a deadline changed after you wrote it down? How did you find out?

## Key scenarios

### S1: Set up a course (uses all three verticals)

1. The student uploads a course syllabus (PDF) in the interface. It is saved to their Dropbox.
2. The agent reads it and lists every graded item with its date and weight. When a date is unclear ("Midterm: Week 7"), it asks: "Week 7 starts Mon Oct 12. Is the midterm on Tue Oct 13 or Thu Oct 15?" It never guesses a date.
3. The agent proposes one change set: a Trello card per assignment, a Google Calendar event per deadline, and a course plan document in Dropbox listing every deadline and its weight.
4. The student approves, edits or rejects the change set. After approval, the agent creates the records, checks each one, and shows links. If one step fails, it reports what was created and what wasn't.

### S2: Sync a change

1. Weeks later, the student uploads a revised syllabus or pastes an announcement ("HW4 is now due Wednesday Nov 4").
2. The agent compares it with what it created for that course and proposes only the differences: "Move HW4 from Fri Nov 6 to Wed Nov 4. Add Quiz 3 on Nov 12. Remove Lab 6 (cancelled)."
3. The student approves. The agent updates or deletes only records it created itself, and adds a "what changed" entry to the course plan document.

### S3: Warn about a heavy week (should)

1. With several courses set up, the agent finds weeks where deadlines pile up, for example three assignments worth 30% of their grades due within two days.
2. It proposes "start early" cards with earlier target dates. The student approves or dismisses them.

## Scope

| Priority | Feature | Why |
|---|---|---|
| **Must** | S1: set up a course from a syllabus PDF, with clarifying questions for unclear dates | The core job |
| **Must** | S2: sync a revised syllabus or a pasted announcement as one change set | The product's reason to exist (see Problem) |
| **Must** | Approval before every create, update or delete; a report of completed, failed and uncertain changes | Required by the spec, and wrong deadlines are worse than none |
| **Must** | Sign-in, with each student's own Dropbox, Trello and Google Calendar | Required by the spec; deadlines are personal data |
| **Should** | S3: heavy-week warnings across courses | Valuable, but only after S1 and S2 work |
| **Should** | A course plan document that records each change | Lets the student see what the agent did and why |
| **Won't** | Read the learning platform (Brightspace, Canvas) directly | No access for students to build on, and it would add a fourth system to authorize. The student uploads or pastes instead |
| **Won't** | Read email announcements | Same reason; the student pastes the text |
| **Won't** | Schedule study time around the student's other events | The shared calendar contract can't list events or free time, so the agent can't see conflicts |
| **Won't** | Track progress or mark assignments done | The shared issue-tracker contract has only an ID and a title: no status or due date. A deleted card means done |
| **Won't** | Calculate grades | A different job, with different users and accuracy needs |
| **Won't** | Group projects or shared plans | One student's courses only; shared plans need team permissions we can't build safely by Dec 2 |

## Success metrics

Measured during launch week (Dec 2–6) from our own records and telemetry.

| Scenario | Outcome we measure | Target **[to confirm]** |
|---|---|---|
| S1 | Share of extracted deadlines that match the syllabus, checked by the student when they approve or edit | ≥ 90% |
| S1 | Share of setup change sets approved without edits | ≥ 60% |
| S2 | Share of proposed syncs approved (with or without edits) | ≥ 70% |
| S2 | Share of real changes in the uploaded document that the proposal includes, checked against a hand-made answer key in evaluations | ≥ 90% |
| S3 | Share of warnings the student acts on (approves the "start early" cards) | Measured, no target yet |

**Guardrails** (the agent must never break these; one violation is a launch blocker):
- No create, update or delete without the student's approval of that exact change.
- Never change or delete a record the agent didn't create.
- Never invent a date: an unclear date is a question, not a guess.
- Never show or change one student's data for another.
- Never report a change as done unless it was checked with the provider.

## Assumptions and risks

Ranked by how much damage a wrong assumption would cause.

| # | We assume | If wrong | How we test it |
|---|---|---|---|
| A1 | The model extracts deadlines from real syllabi accurately enough to propose them (≥ 90% correct) | The product fails at its core job | Task 1.4: 3–4 real syllabi from our courses, scored against a hand-made answer key. **[needs evidence]** |
| A2 | Deadlines change after the start of the semester often enough that students miss changes | S2 has no point, and the product becomes a one-time importer | Task 1.5: discovery questions, plus counting changes in this semester's course announcements. **[needs evidence]** |
| A3 | Students prefer approving one change set to having the agent act alone or only suggest | Approval feels like friction, or students ignore proposals | Discovery, then the approval rate in launch week |
| A4 | Team 4 releases per-user credentials for their Trello package in time | We use a workaround with weaker isolation, recorded as a limitation | Request sent Nov 23; issue on our board |
| A5 | Syllabi are text PDFs, not scans | Extraction needs OCR, which we don't plan to build | Checked with the syllabi from A1 |
| A6 | The free model tier covers launch week (a few hundred model calls a day) | Requests fail or we pay for the model | Our project's limits in AI Studio; funding decision by Nov 28 |
| A7 | Google OAuth in Testing mode works for launch-week users | Every user must be added by hand, and tokens expire after 7 days, during launch week | Add staff and launch-week users as test users; reconnect flow (task 5.6) |

## Open questions

| # | Question | Owner | Decide by |
|---|---|---|---|
| Q1 | Is "three to five courses" the right segment, or do students take more? | _to assign_ | Nov 25 (discovery) |
| Q2 | How should a deadline look in the calendar: an event at the due time, or a block before it? | _to assign_ | Nov 25 |
| Q3 | Which time zone do we assume when a syllabus gives none: the student's, set once at sign-up? | _to assign_ | Nov 25 |
| Q4 | Interface: web or CLI? | _to assign_ | Nov 25 (task 3.1) |
| Q5 | Do we pay for model capacity in launch week, and which provider? | _to assign_ | Nov 28 |
| Q6 | If Team 4's change doesn't arrive by Nov 26, do we use the workaround or ask for another option? | _to assign_ | Nov 26 |
