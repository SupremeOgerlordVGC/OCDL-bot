# Kills and deaths on the league template

Notes from reading **OCDL Standard Template**
(`1q3jJUIIjkt_lFsUSXeZOOtHkNr08AGkSPETf91yqJUY`) on 2026-10-14. Nothing is
built yet; this is what the sheet expects, so that writing to it is a matter of
filling cells rather than guessing.

## Everything flows from one tab

```
Matches  →  BackendMatchLog  →  BackendPokemonStandings  →  Pokémon Leaderboard
   (entered)      (ARRAYFORMULA)        (SUMIF per pool|Pokémon)     (SORT)
                                     →  League Standings / BackendRankings
```

**The bot only ever writes into `Matches`.** Every other tab is formulas and
keeps itself up to date. That is the main finding: there is no separate
kills-and-deaths tab to maintain.

## The `Matches` grid

`BackendMatchLog!A2` is the authoritative description of the layout, and the
sheet matches it. For week `w` and side `s` (0-19, so ten matches a week):

| | |
| --- | --- |
| Week block's first column | `col = 2 + 16 * (w - 1)` — week 1 is **B**, week 2 **R**, week 3 **AH** |
| The side's coach row | `cRow = 4 + 26 * floor(s / 2) + 11 * (s mod 2)` |

So sides 0 and 1 (one match) sit at rows 4 and 15, with `Replays` at row 26;
the next match is rows 30 and 41, and each match band is 26 rows.

Within a side whose coach row is `C`, in the week's own columns:

| Cell | What | Writable? |
| --- | --- | --- |
| `(C, col)` | coach's name | **No** — `=INDEX(Schedule!…)`, it comes from the schedule |
| `(C+3 … C+8, col)` | the six Pokémon they brought | yes |
| `(row, col+1)` / `(row, col+2)` | game 1 kills / deaths | yes |
| `(row, col+4)` / `(row, col+5)` | game 2 kills / deaths | yes |
| `(row, col+7)` / `(row, col+8)` | game 3 kills / deaths | yes |
| `(row, col+3 / +6 / +9)` | per-game K/D | **No** — `=kills - deaths` |
| `(row, col+10)` / `(row, col+11)` | total kills / deaths | **No** — `=SUM` of the three games |
| `(C+9, …)` | the Total row | **No** — sums the six Pokémon |
| `(C+10, col+1 / +4 / +7)` | each game's `W`/`L` | yes |
| `(C+10, col+10)` | the match result | **No** — `=IF(COUNTIF(…,"W")=2,"W",…)` |
| `(C+22, col)` | `Replays` label | row to put replay links on |

Writing a total or a K/D would overwrite a formula, so a writer must stay in
the per-game columns and let the sheet add up.

## To write a match, the bot needs

1. **The week** → the column block.
2. **Which side the coach is on that week.** The coach cell is a formula from
   `Schedule`, so read the 20 coach cells for that week and find the one whose
   value matches the coach. Do not assume seat order: the schedule decides it.
3. **The six Pokémon each player brought**, in the roster's own spelling —
   `rules.normalize` already exists for matching what Showdown reports to what
   the roster says.
4. **Per game**: each Pokémon's kills and deaths, and the game's `W`/`L`.

`ogreclan-kdtracker` already produces kills and deaths per battle, and the
site's match records say which battle belonged to which league match, so the
automatic path is: tracker → site → the bot's web API → these cells.

## Worth settling before building

* **Best of three is baked into the result formula** (`COUNTIF(…,"W") = 2`). A
  best-of-one league would never show a result, so either those leagues enter
  the same result twice, their template differs, or the formula needs a look.
  Larry's note says VGC is best of 3 and singles usually best of 1.

  The bot now knows the answer for its own purposes: `leagues.best_of`
  (default 3, set in `/setup`, changeable in `/edit`) decides when a match is
  over, which is what `results.match_score` reports and what lets the week's
  trades land. A writer should take the games per match from there rather than
  assuming three — and a best-of-one league's template still needs checking
  against that formula before anything is written to it.
* **`SeasonWeeks`** is a named range and bounds how many weeks the log reads.
  A season longer than it will silently stop appearing in the standings.
* **A walkover has no games.** `/setresult` awards a match that was never
  played (`match_reports.walkover_id`, with `note` for the reason), so it has
  a winner and no kills or deaths at all. Whatever writes to the sheet has to
  put the result in without inventing game rows.
* **Who writes the Pokémon names** — they are blank in the template, so
  presumably whoever reports the match. If a player reports by hand and the
  bot writes automatically, the two must not fight over the same six rows.
* **Champions matches** are played off-server, so they can only ever be
  reported by hand; the automatic path has to leave them alone.
