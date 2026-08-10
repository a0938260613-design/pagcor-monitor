# MeitY Documents Monitor Strategy

## Objective

MeitY (India's Ministry of Electronics and Information Technology) publishes regulatory, policy, and reference documents through `meity.gov.in/documents`. The monitor must track every document category under that section, not only a hand-picked subset.

The operating principle is the same as the PAGCOR monitor's:

- collect everything
- preserve every snapshot
- classify every change
- report in a way that business readers can understand

## Scope

The monitor covers all documents listed under:

```text
https://www.meity.gov.in/documents
```

across all 7 document categories the site itself exposes: Report, Act and Policies, Orders and Notices, Publications, Press Release, Gazettes Notifications, Guidelines.

Unlike PAGCOR's site, MeitY's document listing is served by a WordPress REST API (`cms/wp-json/document/documents`) rather than discovered by crawling links, so the monitor enumerates these 7 known categories directly instead of following an open-ended link graph. If MeitY adds an 8th category in the future, it will not be picked up automatically - this is a known, accepted limitation, not something the system can self-detect.

## Change Types

The monitor detects, at the document ("post") level:

- new document
- removed document
- document metadata changed (title, year, modified date)
- files added to a document
- files removed from a document
- files changed within a document (different size, filename, or other file-level fields)

v1 explicitly does **not** download PDF file contents to diff text, and does not take screenshots. The API provides a genuine `post_modified` timestamp per document - a reliable free change signal PAGCOR's site does not have - so metadata-level tracking (title, modified date, file list, file sizes) is the primary detection mechanism. This can be extended to content-level PDF diffing later using the same PDF-handling utilities already built for PAGCOR, if metadata-level tracking proves insufficient in practice.

## Severity Model

Severity is assigned by document category, anchored to PAGCOR's own tiers where a reasonable analogy exists:

### Critical

- **Act and Policies** - regulatory or policy amendments
- **Gazettes Notifications** - official gazette publications with legal effect

### High

- **Orders and Notices** - official orders/notices
- **Report** - reports and statistical/financial data

### Medium

- **Guidelines**
- **Publications**
- **Press Release** (no strong PAGCOR analogy - revisit if this proves too noisy or too quiet in practice)
- any unrecognized/future category (safe default, per the "don't exclude just because it looks low-value" principle)

A change is downgraded to **Low** regardless of category whenever only `post_modified` changed but the document's actual content (title, year, file list) did not - the same "cosmetic-only" downgrade PAGCOR applies.

## Reporting Principle

Same as PAGCOR: Telegram receives a concise stats-only summary (monitored category count, fetch failures, total changes, severity counts, category distribution, links to the full report). The full HTML/Markdown report contains every detected change so nothing is lost. The two monitors' Telegram messages are sent separately (each with its own clear title) to the same chat, and their HTML reports cross-link to each other so a reader can move between the two without them being blended together.

## Baseline Runs

The first run creates the baseline and marks every discovered document as added. This is expected and not a real change.

If any of the 7 categories fails to fetch during a run (network error, unexpected response), that category's documents are excluded from comparison for that run (never reported as "removed" just because the fetch failed), and the whole run's state is **not** saved - the previous baseline is preserved untouched so the next run can retry cleanly, mirroring PAGCOR's own incomplete-crawl safeguard.

## Known Duplication

A small number of files (about 3.5% of all files across the 7 categories, confirmed during initial reconnaissance) are legitimately listed under more than one document/category on MeitY's own site. v1 does not de-duplicate this - if such a file changes, it may appear under more than one reported change. This is treated as an acceptable edge case for now; if it becomes a real source of confusion, it can be addressed the same way PAGCOR's front-end/back-end duplicate reporting was resolved (`mark_subordinate_changes`).
