from app.services.interview_service import _filler_word_rate


def test_no_fillers_gives_zero_rate():
    assert _filler_word_rate("I used a debugger and bisected the change.") == 0.0


def test_counts_common_filler_words():
    rate = _filler_word_rate("Um, so I basically used a debugger and, like, bisected the change.")
    assert rate > 0.0


def test_empty_text_gives_zero_rate():
    assert _filler_word_rate("") == 0.0


def test_rate_is_capped_at_one():
    assert _filler_word_rate("um um um um um") <= 1.0
