# Nonprofit Profile Agent

Give it a nonprofit's name or website. It returns a structured profile with a source on every fact, plus a **sales view** built on **buyer signals** (strategic plans, capital campaigns, new leaders, technology investment, RFPs, revenue growth): a lead score, a one-line "why now", and a flat CSV row ready for CRM import.

```bash
python run.py https://www.charitywater.org        # one org
python run.py -v https://www.cfwesternva.org      # stage-by-stage trace
python run.py --batch tests/messy_inputs.txt      # a list; one bad input never stops the batch
python eval.py                                    # score outputs against hand-labelled gold data
```

Example outputs are committed in [`output/`](output/): one JSON per org, [`profiles.csv`](output/profiles.csv), and a `debug/` folder showing what the crawler found and exactly what the LLM saw.

---

## 1. Problem

Facts about a nonprofit are spread across its website: the about page, the team page, careers, news, the annual-report PDF buried two clicks deep, and a footer holding the registration number. Some of it sits in registries such as the IRS 990 filings. Collecting it by hand takes 20–30 minutes per org and is out of date within months. At 500,000 orgs, nobody can keep it current by hand.

## 2. Value: who uses this, and for what

I split users by **the direction the money flows**:

| | Sell-side: **primary user** | Fund-side: supported |
|---|---|---|
| Who | Vendors selling *to* nonprofits: donor-CRM and fundraising software, consultants, auditors, agencies | Those giving money *to* nonprofits: foundations, government grantmakers, corporate CSR teams |
| Their question | *Why reach out now? Can they pay? What do they use today? Who decides?* | *Do they fit our focus, are they financially healthy, are they legitimate?* |
| What they get | `sales_view`: lead score, why-now line, buyer signals, revenue trend, tech stack, contacts grouped by buyer role, CSV row | `funder_view`: mission, programs, geography, tax status, multi-year revenue trend, impact claims, existing funders |

The sell-side is the primary user for the MVP. It is the larger, repeat-purchase market, and its value depends on freshness, which is exactly what automation provides. The split isn't clean:
- corporate sponsors sit on both sides, since they give money but expect marketing value back;
- size, geography and news matter to everyone.

That's why there is **one shared core profile** and **two thin views** computed from it. A view is a selection and ranking of fields that were already extracted: no second crawl, no second LLM call. Serving a new customer type means adding one function to [`src/views.py`](src/views.py).

---

## 3. The schema, and why each field is there

Defined in [`src/schema.py`](src/schema.py) (Pydantic). The organising question is the sales user's: **is there a reason this org will buy something soon?** So the schema is built around buyer signals first, then ability to pay and current tools, with contacts last.

Every extracted value carries **`source_url` + `confidence`**. Five high-stakes fields also carry verbatim **`evidence`** of at most 30 words, because these are the fields a person checks before acting on them:
- buyer signals
- RFPs
- open roles
- revenue
- registration ID

Evidence isn't on every field because output tokens cost 4–6× input tokens with both Anthropic and OpenAI. Output is about 9% of our tokens but about 30% of the LLM bill (§7).

### Collected

| Field | Who needs it | Why | Source | Planned refresh |
|---|---|---|---|---|
| `name`, `legal_name`, `website` | both | Join key for CRM records and for deduplication | site, IRS | yearly |
| `registration_id` (EIN or charity no.) | both | Join key to registries; legitimacy check | footer or about page; IRS | yearly |
| `country`, `hq_location` | both | Sales territory routing; funder eligibility | site | yearly |
| `mission`, `programs` | funders; personalising outreach | Fit with a funder's focus | site | yearly |
| `cause_area` (fixed list) | both | Segmenting lists (see §9) | NTEE code from IRS, otherwise LLM choosing from the fixed list | yearly |
| `geography_served` (local / regional / national / international) | both | Funders fund places; sales territories | site | yearly |
| `annual_revenue`, `fiscal_year`, `staff_count` → `size_band` | **sales**: can they pay? | Budget qualifies the deal size; the band is computed by rule, not by the LLM | IRS 990 first, then annual report or site | yearly |
| `financial_history` | both | Revenue trend. Sales: growth of 20%+ over about 3 years is scored as a buyer signal (tools have to scale), a similar decline counts against. Funders: financial health | IRS 990 filings (US) | yearly |
| `tax_status` | funders | Many funders can only give to 501(c)(3) orgs | IRS, otherwise site | yearly |
| `impact_metrics` | funders | Credibility; quantified outcomes | site or report | yearly |
| `leadership[]` + `buyer_role` | **sales**: who decides? | Deliberately light: at most 8, most senior first, no evidence snippet (the grounding check still verifies every name and email). A donor-CRM sale goes to the fundraising lead; accounting software goes to finance. The role is set by keyword rules on the title | team or board page | yearly |
| `general_contact` | both | Fallback contact route | contact page, footer | yearly |
| `tech_stack[]` | **sales** | The most useful field for a software vendor: already a competitor's customer? A migration target? No platform yet (greenfield)? Detected **deterministically** from embeds, with no LLM | donate page and other pages' HTML | quarterly |
| **`buyer_signals[]`** | **sales** | **The core sales field.** Events that suggest spending on new tools or services soon, each with a type from a fixed list: `technology_investment`, `capital_campaign`, `strategic_plan`, `leadership_change`, `merger`, `expansion`, `major_grant`, `funding_growth`, `other`. Mostly found in annual reports, strategic plans, CEO letters and news. Signals older than 2 years are dropped in code; ones whose evidence can't be found on the page stay in the profile but don't score | annual report PDF, strategic plan, news, site | monthly |
| `open_rfps[]` | **sales** | The strongest buying signal. **Expect it to be empty for most orgs**: few nonprofits publish RFPs. Empty means none were found; it's not a bug | site | monthly |
| `open_roles[]` + `is_buying_signal` | **sales** | A new Development Director or CRM/database manager usually means new tools within months. The flag comes from keyword rules on the title | careers page / ATS | monthly |
| `recent_news[]` (last 12 months) | both | Context, and a source of buyer signals. A news item only scores if it describes a signal (a new CEO, a capital campaign); being in the news alone doesn't. Items older than 12 months are **filtered in code**, not left to the prompt | news, press, RSS | monthly |
| `funders_and_partners[]` | both | Sales: social proof and warm introductions. Funders: who else is funding them | site | yearly |

