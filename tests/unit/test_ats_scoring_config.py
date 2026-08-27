from app.services.ats_scoring.config import CATEGORY_WEIGHTS, SCORE_BANDS, band_for_score


def test_category_weights_sum_to_one():
    assert abs(sum(CATEGORY_WEIGHTS.values()) - 1.0) < 1e-9


def test_score_bands_cover_every_boundary():
    assert band_for_score(100).key == "excellent"
    assert band_for_score(90).key == "excellent"
    assert band_for_score(89).key == "very_good"
    assert band_for_score(80).key == "very_good"
    assert band_for_score(79).key == "good"
    assert band_for_score(70).key == "good"
    assert band_for_score(69).key == "needs_improvement"
    assert band_for_score(60).key == "needs_improvement"
    assert band_for_score(59).key == "needs_attention"
    assert band_for_score(0).key == "needs_attention"


def test_score_bands_are_ordered_highest_first():
    thresholds = [band.min_score for band in SCORE_BANDS]
    assert thresholds == sorted(thresholds, reverse=True)


def test_band_labels_are_never_harsh():
    # A loose guard against regressions toward discouraging language -- the product
    # explicitly requires supportive copy even at the bottom band.
    harsh_words = ("fail", "bad", "poor", "terrible", "worst")
    for band in SCORE_BANDS:
        lowered = band.label.lower()
        assert not any(word in lowered for word in harsh_words)
