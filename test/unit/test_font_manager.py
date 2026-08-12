# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Tests for the job's font_manager.py script, which finds the fonts that were submitted with a
job in the directories that the session's path mapping rules point at.
"""

import contextlib
import ctypes
import importlib.util
import json
import os
import platform
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

FONT_MANAGER_PATH = (
    Path(__file__).parent.parent.parent
    / "dist"
    / "DeadlineCloudSubmitter_Assets"
    / "JobTemplate"
    / "scripts"
    / "font_manager.py"
)

IS_WINDOWS = platform.system() == "Windows"


def _import_font_manager():
    """Imports the font_manager.py script, which is written for Windows workers, on any OS."""
    spec = importlib.util.spec_from_file_location("font_manager", FONT_MANAGER_PATH)
    module = importlib.util.module_from_spec(spec)
    with contextlib.ExitStack() as stack:
        if not IS_WINDOWS:
            # The script loads Windows-only modules, DLLs and environment variables on import
            wintypes = types.ModuleType("ctypes.wintypes")
            wintypes.DWORD = int
            wintypes.BOOL = int
            stack.enter_context(
                mock.patch.dict(
                    sys.modules,
                    {"ctypes.wintypes": wintypes, "winreg": types.ModuleType("winreg")},
                )
            )
            stack.enter_context(
                mock.patch.object(ctypes, "WinDLL", mock.MagicMock(), create=True)
            )
            stack.enter_context(
                mock.patch.dict(
                    os.environ, {"SystemRoot": os.getcwd(), "LocalAppData": os.getcwd()}
                )
            )
        spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def font_manager():
    return _import_font_manager()


def write_rules_file(directory, destinations, file_name="path_mapping_rules.json"):
    """
    Writes a path mapping rules file like the one an Open Job Description session materializes
    for the {{Session.PathMappingRulesFile}} template variable.
    """
    rules_file = Path(directory) / file_name
    rules_file.write_text(
        json.dumps(
            {
                "version": "pathmapping-1.0",
                "path_mapping_rules": [
                    {
                        "source_path_format": "windows",
                        "source_path": f"C:\\source\\{index}",
                        "destination_path": str(destination),
                    }
                    for index, destination in enumerate(destinations)
                ],
            }
        )
    )
    return str(rules_file)


def make_font_dir(root_dir, font_names, sub_dir="assets/inputs"):
    """Creates a tempFonts folder holding the given files underneath a job file root."""
    font_dir = Path(root_dir).joinpath(*sub_dir.split("/")) / "tempFonts"
    font_dir.mkdir(parents=True)
    for font_name in font_names:
        (font_dir / font_name).write_text("font")
    return font_dir


def test_finds_fonts_under_a_root(font_manager, tmp_path):
    """Fonts are found in a tempFonts folder underneath a rule's destination path."""
    root_dir = tmp_path / "root"
    font_dir = make_font_dir(root_dir, ["a.ttf", "b.OTF", "AdobeOpenTypeFont"])
    rules_file = write_rules_file(tmp_path, [root_dir])

    assert font_manager.find_fonts(rules_file) == {
        str(font_dir / "a.ttf"),
        str(font_dir / "b.OTF"),
        # Adobe's OpenType fonts have no extension
        str(font_dir / "AdobeOpenTypeFont"),
    }


def test_finds_fonts_under_every_root(font_manager, tmp_path):
    """A session has one path mapping rule per root, and all of them are searched."""
    project_root = tmp_path / "project_root"
    project_root.mkdir()
    fonts_root = tmp_path / "fonts_root"
    first_font_dir = make_font_dir(fonts_root, ["a.ttf"])
    other_fonts_root = tmp_path / "other_fonts_root"
    second_font_dir = make_font_dir(
        other_fonts_root, ["b.ttc"], sub_dir="deeply/nested/dir"
    )
    rules_file = write_rules_file(
        tmp_path, [project_root, fonts_root, other_fonts_root]
    )

    assert font_manager.find_fonts(rules_file) == {
        str(first_font_dir / "a.ttf"),
        str(second_font_dir / "b.ttc"),
    }


def test_finds_fonts_in_every_font_dir_under_a_root(font_manager, tmp_path):
    """Every tempFonts folder underneath a root is searched, not just the first one found."""
    root_dir = tmp_path / "root"
    first_font_dir = make_font_dir(root_dir, ["a.ttf"], sub_dir="first")
    second_font_dir = make_font_dir(root_dir, ["b.otf"], sub_dir="second/nested")
    rules_file = write_rules_file(tmp_path, [root_dir])

    assert font_manager.find_fonts(rules_file) == {
        str(first_font_dir / "a.ttf"),
        str(second_font_dir / "b.otf"),
    }


def test_no_fonts_without_a_font_dir(font_manager, tmp_path):
    """A root without a tempFonts folder in it contributes no fonts."""
    root_dir = tmp_path / "root"
    (root_dir / "assets").mkdir(parents=True)
    (root_dir / "assets" / "project.aep").write_text("project")
    rules_file = write_rules_file(tmp_path, [root_dir])

    assert font_manager.find_fonts(rules_file) == set()


