# Tests for source/boundary_classifier.py: the block extraction, structural
# feature derivation, and the conservative leading/trailing trim rule. Model
# inference is faked (via a FakeModel with a scripted predict_proba) so these
# tests never touch a real model.joblib artifact and can pin exact
# probabilities to exercise the trim rule's edge cases.

import pytest

from source import boundary_classifier as bc


class FakeModel:
    """Stands in for the fitted sklearn Pipeline. Returns a scripted
    boilerplate probability for the i-th row passed to predict_proba, cycling
    if more rows are requested than probabilities were scripted."""

    def __init__(self, boilerplate_probs):
        self.boilerplate_probs = boilerplate_probs

    def predict_proba(self, rows):
        return [
            [1 - self.boilerplate_probs[i % len(self.boilerplate_probs)],
             self.boilerplate_probs[i % len(self.boilerplate_probs)]]
            for i in range(len(rows))
        ]


def make_block(text="body text", boilerplate=False, body=False, link_density=0.0):
    ancestor_tags = frozenset(
        (["nav"] if boilerplate else []) + (["main"] if body else [])
    )
    nearest_landmark = "nav" if boilerplate else ("main" if body else None)
    return bc.Block(
        text=text,
        ancestor_tags=ancestor_tags,
        nearest_landmark=nearest_landmark,
        link_density=link_density,
        char_len=len(text),
        position_pct=0.0,
    )


@pytest.fixture(autouse=True)
def _reset_model_cache():
    bc._model = None
    bc._model_path_loaded = None
    yield
    bc._model = None
    bc._model_path_loaded = None


class TestExtractBlocks:
    def test_splits_paragraphs_in_document_order(self):
        html = b"<html><body><p>First paragraph.</p><p>Second paragraph.</p></body></html>"
        blocks = bc.extract_blocks(html)
        assert [b.text for b in blocks] == ["First paragraph.", "Second paragraph."]

    def test_tags_nav_header_footer_as_boilerplate_ancestors(self):
        html = (
            b"<html><body>"
            b"<nav><p>Home About Contact</p></nav>"
            b"<main><p>This is the actual policy text.</p></main>"
            b"<footer><p>All rights reserved.</p></footer>"
            b"</body></html>"
        )
        blocks = bc.extract_blocks(html)
        by_text = {b.text: b for b in blocks}
        assert "nav" in by_text["Home About Contact"].ancestor_tags
        assert "main" in by_text["This is the actual policy text."].ancestor_tags
        assert "footer" in by_text["All rights reserved."].ancestor_tags

    def test_nearest_landmark_prefers_inner_body_over_outer_boilerplate_wrapper(self):
        # Real-world case that caused an over-trim: a site wraps its entire
        # <article> of genuine policy text inside a page-level <nav> used
        # only for layout. "Any ancestor is boilerplate" would mislabel the
        # whole article; the nearest (innermost) landmark should win instead.
        html = (
            b"<html><body>"
            b"<nav>"
            b"<p>Real nav link text</p>"
            b"<article><p>Actual policy clause text here.</p></article>"
            b"</nav>"
            b"</body></html>"
        )
        blocks = bc.extract_blocks(html)
        by_text = {b.text: b for b in blocks}
        assert by_text["Real nav link text"].nearest_landmark == "nav"
        assert by_text["Actual policy clause text here."].nearest_landmark == "article"

    def test_link_density_reflects_fraction_of_text_inside_anchors(self):
        html = (
            b"<html><body>"
            b"<p><a href='/'>Home</a><a href='/about'>About</a></p>"
            b"<p>This paragraph has no links at all in it whatsoever.</p>"
            b"</body></html>"
        )
        blocks = bc.extract_blocks(html)
        assert blocks[0].link_density == pytest.approx(1.0)
        assert blocks[1].link_density == pytest.approx(0.0)

    def test_ignores_style_script_and_img_content(self):
        html = (
            b"<html><body>"
            b"<style>.a { color: red; }</style>"
            b"<script>doSomething();</script>"
            b"<p>Real content.</p>"
            b"</body></html>"
        )
        blocks = bc.extract_blocks(html)
        assert [b.text for b in blocks] == ["Real content."]

    def test_empty_html_returns_no_blocks(self):
        assert bc.extract_blocks(b"<html><body></body></html>") == []

    def test_ignores_html_comments_at_top_level_and_inside_blocks(self):
        # Common in the wild: GTM-style install snippets wrap their
        # <noscript> pixel in literal HTML comments like
        # "<!-- Google Tag Manager (noscript) -->". Comment is a subclass of
        # NavigableString in bs4, so a naive isinstance(x, NavigableString)
        # check would treat it as real leading page content and block the
        # conservative trim rule from ever starting.
        html = (
            b"<html><body>"
            b"<!-- Google Tag Manager (noscript) -->"
            b"<p>Real content<!-- inline comment --> more text.</p>"
            b"<!-- End Google Tag Manager (noscript) -->"
            b"</body></html>"
        )
        blocks = bc.extract_blocks(html)
        assert [b.text for b in blocks] == ["Real content more text."]
        assert "Google Tag Manager" not in blocks[0].text
        assert "inline comment" not in blocks[0].text


