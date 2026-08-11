# Crawling in n8n, scoring in Python

n8n fetches pages and writes one JSON bundle per company. Python reads those
bundles and does every bit of detection, scoring, opener drafting and
validation. Nothing else changes.

## The one rule

**n8n hands over raw fetched pages, never extracted signals.**

Every evidence quote in a report is substring-checked against the page text
it came from. If a detector lived in an n8n Code node, the Python validator
would have nothing left to check the quote against, and "no invented facts"
would go from a property the code enforces to a claim someone makes. So the
Code nodes below assemble and forward; they never decide anything.

A useful consequence: the `text` field in a bundle is advisory. Python
re-derives visible text from `html` with its own extractor, so your n8n text
extraction can be crude without affecting a single score. Send `html` and
don't worry about it.

## Step by step

### 1. Prove the Python side first, with no n8n at all

```bash
python tools/bundle_from_cache.py --out bundles/
python run.py --bundles bundles/
python validate.py
```

This builds bundles in the exact hand-off format from pages already in
`cache/`, then scores them. It is how the contract was verified: over 29
companies, bundle mode produced **byte-identical evidence** to the live run
for all 29. Look at a generated file before you build anything — it is the
target your workflow has to hit.

### 2. Decide where bundles land

Pick a directory n8n can write to and Python can read, e.g.
`C:\signal\bundles`. Everything below writes there; `run.py --bundles` reads
there. Bundles are worth keeping: re-scoring after a prompt or weight change
then costs nothing and re-crawls nothing.

### 3. Build the workflow

Import `n8n/signal-crawl.workflow.json` as a starting point, or build it in
this order. Node names matter only where a Code node references them.

1. **Schedule Trigger** — whatever cadence you want.

2. **Google Sheets → Get Rows** — your prospect list, columns `company`,
   `domain`, `team_size`. A Sheet means you can edit the list from your
   phone. Credentials go in n8n's credential store, never typed into a node
   field: node fields are exported in workflow JSON, credentials are not.

3. **Loop Over Items** (Split In Batches), batch size **1**. Everything from
   here to the write node sits inside the loop, so one company is in flight
   at a time and one failure never takes the run with it.

4. **HTTP Request — robots.txt**
   - URL: `=https://{{ $json.domain }}/robots.txt`
   - Settings → **On Error: Continue (using error output)**
   - Response → Format: **Text**, and **Never Error** on non-2xx if offered.

5. **Code — parse robots** (below). Emits `allows_crawl` and
   `disallowed_paths` for your user-agent.

6. **IF — allowed?** Condition: `{{ $json.allows_crawl }}` is true.
   The false branch does **not** stop: it writes a bundle with
   `robots.allows_crawl: false` and empty `pages`, so the report can say
   "could not look" instead of the company silently vanishing.

7. **HTTP Request — homepage**
   - URL: `=https://{{ $json.domain }}`
   - Response Format: **Text** (you want raw HTML, not parsed JSON)
   - **On Error: Continue (using error output)** — one 403 kills one host,
     not the run.
   - Options → Redirects: follow.
   - Options → **Response → Include Response Headers and Status**, so a
     non-200 reaches your bundle as a `failed` entry rather than as nothing.

8. **Code — find subpages** (below). Pulls about/services/careers/contact
   links out of the homepage HTML. Same-host only.

9. **HTTP Request — subpages**, same settings as the homepage node, running
   over the URLs from step 8.

10. **Wait** — 1 second, inside the loop. n8n has no per-host throttle, and
    this is the politeness the Python client provides for free. Do not skip
    it; the whole tool's posture is that it is a well-behaved visitor.

11. **Code — assemble bundle** (below). Builds the exact JSON contract,
    including `coverage`.

12. **Convert to File** (JSON → file) → **Read/Write Files from Disk**
    (write), filename `={{ $json.domain }}.json` into your bundle directory.

### 4. Score what it produced

```bash
python run.py --bundles C:\signal\bundles
python validate.py
python audit.py
```

If `validate.py` passes, the hand-off is correct. If coverage is wrong you
will see it as check 4 failing or as companies that should read
"could not look" showing up scored — which is exactly the failure the
`coverage` block exists to prevent.

## The Code nodes

### Parse robots

```javascript
// Input: the robots.txt body (or an error branch item).
// Output: allows_crawl + disallowed_paths for our user-agent.
const domain = $('Loop Over Items').item.json.domain;
const body = typeof $json.data === 'string' ? $json.data : ($json.body || '');
const fetched = Boolean(body);

let allows = true;
const disallowed = [];
if (fetched) {
  // Walk the groups; keep the one for * (or for our UA if it appears).
  let applies = false;
  for (const raw of body.split('\n')) {
    const line = raw.split('#')[0].trim();
    if (!line) continue;
    const [field, ...rest] = line.split(':');
    const value = rest.join(':').trim();
    const key = field.trim().toLowerCase();
    if (key === 'user-agent') {
      applies = value === '*' || value.toLowerCase().includes('signal');
    } else if (applies && key === 'disallow') {
      if (value === '/') allows = false;
      else if (value) disallowed.push(value);
    }
  }
}
return [{ json: { domain, robots_fetched: fetched, allows_crawl: allows,
                  disallowed_paths: disallowed } }];
```