Run metadata: `schema_version`, `input`, `resolved_url`, `extracted_at`, `status` (ok / partial / failed), `flags`, `pages_crawled` (URL, type, HTTP status, tokens), `errors` (every non-fatal problem), `gapfill`, `cost`. Each section (identity, financials, contacts, signals, technology) has its own `last_checked` timestamp, which is what makes tiered refresh (§7) workable.

### Deliberately not collected

| Left out | Why |
|---|---|
| Event calendars | Noisy and quickly stale, and of little value to software sellers. They matter to corporate sponsors, so they're a candidate for a future `sponsor_view` |
| Social follower counts | Vanity metric, weakly tied to budget, and platform APIs are restricted |
| Blog archives | Many tokens, little signal. `recent_news` keeps the last 12 months |
| Donation page *copy* | Marketing text. The donate page **is** still fetched, because the payment platform embedded in it feeds `tech_stack` |
| Volunteer opportunities | No money changes hands, so no signal for either side |
| Financial statement line items | The IRS 990 summary covers what either side needs |

---

## 4. MVP: what V1 does

- Input: a URL (a missing scheme and tracking parameters are fine) or a name, which needs `SEARCH_API_KEY`.
- Crawls up to **10 pages or files**, plus one annual-report PDF hop: HTML, **PDF**, **CSV**, **XML** (sitemaps and RSS feeds).
- **One** structured LLM extraction call, validated against the schema, plus a deterministic grounding check.
- IRS 990 data from ProPublica for US orgs: EIN, revenue history, NTEE code, tax status.
- **At most one** gap-fill round when leadership, contact details or revenue are still missing.
- Buyer signals (plans, campaigns, technology investment, leadership change, growth…) extracted in the same call, with evidence, and grounded.
- Rules: size band, buyer roles, buying-signal roles, NTEE → cause area, date filters for news, buyer signals and RFPs.
- Views: `sales_view` (lead score driven by buyer signals + why-now) and `funder_view`, written as JSON plus an upserted CSV row per org.
- 101 unit tests (`pytest`, no network, no LLM), a `-v` trace, a debug folder, an eval script, and a cost log.

---

## 5. Why this approach: a crawler with LLM extraction, not a free-roaming agent

I considered an LLM tool-calling loop that browses the site. I chose a **deterministic crawler + one extraction call + one bounded agentic step** instead:

| | Tool-calling agent | This pipeline |
|---|---|---|
| Tokens per org | Every turn re-sends the growing context. 10 turns over 3k→35k tokens of page text is about 200k input tokens | About 15k input tokens (measured, §7) |
| Predictability | Step count varies per site, so it's hard to budget at 500k | Fixed: 1–3 LLM calls, at most 14 pages (10 + 1 PDF hop + 3 gap-fill) |
| Reproducibility | Different paths on different runs | Same pages, same prompt, cacheable, testable |
| Orchestration | Needs an agent framework | A plain loop. At 500k, orchestration belongs in a job queue and the Batch API, not an in-process agent graph |

