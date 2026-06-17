"""Evaluate the extractors against a labeled gold set and print a per-field report.

Gold file (tests/data/gold.jsonl) rows:
    {id, source, company_key, title, description_text, location_raw, structured_salary,
     gold: {level, sector, country, city, remote, salary:{min,max,currency,interval}|null,
            yoe:{min,max}|null}}

Usage:
    .venv/bin/python scripts/eval_extraction.py [path/to/gold.jsonl]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import ergon_tracker.enrich  # noqa: E402,F401  (registers extractors)
from ergon_tracker.extract.base import ExtractInput, get_extractor  # noqa: E402
from ergon_tracker.extract.geo import normalize_geo  # noqa: E402
from ergon_tracker.models import Location, Salary  # noqa: E402

GOLD = ROOT / "tests" / "data" / "gold.jsonl"


def _input(row: dict) -> ExtractInput:
    ss = row.get("structured_salary")
    return ExtractInput(
        title=row["title"],
        description_text=row.get("description_text"),
        location_raw=row.get("location_raw"),
        company_key=row.get("company_key"),
        company_domain=row.get("company_domain"),
        structured_salary=Salary(**ss) if ss else None,
    )


def _prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return p, r, f


def evaluate(rows: list[dict]) -> dict:
    level_ext = get_extractor("level")
    comp_ext = get_extractor("comp")
    yoe_ext = get_extractor("yoe")
    sector_ext = get_extractor("sector")
    assert level_ext and comp_ext and yoe_ext and sector_ext

    # level: per-class counts for macro-F1
    level_labels: set[str] = set()
    level_tp: dict[str, int] = {}
    level_fp: dict[str, int] = {}
    level_fn: dict[str, int] = {}
    level_correct = level_total = 0

    sector_correct = sector_total = 0
    country_correct = country_total = 0
    city_correct = city_total = 0

    comp_tp = comp_fp = comp_fn = 0
    comp_value_ok = comp_value_total = 0
    yoe_tp = yoe_fp = yoe_fn = 0
    yoe_exact = yoe_total_present = 0
    yoe_abs_err = 0.0

    for row in rows:
        g = row["gold"]
        inp = _input(row)

        # --- level (multiclass) ---
        pred_level = level_ext.extract(inp).value
        gold_level = g.get("level", "unknown")
        level_labels.update([pred_level, gold_level])
        level_total += 1
        if pred_level == gold_level:
            level_correct += 1
            level_tp[gold_level] = level_tp.get(gold_level, 0) + 1
        else:
            level_fp[pred_level] = level_fp.get(pred_level, 0) + 1
            level_fn[gold_level] = level_fn.get(gold_level, 0) + 1

        # --- sector ---
        if g.get("sector"):
            sector_total += 1
            if sector_ext.extract(inp) == g["sector"]:
                sector_correct += 1

        # --- geo ---
        loc = normalize_geo(Location(raw=row.get("location_raw")))
        if g.get("country"):
            country_total += 1
            if (loc.country or "").lower() == g["country"].lower():
                country_correct += 1
        if g.get("city"):
            city_total += 1
            if (loc.city or "").lower() == g["city"].lower():
                city_correct += 1

        # --- comp (presence + value tolerance) ---
        pred_sal = comp_ext.extract(inp)
        gold_sal = g.get("salary")
        pred_has = pred_sal is not None and (pred_sal.min_amount or pred_sal.max_amount)
        gold_has = gold_sal is not None
        if pred_has and gold_has:
            comp_tp += 1
            comp_value_total += 1
            pm = pred_sal.min_amount or pred_sal.max_amount or 0
            gm = gold_sal.get("min") or gold_sal.get("max") or 0
            if gm and abs(pm - gm) <= 0.05 * gm:
                comp_value_ok += 1
        elif pred_has and not gold_has:
            comp_fp += 1
        elif gold_has and not pred_has:
            comp_fn += 1

        # --- yoe (presence + exact + MAE on min) ---
        pmin, pmax = yoe_ext.extract(inp)
        gold_yoe = g.get("yoe")
        pred_y = pmin is not None or pmax is not None
        gold_y = gold_yoe is not None
        if pred_y and gold_y:
            yoe_tp += 1
            yoe_total_present += 1
            gmin = gold_yoe.get("min")
            if pmin is not None and gmin is not None:
                yoe_abs_err += abs(pmin - gmin)
                if pmin == gmin and pmax == gold_yoe.get("max"):
                    yoe_exact += 1
        elif pred_y and not gold_y:
            yoe_fp += 1
        elif gold_y and not pred_y:
            yoe_fn += 1

    # macro-F1 for level
    f1s = []
    for lab in level_labels:
        _, _, f = _prf(level_tp.get(lab, 0), level_fp.get(lab, 0), level_fn.get(lab, 0))
        f1s.append(f)
    level_macro_f1 = sum(f1s) / len(f1s) if f1s else 0.0

    comp_p, comp_r, comp_f = _prf(comp_tp, comp_fp, comp_fn)
    yoe_p, yoe_r, yoe_f = _prf(yoe_tp, yoe_fp, yoe_fn)

    return {
        "n": len(rows),
        "level_accuracy": level_correct / level_total if level_total else 0.0,
        "level_macro_f1": level_macro_f1,
        "sector_accuracy": sector_correct / sector_total if sector_total else None,
        "country_accuracy": country_correct / country_total if country_total else None,
        "city_accuracy": city_correct / city_total if city_total else None,
        "comp_precision": comp_p,
        "comp_recall": comp_r,
        "comp_f1": comp_f,
        "comp_value_within_5pct": (comp_value_ok / comp_value_total) if comp_value_total else None,
        "yoe_precision": yoe_p,
        "yoe_recall": yoe_r,
        "yoe_f1": yoe_f,
        "yoe_exact": (yoe_exact / yoe_total_present) if yoe_total_present else None,
        "yoe_mae_min": (yoe_abs_err / yoe_total_present) if yoe_total_present else None,
    }


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else GOLD
    if not path.exists():
        print(f"gold file not found: {path}")
        raise SystemExit(1)
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    report = evaluate(rows)
    print(f"\n=== Extraction baseline ({report['n']} gold postings) ===")
    for k, v in report.items():
        if k == "n":
            continue
        print(f"  {k:24s}: {v:.3f}" if isinstance(v, float) else f"  {k:24s}: {v}")


if __name__ == "__main__":
    main()
