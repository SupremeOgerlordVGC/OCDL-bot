"""
Static checks on the Discord layer.

The cogs are the one part of the bot that unit tests cannot easily drive -
they need a live Discord connection. That makes them the place where a
rename slips through: Python only resolves `ui.something` when the line
actually runs, so a stale call sits there looking fine until a player
triggers it mid-draft.

These tests read the cogs as source and check every reference against the
real modules, which catches exactly that.
"""

import ast
import glob
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config        # noqa: E402
import database      # noqa: E402
import draft         # noqa: E402
import sheets        # noqa: E402
import timers        # noqa: E402
import ui            # noqa: E402

# How the cogs import our modules: `import draft as rules`, and so on.
ALIASES = {
    "ui": ui,
    "rules": draft,
    "draft": draft,
    "config": config,
    "database": database,
    "sheets": sheets,
    "timers": timers,
}

SOURCES = sorted(glob.glob(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cogs", "*.py"))) + [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), name)
    for name in ("bot.py", "ui.py")]


class ModuleReferenceTests(unittest.TestCase):
    def test_every_reference_exists(self):
        """No cog may call something that is not there any more.

        This caught ui.has_admin_rights surviving in two places after it was
        renamed to ui.is_moderator - the cog loaded fine and only failed when
        a player ran /pick.
        """
        missing = []
        for path in SOURCES:
            tree = ast.parse(open(path, encoding="utf-8").read())
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)):
                    continue
                module = ALIASES.get(node.value.id)
                if module is None:
                    continue
                if not hasattr(module, node.attr):
                    missing.append(
                        f"{os.path.basename(path)}:{node.lineno} "
                        f"{node.value.id}.{node.attr}")

        self.assertEqual(sorted(set(missing)), [],
                         "these refer to things that no longer exist")

    def test_sheet_layout_keys_all_resolve(self):
        """Every SHEET_LAYOUT key the code reads must actually be defined."""
        layout = config.SHEET_LAYOUT
        missing = []
        for path in SOURCES + [os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), name) for name in ("sheets.py", "draft.py")]:
            tree = ast.parse(open(path, encoding="utf-8").read())
            for node in ast.walk(tree):
                # matches config.SHEET_LAYOUT["key"] and layout["key"]
                if (isinstance(node, ast.Subscript)
                        and isinstance(node.slice, ast.Constant)
                        and isinstance(node.slice.value, str)):
                    target = node.value
                    name = None
                    if isinstance(target, ast.Attribute) and target.attr == "SHEET_LAYOUT":
                        name = node.slice.value
                    elif isinstance(target, ast.Name) and target.id == "layout":
                        name = node.slice.value
                    if name is not None and name not in layout:
                        missing.append(f"{os.path.basename(path)}:{node.lineno} "
                                       f"SHEET_LAYOUT[{name!r}]")
        self.assertEqual(sorted(set(missing)), [])


if __name__ == "__main__":
    unittest.main()
