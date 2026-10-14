# Where things stand

Last brought up to date on 2026-10-14, after trades were moved off
`/postschedule` and onto match reports, and match reporting was finished.
Everything below was verified at that point, not assumed. The bot is running
and healthy, nothing is half-built, and what has not been started is listed
plainly.

## The live system

| | |
| --- | --- |
| Server | `root@188.245.40.24`, Ubuntu, Hetzner |
| Bot | `/home/draftbot/OCDL-Bot`, systemd unit `draftbot`, venv at `.venv/bin/python` |
| Database | `/home/draftbot/OCDL-Bot/draft.db` (SQLite, WAL) |
| Backups | `/home/draftbot/OCDL-Bot/backups/`, hourly plus one before each deploy |
| Deployed commit | `ee8f2a3`, which is `main` |

**Deploy recipe** (what every deploy in this session used):

```bash
ssh root@188.245.40.24 'set -e
systemctl stop draftbot
sleep 1
sudo -u draftbot cp /home/draftbot/OCDL-Bot/draft.db /home/draftbot/OCDL-Bot/backups/pre-CHANGE-$(date -u +%Y%m%d-%H%M%S).db
cd /home/draftbot/OCDL-Bot && sudo -u draftbot git pull -q --ff-only
systemctl start draftbot'
```

Then check the log: every active pool should print `resumed with Ns left`, and
`logged in as Ogerpon#3940` should follow. A restart is safe at any time —
`resume_drafts` puts every clock back with its exact remaining time, and
treats a deadline that lapsed while the bot was down as the host's fault
rather than skipping the player. Deploys in this session took about six
seconds each.

**Migrations run on start** (`database.migrate`): any column in
`ADDED_COLUMNS` is added if missing and the views are rebuilt. A schema change
should be tested against a copy of the live database before deploying — there
is a worked example of doing that in this session's history, and it has caught
real breakage twice.

### League state

| League | Pools | Week | Notes |
| --- | --- | --- | --- |
| Hearthflame Season 1 | A and B drafting | 0 | official, public |
| Cornerstone Season 1 | both complete (league complete) | 0 | official, public |
| Teal Season 1 | A and B drafting | 0 | official, public; the only Tera-format league |

Week 1 posts itself for each league as its grace period ends — see "the first
automatic post" below for when Cornerstone's falls due.

* Scheduling channels are set for all three: Hearthflame `1497093133803196577`,
  Cornerstone `1535842881926070382`, Teal `1535843139514933370`.
