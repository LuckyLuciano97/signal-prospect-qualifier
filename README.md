# Signal — prospect qualifier & personalizer

Point it at a CSV of companies. It reads what each company shows the public
internet — their website, their open job listings, review sites where those
permit it — and hands back a ranked shortlist of who actually has a visible,
evidenced reason to need what you sell, with the evidence attached and a
personalized opening line drafted for each strong prospect. You review the
list, pick who to contact, and send the emails yourself.

Built as an internal tool for [Nexis Studio](mailto:support@nexistudio.dev)'s
own client acquisition (the offers it qualifies against: support triage
agents, scrapers, data pipelines, workflow automation). The offer catalogue
and every threshold live in `config.py`, so pointing it at a different
service business is a config edit, not a rewrite — the underlying product is
"find and qualify your ideal customers by their real buying signals."

## The one rule everything else follows

**No evidence, no score.** Every point of every score traces to a specific
observation the tool actually made: a quoted phrase from a job post, a
counted cluster of open roles, a widget visibly absent from a page it
fetched. A company where nothing concrete was found scores 0 with the
reasoning "no concrete signal found" — it is never sent to the model to be
guessed about, because the honest answer to "we found nothing" is a low
score, not confident-sounding filler. `validate.py` proves this property
holds for the committed run (check 2: zero evidence-free scores).

## What a run produces

* **`report.html`** — self-contained, double-clickable, sorted best-first.
  Each company shows its score and band, which offer fits, the full evidence
  list with sources and links, the score arithmetic, the draft opener, and
  what coverage was missed (blocked hosts, robots refusals) so a 0 reads as
  "nothing found" or "could not look", whichever is true.
* **`results.csv`** — the same data flat, for sorting and outreach tracking.
* **`summary.json`** — run statistics: bands, request counts, token counts.
* **`results.jsonl`** — the full structured record `validate.py` audits.

## Scoring: PASS / MAYBE / FAIL, plus a percentage

The score is rule-anchored, not model vibes. Each signal type has a fixed
weight in `config.py` (explicit pain language in a job post: 30; a cluster
of open support roles: 30; a capability gap on their own site: 12; generic
growth language: 5; ...). The base score is the capped sum of the weights of
the signals actually found, so two companies with the same evidence get the
same base, mechanically. The model then reads the gathered evidence — only
the gathered evidence — picks the offer it best supports, writes the one-line
reasoning, and may adjust the base by at most ±15 points, citing the signal
ids its adjustment rests on. A positive adjustment citing nothing is zeroed.

* **PASS (70–100%)** — a clear, specific, evidenced pain. Worth reaching out.
* **MAYBE (40–69%)** — a real but softer signal. Worth a look; the pass line
  is deliberately generous, because burying a borderline prospect helps
  nobody. MAYBEs get draft openers too.
* **FAIL (0–39%)** — nothing concrete found, or not a fit. No opener drafted.

Thresholds, weights, the adjustment cap, and which bands get openers are all
in `config.py`.

## Signal sources

| Module | What it reads | Status |
|---|---|---|
| `gather/site.py` | homepage + about/services/careers/contact: what they do, chat-widget presence, contact channels, growth language, links to their job board | always on |
| `gather/hiring.py` | Greenhouse/Lever public board APIs (board found via links on their own site, else two polite token guesses); role clusters; full text of the few most relevant posts, scanned for explicit pain language | always on |
| `gather/reviews.py` | Trustpilot and Apple App Store review complaints | **effectively closed** — see below |
| `gather/competitive.py` | one cheap check: competitor's site has a chat widget the company's lacks (only for rows with a `competitor_domain`) | lowest weight, optional |

### The honest limit: review sites refuse crawlers, so this tool gets no review signals

Trustpilot's robots.txt ends with `User-agent: * Disallow: /` — unlisted
crawlers are banned from the entire site. Apple's `itunes.apple.com/robots.txt`
disallows `/search*` and `/*/rss/*`, which covers both the app-search
endpoint and the public customer-reviews feed. G2 and Capterra sit behind
bot walls. This tool's policy is to obey robots.txt everywhere with no
convenient exceptions, so in the committed run **every company's review
coverage reads `robots-disallowed` and zero review signals were scored.**
The module stays functional and correctly guarded (confident app matching,
counts-only output) because robots policies change and a licensed source —
Trustpilot's business API, an app-review data vendor — would slot straight
into it. Until then, review complaints are a capability this tool declines
to fake.

## Politeness and legality

