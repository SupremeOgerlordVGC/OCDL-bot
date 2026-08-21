"""
Configuration and tunable settings for the draft bot.

Everything a league host might realistically want to change lives here,
so you should not have to hunt through the rest of the code.
"""

import os

# --------------------------------------------------------------------------
# Files and folders
# --------------------------------------------------------------------------

# Folder this file lives in. Using absolute paths means the bot works no
# matter which directory systemd (or you) started it from.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB_PATH = os.path.join(BASE_DIR, "draft.db")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")

# The bot backs itself up on this schedule and keeps the newest few files,
# so a VM that reboots overnight never costs you more than a few hours.
BACKUP_INTERVAL_HOURS = 6
BACKUPS_TO_KEEP = 40

# --------------------------------------------------------------------------
# Draft rules
# --------------------------------------------------------------------------

MIN_PLAYERS = 2
MAX_PLAYERS = 16

# Longest queue a player may set up with /queuepick.
MAX_QUEUE_LENGTH = 5

# Most Pokemon that can appear on either side of a single /trade.
MAX_TRADE_SIZE = 5

# The cheapest a Pokemon can possibly cost. Used to decide when a player can
# no longer afford anything at all, which is how "everyone is out of budget"
# is detected.
MINIMUM_PICK_COST = 1

# --------------------------------------------------------------------------
# Timer rules
# --------------------------------------------------------------------------

# Each time a player is skipped, their personal timer is divided by this
# number:  120 -> 60 -> 30 -> 15 ...
TIMER_DIVISOR = 2

# A personal timer never drops below this many seconds, no matter how many
# times the player has been skipped. Raise it if halving gets too brutal.
MINIMUM_TIMER_SECONDS = 15

# Sensible bounds for the base timer chosen during /setup.
MIN_BASE_TIMER_SECONDS = 30
MAX_BASE_TIMER_SECONDS = 60 * 60 * 24  # one day

# --------------------------------------------------------------------------
# Judgement calls (see README "Design decisions")
# --------------------------------------------------------------------------

# Should an approved trade refund the cost of dropped Pokemon and charge for
# received ones? Default True: the spreadsheet works out each coach's budget
# from whatever is currently on their roster, so the bot matches that.
TRADE_ADJUSTS_BUDGET = True

# A makeup pick owed for round R must be used before the player's turn in
# round R + 1. Miss that and the round R + 1 turn is skipped too.
#
# In a snake draft the player at a round boundary picks twice in a row
# (seat 16 picks last in round 1 and first in round 2), so that deadline can
# fall immediately after the skip that created it. Both rounds are lost, but
# the two of them together only ever cost ONE timer halving - see
# _expire_stale_makeups in draft.py.

# If True, a pick is rejected when it would leave the player without enough
# budget to still reach their minimum pick count (assuming 1 point per pick).
# Default False - most leagues treat the minimum as a soft target.
ENFORCE_MIN_PICK_BUDGET = False

# --------------------------------------------------------------------------
# Google Sheets (optional)
# --------------------------------------------------------------------------

# Path to a Google service-account JSON key file. If the file is missing,
# every sheet feature degrades gracefully and the draft carries on locally.
GOOGLE_CREDENTIALS_FILE = os.path.join(BASE_DIR, "credentials.json")

# Safety switch. While this is False the bot READS the sheet (for autocomplete,
# /search, /available and cost checks) but never writes to it.
#
# On, because the layout below has been checked against the real template and
# confirmed. Set it back to False for a dry run - everything else still works,
# the bot just stops writing.
SHEETS_WRITE_ENABLED = True

# ---- Spreadsheet layout -------------------------------------------------
# This describes the league template. The bot READS the Pokedex and WRITES
# only two things: coach usernames, and the Pokemon names in each roster
# block. Everything else in the document is formulas that react to those.
#
# Nothing is guessed at runtime: if a tab is missing the sheet layer says so
# rather than writing to the wrong place.
SHEET_LAYOUT = {
    # ---- Pokedex: the draft pool. READ ONLY. ----
    # Column E is the Pokemon name used in every command; column K is its
    # cost. Costs are read live, so a league that re-prices the template
    # works with no code changes.
    "pokedex_tab": "Pokédex",
    "pokedex_first_row": 2,
    "pokedex_name_column": "E",
    "pokedex_cost_column": "K",

    # ---- Coaches: player list. The bot WRITES column B. ----
    # Writing a username here updates it everywhere else in the document,
    # because every other tab references Coaches!B.
    "coaches_tab": "Coaches",
    "coaches_name_column": "B",
    "coaches_first_row": 2,
    "coaches_last_row": 17,

    # ---- Rosters: one block per coach. The bot WRITES the leftmost column. ----
    # Each entry is the top-left cell of a coach's block, in coach order:
    # index 0 is coach 1 (draft seat 1), index 15 is coach 16.
    # Each block is `roster_rows` tall and one row per draft round. The
    # cells are merged across four columns, so writing the leftmost one is
    # correct; the columns either side hold image and cost formulas that
    # must never be written to.
    "rosters_tab": "Rosters",
    "roster_rows": 12,
    "roster_blocks": [
        "C4",  "J4",  "Q4",  "X4",
        "C19", "J19", "Q19", "X19",
        "C34", "J34", "Q34", "X34",
        "C49", "J49", "Q49", "X49",
    ],
}

# A Pokemon is banned - not draftable - when its cost cell is blank, holds
# something that is not a number ("B", "Banned", "-", ...), or holds one of
# these placeholder numbers.
BANNED_COST_VALUES = {99, 999}

# --------------------------------------------------------------------------
# Environment loading
# --------------------------------------------------------------------------


def load_env(path=None):
    """Read simple KEY=VALUE lines from a .env file into os.environ.

    Written by hand so the bot has no extra dependency just to read one
    small file. Existing environment variables always win, which lets
    systemd or the shell override the file.
    """
    path = path or os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def get_token():
    """Return the Discord bot token, or None if it has not been set."""
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    return token or None


def get_dev_guild_id():
    """Return the optional development guild id as an int, or None."""
    raw = os.environ.get("DEV_GUILD_ID", "").strip()
    return int(raw) if raw.isdigit() else None


def get_credentials_file():
    """Return the Google credentials path (env var overrides the default)."""
    return os.environ.get("GOOGLE_CREDENTIALS_FILE", "").strip() or GOOGLE_CREDENTIALS_FILE
