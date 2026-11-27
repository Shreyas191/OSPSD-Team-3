# Assumption Tests

HW3 Milestone 2, Level 1 asks us to test the product's riskiest assumptions before building on them. Each test records the belief, why the plan depends on it, the test, what we observed and the decision it led to. The product brief links here.

## A1: the model extracts syllabus deadlines accurately (#85)

**Belief.** Given a syllabus and related course documents, our model records every explicit deadline with the right date and time, and when a date isn't given as a calendar date, it leaves the date empty and asks instead of guessing.

**Why the plan depends on it.** This is the brief's highest-ranked risk: a wrong calendar deadline can make a student miss work. Key scenario S1 (set up a course) has the model read the documents and propose every event and card. If extraction is unreliable, the product would have to fall back to the student typing their deadlines, which is the job it exists to remove. The brief's target is **at least 95% of explicit dates correct**, with **no guessed dates**.

### The test

- **Documents:** five real courses, chosen to cover different layouts. The files stay outside the repository (they are the instructors' material); only the answer key is committed.

  | Course | Documents | What it tests |
  |---|---|---|
  | CS-GY 6923 Machine Learning, Fall 2026 | Schedule and logistics PDFs (two documents) | **13 explicit deadlines** in a table, due 11:59 PM. Traps: numbering starts at Assignment 2; dates have no year; two items share Dec 1 |
  | OSPSD HW3 timeline, Fall 2026 | Pasted text | **3 explicit dates in prose**, due "before class" (no time given) |
  | CS-GY 6053 Foundations of Data Science, Fall 2025 | Syllabus PDF, mostly policy text | "8 homeworks, multiple due dates" with none given; midterm, proposal and project by week number only |
  | CS-GY 6613 Artificial Intelligence | Course outline PDF (slides) | Vague counts ("five to six homeworks", "four to five quizzes"), an "approximate" schedule with overlapping weeks |
  | Information Visualization | Syllabus (converted from DOCX to text) | Week numbers only; "five mini-projects" with four listed; a survey with no date |

  Together: **16 explicit dates** and **16 items with no usable calendar date**.
- **Answer key:** [`scripts/assumption_a1_key.json`](https://github.com/Shreyas191/OSPSD-Team-3/blob/hw2/scripts/assumption_a1_key.json), written by hand from the documents before any model run.
- **Script:** [`scripts/assumption_a1.py`](https://github.com/Shreyas191/OSPSD-Team-3/blob/hw2/scripts/assumption_a1.py). It reads each document the way the API service does: PDF text through pypdf, capped at 20,000 characters. It removes email addresses, links and phone numbers before anything reaches the model. It then calls our `GeminiModel` once per course, with the product's rules ("never guess a date; ask") and one tool, `record_deadlines`, that records each item's date, time and any date problem. The reply is scored against the key.
- **Model:** `gemini-flash-lite-latest`, which resolved to **`gemini-3.5-flash-lite`** on Nov 26, 2026, through the free tier. "Today" was set to 2026-11-26 and the time zone to `America/New_York`.
- **Runs:** every course 3 times (15 calls), plus one earlier full pass that gave the same scores.

Reproduce it with:

```bash
set -a; . ./.env; set +a          # GEMINI_API_KEY
uv run --with 'pypdf>=6.0' python scripts/assumption_a1.py \
    --documents ~/.config/ospsd-team3/syllabi --runs 3 --today 2026-11-26
```

### What we observed (Nov 26, 2026)

| Measure | Result |
|---|---|
| Explicit dates correct | **48 / 48 (100%)**: 16 dates × 3 runs |
| Times correct | **48 / 48**: 11:59 PM → `23:59` for all 39 Machine Learning items; left empty for all 9 HW3 milestones, whose time isn't stated |
| Unclear dates guessed | **0 / 48** |
| Unclear items flagged (empty date with the problem marked, or asked about) | **46 / 48** under strict scoring. The 2 misses are one AI-course run whose question asked about "2 exams" without naming Exam I and Exam II. That is a vague question, not a guess |
| Invented items | **0**. No run added an "Assignment 1"; one run asked whether it exists |
| Tokens per call | 1,727 to 4,163, mean 2,617 |
| Time per call | 0.95 to 8.92 s, median 1.33 s |

**What the scores don't show:**

1. **For a vague syllabus, the model records nothing and asks.** In all 3 runs of the AI course, it recorded **no items** and asked one broad question ("The syllabus mentions five to six homework assignments, four to five quizzes, two programming projects, and two exams (Exam I and Exam II), but no specific dates or titles are given for them…"), although Exam I (Week 8) and Exam II (final exam week) are named. That's safe, but the student gets a long question instead of a partial plan.
2. **A missing time is left empty but rarely asked about.** The HW3 milestones are due "before class". The model correctly left the time empty in 9 of 9 cases, but asked about it in **none** of the 3 runs, although the instructions say to ask. (Its one question was whether to record the optional Dec 16 extension.) A calendar event needs a time, so this can't be left to the model.
3. **Outdated syllabi aren't flagged.** The Data Science syllabus says Fall 2025; the model recorded "Fall 2025" correctly but never mentioned that it's last year's. The AI outline states no term, and the model wrote "Fall 2026" (taken from today's date), not "unknown".
4. **Two deliverables due together became one item.** The Machine Learning final report and recording (both Dec 1) came back as one "Final Project Submission" item in every run. The date is right, but the student gets one card for two deliverables.
5. **Contradictions weren't noticed.** The ML logistics say the 30% peer grade is "half of the final project grade", and "11:59 PM EST" for September dates, when New York is on EDT. Neither was mentioned. Weights weren't scored in this test.

**Limits of this test.** Five courses from one school, 16 explicit dates, text PDFs only, one model version, and a single extraction call rather than the full agent loop with `propose_changes`. It shows that extraction is not the weak point. It doesn't show that the whole S1 turn is correct; that is the S1 evaluation (#103), which runs the real loop five times.

### Decision

1. **Keep model extraction with "ask, don't guess" for S1.** The 95% target was met with a wide margin on explicit dates, and no unclear date was ever filled in.
2. **Supported input for the first version (Q5): text PDFs and plain text.** DOCX worked once converted to text, so converting it in the API service is a small, later addition.
3. **The model gives the local date and time; code computes the UTC offset.** The test asked only for local wall-clock times and got them right every time. Daylight saving time ends on Nov 1, inside the term, so leaving offsets to the model invites one-hour errors. Proposed change to `propose_changes` (#91): take a local date and time (or a date only), and let the API add the student's time zone.
4. **Code, not the prompt, enforces a time.** A proposed event without a time is rejected back to the model with "ask the student for the time", unless the student has already chosen a default (for example "use 11:59 PM when none is given"). This covers observation 2.
5. **Code checks the term.** If the documents' term is earlier than today's, or no term is found, the agent tells the student before proposing anything. This covers observation 3.
6. **For vague syllabi, propose what is named and ask about the rest.** This goes into the system prompt and into an S2 evaluation case (#105), based on the AI-course result.

## A2: students want to review before anything is added (#86)

Not run yet; see #86.