* All three are **Ogre Clan Official** (`leagues.official = 1`) and public.
* Cornerstone has eight approved trades waiting (#1, #2, #4, #6, #8, #9, #10,
  #11) and three rejected (#3, #5, #7). They no longer apply on a posted week:
  a moderator puts them through with `/updatetrades`, or they land as each
  match is reported. None of them is blocked by another.
* Cornerstone pool A was told its free-transaction window had closed when this
  was deployed (`pools.grace_ended_at`); pool B's closes 2026-10-15 02:42 UTC
  and will be announced then.
* `extensions` and `host_applications` exist and are empty.
* `current_week` is 0 everywhere: the accidental `/postschedule` on Cornerstone
  was reversed in full, and the fixtures it wrote were deleted.

## When a trade takes effect

This changed on 2026-10-14 and is worth reading before touching trades.

Two things have to be true before an approved trade moves a Pokémon:

1. a moderator approved it, and
2. its moment has come — **its players finished the week's match**
   (`results.trades_after_match`, reached from `/reportgame`, `/reportreplay`
   and the site's `/gameresult`), or a moderator ran **`/updatetrades`**
   (player, pool or whole league).

`/postschedule` applies nothing now; it lists what is waiting. A substitute's
transactions still have their own moment, when their window closes.

"The match is over" means somebody has won `best_of // 2 + 1` games, which is
why `leagues.best_of` exists (3 by default, what the template's result formula
expects, settable in `/setup` and changeable later in `/edit`). A best-of-one
league left at three never finishes a match by itself, so its trades only move
on `/updatetrades` — the safe way round, but worth setting.

**Ordering.** Trades go in number order, and `blocking_trade` holds one back
while an earlier approved trade that shares a Pokémon is still waiting. That
is what makes picking up somebody's approved-but-unapplied drop safe
(`pending_drops`): the pickup is filed and lands after the drop. A second
claim on the same Pokémon is refused with the trade number that has it
(`pending_pickups`). `_apply_trade` is now defensive about ownership too — a
drop only touches the roster of the player giving it up, and a pickup that
does take something off another roster hands those points back — so budgets
add up whatever order things arrive in.

`ui.trades_landed` is the one place that finishes trades off: pricing a
free-agency pickup from the sheet, telling the pool, and rewriting the
spreadsheet. Everything that applies a trade goes through it.

## Reporting a match

`results.py` holds the rules, `cogs/results.py` the commands, `ui.py` the
messages, and `webapi.py` the same thing for draft.ogreclan.com. Games arrive
four ways - a Ready battle on the Showdown server, a replay link, a player
typing their own kills and deaths, or a Champions match reported by hand.

**`results.match_score` is the one answer to "where is this match?"**: each
player's game wins, how many are needed, who has won it, and whether it was
awarded. The reply, the result message and the trade rule all read it, and the
stored `match_reports.winner_id` follows the same rule, so the website shows
the match's winner rather than whoever happens to be ahead.

* A match is over when somebody has won a majority of its games
  (`leagues.best_of`). That is when `ui.finish_match` posts the result in the
  schedule channel and lands both players' trades - once, which is what its
  `was_decided` argument is for.
* A **new** game a finished match cannot have is refused (`require_open`),
  checked before the player's Pokémon are read so the answer is "that match is
  over" rather than "Garchomp isn't on your roster" after a trade landed.
  Correcting a game already recorded is always allowed, and `results.reportable`
  lets a player name what they played with even after it has left their roster.
* **`/setresult`** awards a match nobody played out (`walkover_id` + `note`):
  it finishes the match like any other result, trades and all. `clear:True`
  undoes it.
* **`/cleargame`** can reopen a finished match. Trades that already took
  effect stay in effect - the Pokémon have changed hands - and the command
  says so.

## Done since the pause

**Automatic week posting** is finished and deployed. Week 1 goes up once every
pool has drafted and the free-trade window after it has closed; every week
after that on the first Monday 20:00 GMT+8 that is at least seven days later,
so a season starting mid-week keeps a full first week. The schedule is read
from the database, so being down over a Monday does not lose a week. A week
with no matches stops automatic posting and says so in the admin channel;
posting one by hand arms it again. `AdminCog.post_week_due` holds the
per-league work so it can be tested, and `bot.post_due_weeks` is only the loop.

One thing to know: the due-leagues query covers leagues whose status is
`complete` as well as `active`, because a league is marked complete when its
*drafting* finishes — which is exactly when its season starts.

**Substitutes get a day of unlimited transactions.** Somebody taking a seat
over during or after a draft gets `SUB_TRADE_WINDOW_HOURS` (24) of transactions
that do not count against the trade cap, and which take effect the moment the
window closes rather than at the next posted week (`bot.apply_due_transactions`).
They may replace the team they inherited but not grow it — a trade in the
window must drop one for each it picks up, and that check is in
`propose_trade`, not the command's pre-flight, so the website is held to it too.
Subbing in before a draft starts opens no window; there is nothing to rebuild.

**Showdown Voice is tidy and site-managed.** Every `+` rank that had been
added by hand with `/roomvoice` or `/globalvoice` was adopted into the Voice
plugin's own record (`config/chat-plugins/ogreclan-voice.json`), so the site
now looks after all of it and will take it back when a league ends. The next
sync removed the three holders who are not league players (kirrabanana,
lilliantherian, ryanxo) and kept the other 19, none of whom lost voice for a
moment. Rooms stand at Teal 2, Cornerstone 11, Hearthflame 7; `~` and `@`
ranks were not touched. **ryanxo is a real Teal player** who has never linked
a Showdown account on draft.ogreclan.com, so the site cannot see them — they
get voice back by themselves within five minutes of linking. Backups of
`usergroups.csv`, `chatrooms.json` and the plugin file are stamped
`20261014-092124`.

**Applying to host** is wired end to end. The bot already refused unapproved
hosts; the site now shows the application form instead of a dead end, says
where an existing application stands, lets a refusal be answered by applying
again, and says plainly that none of it is needed to run a league on Discord.
`GET /api/hosts/me` and `POST /api/hosts/apply` pass through to the bot.

### The first automatic post

Cornerstone Season 1 is the only league whose draft has finished. Its grace
period runs until **2026-10-15 02:42 UTC** (pool B's window is the later of
the two), so its week 1 posts automatically then. It will **not** apply the
waiting trades any more — run `/updatetrades` for each pool once the window
closes, or let each land as its match is reported. Hearthflame and Teal are
still drafting, so nothing is due for them yet. To stop the posting, set
`auto_weeks = 0` on the league; to bring it forward, post week 1 by hand.

## Not started

* **A page on the site for past and extended matchups.** The data is ready and
  already mirrored: `fixtures` holds every week, and `extensions` says which
  matches were carried and from when. `apps/web/src/pages/discord-matches.tsx`
  is the view to extend. Another session works in that repo — coordinate.
* **Kills and deaths written to the league spreadsheet.** Larry's lowest
  priority, and the last piece of match reporting. The template has been read and written up in
  [`KD-NOTES.md`](KD-NOTES.md): the bot would only ever write into the
  `Matches` tab, in the per-game kills/deaths columns, and every other tab
  keeps itself up to date from it. That note also lists what is worth settling
  before building, including that best-of-three is baked into the result
  formula. No code yet.
* **Showdown-room leagues** need a live check of the draft pages in the real
  client, and the auto points for the format reviewing. Playoffs and a
  standings export are possible extras. That is Showdown and site work.

## Open items

* **The site reads a field the bot no longer sends.**
  `apps/web/src/pages/discord-matches.tsx` reads `applied` from the post-week
  reply to say "N trades now in effect"; posting a week applies nothing now,
  so the bot sends `waiting` instead. The field is only read for a message and
  `undefined` is falsy, so nothing breaks — the line is simply dead until the
  site is updated (that repo is another session's).
* **The website's trades skip two of the bot's checks.** `/trade` resolves
  spelling against the tiering list and checks the budget before the admins
  see a trade, but those live in the cog, and `webapi.py` calls
  `rules.propose_trade` directly. The no-growth rule for substitutes was put
  in `propose_trade` for exactly this reason; the spelling and budget checks
  need the sheet, so moving them means handing `propose_trade` a price list.
* **The 🇦/🇧 votes on posted matchups are decoration.** Nothing reads them. If
  they are meant to settle results, they could feed the existing
  `/matchresult` and `/reportgame` rather than being counted by hand — which
  now matters more, because a reported match is what lets trades land.
* **`/postschedule` has no confirmation.** It advances the week immediately,
  which is how a week was posted by accident in this session. It no longer
  moves any Pokémon, so an accident is much cheaper to undo than it was, but a
  confirm button the shape of `/finishdrafting`'s would still close it.
* **A stale pre-leagues `draft.db` sits in the repo folder on the Mac.** It has
  a `drafts` table and `picks.draft_id` from before the pools refactor, so
  `database.connect()` dies on it with `no such column: pool_id`. Only affects
  running the bot locally; the server's database is healthy.
* **`tests/test_webapi.py`, `webapi.py` and `results.py` belong to another
  session's work.** They are in this repo and this branch, so `git add -A`
  sweeps them up — stage files explicitly.

## Working agreements worth keeping

* **Stage named files, never `git add -A`.** Two sessions commit to this branch.
  Early in this session an `add -A` swept another session's work into a commit
  whose message said nothing about it.
* **Verify against the live system, not from memory.** Every bug found today
  was found by reading the real database or the real spreadsheet: a queued
  Tera Captain that the sheet never ticked, a trade priced from the wrong
  league's tier list, `export_for_sheet` about to wipe a finished coach's team.
* **Test the Discord layer by invoking it.** `tests/test_flow.py` has a
  `FakeInteraction`, a `FakePosted` message that records reactions, and fakes
  for the owner checks. Three bugs this session only existed in command bodies
  and could not fail until somebody actually ran them.
* **Keep every paste block under 1900 characters.** `PLAYERS.md` and
  `MODERATORS.md` are copied into Discord one fenced block per message, and
  the limit is 2000. Both files have been split when a block got close.
