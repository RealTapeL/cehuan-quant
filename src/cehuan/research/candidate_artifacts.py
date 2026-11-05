from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from cehuan.data.canonical import read_bars
from cehuan.signals.strategy import add_momentum, rank_precomputed_day


def classify_candidate_inventory(
    candidate_path: str | Path,
    inventory_path: str | Path | None,
) -> dict[str, Any]:
    """Compare a candidate artifact with a supplied inventory, without claiming runtime provenance."""
    candidate = Path(candidate_path)
    if inventory_path is None:
        return {
            "status": "NOT_PROVIDED",
            "candidate_sha256": _sha256_file(candidate) if candidate.is_file() else None,
            "inventory_path": None,
            "matching_entries": 0,
            "expected_sha256": None,
            "original_run_time_anchor": "UNVERIFIED",
            "original_run_time_anchor_reason": "No original-run manifest binding the candidate artifact was supplied.",
        }
    inventory = Path(inventory_path)
    if not inventory.is_file():
        return {
            "status": "MISSING",
            "candidate_sha256": _sha256_file(candidate) if candidate.is_file() else None,
            "inventory_path": str(inventory.resolve()),
            "matching_entries": 0,
            "expected_sha256": None,
            "original_run_time_anchor": "UNVERIFIED",
            "original_run_time_anchor_reason": "The supplied inventory file is missing.",
        }
    if not candidate.is_file():
        return {
            "status": "FAILED",
            "candidate_sha256": None,
            "inventory_path": str(inventory.resolve()),
            "matching_entries": 0,
            "expected_sha256": None,
            "original_run_time_anchor": "UNVERIFIED",
            "original_run_time_anchor_reason": "The candidate artifact file is missing.",
        }
    frame = pd.read_csv(inventory, dtype=str, keep_default_na=False)
    if not {"relative_path", "sha256"}.issubset(frame.columns):
        return {
            "status": "FAILED",
            "candidate_sha256": None,
            "inventory_path": str(inventory.resolve()),
            "matching_entries": 0,
            "expected_sha256": None,
            "original_run_time_anchor": "UNVERIFIED",
            "original_run_time_anchor_reason": "The supplied inventory lacks relative_path or sha256.",
        }
    target_name = candidate.name
    rows = frame.loc[frame["relative_path"].map(lambda value: Path(value).name == target_name)]
    if rows.empty:
        status, expected = "NOT_LISTED", None
    elif len(rows) != 1:
        status, expected = "AMBIGUOUS", None
    else:
        expected = str(rows.iloc[0]["sha256"]).strip().lower()
        actual = _sha256_file(candidate)
        status = "MATCHED_SUPPLIED_INVENTORY" if expected == actual else "HASH_MISMATCH"
    return {
        "status": status,
        "candidate_sha256": _sha256_file(candidate),
        "inventory_path": str(inventory.resolve()),
        "matching_entries": int(len(rows)),
        "expected_sha256": expected,
        "inventory_semantics": "SUPPLIED_INVENTORY_ONLY_RUNTIME_ORIGIN_NOT_PROVEN",
        "original_run_time_anchor": "UNVERIFIED",
        "original_run_time_anchor_reason": (
            "An inventory supplied for this audit proves only the file-to-inventory comparison. "
            "It does not prove that the inventory or file was sealed at original run time."
        ),
    }


