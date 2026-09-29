from __future__ import annotations

from trend_detect import analyze, apply_bh_correction, benjamini_hochberg


def test_taskshift_adoption_change_uses_pettitt_and_binomial_bic():
    counts = [1] * 12 + [8] * 12
    totals = [10] * 24
    result = analyze(counts, window=7, exposures=totals)
    assert result['trend_test_method'] == 'adoption_binomial'
    assert result['pettitt_p'] < 0.05
    assert result['delta_BIC'] > 0
    assert result['change_point_index'] == 11
    assert result['taskshift_emerging'] is True


def test_taskshift_count_fallback_uses_poisson_bic():
    result = analyze([1] * 12 + [9] * 12, window=7)
    assert result['trend_test_method'] == 'count_poisson'
    assert result['pettitt_p'] < 0.05
    assert result['delta_BIC'] > 0
    assert result['taskshift_emerging'] is True


def test_bh_is_applied_across_the_result_family():
    items = [
        {'pettitt_p': 0.01, 'taskshift_emerging': True},
        {'pettitt_p': 0.04, 'taskshift_emerging': True},
        {'pettitt_p': 0.80, 'taskshift_emerging': False},
    ]
    apply_bh_correction(items, q_threshold=0.10)
    assert [item['pettitt_q'] for item in items] == [0.03, 0.06, 0.8]
    assert [item['taskshift_high_confidence'] for item in items] == [True, True, False]

