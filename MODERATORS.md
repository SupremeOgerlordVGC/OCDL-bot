# Draft moderator guide

For the people running drafts. Copy each block below and paste it into your
moderator channel — they are each under Discord's 2000-character message
limit, so paste them as separate messages.

The player-facing version is [`PLAYERS.md`](PLAYERS.md). Don't send moderators
that one instead; it deliberately leaves all of this out.

---

## Block 1 — how it fits together

```
**🔑 Moderator basics**

**Who can do this?** Anyone with Discord's **Administrator** permission, the server owner, or a role granted access with `/draft-moderator`.

**`/draft-moderator role:@Draft Mods`** — Administrator only. Grants that role every moderator command, server-wide. Run it with no options to see the current list. Add `remove:True` to revoke.

**The channel layout matters.** Two different kinds of channel:
• **The admin channel** — where you run `/setup`. Trades and substitution requests get sent here. One admin channel can run several drafts at once.
• **The draft channel(s)** — where players actually pick. Named *in* the `/setup` command, one per pool.

So: run `/setup` in your admin channel, and tell it which channel the drafting happens in. Not the other way round.

**Names identify a draft.** Every draft needs a unique name. Commands run from the admin channel take a `name:` option so the bot knows which one you mean. Commands run in a draft channel work it out from the channel.
```

---

## Block 2 — creating a draft

```
**📋 `/setup` — run this in your ADMIN channel**

**Required:**
• `name` — unique, e.g. `OCDL Season 1`
• `draft_channel` — where pool A drafts
• `min_picks` / `max_picks` — max_picks is the number of rounds (**12 max** with a template)
• `timer` — base seconds per pick, e.g. `600`
• `mode` — pick one:
   · **Full season** — draft, trades and schedule
   · **Draft + schedule** — no trades
   · **Draft only** — archives itself when the draft ends

**Players — one of these two:**
• `players: @A @B @C` — your order is the draft order
• `players_role: @Season 1` — the bot shuffles that role at random

**Optional:**
• `budget` — read from the template if you link one, so usually leave blank
• `tera_budget` — non-zero turns on Tera Captains (max 3 each)
• `max_trades` — the most Pokémon each player may pick up in trades over the season, 0 = unlimited
• `template_link` **or** `tiering_link` — one, never both
• `draft_channel_b` + `players_b` — a second pool (needs a template)
• `visibility` — **Public** (anyone can see it on draft.ogreclan.com) or **Private** (only its players, signed in with Discord). Public unless you choose. Change it any time with **`/visibility name:... setting:Private`**.
• `weeks` — season length when the bot makes its own schedule

`/edit` changes budget, picks, trade cap or the document — but only until `/draft` runs.
```

---

## Block 3 — starting it

```
**🚀 Starting the draft** *(run in the draft channel)*

**`/draft`** — starts immediately, pings the first player, starts their timer.

**`/draft at:<t:1760000000:F>`** — starts automatically at that moment. Paste a Discord timestamp and everyone sees it in their own timezone. Generate one at <https://hammertime.cyou>.

The channel gets a countdown, and players can `/queuepick round:1 ...` beforehand — so anyone who can't attend the opening still gets their first pick made. If everyone queues, round 1 plays itself.

A scheduled start survives a bot restart. `/draft` with no time starts it early and overrides the schedule.

**Two pools?** Run `/draft` separately in each pool's channel. They run independently, at the same time, with their own turns and timers.
```

---

## Block 4 — fixing things mid-draft

```
**🔧 When something goes wrong** *(run in the draft channel)*

**`/pick`** — you can pick for whoever is on the clock. Useful when somebody is cut off.

**`/skipturn`** — ends the turn of whoever is on the clock, exactly as their timer running out would: they owe a makeup pick and their timer is reduced. For when somebody is clearly away and nobody wants to wait out the clock. Add `penalty:False` to skip them without touching their timer, and `player:@Someone` to refuse unless that is really who is on the clock — turns move on while you type.

**`/editpick round:3 pokemon:Garchomp cost:12 player:@Someone`** — change a pick. Players may only fix their own until the next pick is made, so anything older is your job. **You** are never too late. You can also use it to **add** a pick for a round where they have nothing recorded, which settles any makeup pick they owed.

**`/reopendrafting player:@Someone`** — puts somebody who used `/finishdrafting` back on the clock. Rounds they were passed over for are gone, but nothing was charged for them: no makeup owed, no timer reduced.

**`/setskips player:@Someone skips:0`** — corrects their skip count and recalculates their timer from it, so `skips:0` restores their full base timer. Add `timer:300` to set the clock to an exact value. Use this when somebody was skipped unfairly.

**`/substitute out:@Leaving into:@Joining`** — hands over the seat. Roster, budgets and any makeup picks all stay; only the person changes. Their timer resets to the base.

**`/removeplayer player:@Someone`** — takes them out entirely. Before the draft starts their seat is deleted and the order closes up; once it's running the seat stays and gets passed over. Their Pokémon go back in the pool unless you add `keep_roster:True`.

Use `/substitute` to replace someone, `/removeplayer` when they're simply gone.
```

