"""Config loading, dotted paths, and JSONC parsing."""
import json
import os

from swingtrader.config import Config, deep_merge, strip_jsonc

BASE = os.path.join(os.path.dirname(__file__), "..", "config", "base.jsonc")


def test_shipped_config_parses_and_matches_defaults():
    """config/base.jsonc is documentation as much as configuration. If it drifts
    from the built-in defaults the comments start lying."""
    c = Config.load(BASE)
    d = Config()
    diff = {k: (v, c.flatten().get(k)) for k, v in d.flatten().items()
            if c.flatten().get(k) != v}
    assert not diff, f"base.jsonc has drifted from the defaults: {diff}"


def test_strip_jsonc_keeps_slashes_inside_strings():
    src = '{"a": 1, // trailing comment\n "url": "http://x//y", /* block */ "b": 2}'
    parsed = json.loads(strip_jsonc(src))
    assert parsed == {"a": 1, "url": "http://x//y", "b": 2}


def test_strip_jsonc_handles_escaped_quotes():
    src = r'{"a": "he said \"hi\" // not a comment", "b": 2}'
    parsed = json.loads(strip_jsonc(src))
    assert parsed["a"] == 'he said "hi" // not a comment'


def test_dotted_get_and_set():
    c = Config()
    assert c.get("risk.max_positions") == 6
    c.set("risk.max_positions", 9)
    assert c.get("risk.max_positions") == 9
    assert c.get("nope.missing", "fallback") == "fallback"


def test_with_overrides_does_not_mutate_the_original():
    """The optimiser derives thousands of configs from one base; if overrides
    leaked back, every later sample would be contaminated by the earlier ones."""
    c = Config()
    before = c.get("exit.trail_atr_mult")
    c2 = c.with_overrides({"exit.trail_atr_mult": 99.0})
    assert c2.get("exit.trail_atr_mult") == 99.0
    assert c.get("exit.trail_atr_mult") == before


def test_deep_merge_preserves_unspecified_nested_keys():
    merged = deep_merge({"a": {"x": 1, "y": 2}}, {"a": {"y": 3}})
    assert merged == {"a": {"x": 1, "y": 3}}


def test_every_flattened_key_is_addressable():
    c = Config()
    for k, v in c.flatten().items():
        assert c.get(k) == v, f"{k} is not reachable by dotted path"
