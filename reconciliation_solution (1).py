import os, re, time
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR = os.path.join(BASE, "Input Files")
OUTPUT_DIR = os.path.join(BASE, "Reconciliation output files")
INPUTS = {
    "internal_may": os.path.join(INPUT_DIR, "internal_txns_may2026.csv"),
    "bank_may": os.path.join(INPUT_DIR, "bank_stmt_may2026.csv"),
    "internal_jun": os.path.join(INPUT_DIR, "internal_txns_jun2026.csv"),
    "bank_jun": os.path.join(INPUT_DIR, "bank_stmt_jun2026.csv"),
}
REQ_INTERNAL = ["txn_id","txn_date","channel","merchant_id","txn_type","amount","currency","payment_ref","batch_id","status"]
REQ_BANK = ["line_id","value_date","narration","dr_cr","amount","bank_ref"]

def cents(x):
    return int(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)

def money(x):
    return round(float(x), 2)

def normalize(s):
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())

def ref_prefix(ref, digits=6):
    s = normalize(ref)
    return s[:5 + digits]

def age_days(start, end):
    return max(0, (pd.Timestamp(end) - pd.Timestamp(start)).days)

def lag_classification(txn_date, month_end):
    age = age_days(txn_date, month_end)
    return ("Likely Settlement Lag" if age <= 7 else "Genuine Exception"), age

def validation_report():
    rows = []
    for name, path in INPUTS.items():
        has_issue = False
        try:
            df = pd.read_csv(path)
            rows.append([name, "READ_OK", "File read successfully", "", ""])
        except Exception as exc:
            rows.append([name, "READ_ERROR", str(exc), "", ""])
            continue
        required = REQ_INTERNAL if name.startswith("internal") else REQ_BANK
        missing = [c for c in required if c not in df.columns]
        if missing:
            has_issue = True
            rows.append([name, "MISSING_COLUMNS", ",".join(missing), "", ""])
        datecol = "txn_date" if name.startswith("internal") else "value_date"
        if datecol in df.columns:
            bad = int(pd.to_datetime(df[datecol], errors="coerce").isna().sum())
            if bad:
                has_issue = True
                rows.append([name, "INVALID_DATE", f"{bad} invalid/missing {datecol} value(s)", datecol, ""])
        if "amount" in df.columns:
            bad = int(pd.to_numeric(df["amount"], errors="coerce").isna().sum())
            if bad:
                has_issue = True
                rows.append([name, "INVALID_AMOUNT", f"{bad} invalid/missing amount value(s)", "amount", ""])
        idcols = ["txn_id", "payment_ref"] if name.startswith("internal") else ["line_id", "bank_ref"]
        for c in idcols:
            if c in df.columns:
                bad = int(df[c].isna().sum() + (df[c].astype(str).str.strip() == "").sum())
                if bad:
                    has_issue = True
                    rows.append([name, "MISSING_IDENTIFIER", f"{bad} missing/blank {c} value(s)", c, ""])
        if not has_issue:
            rows.append([name, "VALIDATION_OK", "No basic structural/date/amount/identifier issues detected", "", ""])
    return pd.DataFrame(rows, columns=["file","issue_type","description","field","sample"])

def load_inputs():
    data = {}
    for month in ["may", "jun"]:
        i = pd.read_csv(INPUTS[f"internal_{month}"])
        b = pd.read_csv(INPUTS[f"bank_{month}"])
        i["_idx"] = i.index.astype(int)
        b["_idx"] = b.index.astype(int)
        i["txn_date_dt"] = pd.to_datetime(i["txn_date"], errors="coerce")
        b["value_date_dt"] = pd.to_datetime(b["value_date"], errors="coerce")
        i["amt_c"] = i["amount"].map(cents)
        b["amt_c"] = b["amount"].map(cents)
        i["norm_ref"] = i["payment_ref"].map(normalize)
        b["norm_narr"] = b["narration"].map(normalize)
        data[month] = (i, b)
    return data

def identify_self_net(i):
    ids, groups = set(), []
    for ref, g in i.groupby("payment_ref"):
        types = set(g["txn_type"].astype(str))
        if len(g) >= 2 and int(g["amt_c"].sum()) == 0 and {"SALE", "REVERSAL"}.issubset(types):
            inds = g["_idx"].astype(int).tolist()
            ids.update(inds)
            groups.append(inds)
    return ids, groups

