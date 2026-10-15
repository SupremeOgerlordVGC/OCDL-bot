"""
Optional Google Sheets integration for the league template.

What the bot reads
------------------
* **Pokédex** - column E is the Pokémon name used in every command, column K
  is its cost. Costs are read live, so a league that re-prices the template
  works with no code changes. A Pokémon is banned when its cost cell is
  blank, holds text ("B", "Banned", "-", ...) or holds a placeholder number
  such as 99 or 999.

The Tier List tab is deliberately *not* read: it is generated from the same
Pokédex costs, so reading both could only ever produce disagreement.

What the bot writes
-------------------
* **Coaches!B2:B17** - the player usernames, in draft order. Every other tab
  in the document refers back to these cells, so writing here updates coach
  names everywhere.
* **Rosters** - the leftmost column of each coach's block, one row per round.
  The cells either side hold image and cost formulas driven by that name, so
  writing the name alone fills in the rest.

Nothing else in the document is touched.

Rules this module follows
-------------------------
* The local database is the source of truth; this only ever pushes outward.
* Every failure is caught and reported. A Google outage must never stop a
  draft, and never rewrite good local data with bad remote data.
* Nothing about the layout is guessed. Tabs and columns come from
  config.SHEET_LAYOUT, and anything missing is an explicit error naming what
  was expected and what was actually found.
* Writing stays off until config.SHEETS_WRITE_ENABLED is turned on.
"""

import logging
import os
import re
import time

import config

log = logging.getLogger("draftbot.sheets")

# gspread is optional. Without it the bot simply runs with sheets disabled.
try:
    import gspread
    GSPREAD_AVAILABLE = True
except ImportError:                                  # pragma: no cover
    gspread = None
    GSPREAD_AVAILABLE = False


class SheetError(Exception):
    """Something is wrong with the sheet, its layout, or the connection."""


def _as_sheet_error(error, doing):
    """Turn a Google API failure into advice, not a traceback.

    Most of these are things the league host can fix in thirty seconds -
    a protected range, a sheet not shared with the bot - so they deserve a
    message saying which, rather than surfacing as "something went wrong".
    """
    if isinstance(error, SheetError):
        return error

    text = str(error)
    if "protected cell" in text or "protected range" in text:
        return SheetError(
            f"those cells are **protected**, so I am not allowed to change them "
            f"({doing}).\n"
            f"On the sheet: right-click the tab → **Protect sheet**, or "
            f"**Data → Protected sheets and ranges**, and either remove the "
            f"protection or add this bot's service-account email as an editor of "
            f"that range."
        )
    if "PERMISSION_DENIED" in text or "[403]" in text:
        return SheetError(
            f"I do not have permission ({doing}). Share the document with the "
            f"bot's service-account email as an **Editor**."
        )
    if "[429]" in text or "RESOURCE_EXHAUSTED" in text or "Quota" in text:
        return SheetError(
            f"Google is rate limiting us ({doing}). Wait a minute and try again - "
            f"nothing local was changed."
        )
    return SheetError(f"Google refused the request while {doing}: {text}")


def extract_key(url_or_key):
    """Pull the spreadsheet id out of a full Google Sheets URL."""
    if not url_or_key:
        return None
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url_or_key)
    return match.group(1) if match else url_or_key.strip()


def normalize(name):
    return " ".join(str(name).strip().lower().split())


# Characters to ignore when reading a cost: the cost column is formatted as
# currency, so a league that types values as text can produce "$12" or
# "1,200". Only these are stripped - a marker like "B" or "Banned" still has
# no number left in it and so still counts as banned.
COST_NOISE = str.maketrans("", "", "$£€¥, \t")


def _is_ticked(value):
    """Whether a spreadsheet tick box counts as checked."""
    if isinstance(value, bool):
        return value
    return str(value).strip().upper() in ("TRUE", "YES", "Y", "1", "✓", "X")


def parse_cost(raw):
    """Cost as an int, or None when the Pokémon is banned or unpriced.

    Blank, anything without a number in it, and the placeholder numbers in
    config.BANNED_COST_VALUES all mean "not draftable".
    """
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return None
    try:
        value = int(float(text.translate(COST_NOISE)))
    except ValueError:
        return None
    if value in config.BANNED_COST_VALUES:
        return None
    return value


