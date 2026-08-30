# Paste-ready prompt for generating the contest deck

Business-first framing: the use case, the automation, and the Evalueserve fit.
Copy everything inside the fence into a fresh Claude conversation.

> **Before you paste:** search for `[FILL IN]` in the block below and replace those
> with your real numbers. They are the slides a judging panel will weigh most heavily,
> and I have deliberately left them empty rather than invent them.

---

````
I need you to build a presentation deck for an internal Evalueserve contest submission.
This is a business case presentation, not an engineering talk — the audience cares about
the work being automated, the analyst hours returned, and the client risk removed. Keep
the technology in a supporting role.

Produce the deck as a self-contained HTML artifact: one section per slide, 16:9
proportions, arrow-key navigation, clean professional design, legible from the back of
a room. Ask me clarifying questions before you start if anything is ambiguous.

## THE PRODUCT IN ONE LINE

An agent that turns recurring financial documents — rent rolls, income statements,
balance sheets, cash flow statements, filings — into verified, client-ready Excel,
and that gets faster every month it runs.

## THE BUSINESS PROBLEM

Evalueserve analysts process the same financial documents on a repeating cycle. The
same rent roll, from the same property manager, with the same broken columns, arrives
every month. The same quarterly filings arrive every quarter, in the same formats,
from the same issuers.

Today that work is manual and it repeats in full every cycle:

1. **Re-keying.** An analyst reads figures off a PDF and types them into a model or
   template. Volume is high, the work is unskilled relative to the analyst's training,
   and it is the least interesting part of their job.
2. **Full verification.** Because any figure could be wrong, every figure has to be
   checked. A document with 163 line items means 163 things to verify.
3. **No memory.** The formatting quirks in a recurring document get repaired by hand
   this month, and repaired by hand again next month, and again the month after. The
   effort never declines, because nothing retains what was learned.
4. **Silent errors reach the client.** The failures in this work do not announce
   themselves. A European document read as US convention turns 1.234,56 into 1.234 —
   a 1000x error with no exception raised. A date read month-first turns 3 April into
   4 March. A liability that prints in parentheses gets stored negative and quietly
   flips a ratio. None of these look wrong on the page. They surface in a client
   deliverable.

The cost is not just hours. It is senior analyst time spent on transcription, and
review capacity spent confirming figures that were already correct.

## WHY GENERIC AI TOOLS DON'T SOLVE THIS

The obvious move is to point a language model at the PDF and let it read the numbers.
That fails the specific test this work has to pass: **a client deliverable has to be
defensible.**

When a model produces a figure, there is nothing behind it but the model. It cannot be
audited, it cannot be traced to the page it came from, and when it is confidently wrong
it is wrong in a way that looks exactly like being right. A confidence score of 87%
tells an analyst nothing they can act on.

So this agent is built the other way around.

## THE CORE DESIGN DECISION

**No language model ever touches a figure.**

Every number is carried straight from the page to the workbook by fixed rules that
behave the same way every time. The model is confined to one narrow job — naming the
statement type, the reporting period and the currency. It never reads, alters, or
produces a figure.

That single constraint is what makes the output defensible to a client, and it should
be stated plainly on a slide of its own.

## HOW THE WORK CHANGES — BEFORE AND AFTER

**Before**
    Analyst receives PDF
    -> reads and re-keys every figure by hand
    -> repairs the same formatting quirks as last month
    -> verifies all 163 figures because any one could be wrong
    -> builds the deliverable
    Effort is identical every single cycle.

**After**
    Analyst uploads PDF (single or batch)
    -> agent parses, rebuilds the account hierarchy, and checks the document's
       own arithmetic: cross-foots, column foots, subtotals, accounting identities
    -> agent repairs what it can, and re-checks that the repair actually helped
    -> analyst reviews ONLY the figures that failed a check — 3 exceptions, not 163
    -> analyst approves or corrects the agent's proposed fixes
    -> export to Excel / CSV / JSON
    Effort falls every cycle, because approved fixes are remembered.

