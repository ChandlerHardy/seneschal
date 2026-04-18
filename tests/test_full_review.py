"""Tests for full_review: persona state-file write.

We don't invoke `claude -p` in tests — too slow and requires the CLI
and API keys. We test only the state-file plumbing (the part that
decides what personas the slash command will run).
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from full_review import _write_persona_state  # noqa: E402
from persona_loader import Persona  # noqa: E402


def _builtin(name):
    return Persona(
        name=name,
        subagent_type=f"seneschal-{name}",
        prompt_text="",
        source="builtin",
    )


def _file(name, prompt="You review X."):
    return Persona(
        name=name,
        subagent_type=None,
        prompt_text=prompt,
        source=f"file:.seneschal/personas/{name}.md",
    )


def test_write_persona_state_creates_dir_and_file():
    with tempfile.TemporaryDirectory() as d:
        personas = [_builtin("architect"), _builtin("security")]
        path = _write_persona_state(42, d, personas)
        assert path.exists()
        assert path.name == "seneschal-personas-42.json"
        # Parent dir .claude/plans auto-created
        assert path.parent == (
            __import__("pathlib").Path(d) / ".claude" / "plans"
        )


def test_write_persona_state_serializes_all_fields():
    with tempfile.TemporaryDirectory() as d:
        personas = [
            _builtin("architect"),
            _file("hipaa", "Focus on PHI handling."),
        ]
        path = _write_persona_state(123, d, personas)
        data = json.loads(path.read_text())
        assert data["pr_number"] == 123
        assert len(data["personas"]) == 2
        # Builtin entry
        assert data["personas"][0] == {
            "name": "architect",
            "subagent_type": "seneschal-architect",
            "prompt_text": "",
            "source": "builtin",
        }
        # File entry
        assert data["personas"][1] == {
            "name": "hipaa",
            "subagent_type": None,
            "prompt_text": "Focus on PHI handling.",
            "source": "file:.seneschal/personas/hipaa.md",
        }


def test_write_persona_state_handles_empty_list():
    # Caller shouldn't send us an empty list (that's the default-personas case
    # handled upstream), but if they do, don't crash.
    with tempfile.TemporaryDirectory() as d:
        path = _write_persona_state(1, d, [])
        data = json.loads(path.read_text())
        assert data["pr_number"] == 1
        assert data["personas"] == []
