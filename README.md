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

## Two gates before anything is scored

**Is this even a prospect?** (`gather/entity.py`) A trade association, a trade
publisher and an agency network all describe manual-looking workflows on
their websites, and an earlier version of this tool happily ranked all three
as leads — one at rank 4, with a drafted opener. The entity gate reads the
homepage and removes anything that is not an operating business of the target
type, with a verbatim quote from the page as its evidence. Disqualified
companies get no score and no model call.

It never disqualifies on a name. The list that prompted this gate flagged
"Brown and Brown" as a national brokerage to filter out; the company in the
run was a 20-person independent agency in Auburn, Indiana that happens to
share the name, and it was the best-evidenced prospect in the batch. Scale is
judged on what a site says about its own footprint. `tests/test_entity_gate.py`
runs the four real cases, including that control, and must stay green.

**Is this signal actually a signal?** (`audit.py`) A detector that fires on
nearly every company is a property of the corpus, not of the company: it
inflates every score by the same amount and crowds out the lines that
differ. Run `python audit.py` after any run to see per-signal fire rates;
`validate.py` check 7 fails a run where one signal exceeds 40% of the
evidence. This is how the "no live chat widget" check was caught and deleted
after firing on 15 of 17 companies and making up 29% of all evidence.

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
* **`audit_report.txt`** — per-signal fire rates for the committed run.

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
* **MAYBE (40–69%)** — a real but softer signal. Worth a look. MAYBEs get
  draft openers too.
* **FAIL (0–39%)** — nothing concrete found, or not a fit. No opener drafted.
* **DISQUALIFIED / REVIEW** — set by the entity gate before scoring, and
  shown without a percentage because they were never scored.

The MAYBE line was briefly lowered to 30 and then put back. At 30 the tool
surfaced 53% of a run, which makes it a sorter rather than a qualifier, and
lowering it overrode the model's correct "this evidence is thin" judgement
instead of acting on it. The fix for thin evidence is better detection.

Thresholds, weights, the adjustment cap, and which bands get openers are all
in `config.py`.

## Signal sources

