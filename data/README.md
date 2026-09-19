# Data

Hamsa uses two kinds of data. They are kept apart on purpose and are never mixed up in the bot's output.

| Folder | What it is | Origin |
|---|---|---|
| `csv/` | Synthetic dataset: 10 tables (products, batches, NSQ alerts, scan events, reports, disputes, …) | **Synthetic** — every row is generated. Fictional manufacturers are used wherever a record says something is wrong with a batch. |
| `real/` | **529 real CDSCO NSQ rows** parsed from six official monthly PDFs (April–June 2025), plus the rows that were rejected | **Real** — public CDSCO documents |
| `docs/` | `data_dictionary.md` (field-by-field notes for the synthetic dataset) and `test_scenarios.md` (15 hand-picked batches and their verified verdicts) | — |

The same synthetic rows are also loaded into PostgreSQL by `supabase/seed.sql`; the CSV files exist for inspection and for loading into anything else.

## Synthetic dataset

| Table | Rows |
|---|---|
| products | 126 |
| manufacturer_aliases | 304 (42 alias groups) |
| batches | 650 |
| nsq_alerts | 200 |
| scan_events | 2,526 (2 more serial-replay demo events are added by `supabase/demo_fixups.sql`) |
| scan_evidence | 2,392 |
| reports | 150 |
| disputes | 30 |
| consent_log | 4,000 |
| audit_log | 700 |

Design decisions:

1. **Real manufacturer names are never attached to a fabricated quality failure.** The clean product catalogue uses 30 real, well-known Indian manufacturers. Every NSQ alert, counterfeit-suspicion scan and dispute uses a separate pool of 20 invented company names (for example "Amrutha Drugs Ltd."). A synthetic record must never read as a real regulatory finding about a real company.
2. **`scan_evidence` holds full Evidence Matrices for 398 scans, not all 2,526.** Every one of the 15 scenario batches has a full matrix, plus a stratified sample of red / amber / expired / green scans.
3. **`batch_number` is UNIQUE in `batches`**, required for `disputes.batch_number` to be a valid foreign key.
4. **`nsq_alerts` has no state or district column**; for state-lab alerts the state is part of `source_document`.
5. **`scan_events` and everything derived from it are preview data** for the replay/clone check and are labelled as such in the bot.

## Real NSQ data (`real/`)

| File | Contents |
|---|---|
| `nsq_real_loaded.csv` | The 529 accepted rows (product, batch, manufacturer, reason, source PDF, page, lab type, …) |
| `nsq_real_rejected.csv` | The 38 candidate rows that failed validation, with the reason |
| `pdfs/` | The six source PDFs: April, May and June 2025, central and state lists |

Every row was accepted only if its batch, product and manufacturer all appear in the source page text and its dates parse. `alert_date` is the last day of the alert month, because the PDFs carry no per-row date. All rows are stored as `extraction_confidence = 'medium'`. The six source PDFs (public CDSCO notices) are included in `real/pdfs/`, so every row can be checked against its original page; to fetch newer months run `python -m ingest.scheduler --once` from `api/`. Load the accepted rows with:

```bash
python api/scripts/load_real_nsq.py data/real/nsq_real_loaded.csv
```

## Loading the synthetic dataset

```bash
python api/scripts/db_setup.py --db hamsa --reset     # schema + seed + migrations + demo fixups
```

or, without the helper, run the files in `supabase/migrations/` in order, then `supabase/seed.sql`. See `docs/data_dictionary.md` for distributions and field notes and `docs/test_scenarios.md` for the scenario batches.
