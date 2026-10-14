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
• `schedule_channel` — where each week's matchups are posted. Can be set later with `/schedulechannel`.
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

## Block 6 — trades: deciding them

```
**🤝 Trades** *(full-season drafts only)*

Players propose with `/trade` in their draft channel. It lands in the **admin channel** with a number, the budget arithmetic, and any warnings.

**`/approvetrade trade_id:3`** — approves it. Nothing moves yet: it takes effect when **both its players have finished that week's match** (reported with `/reportgame`, `/reportreplay` or on the site), so no roster changes under a match being played with it. Only works once.

**`/updatetrades`** — puts every approved trade through **now**, without waiting for a match. Run in a pool's channel it does that pool; add `pool:B` or `player:@Someone` to narrow it. This is how trades land during the pre-season grace period, and how you unstick a match nobody is going to report.

**`/rejecttrade trade_id:3`** — declines it. Nothing changes.

**`/trades`** — lists every trade and its status.

Add `name:OCDL Season 1` to any of these if your admin channel runs more than one draft. Trade numbers are **per draft**, so two leagues sharing an admin channel never both have a #1.

**What the bot refuses before you ever see it:** a name that is not on the tiering list, a Pokémon with no price, one the proposer does not own, a free-agency pickup that is genuinely somebody else's, and anything either side cannot afford. Something another coach has an **approved drop** for counts as available — the pickup is filed and lands after their drop does. Names are stored in the tiering list's own spelling, so `rotom-mow` is filed as `Rotom-Mow`.

**What is still yours to judge:** whether a trade is *fair*. Oddities — a roster going over size, somebody dropping nothing, a partner receiving nothing — are attached as ⚠️ warnings, not blocked.
```

---

## Block 7 — trades: fixing them

```
**🔧 When a trade needs fixing**

**`/edittrade trade_id:3 drops:Garchomp gets:Rotom-Wash`** — rewrites both sides of a trade that is nearly right, instead of making the player retype it. The edit is held to all the same rules, and an edited trade **goes back to pending** — you approved the Pokémon in front of you, so changed ones need a fresh decision. Both channels are told.

**`/unapprovetrade trade_id:3`** — takes back an approval given by mistake, putting it back to pending. Only works while it is still waiting: approving moves nothing, so there is nothing to undo. Once it has taken effect the Pokémon have changed hands and you need a fresh `/trade` to move them back.

**`/trade ... for_player:@Someone`** — file a trade on behalf of a player who cannot manage the command. It still needs approving.

**⭐ A Tera Captaincy does not travel with a trade** *(Tera formats)*. Whoever gives one up has their Tera points back; the coach receiving it makes it a captain themselves with **`/teracaptain pokemon:...`**, which charges the Tera price against both budgets. That command is also the only way a Pokémon somebody traded for can ever become a captain. **Taking one off is yours**: `/teracaptain pokemon:... on:False for_player:@Someone`, which gives both budgets their points back.

**🔁 Substitutes get 24 hours of unlimited transactions** when they take over during or after a draft. Theirs don't count against the trade cap, and they take effect when the window closes — there is no match of their own to wait for, so approve them promptly. They can replace the team they inherited but not grow it.

**The limit counts Pokémon picked up**, not dropped: each player counts what they receive. Trades proposed in the **24 hours after a pool's draft ends** (the bot pings the pool when it opens) don't count at all. A player dropping nothing types `nil` or `0` for `drops`.
```

---

## Block 8 — the spreadsheet

```
**📊 Spreadsheet and schedule**

**`/inspectsheet`** — reads only, changes nothing. Reports the tabs, which tiering tab is in use, how many Pokémon are draftable vs banned, and where picks would be written. **Run this before your first real draft.**

**`/sync`** — rewrites the coach list and every roster block from the bot's database. The database is always the source of truth; a failed sync never changes local data.

**Who's in the draft comes from the sheet.** On the **Coaches** tab, only rows with the **In schedule** box ticked take part — a tick reserves that coach's roster block, so gaps are respected. If a ticked row already has a username, the bot matches the player to it, so your @mention order doesn't have to match the sheet.
```

---

## Block 9 — the season

```
**📅 Running the season**

**`/postschedule week:3 name:OCDL Season 1`** — posts that week's matchups. It moves no Pokémon: approved trades land when their players report the match, or on `/updatetrades`, and any still waiting are listed underneath. Reads the template's schedule, or the bot's own.

**Each match gets its own message, with 🇦 and 🇧 on it to vote with.** They go to the league's **schedule channel**; set one with `/schedulechannel name:OCDL Season 1 channel:#schedule`, or with `schedule_channel` in `/setup`. With none set they post wherever you run the command, and the bot tells you so. I need **Add Reactions** in that channel for the votes.

**📅 Weeks post themselves.** Week 1 goes up once **both pools have finished drafting and the free-trade window after it has closed**. Every week after that is posted on **Monday at 20:00 GMT+8**, never less than seven days after the last one — so a season starting mid-week keeps a full first week (week 1 on a Tuesday means week 2 the Monday after next). If the bot is down over a Monday the week goes up as soon as it's back. When a week has no matches it assumes the season is over, stops, and tells you here; posting one by hand starts it again.