---

## Block 5 — players' queues

```
**📋 Queues** *(a queue is the list `/queuepick` drafts from automatically)*

**`/myqueue`** — run it **here in the admin channel** and you get dropdowns: pick the draft, the pool and the player, and their queue appears. In a draft channel it only ever shows your own, so the players have no idea this is possible. Keep it that way.

**`/cancelqueue player:@Someone`** — deletes somebody's queued picks before they fire, in a draft channel. Add `round:3` for just one round. Nothing else changes: no pick is undone and the clock is not touched. The channel is told, so the player knows their queue has gone.

A queue costs a player nothing to lose — it is only a plan for a turn they might miss.
```

---

## Block 6 — trades

```
**🤝 Trades** *(full-season drafts only)*

Players propose with `/trade` in their draft channel. It lands in the **admin channel** with a number.

**`/approvetrade trade_id:3`** — approves it. Rosters and budgets change at the **next `/postschedule`**, so a roster never changes mid-week. Only works once.
**`/rejecttrade trade_id:3`** — declines it. Nothing changes.
**`/trades`** — lists every trade and its status.

Add `name:OCDL Season 1` to any of these if your admin channel runs more than one draft.

**The limit counts Pokémon picked up**, not dropped: each player counts what they receive. Trades proposed in the **24 hours after a pool's draft ends** (the bot pings the pool when it opens) don't count at all. A player dropping nothing types `nil` or `0` for `drops`.

**The bot does not judge trades — you do.** It only checks the size limit and each player's trade cap. Anything odd — trading a Pokémon they don't own, claiming one already drafted — is attached to the message as a ⚠️ warning for you to read, not blocked.

**`/trade ... for_player:@Someone`** — file a trade on behalf of a player who can't manage the command. It still needs approving.

Trade numbers are **per draft**, so two leagues sharing an admin channel never both have a #1.
```

---

## Block 7 — spreadsheet and season

```
**📊 Spreadsheet and schedule**

**`/inspectsheet`** — reads only, changes nothing. Reports the tabs, which tiering tab is in use, how many Pokémon are draftable vs banned, and where picks would be written. **Run this before your first real draft.**

**`/sync`** — rewrites the coach list and every roster block from the bot's database. The database is always the source of truth; a failed sync never changes local data.

**Who's in the draft comes from the sheet.** On the **Coaches** tab, only rows with the **In schedule** box ticked take part — a tick reserves that coach's roster block, so gaps are respected. If a ticked row already has a username, the bot matches the player to it, so your @mention order doesn't have to match the sheet.

**`/postschedule week:3 name:OCDL Season 1`** — posts that week's matchups, pinging both players, and puts every approved trade into effect (listed under the matchups). Reads the template's schedule, or the bot's own if there's no template. Never automatic.
**`/reschedule name:... weeks:8`** — rerolls a bot-generated schedule.
```

---

## Block 8 — ending and housekeeping

```
**🗃️ Finishing up**

**`/archive name:OCDL Season 1`** — closes a finished draft and frees its channels for a new `/setup`. Rosters and history stay readable. Draft-only leagues do this themselves.

**`/canceldraft name:OCDL Season 1 confirm:CANCEL`** — abandons a draft. Nothing is deleted; its picks and rosters stay in the database.

**`/backup`** — saves a timestamped copy of the database. The bot also does this automatically every 6 hours, and nothing is ever lost to a restart: the draft reloads and the current player goes back on the clock.

**Worth knowing:** errors only ever appear to the person who typed the command. Real events — picks, skips, expired makeup picks, approved trades — are posted publicly. If the bot can't post in a draft channel it **pauses the clock** rather than skipping players, so a permissions mistake never costs anyone their timer.
```
