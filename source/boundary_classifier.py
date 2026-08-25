# Transparency Archiver - Online Document Capture Tool
# Boundary detection for captured HTML: identifies the leading/trailing runs of
# header/footer/nav/cookie-banner boilerplate that surround a policy document's
# actual body text, so that boilerplate churn (a nav link changing, a footer
# copyright year ticking over) doesn't get treated as -- or dilute/mask -- a
# real change to the policy content itself.
#
# This module never mutates or replaces an archived capture, and never
# affects change-detection. It only produces an alternate, trimmed view of a
# document's text for source/changes.py's .CHANGES.json diff builder (see
# get_diff_text/_fetch_html_trimmed there), purely so the Comparison Tool's
# display reads cleaner. The full, untrimmed text (equivalent to
# read_text_from_html) remains the archived record and is always what
# change-detection (source/async_compare.py) compares.
#
# The model itself is trained offline -- see scripts/build_boundary_dataset.py
# and scripts/train_boundary_classifier.py -- and loaded here from disk.

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import joblib
from bs4 import BeautifulSoup
from bs4.element import Comment, NavigableString, Tag
from sklearn.base import BaseEstimator, TransformerMixin

from source.globals import logger

DEFAULT_MODEL_PATH = Path("models/boundary_classifier.joblib")

# Ancestor tags that are near-certain signals for/against boilerplate. Used
# both as weak-labeling heuristics when building the training set and as
# model input features.
BOILERPLATE_ANCESTOR_TAGS = frozenset({"nav", "header", "footer", "aside"})
BODY_ANCESTOR_TAGS = frozenset({"main", "article"})

# Tags treated as "inline" when deciding whether an element is a leaf text
# block -- an element whose entire subtree is just text and these tags is
# walked as a single block, rather than recursed into further.
INLINE_TAGS = frozenset(
    {
        "a",
        "span",
        "b",
        "i",
        "strong",
        "em",
        "br",
        "small",
        "sup",
        "sub",
        "u",
        "mark",
        "abbr",
        "code",
        "time",
        "label",
        "wbr",
    }
)

# Conservative guardrails for the trim decision (see get_boundary_trim_range):
# never trim more than this fraction of blocks from either side, and never
# trim at all if it would leave fewer words than data_quality_check's floor
# (source/async_compare.py) would accept as a valid document.
MAX_TRIM_FRACTION = 0.4
MIN_REMAINING_WORDS = 20
DEFAULT_BOILERPLATE_THRESHOLD = 0.85

_model = None
_model_path_loaded: Optional[Path] = None
_model_lock = threading.Lock()


@dataclass
class Block:
    """One leaf text block from a document, in document order, along with the
    structural features used to decide whether it's boilerplate."""

    text: str
    ancestor_tags: frozenset
    nearest_landmark: Optional[str]
    link_density: float
    char_len: int
    position_pct: float
    neighbor_link_density: float = 0.0


# Window size (blocks on each side) averaged into neighbor_link_density.
NEIGHBOR_WINDOW = 3

# A block's own text ending in one of these reads as a legal-style
# enumerated clause ("gender;", "date of birth;") rather than a nav/footer
# label, which is almost never terminally punctuated.
CLAUSE_TERMINAL_PUNCTUATION = (";", ".")


def _is_leaf_block(tag: Tag) -> bool:
    """True if `tag`'s entire subtree is just text and inline tags -- i.e. it's
    a maximal container with no further block-level structure inside it."""
    return all(
        isinstance(desc, NavigableString) or desc.name in INLINE_TAGS # type: ignore
        for desc in tag.descendants
    )


def _nearest_landmark(node) -> Optional[str]:
    """
    The nearest of BOILERPLATE_ANCESTOR_TAGS/BODY_ANCESTOR_TAGS enclosing
    `node` -- checking `node` itself first, then its parents nearest-first.

    Some real pages nest a semantic body container inside an outer semantic
    boilerplate one (e.g. a whole <article> sitting inside a page-level
    <nav> wrapper used purely for layout). Using set-membership across *all*
    ancestors -- with boilerplate winning regardless of distance -- would
    label that entire article as boilerplate. The nearest landmark is the
    one that actually describes this block, so it takes precedence.
    """
    landmarks = BOILERPLATE_ANCESTOR_TAGS | BODY_ANCESTOR_TAGS
    candidates = [node] if isinstance(node, Tag) else []
    candidates.extend(p for p in node.parents if isinstance(p, Tag))
    for tag in candidates:
        if tag.name in landmarks:
            return tag.name
    return None