def reconstruct_candidate_pool(
    frozen_bars_path: str | Path,
    frozen_config_path: str | Path,
    expected_candidates: pd.DataFrame,
) -> tuple[dict[str, Any], pd.DataFrame]:
    """Recreate ranked signal rows from frozen bars/config using the production signal adapters."""
    bars_path, config_path = Path(frozen_bars_path), Path(frozen_config_path)
    if not bars_path.is_file() or not config_path.is_file():
        missing = [str(path) for path in (bars_path, config_path) if not path.is_file()]
        return ({"status": "NOT_PROVIDED", "missing_inputs": missing}, pd.DataFrame())
    required = {"signal_date", "symbol", "rank", "momentum"}
    if not required.issubset(expected_candidates.columns):
        raise ValueError("候选输入缺少字段: " + ", ".join(sorted(required - set(expected_candidates.columns))))
    import json

    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not {"strategy", "market"}.issubset(config):
        raise ValueError("冻结配置必须包含 strategy 和 market")
    bars = read_bars(bars_path)
    bars["trade_date"] = pd.to_datetime(bars["trade_date"], errors="raise").dt.strftime("%Y-%m-%d")
    feature_input = bars.drop(columns=[
        column for column in ("available_at_utc", "retrieved_at_utc_parsed") if column in bars.columns
    ])
    features = add_momentum(
        feature_input,
        int(config["strategy"]["lookback_sessions"]),
        normalized=True,
    )
    sessions = sorted(features["trade_date"].astype(str).unique())
    requested_dates = sorted(pd.to_datetime(expected_candidates["signal_date"], errors="raise").dt.strftime("%Y-%m-%d").unique())
    rows: list[pd.DataFrame] = []
    for signal_day in requested_dates:
        if signal_day not in sessions:
            continue
        signal_index = sessions.index(signal_day)
        if signal_index + 1 >= len(sessions):
            continue
        day = features.loc[features["trade_date"].eq(signal_day)]
        ranked = rank_precomputed_day(
            day,
            config["strategy"],
            config["market"],
            require_order_rules=False,
        ).copy()
        ranked["signal_date"] = signal_day
        ranked["execution_date"] = sessions[signal_index + 1]
        ranked["selected_target"] = ranked["rank"].le(int(config["strategy"]["max_positions"]))
        rows.append(ranked[["signal_date", "execution_date", "symbol", "momentum", "rank", "selected_target"]])
    rebuilt = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(
        columns=["signal_date", "execution_date", "symbol", "momentum", "rank", "selected_target"]
    )
    expected = expected_candidates.copy()
    expected["signal_date"] = pd.to_datetime(expected["signal_date"], errors="raise").dt.strftime("%Y-%m-%d")
    expected["execution_date"] = pd.to_datetime(expected["execution_date"], errors="coerce").dt.strftime("%Y-%m-%d") if "execution_date" in expected else ""
    for column in ("rank",):
        expected[column] = pd.to_numeric(expected[column], errors="coerce")
    if "selected_target" in expected:
        expected["selected_target"] = expected["selected_target"].map(_truthy)
    expected["momentum"] = pd.to_numeric(expected.get("momentum"), errors="coerce")
    comparison_columns = ["signal_date", "symbol", "rank"]
    expected_cmp = expected[comparison_columns + (["execution_date"] if "execution_date" in expected else [])].copy()
    rebuilt_cmp = rebuilt[comparison_columns + ["execution_date"]].copy()
    expected_cmp["symbol"] = expected_cmp.symbol.astype(str)
    rebuilt_cmp["symbol"] = rebuilt_cmp.symbol.astype(str)
    expected_cmp = expected_cmp.sort_values(comparison_columns, kind="stable").reset_index(drop=True)
    rebuilt_cmp = rebuilt_cmp.sort_values(comparison_columns, kind="stable").reset_index(drop=True)
    exact_structure = len(expected_cmp) == len(rebuilt_cmp) and expected_cmp[comparison_columns].equals(rebuilt_cmp[comparison_columns])
    if exact_structure and "execution_date" in expected_cmp:
        exact_structure = expected_cmp["execution_date"].fillna("").equals(rebuilt_cmp["execution_date"].fillna(""))
    momentum_match = False
    selection_match = False
    if exact_structure and len(expected):
        left = expected.sort_values(comparison_columns, kind="stable").reset_index(drop=True)
        right = rebuilt.sort_values(comparison_columns, kind="stable").reset_index(drop=True)
        momentum_match = bool(np.allclose(left["momentum"].to_numpy(dtype=float), right["momentum"].to_numpy(dtype=float), rtol=1e-12, atol=1e-12, equal_nan=True))
        if "selected_target" in left:
            selection_match = left["selected_target"].equals(right["selected_target"])
        else:
            selection_match = True
    matched = bool(exact_structure and momentum_match and selection_match)
    return ({
        "status": "MATCHED_FROZEN_INPUT_RECONSTRUCTION" if matched else "MISMATCH",
        "frozen_bars_path": str(bars_path.resolve()),
        "frozen_config_path": str(config_path.resolve()),
        "expected_rows": int(len(expected)),
        "rebuilt_rows": int(len(rebuilt)),
        "structure_match": bool(exact_structure),
        "momentum_match_with_tolerance_1e-12": bool(momentum_match),
        "selected_target_match": bool(selection_match),
        "proves_original_run_time_anchor": False,
        "interpretation": "Post-run reconstruction from the supplied frozen bars/config only; not evidence the artifact existed at original run time.",
    }, rebuilt)


