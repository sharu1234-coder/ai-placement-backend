"""What a student hears must mean what the screen says.

Two halves. The first proves the notation goes away -- URLs, markdown, table rules -- because a
synthesizer reads those out character by character. The second is the one that matters: it proves
the *meaning* survives. Numbers, warnings, uncertainty and refusals are exactly the parts a
student acts on, and a rendering pass that quietly shortened them would make the spoken answer a
different answer from the written one.
"""

from app.voice.speech_text import for_speech


class TestNotationIsNotSpoken:
    def test_a_job_url_becomes_a_short_phrase(self) -> None:
        said = for_speech(
            "I found one opening: https://careers.example.com/jobs/48123?src=talviora"
        )
        assert "https" not in said
        assert "example.com" not in said
        assert "48123" not in said, "a URL path is not a number a student can act on"
        assert "a link on your screen" in said

    def test_a_bare_domain_with_a_path_is_not_read_aloud(self) -> None:
        said = for_speech("Apply at careers.example.com/apply today.")
        assert "example.com" not in said
        assert "today" in said

    def test_a_sentence_ending_full_stop_is_not_mistaken_for_a_domain(self) -> None:
        # The bare-domain pattern requires a path, precisely so this sentence survives intact.
        said = for_speech("You should practise more. Then we review.")
        assert "You should practise more" in said
        assert "Then we review" in said
        assert "a link on your screen" not in said

    def test_a_markdown_link_says_its_label_not_its_target(self) -> None:
        said = for_speech("See [the role description](https://example.com/jd/9) for detail.")
        assert "the role description" in said
        assert "example.com" not in said

    def test_emphasis_markers_go_but_the_words_stay(self) -> None:
        said = for_speech("This is **important** and _worth doing_ and `pytest` runs it.")
        for word in ("important", "worth doing", "pytest"):
            assert word in said
        for marker in ("**", "__", "`"):
            assert marker not in said

    def test_nested_emphasis_is_fully_unwrapped(self) -> None:
        said = for_speech("**bold with _italic_ inside**")
        assert "bold with italic inside" in said
        assert "*" not in said and "_" not in said

    def test_heading_and_bullet_markers_go_but_their_text_stays(self) -> None:
        said = for_speech("## Next steps\n- Practise SQL\n- Rewrite your resume")
        assert "Next steps" in said
        assert "Practise SQL" in said
        assert "Rewrite your resume" in said
        assert "#" not in said
        # A leading hyphen would be read as a dash or silence, neither of which is a bullet.
        assert not said.lstrip().startswith("-")

    def test_a_table_separator_row_is_silent_but_the_cells_are_not(self) -> None:
        said = for_speech("| Role | Salary |\n| --- | --- |\n| Data Analyst | 6 LPA |")
        assert "Data Analyst" in said
        assert "6 LPA" in said
        assert "---" not in said
        assert "|" not in said

    def test_empty_and_blank_text_produce_nothing_rather_than_a_stray_full_stop(self) -> None:
        for blank in ("", "   ", "\n\n"):
            assert for_speech(blank) == ""

    def test_rendering_is_idempotent(self) -> None:
        # The caller cannot always tell whether a responder already rendered its answer.
        once = for_speech("See [docs](https://example.com) and **do** it.")
        assert for_speech(once) == once


class TestMeaningSurvives:
    """The guard against a rendering pass turning into a summarisation pass."""

    def test_numbers_are_never_dropped(self) -> None:
        said = for_speech("The median is 6.5 LPA across 142 postings, from 2024 data.")
        for figure in ("6.5", "142", "2024"):
            assert figure in said, figure + " is a number a student would act on"

    def test_a_refusal_is_not_softened_away(self) -> None:
        text = "I can't guarantee a placement, but we can improve your preparation."
        said = for_speech(text)
        assert "can't guarantee" in said

    def test_recorded_uncertainty_survives(self) -> None:
        said = for_speech("What I could not establish: your current notice period.")
        assert "could not establish" in said
        assert "notice period" in said

    def test_no_sentence_is_removed(self) -> None:
        text = (
            "Your resume is a good start. The summary needs a clearer result. "
            "I could not verify the employer. Try the STAR structure next."
        )
        said = for_speech(text)
        # Sentence count is the cheapest proxy for "nothing was summarised away".
        assert said.count(".") >= 4
        for fragment in ("good start", "clearer result", "could not verify", "STAR"):
            assert fragment in said

    def test_a_long_answer_is_not_truncated(self) -> None:
        text = " ".join("Sentence number %d is here." % n for n in range(1, 31))
        said = for_speech(text)
        assert "number 30" in said, "a rendering pass must not cap length"

    def test_technical_vocabulary_is_left_alone(self) -> None:
        said = for_speech("Practise Python, SQL, and data engineering concepts.")
        for term in ("Python", "SQL", "data engineering"):
            assert term in said


class TestEmojiIsNotSpoken:
    """Pictographs have no spoken form, and a model emits them even when told not to.

    Every spoken string used to come from the grounded renderer, which assembles sentences from
    recorded claims and cannot produce an emoji -- so this case did not exist until the
    conversational responder shipped. Asked for a greeting, the configured model returned
    "Hey there! Glad you asked \U0001f60a" despite the persona explicitly forbidding emoji,
    and it reached the synthesizer unchanged.
    """

    def test_an_emoji_is_removed(self):
        assert "\U0001f60a" not in for_speech("Glad you asked \U0001f60a")

    def test_the_sentence_still_reads_cleanly(self):
        # No double space left where the emoji was.
        assert for_speech("Glad you asked \U0001f60a Let us dig in.") == (
            "Glad you asked Let us dig in."
        )

    def test_devanagari_survives(self):
        # Scoped to pictographs, not "non-ASCII": the product serves students in India, and
        # Hindi is a supported language of the premium voice under evaluation. Stripping
        # non-ASCII would silently delete the script.
        assert "नमस" in for_speech("Say नमस to them")

    def test_accented_latin_survives(self):
        assert "café" in for_speech("Visit the café")

    def test_removal_is_idempotent(self):
        once = for_speech("Hi \U0001f44b")
        assert for_speech(once) == once