def test_unsupported_files_are_not_fonts(font_manager, tmp_path, caplog):
    """Files in a tempFonts folder that can't be installed are reported and skipped."""
    root_dir = tmp_path / "root"
    font_dir = make_font_dir(root_dir, ["a.ttf", "notes.txt"])
    rules_file = write_rules_file(tmp_path, [root_dir])

    assert font_manager.find_fonts(rules_file) == {str(font_dir / "a.ttf")}
    assert "notes.txt" in caplog.text


def test_no_path_mapping_rules(font_manager, tmp_path):
    """A session without path mapping rules materializes an empty rules document."""
    rules_file = tmp_path / "rules.json"
    rules_file.write_text(json.dumps({}))

    assert font_manager.find_fonts(str(rules_file)) == set()


def test_rules_file_not_provided(font_manager):
    """No rules file at all, which is what an unset {{Session.HasPathMappingRules}} means."""
    assert font_manager.find_fonts(None) == set()
    assert font_manager.find_fonts("") == set()


def test_missing_rules_file(font_manager, tmp_path, caplog):
    """A rules file that isn't there is reported, and doesn't fail the action."""
    missing_rules_file = str(tmp_path / "does_not_exist.json")

    assert font_manager.find_fonts(missing_rules_file) == set()
    assert missing_rules_file in caplog.text


def test_malformed_rules_file(font_manager, tmp_path, caplog):
    """A rules file that isn't valid JSON is reported, and doesn't fail the action."""
    rules_file = tmp_path / "rules.json"
    rules_file.write_text('{"path_mapping_rules": [')

    assert font_manager.find_fonts(str(rules_file)) == set()
    assert str(rules_file) in caplog.text


def test_rules_file_that_is_not_an_object(font_manager, tmp_path, caplog):
    """A rules file holding something other than a JSON object is reported and skipped."""
    rules_file = tmp_path / "rules.json"
    rules_file.write_text(json.dumps(["not", "an", "object"]))

    assert font_manager.find_fonts(str(rules_file)) == set()
    assert str(rules_file) in caplog.text


def test_rules_that_are_not_a_list(font_manager, tmp_path, caplog):
    """Path mapping rules that aren't a list are reported and skipped."""
    rules_file = tmp_path / "rules.json"
    rules_file.write_text(
        json.dumps({"path_mapping_rules": {"destination_path": "C:\\dest"}})
    )

    assert font_manager.find_fonts(str(rules_file)) == set()
    assert str(rules_file) in caplog.text


def test_rule_without_a_destination_path(font_manager, tmp_path, caplog):
    """A rule that has no destination path is skipped, and the other rules are still used."""
    root_dir = tmp_path / "root"
    font_dir = make_font_dir(root_dir, ["a.ttf"])
    rules_file = tmp_path / "rules.json"
    rules_file.write_text(
        json.dumps(
            {
                "version": "pathmapping-1.0",
                "path_mapping_rules": [
                    {"source_path_format": "windows", "source_path": "C:\\source"},
                    "not a rule",
                    {"destination_path": str(root_dir)},
                ],
            }
        )
    )

    assert font_manager.find_fonts(str(rules_file)) == {str(font_dir / "a.ttf")}
    assert "destination path" in caplog.text


def test_destination_path_that_was_not_downloaded(font_manager, tmp_path):
    """A destination path that isn't an existing directory is skipped."""
    rules_file = write_rules_file(tmp_path, [tmp_path / "never_downloaded"])

    assert font_manager.find_fonts(rules_file) == set()


def test_duplicated_destination_paths(font_manager, tmp_path):
    """The same destination path in more than one rule is only searched once."""
    root_dir = tmp_path / "root"
    font_dir = make_font_dir(root_dir, ["a.ttf"])
    rules_file = write_rules_file(tmp_path, [root_dir, root_dir])

    assert font_manager.get_job_file_roots(rules_file) == [str(root_dir)]
    assert font_manager.find_fonts(rules_file) == {str(font_dir / "a.ttf")}


@pytest.mark.parametrize("action", ["install", "remove"])
def test_parse_args(font_manager, action):
    """The arguments that the job template provides are parsed into the rules file to use."""
    parsed_args = font_manager.parse_args(
        [
            action,
            "--has-path-mapping-rules",
            "true",
            "--path-mapping-rules-file",
            "C:\\session\\rules.json",
        ]
    )

    assert parsed_args.action == action
    assert (
        font_manager.get_rules_file_argument(parsed_args) == "C:\\session\\rules.json"
    )


def test_parse_args_without_path_mapping_rules(font_manager):
    """{{Session.HasPathMappingRules}} being false means there are no fonts to look for."""
    parsed_args = font_manager.parse_args(
        [
            "install",
            "--has-path-mapping-rules",
            "false",
            "--path-mapping-rules-file",
            "C:\\session\\rules.json",
        ]
    )

    assert font_manager.get_rules_file_argument(parsed_args) is None
