"""Tests for has_test_framework(): the gate that keeps test_gaps from
firing on repos with zero test infrastructure."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_gaps import has_test_framework  # noqa: E402


def test_missing_dir_defaults_true():
    """Unknown → don't suppress findings."""
    assert has_test_framework("/does/not/exist") is True
    assert has_test_framework(None) is True
    assert has_test_framework("") is True


def test_empty_repo_returns_false(tmp_path):
    assert has_test_framework(str(tmp_path)) is False


def test_js_test_script_in_package_json(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "x",
        "scripts": {"build": "tsc", "test": "jest"},
    }))
    assert has_test_framework(str(tmp_path)) is True


def test_js_no_test_script_no_frameworks_returns_false(tmp_path):
    # package.json exists but has nothing test-shaped.
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "x",
        "scripts": {"build": "next build", "dev": "next dev"},
    }))
    assert has_test_framework(str(tmp_path)) is False


def test_js_jest_dep_without_test_script(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "x",
        "devDependencies": {"jest": "^29"},
    }))
    assert has_test_framework(str(tmp_path)) is True


def test_js_vitest_config_file(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "x"}')
    (tmp_path / "vitest.config.ts").write_text("export default {};")
    assert has_test_framework(str(tmp_path)) is True


def test_js_tests_dir(tmp_path):
    (tmp_path / "__tests__").mkdir()
    assert has_test_framework(str(tmp_path)) is True


def test_python_pytest_ini(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    assert has_test_framework(str(tmp_path)) is True


def test_python_pyproject_pytest_section(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\naddopts = "-q"\n')
    assert has_test_framework(str(tmp_path)) is True


def test_python_pyproject_without_pytest_returns_false(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.poetry]\nname = "x"\n')
    assert has_test_framework(str(tmp_path)) is False


def test_go_test_file_anywhere(tmp_path):
    pkg = tmp_path / "pkg" / "sub"
    pkg.mkdir(parents=True)
    (pkg / "thing_test.go").write_text("package sub\n")
    assert has_test_framework(str(tmp_path)) is True


def test_swift_tests_dir(tmp_path):
    (tmp_path / "Sources").mkdir()
    (tmp_path / "Tests").mkdir()
    assert has_test_framework(str(tmp_path)) is True


def test_ignores_node_modules_tests(tmp_path):
    # Downstream deps have their own tests/_tests; those shouldn't count.
    nm = tmp_path / "node_modules" / "some-package"
    nm.mkdir(parents=True)
    (nm / "thing_test.go").write_text("package x\n")
    (tmp_path / "package.json").write_text('{"name": "x"}')
    assert has_test_framework(str(tmp_path)) is False
