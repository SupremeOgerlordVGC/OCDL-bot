# Getting a bot league into the Draft Planner

Written 2026-10-16, after reading the Showdown draft plugin and the site. No
code yet. The question was "what would it cost?", and the answer turned out
to be "much less than feared, and not in the Showdown repo at all".

## What the planner actually imports

The Draft Planner (`server/chat-plugins/draft/planner.ts`, and the Chrome
extension in `ogreclan/apps/extension`) imports a league by opening its pages
on OgreClan's Showdown server. So the league has to exist as a **draft plugin
league**.

**But the plugin does not own that data — the site's database does.**
`server/chat-plugins/draft/sync.ts` says it plainly: leagues live in
OgreClan's database and are loaded from it on start, through

* `GET /internal/leagues` → `{ roomid, data: DraftLeague }[]`
* `PUT /internal/leagues/:roomid`
* `DELETE /internal/leagues/:roomid`

served by `ogreclan/apps/api/src/leagues.ts` (lines ~186-220). A local JSON
copy is kept only for when the database is unreachable.

`DraftLeague` is the shared type in `packages/league-core/src/types.ts`:

```
roomid, phase, settings, pool {speciesid: points}, players[], picks[],
pickNumber, pickDeadline, schedule: DraftMatch[][], week, trades[],
nextTradeId, transactions[]
```

## So the bridge is a mapper, in the site

The bot's leagues are already mirrored into the site's database and already
mapped once, into `BotLeague`, by `apps/api/src/bot-leagues.ts` (rosters,
costs, players, trades, **fixtures**). Everything the planner needs is there.

The bridge is therefore:

1. **`BotLeague` → `DraftLeague`** — one new module in `apps/api/src`. The
   fiddly parts are `pool` (speciesid → points, from the tiering list, which
   the bot reads from the spreadsheet and the site does not have yet),
   `players`/`picks` (seat ids → Showdown userids, which needs the Showdown
   account each coach linked — `auth.identities`, already used for Voice), and
   `schedule` (straight from `fixtures`).
2. **A write** of that league into the same table `/internal/leagues` serves,
   under a roomid per league (say `hearthflameseason1`).
3. **Published read-only.** The Showdown server *runs* the leagues it loads,
   so a bot league must arrive in a phase where nothing picks or times out —
   otherwise two systems own the same draft. Worth reading `DraftPhase` and
   the `onLoad` hook in `sync.ts` before choosing.
4. **A trigger**: on the mirror refresh, so it follows picks, trades and weeks
   without anybody asking.

Nothing needs changing in `pokemon-showdown` itself, which is where the cost
was going to be.

## What it would cost

* Step 1 is the real work: the species-id mapping and the identity mapping.
  Pokémon names have to become Showdown species ids (`league-core` has
  `setSpeciesNames`, but on the site there is no Dex), and a coach with no
  linked Showdown account has no userid - the same gap that keeps ryanxo off
  Voice.
* Steps 2-4 are small.
* Then a live check in the real client, which is the item in `HANDOVER.md`.

Rough shape: one working session for steps 1-2 behind a `/publishleague`
command, a second for the trigger and the client check. The unknowns that
could stretch it are the two mappings above, not the plumbing.

## Cheaper half-measures, if that is too much

* **Schedule only.** The planner's value for week 1 is "who do I play". A
  league published with players and `schedule` but an empty `pool`/`picks`
  would still answer that, and skips the species-id mapping entirely.
* **Nothing.** The matchups are already on draft.ogreclan.com and in each
  league's scheduling channel; the planner is a convenience.
