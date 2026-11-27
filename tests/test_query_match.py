"""Tests for ai_signal.query_match -- deterministic boolean query
matching, no network, no AI.

The central regression here is a real bug caught live: a flat bag-of-words
matcher let an article about school recycling projects satisfy the
"AI in Textiles" topic query because it merely shared the words "textile"
and "industry" with the query text, never checking that the query's
AND(AI-related-terms) clause was actually present. See query_match.py's
module docstring for the full story and the real article text this test's
REAL_FALSE_POSITIVE_TEXT is drawn from.

Run: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_signal.query_match import matches, parse_query

TOPIC_QUERY = (
    '("textile industry" OR "apparel industry" OR "garment manufacturing" '
    'OR "textile manufacturing") AND ("AI" OR "artificial intelligence" '
    'OR "automation" OR "machine learning" OR "robotics")'
)

#: Drawn from the real article that triggered this fix: textilesresources.com's
#: "Indian school students tackle India's 7.7mn tonne textile waste
#: challenge" -- genuinely about the textile industry, genuinely never
#: about AI, automation, machine learning, or robotics.
REAL_FALSE_POSITIVE_TEXT = (
    "India generates approximately 7.7 million tonnes of textile waste "
    "annually. Children's Academy International School has launched "
    "Clothes for Conscience, a community engagement initiative designed "
    "to integrate sustainability into the core curriculum. The initiative "
    "encourages students to look past the classroom and engage with the "
    "physical realities of the textile industry. Students are now putting "
    "circular economy principles into practice by reclaiming discarded "
    "fabrics to create new, functional goods."
)

REAL_TRUE_POSITIVE_TEXT = (
    "Coats Digital announced that Sri Lankan apparel manufacturer "
    "Winterquilts has adopted its AI-powered GSDQuest platform to "
    "transform garment costing across its production lines, part of a "
    "broader shift toward automation in the textile industry."
)


class TestParseQuery(unittest.TestCase):
    def test_topic_query_parses_into_two_and_clauses(self):
        clauses = parse_query(TOPIC_QUERY)
        self.assertEqual(len(clauses), 2)
        self.assertIn("textile industry", clauses[0])
        self.assertIn("AI", clauses[1])

    def test_entity_plus_expansion_parses_into_two_and_clauses(self):
        """Clause ORDER is not meaningful -- `matches()` requires every
        clause regardless of position -- so this checks clause *contents*,
        not which index each ends up at (the parenthesized group is
        processed before the leftover text, so the expansion clause
        legitimately comes first)."""
        clauses = parse_query('"K.P.R. Mill" (announces OR launches OR expansion)')
        self.assertEqual(len(clauses), 2)
        self.assertIn(["K.P.R. Mill"], clauses)
        self.assertTrue(any("announces" in c for c in clauses))

    def test_bare_phrase_parses_into_one_clause_one_alternative(self):
        clauses = parse_query('"K.P.R. Mill"')
        self.assertEqual(clauses, [["K.P.R. Mill"]])

    def test_flat_or_chain_parses_into_one_clause_many_alternatives(self):
        clauses = parse_query('"Name1" OR "Name2" OR "Name3"')
        self.assertEqual(len(clauses), 1)
        self.assertEqual(set(clauses[0]), {"Name1", "Name2", "Name3"})

    def test_malformed_query_falls_back_to_the_whole_string(self):
        clauses = parse_query("just some words, no structure")
        self.assertEqual(len(clauses), 1)
        self.assertIn("just some words, no structure", clauses[0])

    def test_empty_query_produces_no_clauses(self):
        self.assertEqual(parse_query(""), [])
        self.assertEqual(parse_query('   ""  '), [])


class TestMatches(unittest.TestCase):
    def test_the_real_false_positive_no_longer_matches(self):
        """The actual regression: this article must NOT satisfy the
        AI-in-textiles topic query."""
        self.assertFalse(matches(REAL_FALSE_POSITIVE_TEXT, TOPIC_QUERY))

    def test_a_genuine_ai_in_textiles_article_still_matches(self):
        """The fix must not have overcorrected into matching nothing."""
        self.assertTrue(matches(REAL_TRUE_POSITIVE_TEXT, TOPIC_QUERY))

    def test_satisfying_only_the_first_and_clause_is_not_enough(self):
        text = "The textile industry saw strong export growth this quarter."
        self.assertFalse(matches(text, TOPIC_QUERY))

    def test_satisfying_only_the_second_and_clause_is_not_enough(self):
        text = "A new machine learning breakthrough was announced by researchers."
        self.assertFalse(matches(text, TOPIC_QUERY))

    def test_entity_query_needs_both_the_name_and_an_event_verb(self):
        query = '"K.P.R. Mill" (announces OR launches OR expansion)'
        self.assertTrue(matches("K.P.R. Mill announces a new plant.", query))
        self.assertFalse(matches("K.P.R. Mill is a textile company.", query))
        self.assertFalse(matches("A rival company announces an expansion.", query))

    def test_flat_or_chain_matches_on_any_single_alternative(self):
        query = '"Coats Digital" OR "BlueKaktus" OR "WFX"'
        self.assertTrue(matches("BlueKaktus raised new funding.", query))
        self.assertFalse(matches("An unrelated company raised funding.", query))

    def test_short_abbreviation_does_not_match_inside_unrelated_words(self):
        """"AI" as a bare two-letter term must not match "again",
        "maintain", or "Spain" -- the exact failure mode a naive substring
        check would have."""
        for word in ("again", "maintain", "Spain", "contains", "explains"):
            with self.subTest(word=word):
                self.assertFalse(matches(f"The company {word} its position.", '"AI"'))

    def test_short_abbreviation_matches_as_a_standalone_word_or_hyphenated(self):
        self.assertTrue(matches("The plant uses AI for quality control.", '"AI"'))
        self.assertTrue(matches("An AI-powered inspection system was installed.", '"AI"'))

    def test_matching_is_case_insensitive(self):
        self.assertTrue(matches("ai-powered systems are rolling out.", '"AI"'))
        self.assertTrue(matches("AUTOMATION improved throughput.", '"automation"'))

    def test_empty_text_never_matches(self):
        self.assertFalse(matches("", TOPIC_QUERY))

    def test_empty_query_never_matches(self):
        self.assertFalse(matches("any text at all", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
