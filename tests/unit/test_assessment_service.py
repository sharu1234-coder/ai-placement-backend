from app.services.assessment_service import _question_count


def test_low_weight_non_gap_gets_minimum_questions():
    assert _question_count(0.4, is_gap=False) == 2


def test_mid_weight_gets_three_questions():
    assert _question_count(0.6, is_gap=False) == 3


def test_high_weight_gets_five_questions():
    assert _question_count(0.9, is_gap=False) == 5


def test_gap_bonus_adds_one_question():
    assert _question_count(0.9, is_gap=True) == 6


def test_gap_bonus_is_capped():
    assert _question_count(0.9, is_gap=True) <= 6