The shift to put on the slide: **from "verify all 163 figures" to "review these 3
exceptions."** Review stops being proportional to document size and becomes
proportional to the number of actual problems.

## THE AUTOMATION MECHANISM — three things doing the work

**1. The document's own arithmetic is the quality check.**
Financial statements are self-verifying: subtotals sum their line items, columns foot,
and the accounting identities hold. The agent uses that structure as ground truth. A
check either ties, or it reports the exact amount it is out by and where to look. This
replaced percentage confidence scores entirely — because "out by 4,200 on this subtotal"
is actionable and "87% confident" is not.

**2. Every figure carries a citation.**
Each number can name the page it was printed on, the row and column it sat in, and the
characters the document actually used. That last detail is operational, not cosmetic: a
reviewer sent to check "-1234.56" against a page that reads "(1,234.56)" will not find
it, and will waste the time the tool was supposed to save.
Each figure carries one of three statuses — and the third is where the honesty is:
    ties       a check covers this figure and it holds
    exception  a check covers this figure and it does not, with the amount it is out by
    unchecked  nothing cross-foots this figure
An unchecked figure is not a passing figure. The agent will not claim proof it does not
have. That distinction is what makes the output safe to hand to a client.

**3. The agent learns each layout once, from the analyst.**
When the agent finds a problem it can fix, it proposes the fix — it does not apply it.
An analyst approves or corrects it. Once approved, that fix is stored against the
document's layout fingerprint and applied automatically every time that layout appears
again.
Two states, deliberately kept apart:
    proposals — computed, nobody has approved them, never applied
    rules     — analyst-approved, applied automatically on sight
The analyst is the authority. The agent proposes and remembers.

## THE COMPOUNDING EFFECT — this is the strongest business slide

Effort on a recurring document does not just drop once. It declines with every cycle.

    Month 1   Agent flags the layout's quirks. Analyst approves the fixes once.
    Month 2   Those fixes apply automatically. The agent is quieter.
    Month 6   The layout is fully learned. The agent runs clean and only speaks up
              when something genuinely new appears in the document.

Conventional automation gives a one-time step change and then plateaus. This improves
every month it runs, and the improvement is driven by the analysts already doing the
work — no ML retraining cycle, no data science project, no vendor engagement.

Across a book of recurring client work, that curve is the business case.

## WHY THIS FITS EVALUESERVE SPECIFICALLY

**It is mind+machine, implemented literally.** The machine does what machines are
reliably good at — deterministic transcription and exhaustive arithmetic checking at
volume. The analyst does what only judgment can do — resolving genuine exceptions and
deciding which fixes are correct. Neither is asked to do the other's job. The approval
gate on learned rules is that division of labour written into the software.

**It is built on Evalueserve's own capability, not a third-party product.** It uses the
document parsing and cloud environment the firm already runs. No client document leaves
the existing estate, no new vendor is introduced, and no per-document licence fee scales
up as volume grows.

**It targets exactly the work Evalueserve does at scale** — recurring, high-volume
financial document processing for clients, where the documents repeat in format and the
deliverable has to be defensible.

**It scales across teams without rework.** The agent is not built for one client's rent
roll format. It infers each document's structure, number convention and date convention
from the document itself, so a new client or a new document type does not require new
code — it requires an analyst to approve the fixes for that layout once.

## STATUS TODAY

- Working end to end, deployed and in use
- An analyst can upload a document or a whole batch, review the exceptions, trace any
  figure back to the page it was printed on, approve or correct the agent's proposed
  fixes, and export to Excel, CSV or JSON
- Runs entirely on Evalueserve's own document parser and cloud environment — no new
  vendor, no client document leaving the existing estate

Do not put a technology or architecture slide in this deck. If the panel asks how it
is built, that is a question to answer live, not screen space to spend.

## WHAT IT HAS ACTUALLY PROCESSED

Real measured runs, read back from the actual exported workbooks — not a demo estimate.
Use these as the evidence slide. Every workbook the agent produces follows the same
shape, worth naming once on this slide: one sheet per page (or page range) of extracted
figures, an "Exceptions" sheet listing only the checks that failed, and an "Audit" sheet
recording every model inference and every correction made — nothing on the Audit sheet
is ever a figure.

