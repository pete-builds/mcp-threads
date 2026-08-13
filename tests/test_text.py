"""Splitter, byte-length measure, and link counter."""

from __future__ import annotations

from clients.text import (
    THREADS_TEXT_LIMIT,
    count_unique_links,
    extract_urls,
    fits,
    split_for_chain,
    threads_length,
)

GRINNING = "\U0001f600"  # 4 UTF-8 bytes
FAMILY = "\U0001f468‍\U0001f469‍\U0001f467"  # ZWJ sequence, 18 bytes


# --- byte-aware length -------------------------------------------------


def test_ascii_length_matches_len():
    assert threads_length("hello") == 5 == len("hello")


def test_emoji_counted_as_utf8_bytes_not_one_char():
    # This is the whole point: len() says 1, Threads says 4.
    assert len(GRINNING) == 1
    assert threads_length(GRINNING) == 4


def test_zwj_emoji_sequence_counted_in_bytes():
    assert threads_length(FAMILY) == len(FAMILY.encode("utf-8")) == 18


def test_len_would_pass_but_bytes_reject():
    """400 emoji: len() = 400 (under 500), UTF-8 bytes = 1600 (way over)."""
    text = GRINNING * 400
    assert len(text) == 400  # a naive len() check would let this through
    assert threads_length(text) == 1600
    assert not fits(text)


# --- splitter ----------------------------------------------------------


def test_short_text_is_one_segment():
    assert split_for_chain("just a short post") == ["just a short post"]


def test_empty_text_yields_no_segments():
    assert split_for_chain("   \n  ") == []


def test_every_segment_is_within_the_byte_limit():
    text = ("Sentence number one is here. " * 60).strip()
    segments = split_for_chain(text)
    assert len(segments) > 1
    assert all(threads_length(s) <= THREADS_TEXT_LIMIT for s in segments)


def test_emoji_heavy_text_splits_on_bytes_not_chars():
    # 200 emoji = 800 bytes -> must be at least 2 segments even though
    # len(text) is only 200 and would "fit" under a naive check.
    text = " ".join([GRINNING * 10] * 20)
    segments = split_for_chain(text)
    assert len(segments) >= 2
    for s in segments:
        assert threads_length(s) <= THREADS_TEXT_LIMIT


def test_no_segment_splits_a_multibyte_character():
    text = GRINNING * 400
    segments = split_for_chain(text)
    # Every segment must round-trip through UTF-8 intact and contain only
    # whole emoji.
    for s in segments:
        assert s.encode("utf-8").decode("utf-8") == s
        assert set(s) == {GRINNING}
    assert "".join(segments) == text


def test_paragraph_boundary_preferred_over_sentence():
    para_a = "A" * 300
    para_b = "B" * 300
    segments = split_for_chain(f"{para_a}\n\n{para_b}")
    assert segments == [para_a, para_b]


def test_sentence_boundary_used_when_paragraph_too_big():
    sentences = [f"This is sentence number {i} and it runs on a while." for i in range(30)]
    text = " ".join(sentences)
    segments = split_for_chain(text)
    assert len(segments) > 1
    # Each segment should end on terminal punctuation, i.e. a sentence break.
    assert all(s.rstrip().endswith(".") for s in segments)


def test_never_splits_mid_word_for_ordinary_prose():
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"] * 40
    text = " ".join(words)
    segments = split_for_chain(text)
    rejoined = " ".join(segments).split()
    assert rejoined == words  # no word was cut in half


def test_single_oversized_word_is_hard_split_as_last_resort():
    giant = "x" * 1200  # one "word", no boundary available
    segments = split_for_chain(giant)
    assert len(segments) == 3
    assert all(threads_length(s) <= THREADS_TEXT_LIMIT for s in segments)
    assert "".join(segments) == giant


def test_split_is_lossless_for_prose():
    text = "\n\n".join(f"Paragraph {i}. " + ("word " * 40).strip() for i in range(8))
    segments = split_for_chain(text)
    assert len(segments) > 1
    original_words = text.split()
    assert " ".join(segments).split() == original_words


# --- link counting -----------------------------------------------------


def test_extract_urls_trims_trailing_punctuation():
    assert extract_urls("see https://a.example/x. ok") == ["https://a.example/x"]


def test_duplicate_urls_count_once():
    text = "https://x.example and again https://x.example"
    assert count_unique_links(text) == 1


def test_link_attachment_counts_when_different():
    assert count_unique_links("https://a.example", "https://b.example") == 2


def test_link_attachment_does_not_double_count_when_present_in_text():
    assert count_unique_links("go to https://a.example now", "https://a.example") == 1


def test_five_links_is_allowed_six_is_not():
    five = " ".join(f"https://s{i}.example" for i in range(5))
    assert count_unique_links(five) == 5
    assert count_unique_links(five, "https://s9.example") == 6


def test_no_links_is_zero():
    assert count_unique_links("plain text with no urls at all") == 0