class TestNeighborLinkDensity:
    def test_averages_over_nearby_blocks_in_document_order(self):
        # A lone unlinked block dropped in the middle of a run of link-heavy
        # nav items should still read as "in a link-heavy neighborhood",
        # which a block's own link_density alone can't capture.
        html = (
            b"<html><body>"
            b"<p><a href='/a'>A</a></p>"
            b"<p><a href='/b'>B</a></p>"
            b"<p>no link here</p>"
            b"<p><a href='/c'>C</a></p>"
            b"<p><a href='/d'>D</a></p>"
            b"</body></html>"
        )
        blocks = bc.extract_blocks(html)
        middle = blocks[2]
        assert middle.link_density == pytest.approx(0.0)
        assert middle.neighbor_link_density > 0.5

    def test_isolated_body_run_has_low_neighbor_density(self):
        html = (
            b"<html><body>"
            b"<p>gender;</p><p>date of birth;</p><p>eye color;</p>"
            b"</body></html>"
        )
        blocks = bc.extract_blocks(html)
        for b in blocks:
            assert b.neighbor_link_density == pytest.approx(0.0)


class TestBlockToFeatureDict:
    def test_itemized_clause_features_differ_from_nav_item_features(self):
        # The exact contrast the Samsung over-trim bug turned on: a data
        # clause ("gender;") has no link and ends in punctuation; a nav
        # label ("Careers") has a link and doesn't.
        html = (
            b"<html><body>"
            b"<nav><p><a href='/careers'>Careers</a></p></nav>"
            b"<article><p>gender;</p></article>"
            b"</body></html>"
        )
        blocks = bc.extract_blocks(html)
        by_text = {b.text: bc.block_to_feature_dict(b) for b in blocks}

        nav_features = by_text["Careers"]
        clause_features = by_text["gender;"]

        assert nav_features["has_link"] == 1.0
        assert nav_features["ends_with_clause_punct"] == 0.0

        assert clause_features["has_link"] == 0.0
        assert clause_features["ends_with_clause_punct"] == 1.0

    def test_boilerplate_ancestor_flags_follow_nearest_landmark_not_any_ancestor(self):
        # Regression for the over-trim bug: an <article> nested inside an
        # outer <nav> wrapper must score as body, not boilerplate, because
        # article is the *nearest* landmark even though nav is also an
        # ancestor further out.
        html = (
            b"<html><body>"
            b"<nav><article><p>Actual policy clause text here.</p></article></nav>"
            b"</body></html>"
        )
        block = bc.extract_blocks(html)[0]
        features = bc.block_to_feature_dict(block)
        assert features["is_body_ancestor"] == 1.0
        assert features["is_boilerplate_ancestor"] == 0.0