def subset_sum_indices(cands, target, max_n=22):
    vals = [int(x) for x in cands]
    if len(vals) > max_n:
        return None
    sums = {0: ()}
    for pos, val in enumerate(vals):
        current = list(sums.items())
        for s, combo in current:
            ns = s + val
            if ns not in sums:
                sums[ns] = combo + (pos,)
        if target in sums:
            return list(sums[target])
    return None

def run_month(month, data):
    i, b = data[month]
    month_end = pd.Timestamp("2026-05-31" if month == "may" else "2026-06-30")
    used_i, used_b = set(), set()
    records = []
    gnum = 1

    def gid():
        nonlocal gnum
        val = f"{month.upper()}-{gnum:04d}"
        gnum += 1
        return val

    self_ids, self_groups = identify_self_net(i)
    for inds in self_groups:
        G = gid()
        used_i.update(inds)
        records.append({
            "group": G, "cardinality": f"{len(inds)}:0", "status": "EXCEPTION",
            "method": "Internal self-netting pair",
            "comment": "Offsetting SALE and REVERSAL share payment_ref and net to zero; no bank settlement expected.",
            "i_idxs": inds, "b_idxs": [], "category": "Internal self-netting pair"
        })

    batch_rows = i[i["batch_id"].notna() & ~i["_idx"].isin(used_i)]
    for batch, gi in batch_rows.groupby("batch_id"):
        gb = b[b["narration"].str.contains(str(batch), case=False, regex=False, na=False) & ~b["_idx"].isin(used_b)]
        if gb.empty:
            continue
        target = int(gi["amt_c"].sum())
        exact = gb[gb["amt_c"] == target]
        if len(exact):
            bsel = [int(exact.iloc[0]["_idx"])]
        else:
            sol = subset_sum_indices(gb["amt_c"].tolist(), target, 12)
            bsel = [int(gb.iloc[p]["_idx"]) for p in sol] if sol is not None else [int(gb.iloc[0]["_idx"])]
        bs = sum(int(b.loc[x, "amt_c"]) for x in bsel)
        G = gid()
        iidx = gi["_idx"].astype(int).tolist()
        used_i.update(iidx); used_b.update(bsel)
        status = "MATCHED" if bs == target else "PARTIAL_MATCH"
        records.append({
            "group": G, "cardinality": f"{len(iidx)}:{len(bsel)}", "status": status,
            "method": "Batch ID match",
            "comment": "Batch total agrees." if status == "MATCHED" else "Batch settlement is net of charges; bank amount is short versus internal batch total.",
            "i_idxs": iidx, "b_idxs": bsel,
            "category": None if status == "MATCHED" else "Short settlement"
        })

    for ref, gi in i[~i["_idx"].isin(used_i)].groupby("payment_ref"):
        if any(int(x) in self_ids for x in gi["_idx"].tolist()):
            continue
        pr = normalize(ref)
        pfx = ref_prefix(ref, 6)
        gb = b[(~b["_idx"].isin(used_b)) &
               (b["norm_narr"].str.contains(pr, regex=False, na=False) |
                b["norm_narr"].str.contains(pfx, regex=False, na=False))]
        if gb.empty:
            continue
        iamt = int(gi["amt_c"].sum())
        exact_single = gb[gb["amt_c"] == iamt]
        if len(gi) == 1 and len(exact_single):
            bsel = [int(exact_single.iloc[0]["_idx"])]
            status = "MATCHED"
            method = "Reference + exact amount"
            comment = "Related bank line selected; any redundant related credits remain unconsumed for duplicate-credit exception handling."
        else:
            bsel = gb["_idx"].astype(int).tolist()
            bs = sum(int(b.loc[x, "amt_c"]) for x in bsel)
            status = "MATCHED" if bs == iamt else "PARTIAL_MATCH"
            method = "Reference aggregate match"
            comment = ("All related bank lines aggregated by normalized/truncated reference."
                       if status == "MATCHED" else
                       "Related bank lines found, but the net amount does not fully reconcile.")
        bs = sum(int(b.loc[x, "amt_c"]) for x in bsel)
        G = gid()
        iidx = gi["_idx"].astype(int).tolist()
        used_i.update(iidx); used_b.update(bsel)
        records.append({
            "group": G, "cardinality": f"{len(iidx)}:{len(bsel)}", "status": status,
            "method": method, "comment": comment, "i_idxs": iidx, "b_idxs": bsel,
            "category": None if status == "MATCHED" else ("Short settlement" if bs < iamt else "Other investigation required")
        })

    for idx, r in i[~i["_idx"].isin(used_i)].iterrows():
        cand = b[~b["_idx"].isin(used_b) & (b["amt_c"] == r["amt_c"])]
        if cand.empty:
            continue
        pr, pfx = r["norm_ref"], ref_prefix(r["payment_ref"], 6)
        strong = cand[cand["norm_narr"].str.contains(pr, regex=False, na=False) |
                      cand["norm_narr"].str.contains(pfx, regex=False, na=False)]
        if len(strong) == 1:
            bi = int(strong.iloc[0]["_idx"])
            G = gid()
            used_i.add(int(idx)); used_b.add(bi)
            records.append({
                "group": G, "cardinality": "1:1", "status": "MATCHED",
                "method": "Exact amount + fuzzy reference",
                "comment": "Reference was case/spacing/truncation-obfuscated; amount confirmed uniquely.",
                "i_idxs": [int(idx)], "b_idxs": [bi], "category": None
            })

    net_bank = b[b["narration"].str.contains("NET SETTLEMENT", case=False, regex=False, na=False) & ~b["_idx"].isin(used_b)]
    parsed = []
    for _, r in net_bank.iterrows():
        mm = re.search(r"M10\d+", r["narration"])
        dd = re.search(r"20\d{2}-\d{2}-\d{2}", r["narration"])
        if mm and dd:
            parsed.append((mm.group(0), dd.group(0), int(r["_idx"])))
    grp = defaultdict(list)
    for merchant, dt, bi in parsed:
        grp[(merchant, dt)].append(bi)
    for (merchant, dt), bidxs in grp.items():
        cand_i = i[(~i["_idx"].isin(used_i)) & (i["merchant_id"] == merchant) & (i["txn_date"].astype(str) == dt)]
        if cand_i.empty:
            continue
        target = sum(int(b.loc[x, "amt_c"]) for x in bidxs)
        sol = subset_sum_indices(cand_i["amt_c"].tolist(), target, 22)
        if sol is None:
            continue
        iidxs = [int(cand_i.iloc[p]["_idx"]) for p in sol]
        internal_total = sum(int(i.loc[x, "amt_c"]) for x in iidxs)
        G = gid()
        used_i.update(iidxs); used_b.update(bidxs)
        status = "MATCHED" if internal_total == target else "PARTIAL_MATCH"
        records.append({
            "group": G, "cardinality": f"{len(iidxs)}:{len(bidxs)}", "status": status,
            "method": "Merchant/date net-settlement subset match",
            "comment": "Internal transactions matched as one settlement group based on merchant/date and exact net total."
                    if status == "MATCHED" else
                    "Related merchant/date transactions found, but settlement is short.",
            "i_idxs": iidxs, "b_idxs": bidxs,
            "category": None if status == "MATCHED" else "Short settlement"
        })

    return {
        "month": month, "i": i, "b": b, "records": records,
        "used_i": used_i, "used_b": used_b, "month_end": month_end,
    }

