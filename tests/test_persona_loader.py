"""Tests for persona_loader: resolve personas from .seneschal.yml."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from persona_loader import (  # noqa: E402
    BUILTIN_PERSONAS,
    MAX_PERSONA_PROMPT_LEN,
    Persona,
    default_personas,
    load_personas,
)


# --------------------------------------------------------------------------
# default_personas
# --------------------------------------------------------------------------


def test_default_personas_returns_all_six_builtins():
    personas = default_personas()
    assert len(personas) == 6
    names = {p.name for p in personas}
    assert names == set(BUILTIN_PERSONAS.keys())
    for p in personas:
        assert p.source == "builtin"
        assert p.subagent_type == BUILTIN_PERSONAS[p.name]
        assert p.prompt_text == ""


# --------------------------------------------------------------------------
# load_personas: empty / fallback behavior
# --------------------------------------------------------------------------


def test_empty_config_returns_all_builtins():
    with tempfile.TemporaryDirectory() as d:
        result = load_personas([], d)
        assert len(result) == 6


def test_none_config_returns_all_builtins():
    # parse_config may pass None if the field is missing — load_personas
    # must still return defaults rather than crash.
    with tempfile.TemporaryDirectory() as d:
        result = load_personas(None, d)  # type: ignore[arg-type]
        assert len(result) == 6


def test_all_bad_entries_falls_back_to_defaults():
    # Every entry fails → safer to run defaults than zero reviewers.
    with tempfile.TemporaryDirectory() as d:
        result = load_personas(
            [
                {"builtin": "does-not-exist"},
                {"file": "does-not-exist.md"},
                "not-a-dict",
                {"weird_key": "value"},
            ],
            d,
        )
        assert len(result) == 6
        assert all(p.source == "builtin" for p in result)


# --------------------------------------------------------------------------
# Built-in persona resolution
# --------------------------------------------------------------------------


def test_single_builtin_resolves():
    with tempfile.TemporaryDirectory() as d:
        result = load_personas([{"builtin": "architect"}], d)
        assert len(result) == 1
        assert result[0].name == "architect"
        assert result[0].subagent_type == "seneschal-architect"
        assert result[0].source == "builtin"


def test_multiple_builtins_resolve_in_order():
    with tempfile.TemporaryDirectory() as d:
        result = load_personas(
            [{"builtin": "security"}, {"builtin": "architect"}], d
        )
        assert [p.name for p in result] == ["security", "architect"]


def test_unknown_builtin_is_skipped():
    # If ONE valid entry, use it; unknown silently dropped.
    with tempfile.TemporaryDirectory() as d:
        result = load_personas(
            [{"builtin": "architect"}, {"builtin": "bogus"}], d
        )
        assert len(result) == 1
        assert result[0].name == "architect"


def test_builtin_name_case_insensitive():
    with tempfile.TemporaryDirectory() as d:
        result = load_personas([{"builtin": "ARCHITECT"}], d)
        assert len(result) == 1
        assert result[0].subagent_type == "seneschal-architect"


# --------------------------------------------------------------------------
# File-based persona resolution
# --------------------------------------------------------------------------


def test_file_persona_loads_content():
    with tempfile.TemporaryDirectory() as d:
        persona_dir = os.path.join(d, ".seneschal", "personas")
        os.makedirs(persona_dir)
        persona_path = os.path.join(persona_dir, "hipaa.md")
        with open(persona_path, "w") as fh:
            fh.write(
                "You are a HIPAA reviewer. Focus on PHI handling, audit logging, "
                "and encryption at rest.\n"
            )
        result = load_personas(
            [{"file": ".seneschal/personas/hipaa.md"}], d
        )
        assert len(result) == 1
        p = result[0]
        assert p.name == "hipaa"
        assert p.subagent_type is None
        assert "HIPAA" in p.prompt_text
        assert p.source == "file:.seneschal/personas/hipaa.md"


def test_file_persona_path_traversal_rejected():
    # Any ../-style path escaping repo_root must be rejected.
    with tempfile.TemporaryDirectory() as d:
        # Create a file OUTSIDE the repo root (as if attacker pointed there).
        outer_dir = tempfile.mkdtemp()
        outer_path = os.path.join(outer_dir, "sensitive.md")
        with open(outer_path, "w") as fh:
            fh.write("secret prompt")
        try:
            # Use relative path traversal
            escape_path = os.path.relpath(outer_path, d)
            assert ".." in escape_path, "test setup: path must traverse upward"
            result = load_personas([{"file": escape_path}], d)
            # Traversal rejected → falls back to defaults
            assert len(result) == 6
            assert all(p.source == "builtin" for p in result)
        finally:
            os.remove(outer_path)
            os.rmdir(outer_dir)


def test_file_persona_missing_file_is_skipped():
    with tempfile.TemporaryDirectory() as d:
        result = load_personas(
            [
                {"builtin": "architect"},
                {"file": "does-not-exist.md"},
            ],
            d,
        )
        # Missing file drops, builtin remains
        assert len(result) == 1
        assert result[0].name == "architect"


def test_file_persona_empty_file_is_skipped():
    with tempfile.TemporaryDirectory() as d:
        empty_path = os.path.join(d, "empty.md")
        open(empty_path, "w").close()
        result = load_personas([{"file": "empty.md"}], d)
        # Empty file → rejected → falls back to defaults (no resolved personas)
        assert len(result) == 6


def test_file_persona_content_truncated_at_cap():
    # Oversize file should be truncated, not rejected.
    with tempfile.TemporaryDirectory() as d:
        big_path = os.path.join(d, "big.md")
        with open(big_path, "w") as fh:
            fh.write("A" * (MAX_PERSONA_PROMPT_LEN * 3))
        result = load_personas([{"file": "big.md"}], d)
        assert len(result) == 1
        assert len(result[0].prompt_text) == MAX_PERSONA_PROMPT_LEN


def test_file_persona_strips_control_chars():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sneaky.md")
        with open(path, "w") as fh:
            fh.write("Normal text\x00\x01\x02more text\n")
        result = load_personas([{"file": "sneaky.md"}], d)
        assert len(result) == 1
        assert "\x00" not in result[0].prompt_text
        assert "\x01" not in result[0].prompt_text
        assert "Normal text" in result[0].prompt_text


# --------------------------------------------------------------------------
# Mixed / overall behavior
# --------------------------------------------------------------------------


def test_mix_of_builtin_and_file_personas():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "perf.md")
        with open(path, "w") as fh:
            fh.write("Focus on N+1 queries and allocation hotspots.\n")
        result = load_personas(
            [
                {"builtin": "security"},
                {"file": "perf.md"},
            ],
            d,
        )
        assert len(result) == 2
        assert result[0].subagent_type == "seneschal-security"
        assert result[1].subagent_type is None
        assert "N+1" in result[1].prompt_text


def test_cap_of_10_personas_per_repo():
    with tempfile.TemporaryDirectory() as d:
        # 15 builtin entries (all same name, that's fine for this cap test)
        entries = [{"builtin": "architect"} for _ in range(15)]
        result = load_personas(entries, d)
        assert len(result) == 10


def test_non_dict_entry_silently_dropped():
    with tempfile.TemporaryDirectory() as d:
        result = load_personas(
            [
                {"builtin": "architect"},
                "just a string",
                42,
                None,
                {"builtin": "security"},
            ],
            d,
        )
        assert [p.name for p in result] == ["architect", "security"]
