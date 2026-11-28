# S1 Task Evaluation

HW3 Milestone 2, Level 3 (#103). Key scenario S1: a student asks Course Autopilot to set up a course from its syllabus, approves the proposal, and the deadlines land in their calendar and board. Success is judged by the **final provider records**, never by the model's summary.

## How it's measured

- **Labeled syllabi:** [`scripts/s1_eval_cases.json`](https://github.com/Shreyas191/OSPSD-Team-3/blob/hw2/scripts/s1_eval_cases.json). There are five synthetic syllabi with different layouts, written for this test before any run, each with the deadlines a correct setup creates:
  - `explicit`: dates and times written out;
  - `weeks`: week numbers from a stated first class;
  - `table`: a Markdown table;
  - `single`: one graded item;
  - `pacific`: an online section on Pacific time.
- **Initial state:** the syllabus in the course folder, plus an event ("Dentist") and a card ("Buy groceries") the student already had.
- **A run** ([`scripts/s1_eval.py`](https://github.com/Shreyas191/OSPSD-Team-3/blob/hw2/scripts/s1_eval.py)) plays the whole of S1 with the real model, against the real API and agent apps:
  1. The student asks for the setup.
  2. If the assistant asks a question, the case's labeled answer is given (at most twice).
  3. The person approves the proposal, and it runs.
- **Success** means all of the following:
  - every labeled item is an event on the right local date and time (an exam keeps its own start and end; a deadline's event starts or ends at the due time);
  - every item has a card;
  - there's a `course plan.md`;
  - nothing was updated or deleted, and the student's existing records are untouched.
- **Recorded per run** ([raw results](evidence/hw3-s1-evaluation.json)): success, wall time, model calls, tokens, turns, stop reasons and every failure. Also recorded: the model version (from the run record) and the **prompt version**, a hash of the system prompt and tool specs.

All runs below used `gemini-3.5-flash-lite` on the `test` key, on Nov 28, 2026, paced under the free tier's 15 calls per minute.

## Before: 0 of 5

Prompt version `41961ab4`. Wording: "Set up {course} from my syllabus."

| Case | Success | Time (s) | Model calls | Tokens | Failure |
|---|---|---|---|---|---|
| explicit | no | 39.4 | 7 | 15,456 | no proposal: three invalid proposals |
| weeks | no¹ | 43.3 | 8 | 15,349 | — (one question, then a proposal) |
| table | no | 38.1 | 7 | 13,505 | no proposal: three invalid proposals |
| single | no | 36.4 | 7 | 11,396 | no proposal: three invalid proposals |
| pacific | no | 38.4 | 7 | 13,869 | no proposal: three invalid proposals |

¹ Scored against a first, stricter rule that a deadline's event must *end* at the due time. The model's event started at it. The rule was widened (starts or ends) before any further run. Under it, `weeks` would pass.

## The failure, traced

The API's reasons for refusing the proposals, read in a verbose run:

1. New **cards** carried event and document fields (`start`, `end`, `name`, `content`), which `create_issue` doesn't take.
2. Next, deadlines became events whose **end equals their start**, which the calendar contract refuses.
3. The assistant also asked for a time zone the syllabus already stated ("all times Eastern").

The cause was the tool's description. `propose_changes` shows every field of every kind of change in one flat object, and nothing said which fields belong to which operation, or how to show something with no duration. The same two mistakes appear in the measured S1 turn of Nov 26 (readiness record) and in the adversarial evaluation's controls (#104).

## The fix

Wording only. Validation stays as strict as before, so a wrong proposal is still refused.

- **The `propose_changes` description** lists the fields each operation takes. For example: "create_event: title, start, end … create_issue: title only". It also says that an event's end must be after its start.
- **Deadlines:** "Something that happens over a time (an exam, a quiz, a class) keeps its own start and end. Only something due at a single time (an assignment, a report) becomes an event that ends at the due time and starts 30 minutes earlier."
- **Each field** says which operations it's for ("Events only", "write_document only").
- **Time zone:** use the zone the syllabus states (Eastern is America/New_York, Pacific is America/Los_Angeles), and ask only if it states none. This is in both the tool's description and the system prompt.

A first version of the deadline sentence ("for something due at a time, the event ends at the due time and starts 30 minutes earlier") got 3 of 5 (prompt version `f8ed0b1f`). It turned the `weeks` quiz ("first 30 minutes of class", 9:30) into 9:00–9:30, so the sentence was narrowed to what's due at a single time.

## After: 5 of 5

Prompt version `25fe2a7c`. **Fresh wording**: "Could you put everything graded in the {course} syllabus on my calendar and Trello board?"

| Case | Success | Time (s) | Model calls | Tokens | Failure |
|---|---|---|---|---|---|
| explicit | yes | 27.2 | 5 | 8,867 | — |
| weeks | yes | 26.7 | 5 | 8,586 | — (worked out the week dates without asking) |
| table | yes | 33.2 | 5 | 8,606 | — |
| single | yes | 25.5 | 5 | 8,176 | — |
| pacific | yes | 28.6 | 5 | 8,581 | — |

Every proposal was valid on its first try. Compared with before, each setup took 5 model calls instead of 7–8, about 40% fewer tokens, and about 28 s instead of 39 s.

**The same version with the original wording** got 4 of 5. So the improvement comes from the fix, not the different phrasing:

| Case | explicit | weeks | table | single | pacific |
|---|---|---|---|---|---|
| Success | yes | yes | yes | **no**: empty reply | yes |

## Still open

- **An intermittent empty reply.** In 2 of 15 runs since the fix, in different cases, the model returned no text and no tool call. The agent then stops with `empty_reply` ("I didn't get an answer from the assistant; please try again"), and nothing is written. Retrying the model call once before stopping would hide most of these; it's a small change to `loop.py`.
- **These are our own syllabi.** John's assumption test A1 (#85) used five real ones. The same harness can score them where the files are.