def build_reconciliation():
    start = time.perf_counter()
    data = load_inputs()
    validation = validation_report()
    runs = {m: run_month(m, data) for m in ["may", "jun"]}

    may = runs["may"]; jun = runs["jun"]
    may_open = [int(x) for x in may["i"].loc[~may["i"]["_idx"].isin(may["used_i"]), "_idx"].tolist()]

    backlog = []
    for seq, idx in enumerate(may_open, 1):
        r = may["i"].loc[idx]
        pr, pfx = r["norm_ref"], ref_prefix(r["payment_ref"], 6)
        cand = jun["b"][
            (~jun["b"]["_idx"].isin(jun["used_b"])) &
            (jun["b"]["amt_c"] == r["amt_c"]) &
            (jun["b"]["narration"].str.contains("PREV CYCLE", case=False, regex=False, na=False)) &
            (jun["b"]["norm_narr"].str.contains(pr, regex=False, na=False) |
             jun["b"]["norm_narr"].str.contains(pfx, regex=False, na=False))
        ]
        G = f"BL-{seq:04d}"
        if len(cand) == 1:
            bi = int(cand.iloc[0]["_idx"])
            jun["used_b"].add(bi)
            backlog.append({"group": G, "idx": idx, "bank_idx": bi, "cleared": True})
        else:
            backlog.append({"group": G, "idx": idx, "bank_idx": None, "cleared": False})

    recon_rows = []
    exception_rows = []

    def append_group(run, rec, reporting_month, settlement_month=None, settlement_type=None, group_override=None, source_month=None):
        i, b = run["i"], run["b"]
        iidx, bidx = rec["i_idxs"], rec["b_idxs"]
        int_refs = [str(i.loc[x, "payment_ref"]) for x in iidx]
        bank_ids = [str(b.loc[x, "line_id"]) for x in bidx]
        bank_refs = [str(b.loc[x, "bank_ref"]) for x in bidx]
        int_amt = sum(float(i.loc[x, "amount"]) for x in iidx)
        bank_amt = sum(float(b.loc[x, "amount"]) for x in bidx)
        recon_rows.append({
            "reporting_month": reporting_month.upper(),
            "source_month": (source_month or run["month"]).upper(),
            "settlement_month": settlement_month or (run["month"].upper() if bidx else ""),
            "settlement_type": settlement_type or ("Same-month settlement" if bidx else "Unsettled"),
            "record_type": "MATCH_GROUP",
            "match_group_id": group_override or rec["group"],
            "internal_transaction_ids": "|".join(str(i.loc[x, "txn_id"]) for x in iidx),
            "internal_payment_refs": "|".join(int_refs),
            "bank_line_ids": "|".join(bank_ids),
            "bank_references": "|".join(bank_refs),
            "cardinality": rec["cardinality"],
            "internal_amount": money(int_amt),
            "bank_amount": money(bank_amt),
            "variance": money(bank_amt - int_amt),
            "reconciliation_status": rec["status"],
            "match_method": rec["method"],
            "comments": rec["comment"]
        })

    for rec in may["records"]:
        append_group(may, rec, "May")
    for rec in jun["records"]:
        append_group(jun, rec, "June")

    for item in backlog:
        r = may["i"].loc[item["idx"]]
        age = age_days(r["txn_date_dt"], may["month_end"])
        cls, _ = lag_classification(r["txn_date_dt"], may["month_end"])
        group = item["group"]
        may_int = float(r["amount"])
        recon_rows.append({
            "reporting_month": "MAY", "source_month": "MAY",
            "settlement_month": "JUNE" if item["cleared"] else "",
            "settlement_type": "May opening backlog" if not item["cleared"] else "Backlog identified in May",
            "record_type": "OPEN_ITEM", "match_group_id": group,
            "internal_transaction_ids": str(r["txn_id"]),
            "internal_payment_refs": str(r["payment_ref"]),
            "bank_line_ids": "", "bank_references": "",
            "cardinality": "1:0",
            "internal_amount": money(may_int), "bank_amount": 0.0, "variance": money(-may_int),
            "reconciliation_status": "OPEN",
            "match_method": "No May settlement found",
            "comments": f"At May month-end: {cls}; ageing {age} days. {'Cleared by June PREV CYCLE credit.' if item['cleared'] else 'Still open at June month-end.'}"
        })
        if item["cleared"]:
            jb = jun["b"].loc[item["bank_idx"]]
            recon_rows.append({
                "reporting_month": "JUNE", "source_month": "MAY", "settlement_month": "JUNE",
                "settlement_type": "May backlog cleared in June",
                "record_type": "BACKLOG_CLEARED", "match_group_id": group,
                "internal_transaction_ids": str(r["txn_id"]),
                "internal_payment_refs": str(r["payment_ref"]),
                "bank_line_ids": str(jb["line_id"]), "bank_references": str(jb["bank_ref"]),
                "cardinality": "1:1", "internal_amount": money(may_int),
                "bank_amount": money(jb["amount"]), "variance": money(jb["amount"] - may_int),
                "reconciliation_status": "MATCHED", "match_method": "PREV CYCLE reference + exact amount",
                "comments": "May opening backlog settled in June and removed from open backlog."
            })
        else:
            recon_rows.append({
                "reporting_month": "JUNE", "source_month": "MAY", "settlement_month": "",
                "settlement_type": "May backlog still open",
                "record_type": "BACKLOG_OPEN", "match_group_id": group,
                "internal_transaction_ids": str(r["txn_id"]),
                "internal_payment_refs": str(r["payment_ref"]),
                "bank_line_ids": "", "bank_references": "",
                "cardinality": "1:0", "internal_amount": money(may_int),
                "bank_amount": 0.0, "variance": money(-may_int),
                "reconciliation_status": "OPEN", "match_method": "No June PREV CYCLE settlement found",
                "comments": "Carried forward from May; remains open at June month-end and requires investigation."
            })

    jun_current_open = [int(x) for x in jun["i"].loc[~jun["i"]["_idx"].isin(jun["used_i"]), "_idx"].tolist()]
    for seq, idx in enumerate(jun_current_open, 1):
        r = jun["i"].loc[idx]
        cls, age = lag_classification(r["txn_date_dt"], jun["month_end"])
        G = f"JUN-OPEN-{seq:04d}"
        recon_rows.append({
            "reporting_month": "JUNE", "source_month": "JUNE", "settlement_month": "",
            "settlement_type": "June transaction not settled by June month-end",
            "record_type": "OPEN_ITEM", "match_group_id": G,
            "internal_transaction_ids": str(r["txn_id"]), "internal_payment_refs": str(r["payment_ref"]),
            "bank_line_ids": "", "bank_references": "", "cardinality": "1:0",
            "internal_amount": money(r["amount"]), "bank_amount": 0.0, "variance": money(-r["amount"]),
            "reconciliation_status": "OPEN", "match_method": "No June settlement found",
            "comments": f"{cls}; ageing {age} days. Rule: <=7 days at month-end = likely lag; >7 days = genuine exception."
        })

    def add_exception(month, category, group, ref, amount, variance, ageing, action, status_note="", source_month=None, related_bank_ref="", related_txn_ref=""):
        exception_rows.append({
            "reporting_month": month.upper(), "source_month": (source_month or month).upper(),
            "exception_category": category, "match_group_id": group,
            "related_transaction_reference": related_txn_ref or ref,
            "related_bank_reference": related_bank_ref,
            "amount": money(amount), "variance": money(variance),
            "ageing_days": int(ageing), "classification": status_note,
            "recommended_action": action
        })

    for run in [may, jun]:
        for rec in run["records"]:
            if rec["category"] == "Internal self-netting pair":
                i = run["i"]; inds = rec["i_idxs"]
                start_date = min(i.loc[x, "txn_date_dt"] for x in inds)
                age = age_days(start_date, run["month_end"])
                add_exception(run["month"], "Internal self-netting pair", rec["group"],
                              "|".join(str(i.loc[x, "payment_ref"]) for x in inds), 0.0, 0.0, age,
                              "Document as internally netted; do not expect a bank credit for the zero-net pair.",
                              "Closed internal netting", source_month=run["month"])

    for run in [may, jun]:
        for rec in run["records"]:
            if rec["status"] != "PARTIAL_MATCH":
                continue
            i, b = run["i"], run["b"]
            int_amt = sum(float(i.loc[x, "amount"]) for x in rec["i_idxs"])
            bank_amt = sum(float(b.loc[x, "amount"]) for x in rec["b_idxs"])
            start_date = min(i.loc[x, "txn_date_dt"] for x in rec["i_idxs"])
            age = age_days(start_date, run["month_end"])
            category = rec["category"] or ("Reference/amount mismatch" if "Reference" in rec["method"] else "Other investigation required")
            if "Reference" in rec["method"] and category == "Short settlement":
                category = "Reference/amount mismatch"
            add_exception(run["month"], category, rec["group"],
                          "|".join(str(i.loc[x, "payment_ref"]) for x in rec["i_idxs"]),
                          int_amt, bank_amt - int_amt, age,
                          "Investigate settlement shortfall / fee treatment and confirm expected payout amount.",
                          "Imperfect match", source_month=run["month"],
                          related_bank_ref="|".join(str(b.loc[x, "bank_ref"]) for x in rec["b_idxs"]))

    for run in [may, jun]:
        i, b = run["i"], run["b"]
        leftovers = b[~b["_idx"].isin(run["used_b"])].copy()
        for _, r in leftovers.iterrows():
            n = normalize(r["narration"])
            pfx_match = re.search(r"PR(?:MAY|JUN)\d{6,8}", n)
            related_internal = None
            if pfx_match:
                token = pfx_match.group(0)
                candidates = i[i["norm_ref"].apply(lambda x: x == token or x.startswith(token) or token.startswith(x[:len(token)]))]
                if not candidates.empty:
                    ea = candidates[candidates["amt_c"] == int(r["amt_c"])]
                    related_internal = ea.iloc[0] if not ea.empty else candidates.iloc[0]
            if related_internal is not None:
                age = age_days(related_internal["txn_date_dt"], run["month_end"])
                G = f"{run['month'].upper()}-DUP-{r['_idx']:04d}"
                recon_rows.append({
                    "reporting_month": ("MAY" if run["month"] == "may" else "JUNE"), "source_month": ("MAY" if run["month"] == "may" else "JUNE"),
                    "settlement_month": ("MAY" if run["month"] == "may" else "JUNE"), "settlement_type": "Duplicate bank credit",
                    "record_type": "EXCEPTION_BANK", "match_group_id": G,
                    "internal_transaction_ids": "", "internal_payment_refs": "",
                    "bank_line_ids": str(r["line_id"]), "bank_references": str(r["bank_ref"]),
                    "cardinality": "0:1", "internal_amount": 0.0,
                    "bank_amount": money(r["amount"]), "variance": money(r["amount"]),
                    "reconciliation_status": "EXCEPTION", "match_method": "Duplicate credit detection",
                    "comments": f"Bank credit duplicates already matched internal reference {related_internal['payment_ref']} / amount; duplicate bank line is not consumed by the match."
                })
                add_exception(run["month"], "Duplicate credit", G,
                              str(related_internal["payment_ref"]), r["amount"], 0.0, age,
                              "Reverse/return the redundant bank credit after confirming the valid matched line.",
                              "Repeated bank credit", source_month=run["month"],
                              related_bank_ref=str(r["bank_ref"]))
            else:
                age = age_days(r["value_date_dt"], run["month_end"])
                G = f"{run['month'].upper()}-ORPH-{r['_idx']:04d}"
                recon_rows.append({
                    "reporting_month": ("MAY" if run["month"] == "may" else "JUNE"), "source_month": ("MAY" if run["month"] == "may" else "JUNE"),
                    "settlement_month": ("MAY" if run["month"] == "may" else "JUNE"), "settlement_type": "Orphan bank credit",
                    "record_type": "EXCEPTION_BANK", "match_group_id": G,
                    "internal_transaction_ids": "", "internal_payment_refs": "",
                    "bank_line_ids": str(r["line_id"]), "bank_references": str(r["bank_ref"]),
                    "cardinality": "0:1", "internal_amount": 0.0,
                    "bank_amount": money(r["amount"]), "variance": money(r["amount"]),
                    "reconciliation_status": "EXCEPTION", "match_method": "Orphan credit detection",
                    "comments": "No supporting internal transaction or usable reference was found."
                })
                add_exception(run["month"], "Orphan credit", G,
                              "", r["amount"], r["amount"], age,
                              "Investigate bank credit against source/payment operations; no internal transaction link was found.",
                              "No supporting internal reference", source_month=run["month"],
                              related_bank_ref=str(r["bank_ref"]))

    for item in backlog:
        r = may["i"].loc[item["idx"]]
        cls, age = lag_classification(r["txn_date_dt"], may["month_end"])
        add_exception("May", "Unsettled transaction", item["group"], str(r["payment_ref"]),
                      r["amount"], -r["amount"], age,
                      "Track to settlement. Reclassify to cleared when the June PREV CYCLE credit is posted."
                      if item["cleared"] else
                      "Escalate to payments/settlement operations; item remained open beyond the normal lag window.",
                      f"{cls}; {'cleared in June' if item['cleared'] else 'still open at June month-end'}",
                      source_month="May", related_txn_ref=str(r["payment_ref"]))

    for seq, idx in enumerate(jun_current_open, 1):
        r = jun["i"].loc[idx]
        cls, age = lag_classification(r["txn_date_dt"], jun["month_end"])
        add_exception("June", "Unsettled transaction", f"JUN-OPEN-{seq:04d}", str(r["payment_ref"]),
                      r["amount"], -r["amount"], age,
                      "Allow settlement if within normal lag window." if cls == "Likely Settlement Lag"
                      else "Escalate to settlement operations; older than 7 days at month-end.",
                      cls, source_month="June", related_txn_ref=str(r["payment_ref"]))

    backlog_rows = []
    for item in backlog:
        r = may["i"].loc[item["idx"]]
        if item["cleared"]:
            jb = jun["b"].loc[item["bank_idx"]]
            age_clear = age_days(r["txn_date_dt"], jb["value_date_dt"])
            backlog_rows.append({
                "item_type": "May opening backlog", "match_group_id": item["group"],
                "internal_reference": str(r["payment_ref"]), "internal_amount": money(r["amount"]),
                "may_opening_status": "OPEN", "june_status": "CLEARED IN JUNE",
                "june_bank_reference": str(jb["bank_ref"]), "june_bank_line_id": str(jb["line_id"]),
                "clearance_date": str(jb["value_date"]), "ageing_at_clearance_days": age_clear,
                "lag_vs_exception": lag_classification(r["txn_date_dt"], may["month_end"])[0],
                "comments": "Cleared by PREV CYCLE credit in June."
            })
        else:
            cls, age = lag_classification(r["txn_date_dt"], may["month_end"])
            backlog_rows.append({
                "item_type": "May opening backlog", "match_group_id": item["group"],
                "internal_reference": str(r["payment_ref"]), "internal_amount": money(r["amount"]),
                "may_opening_status": "OPEN", "june_status": "STILL OPEN AT JUNE MONTH-END",
                "june_bank_reference": "", "june_bank_line_id": "", "clearance_date": "",
                "ageing_at_clearance_days": "", "lag_vs_exception": "Genuine Exception",
                "comments": f"No June PREV CYCLE settlement found; ageing {age} days at May month-end and older than normal lag."
            })

    recon = pd.DataFrame(recon_rows)
    exc = pd.DataFrame(exception_rows)
    if not exc.empty:
        exc["reporting_month"] = exc["reporting_month"].replace({"MAY":"MAY","JUN":"JUNE","JUNE":"JUNE"})
    back = pd.DataFrame(backlog_rows)

    summary_rows = []
    for reporting_month, runslice in [("May", recon[recon["reporting_month"]=="MAY"]), ("June", recon[recon["reporting_month"]=="JUNE"])]:
        for status in ["MATCHED","PARTIAL_MATCH","OPEN","EXCEPTION"]:
            s = runslice[runslice["reconciliation_status"] == status]
            summary_rows.append({
                "reporting_month": reporting_month,
                "status": status,
                "group_or_item_count": int(len(s)),
                "internal_transaction_count": int(sum((1 if not x else len(x.split("|"))) for x in s["internal_transaction_ids"].fillna("").tolist())),
                "internal_value": money(s["internal_amount"].astype(float).sum()),
                "bank_value": money(s["bank_amount"].astype(float).sum()),
                "net_variance": money(s["variance"].astype(float).sum())
            })
        summary_rows.append({
            "reporting_month": reporting_month,
            "status": "BACKLOG_CLEARED",
            "group_or_item_count": int(len(runslice[runslice["settlement_type"]=="May backlog cleared in June"])) if reporting_month=="June" else 0,
            "internal_transaction_count": int(len(back[back["june_status"]=="CLEARED IN JUNE"])) if reporting_month=="June" else 0,
            "internal_value": money(back.loc[back["june_status"]=="CLEARED IN JUNE","internal_amount"].astype(float).sum()) if reporting_month=="June" else 0.0,
            "bank_value": money(back.loc[back["june_status"]=="CLEARED IN JUNE","internal_amount"].astype(float).sum()) if reporting_month=="June" else 0.0,
            "net_variance": 0.0
        })
    summary = pd.DataFrame(summary_rows)

    exc_summary = exc.groupby(["reporting_month","exception_category"], dropna=False).agg(
        count=("exception_category","size"),
        value=("amount","sum"),
        variance=("variance","sum"),
        max_ageing_days=("ageing_days","max")
    ).reset_index()
    exc_summary["value"] = exc_summary["value"].round(2)
    exc_summary["variance"] = exc_summary["variance"].round(2)

    elapsed = time.perf_counter() - start

    out = {
        "reconciliation_output.csv": recon,
        "exception_report.csv": exc,
        "backlog_report.csv": back,
        "input_validation_report.csv": validation,
        "summary_control_report.csv": summary,
        "exception_summary.csv": exc_summary,
    }
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for name, df in out.items():
        df.to_csv(os.path.join(OUTPUT_DIR, name), index=False)

    generate_dashboard(recon, exc, back, summary, exc_summary, elapsed)
    generate_writeup(recon, exc, back, exc_summary, elapsed)

    return recon, exc, back, validation, summary, exc_summary, elapsed