* Descriptive User-Agent carrying a contact address.
* robots.txt fetched and obeyed per host, including `Crawl-delay`.
* Serial requests, minimum 1s between requests to the same host.
* Retries only on 429/5xx, honouring `Retry-After`, three attempts.
* **A 403 stops all further requests to that host for the run.** It is
  recorded in the company's coverage notes. No header rotation, no evasion,
  no CAPTCHA solving — a refusal is a refusal.
* Every response cached to disk (`cache/`, gitignored) so development
  re-runs cost the sources nothing.

## No personal data

All gathered signals are company-level: role counts, job-post phrases, page
features, review-complaint *counts*. Review text and reviewer identities
never leave the gathering function. The only personal data in the output is
the contact you supplied in your own input CSV, passed through untouched.
`validate.py` check 1 scans every signal, reasoning line, and opener for
emails, phone patterns, and leakage of your supplied contact, and fails the
run if any appears.

## Openers

Companies above the FAIL line get one or two drafted sentences that
reference the strongest actual signal ("noticed you have four open support
roles and your contact page routes everything through a form"), lead with
the prospect's pain rather than the service, and stop — no pitch, no call to
action. The model must cite which signals the line rests on; a line grounded
in nothing is discarded. The tool **never sends email**. You write the rest
and send it from your own address.

## Run it

```bash
pip install -r requirements.txt
copy .env.example .env        # add your Anthropic API key
python run.py                 # the committed example list
python run.py --input your_apollo_export.csv
python run.py --no-llm        # rule-based scores only, no key needed
python validate.py            # audit the run's promises; exits non-zero on failure
```

Input CSV needs `company` and `domain` columns; `industry`, `contact_name`,
`contact_title`, `contact_email`, `competitor_domain` are optional and
passed through. Common Apollo header spellings are recognised.

## Numbers from the committed run

The committed `report.html` / `results.csv` come from a real run over the 12
companies in `input_example.csv` on 2026-08-06 (they are large, well-known
companies because their signals are public and checkable — as prospects for
a small studio most of them *should* score mediocre, and they do):

* **12 companies, 44 signals gathered, 4 PASS / 2 MAYBE / 6 FAIL**,
  6 openers drafted. Cold crawl ~3 minutes; re-runs ~90 s from cache.
* **13 model calls** (claude-opus-5), 18.7k input / 2.9k output tokens.
* Top of the list: Braze at 80% — 18 open support roles plus a Technical
  Support Specialist post literally requiring "managing and prioritizing a
  high volume of inquiries and escalations", quoted in the evidence.
* Bottom of the list: DoorDash at 0% — its site answered 403, no job board
  was found, and the tool says exactly that instead of inventing a reason.
* **2 sites refused the crawler with a 403** (gopuff.com, doordash.com):
  recorded as blocked coverage, not worked around. Gopuff still scored 30%
  from its public Lever board — evidence that was genuinely available.
* **Review coverage was robots-disallowed for all 12 companies** (see the
  honest-limit section above), so the "partial coverage" counter in the
  report reads 12/12. That is the tool being truthful about what it could
  not see, not a crash.
* The model's adjustment was negative for every scored company (−5 to −15):
  it repeatedly recognised that big-enterprise hiring clusters are in-house
  build capacity rather than outsourceable pain. The rule base proposes,
  the model tempers, and both halves are printed on every card.

One defect this process caught and fixed, left here as evidence the checks
work: an early run scored Elastic 57% on a "support cluster" that title-
matching had built out of an FP&A manager and a revenue-ops role. The
qualifier's own reasoning flagged the misclassification; a finance-title
guard now excludes such roles, and Elastic honestly sits at 29% FAIL.

## Validation

`validate.py` re-derives every score from the stored signals and checks the
five promises above (no personal data, no evidence-free scores, openers
trace to signals, unreachable companies score 0 without invented reasons,
distribution sanity). Its verbatim output is committed as
`validation_report.txt`, failures included, next to the `report.html` and
`results.csv` it audited.

## Non-goals

* Does **not** send email. Human reviews, human sends.
* Does **not** scrape personal data. Company-level signals only.
* Does **not** infer revenue or financial health — not reliably public, so
  not guessed at.
* Does **not** work around blocks: no CAPTCHA solving, no header games, no
  scraping sources whose robots.txt forbids it. Blocked coverage is reported
  as blocked coverage.
* Does **not** inflate: low signal means a low score with an honest reason.

## Working together

Nexis Studio builds tools like this — scrapers with validation baked in,
data pipelines, support triage agents, and workflow automation with a
human-in-the-loop where it matters. If you want your prospect list qualified
by real buying signals, or the pipeline behind it built for your team:
**support@nexistudio.dev**.

MIT licensed.
