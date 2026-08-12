# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.

"""
Utility functions for the handling of fonts
"""

import argparse
import ctypes
import json
import logging
import os
import shutil
import sys
import traceback
from ctypes import wintypes

try:
    import winreg
except ImportError:
    import _winreg as winreg

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

FONTS_REG_PATH = r"Software\Microsoft\Windows NT\CurrentVersion\Fonts"

HWND_BROADCAST = 0xFFFF
SMTO_ABORTIFHUNG = 0x0002
WM_FONTCHANGE = 0x001D
GFRI_DESCRIPTION = 1
GFRI_ISTRUETYPE = 3

INSTALL_SCOPE_USER = "USER"
INSTALL_SCOPE_SYSTEM = "SYSTEM"

FONT_LOCATION_SYSTEM = os.path.join(os.environ.get("SystemRoot"), "Fonts")
FONT_LOCATION_USER = os.path.join(os.environ.get("LocalAppData"), "Microsoft", "Windows", "Fonts")

# Font extensions supported in gdi32.AddFontResourceW
# OpenType fonts without an extension can also be installed (e.g. Adobe Fonts)
FONT_EXTENSIONS = [".otf", ".ttf", ".ttc", ".fon", ""]

# Keys of the session's path mapping rules file, which holds a "pathmapping-1.0" document
PATH_MAPPING_RULES_KEY = "path_mapping_rules"
DESTINATION_PATH_KEY = "destination_path"

# Name of the folder that the submitter gathers the project's fonts into
FONT_DIR_NAME = "tempFonts"

logger = logging.getLogger(__name__)


def get_job_file_roots(path_mapping_rules_file):
    """
    Reads the directories that the job's files were made available in out of the session's
    path mapping rules file.

    The Open Job Description session materializes its path mapping rules to a JSON file, and
    passes the location of that file to this script through the
    {{Session.PathMappingRulesFile}} template variable. Every rule's "destination_path" is a
    directory that the job's input files, fonts included, were downloaded to.

    :param path_mapping_rules_file: path of the session's path mapping rules file, or None
        when the session has no path mapping rules

    :returns: a list of the destination path of every path mapping rule
    """
    if not path_mapping_rules_file:
        logger.debug("The session has no path mapping rules, so the job has no fonts.")
        return []

    try:
        with open(path_mapping_rules_file, encoding="utf-8") as rules_file:
            rules_document = json.load(rules_file)
    except (OSError, ValueError) as e:
        # Fonts are an optional part of a job, so a rules file we can't use must not fail a render
        logger.warning(
            f"Couldn't read the path mapping rules file '{path_mapping_rules_file}', "
            f"so no fonts can be found: {e}"
        )
        return []

    if not isinstance(rules_document, dict):
        logger.warning(
            f"The path mapping rules file '{path_mapping_rules_file}' doesn't hold a JSON "
            "object, so no fonts can be found."
        )
        return []

    # The file holds an empty object when the session has no path mapping rules
    rules = rules_document.get(PATH_MAPPING_RULES_KEY, [])
    if not isinstance(rules, list):
        logger.warning(
            f"The '{PATH_MAPPING_RULES_KEY}' of the path mapping rules file "
            f"'{path_mapping_rules_file}' isn't a list, so no fonts can be found."
        )
        return []

    roots = []
    for rule in rules:
        destination = rule.get(DESTINATION_PATH_KEY) if isinstance(rule, dict) else None
        if not destination or not isinstance(destination, str):
            logger.warning(f"Skipping a path mapping rule without a destination path: {rule}")
            continue
        # The same destination can be the target of more than one rule
        if destination not in roots:
            roots.append(destination)
    return roots


def find_font_dirs(root_dir):
    """
    Looks for all font folders anywhere underneath a directory

    :param root_dir: the folder in which to look for font folders

    :returns: a list with all found font folders
    """
    font_dirs = []
    for path, dirs, _files in os.walk(root_dir):
        for d in dirs:
            if FONT_DIR_NAME in d:
                full_sub_dir = os.path.join(path, d)
                logger.debug(f"{FONT_DIR_NAME} directory: {full_sub_dir}")
                font_dirs.append(full_sub_dir)
    return font_dirs


