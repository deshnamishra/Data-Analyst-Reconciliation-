# Reconciliation Take-Home — May/June 2026

## 1. Monthly results

**May.** The script reconciles 328 successful internal transactions against 252 bank credits. At May month-end, 298 internal transactions are linked into 1:1, 1:N, N:1 or N:M reconciliation groups; 5 groups are PARTIAL_MATCH because of settlement shortfalls/amount mismatches; 30 internal transactions remain OPEN. Of those 30, 22 form the opening backlog that clears through June PREV CYCLE credits, while 8 remain open at June month-end. Duplicate credits and orphan bank credits are not reused as matches.

**June.** The script reconciles 288 successful June transactions and also applies the May opening backlog. All 22 May backlog items with PREV CYCLE credits are cleared in June. The remaining open items at June month-end are the 8 uncleared May backlog items plus 24 June transactions. The June open June transactions are classified using the stated age rule.

## 2. Matching methodology and pass order

1. Validate every input file, required column, date, amount and required identifier.
2. Reserve offsetting SALE/REVERSAL records with the same payment reference and zero net as **Internal self-netting pair** exceptions.
3. Match `batch_id` groups to bank batch settlements. This handles N:1 and identifies fee-driven PARTIAL_MATCH cases.
4. Match payment references after normalization: case, punctuation and spacing are ignored; a six-digit reference prefix is accepted for truncated narrations. For partial-credit references, all unused related bank lines are aggregated, handling 1:N.
5. For malformed references, use a unique exact-amount match only when the same reference/prefix is present in the narration. This controls false matches.
6. Match explicit `NET SETTLEMENT` merchant/date groups using an exact subset-sum of internal transactions to the combined bank total. This handles N:M and prevents artificial one-to-one splitting.
7. Carry May unmatched items into June and match June `PREV CYCLE` bank credits to the May internal reference and exact amount.
8. Remaining bank lines are classified as Duplicate credit when tied to an already matched internal reference, otherwise Orphan credit. Remaining internal transactions stay OPEN.

A transaction or bank line is marked used as soon as it is assigned to a match group. All later passes filter on the used sets, so the same record can never be consumed twice.

## 3. Backlog and ageing rule

May opening backlog: **30 items; 22 cleared in June; 8 still open at June month-end.**

Lag classification rule: **7 days or less at month-end = Likely Settlement Lag; more than 7 days = Genuine Exception.** This is a conservative operational rule: late-month items can reasonably settle after month-end, while older items need investigation.

## 4. Exception summary

The exception report contains 90 rows across both monthly reporting views. Main categories and counts are:

reporting_month         exception_category  count      value    variance  max_ageing_days
           JUNE           Duplicate credit      5  234660.91        0.00               23
           JUNE Internal self-netting pair      3       0.00        0.00               23
           JUNE              Orphan credit      4  210295.43   210295.43               22
           JUNE  Reference/amount mismatch      2   36849.71     -667.15               26
           JUNE           Short settlement      2  157671.49     -726.77               21
           JUNE      Unsettled transaction     24 1475441.70 -1475441.70               22
            MAY           Duplicate credit      6  261562.13        0.00               25
            MAY Internal self-netting pair      4       0.00        0.00               28
            MAY              Orphan credit      5  212381.41   212381.41               26
            MAY  Reference/amount mismatch      3   74256.73     -941.29               19
            MAY           Short settlement      2  126464.21     -541.93               19
            MAY      Unsettled transaction     30 1566002.71 -1566002.71               29

## 5. Output design

For 1:1, the flat row is operationally convenient and auditor-friendly. For 1:N and N:1, a common `match_group_id` with pipe-delimited member references preserves traceability without pretending that each member was independently matched. N:M is best represented as a match group rather than a Cartesian pair table, because forcing every internal line against every bank line creates false relationships. A parent-child model is the cleanest long-term design: one match-group parent with child internal and bank records. The delivered CSV uses the same concept in a reproducible flat representation.

## 6. Assumptions and limitations

- Bank statement rows are business-relevant credits only, as instructed.
- INR amounts are rounded to cents using `Decimal` before matching.
- Reference obfuscation is normalized for case/spacing/punctuation and accepts a six-digit reference prefix for truncated bank narrations.
- Settlement lag classification uses the explicit 7-day rule above.
- No web/external data was used; all numbers are derived from the supplied four files.
- Input validation found no read, required-column, date, amount or identifier errors.

**Actual processing time:** 1.71 seconds for validation, reconciliation, report generation and HTML dashboard generation on the submission runtime.