What an agent does better is **adapting when the obvious pages aren't enough**. I kept that, but bounded it. When leadership, contact details or revenue are still empty after extraction, the LLM sees the unfetched links and picks **up to 3** to read. Only the missing fields are then re-extracted, in one round, and every picked URL must come from the list. That's where judgment pays off. Everywhere else, code is cheaper and more consistent.

**Code over LLM wherever a rule can do the job:**
- tech stack
- buyer roles
- buying-signal roles
- size bands
- date filters
- the lead score and the why-now text

These give the same answer for all 500k orgs, and they cost nothing.

---

## 6. Methodology

```
input ──► resolve ──► discover ──► fetch & clean ──► tech stack ──► extract ──► ground-check
          (URL or      (homepage     (HTML/PDF/       (fingerprint    (1 LLM      (drop/downgrade
          name→search  links +       CSV/XML → text,  embeds, no      call,       claims the text
          →verify)     sitemap,      token budget,    LLM)            structured  doesn't support)
                       keyword       +1 PDF hop)                      output)
                       scoring)
      ──► registry (US) ──► gap-fill (≤1 round) ──► rules ──► views ──► JSON + CSV + debug
          ProPublica         LLM picks ≤3 links,     size band,  lead score,
          IRS 990 data       re-extract missing      roles,      why_now,
                             fields only             filters     funder view
```

1. **Resolve** ([`resolve.py`](src/resolve.py)). URLs are normalised: scheme added, host lowercased, `utm_*`/`fbclid`/… removed, redirects followed, `http` fallback. Names go to one web search (Serper). Directories, social sites and news sites are dropped, and the top result is **verified** by fuzzy-matching the org name against its title, h1 and og:site_name, which tolerates typos. If there's no key, no result or the check fails, the run stops with "please provide the URL". **It never profiles a guessed site.**
2. **Discover** ([`discover.py`](src/discover.py)). Homepage links + `sitemap.xml` (from `robots.txt`), + RSS feeds are scored by keyword category: about, financials, plans (strategic plan, capital campaign), rfp, careers, news, donate, team, programs, contact, partners. The rules:
   - The last path segment decides the category.
   - Word boundaries keep `/research` from matching `search`.
   - Deep URLs and long article-like slugs are penalised.
   - The best page per category is picked first, then remaining slots fill by score. The category order puts buyer-signal sources (reports, plans, RFPs, careers, news) ahead of team, contact and partner pages, so those are the ones dropped when the 10-page cap bites.
   - The site's own domain is allowed, plus an allowlist of job boards and donation platforms.
   - Annual-report PDFs are usually linked from the financials page, so there's **one hop** to the best report PDF. It prefers the newest year in the file name and skips reports more than 3 years old.
3. **Fetch & clean** ([`fetch.py`](src/fetch.py), [`parse.py`](src/parse.py)).
   - HTTP: `httpx` with timeouts and a real User-Agent; `tenacity` retries 5xx/429/network errors with backoff; `robots.txt` is respected; 1 s between requests to the same host.
   - HTML goes through trafilatura. **Team pages**, and any page where trafilatura keeps less than 40% of the visible text, use the full BeautifulSoup text instead, because main-text extractors drop name cards. mailto:/tel: links are appended, and so is the **tail of the homepage footer**, where registration numbers live.
   - PDF: the first 2 pages plus the pages with the most financial or leadership keywords, 10 pages at most.
   - CSV: header plus the first 20 rows. XML: flattened leaf text.
   - Each document is capped at 4–6k tokens, with 30k tokens in total.
4. **Tech stack** ([`techstack.py`](src/techstack.py)). About 50 fingerprints: Blackbaud, Salesforce, Bloomerang, Classy, Donorbox, Mailchimp, Eventbrite, Greenhouse, … They are matched against script/iframe/form/link URLs and inline scripts, **never visible text**, so "thanks to our partner Salesforce" doesn't count.
5. **Extract** ([`extract.py`](src/extract.py)). One call to the configured LLM (Claude Haiku 4.5 or GPT-4.1-mini by default) with **structured outputs** (JSON schema generated from Pydantic), documents tagged `<document url=… type=… page=…>`. A static system prompt says "only facts from the documents, null otherwise". Validation failures get one retry that includes the error message.
6. **Grounding check** (deterministic, catches most hallucinations):
   - every `source_url` must be a page we actually fetched;
   - every `evidence` quote must appear in that page's text;
   - every leader's name must appear somewhere in the documents, or the entry is dropped;
   - every email and phone number must appear too, or it's removed;
   - every URL (contact page, job, RFP and news links) must be a page we fetched or a link in the fetched HTML, or it's removed.

   Failures are logged in `errors`.