**Monthly profit-and-loss statement** (35 KB) — exported as Page 1 / Exceptions / Audit
- 179 figures extracted across 50 rows
- 54 arithmetic checks run, all 54 tied — zero exceptions
- Audit sheet shows zero corrections; the parser's own output needed nothing fixed

Read the exceptions line the way the agent means it: not "the model was confident", but
"every number on the page was cross-checked against the document's own totals and all
of them held". The reviewer's job on this document was nothing.

**Monthly rent roll** (10 pages) — exported as Pages 1-9 / Page 10 / Exceptions / Audit
- 2,619 figures extracted across 292 unit-level rows
- 0 arithmetic checks applied — a per-unit rent roll has no footing subtotals to check
  against, unlike a P&L. State this plainly if asked: the agent reports "0 of 0 checks
  tie" rather than manufacture a reassuring number it has no basis for. That is the same
  honesty as the "unchecked" status, applied to a whole document instead of one figure.
- 6 structural repairs applied automatically, covering 185 of the 292 rows: the parser
  had merged "Unit Type" and "Resident Name" into a single column and written the same
  text into both; the agent split them back into two, with no analyst re-keying a cell.

The rent roll is the more persuasive of the two for the compounding-effect slide,
because it is the recurring document the whole argument rests on, and it is where a
real repair happened rather than a clean pass.

## WIDER IMPACT — [FILL IN before presenting]

Replace these with your real figures. If you do not have them yet, run the agent
against one month of a live recurring engagement and measure. This is the slide the
panel will weigh most heavily, and estimates you can defend are far better than
impressive numbers you cannot.

- Documents processed per cycle today: [FILL IN]
- Analyst time per document, before: [FILL IN] / after: [FILL IN]
- Figures per document, before: verify all N / after: review only the exceptions [FILL IN]
- Errors caught that manual review had historically missed: [FILL IN]
- Recurring engagements this could be applied to: [FILL IN]

## SUGGESTED SLIDE STRUCTURE

1.  Title
2.  The work today — recurring financial documents, manually processed every cycle
3.  What it costs — re-keying, full verification, no memory, silent errors
4.  Why generic AI does not solve it — a client deliverable must be defensible
5.  The design decision — no model ever touches a figure
6.  Before and after — the analyst workflow, side by side
7.  From 163 figures to 3 exceptions — arithmetic as the quality check
8.  Every figure carries a citation — including "unchecked" and why that matters
9.  Learned rules — the analyst approves once, the agent remembers
10. The compounding curve — month 1 vs month 6
11. Why this fits Evalueserve — mind+machine, in-house infrastructure, scale
12. Impact [FILL IN]
13. Status and what's next

## TONE

Business-first and understated. This is a deck about work being done differently, not
about how the software is built: no architecture diagrams, no file or component names,
no code, no stack list, no framework or model names. Every point lands as an analyst or
client consequence. Where a slide would otherwise describe a mechanism, describe what
it changes for the person doing the job instead. Do not use marketing superlatives — the
mechanism is persuasive on its own. Do not invent metrics, accuracy figures, or
client names; where a number is needed and not supplied, leave a visible placeholder.
Where a design choice was made for honesty rather than for a better demo — the
"unchecked" status, refusing to guess a document's date convention, requiring analyst
approval before any fix is reused — say so plainly. A panel of practitioners will
recognise why those choices were made.
````

---

## Before you present — three cautions

1. **Fill in every `[FILL IN]`.** Slide 12 is the one that decides contests. One
   honestly measured engagement beats a deck of estimates.
2. **Do not screenshot real documents.** The repo's test fixtures contain genuine
   client financial data. Use synthetic examples for any slide showing a document,
   a table, or extracted figures — and check the same before any live demo.
3. **Verify the Evalueserve positioning language yourself.** I have framed the fit
   around mind+machine and in-house IDP/Azure infrastructure. Confirm that matches
   how your business unit currently presents itself, and swap in the specific service
   line and client-team names that the panel will recognise.