def inspect_original_run_candidate_anchor(
    manifest_path: str | Path | None,
    candidate_path: str | Path,
) -> dict[str, Any]:
    """Check a runtime manifest claim while keeping unsigned manifest authenticity unverified."""
    if manifest_path is None:
        return {"status": "UNVERIFIED", "manifest_status": "NOT_PROVIDED", "reason": "No original-run manifest was supplied."}
    manifest_file, candidate = Path(manifest_path), Path(candidate_path)
    if not manifest_file.is_file():
        return {"status": "UNVERIFIED", "manifest_status": "MISSING", "reason": "The original-run manifest path does not exist."}
    import json

    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "FAILED",
            "manifest_status": "INVALID_MANIFEST",
            "manifest_path": str(manifest_file.resolve()),
            "reason": f"The original-run manifest cannot be read as JSON: {exc}",
        }
    if not isinstance(manifest, dict):
        return {
            "status": "FAILED",
            "manifest_status": "INVALID_MANIFEST",
            "manifest_path": str(manifest_file.resolve()),
            "reason": "The original-run manifest root must be a JSON object.",
        }
    bindings: list[tuple[str, str]] = []
    runtime_artifacts = manifest.get("runtime_artifacts", {})
    if isinstance(runtime_artifacts, dict):
        for name, value in runtime_artifacts.items():
            if Path(str(name)).name == candidate.name and isinstance(value, dict) and value.get("sha256"):
                bindings.append((str(name), str(value["sha256"]).lower()))
    if not bindings:
        return {
            "status": "UNVERIFIED",
            "manifest_status": "AVAILABLE_NO_RUNTIME_CANDIDATE_HASH",
            "manifest_path": str(manifest_file.resolve()),
            "reason": "The supplied experiment manifest contains no runtime_artifacts hash binding for this candidate file.",
        }
    if len(bindings) != 1:
        return {
            "status": "FAILED",
            "manifest_status": "AMBIGUOUS_RUNTIME_BINDING",
            "manifest_path": str(manifest_file.resolve()),
            "binding_count": len(bindings),
            "reason": "More than one original-run manifest entry names the candidate artifact.",
        }
    expected = bindings[0][1]
    actual = _sha256_file(candidate) if candidate.is_file() else None
    valid_expected = len(expected) == 64 and all(char in "0123456789abcdef" for char in expected)
    matches = valid_expected and actual == expected
    return {
        "status": "UNVERIFIED" if matches else "FAILED",
        "manifest_status": (
            "RUNTIME_HASH_CLAIM_MATCHED_BUT_MANIFEST_AUTHENTICITY_UNVERIFIED"
            if matches else "RUNTIME_HASH_MISMATCH_OR_INVALID"
        ),
        "manifest_path": str(manifest_file.resolve()),
        "binding_key": bindings[0][0],
        "expected_sha256": expected,
        "observed_sha256": actual,
        "reason": "A matching runtime hash claim is present, but no trusted parent binding for the manifest itself was supplied." if matches else "Candidate bytes do not match the original-run manifest claim.",
    }


def _truthy(value: object) -> bool | None:
    if pd.isna(value) or str(value).strip() == "":
        return None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
