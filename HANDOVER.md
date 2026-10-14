# Where things stand

Written when work was paused on 2026-10-14. Everything below was verified at
that point, not assumed. The bot is running and healthy; one feature is
half-built and is described in full so it can be picked up cold.

## The live system

| | |
| --- | --- |
| Server | `root@188.245.40.24`, Ubuntu, Hetzner |
| Bot | `/home/draftbot/OCDL-Bot`, systemd unit `draftbot`, venv at `.venv/bin/python` |
| Database | `/home/draftbot/OCDL-Bot/draft.db` (SQLite, WAL) |
| Backups | `/home/draftbot/OCDL-Bot/backups/`, hourly plus one before each deploy |
| Deployed commit | `8d626c8` — one behind `main`, and the difference is `MODERATORS.md` only, so there is nothing to deploy |

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

* Scheduling channels are set for all three: Hearthflame `1497093133803196577`,
  Cornerstone `1535842881926070382`, Teal `1535843139514933370`.
* All three are **Ogre Clan Official** (`leagues.official = 1`) and public.
* Cornerstone has four approved trades waiting to apply (#1, #2, #4, #6) and
  two rejected (#3, #5). They apply at its next `/postschedule`.
* `extensions` and `host_applications` exist and are empty.
* `current_week` is 0 everywhere: the accidental `/postschedule` on Cornerstone
  was reversed in full, and the fixtures it wrote were deleted.

## Paused mid-change: automatic week posting

**What was asked.** Week 1 posts automatically once both pools have finished
drafting and their grace periods have ended. Every week after that posts in
all leagues at **Monday 20:00 GMT+8**, and week 1 is "extended to the
following Monday" — if the grace period ends on a Tuesday and week 1 goes up
then, week 2 is *not* the next Monday but the one after, so a week is never
cut short.

**Done and committed** (inert: nothing reads these yet):

* `config.WEEK_POST_UTC_OFFSET_HOURS = 8`, `WEEK_POST_WEEKDAY = 0` (Monday),
  `WEEK_POST_HOUR = 20`, `WEEK_GAP_DAYS = 7`.
* `leagues.week_posted_at` (TEXT) and `leagues.auto_weeks` (INTEGER, default 1),
  in the table, in `ADDED_COLUMNS` and in `pool_view`.

**Still to write**, with the decisions already made:

1. `rules.grace_over(conn, league_id, now)` — true once every pool of the
   league has `status = 'complete'` **and** its `free_trades_until` has passed.
   One-pool leagues are the same rule. A `draft_schedule` league has no trade
   window, so `free_trades_until` is NULL and only the draft matters.
2. `rules.next_week_time(after)` — the first Monday 20:00 GMT+8 that is at
   least `WEEK_GAP_DAYS` after `after`. GMT+8 never changes offset, so a fixed
   `timezone(timedelta(hours=8))` is correct; no DST handling needed. Worked
   example to test against: week 1 posted Tuesday → the next Monday is only
   six days later, so it is skipped and week 2 lands on the Monday after,
   thirteen days on. A week 1 posted Monday 19:00 is due the next Monday 20:00
   (seven days); posted Monday 21:00, the Monday after that.
3. `rules.leagues_due_a_week(conn, now)` — active leagues in a schedule mode
   with `auto_weeks = 1`, returning `[(league_row, week_number)]`. With
   `current_week = 0` it is week 1 and the test is `grace_over`; otherwise it
   is `current_week + 1` when `now >= next_week_time(week_posted_at)`. A league
   with no `week_posted_at` (every league today) is left alone until a week is
   posted once — a manual `/postschedule` sets the cadence going.
4. `rules.record_week_posted(conn, league_id)` — stamps `week_posted_at` and
   sets `auto_weeks = 1`. Call it from `AdminCog.post_week`, which both
   `/postschedule` and the web API already go through, so a manual post also
   sets the cadence and re-arms automatic posting.
5. `rules.stop_auto_weeks(conn, league_id)`.
6. A `@tasks.loop(minutes=1)` in `bot.py` beside `start_scheduled_drafts`
   (copy its shape, including the `before_loop` wait and the try/except around
   the lookup). For each due league: `cog = self.get_cog("AdminCog")`, then
   `await cog.post_week(league, week)`; a `None` result means the schedule has
   run out, so call `stop_auto_weeks`, say so in the admin channel, and carry
   on. Otherwise `await cog.publish_week(league, result, self.get_channel(league["admin_channel_id"]))`
   — the admin channel is the fallback because there is no interaction channel.
7. Tests. `next_week_time` is pure and deserves a table of cases, including
   the Tuesday example above. `leagues_due_a_week` can be driven by writing
   `week_posted_at` directly and passing an explicit `now`, so no clock games.

**Not started: the second half of that request** — "a place in the league UI
for players to view their past/extended matchups". That is the website
(`apps/web` and `apps/api` in `/Users/Larry/Desktop/ogreclan`), not the bot.
The data is all there and already mirrored to the site: `fixtures` holds every
week, and `extensions` says which matches were carried and from which week.
The site already has a bot-league Matches view to extend. **Another session is
actively working in that repo**, so coordinate rather than editing blind.

## Open items

* **The site change for Voice is committed but not deployed.** `cbebb92` in
  `/Users/Larry/Desktop/ogreclan` makes Voice follow `leagues.official`
  instead of only the `AUTO_VOICE_DISCORD_IDS` list. That repo has **no git
  remote**, so the commit is local only, and deploying the site and restarting
  Showdown is the other session's workflow. Until it ships, an Ogre Clan server
  league created by somebody other than the owner will be official and public
  but will not get Voice.
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