def _link_density(tag: Tag) -> float:
    """Fraction of `tag`'s text that sits inside an <a> element. Nav/footer
    link lists score high; body prose scores near zero."""
    total = 0
    link = 0
    for desc in tag.descendants:
        if isinstance(desc, NavigableString) and not isinstance(desc, Comment):
            length = len(str(desc))
            total += length
            if desc.find_parent("a") is not None:
                link += length
    return (link / total) if total else 0.0


def extract_blocks(html: bytes) -> list[Block]:
    """
    Walks parsed HTML in document order and returns one Block per leaf text
    container (a tag with no nested block-level structure, or a stray text
    node), tagged with the structural features the boundary classifier and
    the conservative trim rule use.

    Roughly parallels source/async_compare.py:read_text_from_html's coverage
    (all visible text, style/script/img removed) but keeps block boundaries
    and structural context instead of flattening everything into one string.
    """
    soup = BeautifulSoup(html, features="lxml")
    for rTag in ("style", "script", "img", "noscript"):
        for tag in soup.find_all(rTag):
            tag.decompose()

    raw: list[tuple] = []

    def ancestor_tags_of(node) -> frozenset:
        names = [node.name] if isinstance(node, Tag) else []
        names.extend(p.name for p in node.parents if isinstance(p, Tag))
        return frozenset(names)

    def walk(node):
        for child in node.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                text = " ".join(str(child).split())
                if text:
                    raw.append(
                        (text, ancestor_tags_of(node), _nearest_landmark(node), 0.0, len(text))
                    )
                continue
            if not isinstance(child, Tag):
                continue
            if _is_leaf_block(child):
                text = " ".join(child.get_text(separator=" ", strip=True).split())
                if text:
                    raw.append(
                        (
                            text,
                            ancestor_tags_of(child),
                            _nearest_landmark(child),
                            _link_density(child),
                            len(text),
                        )
                    )
            else:
                walk(child)

    walk(soup.body or soup)

    n = len(raw)
    densities = [link_density for (_, _, _, link_density, _) in raw]
    blocks = [
        Block(
            text=text,
            ancestor_tags=ancestor_tags,
            nearest_landmark=nearest_landmark,
            link_density=link_density,
            char_len=char_len,
            position_pct=(i / (n - 1)) if n > 1 else 0.0,
        )
        for i, (text, ancestor_tags, nearest_landmark, link_density, char_len) in enumerate(raw)
    ]
    for i, block in enumerate(blocks):
        window = densities[max(0, i - NEIGHBOR_WINDOW) : i + NEIGHBOR_WINDOW + 1]
        block.neighbor_link_density = sum(window) / len(window)
    return blocks


def block_to_feature_dict(block: Block) -> dict:
    """
    Converts a Block into the flat feature dict the model pipeline's
    BlockFeatureExtractor stages consume. Kept as a standalone function (used
    identically by scripts/build_boundary_dataset.py and at inference time
    here) so training and inference can never drift out of sync on features.
    """
    stripped = block.text.rstrip()
    return {
        "text": block.text,
        "link_density": block.link_density,
        "char_len": block.char_len,
        "position_pct": block.position_pct,
        "is_boilerplate_ancestor": 1.0
        if block.nearest_landmark in BOILERPLATE_ANCESTOR_TAGS
        else 0.0,
        "is_body_ancestor": 1.0 if block.nearest_landmark in BODY_ANCESTOR_TAGS else 0.0,
        "word_count": float(len(block.text.split())),
        # A block with zero links is structurally unlike a nav/footer item --
        # almost every genuine nav/link-list entry is wrapped in an <a>, even
        # if link_density alone (fraction of *characters*) sits below the
        # link-heavy threshold for a longer block.
        "has_link": 1.0 if block.link_density > 0 else 0.0,
        # Legal-style enumerated clauses ("gender;", "date of birth;") end in
        # terminal punctuation; nav/footer labels almost never do.
        "ends_with_clause_punct": 1.0
        if stripped.endswith(CLAUSE_TERMINAL_PUNCTUATION)
        else 0.0,
        # Local context: the average link density of this block's nearby
        # neighbors. A lone short, unlinked block sitting inside an
        # otherwise link-heavy nav run reads very differently from the same
        # block sitting inside a run of equally unlinked policy clauses --
        # this is what actually distinguishes "a link list" from "a bulleted
        # list of data categories" beyond this one block's own density.
        "neighbor_link_density": block.neighbor_link_density,
    }


NUMERIC_FEATURE_KEYS = [
    "link_density",
    "char_len",
    "position_pct",
    "is_boilerplate_ancestor",
    "is_body_ancestor",
    "word_count",
    "has_link",
    "ends_with_clause_punct",
    "neighbor_link_density",
]


