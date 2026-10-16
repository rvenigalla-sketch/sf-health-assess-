"""Stage 6 - Consultant review gate. Nothing reaches the client report without a human decision.

export: findings/findings.json -> review/findings_review.xlsx
        One row per finding, with Decision (Accept / Reject / Edit) and optional edits.
import: review/findings_review.xlsx -> findings/accepted.json
        Keeps Accept and Edit rows (with edits applied), recomputes scores from accepted findings only.
        Fails if any row is still undecided, unless --allow-pending (pending rows are left out).

Usage:
  python review_gate.py export --run <run>
  python review_gate.py import --run <run> [--allow-pending] [--config client.yml]
  python review_gate.py accept-all --run <run>     # testing only: accept everything as-is
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

from common import Run, info, load_config, read_json, write_json
from score import SEV_ORDER, assessed_areas, score_areas

COLUMNS = [
    ("ID", 8), ("Severity", 10), ("Area", 12), ("Title", 50), ("Component", 34), ("Evidence", 46),
    ("Impact", 50), ("Recommendation", 50), ("Effort", 7), ("Verification", 26), ("Baseline", 12),
    ("Decision", 11), ("New severity", 12), ("Edited title", 40), ("Edited recommendation", 50), ("Reviewer notes", 40),
]
EDITABLE_FROM = 12          # 1-based column index of "Decision"
SEV_FILL = {"Critical": "F8D7DA", "High": "FDE2C8", "Medium": "FFF3CD", "Low": "E2E8F0"}


def export(run: Run) -> Path:
    data = read_json(run.path("findings", "findings.json"))
    wb = Workbook()
    ws = wb.active
    ws.title = "Findings"
    ws.append([c for c, _ in COLUMNS])
    for i, (_, w) in enumerate(COLUMNS, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = w
        ws.cell(1, i).font = Font(bold=True)
        if i >= EDITABLE_FROM:
            ws.cell(1, i).fill = PatternFill("solid", fgColor="DBEAFE")
    for f in data["findings"]:
        ev = "\n".join(f"{e['path'].split('/')[-1]}:{e['line_start']}-{e['line_end']}" for e in f["evidence"])
        ver = f.get("verification", {})
        ws.append([f["id"], f["severity"], f["area"], f["title"], f["component"], ev, f["impact"], f["recommendation"],
                   f["effort"], f"{ver.get('verdict', '-')}: {ver.get('reason', '')}"[:300] if ver else "-",
                   f.get("baseline_status", ""), "", "", "", "", ""])
        r = ws.max_row
        ws.cell(r, 2).fill = PatternFill("solid", fgColor=SEV_FILL[f["severity"]])
        for c in range(1, len(COLUMNS) + 1):
            ws.cell(r, c).alignment = Alignment(wrap_text=True, vertical="top")
    last = ws.max_row
    dv = DataValidation(type="list", formula1='"Accept,Reject,Edit"', allow_blank=True)
    sv = DataValidation(type="list", formula1='"Critical,High,Medium,Low"', allow_blank=True)
    ws.add_data_validation(dv)
    ws.add_data_validation(sv)
    dv.add(f"L2:L{last}")
    sv.add(f"M2:M{last}")
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{ws.cell(1, len(COLUMNS)).column_letter}{last}"

    help_ws = wb.create_sheet("How to review")
    for line in [
        "For every row, set Decision:",
        "  Accept - correct as written.",
        "  Reject - wrong or not relevant (say why in Reviewer notes).",
        "  Edit   - correct, but change severity / title / recommendation in the blue columns.",
        "Check the Evidence column against the code before accepting High and Critical findings.",
        "Save the file, then run: python review_gate.py import --run <run folder>",
    ]:
        help_ws.append([line])
    help_ws.column_dimensions["A"].width = 100
    out = run.path("review", "findings_review.xlsx")
    wb.save(out)
    return out


def do_import(run: Run, cfg: dict, allow_pending: bool) -> int:
    data = read_json(run.path("findings", "findings.json"))
    by_id = {f["id"]: f for f in data["findings"]}
    ws = load_workbook(run.path("review", "findings_review.xlsx")).active
    header = [c.value for c in ws[1]]
    col = {name: header.index(name) for name, _ in COLUMNS}
    accepted, rejected, pending, problems = [], [], [], []
    for row in ws.iter_rows(min_row=2, values_only=True):
        fid = row[col["ID"]]
        if fid not in by_id:
            continue
        f = dict(by_id[fid])
        decision = (row[col["Decision"]] or "").strip().capitalize()
        notes = row[col["Reviewer notes"]] or ""
        if decision == "Reject":
            f["review"] = {"status": "rejected", "notes": notes}
            rejected.append(f)
        elif decision in ("Accept", "Edit"):
            f["review"] = {"status": "accepted" if decision == "Accept" else "edited", "notes": notes}
            if decision == "Edit":
                new_sev = (row[col["New severity"]] or "").strip().capitalize()
                if new_sev:
                    if new_sev not in SEV_ORDER:
                        problems.append(f"{fid}: unknown severity {new_sev!r}")
                        continue
                    f["original_severity"] = f.get("original_severity", f["severity"])
                    f["severity"] = new_sev
                if row[col["Edited title"]]:
                    f["title"] = str(row[col["Edited title"]]).strip()
                if row[col["Edited recommendation"]]:
                    f["recommendation"] = str(row[col["Edited recommendation"]]).strip()
            accepted.append(f)
        else:
            pending.append(fid)
    if problems:
        for p in problems:
            info(f"REVIEW: {p}")
        return 2
    if pending and not allow_pending:
        info(f"REVIEW: {len(pending)} finding(s) still have no decision (e.g. {', '.join(pending[:5])}). "
             f"Finish the review or pass --allow-pending.")
        return 3
    accepted.sort(key=lambda f: (SEV_ORDER[f["severity"]], f["area"], f["component"]))
    write_json(run.path("findings", "accepted.json"), {
        "findings": accepted, "scores": score_areas(accepted, cfg, assessed_areas(run)),
        "review": {"accepted": len(accepted), "rejected": len(rejected), "pending": len(pending)},
        "baseline": data.get("baseline"),
    })
    run.log_step("review-import", "ok", 0, accepted=len(accepted), rejected=len(rejected), pending=len(pending))
    info(f"REVIEW: {len(accepted)} accepted, {len(rejected)} rejected, {len(pending)} pending -> findings/accepted.json")
    return 0


def accept_all(run: Run, cfg: dict) -> int:
    data = read_json(run.path("findings", "findings.json"))
    for f in data["findings"]:
        f["review"] = {"status": "accepted", "notes": "auto-accepted (testing)"}
    write_json(run.path("findings", "accepted.json"), {
        "findings": data["findings"], "scores": score_areas(data["findings"], cfg, assessed_areas(run)),
        "review": {"accepted": len(data["findings"]), "rejected": 0, "pending": 0, "autoAccepted": True},
        "baseline": data.get("baseline"),
    })
    info("REVIEW: all findings auto-accepted (testing only; the report will say so)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["export", "import", "accept-all"])
    ap.add_argument("--run", required=True)
    ap.add_argument("--allow-pending", action="store_true")
    ap.add_argument("--config")
    args = ap.parse_args()
    run = Run(Path(args.run))
    cfg = load_config(args.config)
    if args.action == "export":
        info(f"REVIEW: wrote {export(run)}")
        return 0
    if args.action == "accept-all":
        return accept_all(run, cfg)
    return do_import(run, cfg, args.allow_pending)


if __name__ == "__main__":
    sys.exit(main())
