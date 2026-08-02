"""``_strip_vectors`` drops embedding-like vector properties from the node
dicts the KG tools return.

Once ``:Entity`` nodes carry a 1536-float ``embedding`` (for
``semantic_search`` / ``drevo.vector.query``), every tool that projected the
node with ``.*`` — ``vector_search`` / ``fts_search`` / ``semantic_search`` /
``get_entity`` / ``search_knowledge`` / ``create_entity`` — would echo the whole
vector back (~30 KB of JSON per node), blowing an LLM's token budget. The vector
is used *server-side* for the search; the caller never needs it in the result.
``_strip_vectors`` removes any embedding-like value (a long numeric list) while
leaving real properties untouched.
"""

from __future__ import annotations

from drevo_mcp_bolt.graph import _strip_vectors


def test_drops_a_long_float_embedding() -> None:
    node = {"name": "n", "embedding": [0.1] * 1536, "title": "t"}
    assert _strip_vectors(node) == {"name": "n", "title": "t"}


def test_drops_a_long_int_vector() -> None:
    node = {"embedding": list(range(64)), "k": 1}
    assert _strip_vectors(node) == {"k": 1}


def test_drops_an_arbitrary_vector_property_name() -> None:
    # Not keyed on the name "embedding": any long numeric list is a vector.
    node = {"vec": [0.5] * 128, "name": "n"}
    assert _strip_vectors(node) == {"name": "n"}


def test_keeps_a_short_numeric_list() -> None:
    # Real data (e.g. a few scores/ids) stays — only embedding-sized lists go.
    node = {"scores": [1, 2, 3], "name": "n"}
    assert _strip_vectors(node) == {"scores": [1, 2, 3], "name": "n"}


def test_keeps_strings_and_long_string_lists() -> None:
    node = {"name": "n", "observations": ["obs"] * 200, "count": 5}
    assert _strip_vectors(node) == node


def test_keeps_a_long_bool_list() -> None:
    # Booleans are not embeddings, even in a long list.
    node = {"flags": [True, False] * 50}
    assert _strip_vectors(node) == node


def test_empty_and_vectorless_nodes_are_unchanged() -> None:
    assert _strip_vectors({}) == {}
    assert _strip_vectors({"a": 1, "b": "x"}) == {"a": 1, "b": "x"}


def test_returns_a_new_dict_without_mutating_input() -> None:
    node = {"embedding": [0.0] * 100, "name": "n"}
    out = _strip_vectors(node)
    assert "embedding" in node  # original untouched
    assert "embedding" not in out
