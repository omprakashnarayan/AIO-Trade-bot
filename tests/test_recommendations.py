from portfolio_tracker.recommendations import build_recommendations


def test_concentration_warning_and_long_term_help():
    summary = {"total_value": 1000.0, "total_cost": 800.0, "gain": 200.0, "gain_pct": 25.0}
    holdings = [
        {"symbol": "AAPL", "weight": 0.55, "day_change_pct": 1.2},
        {"symbol": "VOO", "weight": 0.45, "day_change_pct": 0.1},
    ]
    recs = build_recommendations(summary, holdings, {"risk_profile": "balanced"})
    assert recs["long_term"]
    assert any("concentration" in item.lower() or "trim" in item.lower() for item in recs["long_term"])
    assert any("concentration" in item.lower() for item in recs["risks"])


def test_no_holdings_returns_starting_guidance():
    recs = build_recommendations({}, [], {"risk_profile": "low"})
    assert recs["long_term"]
    assert recs["short_term"]