class TestTrimBoilerplateBlocks:
    def test_trims_leading_and_trailing_high_confidence_runs(self):
        blocks = [
            make_block("nav text one two three four five"),
            make_block("nav text two two three four five"),
            make_block("the actual policy body paragraph one two three four five"),
            make_block("the actual policy body paragraph two two three four five"),
            make_block("footer text one two three four five"),
        ]
        bc._model = FakeModel([0.95, 0.9, 0.1, 0.05, 0.92])
        bc._model_path_loaded = bc.DEFAULT_MODEL_PATH

        start, end = bc.trim_boilerplate_blocks(blocks)

        assert (start, end) == (2, 4)

    def test_never_trims_past_a_low_confidence_block_in_the_middle(self):
        # Even though blocks 2-5 score high boilerplate again, block 1's low
        # score must stop the leading run right after block 0.
        ten_words = "one two three four five six seven eight nine ten"
        blocks = [make_block(ten_words) for _ in range(6)]
        bc._model = FakeModel([0.95, 0.2, 0.95, 0.95, 0.95, 0.95])
        bc._model_path_loaded = bc.DEFAULT_MODEL_PATH

        start, _ = bc.trim_boilerplate_blocks(blocks)

        assert start == 1  # only the first block trimmed, not blocks 2-5 too

    def test_refuses_to_trim_below_minimum_word_count(self):
        # A short document where nearly every block scores as boilerplate --
        # trimming down to MIN_REMAINING_WORDS-1 words must be refused outright.
        blocks = [make_block("only three words") for _ in range(3)]
        bc._model = FakeModel([0.99, 0.99, 0.99])
        bc._model_path_loaded = bc.DEFAULT_MODEL_PATH

        start, end = bc.trim_boilerplate_blocks(blocks)

        assert (start, end) == (0, len(blocks))

    def test_never_trims_more_than_max_fraction_from_either_side(self):
        # 20 words/block so the two blocks left after a max-fraction trim
        # still clear MIN_REMAINING_WORDS and the cap itself is what's tested.
        padded = f"block {'word ' * 18}".strip()
        blocks = [make_block(padded) for _ in range(10)]
        bc._model = FakeModel([0.99] * 10)
        bc._model_path_loaded = bc.DEFAULT_MODEL_PATH

        start, end = bc.trim_boilerplate_blocks(blocks)

        max_trim = int(len(blocks) * bc.MAX_TRIM_FRACTION)
        assert start == max_trim
        assert (len(blocks) - end) == max_trim

    def test_empty_blocks_returns_empty_range(self):
        assert bc.trim_boilerplate_blocks([]) == (0, 0)

    def test_respects_custom_threshold(self):
        ten_words = "one two three four five six seven eight nine ten"
        blocks = [make_block(ten_words) for _ in range(5)]
        bc._model = FakeModel([0.6, 0.1, 0.1, 0.1, 0.1])
        bc._model_path_loaded = bc.DEFAULT_MODEL_PATH

        # Default threshold (0.85) shouldn't trim block 0 at 0.6 confidence.
        assert bc.trim_boilerplate_blocks(blocks)[0] == 0
        # A looser threshold should.
        assert bc.trim_boilerplate_blocks(blocks, threshold=0.5)[0] == 1


class TestGetBoundaryTrimRange:
    def test_fails_open_to_full_range_when_model_missing(self, tmp_path):
        blocks = [make_block("some text here") for _ in range(3)]
        missing_path = tmp_path / "does_not_exist.joblib"

        start, end = bc.get_boundary_trim_range(blocks, model_path=missing_path)

        assert (start, end) == (0, len(blocks))


class TestExtractPolicyText:
    def test_joins_kept_block_text_with_spaces(self):
        html = b"<html><body><p>First.</p><p>Second.</p></body></html>"
        bc._model = FakeModel([0.1, 0.1])
        bc._model_path_loaded = bc.DEFAULT_MODEL_PATH

        assert bc.extract_policy_text(html) == "First. Second."

    def test_returns_empty_string_for_no_blocks(self):
        assert bc.extract_policy_text(b"<html><body></body></html>") == ""
