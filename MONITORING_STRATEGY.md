# PAGCOR Regulatory Monitor Strategy

## Objective

PAGCOR is a primary regulatory source for the Philippine market. The monitor must track all changes under the PAGCOR regulatory site, not only pre-selected high-value pages.

The operating principle is:

- collect everything
- preserve every snapshot
- classify every change
- report in a way that business readers can understand

## Scope

The monitor covers resources under:

```text
https://www.pagcor.ph/regulatory/
```

Included resource types:

- HTML pages
- PDF files
- Excel files
- Word/document files
- CSV files
- forms, manuals, advisories, notices, frameworks, requirements, lists, and statistics

No page should be excluded only because it appears low-value. Low-value changes should still be recorded, then classified as Low in the report.

## Change Types

The monitor should detect:

- new resource
- removed resource
- HTML visible text change
- added or removed links
- file content hash change
- PDF extracted text change
- added or removed domains / URLs
- added or removed dates
- licensee / accredited entity / cancelled entity list changes
- regulation, framework, amendment, notice, manual, requirement, or form changes

## Severity Model

### Critical

Changes that may directly affect market access, compliance, permitted brands, operators, or domains:

- licensee list changes
- accredited entity list changes
- cancelled entity / cancelled licensee changes
- registered brands, domain names, URLs changes
- gaming system administrator changes
- formal regulation / framework / amendment changes
- notices about reported websites, counterfeit certificates, or compliance warnings

### High

Changes likely to matter commercially or operationally:

- new announcements
- new or changed application kits
- new or changed requirements
- industry statistics releases
- schedule of fees changes
- major operational request form updates

### Medium

Changes that should be reviewed but are less urgent:

- manual updates
- guidelines
- form changes
- technical standards
- department/process wording changes

### Low

Changes that should be logged but normally do not need immediate action:

- footer changes
- navigation changes
- images or static assets
- layout or minor static copy changes
- contact block changes unless tied to a regulated process

## Reporting Principle

Telegram should not receive a raw diff dump. It should receive a concise daily summary:

- total monitored resources
- total changes
- counts by severity
- Critical and High details
- Medium summary
- Low count with optional appendix reference

The full Markdown or HTML report should contain all detected changes so nothing is lost.

## Baseline Runs

The first run creates the baseline and will mark discovered resources as added. This is expected and should not be interpreted as a real market change.

Full baseline can take several minutes because the site contains many PDFs and downloadable files. Automation should allow at least 10-15 minutes for full runs.