def find_fonts(path_mapping_rules_file):
    """
    Looks for all font files that were sent along with the job

    :param path_mapping_rules_file: path of the session's path mapping rules file, or None
        when the session has no path mapping rules

    :returns: a set with all found fonts
    """
    fonts = set()
    for root_dir in get_job_file_roots(path_mapping_rules_file):
        if not os.path.isdir(root_dir):
            logger.debug(f"The job file root isn't an existing directory: {root_dir}")
            continue

        font_dirs = find_font_dirs(root_dir)
        if not font_dirs:
            logger.debug(f"Couldn't recursively find {FONT_DIR_NAME} in: {root_dir}")
            continue

        for font_dir in font_dirs:
            for file_name in os.listdir(font_dir):
                full_assetpath = os.path.join(font_dir, file_name)
                _, ext = os.path.splitext(full_assetpath)
                if ext.lower() in FONT_EXTENSIONS:
                    logger.debug(f"Adding: {full_assetpath}")
                    fonts.add(full_assetpath)
                else:
                    logger.warning(f"A file that is not a supported font was found in the {FONT_DIR_NAME} folder: {full_assetpath}")
    return fonts


def get_font_name(dst_path):
    """
    Get a font's Windows system name, which is the name stored in the registry.

    :param dst_path: path of font that needs to be named

    :returns: string with the font's name
    """
    try:
        filename = os.path.basename(dst_path)
        fontname = os.path.splitext(filename)[0]

        # Try to get the font's real name
        cb = wintypes.DWORD()
        if gdi32.GetFontResourceInfoW(filename, ctypes.byref(cb), None, GFRI_DESCRIPTION):
            buf = (ctypes.c_wchar * cb.value)()
            if gdi32.GetFontResourceInfoW(filename, ctypes.byref(cb), buf, GFRI_DESCRIPTION):
                fontname = buf.value
        is_truetype = wintypes.BOOL()
        cb.value = ctypes.sizeof(is_truetype)
        gdi32.GetFontResourceInfoW(
            filename, ctypes.byref(cb), ctypes.byref(is_truetype), GFRI_ISTRUETYPE
        )
        if is_truetype:
            fontname += " (TrueType)"

    except Exception as e:
        raise

    return fontname


def install_font(src_path, scope=INSTALL_SCOPE_USER):
    """
    Install provided font to the worker machine

    :param src_path: path of font that needs to be installed

    :returns: boolean that represents if the font was installed and a string with any traceback that was created
    """
    try:
        # Determine font destination
        if scope == INSTALL_SCOPE_SYSTEM:
            dst_dir = FONT_LOCATION_SYSTEM
            registry_scope = winreg.HKEY_LOCAL_MACHINE
        else:
            # Check if the Fonts folder exists, create it if it doesn't
            if not os.path.exists(FONT_LOCATION_USER):
                logger.info(f"Creating User Fonts folder: {FONT_LOCATION_USER}")
                os.makedirs(FONT_LOCATION_USER)

            dst_dir = FONT_LOCATION_USER
            registry_scope = winreg.HKEY_CURRENT_USER
        dst_path = os.path.join(dst_dir, os.path.basename(src_path))

        # Copy the font to the Windows Fonts folder
        shutil.copy(src_path, dst_path)

        # Load the font in the current session, remove font when loading fails
        if not gdi32.AddFontResourceW(dst_path):
            os.remove(dst_path)
            raise WindowsError(f'AddFontResource failed to load "{src_path}"')

        # Notify running programs
        user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_FONTCHANGE, 0, 0, SMTO_ABORTIFHUNG, 1000, None
        )

        # Store the fontname/filename in the registry
        filename = os.path.basename(dst_path)
        fontname = get_font_name(dst_path)

        # Creates registry if it doesn't exist, opens when it does exist
        with winreg.CreateKeyEx(registry_scope, FONTS_REG_PATH, 0, access= winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, fontname, 0, winreg.REG_SZ, filename)
    except Exception:
        return False, traceback.format_exc()
    return True, ""


