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


class CommandOptionTests(unittest.TestCase):
    """What each command actually offers in Discord.

    A missing optional parameter is invisible: the cog imports, the bot
    starts, and the option simply is not there. /pick and /editpick shipped
    without tera_captain this way, which made Tera Captains unusable even
    though the rules supported them and were tested.
    """

    EXPECTED = {
        "pick": {"round", "pokemon", "cost", "tera_captain"},
        "makeuppick": {"round", "pokemon", "cost", "tera_captain"},
        "editpick": {"round", "pokemon", "cost", "tera_captain", "player"},
        "teracaptain": {"pokemon", "on", "for_player"},
        "queuepick": {"round", "pokemon", "costs", "tera"},
        "cancelqueue": {"round", "player"},
        # No player option on purpose: a declared option is visible to
        # everybody, which would advertise that queues can be read.
        "myqueue": {"round"},
        "draft": {"at"},
        "setup": {"name", "draft_channel", "players", "players_role", "min_picks",
                  "max_picks", "timer", "mode", "budget", "tera_budget", "max_trades",
                  "best_of", "template_link", "tiering_link", "draft_channel_b", "players_b",
                  "schedule_channel",
                  "players_role_b", "weeks", "visibility", "website"},
        "website": {"name", "on"},
        "visibility": {"name", "setting"},
        "deleteleague": {"name", "confirm"},
        "reportgame": {"week", "game", "kd", "winner", "name"},
        "reportreplay": {"week", "game", "replay", "name"},
        "champions": {"week", "agree", "name"},
        "pokepaste": {"week", "url", "name"},
        "matchresult": {"week", "player", "name"},
        "cleargame": {"week", "game", "player", "name"},
        "setresult": {"week", "player", "reason", "clear", "name"},
        "syncmatch": {"week", "player", "name"},
        "removeplayer": {"player", "keep_roster"},
        "setskips": {"player", "skips", "timer"},
        "skipturn": {"player", "penalty"},
        "reopendrafting": {"player"},
        # No options at all: it is always your own draft, and the
        # confirmation happens on a button rather than a flag.
        "finishdrafting": set(),
        "substitute": {"out", "into"},
        "trade": {"drops", "gets", "partner", "tera_on", "tera_off", "for_player"},
        "approvetrade": {"trade_id", "name"},
        "unapprovetrade": {"trade_id", "name"},
        "edittrade": {"trade_id", "drops", "gets", "name"},
        "updatetrades": {"player", "pool", "name"},
        "edit": {"budget", "tera_budget", "min_picks", "max_picks", "max_trades",
                 "best_of", "document_link"},
        "postschedule": {"week", "name"},
        "extension": {"reason"},
        "grantextension": {"number", "player", "name"},
        "rejectextension": {"number", "name"},
        "extensions": {"name"},
        "schedulechannel": {"name", "channel"},
        "hostapplications": {"waiting"},
        "approvehost": {"user"},
        "rejecthost": {"user", "reason"},
        "roster": {"player"},
        "progress": {"round"},
        "available": {"cost", "tera"},
        "search": {"pokemon"},
    }

    def setUp(self):
        import asyncio, tempfile, os as _os
        import config as _config
        handle, self.path = tempfile.mkstemp(suffix=".db")
        _os.close(handle)
        _config.DB_PATH = self.path

        import bot as botmod
        self.bot = botmod.DraftBot(members_intent=False)

        async def load():
            for extension in botmod.COGS:
                await self.bot.load_extension(extension)
        asyncio.run(load())
        self.commands = {c.name: c for c in self.bot.tree.get_commands()}

    def tearDown(self):
        import asyncio, os as _os
        asyncio.run(self.bot.close())
        for suffix in ("", "-wal", "-shm"):
            if _os.path.exists(self.path + suffix):
                _os.remove(self.path + suffix)

    def test_every_command_offers_the_options_it_should(self):
        for name, expected in self.EXPECTED.items():
            with self.subTest(command=name):
                self.assertIn(name, self.commands, f"/{name} is not registered")
                actual = {p.name for p in self.commands[name].parameters}
                self.assertEqual(actual, expected,
                                 f"/{name} options do not match what the bot intends")

    def test_everything_is_documented(self):
        for command in self.commands.values():
            self.assertTrue(command.description, f"/{command.name} has no description")
            for param in command.parameters:
                self.assertTrue(param.description,
                                f"/{command.name} {param.name} has no description")


class ErrorReportingTests(unittest.TestCase):
    """What the user is told when a command cannot even be parsed.

    Discord rejects a name typed into a player: option before the command
    runs, which used to surface as "Something went wrong". Somebody leaving
    the server mid-season makes that a normal thing to hit.
    """

    def setUp(self):
        import asyncio, tempfile, os as _os
        import config as _config
        handle, self.path = tempfile.mkstemp(suffix=".db")
        _os.close(handle)
        _config.DB_PATH = self.path
        import bot as botmod
        self.bot = botmod.DraftBot(members_intent=False)

    def tearDown(self):
        import asyncio, os as _os
        asyncio.run(self.bot.close())
        for suffix in ("", "-wal", "-shm"):
            if _os.path.exists(self.path + suffix):
                _os.remove(self.path + suffix)

    def report(self, error):
        import asyncio

        sent = []

        class Response:
            def is_done(self):
                return False

            async def send_message(self, content, ephemeral=False):
                sent.append(content)

        class Interaction:
            response = Response()

        asyncio.run(self.bot.on_app_command_error(Interaction(), error))
        return sent[0]

    def test_an_unknown_member_is_named_rather_than_hidden(self):
        import discord
        from discord import app_commands

        class Transformer:
            _error_display_name = "Member"

        message = self.report(app_commands.TransformerError(
            "philomelee", discord.AppCommandOptionType.user, Transformer()))
        self.assertIn("philomelee", message)
        self.assertNotIn("Something went wrong", message)

    def test_a_rule_violation_is_passed_through_unchanged(self):
        import draft as rules
        self.assertEqual(self.report(rules.DraftError("❌ It is not your turn.")),
                         "❌ It is not your turn.")

    def test_anything_unexpected_still_says_the_draft_is_safe(self):
        message = self.report(RuntimeError("boom"))
        self.assertIn("has not been changed", message)


if __name__ == "__main__":
    unittest.main()
