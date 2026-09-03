from app.services.project_builder.stages import FALLBACK_GUIDANCE, STAGE_ORDER, STAGE_TITLES


def test_stage_order_has_ten_unique_stages():
    assert len(STAGE_ORDER) == 10
    assert len(set(STAGE_ORDER)) == 10


def test_every_stage_has_a_title_and_fallback_guidance():
    assert set(STAGE_TITLES.keys()) == set(STAGE_ORDER)
    assert set(FALLBACK_GUIDANCE.keys()) == set(STAGE_ORDER)
    for key in STAGE_ORDER:
        assert STAGE_TITLES[key]
        assert FALLBACK_GUIDANCE[key]
