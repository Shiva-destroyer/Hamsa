# Scenario batches

Fifteen hand-picked batch numbers in the synthetic dataset, chosen so that every branch of the Evidence Engine has a known batch to demo or test against. Every manufacturer involved in a flagged scenario is fictional (see `data/README.md`).

Enter any of them as text in the WhatsApp bot, or send `{"input_type": "manual", "batch_number": "<batch>"}` to `POST /api/verify` (or `GET /api/lookup-batch/<batch>`).

## Manual entry

Verified by running `engine.verify_manual` on a freshly built database (`db_setup.py --reset`) on 19 Sep 2026. Manual entry can only evaluate the official NSQ record, expiry, and the replay preview; it has no code or label to compare.

| Batch | Verdict | Reason | What it shows |
|---|---|---|---|
| `AX2291` | 🔴 RED | `nsq_match` | Active NSQ listing (synthetic record) |
| `FM9184` | 🔴 RED, also expired | `nsq_match` | Expired **and** NSQ-listed: RED wins, "also expired" is added |
| `PT8813` | 🔴 RED + "under review" | `nsq_match` | An open dispute adds a banner but never changes the verdict |
| `TX6690` | 🔴 RED | `nsq_match` | Dispute resolved as *upheld*: the listing stands |
| `EN3302` | ⚪ EXPIRED | `expired` | Expiry is its own outcome, not RED |
| `JK4471`, `CT5510`, `BQ7743`, `DR8820` | 🟠 AMBER | `single_signal` | Only the replay preview (synthetic) fires: one weak signal caps at AMBER |
| `MQ7756`, `NS3340`, `GH6625`, `LP2098` | 🟢 GREEN | `no_warning` | Nothing on file. GH6625 and LP2098 only carry signals on the photo path |
| `RV5567`, `SW1123` | 🟢 GREEN | `no_warning` | An earlier NSQ listing was corrected / overturned; the history is shown in `TIMELINE <batch>` |

## Photo path

Photographing a printed pack adds the code and label checks. The printable packs and their expected results are in [`api/assets/packs/README.md`](../../api/assets/packs/README.md). Verified on 19 Sep 2026 with `make_demo_packs.py --verify` (10/10) and `make_qr_sheet.py --verify` (15/15): for example `GH6625` (printed manufacturer differs) → AMBER, `CT5510` and `BQ7743` (printed manufacturer differs **and** replay flag) → RED, escalated, `LP2098` (blurred print) → AMBER, and a non-GS1 QR → "type the batch number".
