from __future__ import annotations

import pandas as pd
import pytest

from cehuan.data.corporate_actions import (
    corporate_action_phase,
    make_share_swap_claim,
    read_corporate_actions,
)
from cehuan.data.security_identity import normalize_security_identity_events, read_security_identity_events


def test_code_change_table_rejects_merger_ratio():
    event = read_security_identity_events()
    event.loc[0, "share_ratio"] = 0.1339
    with pytest.raises(ValueError, match="严格为 1.0"):
        normalize_security_identity_events(event)


def test_absorption_merger_has_explicit_dates_and_never_trades_before_new_shares_list():
    action = read_corporate_actions().iloc[0]
    assert action["old_last_trade_date"] == "2025-08-12"
    assert action["record_date"] == "2025-09-04"
    assert action["old_delisting_date"] == "2025-09-05"
    assert action["new_share_registration_date"] == "2025-09-11"
    assert action["new_shares_tradable_date"] == "2025-09-16"
    for day in ("2025-09-05", "2025-09-15"):
        phase = corporate_action_phase(action, day)
        assert phase["phase"] == "PENDING_NEW_SHARES"
        assert phase["tradable_symbol"] is None
    assert corporate_action_phase(action, "2025-09-16")["phase"] == "ALLOCATION_UNRESOLVED"

    confirmed = action.copy()
    confirmed["allocation_status"] = "CONFIRMED_ACCOUNT_ALLOCATION"
    phase = corporate_action_phase(confirmed, "2025-09-16")
    assert phase == {"phase": "NEW_SECURITY", "tradable_symbol": "SH600150"}


@pytest.mark.parametrize(
    ("old_shares", "lower", "upper", "exact"),
    [(100, 13, 14, "13.3900"), (1000, 133, 134, "133.9000"), (1, 0, 1, "0.1339")],
)
def test_share_swap_claim_keeps_fractional_tail_as_unresolved_right(old_shares, lower, upper, exact):
    action = read_corporate_actions().iloc[0]
    claim = make_share_swap_claim(action, old_shares, 12345.67, "2025-01-15")
    assert claim["expected_new_shares_exact"] == exact
    assert claim["new_shares_lower_bound"] == lower
    assert claim["new_shares_upper_bound"] == upper
    assert claim["final_allocated_new_shares"] is None
    assert claim["claim_status"] == "PENDING_FINAL_ALLOCATION"
    assert claim["cost_basis_cny"] == 12345.67
    assert claim["cash_change_cny"] == 0.0
    assert claim["formal_valuation_status"] == "UNVALUED_PENDING_CORPORATE_ACTION_REVIEW"


def test_unresolved_merger_does_not_accept_an_assumed_final_allocation():
    action = read_corporate_actions().iloc[0]
    with pytest.raises(ValueError, match="不得注入一个假定整数股数"):
        make_share_swap_claim(
            action, 100, 1000.0, "2025-01-15", final_allocated_new_shares=13,
        )
