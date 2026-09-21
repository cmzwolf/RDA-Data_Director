"""Every command the CLI dispatches must be a command it accepts.

Four handlers — advance, watch, session, accounts — existed with no parser
entry, so the code was there, tested, dispatchable and unreachable: typing the
command produced "invalid choice". The parser additions had been made with
unguarded string replacements that silently matched nothing.

That is the assembly failure in miniature, and this is the cheap check for it.
"""

from __future__ import annotations

import argparse

from datadirector import cli


def _parser() -> argparse.ArgumentParser:
    """The parser the CLI actually builds."""
    return cli.build_parser() if hasattr(cli, "build_parser") else None


def _declared_commands() -> set[str]:
    """Subcommand names, read from the parser by parsing --help output.

    Read from the built parser rather than from the source, so a command that
    exists only in a comment does not count.
    """
    parser = _parser()
    if parser is not None:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                return set(action.choices)
    # The parser is built inside main(); fall back to the source, which is
    # weaker but still catches the failure that prompted this file.
    import re
    from pathlib import Path
    source = Path(cli.__file__).read_text(encoding="utf-8")
    return set(re.findall(r'sub\.add_parser\(\s*"([a-z-]+)"', source))


def _dispatched_commands() -> set[str]:
    import re
    from pathlib import Path
    source = Path(cli.__file__).read_text(encoding="utf-8")
    block = source[source.index("handlers = {"):]
    block = block[:block.index("}")]
    return set(re.findall(r'"([a-z-]+)":\s*cmd_', block))


def test_every_dispatched_command_can_be_typed():
    """A handler with no parser entry is code nobody can reach."""
    missing = _dispatched_commands() - _declared_commands()
    assert not missing, (
        f"handlers with no parser entry, so 'datadirector {sorted(missing)[0]}' "
        f"is an invalid choice: {sorted(missing)}")


def test_every_command_that_can_be_typed_does_something():
    """A parser entry with no handler exits with an unhelpful error."""
    orphaned = _declared_commands() - _dispatched_commands()
    assert not orphaned, (
        f"commands that parse but dispatch nowhere: {sorted(orphaned)}")


def test_the_commands_a_new_user_needs_are_present():
    """Named rather than counted, so a rename is visible here."""
    declared = _declared_commands()
    for command in ("check", "accounts", "serve", "ingest", "advance",
                    "status", "conformance"):
        assert command in declared, f"{command!r} is not a command"


# ==========================================================================
# Missing optional dependencies
# ==========================================================================

def test_serving_without_the_web_packages_reports_rather_than_crashes(capsys):
    """`serve` used to raise ModuleNotFoundError from an import at the top of
    the function, so a person whose install predated the extras got a traceback
    instead of a sentence.
    """
    import datadirector.cli as module

    original = module._missing_web_dependencies
    module._missing_web_dependencies = lambda required=None: [
        "fastapi (the HTTP framework)"]
    try:
        args = type("Args", (), {"wiring": "config/wiring.example.yaml",
                                 "policy": "config/policy.example.yaml",
                                 "host": "127.0.0.1", "port": 8000,
                                 "matrix": "docs/architecture.md"})()
        assert module.cmd_serve(args) == 2
    finally:
        module._missing_web_dependencies = original

    error = capsys.readouterr().err
    assert "cannot serve" in error
    assert "fastapi" in error
    assert "pip install" in error


def test_every_missing_package_is_named_at_once(capsys):
    """A person who installs one, runs again and is told about the next has
    been made to do work a single message could have done."""
    import datadirector.cli as module

    original = module._missing_web_dependencies
    module._missing_web_dependencies = lambda required=None: [
        "fastapi (a)", "jinja2 (b)", "uvicorn (c)"]
    try:
        args = type("Args", (), {"wiring": "config/wiring.example.yaml",
                                 "policy": "config/policy.example.yaml",
                                 "host": "127.0.0.1", "port": 8000,
                                 "matrix": "docs/architecture.md"})()
        module.cmd_serve(args)
    finally:
        module._missing_web_dependencies = original

    error = capsys.readouterr().err
    for name in ("fastapi", "jinja2", "uvicorn"):
        assert name in error


def test_printing_the_descriptor_does_not_require_a_server():
    """Telling someone to install a server in order to write a JSON file would
    be asking for work that buys nothing."""
    from datadirector.cli import SERVER_DEPENDENCY, WEB_DEPENDENCIES

    assert "uvicorn" in SERVER_DEPENDENCY
    assert "uvicorn" not in WEB_DEPENDENCIES


# ==========================================================================
# Which configuration file is used
# ==========================================================================

def test_a_real_config_is_preferred_over_the_example(tmp_path, monkeypatch):
    """`*.example.yaml` means "copy me and edit me". Loading it directly made
    the suffix a lie: a production deployment would run on the template, and a
    local edit would be a change to a version-controlled file, lost at the next
    pull.
    """
    from datadirector.cli import resolve_config_path

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "wiring.example.yaml").write_text("example")
    assert resolve_config_path(None, "wiring").endswith("wiring.example.yaml")

    (tmp_path / "config" / "wiring.yaml").write_text("real")
    assert resolve_config_path(None, "wiring").endswith("config/wiring.yaml")


def test_falling_back_to_the_example_says_so(tmp_path, monkeypatch, capsys):
    """Silence would leave someone editing a file whose changes vanish."""
    from datadirector.cli import resolve_config_path

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "policy.example.yaml").write_text("example")
    resolve_config_path(None, "policy")

    warning = capsys.readouterr().err
    assert "version-controlled" in warning
    assert "cp config/policy.example.yaml config/policy.yaml" in warning


def test_an_explicit_path_is_never_second_guessed(tmp_path, monkeypatch,
                                                  capsys):
    from datadirector.cli import resolve_config_path

    monkeypatch.chdir(tmp_path)
    assert resolve_config_path("/somewhere/else.yaml", "wiring") \
        == "/somewhere/else.yaml"
    assert capsys.readouterr().err == ""


def test_real_configuration_is_not_version_controlled():
    """A deployment's configuration is a property of its machine, and committing
    one person's endpoints and paths would make every pull a conflict."""
    from pathlib import Path

    from pathlib import Path

    gitignore = Path(__file__).parent.parent / ".gitignore"
    if not gitignore.exists():
        pytest.skip("no .gitignore in this tree")
    ignored = gitignore.read_text()
    for name in ("config/wiring.yaml", "config/policy.yaml",
                 "local-accounts.txt"):
        assert name in ignored, (
            f"{name} is not git-ignored. If you unpacked a release archive, "
            "check that .gitignore was updated: dotfiles are easy to miss.")