7. **Registry** (US). ProPublica's Nonprofit Explorer API gives EIN, revenue history, NTEE code and tax status. It matches **only** on the EIN found on the site, or on a *unique*, near-exact name match in the same state, narrowed by city when needed. With no state known, the name must match exactly. On revenue, IRS wins ties and the newer fiscal year wins otherwise. The NTEE code overrides the LLM's cause area, and a disagreement between the two is flagged on the profile.
8. **Gap-fill**: see §5.
9. **Rules** ([`categorise.py`](src/categorise.py)) and **views** ([`views.py`](src/views.py)). The lead score adds:
   - open RFP +3;
   - buying-signal role +2;
   - buyer signals, each type counted once: technology investment +3; capital campaign, strategic plan, leadership change, merger +2; expansion, major grant, funding growth +1. Low-confidence signals (evidence not found) don't count;
   - IRS revenue up 20%+ over about 3 filings +1, down 20%+ −1;
   - a competitor donor CRM in the stack +1;
   - revenue in the target band +1, or −1 if under $500k;
   - a named decision-maker with an email +1.

   Being in the news no longer scores on its own; only news that describes a signal does. The weights are my judgment, not fitted to data (§12). `why_now` is filled from a template using only the signals that fired, so it reads the same way across 500k orgs.

