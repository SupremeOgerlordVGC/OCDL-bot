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

# Listing players by role needs Discord's privileged "Server Members
# Intent", because otherwise the bot cannot see who holds a role.
#
# This is OFF by default, and must stay off until you have enabled the
# intent in the Developer Portal: asking for an intent Discord has not
# approved makes it refuse the gateway connection over and over, so the bot
# never finishes starting. Everything except `players_role` works without it.
#
# To turn it on:
#   1. https://discord.com/developers/applications -> your app -> Bot ->
#      Privileged Gateway Intents -> enable "Server Members Intent" -> Save
#   2. put REQUEST_MEMBERS_INTENT=true in your .env
#   3. restart the bot
REQUEST_MEMBERS_INTENT = False

# --------------------------------------------------------------------------
# Draft rules
# --------------------------------------------------------------------------

# How much of a season the bot runs.
#   draft_only     - run the draft, then archive the league
#   draft_schedule - draft plus schedule posting, but no trades
#   full_season    - draft, trades and schedule posting
LEAGUE_MODES = ("draft_only", "draft_schedule", "full_season")

# Modes in which /postschedule works.
SCHEDULE_MODES = ("draft_schedule", "full_season")

MIN_PLAYERS = 2
# The spreadsheet has 20 coach rows and 20 roster blocks.
MAX_PLAYERS = 20

# Pool B's roster blocks start at this coach number, which is what caps a
# two-pool league at ten coaches per pool. One pool can use all 20.
POOL_B_FIRST_COACH = 11

# Most Tera Captains one team may have. Standard across drafts; there is no
# minimum. A captain is charged its Tera Tax against both budgets.
MAX_TERA_CAPTAINS = 3

# Longest queue a player may set up with /queuepick.
MAX_QUEUE_LENGTH = 5

# Most Pokemon that can appear on either side of a single /trade.
# A match may be put off by at most this many weeks, so an extension
# granted in week 3 moves the match to week 4 and no further.
# Leagues made by the bot's owner, or for this Discord server, are Ogre Clan
# Official: public, and the website switches Voice on for their players.
OGRE_CLAN_GUILD_ID = 1497084427749167127

# Week 1 goes up once every pool has drafted and its free-trade window has
# closed. Every week after that is posted on a Monday at 20:00 GMT+8, and
# never sooner than WEEK_GAP_DAYS after the previous one - so a season that
# starts mid-week does not get a short first week.
WEEK_POST_UTC_OFFSET_HOURS = 8
WEEK_POST_WEEKDAY = 0           # Monday, as date.weekday() counts
WEEK_POST_HOUR = 20
WEEK_GAP_DAYS = 7

MAX_EXTENSION_WEEKS = 1

MAX_TRADE_SIZE = 5

# When a full-season pool finishes drafting, every player is pinged and for
# this many hours trades are unlimited: ones proposed in this window don't
# count toward the league's limit on Pokémon picked up in trades.
FREE_TRADE_HOURS = 24

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
    # ---- The tiering list: names and prices. READ ONLY. ----
    # This is the one shape the bot reads costs from, and it is the same
    # whether it comes from a template or a standalone tiering document:
    #   column A = Pokemon name, B = price, C = Tera price, from row 2.
    # In a template, the TAB to read is named in the Settings cell below,
    # so a league can re-price by switching tiering tabs with no code change.
    "tiering_name_column": "A",
    "tiering_cost_column": "B",
    "tiering_tera_column": "C",
    "tiering_first_row": 2,

    # ---- Settings: the league's own configuration. READ ONLY. ----
    # Read through these named ranges rather than fixed cells, so the
    # template can move them around without breaking the bot.
    "settings_tab": "Settings",
    "named_ranges": {
        "budget": "Budget",
        "tera_budget": "TeraBudget",
        "pool_count": "NumPools",
        "tera_format": "TeraFormat",
        "tiering_sheet": "TieringSheet",
        "season_weeks": "SeasonWeeks",
    },

    # ---- Coaches: the player list. The bot WRITES usernames and pools. ----
    # Writing a username here updates it everywhere else in the document,
    # because every other tab references Coaches!B.
    "coaches_tab": "Coaches",
    "coaches_name_column": "B",
    "coaches_pool_column": "Y",
    # "In schedule" tick boxes. Only ticked rows take part in the draft, so
    # these decide which roster blocks the bot fills. READ ONLY.
    "coaches_in_draft_column": "AC",
    "coaches_first_row": 2,
    "coaches_last_row": 21,

    # ---- Rosters: the bot WRITES one column of Pokemon names per coach. ----
    # A Tera format uses the "Tera Rosters" tab instead, which is laid out
    # identically but adds a Tera Captain tick box.
    "rosters_tab": "Rosters",
    "tera_rosters_tab": "Tera Rosters",
    "roster_rows": 12,
    # Top-left pick cell of each coach's block, in coach order: index 0 is
    # coach 1. Coaches 1-10 sit under the "Pool A" heading, 11-20 under
    # "Pool B". The columns either side hold image and cost formulas that
    # must never be written to.
    "roster_blocks": [
        "C6",  "J6",  "Q6",  "X6",  "AE6",
        "C21", "J21", "Q21", "X21", "AE21",
        "C38", "J38", "Q38", "X38", "AE38",
        "C53", "J53", "Q53", "X53", "AE53",
    ],
    # The Tera Captain tick box sits this many columns right of the name.
    "tera_flag_offset": 3,

    # ---- Schedule: READ ONLY, for /postschedule. ----
    "schedule_tab": "BackendSchedule",
    "schedule_first_row": 4,
    "schedule_week_column": "A",
    "schedule_side_a_column": "E",
    "schedule_side_b_column": "F",
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


def get_ogreclan_api():
    """The port and shared secret of the private web API for draft.ogreclan.com (webapi.py), or None when it is off."""
    port = os.environ.get("OGRECLAN_API_PORT", "").strip()
    secret = os.environ.get("OGRECLAN_SECRET", "").strip()
    if not port.isdigit() or len(secret) < 24:
        return None
    return int(port), secret


def get_site_api():
    """draft.ogreclan.com's API, for reading kills and deaths from replay links (/reportreplay): its address
    (OGRECLAN_SITE_URL, on the server http://127.0.0.1:8100) and the shared secret, or None when it is off."""
    url = os.environ.get("OGRECLAN_SITE_URL", "").strip().rstrip("/")
    secret = os.environ.get("OGRECLAN_SECRET", "").strip()
    if not url or len(secret) < 24:
        return None
    return url, secret


def get_token():
    """Return the Discord bot token, or None if it has not been set."""
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    return token or None


def get_dev_guild_id():
    """Return the optional development guild id as an int, or None."""
    raw = os.environ.get("DEV_GUILD_ID", "").strip()
    return int(raw) if raw.isdigit() else None


def wants_members_intent():
    """Whether to ask Discord for the privileged members intent.

    The .env setting wins, so a server can turn it on without editing code.
    """
    raw = os.environ.get("REQUEST_MEMBERS_INTENT", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return REQUEST_MEMBERS_INTENT


def get_credentials_file():
    """Return the Google credentials path (env var overrides the default)."""
    return os.environ.get("GOOGLE_CREDENTIALS_FILE", "").strip() or GOOGLE_CREDENTIALS_FILE
