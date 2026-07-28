#!/usr/bin/env python3
"""Vendor risk scorer: intake × crosswalk → residual tier + audit artifacts.

Two-axis model (issue #2):
  data_handling → inherent risk
  weighted checklist → assurance band
  config matrix → residual Low/Medium/High

Everything except evidence metadata.generated_at is a pure function of
(intake × crosswalk). assessed_date always comes from the intake record.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Drive slot (rung 3) — Luigi wrote score_checklist; do not rewrite the body.
# ---------------------------------------------------------------------------


def score_checklist(items, responses):
    """Score applicable checklist items against a vendor's intake responses.

    Args:
        items: list of dicts — already filtered by applies_to. Each has
               'id', 'weight' (int), 'intake_field' (str),
               'score_map' (dict of intake value -> credit float 0.0-1.0),
               'controls' (list of str).
        responses: dict — intake_field name -> the vendor's answer (str).

    Returns:
        (earned_points, applicable_points, results)
          earned_points: float — sum of weight * credit
          applicable_points: int — sum of weight (the runtime denominator)
          results: list of dicts, one per item, each with
                   id, credit, weight, earned, controls

    Raises:
        ValueError: if an item's intake_field is missing from responses, or
                    the answer is not a key in that item's score_map.
                    Message must name the item id and the field.
    """

    earned_total = float()
    applicable_total = int()
    results = []

    for item in items:
        field = item["intake_field"]
        if field not in responses:
            raise ValueError(f"missing intake field {field!r} for item {item['id']}")
        answer = responses[field]

        if answer not in item["score_map"]:
            raise ValueError(
                f"invalid value {answer!r} for intake field {field!r} "
                f"on item {item['id']}"
            )

        credit = item["score_map"][answer]
        weight = item["weight"]
        earned = credit * weight
        earned_total += earned
        applicable_total += weight
        results.append({
            "id": item["id"],
            "credit": credit,
            "weight": weight,
            "earned": earned,
            "controls": item["controls"],
        })

    return earned_total, applicable_total, results


# ---------------------------------------------------------------------------
# Load / validate / filter
# ---------------------------------------------------------------------------


def load_yaml(path: Path) -> dict[str, Any]:
    """TL;DR: Read a YAML file into a Python dict."""
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping at the top level")
    return data


def validate_crosswalk(crosswalk: dict[str, Any]) -> None:
    """TL;DR: Fail loud if weights, score_maps, or scoring policy are invalid."""
    items = crosswalk.get("checklist")
    if not isinstance(items, list) or not items:
        raise ValueError("crosswalk: 'checklist' must be a non-empty list")

    weight_sum = 0
    seen_fields: dict[str, str] = {}
    for item in items:
        item_id = item.get("id", "<unknown>")
        if "intake_field" not in item or "score_map" not in item:
            raise ValueError(
                f"crosswalk item {item_id}: missing intake_field or score_map"
            )
        if "weight" not in item:
            raise ValueError(f"crosswalk item {item_id}: missing weight")

        field = item["intake_field"]
        if field in seen_fields:
            raise ValueError(
                f"crosswalk: duplicate intake_field {field!r} on "
                f"{item_id} and {seen_fields[field]}"
            )
        seen_fields[field] = item_id

        weight = item["weight"]
        # bool subclasses int — reject True/False credits/weights explicitly (B5/B6).
        if isinstance(weight, bool) or not isinstance(weight, int) or weight <= 0:
            raise ValueError(
                f"crosswalk item {item_id}: weight must be a positive integer "
                f"(got {weight!r})"
            )
        weight_sum += weight

        score_map = item["score_map"]
        if not isinstance(score_map, dict) or not score_map:
            raise ValueError(f"crosswalk item {item_id}: score_map must be a non-empty dict")
        for key, credit in score_map.items():
            if isinstance(credit, bool) or not isinstance(credit, (int, float)):
                raise ValueError(
                    f"crosswalk item {item_id}: score_map[{key!r}]={credit!r} "
                    "must be a number in 0.0–1.0 (bool is not allowed)"
                )
            if credit < 0.0 or credit > 1.0:
                raise ValueError(
                    f"crosswalk item {item_id}: score_map[{key!r}]={credit!r} "
                    "must be a number in 0.0–1.0"
                )

    if weight_sum != 100:
        raise ValueError(f"crosswalk: weights sum to {weight_sum}, expected 100")

    scoring = crosswalk.get("scoring")
    if not isinstance(scoring, dict):
        raise ValueError("crosswalk: missing 'scoring' policy block")
    for required in ("inherent_risk", "assurance_bands", "residual_tier_matrix"):
        if required not in scoring:
            raise ValueError(f"crosswalk scoring: missing '{required}'")

    # S2: _CERT_LABELS must stay aligned with VDD-02 score_map keys so
    # evidence.json cannot silently report "no certs" for a valid new tier.
    vdd02 = _checklist_item(items, "VDD-02")
    label_keys = set(_CERT_LABELS)
    score_keys = set(vdd02["score_map"])
    if label_keys != score_keys:
        raise ValueError(
            f"_CERT_LABELS keys {sorted(label_keys)} != "
            f"VDD-02 score_map keys {sorted(score_keys)}"
        )


def filter_applicable(items: list[dict[str, Any]], is_cloud_saas: bool) -> list[dict[str, Any]]:
    """TL;DR: Drop cloud-only checklist rows when the vendor is not cloud/SaaS.

    Denominator is whatever weights survive this filter — never hardcode 84.
    """
    applicable: list[dict[str, Any]] = []
    for item in items:
        applies_to = item.get("applies_to")
        if applies_to == "cloud_saas_only" and not is_cloud_saas:
            continue
        applicable.append(item)
    return applicable


def _checklist_item(items: list[dict[str, Any]], item_id: str) -> dict[str, Any]:
    """Look up a checklist row by id; fail loud if missing."""
    for item in items:
        if item.get("id") == item_id:
            return item
    raise ValueError(f"crosswalk: {item_id} not found")


def _intake_field_for(items: list[dict[str, Any]], item_id: str) -> str:
    """Resolve questionnaire field name from the crosswalk (not a Python constant)."""
    return _checklist_item(items, item_id)["intake_field"]


# ---------------------------------------------------------------------------
# Two-axis risk model
# ---------------------------------------------------------------------------

_TIER_ORDER = ("Low", "Medium", "High")


def _escalate_one_tier(tier: str) -> str:
    """Move Low→Medium or Medium→High; High stays High."""
    idx = _TIER_ORDER.index(tier)
    return _TIER_ORDER[min(idx + 1, len(_TIER_ORDER) - 1)]


def resolve_inherent_risk(
    data_handling: dict[str, Any], scoring: dict[str, Any]
) -> str:
    """TL;DR: Map data_classification to inherent risk; optionally escalate once.

    Data handling is inherent risk, not a weighted control.
    production_access must already be a bool (validated in assess).
    """
    inherent_cfg = scoring["inherent_risk"]
    classification = data_handling.get("data_classification")
    lookup = inherent_cfg["data_classification"]
    if classification not in lookup:
        raise ValueError(
            f"unknown data_classification {classification!r}; "
            f"expected one of {sorted(lookup)}"
        )
    tier = lookup[classification]

    escalate_when = inherent_cfg.get("escalate_one_tier_if", {})
    if escalate_when.get("production_access") is True:
        if data_handling["production_access"] is True:
            tier = _escalate_one_tier(tier)
    return tier


def resolve_assurance_band(assurance_pct: Decimal, bands: dict[str, int]) -> str:
    """TL;DR: Band the UNROUNDED percent against config thresholds.

    B1: round-then-band wrongly promotes 84.5 → Strong. Config says Adequate is
    65–84 inclusive of true values below 85 — compare Decimal, display int separately.
    """
    if assurance_pct >= _decimal(bands["Strong"]):
        return "Strong"
    if assurance_pct >= _decimal(bands["Adequate"]):
        return "Adequate"
    return "Weak"


def resolve_residual_tier(
    inherent: str, band: str, matrix: dict[str, dict[str, str]]
) -> str:
    """TL;DR: Look up residual Low/Medium/High from the config matrix."""
    try:
        return matrix[inherent][band]
    except KeyError as exc:
        raise ValueError(
            f"residual_tier_matrix has no entry for inherent={inherent!r}, band={band!r}"
        ) from exc


def _decimal(value: Any) -> Decimal:
    """Stable Decimal from int/float/str — avoids binary float residue."""
    return Decimal(str(value))


def refine_checklist_math(
    results: list[dict[str, Any]], applicable_points: int
) -> tuple[Decimal, int, list[dict[str, Any]]]:
    """TL;DR: Recompute earned with Decimal so memo columns defend the headline.

    score_checklist's float accumulator is kept for the drive-slot contract; this
    function is what evidence/memo publish (avoids 69.30000000000001 residue).
    """
    earned = Decimal("0")
    refined: list[dict[str, Any]] = []
    for row in results:
        credit = _decimal(row["credit"])
        weight = _decimal(row["weight"])
        item_earned = credit * weight
        earned += item_earned
        refined.append({
            "id": row["id"],
            "credit": float(credit),
            "weight": int(weight),
            "earned": float(item_earned),
            "controls": row["controls"],
        })
    return earned, int(applicable_points), refined


def half_up_percent(earned: Decimal, applicable_points: int) -> int:
    """Display/evidence int: half-up percent (spreadsheet-style), not banker's round()."""
    if applicable_points == 0:
        raise ValueError("applicable_points is 0 — cannot compute assurance score")
    pct = (earned / Decimal(applicable_points)) * Decimal(100)
    return int(pct.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def raw_assurance_percent(earned: Decimal, applicable_points: int) -> Decimal:
    """Unrounded percent used for banding (B1)."""
    if applicable_points == 0:
        raise ValueError("applicable_points is 0 — cannot compute assurance score")
    return (earned / Decimal(applicable_points)) * Decimal(100)


def assurance_percent_for_evidence(raw_pct: Decimal) -> float:
    """Raw percent published beside assurance_band (B7 option a).

    Banding uses the unrounded Decimal; this is the same value, rounded to
    two decimals only for JSON float hygiene — not half-up to an integer.
    """
    return float(raw_pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def format_assurance_percent_label(raw_pct: Decimal) -> str:
    """Memo parenthetical — must match the number banding used (B7)."""
    pct = raw_pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    text = format(pct, "f").rstrip("0").rstrip(".")
    return text


# ---------------------------------------------------------------------------
# Evidence / memo rendering
# ---------------------------------------------------------------------------

_CERT_LABELS = {
    "soc2_type_ii_current": ["SOC 2 Type II"],
    "iso27001_only": ["ISO 27001"],
    "soc2_type_ii_stale": ["SOC 2 Type II (stale / no bridge letter)"],
    "none": [],
}


def certifications_on_file(
    responses: dict[str, Any], checklist: list[dict[str, Any]]
) -> list[str]:
    """Derive cert labels via VDD-02's intake_field from the crosswalk."""
    field = _intake_field_for(checklist, "VDD-02")
    key = responses.get(field)
    return list(_CERT_LABELS.get(key, []))


def build_evidence(
    *,
    intake: dict[str, Any],
    checklist: list[dict[str, Any]],
    applicable_items: list[dict[str, Any]],
    inherent_risk: str,
    assurance_score: int,
    assurance_percent: float,
    assurance_band: str,
    risk_tier: str,
    earned_points: float,
    applicable_points: int,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """TL;DR: Build the audit JSON record (wall-clock only in metadata)."""
    responses = intake["responses"]
    rating_field = _intake_field_for(checklist, "VDD-04")
    applicable_ids = {item["id"] for item in applicable_items}
    excluded_items = [
        item["id"] for item in checklist if item["id"] not in applicable_ids
    ]
    flagged_gaps = [
        {
            "id": row["id"],
            "credit": row["credit"],
            "weight": row["weight"],
            "controls": row["controls"],
        }
        for row in results
        if row["credit"] < 1.0
    ]
    controls_referenced: list[str] = []
    seen: set[str] = set()
    for row in results:
        for control in row["controls"]:
            if control not in seen:
                seen.add(control)
                controls_referenced.append(control)

    return {
        "vendor": intake["vendor"],
        "assessed_date": intake["assessed_date"],
        "is_cloud_saas": intake["is_cloud_saas"],
        "excluded_items": excluded_items,
        "inherent_risk": inherent_risk,
        "assurance_score": assurance_score,
        "assurance_percent": assurance_percent,
        "assurance_band": assurance_band,
        "risk_tier": risk_tier,
        "earned_points": earned_points,
        "applicable_points": applicable_points,
        "certifications_on_file": certifications_on_file(responses, checklist),
        "external_rating_tier": responses.get(rating_field),
        "controls_referenced": controls_referenced,
        "flagged_gaps": flagged_gaps,
        "item_results": results,
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "tool": "score_vendor.py",
            "model": "inherent_x_assurance",
        },
    }


def render_memo(
    *,
    intake: dict[str, Any],
    inherent_risk: str,
    assurance_percent_label: str,
    assurance_band: str,
    risk_tier: str,
    earned_points: float,
    applicable_points: int,
    results: list[dict[str, Any]],
) -> str:
    """TL;DR: Human-readable due-diligence memo with tier rationale + gaps."""
    # B3: headline must be reconcilable from the Earned column alone.
    # B7: parenthetical percent must match the number banding used, not half-up int.
    earned_txt = format(earned_points, ".10g")
    lines: list[str] = [
        f"# Vendor due-diligence memo — {intake['vendor']}",
        "",
        f"- **Assessed date:** {intake['assessed_date']}",
        f"- **Residual risk tier:** {risk_tier}",
        f"- **Inherent risk:** {inherent_risk} "
        f"(data_classification={intake['data_handling']['data_classification']}, "
        f"production_access={intake['data_handling']['production_access']})",
        f"- **Assurance:** {assurance_band} — {earned_txt} of {applicable_points} "
        f"applicable points ({assurance_percent_label}%)",
        "",
        "## Tier rationale",
        "",
        f"Inherent risk is driven by the data-handling profile ({inherent_risk}), "
        "not by checklist weight. Assurance reflects graded credit across "
        f"applicable due-diligence items ({assurance_band}). The residual tier "
        f"**{risk_tier}** is the matrix intersection of those two axes — "
        "controls reduce inherent risk; they do not retire a High inherent profile.",
        "",
        "## Checklist results",
        "",
        "| Item | Credit | Weight | Earned | Controls |",
        "|---|---:|---:|---:|---|",
    ]
    for row in results:
        controls = ", ".join(row["controls"])
        lines.append(
            f"| {row['id']} | {row['credit']:.2f} | {row['weight']} | "
            f"{row['earned']:.2f} | {controls} |"
        )

    gaps = [row for row in results if row["credit"] < 1.0]
    lines.extend(["", "## Gaps / Follow-ups", ""])
    if not gaps:
        lines.append("No below-full-credit items.")
    else:
        for row in gaps:
            controls = ", ".join(row["controls"])
            lines.append(
                f"- **{row['id']}** — credit {row['credit']:.2f} / weight {row['weight']} "
                f"({controls})"
            )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _validate_intake_types(intake: dict[str, Any]) -> None:
    """Presence is not enough — reject wrong YAML types (fail loud)."""
    for required in ("vendor", "assessed_date", "is_cloud_saas", "data_handling", "responses"):
        if required not in intake:
            raise ValueError(f"intake: missing required key {required!r}")

    # Bare `responses:` / `data_handling:` in YAML parse as None, not {}.
    for mapping_key in ("data_handling", "responses"):
        value = intake[mapping_key]
        if not isinstance(value, dict):
            raise ValueError(
                f"intake: {mapping_key!r} must be a mapping "
                f"(got {type(value).__name__}; a bare '{mapping_key}:' "
                "in YAML parses as null)"
            )

    vendor = intake["vendor"]
    if not isinstance(vendor, str) or not vendor.strip():
        raise ValueError(
            f"intake: 'vendor' must be a non-empty string "
            f"(got {type(vendor).__name__})"
        )
    if "\n" in vendor or "\r" in vendor:
        raise ValueError(
            "intake: 'vendor' must not contain newline characters "
            "(memo headings are derived from this field)"
        )

    assessed_date = intake["assessed_date"]
    if not isinstance(assessed_date, str) or not assessed_date.strip():
        raise ValueError(
            f"intake: 'assessed_date' must be a non-empty string "
            f"(got {type(assessed_date).__name__}; quote dates in YAML "
            "so they are not parsed as datetime.date)"
        )

    is_cloud_saas = intake["is_cloud_saas"]
    if not isinstance(is_cloud_saas, bool):
        raise ValueError(
            f"intake: 'is_cloud_saas' must be a bool "
            f"(got {type(is_cloud_saas).__name__}; do not quote true/false)"
        )

    production_access = intake["data_handling"].get("production_access")
    if not isinstance(production_access, bool):
        raise ValueError(
            f"intake: data_handling.production_access must be a bool "
            f"(got {type(production_access).__name__}; do not quote true/false)"
        )


def _validate_response_value_types(
    items: list[dict[str, Any]], responses: dict[str, Any]
) -> None:
    """Reject list/mapping answers before score_checklist (avoids TypeError/exit 1)."""
    for item in items:
        field = item["intake_field"]
        if field not in responses:
            continue  # score_checklist raises the missing-field ValueError
        answer = responses[field]
        if not isinstance(answer, str):
            raise ValueError(
                f"invalid value type {type(answer).__name__} for intake field "
                f"{field!r} on item {item['id']} (expected a string)"
            )


def _validate_responses_against_scope(
    checklist: list[dict[str, Any]],
    applicable_items: list[dict[str, Any]],
    responses: dict[str, Any],
    is_cloud_saas: bool,
) -> None:
    """B2: reject unknown keys and answers for applies_to-excluded items."""
    known: dict[str, dict[str, Any]] = {
        item["intake_field"]: item for item in checklist
    }
    applicable_fields = {item["intake_field"] for item in applicable_items}

    unknown = sorted(key for key in responses if key not in known)
    if unknown:
        raise ValueError(
            f"intake: unknown response field(s) {unknown} "
            "(not defined as any checklist intake_field)"
        )

    contradicting = [
        f"{item['id']}/{field}"
        for field, item in known.items()
        if field not in applicable_fields and field in responses
    ]
    if contradicting:
        raise ValueError(
            f"intake: responses include field(s) for items excluded by applies_to "
            f"({', '.join(contradicting)}) but is_cloud_saas={is_cloud_saas}"
        )


def _write_artifacts_atomic(out_dir: Path, memo: str, evidence_text: str) -> tuple[Path, Path]:
    """B4: both artifacts land or neither does; leave prior pair intact on failure.

    Pre-serializing JSON only prevents dumps() failures from orphaning a memo —
    it does not protect against a mid-pair write failure. Temps + replace + rollback.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    memo_path = out_dir / "memo.md"
    evidence_path = out_dir / "evidence.json"
    tmp_memo = out_dir / ".memo.md.tmp"
    tmp_evidence = out_dir / ".evidence.json.tmp"

    prior_memo = memo_path.read_text(encoding="utf-8") if memo_path.exists() else None
    prior_evidence = (
        evidence_path.read_text(encoding="utf-8") if evidence_path.exists() else None
    )

    try:
        tmp_memo.write_text(memo, encoding="utf-8")
        tmp_evidence.write_text(evidence_text, encoding="utf-8")
        tmp_memo.replace(memo_path)
        try:
            tmp_evidence.replace(evidence_path)
        except OSError:
            if prior_memo is not None:
                memo_path.write_text(prior_memo, encoding="utf-8")
            elif memo_path.exists():
                memo_path.unlink()
            raise
    except OSError:
        for tmp in (tmp_memo, tmp_evidence):
            if tmp.exists():
                tmp.unlink()
        raise
    finally:
        for tmp in (tmp_memo, tmp_evidence):
            if tmp.exists():
                tmp.unlink()

    return memo_path, evidence_path


def assess(crosswalk: dict[str, Any], intake: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Run the full inherent × assurance assessment. Raises ValueError on bad input."""
    _validate_intake_types(intake)

    validate_crosswalk(crosswalk)
    scoring = crosswalk["scoring"]
    checklist = crosswalk["checklist"]
    items = filter_applicable(checklist, intake["is_cloud_saas"])
    if not items:
        raise ValueError("no applicable checklist items after applies_to filtering")

    _validate_responses_against_scope(
        checklist, items, intake["responses"], intake["is_cloud_saas"]
    )
    _validate_response_value_types(items, intake["responses"])

    # Drive-slot float total is intentionally unused for publication (S10):
    # refine_checklist_math recomputes with Decimal for audit-defensible numbers.
    _, applicable, results = score_checklist(items, intake["responses"])
    earned, applicable, results = refine_checklist_math(results, applicable)

    raw_pct = raw_assurance_percent(earned, applicable)
    assurance_score = half_up_percent(earned, applicable)  # legacy int field; unchanged
    assurance_percent = assurance_percent_for_evidence(raw_pct)
    assurance_percent_label = format_assurance_percent_label(raw_pct)
    band = resolve_assurance_band(raw_pct, scoring["assurance_bands"])  # B1: unrounded
    inherent = resolve_inherent_risk(intake["data_handling"], scoring)
    tier = resolve_residual_tier(inherent, band, scoring["residual_tier_matrix"])

    evidence = build_evidence(
        intake=intake,
        checklist=checklist,
        applicable_items=items,
        inherent_risk=inherent,
        assurance_score=assurance_score,
        assurance_percent=assurance_percent,
        assurance_band=band,
        risk_tier=tier,
        earned_points=float(earned),
        applicable_points=applicable,
        results=results,
    )
    memo = render_memo(
        intake=intake,
        inherent_risk=inherent,
        assurance_percent_label=assurance_percent_label,
        assurance_band=band,
        risk_tier=tier,
        earned_points=float(earned),
        applicable_points=applicable,
        results=results,
    )
    return evidence, memo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score a vendor intake record against the due-diligence crosswalk."
    )
    parser.add_argument(
        "--intake",
        required=True,
        type=Path,
        help="Path to vendor intake YAML",
    )
    parser.add_argument(
        "--crosswalk",
        type=Path,
        default=Path("crosswalk.yaml"),
        help="Path to crosswalk.yaml (default: ./crosswalk.yaml)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("."),
        help="Directory for memo.md and evidence.json (default: .)",
    )
    args = parser.parse_args(argv)

    try:
        crosswalk = load_yaml(args.crosswalk)
        intake = load_yaml(args.intake)
        evidence, memo = assess(crosswalk, intake)
        evidence_text = json.dumps(evidence, indent=2, sort_keys=False) + "\n"
        memo_path, evidence_path = _write_artifacts_atomic(
            args.out_dir, memo, evidence_text
        )
    except (
        OSError,
        ValueError,
        TypeError,
        InvalidOperation,
        yaml.YAMLError,
        KeyError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"{evidence['vendor']}: residual={evidence['risk_tier']} "
        f"inherent={evidence['inherent_risk']} "
        f"assurance={evidence['assurance_band']} "
        f"({evidence['assurance_score']}/100, "
        f"denom={evidence['applicable_points']})"
    )
    print(f"wrote {memo_path}")
    print(f"wrote {evidence_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