**Debugging:** `-v` prints one block per stage. `output/debug/<domain>/` holds the scored candidate links, the **exact text sent to the LLM** and its raw response. A wrong field can then be traced to one of two causes: a **crawl** failure (the fact isn't in `extract_input.txt`) or an **extraction** failure (the fact is there and the LLM misread it).

---

## 7. Cost, scale and feasibility

### Measured per org
From the final batch, run on gpt-4.1-mini. Averages cover the 5 orgs with a full crawl (the one-page and JS-only sites are cheaper still).

| | Per org |
|---|---|
| Pages/files fetched | 9–13 |
| HTTP requests | about 12–16, including robots.txt, sitemap and the IRS lookup |
| LLM calls | 1. Gap-fill fired for 2 of the 7 orgs that reached extraction (RSPB and Habitat), making 3 calls there |
| Input tokens | 14.9k on average |
| Output tokens | 1.4k on average (0.7k–2.4k) |
| LLM cost | **$0.0075** on average ($0.0056–0.0104). Up from $0.0055 before buyer signals: a little more input from report and plan pages, more output, and one more gap-fill |
| Wall time | about 35 s, almost all of it the 1 s politeness delay between requests |

### Formula

```
cost/org = input_tokens × input_price + cached_tokens × cached_price + output_tokens × output_price
           (per 1M tokens: Claude Haiku 4.5 $1 / $0.10 / $5;  GPT-4.1-mini $0.40 / $0.10 / $1.60)
         + gap-fill share × (its tokens × prices)
500k × cost/org = first full crawl
```

### First full crawl of 500k orgs
Measured tokens (14.9k in, 1.4k out) × list prices × 500,000. This is conservative, because it ignores prompt-cache discounts:

| Model | $/org | 500k orgs | 500k with Batch API (−50%) |
|---|---|---|---|
| gpt-4.1-nano ¹ | $0.0020 | $1,020 | $510 |
| gpt-6-luna ¹ | $0.0022 | $1,089 | $545 |
| **gpt-4.1-mini** (what we ran) | **$0.0082** | **$4,081** | **$2,040** |
| Claude Haiku 4.5 | $0.0218 | $10,893 | $5,446 |
| gpt-5.4 (frontier) | $0.0579 | $28,960 | $14,480 |
| Claude Opus 5 (frontier) | $0.1089 | $54,464 | $27,232 |

¹ Not yet run against the eval set. A cheaper model only counts if `eval.py` still shows about 0 wrong.

The first pass costs about **$2k in LLM spend** with a small model and the Batch API, and roughly 13× that with a frontier model. "Under a cent per org" holds for gpt-4.1-mini-class models, but not for Haiku 4.5, which is about 2¢.

### Staying fresh (steady state; design, not built, see §12)
| Cadence | What runs | LLM $/month (gpt-4.1-mini, Batch API) |
|---|---|---|
| Monthly | Re-fetch careers, news and RFP pages (about 3 requests per org) and compare content hashes. Re-extract signals only where a page changed. Assumes 30% change a month, so 150k orgs × about 4k in / 0.4k out | about $170 |
| Yearly, spread monthly | Full re-crawl and extraction (identity, leadership, programs, reports and plans) | about $170 |
| When the IRS publishes | Registry refresh: revenue, EIN, NTEE | $0 (free API) |

That's about **$340 a month** to keep 500k profiles current, compared with about $2k for the first pass. The 30% change rate is an assumption; the per-section `last_checked` timestamps and stored ETags are what would measure it.

### Non-LLM costs
- **Requests:** about 12–15 per org (robots.txt, sitemap, 10 pages, a registry call), so about 7M requests per full pass. Bandwidth is trivial: text only, PDFs capped.
- **Compute:** the 1 s politeness delay makes each org take about 15–25 s of *wall time* but almost no CPU. Hosts are independent, so 200 concurrent workers finish 500k orgs in about 12–15 hours on a few small VMs, at tens of dollars.
- **Search API:** only for name-only inputs. Bulk runs should start from lists that already carry websites (the IRS 990 e-file data includes a website field), so search stays a fallback.
- **The expensive extras** would be headless rendering for JS sites (about 10× the CPU per page) and proxies for bot-protected sites. Both are best used only on sites flagged `partial`.

### What I'd change to keep the bill down at 500k
Items marked **(done)** are in V1; **(not built)** are next steps.

1. **(not built) Change detection first.** Store `ETag`/`Last-Modified` (already captured on every fetch) and a content hash per page. Re-extract only when a page changed. Most nonprofit sites change rarely, so this is the biggest lever for staying fresh.
2. **(not built) Tiered refresh using the per-section `last_checked`.** Signals (careers, news, RFP pages) are rechecked monthly, re-extracting only those pages with a smaller signals-only schema. Identity, leadership and programs are rechecked yearly. The registry is refreshed when the IRS publishes new data.
3. **(not built) Batch API** for bulk runs: 50% off, and latency doesn't matter here.
4. **(done) Registry first.** For US orgs the IRS data already gives EIN, revenue, NTEE and tax status for free, so PDFs aren't parsed just to find revenue.
5. **(done) Fewer output tokens.** Output tokens cost 4–6× input tokens and are already about 30% of the bill, so evidence stays on only 5 fields, lists are capped (8 programs, 8 leaders, 8 buyer signals, 8 news items), and fields code can derive are kept out of the LLM schema.
6. **(done) Prompt caching** of the static system prompt and schema, about 3.4k tokens. It's a smaller lever than it sounds. OpenAI caches long repeated prefixes automatically; Anthropic's Haiku 4.5 only caches prefixes of at least 4,096 tokens, so ours doesn't qualify. Either way, the per-org documents are what cost money, and they're unique.
7. **(partly done) Cheapest model that holds quality on the eval set.** A small model handles routine extraction; sending only orgs where extraction fails or confidence is low to a stronger model is not built. Switching model is one line in `.env` (`LLM_MODEL`), and `eval.py` decides whether the cheaper one is good enough.

---

## 8. Messy inputs

`python run.py --batch tests/messy_inputs.txt`

Results from the committed run (gpt-4.1-mini). All 10 inputs finished; nothing crashed the batch.

| Input | What it tests | Result |
|---|---|---|
| charitywater.org | large, well-structured charity | `ok`, score 0. EIN from the site footer, confirmed by the IRS record. 7 executives plus the board chair, including the founder (after fix 10). No buyer signals on the pages fetched (no plan, campaign or leadership change, and no reachable report PDF), so "nurture". An earlier run labelled the annual report itself a "strategic plan"; the tighter type definitions fixed that |
| communityoutreachgroupinc.com | tiny local org, one-page site | `ok`, score **−1** (under $500k). One page, no links to follow. EIN and revenue came from an exact IRS name match; PayPal Giving detected |
| rspb.org.uk | non-US (UK) | `ok`, score 3. Charity no. **207076** from the footer (after fix 1); IRS lookup skipped. Gap-fill fired but the pages it picked held no leaders or income figure, so those fields honestly stay null. The score comes from a **false positive**: a biodiversity-credit scheme it sells to developers, labelled `technology_investment` (see §12) |
| lfcfp.org | JS-rendered React site | `partial`, with the error "homepage has almost no visible text (likely rendered by JavaScript)". The IRS record still gives EIN, revenue and a **+70% revenue trend** (scored +1, offset by −1 for being under $500k) |
| cfwesternva.org | annual-report PDF one hop from the financials page | `ok`, score 4. The **2025 annual-report PDF** was reached through the hop. 7 staff with emails, mapped to buyer roles. New board members labelled `leadership_change` were **dropped by the board-appointment rule**; a learning programme it runs for other nonprofits still came back as `strategic_plan` (+2), a false positive |
| `Habitat for Humantiy` | typo'd name | `failed`: "no SEARCH_API_KEY is set; please provide the website URL". The search path (search → drop directories → verify the name on the page) is unit-tested but wasn't run live: no Serper key |
| this-charity-does-not-exist-4821.org | dead domain | `failed`: "could not connect (DNS or connection failure)" |
| basecamp.com | not a nonprofit | `partial`, flagged **`not_a_nonprofit?`**: "a commercial software company". **Not scored** (fix 14), although its product release was extracted as a technology investment |
| `habitat.org/?utm_source=…` | no scheme + tracking params | Normalised to `https://www.habitat.org/`, `ok`, score **6**, the best example of buyer signals working: a **new COO and a new CFO** (Sept 2026), a $6.3M Wells Fargo Foundation grant, and **Blackbaud Luminate** detected (a migration target for a CRM vendor). Two labels are loose (a partnership renewal as `funding_growth`, donated trucks as `expansion`). Gap-fill found the leadership page. The site and registry categories disagree, so it's flagged for a human |
| alleganfoundation.org | bot protection | `failed`: "HTTP 403 (access denied, possibly bot protection)" |

Scores range from −1 to 6. Habitat ranks first for a real reason (new finance and operations leaders plus a CRM migration target). None of these orgs has an open RFP or a buying-signal job right now. **Signal precision is the weak spot:** of the 6 buyer signals that scored across the sample, 2 are solid (Habitat's new leaders and its grant), 2 are loosely labelled (Habitat) and 2 are clear false positives (RSPB, cfwesternva).

---

## 9. Categorisation, kept consistent across 500k orgs

- **`cause_area`**: a fixed list of 14 buckets collapsed from the **NTEE major groups** (versioned: `taxonomy_version: ntee-major-v1`).
  - US orgs with an IRS record use their **registry NTEE code**. It's self-reported but consistent, and it costs nothing.
  - Otherwise the LLM chooses from the fixed list, with an `enum` in the schema, so it can't invent a category.
- **`size_band`**: pure rules on revenue (`<$500k`, `$500k–$5M`, `$5M–$50M`, `>$50M`, `unknown`), with approximate FX for non-USD figures.
- **`geography_served`**: a fixed set: local, regional, national, international.
- **`buyer_role`, `is_buying_signal`**: ordered keyword rules. "Board Chair" is checked before "executive", and "Youth Development Manager" is excluded from fundraising.

Keeping it consistent over time:
1. Fixed lists plus a versioned taxonomy. Every profile records its `taxonomy_version`, so a list change can trigger a re-map of stored profiles instead of silent drift (the re-map itself is not built).
2. Registry codes preferred wherever they exist.
3. A growing hand-labelled sample. `eval.py` is the seed; I rerun it by hand after every prompt or model change.
4. Disagreements between the registry code and the site-based pick are already flagged (`cause_area_disagreement:…`, e.g. Habitat: site says community_development, IRS says international). That flag is a ready-made spot-check queue.
5. (Not built) Spot-checks at scale: embed mission + programs and flag orgs whose category disagrees with their nearest neighbours.

---

## 10. Evaluation

Five orgs were hand-labelled by reading their sites directly, plus IRS data for EIN and revenue ([`tests/gold/`](tests/gold/)). About 10 fields each. `python eval.py` reports each field as:
- **correct**
- **wrong**: filled but incorrect, i.e. a hallucination or misread; the target is about 0
- **missed**: empty although the site has it

Results for the committed outputs (gpt-4.1-mini):

| field | correct | wrong | missed |
|---|---|---|---|
| name | 5 | 0 | 0 |
| registration_id | 5 | 0 | 0 |
| annual_revenue_usd | 4 | 0 | 0 |
| executive_name | 4 | 0 | 0 |
| contact_email | 4 | 0 | 0 |
| has_careers_page | 4 | 0 | 0 |
| open_roles_count | 2 | 0 | 0 |
| fundraising_tech | 5 | 0 | 0 |
| cause_area | 4 | 1 | 0 |
| country | 5 | 0 | 0 |
| hq_city | 5 | 0 | 0 |
| contact_phone | 1 | 0 | 0 |
| **overall** | **48/49 (98%)** | **1** | **0** |

The one "wrong" is charity: water's cause area. The site-based pick and the IRS code both say `human_services`; my label says `international` (clean water in developing countries). It's a genuine judgment call, and I left the label as I first wrote it rather than editing it to match.

The score is unchanged after adding buyer signals and slimming contacts, so the lighter contact extraction lost nothing on these fields. Buyer signals themselves aren't labelled yet (§12).

**The eval moved during development:** 94% → 96% → 98%, with fixes to both the pipeline and the eval (see §11, items 9–12). Five orgs and 49 labels is a smoke test, not a benchmark; the value is that every prompt or model change reruns the same labelled sample.

---

## 11. What broke during development (and the fix)

1. **Registration numbers disappeared during text cleaning.** RSPB's "registered charity no. 207076" sits in a `<footer>` wrapped in `<nav>`. trafilatura drops footers and the fallback drops navs. Fix: append the tail of the homepage footer.
2. **Keyword link scoring misfired:**
   - `/about/financials` was classed as "about";
   - `/research` was blocked by the negative keyword `search`;
   - `/accountability` was blocked by `account`;
   - news posts like `/inspiring-young-people-to-…` were picked as the "team" page.

   Fix: the last path segment decides the category, keywords match on word boundaries, and long article-like slugs are penalised.
3. **Registry name matching is dangerous.**
   - ProPublica's top hit for "charity water" is *Water Charity*, a different org.
   - Ten IRS entities are called "Habitat for Humanity International".

   Fix: prefer the EIN printed on the site; otherwise require a *unique* near-exact name match in the same state, narrowed by city; otherwise no match.
4. **Annual-report PDFs are never on the homepage.** They sit one click away on the financials page. Fix: one PDF hop, choosing the newest year in the file name, not in the upload folder.
5. **A CMS false positive.** A link to a county website's Drupal files made a Squarespace site look like Drupal. Fix: CMS detection uses only the page's own assets, never outbound links.
6. **Registry categories can be wrong.** charity: water's NTEE code is P80 ("services to promote independence"), so the rules label it `human_services` although it's plainly international development. Registry codes win for consistency, and the eval surfaces the cost of that choice.

Found in the live LLM runs (the debug folder shows whether each was a crawl or an extraction problem):

7. **An invented URL.** For cfwesternva.org the model returned a contact page `/contact/` that the site never links to (it's a 404). Extraction problem. Fix: every URL in the output (contact page, job, RFP and news links) must be a page we fetched or a link that appears in the fetched HTML or text; otherwise it's removed and logged.
8. **Other organisations' impact counted as the org's own.** A community foundation's `impact_metrics` listed its grantees' results (the YMCA's, the SPCA's). Fix: the prompt now asks for the organisation's own outcomes only.
9. **A registration number returned as a phrase.** RSPB's came back as "England and Wales no. 207076, Scotland no. SC037654": correct, but useless as a CRM join key. Fix: ask for the number only.
10. **The founder came back as just "Scott".** The executive-team page puts full names in `<h2>` card headings. trafilatura dropped those as boilerplate and kept the bios, one of which starts "Scott spent…". Crawl/cleaning problem: "Scott Harrison" never reached the model. Fix: team pages always use the full visible text (about 1k more tokens per team page).
11. **A registry match on a name alone.** The JS-rendered site gave no state to narrow the search, and the unique name match turned out right. It's still the riskiest path, so without a state the name must now match exactly.
12. **The eval itself had bugs.**
    - One "wrong" was *my* gold label: I missed the email on the one-page site.
    - The name matcher was too strict: "Jonathan T.M. Reckford" didn't count as Jonathan Reckford.
    - It was also too lenient: "Scott" counted as Scott Harrison.

    I fixed all three and reported the numbers after the fixes. Lesson: a gold set needs a second labeller before anyone trusts its numbers.

Found after adding buyer signals:

13. **Programme activity read as buyer signals.** A community foundation's public nonprofit directory came back as `technology_investment`, and a learning programme it runs *for other nonprofits* as `strategic_plan`, inflating its score to 7. charity: water's annual report was labelled a strategic plan, and new board members a leadership change. Extraction problem. Two prompt passes (signals must be about the organisation itself; a stricter definition per type) fixed some of it. gpt-4.1-mini still ignores the finer rules, so where a rule can be checked in code it now is: `leadership_change` signals that mention a board are dropped. The rest is a documented limitation (§12).
14. **A software company scored 4** from its own product release. It was already flagged `not_a_nonprofit?`, but the flag didn't stop scoring. Fix: flagged sites aren't scored and aren't marked `ok`.

---

## 12. Limitations

- **Refresh is designed, not built.** Re-running an org re-crawls it fully; only the local LLM response cache avoids paying twice for identical content. ETags and per-section `last_checked` are already recorded, so change detection and tiered refresh (§7) can be added without schema changes.
- **JS-rendered sites** (like lfcfp.org) give near-empty HTML. They're detected and marked `partial`; headless rendering isn't in V1.
- **Bot-protected sites** (403) fail cleanly. The crawler doesn't evade protection.
- **Name inputs** need a search API key and fail safely without one. The search path is unit-tested but hasn't been run live (no Serper key during development).
- **Registry data is US-only.** Other countries get what their site says; the UK Charity Commission API would be the next source.
- **Team pages built from images or JS carousels** hide leadership from text extraction. Gap-fill helps, but can't read images.
- **Chapters and affiliates** (e.g. local Habitat affiliates) aren't linked to their national parent.
- **RFPs are rare** on nonprofit sites, so `open_rfps` is usually and correctly empty.
- **Prompts and keyword rules are English-only.**
- The size band for non-USD revenue uses **approximate** FX rates.
- **About 35 s per org**, mostly the politeness delay. That's fine in bulk, since sites are crawled in parallel, but slow for one interactive lookup.
- **Only 5 hand-labelled orgs.** The eval catches regressions; it doesn't prove accuracy at 500k.
- **Buyer-signal precision is the weakest part.** In the final batch, 2 of the 6 scoring signals are false positives (a service RSPB sells labelled `technology_investment`; a programme cfwesternva runs for others labelled `strategic_plan`) and 2 more are loosely typed. The grounding check proves the evidence is on the page, not that the type is right. Next steps: a stronger model only for the signal list (a few hundred output tokens), or a second cheap check per signal.
- **Buyer signals aren't in the eval yet.** The gold labels predate them, so their accuracy rests on the grounding check and on spot-reading the outputs, as in §8.
- **The `plans` keyword "strategy" also matches long news slugs** (e.g. an RSPB article ending in `-strategy`). The long-slug penalty lowers them but doesn't always exclude them.
- **Score weights are judgment, not data.** Whether a strategic plan is worth +2 or +1 should be fitted against which leads actually converted, which needs a customer's CRM outcomes.
- **Signal types are the LLM's call** within a fixed list, so the line between, say, `expansion` and `strategic_plan` can vary between orgs.
- **Personal data.** Contacts are limited to up to 8 named leaders listed by the org itself. Emails and phones are kept only if they appear verbatim on a fetched page (the grounding check removes the rest), and addresses are never guessed from naming patterns. There is no opt-out or deletion process, and EU/UK orgs (GDPR) would need a proper legal basis before this data is resold.
- **Prompt injection.** Scraped text goes into the prompt, so a page could try to instruct the model. The damage is limited: output is a fixed schema, values not found on the fetched pages are dropped or downgraded, and registry data overrides EIN and revenue. The prompt doesn't yet explicitly tell the model to treat page text as untrusted data.

---

## 13. With more time

1. Headless rendering (Playwright), only for pages flagged near-empty.
2. Batch API and a job queue for bulk runs; change detection with ETag and content hashes; section-level refresh.
3. More registries: UK Charity Commission, Canada CRA, and IRS e-file data for websites and officer names.
4. A `sponsor_view` for corporate sponsorship teams, with events and audience size.
5. CRM push (HubSpot / Salesforce) straight from `to_csv_row`, with dedupe on `registration_id` then domain.
6. Linking affiliates to parents; a larger gold set with confidence calibration, including buyer-signal labels.
7. Fit the lead-score weights to real outcomes (which signals preceded a won deal), and let gap-fill hunt for a missing annual report or strategic plan, not only for contacts and revenue.

---

## 14. Tools

| Tool | Why |
|---|---|
| Python 3.12, **httpx** + **tenacity** | Timeouts, retries with backoff, streaming size caps |
| **trafilatura** + **BeautifulSoup** | Main-text extraction, with a fallback for card or grid layouts |
| **pymupdf** | Fast PDF text; page-level selection |
| **pandas** | CSV sniffing and preview |
| **Pydantic** | One schema for the LLM (JSON schema), for validation, and for the output |
| **Anthropic or OpenAI SDK** | Whichever key you have: the provider is detected from the key. Defaults are Claude Haiku 4.5 and GPT-4.1-mini, cheap tiers with strict structured outputs (JSON that matches the schema). `LLM_MODEL` switches model and `LLM_BASE_URL` points at an OpenAI-compatible server (Ollama, OpenRouter) for local or other models |
| **ProPublica Nonprofit Explorer API** | Free IRS 990 data: EIN, revenue history, NTEE, tax status |
| **Serper** (optional) | One search call for name → website |

Setup:

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env    # paste ONE key into LLM_API_KEY (Anthropic or OpenAI), optionally SEARCH_API_KEY
pytest -q               # no key needed
python run.py -v https://www.charitywater.org
```

**Using your own key.** Set `LLM_API_KEY` in `.env`. That's all you need:
- `sk-ant-…` keys run Claude Haiku 4.5; any other key runs OpenAI GPT-4.1-mini.
- Set `LLM_PROVIDER` only if the guess is wrong; set `LLM_MODEL` to try another model.
- For local models, use `LLM_BASE_URL=http://localhost:11434/v1` (Ollama) plus `LLM_MODEL=<name>`. The model must support JSON-schema output.
- Costs for models not in [`src/cost.py`](src/cost.py) show as unknown unless you set `LLM_PRICE_IN` / `LLM_PRICE_OUT`.
- ProPublica needs no key. `SEARCH_API_KEY` (Serper, free tier) only matters for name inputs.

## 15. What I spent

**$0.24 in total** (`python run.py --spend`): 63 runs, 51 real LLM calls, 492k input and 48k output tokens, on gpt-4.1-mini. That includes every development run and three batch runs while adding buyer signals. The final 10-input batch cost about **$0.04**. Re-running anything unchanged costs $0, thanks to the local LLM response cache (`.cache/llm/`).