| Module | What it reads | Status |
|---|---|---|
| `gather/site.py` | homepage + about/services/careers/contact: what they do, chat-widget presence, client-portal presence, manual-process language in their own copy ("call us for a quote", fax numbers), contact channels, growth language, links to their job board | always on |
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
python audit.py               # per-signal fire rates; run before changing a weight
python tests/test_entity_gate.py   # entity-gate regression, no key needed
```

Input CSV needs `company` and `domain` columns; `industry`, `team_size`,
`contact_name`, `contact_title`, `contact_email`, `competitor_domain` are
optional and passed through (`team_size` is also given to the qualifier as
context — a 5-person agency and a 5,000-person vendor mean different things
by the same signal). Common Apollo header spellings are recognised.

## Numbers from the committed run

The committed `report.html` / `results.csv` come from a run on 2026-08-11
over the 29 companies in `input_example.csv`: small US insurance agencies,
5 to 49 people — the segment a small automation studio actually sells to.

* **29 companies: 0 PASS, 2 MAYBE, 22 FAIL, 3 disqualified, 2 to review.**
  48 evidence lines across 6 signal types, no type above 31%.
* Top of the list: **Burkhart Insurance Agency at 66%** — service requests
  routed to a person, quote language with no online quote path anywhere on
  the site, and a "call for a quote" line, each quoted with its page.
* **Two MAYBEs out of 24 scored is the honest read of this corpus.** These
  sites mostly show fax numbers and "Join Our Team", which are weak signals
  and are now weighted like it. The way to get more real prospects out of
  this segment is a live review source (see below), not a lower threshold.
* The entity gate removed a trade association, a trade publisher and an
  agency network — all three of which the previous version had scored as
  prospects, one at rank 4 with a drafted opener.

An earlier run of the same list is what produced most of the above: it is
kept in the history because every fix here was paid for by a specific defect
that reached the output.

* **12 companies, 32 signals gathered, 0 PASS / 2 MAYBE / 10 FAIL**,
  scores spread 2%–50%, openers drafted for both MAYBEs. 14 model calls
  (claude-opus-5), 21.5k input / 3.9k output tokens, ~100 s with a warm
  page cache.
* Top of the list: Jacobs Insurance Agency at 50% — no chat widget, no
  client portal on any page checked, and a contact page that both promises
  "we'll get back to you" and lists a fax number. Four independent
  observations, each quoted with its page.
* **Zero PASS is the honest result**: none of these agencies shows a
  burning, specific pain in public. The two MAYBEs are "worth a look",
  which is exactly what the band means; nothing was inflated to make the
  demo prettier.
* The model's adjustment went *positive* only once (Jacobs, +2, a team of
  8 where the manual-process markers corroborate each other) and negative
  where the same markers were thin or the team large enough to fix things
  in-house. Both halves of every score are printed on the card.
* An earlier run of the same list scored **all 12 into FAIL** — the signal
  catalogue at the time only knew hiring-board and chat-widget vocabulary,
  and `validate.py` check 5 failed the run for not discriminating. The fix
  was more *observation*, not more generosity: detectors for
  manual-process language ("call us for a quote", "we'll get back to you",
  fax numbers) and missing client portals, each quoted verbatim from the
  company's own pages. That failed validation run is the system working.

### Defects the checks caught, kept as evidence they work

* **A false claim reached a draft opener.** The contact-channel detector
  looked only for `tel:` links, so it announced "no phone number or live
  channel" about a page that displayed `260-925-4766` beside the words
  "call, email or stop by". Absence is now only claimed when the phone
  pattern is missing from the rendered text too. This is the failure mode
  the whole tool exists to prevent, and it got out.
* **A constant masquerading as a signal.** "No live chat widget" fired on 15
  of 17 companies and made up 29% of all evidence. Deleted; `audit.py` and
  validation check 7 now catch this class automatically.
* **Entity-type blindness**, above.
* **A regression test earning its keep on day one.** The entity gate labelled
  an SIAA network an "association" because generic membership vocabulary
  matched before the network-specific pattern. Caught by
  `tests/test_entity_gate.py`, fixed by ordering the patterns most-specific
  first.
* **Validation catching its own author.** The first run with the entity gate
  failed three checks, all in newly written code: disqualified companies
  whose stored arithmetic no longer reconciled, an "unclear" verdict that
  was disqualifying instead of flagging, and a claim-family regex that read
  "policy reviews" as a claim about review sites.
* An enterprise test run scored Elastic 57% on a "support cluster" that
  title-matching had built out of an FP&A manager and a revenue-ops role;
  a finance-title guard now excludes those.
* A first draft of the manual-process detectors matched the `&quot;` HTML
  entity as the word "quote", which is why every detector runs on extracted
  visible text, never raw markup.

### Known limit: the heavyweight signals are silent on this niche

`audit.py` reports that `manual_intake`, the hiring signals and
`review_complaints` fired zero times across 29 companies with 27 readable
sites. That is not a broken matcher: these sites have no public job boards
and no downloadable intake forms.

**The review signal is now live and still does not fire, which is the more
interesting result.** Google Places is connected and matched 21 of 29
companies to their own listing. Their ratings:

```
5.0  4.9  4.9  4.9  4.9  4.9  4.9  4.9  4.8  4.8  4.8
4.6  4.5  3.8  3.0 (2 reviews)  ...
```

Small local insurance agencies are rated overwhelmingly well. Only one sits
below the 3.7 complaint threshold, and it has two reviews — noise, not a
pattern, so the `GOOGLE_MIN_RATINGS` floor correctly withholds it. The
hypothesis that a neglected agency would show up as an angry review pattern
simply does not hold for this segment. That is a finding about the market,
not a gap in the tool, and it is the reason the run reports 0 PASS honestly
instead of manufacturing one.

The signal stays wired because it costs nothing when silent and will fire
immediately on a segment where service complaints are public — consumer
services, trades, clinics, anything with a real complaint tail.

### Identity, not name matching

The Places integration only accepts a listing that **publishes the same
domain** as the company being scored. Name matching alone had put
"Avanti Travel Insurance" (a UK travel insurer, 3,750 reviews) against a
five-person Michigan agency, "Brown & Brown Insurance of Arizona" against an
Indiana one, and "Lloyd Agencies" (5,408 reviews) against a fifteen-person
office. None of those produced a false signal, but only because the wrong
entities happened to be well rated: a low-rated mismatch would have offered
another company's reviews as evidence about a prospect. A shared word in a
business name is a coincidence; a shared domain is the company. No domain on
the listing means no match and no signal, and the run records which
candidates were rejected and why.

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