def generate_dashboard(recon, exc, back, summary, exc_summary, elapsed):
    status_order = ["MATCHED","PARTIAL_MATCH","OPEN","EXCEPTION"]
    cards = []
    for month in ["MAY","JUNE"]:
        s = recon[recon["reporting_month"]==month]
        cards.append(f"""
        <section class="month">
          <h2>{month}</h2>
          <div class="grid">
            {''.join(f'<div class="card"><div class="label">{st}</div><div class="value">{len(s[s.reconciliation_status==st])}</div><div class="small">₹{s.loc[s.reconciliation_status==st,"internal_amount"].astype(float).sum():,.2f}</div></div>' for st in status_order)}
          </div>
        </section>""")
    ex_html = exc_summary.to_html(index=False, classes="table", border=0)
    bl_cleared = int((back["june_status"]=="CLEARED IN JUNE").sum())
    bl_open = int((back["june_status"]!="CLEARED IN JUNE").sum())
    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Reconciliation Dashboard</title>
<style>
body{{font-family:Arial,Helvetica,sans-serif;margin:32px;background:#f7f8fa;color:#1f2937}}
h1{{margin-bottom:4px}} .muted{{color:#6b7280}}
.month{{margin:24px 0}} .grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}
.card{{background:white;border:1px solid #e5e7eb;border-radius:10px;padding:16px;box-shadow:0 1px 2px rgba(0,0,0,.04)}}
.label{{font-size:12px;text-transform:uppercase;color:#6b7280;letter-spacing:.05em}}
.value{{font-size:26px;font-weight:700;margin:6px 0}} .small{{font-size:13px;color:#4b5563}}
.kpis{{display:flex;gap:12px;flex-wrap:wrap;margin:20px 0}} .kpi{{background:white;border:1px solid #e5e7eb;border-radius:10px;padding:14px 18px}}
table{{width:100%;border-collapse:collapse;background:white}} th,td{{padding:8px;border-bottom:1px solid #e5e7eb;text-align:left;font-size:13px}} th{{background:#f3f4f6}}
.note{{background:white;border-left:4px solid #374151;padding:12px 14px;border-radius:6px;margin:18px 0}}
</style></head><body>
<h1>Payments Reconciliation Dashboard</h1>
<div class="muted">Generated from reconciliation_output.csv and exception_report.csv</div>
<div class="kpis">
  <div class="kpi"><b>May backlog opened</b><br>{len(back)} items</div>
  <div class="kpi"><b>Cleared in June</b><br>{bl_cleared} items</div>
  <div class="kpi"><b>Still open at June month-end</b><br>{bl_open + len(recon[(recon.reporting_month=="JUNE") & (recon.settlement_type=="June transaction not settled by June month-end")])} items</div>
  <div class="kpi"><b>Exception rows</b><br>{len(exc)} rows</div>
</div>
{''.join(cards)}
<div class="note"><b>Lag rule:</b> items aged 7 days or less at month-end are classified as Likely Settlement Lag; older open items are Genuine Exception. Processing time reported by the script: {elapsed:.2f} seconds.</div>
<h2>Exception categories</h2>{ex_html}
</body></html>"""
    with open(os.path.join(BASE, "reconciliation_dashboard.html"), "w", encoding="utf-8") as f:
        f.write(html)


def generate_writeup(recon, exc, back, exc_summary, elapsed):
    cleared = back[back["june_status"] == "CLEARED IN JUNE"]
    still_open = back[back["june_status"] != "CLEARED IN JUNE"]

    txt = f"""# Reconciliation Summary (May & June 2026)

This run reconciles internal payouts with bank credits and highlights anything that still needs action.

## Quick outcome
- May backlog opened: {len(back)}
- Cleared in June: {len(cleared)}
- Still open at June month-end: {len(still_open)}
- Total exception rows: {len(exc)}

## How matching works
1. Validate files and required fields.
2. Match by batch references and totals.
3. Match by normalized payment references.
4. Use amount + strong reference hint for fuzzy cases.
5. Match NET SETTLEMENT merchant/date groups.
6. Carry May open items into June via PREV CYCLE credits.
7. Mark leftovers as duplicate or orphan credits.

## Exception summary
{exc_summary.to_string(index=False)}

## Rule used for open-item ageing
- 0-7 days at month-end: Likely Settlement Lag
- More than 7 days: Genuine Exception

Processing time: {elapsed:.2f} seconds.
"""
    with open(os.path.join(BASE, "summary_writeup.md"), "w", encoding="utf-8") as f:
        f.write(txt)