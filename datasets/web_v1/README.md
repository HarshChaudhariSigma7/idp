# web_v1: 25 real documents from the public web

Stress set, not a benchmark: too small and too template-heavy for accuracy claims (the gate needs
20+ real customer documents per bucket). Use it to catch regressions on real-world messiness.

| Bucket | n | What it stresses |
|---|---|---|
| printed | 14 | GST templates, Tally e-invoice, NIC e-invoice, POS receipt, computer bilty; 3 blank templates (hallucination traps) |
| handwritten | 6 | crumpled phone photo with overwritten total, 1941 cursive memo, perspective photo of a motorcycle bill, carbon-copy TCI LR, handwritten LR book |
| vernacular | 3 | Hindi printed cash memo with handwritten figures, Hindi-header restaurant bill at 310×450 px, 1960 memo with handwritten Devanagari numerals |
| poor_scan | 2 | sideways-scanned consignment note, photo of a blank voucher (not an LR) |

`must_review` lists fields that must never be auto-accepted with a value (blank or genuinely
illegible). Labels: Claude, visually, 2026-09-27; get a second human pass before quoting numbers.

## Findings from the offline run (no model), all fixed with regression tests

1. **False refusal**: a readable photo was rejected as "too low resolution": paper texture specks
   outvoted real glyphs in the text-size estimate. Estimator now counts only text-line components,
   weighted by ink (`quality._char_height`).
2. **Validator false alarm**: NIC e-invoices print no per-line tax column, so "line total" failed on
   correct values. Now reconciles taxable × (1 + rate).
3. **Validator false alarm**: tax-inclusive POS receipts with mixed 0/3/5/12% rates tripped the
   effective-rate check. Now reconciles via tax-inclusive line totals.
4. **Clean renders bucketed as poor scans** (grey fonts): contrast threshold recalibrated.
5. **Sideways scans**: added a model-free quarter-turn detector (caught the rotated consignment
   note, 0 false positives on the other 24).
6. **True catches on real samples**: invalid GSTIN check digits on 6 sample invoices, a 13-char
   GSTIN, the same GSTIN on buyer and seller, an 8-digit e-way bill, an 8-char IFSC.