def column_number(letters):
    """'A' -> 1, 'Z' -> 26, 'AA' -> 27."""
    total = 0
    for char in letters.upper():
        total = total * 26 + (ord(char) - ord("A") + 1)
    return total


def column_letters(number):
    """1 -> 'A', 27 -> 'AA'. The inverse of column_number."""
    letters = ""
    while number > 0:
        number, remainder = divmod(number - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def shift_column(reference, columns):
    """Move a cell reference sideways: shift_column('C6', 3) -> 'F6'."""
    letters, row = split_cell(reference)
    return f"{column_letters(column_number(letters) + columns)}{row}"


def split_cell(reference):
    """'C4' -> ('C', 4)."""
    match = re.fullmatch(r"([A-Za-z]+)(\d+)", reference.strip())
    if not match:
        raise SheetError(f"`{reference}` is not a valid cell reference.")
    return match.group(1).upper(), int(match.group(2))


class SheetSync:
    """Thin wrapper around one service-account connection."""

    # How long the Pokédex may be reused before it is fetched again. Keeps
    # the bot well inside Google's rate limits during a fast draft, and makes
    # slash-command autocomplete instant.
    CACHE_SECONDS = 60

    def __init__(self, credentials_file=None):
        self.credentials_file = credentials_file or config.get_credentials_file()
        self.client = None
        self.reason = None            # why it is disabled, shown to admins
        self._books = {}              # spreadsheet key -> gspread Spreadsheet
        self._pool_cache = {}         # spreadsheet key -> (timestamp, entries)

        if not GSPREAD_AVAILABLE:
            self.reason = "the `gspread` package is not installed (`pip install gspread google-auth`)"
        elif not os.path.exists(self.credentials_file):
            self.reason = f"no service-account key found at `{self.credentials_file}`"
        else:
            try:
                self.client = gspread.service_account(filename=self.credentials_file)
            except Exception as error:               # pragma: no cover
                self.reason = f"could not sign in to Google: {error}"
                log.exception("Google sign-in failed")

    @property
    def enabled(self):
        return self.client is not None

    @property
    def can_write(self):
        return self.enabled and config.SHEETS_WRITE_ENABLED

    # ---- low level helpers ---------------------------------------------

    def _book(self, doc_url):
        """Open a spreadsheet, reusing the handle between calls."""
        if not self.enabled:
            raise SheetError(f"Google Sheets is not available: {self.reason}")

        key = extract_key(doc_url)
        if not key:
            raise SheetError("This draft has no Google Sheet configured.")

        if key not in self._books:
            try:
                self._books[key] = self.client.open_by_key(key)
            except Exception as error:
                raise SheetError(
                    f"Could not open the spreadsheet: {error}. Check the link, and make sure "
                    f"the sheet is shared with the service account email."
                ) from error
        return self._books[key]

    def _tab(self, doc_url, tab_name):
        book = self._book(doc_url)
        try:
            return book.worksheet(tab_name)
        except Exception as error:
            available = ", ".join(w.title for w in book.worksheets())
            raise SheetError(
                f"Tab **{tab_name}** was not found. The spreadsheet has: {available}. "
                f"Fix the tab names in `config.SHEET_LAYOUT`."
            ) from error

    def forget_cache(self, doc_url):
        key = extract_key(doc_url)
        for cached in [k for k in self._pool_cache if k[0] == key]:
            self._pool_cache.pop(cached, None)

    # ---- reading the Settings tab --------------------------------------

    def _named_ranges(self, doc_url):
        """{named range: 'Tab'!A1} for the document.

        Reading settings through the document's own named ranges rather than
        fixed cells means the template can move them around without
        breaking the bot.
        """
        book = self._book(doc_url)
        meta = book.fetch_sheet_metadata()
        titles = {sheet["properties"]["sheetId"]: sheet["properties"]["title"]
                  for sheet in meta.get("sheets", [])}

        found = {}
        for entry in meta.get("namedRanges", []):
            name = entry.get("name")
            spec = entry.get("range", {})
            title = titles.get(spec.get("sheetId", 0))
            if not name or title is None:
                continue
            column = column_letters(spec.get("startColumnIndex", 0) + 1)
            row = spec.get("startRowIndex", 0) + 1
            found[name] = f"'{title}'!{column}{row}"
        return found

    def read_settings(self, doc_url):
        """The league settings the template declares, as plain Python values.

        Returns whatever it can find, keyed by the short names in
        config.SHEET_LAYOUT["named_ranges"]. A missing range is simply
        absent rather than an error, so an older template still works.
        """
        wanted = config.SHEET_LAYOUT["named_ranges"]
        ranges = self._named_ranges(doc_url)

        lookup = {key: ranges[name] for key, name in wanted.items() if name in ranges}
        if not lookup:
            raise SheetError(
                "This document has no named ranges, so its Settings tab cannot be read. "
                f"Expected: {', '.join(wanted.values())}."
            )

        book = self._book(doc_url)
        keys = list(lookup)
        try:
            response = book.values_batch_get(
                [lookup[key] for key in keys],
                params={"valueRenderOption": "UNFORMATTED_VALUE"},
            )
        except Exception as error:
            raise SheetError(f"Could not read the Settings tab: {error}") from error

        settings = {}
        for key, block in zip(keys, response.get("valueRanges", [])):
            values = block.get("values") or [[]]
            settings[key] = values[0][0] if values[0] else None
        return settings

    def read_coach_slots(self, doc_url):
        """Which coach rows are actually in the draft, grouped by pool.

        A coach counts only when their "In schedule" box is ticked, so the
        ticked rows are exactly the roster blocks the bot should fill. Gaps
        are normal and must be respected: with rows 5 and 10 unticked, eight
        pool A players belong in blocks 1-4 and 6-9, not 1-8.

        Returns {"A": [coach numbers], "B": [...]} plus the names currently
        in those rows, so /setup can report what it is about to overwrite.
        """
        layout = config.SHEET_LAYOUT
        first, last = layout["coaches_first_row"], layout["coaches_last_row"]
        tab = self._tab(doc_url, layout["coaches_tab"])

        name_col = layout["coaches_name_column"]
        pool_col = layout["coaches_pool_column"]
        flag_col = layout["coaches_in_draft_column"]

        try:
            names, pools, flags = tab.batch_get(
                [f"{name_col}{first}:{name_col}{last}",
                 f"{pool_col}{first}:{pool_col}{last}",
                 f"{flag_col}{first}:{flag_col}{last}"],
                value_render_option="UNFORMATTED_VALUE",
            )
        except Exception as error:
            raise SheetError(f"Could not read the {layout['coaches_tab']} tab: {error}") from error

        def cell(block, index):
            return block[index][0] if index < len(block) and block[index] else ""

        slots = {"A": [], "B": []}
        current = {}
        for offset in range(last - first + 1):
            coach_number = offset + 1
            if not _is_ticked(cell(flags, offset)):
                continue
            label = str(cell(pools, offset)).strip().upper()
            # Coaches!Z treats anything that is not "B" as pool A, so the bot
            # matches that rather than inventing a third pool.
            slots["B" if label == "B" else "A"].append(coach_number)
            current[coach_number] = str(cell(names, offset)).strip()

        if not slots["A"] and not slots["B"]:
            raise SheetError(
                f"No coach is marked as taking part. Tick the "
                f"**{flag_col}** (\"In schedule\") boxes on the "
                f"{layout['coaches_tab']} tab for the coaches who are drafting."
            )
        return slots, current

    def tiering_tab(self, doc_url):
        """Which tab of a template holds the price list, per its Settings."""
        name = self.read_settings(doc_url).get("tiering_sheet")
        if not name:
            raise SheetError(
                "The template does not say which tiering tab to use. Set the "
                "`TieringSheet` cell on its Settings tab."
            )
        return str(name).strip()

    # ---- reading the price list ----------------------------------------

    def pool(self, doc_url, tab=None, force=False):
        """Every Pokemon in the price list.

        Reads the one shape the bot understands - column A name, B price,
        C Tera price, from row 2 - which is the same whether it comes from a
        template's tiering tab or a standalone tiering document.

        Returns a list of {key, name, cost, tera_cost, banned, tera_banned,
        row}. `cost` is None for a Pokemon that cannot be drafted at all;
        `tera_cost` is None for one that cannot be a Tera Captain.
        """
        layout = config.SHEET_LAYOUT
        cache_key = (extract_key(doc_url), tab)
        cached = self._pool_cache.get(cache_key)
        if cached and not force and time.time() - cached[0] < self.CACHE_SECONDS:
            return cached[1]

        first = layout["tiering_first_row"]
        name_col = layout["tiering_name_column"]
        cost_col = layout["tiering_cost_column"]
        tera_col = layout["tiering_tera_column"]

        worksheet = self._tab(doc_url, tab) if tab else self._first_tab(doc_url)
        try:
            # UNFORMATTED_VALUE matters: a price column formatted as currency
            # would otherwise come back as "$12" instead of 12.
            names, costs, teras = worksheet.batch_get(
                [f"{name_col}{first}:{name_col}",
                 f"{cost_col}{first}:{cost_col}",
                 f"{tera_col}{first}:{tera_col}"],
                value_render_option="UNFORMATTED_VALUE",
            )
        except Exception as error:
            raise SheetError(f"Could not read the price list: {error}") from error

        def cell(block, index):
            return block[index][0] if index < len(block) and block[index] else ""

        entries = []
        for offset in range(len(names)):
            name = str(cell(names, offset)).strip()
            if not name:
                continue
            cost = parse_cost(cell(costs, offset))
            tera_cost = parse_cost(cell(teras, offset))
            entries.append({
                "key": normalize(name),
                "name": name,
                "cost": cost,
                "tera_cost": tera_cost,
                "banned": cost is None,
                "tera_banned": tera_cost is None,
                "row": first + offset,
            })

        if not entries:
            raise SheetError(
                f"No Pokemon names found in column {name_col} from row {first}. "
                f"A tiering list needs names in A, prices in B and Tera prices in C."
            )

        self._pool_cache[cache_key] = (time.time(), entries)
        return entries

    def _first_tab(self, doc_url):
        """The only sheet of a standalone tiering document."""
        return self._book(doc_url).get_worksheet(0)

    def lookup(self, doc_url, pokemon, tab=None):
        """Find one Pokémon in the price list, or None."""
        wanted = normalize(pokemon)
        for entry in self.pool(doc_url, tab=tab):
            if entry["key"] == wanted:
                return entry
        return None

    def at_cost(self, doc_url, cost, taken_keys, tab=None, tera=False):
        """Undrafted, draftable Pokémon at exactly this price.

        With `tera` the Tera price is used instead, and anything that cannot
        be a Tera Captain is left out.
        """
        field = "tera_cost" if tera else "cost"
        return [entry for entry in self.pool(doc_url, tab=tab)
                if entry[field] == cost and entry["key"] not in taken_keys]

    def suggest(self, doc_url, text, taken_keys, limit=25, tab=None, tera=False):
        """Autocomplete matches: undrafted, draftable, name contains `text`.

        With `tera` only Pokémon that may be Tera Captains are offered.
        """
        wanted = normalize(text)
        starts, contains = [], []
        for entry in self.pool(doc_url, tab=tab):
            if entry["banned"] or entry["key"] in taken_keys:
                continue
            if tera and entry["tera_banned"]:
                continue
            if not wanted:
                starts.append(entry)
            elif entry["key"].startswith(wanted):
                starts.append(entry)
            elif wanted in entry["key"]:
                contains.append(entry)
            if len(starts) >= limit and not wanted:
                break
        return (starts + contains)[:limit]

    # ---- writing --------------------------------------------------------

    def roster_tab_name(self, tera_format):
        """Which tab holds the rosters: a Tera format uses its own."""
        layout = config.SHEET_LAYOUT
        return layout["tera_rosters_tab"] if tera_format else layout["rosters_tab"]

    def write_pick(self, doc_url, tera_format, coach_number, round_number,
                   pokemon, tera_captain=False):
        """Write one Pokemon into one coach's roster block.

        Called from a background thread with plain values only - never a
        database handle. The pick is already saved locally, so a failure
        here is logged and ignored.
        """
        if not self.can_write:
            return False

        tab = self._tab(doc_url, self.roster_tab_name(tera_format))
        updates = [{"range": self._roster_cell(coach_number, round_number),
                    "values": [[pokemon]]}]
        if tera_format:
            # The tick box has to be set either way round, so that demoting a
            # captain with /editpick clears it again.
            updates.append({"range": self._tera_cell(coach_number, round_number),
                            "values": [[bool(tera_captain)]]})
        tab.batch_update(updates, value_input_option="USER_ENTERED")
        return True

    @staticmethod
    def _roster_cell(draft_position, round_number):
        """Which cell holds this coach's pick for this round."""
        layout = config.SHEET_LAYOUT
        blocks = layout["roster_blocks"]
        rows = layout["roster_rows"]

        if not 1 <= draft_position <= len(blocks):
            raise SheetError(
                f"Coach number {draft_position} has no roster block. The template "
                f"has {len(blocks)} of them."
            )
        if not 1 <= round_number <= rows:
            raise SheetError(
                f"Round {round_number} does not fit: each roster block on the sheet "
                f"holds {rows} rounds."
            )

        column, first_row = split_cell(blocks[draft_position - 1])
        return f"{column}{first_row + round_number - 1}"

    @staticmethod
    def _tera_cell(draft_position, round_number):
        """The Tera Captain tick box beside a pick.

        It sits a fixed number of columns right of the name - clear of the
        image formula on one side and the cost formula on the other.
        """
        return shift_column(SheetSync._roster_cell(draft_position, round_number),
                            config.SHEET_LAYOUT["tera_flag_offset"])

    def write_coaches(self, doc_url, coaches, manage_rows=None):
        """Write usernames and pool letters to the Coaches tab.

        `coaches` is {coach_number: (display_name, pool_label)}.

        Only rows the bot is responsible for are touched, and only cells
        whose value would actually change. That matters for two reasons: a
        matched player has already adopted the sheet's own spelling, so
        writing it back is a no-op, and league hosts often protect the
        Coaches tab - so a pointless write is the difference between /sync
        working and failing outright.

        `manage_rows` is the set of coach numbers it may write, normally the
        ticked rows of the pools in this league. A managed row with no player
        is cleared, so removing somebody does not leave their name behind.
        Every other row, including every unticked one, is left alone.
        """
        layout = config.SHEET_LAYOUT
        first, last = layout["coaches_first_row"], layout["coaches_last_row"]
        name_col = layout["coaches_name_column"]
        pool_col = layout["coaches_pool_column"]

        rows = sorted(set(manage_rows) if manage_rows else set(coaches))
        if not rows:
            return 0

        tab = self._tab(doc_url, layout["coaches_tab"])
        try:
            current_names, current_pools = tab.batch_get(
                [f"{name_col}{first}:{name_col}{last}",
                 f"{pool_col}{first}:{pool_col}{last}"],
                value_render_option="UNFORMATTED_VALUE",
            )
        except Exception as error:
            raise _as_sheet_error(error, f"reading the {layout['coaches_tab']} tab")

        def existing(block, coach_number):
            index = coach_number - 1
            if index < len(block) and block[index]:
                return str(block[index][0]).strip()
            return ""

        updates = []
        for coach_number in rows:
            name, pool_label = coaches.get(coach_number, ("", ""))
            row = first + coach_number - 1
            if existing(current_names, coach_number) != str(name).strip():
                updates.append({"range": f"{name_col}{row}", "values": [[name]]})
            if existing(current_pools, coach_number) != str(pool_label).strip():
                updates.append({"range": f"{pool_col}{row}", "values": [[pool_label]]})

        if updates:
            try:
                tab.batch_update(updates, value_input_option="USER_ENTERED")
            except Exception as error:
                raise _as_sheet_error(error, f"writing to the {layout['coaches_tab']} tab")

        return sum(1 for number in rows if coaches.get(number, ("", ""))[0])

    def full_sync(self, doc_url, tera_format, coaches, blocks, manage_rows=None):
        """Rewrite the coach list and every roster block from local data.

        `coaches` is {coach_number: (name, pool)} and `blocks` is
        {coach_number: [(pokemon, is_tera_captain), ...]}, both prepared by
        draft.export_for_sheet on the bot's main thread.

        The local database always wins: this only ever pushes outward, so a
        bad sheet can never corrupt a live draft.
        """
        if not self.can_write:
            raise SheetError(
                "Sheet writing is switched off. Set `SHEETS_WRITE_ENABLED = True` in "
                "config.py once the template layout has been confirmed."
            )
        if not doc_url:
            raise SheetError("This draft has no document configured.")

        layout = config.SHEET_LAYOUT
        rows = layout["roster_rows"]
        block_count = len(layout["roster_blocks"])

        written_coaches = self.write_coaches(doc_url, coaches, manage_rows)

        # Only rewrite the roster blocks the bot is responsible for, for the
        # same reason: an untouched block may be somebody else's business.
        managed = sorted(set(manage_rows) if manage_rows else set(blocks))
        updates = []
        for coach_number in managed:
            if not 1 <= coach_number <= block_count:
                raise SheetError(
                    f"Coach number {coach_number} has no roster block on the sheet.")
            entries = blocks.get(coach_number) or []
            padded = (list(entries) + [("", False)] * rows)[:rows]

            column, first_row = split_cell(layout["roster_blocks"][coach_number - 1])
            updates.append({
                "range": f"{column}{first_row}:{column}{first_row + rows - 1}",
                "values": [[name] for name, _ in padded],
            })
            if tera_format:
                tera_column, _ = split_cell(
                    shift_column(layout["roster_blocks"][coach_number - 1],
                                 layout["tera_flag_offset"]))
                updates.append({
                    "range": f"{tera_column}{first_row}:{tera_column}{first_row + rows - 1}",
                    "values": [[bool(flag)] for _, flag in padded],
                })

        tab = self._tab(doc_url, self.roster_tab_name(tera_format))
        try:
            tab.batch_update(updates, value_input_option="USER_ENTERED")
        except Exception as error:
            raise _as_sheet_error(
                error, f"writing to the {self.roster_tab_name(tera_format)} tab")

        self.forget_cache(doc_url)
        return {"coaches": written_coaches, "rosters": len(managed),
                "tab": self.roster_tab_name(tera_format)}

    # ---- match results --------------------------------------------------

    def match_rows(self, doc_url, week):
        """{coach name, normalised: the row their block starts on} for a week.

        The coach cells are formulas fed from the Schedule tab, so this is
        also the answer to "which side is this coach on this week?" - it is
        read rather than worked out, because the schedule decides it.
        """
        layout = config.SHEET_LAYOUT
        tab = self._tab(doc_url, layout["matches_tab"])
        column = column_letters(layout["matches_first_column"]
                                + layout["matches_week_width"] * (int(week) - 1))
        rows = [layout["matches_first_row"]
                + layout["matches_match_height"] * (side // 2)
                + layout["matches_side_offset"] * (side % 2)
                for side in range(layout["matches_sides"])]
        try:
            blocks = tab.batch_get([f"{column}{row}" for row in rows],
                                   value_render_option="FORMATTED_VALUE")
        except Exception as error:
            raise _as_sheet_error(error, f"reading week {week} of the "
                                         f"{layout['matches_tab']} tab")
        found = {}
        for row, block in zip(rows, blocks):
            name = str(block[0][0]).strip() if block and block[0] else ""
            if name:
                found.setdefault(normalize(name), (name, row))
        return found

    def write_match(self, doc_url, week, sides, replays=None):
        """Fill in one match: each side's Pokémon, their kills and deaths game
        by game, and each game's W/L.

        `sides` is [{"coach", "pokemon": [{"name", "games": [(kills, deaths)
        or None, ...]}], "results": ["W", "L", ""]}]. Only the cells the
        template leaves for data are written: the per-game kills and deaths,
        the names beside them and the W/L row. Every K/D, every total and the
        match result are formulas and are left alone.

        Returns {"tab", "rows": {coach: row}, "games": how many were written,
        "skipped_games", "overflow": {coach: [names that did not fit]}}.
        """
        if not self.can_write:
            raise SheetError(
                "Sheet writing is switched off. Set `SHEETS_WRITE_ENABLED = True` in "
                "config.py once the template layout has been confirmed.")
        if not doc_url:
            raise SheetError("This draft has no document configured.")

        layout = config.SHEET_LAYOUT
        first = layout["matches_first_column"] + layout["matches_week_width"] * (int(week) - 1)
        capacity = layout["matches_roster_rows"]
        most_games = layout["matches_games"]
        known = self.match_rows(doc_url, week)

        updates = []
        rows, overflow = {}, {}
        wanted_games = max((len(side["results"]) for side in sides), default=0)
        for side in sides:
            key = normalize(side["coach"])
            if key not in known:
                raise SheetError(
                    f"{side['coach']} is not on the {layout['matches_tab']} tab for week "
                    f"{week}. The coach names there come from the Schedule tab, so check "
                    f"the schedule (and `/sync`, which writes the names the bot uses).")
            _, row = known[key]
            rows[side["coach"]] = row

            entries = list(side["pokemon"])
            if len(entries) > capacity:
                overflow[side["coach"]] = [entry["name"] for entry in entries[capacity:]]
                entries = entries[:capacity]
            padded = entries + [None] * (capacity - len(entries))
            top = row + layout["matches_name_offset"]
            bottom = top + capacity - 1

            name_column = column_letters(first)
            updates.append({
                "range": f"{name_column}{top}:{name_column}{bottom}",
                "values": [[entry["name"] if entry else ""] for entry in padded],
            })

            for game in range(min(wanted_games, most_games)):
                kills = first + layout["matches_kills_column"] + \
                    layout["matches_game_columns"] * game
                deaths = first + layout["matches_deaths_column"] + \
                    layout["matches_game_columns"] * game
                values = []
                for entry in padded:
                    played = entry["games"][game] if entry and game < len(entry["games"]) else None
                    values.append(["", ""] if played is None else [played[0], played[1]])
                updates.append({
                    "range": f"{column_letters(kills)}{top}:{column_letters(deaths)}{bottom}",
                    "values": values,
                })
                result = side["results"][game] if game < len(side["results"]) else ""
                updates.append({
                    "range": f"{column_letters(kills)}{row + layout['matches_result_offset']}",
                    "values": [[result]],
                })

        for game, link in enumerate(list(replays or [])[:most_games]):
            if not link:
                continue
            band = min(rows.values()) + layout["matches_replay_offset"]
            column = column_letters(first + layout["matches_kills_column"]
                                    + layout["matches_game_columns"] * game)
            updates.append({"range": f"{column}{band}", "values": [[link]]})

        tab = self._tab(doc_url, layout["matches_tab"])
        try:
            tab.batch_update(updates, value_input_option="USER_ENTERED")
        except Exception as error:
            raise _as_sheet_error(error, f"writing week {week} to the "
                                        f"{layout['matches_tab']} tab")
        return {"tab": layout["matches_tab"], "rows": rows,
                "games": min(wanted_games, most_games),
                "skipped_games": max(0, wanted_games - most_games),
                "overflow": overflow}

    # ---- the schedule ---------------------------------------------------

    def read_schedule(self, doc_url, week):
        """Matchups for one week as [(side A, side B), ...]. Read only."""
        layout = config.SHEET_LAYOUT
        tab = self._tab(doc_url, layout["schedule_tab"])
        first = layout["schedule_first_row"]

        try:
            weeks, side_a, side_b = tab.batch_get(
                [f"{layout['schedule_week_column']}{first}:{layout['schedule_week_column']}",
                 f"{layout['schedule_side_a_column']}{first}:{layout['schedule_side_a_column']}",
                 f"{layout['schedule_side_b_column']}{first}:{layout['schedule_side_b_column']}"],
                value_render_option="UNFORMATTED_VALUE",
            )
        except Exception as error:
            raise SheetError(f"Could not read the {layout['schedule_tab']} tab: {error}") from error

        def cell(block, index):
            return str(block[index][0]).strip() if index < len(block) and block[index] else ""

        matches = []
        for offset in range(len(weeks)):
            raw_week = cell(weeks, offset)
            if not raw_week:
                continue
            try:
                if int(float(raw_week)) != int(week):
                    continue
            except ValueError:
                continue
            home, away = cell(side_a, offset), cell(side_b, offset)
            if home and away:
                matches.append((home, away))
        return matches

    # ---- inspection -----------------------------------------------------

    def describe(self, doc_url, doc_kind="template"):
        """Check a document against config.SHEET_LAYOUT. Changes nothing."""
        layout = config.SHEET_LAYOUT
        book = self._book(doc_url)
        titles = [w.title for w in book.worksheets()]
        report = {"title": book.title, "tabs": titles, "problems": [], "notes": []}

        if doc_kind == "tiering":
            entries = self.pool(doc_url, force=True)
            report["notes"] += self._price_notes(entries)
            report["notes"].append("Read as a tiering document: column A names, "
                                   "B prices, C Tera prices.")
            return report

        for role, tab_name in [("Settings", layout["settings_tab"]),
                               ("Coaches", layout["coaches_tab"]),
                               ("Rosters", layout["rosters_tab"])]:
            if tab_name not in titles:
                report["problems"].append(f"{role} tab `{tab_name}` is missing.")
        if report["problems"]:
            return report

        try:
            settings = self.read_settings(doc_url)
        except SheetError as error:
            report["problems"].append(str(error))
            return report

        tera_on = bool(settings.get("tera_format"))
        report["notes"].append(
            f"Settings: budget {settings.get('budget')}, Tera budget "
            f"{settings.get('tera_budget')}, pools {settings.get('pool_count')}, "
            f"Tera format {'on' if tera_on else 'off'}, "
            f"season {settings.get('season_weeks')} weeks."
        )

        tab_name = settings.get("tiering_sheet")
        if not tab_name:
            report["problems"].append("Settings does not name a tiering tab (`TieringSheet`).")
            return report
        if str(tab_name).strip() not in titles:
            report["problems"].append(
                f"Settings points at tiering tab `{tab_name}`, which does not exist.")
            return report

        report["notes"].append(f"Prices come from the `{tab_name}` tab.")
        report["notes"] += self._price_notes(self.pool(doc_url, tab=str(tab_name).strip(),
                                                       force=True))

        if tera_on and layout["tera_rosters_tab"] not in titles:
            report["problems"].append(
                f"Tera format is on but the `{layout['tera_rosters_tab']}` tab is missing.")
        report["notes"].append(
            f"Rosters: {len(layout['roster_blocks'])} blocks of {layout['roster_rows']} "
            f"rounds. Pool A uses blocks 1-{config.POOL_B_FIRST_COACH - 1}, pool B "
            f"{config.POOL_B_FIRST_COACH} upwards."
        )
        report["notes"].append(
            f"Picks would be written to `{self.roster_tab_name(tera_on)}`.")
        return report

    @staticmethod
    def _price_notes(entries):
        draftable = [e for e in entries if not e["banned"]]
        captains = [e for e in entries if not e["tera_banned"]]
        costs = sorted({e["cost"] for e in draftable})
        notes = [f"{len(entries)} names, {len(draftable)} draftable, "
                 f"{len(entries) - len(draftable)} banned or unpriced."]
        if costs:
            notes.append(f"Point values {costs[0]}–{costs[-1]}.")
        notes.append(f"{len(captains)} may be Tera Captains "
                     f"({len(entries) - len(captains)} Tera banned).")
        return notes


class DisabledSheets:
    """Stand-in used when Google Sheets is switched off entirely."""

    enabled = False
    can_write = False
    reason = "Google Sheets is not configured"

    def __getattr__(self, name):
        def unavailable(*args, **kwargs):
            raise SheetError(self.reason)
        return unavailable