def uninstall_font(src_path, scope=INSTALL_SCOPE_USER):
    """
    Uninstall provided font from the worker machine

    :param src_path: path of font that needs to be removed

    :returns: boolean that represents if the font was uninstalled and a string with any traceback that was created
    """
    try:
        # Determine where the font was installed
        if scope == INSTALL_SCOPE_SYSTEM:
            dst_path = os.path.join(FONT_LOCATION_SYSTEM, os.path.basename(src_path))
            registry_scope = winreg.HKEY_LOCAL_MACHINE
        else:
            dst_path = os.path.join(FONT_LOCATION_USER, os.path.basename(src_path))
            registry_scope = winreg.HKEY_CURRENT_USER

        # Remove the fontname/filename from the registry
        fontname = get_font_name(dst_path)

        with winreg.OpenKey(registry_scope, FONTS_REG_PATH, 0, access= winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, fontname)

        # Unload the font in the current session
        if not gdi32.RemoveFontResourceW(dst_path):
            os.remove(dst_path)
            raise WindowsError(f'RemoveFontResourceW failed to load "{src_path}"')

        if os.path.exists(dst_path):
            os.remove(dst_path)

        # Notify running programs
        user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_FONTCHANGE, 0, 0, SMTO_ABORTIFHUNG, 1000, None
        )
    except Exception:
        return False, traceback.format_exc()
    return True, ""


def _install_fonts(path_mapping_rules_file):
    """
    Calls all needed functions for installing fonts

    :param path_mapping_rules_file: path of the session's path mapping rules file, or None
        when the session has no path mapping rules
    """
    logger.info("Looking for fonts to install...")
    fonts = find_fonts(path_mapping_rules_file)

    if not fonts:
        logger.info("No custom fonts found, continuing task...")
        return
    for font in fonts:
        logger.info("Installing font: " + font)
        installed, msg = install_font(font)
        if not installed:
            raise RuntimeError(f"Error installing font: {msg}")


def _remove_fonts(path_mapping_rules_file):
    """
    Calls all needed functions for removing fonts

    :param path_mapping_rules_file: path of the session's path mapping rules file, or None
        when the session has no path mapping rules
    """
    logger.info("Looking for fonts to uninstall...")
    fonts = find_fonts(path_mapping_rules_file)

    if not fonts:
        logger.info("No custom fonts found, finishing task...")
        return

    for font in fonts:
        logger.info("Uninstalling font: " + font)
        removed, msg = uninstall_font(font)
        if not removed:
            # Don't fail task if font didn't get uninstalled
            logger.error(f"Error uninstalling font: {msg}")


def setup_logger():
    """
    Does a basic setup for a logger
    """
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(levelname)s:%(message)s')
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)


def parse_args(args):
    """
    Parses the command line arguments of this script

    :param args: the command line arguments, without the name of this script

    :returns: the parsed arguments
    """
    parser = argparse.ArgumentParser(description="Installs and removes a job's fonts.")
    parser.add_argument("action", choices=["install", "remove"], help="The action to run.")
    parser.add_argument(
        "--path-mapping-rules-file",
        default="",
        help="Location of the session's path mapping rules file, whose destination paths are "
        "the directories that the job's files were made available in. Provide it with the "
        "{{Session.PathMappingRulesFile}} template variable.",
    )
    parser.add_argument(
        "--has-path-mapping-rules",
        default="true",
        help="Whether the session has any path mapping rules, which is 'true' or 'false'. "
        "Provide it with the {{Session.HasPathMappingRules}} template variable.",
    )
    return parser.parse_args(args)


def get_rules_file_argument(parsed_args):
    """
    Determines which path mapping rules file, if any, the fonts should be looked for in

    :param parsed_args: the parsed command line arguments

    :returns: the location of the session's path mapping rules file, or None when the session
        has no path mapping rules
    """
    # A session without path mapping rules has no job files, and so no fonts, to look through.
    # Its rules file holds an empty document, and isn't guaranteed to have been written at all.
    if parsed_args.has_path_mapping_rules.strip().lower() != "true":
        return None
    return parsed_args.path_mapping_rules_file


if __name__ == "__main__":
    setup_logger()
    parsed_args = parse_args(sys.argv[1:])
    path_mapping_rules_file = get_rules_file_argument(parsed_args)

    logger.debug(f"Running font script job: {parsed_args.action}")

    if parsed_args.action == "install":
        _install_fonts(path_mapping_rules_file)
    if parsed_args.action == "remove":
        _remove_fonts(path_mapping_rules_file)