class BlockFeatureExtractor(BaseEstimator, TransformerMixin):
    """
    Pulls one half of a block's feature dict (see block_to_feature_dict) out
    for its corresponding half of the model pipeline: "text" for the TF-IDF
    vectorizer, "numeric" for the structural features. Defined here (rather
    than inline in the training script) so the fitted pipeline saved by
    scripts/train_boundary_classifier.py can be unpickled by this module.
    """

    def __init__(self, mode: str = "text"):
        self.mode = mode

    def fit(self, X, y=None):
        return self

    def transform(self, X):
        if self.mode == "text":
            return [row["text"] for row in X]
        return [[row[key] for key in NUMERIC_FEATURE_KEYS] for row in X]


def load_model(model_path: Optional[Path] = None):
    """
    Loads (and caches) the trained boundary classifier pipeline. Re-loads
    only if a different model_path is requested than what's cached.
    """
    global _model, _model_path_loaded
    path = Path(model_path) if model_path else DEFAULT_MODEL_PATH

    with _model_lock:
        if _model is None or _model_path_loaded != path:
            if not path.exists():
                raise FileNotFoundError(
                    f"No boundary classifier model found at {path}. "
                    "Run scripts/build_boundary_dataset.py and "
                    "scripts/train_boundary_classifier.py to train one."
                )
            logger.info(f"Loading boundary classifier from {path}")
            _model = joblib.load(path)
            _model_path_loaded = path

    return _model


def boilerplate_probabilities(
    blocks: list[Block], model_path: Optional[Path] = None
) -> list[float]:
    """
    Returns the model's estimated probability (0-1) that each block in
    `blocks` is boilerplate, in the same order.
    """
    if not blocks:
        return []
    model = load_model(model_path)
    rows = [block_to_feature_dict(b) for b in blocks]
    return [float(row[1]) for row in model.predict_proba(rows)]


def trim_boilerplate_blocks(
    blocks: list[Block],
    threshold: float = DEFAULT_BOILERPLATE_THRESHOLD,
    model_path: Optional[Path] = None,
) -> tuple[int, int]:
    """
    Returns (start, end) block indices (end exclusive) marking the range of
    `blocks` to keep as policy body text.

    Conservative by construction:
      - Only trims a *leading* and/or *trailing* run of blocks scoring at or
        above `threshold`. A single low-confidence block anywhere stops that
        run immediately, even if later blocks score high boilerplate again --
        real content is never cut out of the middle of the document.
      - Never trims more than MAX_TRIM_FRACTION of the blocks from either
        side, regardless of confidence.
      - Refuses to trim at all (returns the full range) if doing so would
        leave fewer than MIN_REMAINING_WORDS words, so an over-confident
        model can't gut a short but genuine document.

    Raises if the model can't be loaded or run; callers that want to fail
    open onto the untrimmed text should use get_boundary_trim_range instead.
    """
    n = len(blocks)
    if n == 0:
        return 0, 0

    probs = boilerplate_probabilities(blocks, model_path)

    max_leading = int(n * MAX_TRIM_FRACTION)
    start = 0
    while start < max_leading and probs[start] >= threshold:
        start += 1

    min_trailing_start = n - int(n * MAX_TRIM_FRACTION)
    end = n
    while end > min_trailing_start and end > start and probs[end - 1] >= threshold:
        end -= 1

    kept_words = sum(len(b.text.split()) for b in blocks[start:end])
    if kept_words < MIN_REMAINING_WORDS:
        return 0, n

    return start, end


def get_boundary_trim_range(
    blocks: list[Block],
    threshold: Optional[float] = None,
    model_path: Optional[Path] = None,
) -> tuple[int, int]:
    """
    Fail-open wrapper around trim_boilerplate_blocks: if the model can't be
    loaded or errors during inference, keeps the full block range instead of
    blocking or guessing.
    """
    if threshold is None:
        threshold = DEFAULT_BOILERPLATE_THRESHOLD
    try:
        return trim_boilerplate_blocks(blocks, threshold, model_path)
    except Exception as e:
        logger.warning(f"Boundary classifier failed, keeping full text untrimmed: {e}")
        return 0, len(blocks)


def extract_policy_text(
    html: bytes,
    threshold: Optional[float] = None,
    model_path: Optional[Path] = None,
) -> str:
    """
    Extracts text from `html`, trimming a conservative, high-confidence
    leading/trailing run of header/footer/nav boilerplate blocks. Falls back
    to the full flattened text if no model is available, the model errors, or
    block extraction finds nothing to work with.

    This is meant for .CHANGES.json diff display only (see
    source/changes.py:get_diff_text) -- it never affects what gets archived
    as the actual capture, and never affects change-detection.
    """
    blocks = extract_blocks(html)
    if not blocks:
        return ""
    start, end = get_boundary_trim_range(blocks, threshold, model_path)
    return " ".join(block.text for block in blocks[start:end])