**`/reschedule name:... weeks:8`** — rerolls a bot-generated schedule.

**⏳ Extensions.** A player runs `/extension` for their current week's match; it arrives here with a number. **`/grantextension number:2 name:...`** posts that match again with the following week, **`/rejectextension`** leaves it where it is, and both players are told in the schedule channel either way. You don't need a request: **`/grantextension player:@Someone name:...`** extends that player's match for this week outright. **`/extensions`** lists them all. One week is the limit, so a carried-over match can't be put off again.
```

---

## Block 10 — results

```
**📊 Results**

Players report their own matches: `/reportreplay` (a replay fills in both sides), `/reportgame` (their own Pokémon's kills and deaths), `/champions` and `/pokepaste` for a match played on Pokémon Champions. The site offers the same.

**A match is over when somebody has won a majority of its games** — two of three by default, set per league with `best_of` in `/setup` or `/edit`. That moment matters: the result is posted in the scheduling channel, and **both players' approved trades take effect**.

**`/matchresult week:1 player:@Someone`** — everything reported for that match, with the score.

**`/setresult week:1 player:@Alex reason:opponent never showed`** — awards a match that was not played out: a forfeit, a no-show, a result settled elsewhere. It finishes the match like any other result, so the trades land too. `clear:True` takes the award back and leaves whatever the games say.

**`/cleargame week:1 game:2 player:@Someone`** — removes a game so it can be reported again. If that reopens a finished match, trades that already took effect **stay** in effect: the Pokémon have changed hands, so put them back with a fresh `/trade` if that is wrong.

The bot refuses a game a finished match cannot have — no game 4 in a best of three, nothing after 2-0 — but a game already recorded can always be corrected, which is how players fix a mistake. That still works after a trade has changed their team.
```

---

## Block 11 — ending and housekeeping

```
**🗃️ Finishing up**

**`/archive name:OCDL Season 1`** — closes a finished draft and frees its channels for a new `/setup`. Rosters and history stay readable. Draft-only leagues do this themselves.

**`/canceldraft name:OCDL Season 1 confirm:CANCEL`** — abandons a draft. Nothing is deleted; its picks and rosters stay in the database.

**`/backup`** — saves a timestamped copy of the database. The bot also does this automatically every 6 hours, and nothing is ever lost to a restart: the draft reloads and the current player goes back on the clock.

**Worth knowing:** errors only ever appear to the person who typed the command. Real events — picks, skips, expired makeup picks, approved trades — are posted publicly. If the bot can't post in a draft channel it **pauses the clock** rather than skipping players, so a permissions mistake never costs anyone their timer.
```

---

## Block 12 — what's new

For moderators who already have the blocks above. Everything here is also written into them, so a moderator reading the guide fresh does not need this one. It is two messages.

```
**🆕 What's new (1/2) — trades land on match reports**

**When an approved trade actually moves**
• Not when a week is posted any more. Two things have to be true: **you approved it**, and **its players have finished that week's match** — reported with `/reportgame`, `/reportreplay` or on the site. The team that starts a match finishes it.
• **`/updatetrades`** — puts them through now, without waiting. In a pool's channel it does that pool; add `pool:B` for the other one, or `player:@Someone` for just them. This is what you use during the **grace period before week 1**, and when a match is never going to be reported.
• **`/postschedule`** now only posts matchups. It lists anything still waiting underneath.
• `/trades` shows each one as *waiting for their match* — or *waiting for #6* when an earlier trade has to go first.

**Dropped Pokémon can be picked up straight away**
• A coach may now pick up something another coach has an **approved drop** for, even though it is still on their roster. The pickup is filed and lands **after** that drop does, automatically and in the right order. Nobody has to use a placeholder and wait any more.
• Two coaches cannot both claim it: once one approved trade is taking it, the next is refused with the trade number that has it.
```

```
**🆕 What's new (2/2) — finishing a match, and Tera Captains**

**Tera Captains for a traded Pokémon**
• **`/teracaptain pokemon:...`** — a player makes something already on their roster a Tera Captain, charged its Tera price against both budgets. A pick carried that choice and a trade never did, so until now a Pokémon somebody traded for could not be one at all. It is announced in the pool.
• A captaincy **does not travel with a trade**: whoever gives one up has their Tera points back, and the new owner nominates it themselves. Taking one off is yours — `on:False`, plus `for_player:` for somebody else.

**Finishing a match**

• **A match is over when somebody has won a majority of its games** — two of three by default. **`/setup best_of:1`** or **`/edit best_of:1`** for a league that plays single games; `/edit` takes it mid-season. A best-of-one league left at three never finishes by itself, so its trades would only move on `/updatetrades`.
• **The result is posted in the scheduling channel** when a match ends, with the score, and that is when the trades land.
• **`/setresult week:1 player:@Alex reason:no-show`** — awards a match nobody played out. It finishes the match like a real result, trades and all; `clear:True` undoes it.
• **`/cleargame`** now shows what is left and warns you if it reopened a finished match. Trades that already landed stay landed — the Pokémon have changed hands.
• The bot refuses a game a finished match cannot have (no game 4 in a best of three), but a game already recorded can always be corrected.
• **The end of a pool's free-transaction window is now announced** in its channel, saying what changes. It used to pass in silence.
```
