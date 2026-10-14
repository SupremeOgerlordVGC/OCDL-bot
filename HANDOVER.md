# Where things stand

Last brought up to date on 2026-10-14, after the paused work was finished.
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
| Deployed commit | `15dbd3a`, which is `main` apart from documentation |

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
* Cornerstone has four approved trades waiting to apply (#1, #2, #4, #6) and
  two rejected (#3, #5). They apply at its next `/postschedule`.
* `extensions` and `host_applications` exist and are empty.
* `current_week` is 0 everywhere: the accidental `/postschedule` on Cornerstone
  was reversed in full, and the fixtures it wrote were deleted.

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

**Applying to host** is wired end to end. The bot already refused unapproved
hosts; the site now shows the application form instead of a dead end, says
where an existing application stands, lets a refusal be answered by applying
again, and says plainly that none of it is needed to run a league on Discord.
`GET /api/hosts/me` and `POST /api/hosts/apply` pass through to the bot.

### The first automatic post

Cornerstone Season 1 is the only league whose draft has finished. Its grace
period runs until **2026-10-15 02:42 UTC** (pool B's window is the later of
the two), so its week 1 posts automatically then, applying the four approved
trades waiting on it (#1, #2, #4 and #6). Hearthflame and Teal are still
drafting, so nothing is due for them yet. To stop that happening, set
`auto_weeks = 0` on the league; to bring it forward, post week 1 by hand.

## Not started

* **A page on the site for past and extended matchups.** The data is ready and
  already mirrored: `fixtures` holds every week, and `extensions` says which
  matches were carried and from when. `apps/web/src/pages/discord-matches.tsx`
  is the view to extend. Another session works in that repo — coordinate.
* **Kills and deaths written to the league spreadsheet.** Larry's lowest
  priority, and he wants to study the template first, so it is deliberately
  untouched.
* **Showdown-room leagues** need a live check of the draft pages in the real
  client, and the auto points for the format reviewing. Playoffs and a
  standings export are possible extras. That is Showdown and site work.

## Open items

* **Two site changes are committed but not deployed.** In
  `/Users/Larry/Desktop/ogreclan`: `cbebb92` makes Voice follow
  `leagues.official` rather than only the `AUTO_VOICE_DISCORD_IDS` list, and
  `161de95` adds the apply-to-host form. That repo has **no git remote**, so
  both are local only, and deploying the site is the other session's workflow.
  Until they ship, an Ogre Clan server league made by somebody other than the
  owner is official and public but gets no Voice, and the apply form is not
  live.
* **The website's trades skip two of the bot's checks.** `/trade` resolves
  spelling against the tiering list and checks the budget before the admins
  see a trade, but those live in the cog, and `webapi.py` calls
  `rules.propose_trade` directly. The no-growth rule for substitutes was put
  in `propose_trade` for exactly this reason; the spelling and budget checks
  need the sheet, so moving them means handing `propose_trade` a price list.
* **The 🇦/🇧 votes on posted matchups are decoration.** Nothing reads them. If
  they are meant to settle results, they could feed the existing
  `/matchresult` and `/reportgame` rather than being counted by hand.
* **`/postschedule` has no confirmation.** It applies trades and advances the
  week immediately, which is how a week was posted by accident in this
  session. A confirm button listing what is about to apply would close that,
  the same shape as `/finishdrafting`.
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