### Find subpages

```javascript
// Input: homepage HTML. Output: same-host about/services/careers/contact URLs.
const domain = $('Loop Over Items').item.json.domain;
const html = typeof $json.data === 'string' ? $json.data : '';
const base = `https://${domain}`;

const WANTED = {
  about: /about|company|who-we-are/i,
  services: /services|products|solutions|coverage/i,
  careers: /careers|jobs|join-us/i,
  contact: /contact/i,
};

const found = {};
const re = /<a[^>]+href=["']([^"']+)["'][^>]*>(.*?)<\/a>/gis;
let m;
while ((m = re.exec(html)) !== null) {
  const href = m[1].trim();
  if (!href || /^(mailto:|tel:|javascript:|#)/i.test(href)) continue;
  let url;
  try { url = new URL(href, base); } catch { continue; }
  if (url.hostname.replace(/^www\./, '') !== domain.replace(/^www\./, '')) continue;
  const label = m[2].replace(/<[^>]*>/g, ' ');
  for (const [type, pattern] of Object.entries(WANTED)) {
    if (found[type]) continue;
    if (pattern.test(url.pathname) || pattern.test(label)) {
      found[type] = url.toString();
    }
  }
}
return Object.entries(found).map(([page_type, url]) => ({ json: { domain, page_type, url } }));
```

### Assemble bundle

```javascript
// Builds the contract. Sends html and lets Python extract text.
const row = $('Loop Over Items').item.json;
const robots = $('Parse robots').item.json;

const ATTEMPTED = ['homepage', 'about', 'services', 'careers', 'contact'];
const pages = [];
const failed = [];

for (const item of $input.all()) {
  const j = item.json;
  const status = j.statusCode ?? j.status ?? (j.error ? 0 : 200);
  const pageType = j.page_type || 'homepage';
  if (status === 200 && typeof j.data === 'string' && j.data.length) {
    pages.push({
      url: j.url || `https://${row.domain}`,
      page_type: pageType,
      status: 200,
      fetched_at: new Date().toISOString(),
      html: j.data,
      text: j.data.replace(/<script[\s\S]*?<\/script>/gi, ' ')
                  .replace(/<style[\s\S]*?<\/style>/gi, ' ')
                  .replace(/<[^>]+>/g, ' ')
                  .replace(/\s+/g, ' ').trim(),
    });
  } else {
    failed.push({ page_type: pageType, reason: status ? `HTTP ${status}` : 'request failed' });
  }
}

const reached = [...new Set(pages.map(p => p.page_type))];
for (const type of ATTEMPTED) {
  if (!reached.includes(type) && !failed.some(f => f.page_type === type)) {
    failed.push({ page_type: type, reason: 'not reached' });
  }
}

return [{ json: {
  company: row.company,
  domain: row.domain,
  industry: row.industry || '',
  team_size: row.team_size || '',
  crawled_at: new Date().toISOString(),
  robots: {
    fetched: robots.robots_fetched,
    allows_crawl: robots.allows_crawl,
    disallowed_paths: robots.disallowed_paths || [],
  },
  pages,
  coverage: {
    attempted: ATTEMPTED,
    reached,
    failed,
    // Anything you did not even try belongs here, named. Leaving it out is
    // how a report starts implying it looked at something it never opened.
    blocked: [{ source: 'reviews', reason: 'not attempted by this workflow' }],
  },
} }];
```

## Adding Google reviews later

Once **Places API (New)** is enabled on the key's project, add a branch after
the homepage fetch:

1. **HTTP Request** → `POST https://places.googleapis.com/v1/places:searchText`
   with header `X-Goog-Api-Key` (from the credential store) and
   `X-Goog-FieldMask: places.id,places.displayName,places.rating,places.userRatingCount`,
   body `{"textQuery": "{{ $json.company }} {{ $json.location }}"}`.
2. **HTTP Request** → the place details endpoint for `reviews`.
3. Put `rating`, `userRatingCount` and the review texts into the bundle under
   a `reviews` block with the same `fetched_at` provenance.

Python then detects `review_complaints` against that text like any other
source. Review count also becomes a better volume proxy than `team_size`.

## Two things that will bite you

**Credentials in node fields end up in exported workflow JSON.** Use the
credential store for the Google Sheets and Places keys. This is the same
reason `net.py` sends API keys as headers rather than query parameters — a
key in a URL lands in the response cache on disk.

**n8n has no HTTP cache.** Every execution re-fetches. That is the reason
bundles are files rather than a stream: keep them, and `run.py --bundles`
re-scores a corpus in about ten seconds without touching a website. The
29-company corpus in this repo re-scores in 10s from bundles versus roughly
three minutes of crawling.
